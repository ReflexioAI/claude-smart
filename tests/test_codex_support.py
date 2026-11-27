"""Tests for Codex plugin packaging and host-specific hook behavior."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

from claude_smart import cs_cite, hook, runtime, state
from claude_smart.events import post_tool, pre_tool, stop

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_json(path: str) -> dict[str, Any]:
    return json.loads((REPO_ROOT / path).read_text())


def _command_hooks(path: str) -> list[str]:
    parsed = _read_json(path)
    commands: list[str] = []
    for entries in (parsed.get("hooks") or {}).values():
        for entry in entries:
            for hook_def in entry.get("hooks") or []:
                if hook_def.get("type") == "command":
                    commands.append(str(hook_def.get("command") or ""))
    return commands


def test_codex_manifest_points_at_codex_hooks() -> None:
    manifest = _read_json("plugin/.codex-plugin/plugin.json")
    assert manifest["name"] == "claude-smart"
    assert manifest["hooks"] == "./hooks/codex-hooks.json"
    assert manifest["skills"] == "./skills/"
    assert manifest["interface"]["displayName"] == "claude-smart"


def test_codex_skill_documents_command_mapping() -> None:
    skill = (REPO_ROOT / "plugin" / "skills" / "claude-smart" / "SKILL.md").read_text()

    assert "name: claude-smart" in skill
    assert "Codex does not currently support plugin-provided slash commands" in skill
    assert "bash ~/.claude-smart/guided-runtimes/guided-command.sh cli.sh show" in skill
    assert (
        "bash ~/.claude-smart/guided-runtimes/guided-command.sh cli.sh learn --note"
        in skill
    )
    assert (
        "bash ~/.claude-smart/guided-runtimes/guided-command.sh cli.sh restart" in skill
    )
    assert (
        "bash ~/.claude-smart/guided-runtimes/guided-command.sh cli.sh clear-all --yes"
        in skill
    )


def test_codex_hook_loads_managed_reflexio_env_and_skips_backend_start() -> None:
    script = (REPO_ROOT / "plugin" / "scripts" / "codex-hook.js").read_text()

    assert "function loadReflexioEnv()" in script
    assert '"REFLEXIO_API_KEY"' in script
    assert '"CLAUDE_SMART_READ_ONLY"' in script
    assert "function runServiceScript(root, scriptName, action, logName)" in script
    assert "function parsePort(value, fallback)" in script
    assert (
        'runServiceScript(root, "backend-service.sh", "start", "backend.log")' in script
    )
    assert (
        'runServiceScript(root, "dashboard-service.sh", "start", "dashboard.log")'
        in script
    )
    assert "loadReflexioEnv();" in script
    # Backend start (including the remote-REFLEXIO_URL skip) is delegated to
    # backend-service.sh; codex-hook.js does not decide locally.
    assert "function reflexioUrlIsRemote()" not in script
    assert "[script, action]" in script
    assert "backend-service.sh" in script
    backend = (REPO_ROOT / "plugin" / "scripts" / "backend-service.sh").read_text()
    assert "remote REFLEXIO_URL configured; skipping local backend start" in backend
    assert "export REFLEXIO_RERANK_ENABLED=false" in backend


def test_codex_marketplace_points_at_plugin_root() -> None:
    marketplace = _read_json(".agents/plugins/marketplace.json")
    entry = marketplace["plugins"][0]
    assert marketplace["name"] == "reflexioai"
    assert marketplace["interface"]["displayName"] == "ReflexioAI"
    assert entry["name"] == "claude-smart"
    assert entry["source"]["path"] == "./plugin"
    assert entry["policy"]["installation"] == "AVAILABLE"


def test_npm_package_includes_codex_marketplace_payload() -> None:
    package = _read_json("package.json")
    files = set(package["files"])

    assert ".agents/plugins/marketplace.json" in files
    assert "plugin" in files
    assert "!plugin/**/__pycache__" in files
    assert "!plugin/**/*.py[cod]" in files


def test_release_bump_updates_codex_manifest() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text()

    assert "plugin/.codex-plugin/plugin.json" in makefile


def test_hook_commands_pass_explicit_host() -> None:
    claude_commands = [
        c for c in _command_hooks("plugin/hooks/hooks.json") if "hook_entry.sh" in c
    ]
    codex_commands = [
        c
        for c in _command_hooks("plugin/hooks/codex-hooks.json")
        if "hook_entry.sh" in c
    ]
    assert claude_commands
    assert codex_commands
    assert all('hook_entry.sh" claude-code ' in c for c in claude_commands)
    assert all('hook_entry.sh" codex ' in c for c in codex_commands)


def test_codex_hooks_use_expected_events_and_marketplace_fallback() -> None:
    hooks = _read_json("plugin/hooks/codex-hooks.json")["hooks"]
    codex_env = (REPO_ROOT / "plugin" / "scripts" / "_codex_env.sh").read_text()
    assert set(hooks) == {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
    }
    assert "CLAUDE_SMART_HOST" not in codex_env
    for command in _command_hooks("plugin/hooks/codex-hooks.json"):
        assert "_codex_env.sh" in command
        assert "printenv PATH" not in command
        assert "$HOME/plugins/claude-smart" not in command
        assert "CLAUDE_PLUGIN_ROOT" in command
        assert ".codex/plugins/cache/reflexioai/claude-smart" in command


def test_codex_hooks_top_level_matches_codex_schema() -> None:
    # Codex's plugin hook schema is strict and rejects any top-level field
    # other than "hooks" (e.g. "description" causes
    # "unknown field description, expected hooks"). Tests that only read
    # `["hooks"]` would silently miss a regression of this kind, so guard the
    # top-level shape explicitly.
    assert set(_read_json("plugin/hooks/codex-hooks.json")) == {"hooks"}


def test_hook_main_accepts_legacy_and_host_argv(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_read() -> dict[str, Any]:
        return {"session_id": "s1"}

    def fake_handlers():
        return {"stop": lambda _payload: calls.append((runtime.host(), "stop"))}

    monkeypatch.setattr(hook, "_read_stdin_json", fake_read)
    monkeypatch.setattr(hook, "_load_handlers", fake_handlers)

    assert hook.main(["stop"]) == 0
    assert hook.main(["codex", "stop"]) == 0
    assert calls == [("claude-code", "stop"), ("codex", "stop")]


def test_codex_pre_tool_is_registered_for_bash_only() -> None:
    """Codex PreToolUse additionalContext reaches the model; shell is ``Bash``."""
    blocks = _read_json("plugin/hooks/codex-hooks.json")["hooks"]["PreToolUse"]
    assert [block["matcher"] for block in blocks] == ["Bash"]
    (hook_def,) = blocks[0]["hooks"]
    assert hook_def["command"].endswith('hook_entry.sh" codex pre-tool')
    # Within the 12-second Python budget codex-hook.js gives pre-tool.
    assert 12 < hook_def["timeout"] <= 15


def test_codex_session_start_runs_handler_after_compaction() -> None:
    """Codex fires SessionStart with source "compact" before the next request."""
    blocks = _read_json("plugin/hooks/codex-hooks.json")["hooks"]["SessionStart"]
    # Existing trust keys index blocks; the compact block is appended.
    assert blocks[0]["matcher"] == "startup|resume"
    compact = [block for block in blocks if block["matcher"] == "compact"]
    assert len(compact) == 1 and blocks[-1] is compact[0]
    (hook_def,) = compact[0]["hooks"]
    assert hook_def["command"].endswith('hook_entry.sh" codex session-start')
    assert hook_def["timeout"] > 24  # codex-hook.js session-start budget


def test_codex_pre_tool_injects_context(monkeypatch) -> None:
    runtime.set_host(runtime.HOST_CODEX)
    calls: list[dict[str, Any]] = []

    def fake_emit(**kwargs):
        calls.append(kwargs)
        sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": "ctx",
                    }
                }
            )
            + "\n"
        )
        return True

    monkeypatch.setattr(pre_tool.context_inject, "emit_context", fake_emit)
    monkeypatch.setattr(pre_tool.ids, "resolve_user_id", lambda _cwd: "proj")
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)

    pre_tool.handle(
        {
            "session_id": "s1",
            "turn_id": "t1",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_use_id": "call-1",
            "tool_input": {"command": "uv run pytest"},
            "cwd": "/repo",
        }
    )

    assert [call["query"] for call in calls] == ["uv run pytest"]
    assert calls[0]["hook_event_name"] == "PreToolUse"
    assert calls[0]["session_id"] == "s1"
    output = json.loads(buf.getvalue())
    assert output["hookSpecificOutput"]["additionalContext"] == "ctx"
    # Codex fails a PreToolUse run that returns suppressOutput.
    assert "suppressOutput" not in output


def test_codex_citation_instruction_uses_text_marker_not_tool_call() -> None:
    runtime.set_host(runtime.HOST_CODEX)

    instruction = cs_cite.CITATION_INSTRUCTION

    assert "materially and meaningfully changed your response" in instruction
    assert "✨ claude-smart rule applied:" in instruction
    assert "tool call" not in instruction


def test_codex_stop_prefers_last_assistant_message(session_dir, monkeypatch) -> None:
    runtime.set_host(runtime.HOST_CODEX)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        stop.publish_outbox,
        "enqueue",
        lambda session_id, project_id: (
            calls.append({"session_id": session_id, "project_id": project_id}) or True
        ),
    )
    monkeypatch.setattr(stop.ids, "resolve_user_id", lambda *_a, **_kw: "demo")

    stop.handle(
        {
            "session_id": "s1",
            "cwd": str(REPO_ROOT),
            "last_assistant_message": "final answer from codex",
            "transcript_path": "/does/not/exist.jsonl",
        }
    )

    records = state.read_all("s1")
    assert records[-1]["role"] == "Assistant"
    assert records[-1]["content"] == "final answer from codex"
    assert calls and calls[0]["project_id"] == "demo"


def test_codex_stop_skips_internal_prompt_from_transcript(
    session_dir, tmp_path, monkeypatch
) -> None:
    runtime.set_host(runtime.HOST_CODEX)
    calls: list[dict[str, Any]] = []
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "content": (
                                "You are a helpful assistant. You will be presented "
                                "with a user prompt, and your job is to provide a "
                                "short title for a task that will be created from "
                                "that prompt."
                            )
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "Fix bug"}]},
                    }
                ),
            ]
        )
        + "\n"
    )
    monkeypatch.setattr(
        stop.publish_outbox,
        "enqueue",
        lambda session_id, project_id: (
            calls.append({"session_id": session_id, "project_id": project_id}) or True
        ),
    )

    stop.handle({"session_id": "s1", "transcript_path": str(transcript)})

    assert state.read_all("s1") == []
    assert calls == []


def test_codex_stop_skips_orphan_title_response(session_dir, monkeypatch) -> None:
    runtime.set_host(runtime.HOST_CODEX)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        stop.publish_outbox,
        "enqueue",
        lambda session_id, project_id: (
            calls.append({"session_id": session_id, "project_id": project_id}) or True
        ),
    )

    stop.handle(
        {
            "session_id": "s1",
            "cwd": str(REPO_ROOT),
            "last_assistant_message": '{"title":"Find claude-smart install type"}',
            "transcript_path": "/does/not/exist.jsonl",
        }
    )

    assert state.read_all("s1") == []
    assert calls == []


def test_codex_stop_skips_orphan_suggestions_response(session_dir, monkeypatch) -> None:
    runtime.set_host(runtime.HOST_CODEX)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        stop.publish_outbox,
        "enqueue",
        lambda session_id, project_id: (
            calls.append({"session_id": session_id, "project_id": project_id}) or True
        ),
    )

    stop.handle(
        {
            "session_id": "s1",
            "cwd": str(REPO_ROOT),
            "last_assistant_message": (
                '{"suggestions":[{"title":"Prepare patch",'
                '"description":"Fix the issue.","prompt":"Make the change.",'
                '"appId":""}]}'
            ),
            "transcript_path": "/does/not/exist.jsonl",
        }
    )

    assert state.read_all("s1") == []
    assert calls == []


def test_codex_stop_keeps_user_requested_title_json(session_dir, monkeypatch) -> None:
    runtime.set_host(runtime.HOST_CODEX)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        stop.publish_outbox,
        "enqueue",
        lambda session_id, project_id: (
            calls.append({"session_id": session_id, "project_id": project_id}) or True
        ),
    )
    state.append(
        "s1",
        {
            "role": "User",
            "content": "Return only a JSON title for the task.",
            "user_id": "demo",
        },
    )

    stop.handle(
        {
            "session_id": "s1",
            "cwd": str(REPO_ROOT),
            "last_assistant_message": '{"title":"Fix docs"}',
            "transcript_path": "/does/not/exist.jsonl",
        }
    )

    records = state.read_all("s1")
    assert records[-1]["role"] == "Assistant"
    assert records[-1]["content"] == '{"title":"Fix docs"}'
    assert calls


def test_codex_stop_keeps_user_requested_suggestions_json(
    session_dir, monkeypatch
) -> None:
    runtime.set_host(runtime.HOST_CODEX)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        stop.publish_outbox,
        "enqueue",
        lambda session_id, project_id: (
            calls.append({"session_id": session_id, "project_id": project_id}) or True
        ),
    )
    state.append(
        "s1",
        {
            "role": "User",
            "content": "Return only a JSON suggestions object.",
            "user_id": "demo",
        },
    )

    assistant_json = (
        '{"suggestions":[{"title":"Prepare patch",'
        '"description":"Fix the issue.","prompt":"Make the change.",'
        '"appId":""}]}'
    )
    stop.handle(
        {
            "session_id": "s1",
            "cwd": str(REPO_ROOT),
            "last_assistant_message": assistant_json,
            "transcript_path": "/does/not/exist.jsonl",
        }
    )

    records = state.read_all("s1")
    assert records[-1]["role"] == "Assistant"
    assert records[-1]["content"] == assistant_json
    assert calls


def test_codex_stop_resolves_text_citations_from_last_assistant_message(
    session_dir, monkeypatch
) -> None:
    runtime.set_host(runtime.HOST_CODEX)
    monkeypatch.setattr(stop.publish, "publish_unpublished", lambda **_kw: ("ok", 1))
    monkeypatch.setattr(stop.ids, "resolve_user_id", lambda *_a, **_kw: "demo")
    state.append_injected(
        "s1",
        [
            {
                "id": "s2-17",
                "real_id": "17",
                "kind": "playbook",
                "source_kind": "user_playbook",
                "title": "Check editable installs",
            }
        ],
    )

    stop.handle(
        {
            "session_id": "s1",
            "cwd": str(REPO_ROOT),
            "last_assistant_message": (
                "The answer stays visible.\n\n"
                "✨ 1 claude-smart learning applied [cs:s2-17]"
            ),
            "transcript_path": "/does/not/exist.jsonl",
        }
    )

    records = state.read_all("s1")
    assert records[-1]["cited_items"] == [
        {
            "id": "s2-17",
            "real_id": "17",
            "kind": "playbook",
            "source_kind": "user_playbook",
            "title": "Check editable installs",
        }
    ]


def test_codex_post_tool_records_apply_patch_payload(session_dir) -> None:
    runtime.set_host(runtime.HOST_CODEX)

    post_tool.handle(
        {
            "session_id": "s1",
            "tool_name": "apply_patch",
            "tool_input": {"patch": "*** Begin Patch\n*** End Patch"},
            "tool_response": {"stdout": "ok"},
        }
    )

    record = state.read_all("s1")[0]
    assert record["tool_name"] == "apply_patch"
    assert record["tool_input"]["patch"].startswith("*** Begin Patch")
    assert record["tool_output"] == "ok"
