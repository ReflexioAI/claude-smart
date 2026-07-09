"""Reusable happy-path harness for claude-smart host learning flows."""

from __future__ import annotations

import io
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class FakeReflexioAdapter:
    def __init__(self, host_label: str) -> None:
        self.host_label = host_label
        self.url = "http://localhost:8071/"
        self.search_calls: list[dict[str, Any]] = []
        self.publish_calls: list[dict[str, Any]] = []
        self.extraction_defaults: list[dict[str, int]] = []
        self.optimizer_defaults: list[dict[str, Any]] = []

    def fetch_stall_state(self) -> None:
        return None

    def apply_extraction_defaults(self, *, window_size: int, stride_size: int) -> bool:
        self.extraction_defaults.append(
            {"window_size": window_size, "stride_size": stride_size}
        )
        return True

    def apply_optimizer_defaults(
        self, *, script_path: str, timeout_seconds: int = 300
    ) -> bool:
        self.optimizer_defaults.append(
            {"script_path": script_path, "timeout_seconds": timeout_seconds}
        )
        return True

    def search_all(
        self, *, project_id: str, query: str, top_k: int, session_id: str | None = None
    ) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
        from claude_smart import runtime

        self.search_calls.append(
            {
                "project_id": project_id,
                "query": query,
                "top_k": top_k,
                "session_id": session_id,
                "host": runtime.host(),
            }
        )
        return (
            [
                {
                    "user_playbook_id": f"user-{self.host_label}",
                    "content": f"{self.host_label} project skill",
                    "trigger": "matching prompt",
                }
            ],
            [
                {
                    "agent_playbook_id": f"agent-{self.host_label}",
                    "content": f"{self.host_label} shared skill",
                }
            ],
            [
                {
                    "profile_id": f"profile-{self.host_label}",
                    "content": f"{self.host_label} preference",
                }
            ],
        )

    def publish(
        self,
        *,
        session_id: str,
        project_id: str,
        interactions: list[dict[str, Any]],
        force_extraction: bool = False,
        override_learning_stall: bool = False,
        skip_aggregation: bool = False,
    ) -> bool:
        from claude_smart import runtime

        self.publish_calls.append(
            {
                "session_id": session_id,
                "project_id": project_id,
                "interactions": interactions,
                "force_extraction": force_extraction,
                "override_learning_stall": override_learning_stall,
                "skip_aggregation": skip_aggregation,
                "host": runtime.host(),
                "agent_version": runtime.agent_version(),
            }
        )
        return True


@contextmanager
def _redirected_stdio(payload: dict[str, Any]):
    old_stdin = sys.stdin
    old_stdout = sys.stdout
    stdout = io.StringIO()
    sys.stdin = io.StringIO(json.dumps(payload))
    sys.stdout = stdout
    try:
        yield stdout
    finally:
        sys.stdin = old_stdin
        sys.stdout = old_stdout


def _run_hook(host: str, event: str, payload: dict[str, Any]) -> dict[str, Any]:
    from claude_smart import hook

    with _redirected_stdio(payload) as stdout:
        assert hook.main([host, event]) == 0
    output = stdout.getvalue().strip()
    assert output, f"{host} {event} produced no hook response"
    return json.loads(output)


def _claude_code_transcript(path: Path, *, assistant_text: str) -> Path:
    transcript = path / "claude-code-transcript.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "message": {"content": "Use memory"}}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [{"type": "text", "text": assistant_text}]
                        },
                    }
                ),
            ]
        )
        + "\n"
    )
    return transcript


def run_host_learning_happy_path(
    host: str,
    *,
    work_root: Path,
    expected_plugin_root: Path | None = None,
    expected_state_dir: Path | None = None,
) -> dict[str, Any]:
    import claude_smart
    from claude_smart import context_inject, cs_cite, publish, runtime, state
    from claude_smart.events import session_start

    loaded_from = Path(claude_smart.__file__).resolve()
    if expected_plugin_root is not None:
        expected_src = (expected_plugin_root / "src").resolve()
        try:
            loaded_from.relative_to(expected_src)
        except ValueError as exc:
            raise AssertionError(
                f"loaded {loaded_from}, expected installed plugin root {expected_src}"
            ) from exc

    fake_reflexio = FakeReflexioAdapter(host_label=host)
    original_context_adapter = context_inject.Adapter
    original_publish_adapter = publish.Adapter
    original_session_start_adapter = session_start._adapter
    previous_host = runtime.host()
    context_inject.Adapter = lambda: fake_reflexio
    publish.Adapter = lambda: fake_reflexio
    session_start._adapter = lambda: fake_reflexio
    try:
        project = work_root / f"{host}-learning-e2e-project"
        project.mkdir(parents=True, exist_ok=True)
        session_id = f"learning-session-{host}"
        prompt = f"Use learned context for {host}"
        citation_marker = cs_cite.build_marker(
            f"[{host} project skill](http://localhost:3001/rules/s2-user)",
            "markdown",
        )
        assistant_text = f"{host} final answer\n\n{citation_marker}"

        session_start_output = _run_hook(
            host,
            "session-start",
            {"session_id": session_id, "cwd": str(project)},
        )
        assert session_start_output["continue"] is True

        user_prompt_output = _run_hook(
            host,
            "user-prompt",
            {"session_id": session_id, "cwd": str(project), "prompt": prompt},
        )
        additional_context = user_prompt_output["hookSpecificOutput"][
            "additionalContext"
        ]
        assert f"{host} project skill" in additional_context
        assert f"{host} shared skill" in additional_context
        assert f"{host} preference" in additional_context
        assert cs_cite.MARKER_PREFIX in additional_context
        assert cs_cite.marker_attribution("markdown") in additional_context
        assert "✨ N claude-smart learnings applied" not in additional_context

        post_tool_output = _run_hook(
            host,
            "post-tool",
            {
                "session_id": session_id,
                "tool_name": "Bash",
                "tool_input": {"command": "echo ok"},
                "tool_response": {"stdout": "ok"},
            },
        )
        assert post_tool_output["continue"] is True

        stop_payload: dict[str, Any] = {"session_id": session_id, "cwd": str(project)}
        if host == runtime.HOST_CLAUDE_CODE:
            stop_payload["transcript_path"] = str(
                _claude_code_transcript(project, assistant_text=assistant_text)
            )
        else:
            stop_payload["last_assistant_message"] = assistant_text

        stop_output = _run_hook(host, "stop", stop_payload)
        assert stop_output["continue"] is True

        assert fake_reflexio.extraction_defaults == [
            {"window_size": 5, "stride_size": 3}
        ]
        assert fake_reflexio.search_calls == [
            {
                "project_id": project.name,
                "query": prompt,
                "top_k": 3,
                "session_id": session_id,
                "host": host,
            }
        ]
        assert len(fake_reflexio.publish_calls) == 1
        publish_call = fake_reflexio.publish_calls[0]
        assert publish_call["session_id"] == session_id
        assert publish_call["project_id"] == project.name
        assert publish_call["force_extraction"] is False
        assert publish_call["skip_aggregation"] is False
        assert publish_call["host"] == host
        assert publish_call["agent_version"] == runtime.HOST_CLAUDE_CODE

        interactions = publish_call["interactions"]
        assert [item["role"] for item in interactions] == ["User", "Assistant"]
        assert interactions[0]["content"] == prompt
        assert interactions[1]["content"] == assistant_text
        assert interactions[1]["tools_used"] == [
            {
                "tool_name": "Bash",
                "status": "success",
                "tool_data": {"input": {"command": "echo ok"}, "output": "ok"},
            }
        ]
        assert interactions[1]["citations"] == [
            {
                "kind": "user_playbook",
                "real_id": f"user-{host}",
                "tag": "s2-user",
                "title": f"{host} project skill",
            }
        ]

        _, unpublished = state.unpublished_slice(state.read_all(session_id))
        assert unpublished == []
        assert state.session_path(session_id).exists()
        if expected_state_dir is not None:
            assert expected_state_dir in state.session_path(session_id).parents
        return {
            "host": host,
            "loaded_from": str(loaded_from),
            "session_path": str(state.session_path(session_id)),
            "published": len(interactions),
        }
    finally:
        context_inject.Adapter = original_context_adapter
        publish.Adapter = original_publish_adapter
        session_start._adapter = original_session_start_adapter
        runtime.set_host(previous_host)
