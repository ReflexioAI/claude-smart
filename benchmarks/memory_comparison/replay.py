"""Direct-injection replay for benchmark scenarios.

Context: nested ``claude -p`` does not fire plugin hooks, so we can't drive
both systems via a real Claude CLI session. Instead, we push the same
synthetic multi-turn conversation into each system's **native ingestion
path**:

- **claude-smart** → ``reflexio.ReflexioClient.publish_interaction`` with
  ``force_extraction=True``; reflexio's real LLM extraction runs and
  produces playbooks + profiles for the scenario's project_id.
- **claude-mem** → a fresh ``sdk_sessions`` row plus one ``pending_messages``
  row per turn, dropped straight into the SQLite queue; the already-running
  claude-mem worker picks them up and runs its real LLM extraction to
  produce observations.

Both systems see identical text for each scenario, so any quality
difference reflects extraction/retrieval, not capture scaffolding.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parents[1]
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from claude_smart.reflexio_adapter import Adapter  # noqa: E402

_LOGGER = logging.getLogger(__name__)
SCRATCH_ROOT = Path("/tmp/membench")
_MEM_PLUGIN_DIR = Path.home() / ".claude/plugins/marketplaces/thedotmack/plugin"
_MEM_WORKER_CJS = _MEM_PLUGIN_DIR / "scripts/worker-service.cjs"
_MEM_BUN_RUNNER = _MEM_PLUGIN_DIR / "scripts/bun-runner.js"


@dataclass
class ReplayResult:
    """Outcome of a scenario's direct injection into both systems.

    Attributes:
        scenario_id (str): Matches ``Scenario.id``.
        project_dir (Path): Used as both systems' project key.
        session_id (str): Freshly generated UUID, reused as reflexio user_id.
        smart_ok (bool): True if reflexio publish succeeded.
        mem_ok (bool): True if claude-mem rows were inserted.
        errors (list[str]): Per-system error strings (empty on success).
    """

    scenario_id: str
    project_dir: Path
    session_id: str
    smart_ok: bool
    mem_ok: bool
    errors: list[str]


def scratch_dir_for(scenario_id: str) -> Path:
    """Return the isolated working dir used as project key for both systems.

    Args:
        scenario_id (str): Stable slug from ``Scenario.id``.

    Returns:
        Path: The scratch dir (created if absent).
    """
    target = SCRATCH_ROOT / scenario_id
    target.mkdir(parents=True, exist_ok=True)
    return target


def _build_assistant_replies(turns: tuple[str, ...]) -> list[str]:
    """Produce plausible one-line assistant replies that echo the user turn.

    Both extraction pipelines need a back-and-forth conversation to work on.
    We use a lightweight template rather than calling Claude — the goal is
    to give each system the *user's stated preferences* to extract, not to
    test the quality of the assistant's answers.

    Args:
        turns (tuple[str, ...]): User messages from the scenario.

    Returns:
        list[str]: One reply per user turn (same length as ``turns``).
    """
    # Keep replies short and content-free so the extractor doesn't treat
    # them as evidence of agent behavior. Earlier templates that quoted the
    # user turn verbatim caused claude-smart's playbook extractor to
    # generate meta-playbooks about "the agent echoes user input" instead
    # of capturing the user's stated rule.
    return ["Got it." for _ in turns]


def _inject_claude_smart(
    *, project_dir: Path, session_id: str, turns: tuple[str, ...]
) -> tuple[bool, str | None]:
    """Publish a scenario's turns as interactions to reflexio.

    Args:
        project_dir (Path): Scenario project_id / reflexio agent_version.
        session_id (str): UUID reused as reflexio user_id and session_id.
        turns (tuple[str, ...]): User turns for the scenario.

    Returns:
        tuple[bool, str | None]: ``(ok, error)``.
    """
    replies = _build_assistant_replies(turns)
    interactions: list[dict] = []
    for user_turn, assistant_reply in zip(turns, replies):
        interactions.append({"role": "User", "content": user_turn})
        interactions.append({"role": "Assistant", "content": assistant_reply})

    adapter = Adapter()
    # Use production publish() so user_id == project_id == agent_version.
    # Passing user_id=session_id directly to the underlying client would
    # store profiles under a different key than the one
    # search_profiles(project_id=...) reads — making every captured profile
    # invisible to retrieval. Going through the adapter mirrors the
    # production hook's wiring exactly.
    ok = adapter.publish(
        session_id=session_id,
        project_id=str(project_dir),
        interactions=interactions,
        force_extraction=True,
    )
    if not ok:
        return False, "Adapter.publish failed"
    return True, None


def _write_fake_transcript(
    *, project_dir: Path, turns: tuple[str, ...], replies: list[str]
) -> Path:
    """Write a Claude Code-format transcript the worker can read.

    claude-mem's observation hook reads ``last_user_message`` /
    ``last_assistant_message`` from the transcript file, not from the hook
    payload. We lay down a valid-shape JSONL containing the scenario's full
    conversation so the worker's extraction model sees the user's stated
    preferences.

    Args:
        project_dir (Path): Scenario scratch dir; transcript lives under it.
        turns (tuple[str, ...]): User turns from the scenario.
        replies (list[str]): Matching assistant replies.

    Returns:
        Path: Absolute transcript path to pass in each hook payload.
    """
    path = project_dir / "transcript.jsonl"
    lines: list[str] = []
    for idx, (user_turn, reply) in enumerate(zip(turns, replies), start=1):
        lines.append(
            json.dumps(
                {
                    "type": "user",
                    "uuid": f"u{idx}",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": user_turn}],
                    },
                }
            )
        )
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": f"a{idx}",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": reply}],
                    },
                }
            )
        )
    path.write_text("\n".join(lines) + "\n")
    return path


def _run_mem_hook(
    *, event: str, payload: dict, timeout_s: int = 120
) -> tuple[bool, str | None]:
    """Pipe a hook payload into ``worker-service.cjs hook claude-code <event>``.

    Args:
        event (str): Either ``session-init`` or ``observation``.
        payload (dict): Claude Code hook JSON to deliver on stdin.
        timeout_s (int): Per-invocation cap.

    Returns:
        tuple[bool, str | None]: ``(ok, error)``.
    """
    cmd = [
        "node",
        str(_MEM_BUN_RUNNER),
        str(_MEM_WORKER_CJS),
        "hook",
        "claude-code",
        event,
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=json.dumps(payload),
            env=dict(os.environ),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, f"{event}: {exc}"
    if proc.returncode != 0:
        return False, f"{event}: exit {proc.returncode} stderr={proc.stderr[-200:]}"
    return True, None


def _inject_claude_mem(
    *, project_dir: Path, session_id: str, turns: tuple[str, ...]
) -> tuple[bool, str | None]:
    """Drive claude-mem through its real hook pipeline for the scenario.

    Flow per scenario:
        1. Lay down a fake but valid transcript JSONL.
        2. Fire ``session-init`` once (creates the sdk_sessions row).
        3. Fire ``observation`` per turn (queues a pending_messages row that
           the worker's LLM extraction will consume to produce observations).

    Args:
        project_dir (Path): Scenario cwd / project key.
        session_id (str): Claude Code content_session_id.
        turns (tuple[str, ...]): User turns.

    Returns:
        tuple[bool, str | None]: ``(ok, first-error)``.
    """
    replies = _build_assistant_replies(turns)
    transcript = _write_fake_transcript(
        project_dir=project_dir, turns=turns, replies=replies
    )
    base_payload = {
        "session_id": session_id,
        "cwd": str(project_dir),
        "transcript_path": str(transcript),
    }
    ok, err = _run_mem_hook(
        event="session-init",
        payload={
            **base_payload,
            "hook_event_name": "UserPromptSubmit",
            "prompt": turns[0],
        },
    )
    if not ok:
        return False, err

    for idx, (user_turn, reply) in enumerate(zip(turns, replies), start=1):
        ok, err = _run_mem_hook(
            event="observation",
            payload={
                **base_payload,
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": f"# turn {idx}: {user_turn[:120]}"},
                "tool_response": {"output": reply[:200]},
            },
        )
        if not ok:
            return False, err
    return True, None


def replay_scenario(*, scenario_id: str, turns: tuple[str, ...]) -> ReplayResult:
    """Inject a scenario into both systems and return per-system success.

    Args:
        scenario_id (str): Slug used to pick the scratch project dir.
        turns (tuple[str, ...]): User messages delivered in order.

    Returns:
        ReplayResult: Includes the generated ``session_id`` and the per-
            system success flags; always returns (never raises) so the
            orchestrator can move on to scoring even if one side failed.
    """
    project_dir = scratch_dir_for(scenario_id)
    session_id = str(uuid.uuid4())
    errors: list[str] = []

    smart_ok, smart_err = _inject_claude_smart(
        project_dir=project_dir, session_id=session_id, turns=turns
    )
    if smart_err:
        errors.append(f"claude-smart: {smart_err}")

    mem_ok, mem_err = _inject_claude_mem(
        project_dir=project_dir, session_id=session_id, turns=turns
    )
    if mem_err:
        errors.append(f"claude-mem: {mem_err}")

    _LOGGER.info(
        "replay %s project=%s session=%s smart=%s mem=%s",
        scenario_id,
        project_dir,
        session_id,
        smart_ok,
        mem_ok,
    )
    return ReplayResult(
        scenario_id=scenario_id,
        project_dir=project_dir,
        session_id=session_id,
        smart_ok=smart_ok,
        mem_ok=mem_ok,
        errors=errors,
    )
