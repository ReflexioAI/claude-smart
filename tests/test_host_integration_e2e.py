"""Contained happy-path E2E coverage for host hook integration."""

from __future__ import annotations

import io
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest  # type: ignore[reportMissingImports]

from claude_smart import context_inject, hook, publish, runtime, state
from claude_smart.events import session_start


@dataclass
class _FakeReflexioAdapter:
    host_label: str
    url: str = "http://localhost:8071/"
    search_calls: list[dict[str, Any]] = field(default_factory=list)
    publish_calls: list[dict[str, Any]] = field(default_factory=list)
    extraction_defaults: list[dict[str, int]] = field(default_factory=list)

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
        del script_path, timeout_seconds
        return True

    def search_all(
        self, *, project_id: str, query: str, top_k: int
    ) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
        self.search_calls.append(
            {
                "project_id": project_id,
                "query": query,
                "top_k": top_k,
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


def _install_fake_reflexio(monkeypatch: pytest.MonkeyPatch, fake: _FakeReflexioAdapter) -> None:
    monkeypatch.setattr(context_inject, "Adapter", lambda: fake)
    monkeypatch.setattr(publish, "Adapter", lambda: fake)
    monkeypatch.setattr(session_start, "_adapter", lambda: fake)


def _run_hook(
    monkeypatch: pytest.MonkeyPatch,
    *,
    host: str,
    event: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "stdout", stdout)

    assert hook.main([host, event]) == 0

    output = stdout.getvalue().strip()
    assert output
    return json.loads(output)


def _claude_code_transcript(tmp_path: Path, *, assistant_text: str) -> Path:
    transcript = tmp_path / "claude-code-transcript.jsonl"
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


@pytest.mark.parametrize(
    "host",
    [runtime.HOST_CLAUDE_CODE, runtime.HOST_CODEX, runtime.HOST_OPENCODE],
)
def test_host_happy_path_retrieves_context_buffers_tools_and_publishes(
    host: str,
    monkeypatch: pytest.MonkeyPatch,
    session_dir: Path,
    tmp_path: Path,
) -> None:
    fake_reflexio = _FakeReflexioAdapter(host_label=host)
    _install_fake_reflexio(monkeypatch, fake_reflexio)
    monkeypatch.setenv("CLAUDE_SMART_HOOK_LOG", str(tmp_path / "hook.log"))
    monkeypatch.delenv("REFLEXIO_USER_ID", raising=False)

    project = tmp_path / f"{host}-project"
    project.mkdir()
    session_id = f"session-{host}"
    prompt = f"Use learned context for {host}"
    assistant_text = f"{host} final answer"

    session_start_output = _run_hook(
        monkeypatch,
        host=host,
        event="session-start",
        payload={"session_id": session_id, "cwd": str(project)},
    )
    assert session_start_output["continue"] is True

    user_prompt_output = _run_hook(
        monkeypatch,
        host=host,
        event="user-prompt",
        payload={"session_id": session_id, "cwd": str(project), "prompt": prompt},
    )
    additional_context = user_prompt_output["hookSpecificOutput"]["additionalContext"]
    assert f"{host} project skill" in additional_context
    assert f"{host} shared skill" in additional_context
    assert f"{host} preference" in additional_context

    post_tool_output = _run_hook(
        monkeypatch,
        host=host,
        event="post-tool",
        payload={
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
            _claude_code_transcript(tmp_path, assistant_text=assistant_text)
        )
    else:
        stop_payload["last_assistant_message"] = assistant_text

    stop_output = _run_hook(
        monkeypatch,
        host=host,
        event="stop",
        payload=stop_payload,
    )
    assert stop_output["continue"] is True

    assert fake_reflexio.extraction_defaults == [
        {"window_size": 5, "stride_size": 3}
    ]
    assert fake_reflexio.search_calls == [
        {
            "project_id": project.name,
            "query": prompt,
            "top_k": 3,
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

    _, unpublished = state.unpublished_slice(state.read_all(session_id))
    assert unpublished == []
    assert Path(session_dir) in state.session_path(session_id).parents
