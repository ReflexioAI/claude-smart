"""Tests for the per-event hook handlers."""

from __future__ import annotations

import io
import json
import sys
from typing import Any

import pytest

from claude_smart import state
from claude_smart.events import post_tool, session_start, stop, user_prompt


# -----------------------------------------------------------------------------
# post_tool
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_response, expected",
    [
        ({"is_error": True}, "error"),
        ({"error": "boom"}, "error"),
        ({"ok": True}, "success"),
        ("plain string output", "success"),
        (None, "success"),
    ],
)
def test_post_tool_status_derivation(session_dir, tool_response, expected) -> None:
    post_tool.handle(
        {
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_response": tool_response,
        }
    )
    records = state.read_all("s1")
    assert records[0]["status"] == expected


def test_post_tool_drops_payload_without_session_or_tool(session_dir) -> None:
    post_tool.handle({"tool_name": "Bash"})
    post_tool.handle({"session_id": "s1"})
    assert state.read_all("s1") == []


def test_post_tool_records_tool_input(session_dir) -> None:
    post_tool.handle(
        {
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},
            "tool_response": {"ok": True},
        }
    )
    post_tool.handle(
        {
            "session_id": "s1",
            "tool_name": "Read",
            "tool_response": "plain",
        }
    )
    records = state.read_all("s1")
    assert records[0]["tool_input"] == {"command": "ls -la"}
    assert records[1]["tool_input"] == {}


def test_post_tool_redacts_secret_assignments(session_dir) -> None:
    """High-entropy KEY=VALUE secrets are masked; benign assignments survive."""
    post_tool.handle(
        {
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {
                "command": (
                    'AWS_SECRET_ACCESS_KEY="wJalrXUtnFEMI0abcdEXAMPLEkey123" '
                    "LOG_LEVEL=INFO curl -sf https://example.com"
                ),
            },
        }
    )
    records = state.read_all("s1")
    command = records[0]["tool_input"]["command"]
    assert "wJalrXUtnFEMI0abcdEXAMPLEkey123" not in command
    assert "<redacted:" in command
    # Benign lowercase-or-short assignments should pass through unchanged.
    assert "LOG_LEVEL=INFO" in command


def test_post_tool_exit_plan_mode_records_only_assistant_tool(session_dir) -> None:
    """ExitPlanMode PostToolUse fires *before* the user approves/rejects, so
    its ``tool_response`` carries the plan payload itself (no decision). The
    hook therefore writes a plain ``Assistant_tool`` record and nothing else —
    plan-decision synthesis happens later in the Stop hook from the transcript.
    """
    post_tool.handle(
        {
            "session_id": "s1",
            "tool_name": "ExitPlanMode",
            "tool_input": {},
            "tool_response": {
                "plan": "# Plan\n\nbody",
                "isAgent": False,
                "filePath": "/tmp/plan.md",
                "hasTaskTool": True,
            },
        }
    )
    records = state.read_all("s1")
    assert [r["role"] for r in records] == ["Assistant_tool"]
    assert records[0]["tool_name"] == "ExitPlanMode"


@pytest.mark.parametrize(
    "tool_response, expected",
    [
        ({"stdout": "out\n", "stderr": "err\n"}, "out\n\nerr\n"),
        ({"stderr": "permission denied"}, "permission denied"),
        ({"error": "boom"}, "boom"),
        ({"content": "text body"}, "text body"),
        ("plain", "plain"),
        (None, ""),
        ({"unrelated": 1}, ""),
    ],
)
def test_post_tool_flattens_tool_response_text(tool_response, expected) -> None:
    assert post_tool._flatten_tool_response_text(tool_response) == expected


def test_post_tool_records_tool_output(session_dir) -> None:
    """Bash stdout/stderr is captured under ``tool_output`` so reflexio sees
    failure text, not just a status flag.
    """
    post_tool.handle(
        {
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {"command": "ls /nope"},
            "tool_response": {
                "stdout": "",
                "stderr": "ls: /nope: No such file or directory",
                "is_error": True,
            },
        }
    )
    post_tool.handle(
        {
            "session_id": "s1",
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/x"},
            "tool_response": None,
        }
    )
    records = state.read_all("s1")
    assert records[0]["tool_output"] == "ls: /nope: No such file or directory"
    assert records[0]["status"] == "error"
    # Missing/None responses leave the field empty rather than absent.
    assert records[1]["tool_output"] == ""


def test_post_tool_redacts_secrets_in_tool_output(session_dir) -> None:
    """Secret-shaped ``KEY=value`` assignments leaking through stderr must be
    masked the same way as ``tool_input`` strings — ``tool_output`` flows
    through the same redaction step.
    """
    post_tool.handle(
        {
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {"command": "deploy"},
            "tool_response": {
                "stdout": "",
                "stderr": "AWS_SECRET_ACCESS_KEY=AbCdEf1234567890XyZQrStUv",
                "is_error": True,
            },
        }
    )
    records = state.read_all("s1")
    assert "AbCdEf1234567890XyZQrStUv" not in records[0]["tool_output"]
    assert "<redacted:" in records[0]["tool_output"]


def test_post_tool_truncates_oversized_string(session_dir) -> None:
    blob = "x" * 5000
    post_tool.handle(
        {
            "session_id": "s1",
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/huge", "content": blob},
        }
    )
    stored = state.read_all("s1")[0]["tool_input"]["content"]
    assert stored.endswith("…(truncated)")
    assert len(stored) < len(blob)
    # file_path is short and must not be affected.
    assert state.read_all("s1")[0]["tool_input"]["file_path"] == "/tmp/huge"


# -----------------------------------------------------------------------------
# stop — always appends an Assistant record (Option A)
# -----------------------------------------------------------------------------


def _write_transcript(tmp_path, entries):
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return path


def test_stop_appends_assistant_text_from_transcript(
    session_dir, tmp_path, monkeypatch
) -> None:
    transcript = _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "hi"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "text": "hmm"},
                        {"type": "text", "text": "hello there"},
                    ]
                },
            },
        ],
    )
    monkeypatch.setattr(
        "claude_smart.publish.publish_unpublished",
        lambda **_: ("nothing", 0),
    )
    stop.handle({"session_id": "s1", "transcript_path": str(transcript)})
    records = state.read_all("s1")
    assert records[-1]["role"] == "Assistant"
    assert records[-1]["content"] == "hello there"


def test_stop_concatenates_text_across_multi_entry_assistant_turn(
    session_dir, tmp_path, monkeypatch
) -> None:
    """A turn spans many transcript entries; all text blocks must be captured.

    Claude Code's JSONL writes each thinking/tool_use/text block as its own
    ``type: assistant`` entry. Tool results arrive as ``type: user`` entries
    holding ``tool_result`` blocks — they must not be treated as a turn
    boundary. Only the leading real user message bounds the collection.
    """
    transcript = _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "do the thing"}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "thinking", "text": "hmm"}]},
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Let me check."}]},
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Read"}]},
            },
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": "..."}]},
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Found it."}]},
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Edit"}]},
            },
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": "ok"}]},
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Done."}]},
            },
        ],
    )
    monkeypatch.setattr(
        "claude_smart.publish.publish_unpublished",
        lambda **_: ("nothing", 0),
    )
    stop.handle({"session_id": "s1", "transcript_path": str(transcript)})
    records = state.read_all("s1")
    assert records[-1]["role"] == "Assistant"
    assert records[-1]["content"] == "Let me check.\n\nFound it.\n\nDone."


def test_stop_captures_exit_plan_mode_plan_as_assistant_text(
    session_dir, tmp_path, monkeypatch
) -> None:
    """ExitPlanMode.input.plan must land in the Assistant content.

    The plan is emitted as a tool_use argument, not a ``type: text`` block,
    so without the explicit branch the whole plan would be silently dropped
    from the published interaction.
    """
    transcript = _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "plan it"}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Thinking…"}]},
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "ExitPlanMode",
                            "input": {"plan": "# Plan\n\nStep 1. do X\nStep 2. do Y"},
                        }
                    ]
                },
            },
        ],
    )
    monkeypatch.setattr(
        "claude_smart.publish.publish_unpublished",
        lambda **_: ("nothing", 0),
    )
    stop.handle({"session_id": "s1", "transcript_path": str(transcript)})
    records = state.read_all("s1")
    content = records[-1]["content"]
    assert "Thinking…" in content
    assert "Plan:\n# Plan\n\nStep 1. do X\nStep 2. do Y" in content


def test_stop_synthesizes_user_record_on_plan_approval(
    session_dir, tmp_path, monkeypatch
) -> None:
    """Plan approval arrives as a ``tool_result`` in the transcript — Stop
    must mint a User turn for it so reflexio sees the decision signal.

    PostToolUse fires before the user decides (tool_response carries only the
    plan payload), so the approval text never reaches the hook payload. The
    transcript is the only place it exists.
    """
    transcript = _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "plan it"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "ExitPlanMode",
                            "input": {"plan": "# Plan"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": (
                                "Your plan has been saved.\n"
                                "User has approved your plan. You can now start coding."
                            ),
                        }
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Done."}]},
            },
        ],
    )
    monkeypatch.setattr(
        "claude_smart.publish.publish_unpublished",
        lambda **_: ("nothing", 0),
    )
    stop.handle(
        {
            "session_id": "s1",
            "transcript_path": str(transcript),
            "cwd": "/some/repo",
        }
    )
    records = state.read_all("s1")
    roles = [r["role"] for r in records]
    assert roles == ["User", "Assistant"]
    assert records[0]["content"] == "Approved the plan."


def test_stop_synthesizes_user_record_on_plan_rejection_with_comment(
    session_dir, tmp_path, monkeypatch
) -> None:
    """Rejection-with-feedback: the user's comment is the correction signal,
    hoisted from after ``the user said:`` into the User record content.
    """
    rejection_text = (
        "The user doesn't want to proceed with this tool use. "
        "The tool use was rejected. To tell you how to proceed, the user said:\n"
        "avoid making the prompt too long, use v4.0.1"
    )
    transcript = _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "plan it"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "ExitPlanMode",
                            "input": {"plan": "# Plan"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "content": rejection_text}]
                },
            },
        ],
    )
    monkeypatch.setattr(
        "claude_smart.publish.publish_unpublished",
        lambda **_: ("nothing", 0),
    )
    stop.handle({"session_id": "s1", "transcript_path": str(transcript)})
    records = state.read_all("s1")
    assert records[0]["role"] == "User"
    assert records[0]["content"] == (
        "Rejected the plan. Instead: avoid making the prompt too long, use v4.0.1"
    )


def test_stop_synthesizes_user_record_on_silent_plan_rejection(
    session_dir, tmp_path, monkeypatch
) -> None:
    """Rejection without a follow-up comment still emits a User record."""
    transcript = _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "plan it"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "ExitPlanMode",
                            "input": {"plan": "# Plan"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": (
                                "The user doesn't want to proceed with this tool use. "
                                "The tool use was rejected."
                            ),
                        }
                    ]
                },
            },
        ],
    )
    monkeypatch.setattr(
        "claude_smart.publish.publish_unpublished",
        lambda **_: ("nothing", 0),
    )
    stop.handle({"session_id": "s1", "transcript_path": str(transcript)})
    records = state.read_all("s1")
    assert records[0]["role"] == "User"
    assert records[0]["content"] == "Rejected the plan."


def test_stop_plan_decision_handles_list_content_shape(
    session_dir, tmp_path, monkeypatch
) -> None:
    """tool_result content can be a list of ``{type: text, text: …}`` blocks —
    the scanner must match markers across that shape too.
    """
    transcript = _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "plan it"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "ExitPlanMode",
                            "input": {"plan": "# Plan"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": [
                                {"type": "text", "text": "Your plan has been saved."},
                                {
                                    "type": "text",
                                    "text": "User has approved your plan.",
                                },
                            ],
                        }
                    ]
                },
            },
        ],
    )
    monkeypatch.setattr(
        "claude_smart.publish.publish_unpublished",
        lambda **_: ("nothing", 0),
    )
    stop.handle({"session_id": "s1", "transcript_path": str(transcript)})
    records = state.read_all("s1")
    assert records[0]["role"] == "User"
    assert records[0]["content"] == "Approved the plan."


def test_stop_plan_decision_ignores_prior_turn(
    session_dir, tmp_path, monkeypatch
) -> None:
    """Plan decisions in *previous* turns were already published — only the
    current turn's decisions (if any) should be synthesized now.
    """
    transcript = _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "first plan"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "ExitPlanMode",
                            "input": {"plan": "# old"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": "User has approved your plan.",
                        }
                    ]
                },
            },
            {"type": "user", "message": {"content": "follow-up, no plan"}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Ok."}]},
            },
        ],
    )
    monkeypatch.setattr(
        "claude_smart.publish.publish_unpublished",
        lambda **_: ("nothing", 0),
    )
    stop.handle({"session_id": "s1", "transcript_path": str(transcript)})
    records = state.read_all("s1")
    assert [r["role"] for r in records] == ["Assistant"]


def test_stop_plan_decision_ignores_non_exitplanmode_tool_result_with_marker_text(
    session_dir, tmp_path, monkeypatch
) -> None:
    """A non-plan tool whose output happens to contain the marker text must
    NOT be synthesized as a plan decision. The scanner only treats a
    ``tool_result`` as a decision when the immediately-preceding assistant
    ``tool_use`` was ``ExitPlanMode``.
    """
    transcript = _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "do thing"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "echo 'User has approved your plan.'"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": "User has approved your plan.\n",
                        }
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Done."}]},
            },
        ],
    )
    monkeypatch.setattr(
        "claude_smart.publish.publish_unpublished",
        lambda **_: ("nothing", 0),
    )
    stop.handle({"session_id": "s1", "transcript_path": str(transcript)})
    records = state.read_all("s1")
    # Only the Assistant record should be appended — no synthetic User turn.
    assert [r["role"] for r in records] == ["Assistant"]


def test_stop_exit_plan_mode_plan_interleaves_with_text_blocks_in_order(
    session_dir, tmp_path, monkeypatch
) -> None:
    """Text and ExitPlanMode blocks in the same assistant entry must be
    joined in emission order so the plan reads coherently relative to
    surrounding narration.
    """
    transcript = _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "plan it"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Here's the plan:"},
                        {
                            "type": "tool_use",
                            "name": "ExitPlanMode",
                            "input": {"plan": "step 1\nstep 2"},
                        },
                        {"type": "text", "text": "Approve if you're happy."},
                    ]
                },
            },
        ],
    )
    monkeypatch.setattr(
        "claude_smart.publish.publish_unpublished",
        lambda **_: ("nothing", 0),
    )
    stop.handle({"session_id": "s1", "transcript_path": str(transcript)})
    records = state.read_all("s1")
    assert records[-1]["content"] == (
        "Here's the plan:\n\nPlan:\nstep 1\nstep 2\n\nApprove if you're happy."
    )


def test_stop_does_not_cross_prior_turn_boundary(
    session_dir, tmp_path, monkeypatch
) -> None:
    """Backward walk must stop at the user message that opened THIS turn.

    Regression guard: earlier assistant text must NOT leak into the
    current Assistant record, or prior-turn content would get republished
    to reflexio every time the Stop hook fires.
    """
    transcript = _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "first question"}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "First answer."}]},
            },
            {"type": "user", "message": {"content": "follow-up"}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Second answer."}]},
            },
        ],
    )
    monkeypatch.setattr(
        "claude_smart.publish.publish_unpublished",
        lambda **_: ("nothing", 0),
    )
    stop.handle({"session_id": "s1", "transcript_path": str(transcript)})
    records = state.read_all("s1")
    assert records[-1]["content"] == "Second answer."


def test_stop_appends_empty_assistant_record_when_turn_was_tools_only(
    session_dir, tmp_path, monkeypatch
) -> None:
    """Option A: tool-only assistant turn still gets a placeholder record."""
    transcript = _write_transcript(
        tmp_path,
        [
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Bash"}]},
            }
        ],
    )
    monkeypatch.setattr(
        "claude_smart.publish.publish_unpublished",
        lambda **_: ("nothing", 0),
    )
    stop.handle({"session_id": "s1", "transcript_path": str(transcript)})
    records = state.read_all("s1")
    assert any(r.get("role") == "Assistant" and r.get("content") == "" for r in records)


def test_stop_retries_read_when_transcript_not_yet_flushed(
    session_dir, tmp_path, monkeypatch
) -> None:
    """Race: Stop fires before Claude Code's final text write is readable.

    Regression guard for the documented race in ``_read_last_assistant_text``
    — first scan sees a tool-only tail because the final ``type: text`` entry
    hasn't hit the page cache yet. The retry must pick up the text instead
    of persisting an empty Assistant record.
    """
    transcript = _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "hi"}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Bash"}]},
            },
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": "ok"}]},
            },
        ],
    )

    sleep_calls: list[float] = []

    def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        if len(sleep_calls) == 1:
            with transcript.open("a") as fh:
                fh.write(
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {
                                "content": [{"type": "text", "text": "late answer"}]
                            },
                        }
                    )
                    + "\n"
                )

    monkeypatch.setattr("claude_smart.events.stop.time.sleep", fake_sleep)
    monkeypatch.setattr(
        "claude_smart.publish.publish_unpublished",
        lambda **_: ("nothing", 0),
    )
    stop.handle({"session_id": "s1", "transcript_path": str(transcript)})
    records = state.read_all("s1")
    assert records[-1]["content"] == "late answer"
    assert len(sleep_calls) == 1


def test_stop_retry_exhausts_budget_and_returns_empty(
    session_dir, tmp_path, monkeypatch
) -> None:
    """All retries miss the flush — Assistant record persists with empty content.

    Confirms the retry loop is bounded and falls through to the empty-record
    path (Option A) instead of spinning or raising.
    """
    transcript = _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "hi"}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Bash"}]},
            },
        ],
    )
    sleep_calls: list[float] = []
    monkeypatch.setattr(
        "claude_smart.events.stop.time.sleep",
        lambda d: sleep_calls.append(d),
    )
    monkeypatch.setattr(
        "claude_smart.publish.publish_unpublished",
        lambda **_: ("nothing", 0),
    )
    stop.handle({"session_id": "s1", "transcript_path": str(transcript)})
    records = state.read_all("s1")
    assert records[-1]["role"] == "Assistant"
    assert records[-1]["content"] == ""
    # All configured delays were consumed — no early exit on non-empty.
    assert len(sleep_calls) == len(stop._TRANSCRIPT_RETRY_DELAYS_S)


def test_stop_stamps_user_id_from_payload_cwd(
    session_dir, tmp_path, monkeypatch
) -> None:
    """Assistant record carries user_id resolved from the hook payload's cwd."""
    transcript = _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "hi"}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "ok"}]},
            },
        ],
    )
    monkeypatch.setattr(
        "claude_smart.events.stop.ids.resolve_project_id",
        lambda cwd=None: f"proj::{cwd}",
    )
    monkeypatch.setattr(
        "claude_smart.publish.publish_unpublished",
        lambda **_: ("nothing", 0),
    )
    stop.handle(
        {
            "session_id": "s1",
            "transcript_path": str(transcript),
            "cwd": "/some/repo",
        }
    )
    records = state.read_all("s1")
    assert records[-1]["user_id"] == "proj::/some/repo"


def _stub_user_prompt_adapter(
    monkeypatch, *, playbooks=None, profiles=None, calls=None
):
    class Stub:
        def search_both(self, **kwargs):
            if calls is not None:
                calls.append(kwargs)
            return (list(playbooks or []), list(profiles or []))

    monkeypatch.setattr("claude_smart.context_inject.Adapter", lambda *a, **kw: Stub())
    monkeypatch.setattr(
        "claude_smart.events.user_prompt.ids.resolve_project_id",
        lambda *_a, **_kw: "demo",
    )


def test_user_prompt_stamps_user_id_from_payload_cwd(session_dir, monkeypatch) -> None:
    """User record carries user_id resolved from the hook payload's cwd."""
    _stub_user_prompt_adapter(monkeypatch)
    monkeypatch.setattr(
        "claude_smart.events.user_prompt.ids.resolve_project_id",
        lambda cwd=None: f"proj::{cwd}",
    )
    user_prompt.handle({"session_id": "s1", "prompt": "hello", "cwd": "/some/repo"})
    records = state.read_all("s1")
    assert records[-1]["role"] == "User"
    assert records[-1]["content"] == "hello"
    assert records[-1]["user_id"] == "proj::/some/repo"


def test_user_prompt_injects_context_when_hits_present(
    session_dir, monkeypatch
) -> None:
    """A prompt with matching profiles/playbooks injects additionalContext."""
    calls: list[dict[str, Any]] = []
    _stub_user_prompt_adapter(
        monkeypatch,
        playbooks=[{"content": "run uv sync after edits", "trigger": "pyproject.toml"}],
        profiles=[{"content": "prefers anyio over asyncio"}],
        calls=calls,
    )
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    user_prompt.handle(
        {"session_id": "s1", "prompt": "edit pyproject.toml", "cwd": "/r"}
    )
    payload = json.loads(buf.getvalue().strip())
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    markdown = payload["hookSpecificOutput"]["additionalContext"]
    assert "run uv sync after edits" in markdown
    assert "prefers anyio over asyncio" in markdown
    # Prompt text is passed verbatim as the search query.
    assert calls[0]["project_id"] == "demo"
    assert calls[0]["query"] == "edit pyproject.toml"
    # The prompt is still buffered for downstream extraction.
    records = state.read_all("s1")
    assert records[-1]["role"] == "User"
    assert records[-1]["content"] == "edit pyproject.toml"
    # The citation registry is populated so Stop can resolve cs-cite ids.
    registry = state.read_injected("s1")
    assert registry, "expected injected registry to hold at least one entry"
    kinds = {entry["kind"] for entry in registry.values()}
    assert kinds == {"playbook", "profile"}
    for cite_id, entry in registry.items():
        assert cite_id[0] in {"r", "p"}
        assert entry["content"]
        assert entry["title"]


def test_user_prompt_writes_nothing_when_search_empty(session_dir, monkeypatch) -> None:
    """No hits → no stdout output, but the prompt is still buffered."""
    _stub_user_prompt_adapter(monkeypatch, playbooks=[], profiles=[])
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    user_prompt.handle({"session_id": "s1", "prompt": "anything", "cwd": "/r"})
    assert buf.getvalue() == ""
    assert state.read_all("s1")[-1]["content"] == "anything"


def test_user_prompt_buffers_even_when_search_raises(session_dir, monkeypatch) -> None:
    """A failing search must not prevent the prompt from being buffered."""

    class Boom:
        def search_both(self, **_kw):
            raise RuntimeError("reflexio down")

    monkeypatch.setattr("claude_smart.context_inject.Adapter", lambda *a, **kw: Boom())
    monkeypatch.setattr(
        "claude_smart.events.user_prompt.ids.resolve_project_id",
        lambda *_a, **_kw: "demo",
    )
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    user_prompt.handle({"session_id": "s1", "prompt": "hello", "cwd": "/r"})
    assert buf.getvalue() == ""
    assert state.read_all("s1")[-1]["content"] == "hello"


def test_user_prompt_noop_on_empty_prompt(session_dir, monkeypatch) -> None:
    """Empty prompt → nothing buffered, nothing emitted, no search attempted."""
    calls: list[dict[str, Any]] = []
    _stub_user_prompt_adapter(monkeypatch, calls=calls)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    user_prompt.handle({"session_id": "s1", "prompt": "", "cwd": "/r"})
    assert state.read_all("s1") == []
    assert buf.getvalue() == ""
    assert calls == []


def test_stop_without_session_is_noop(session_dir) -> None:
    stop.handle({})
    assert state.read_all("s1") == []


# -----------------------------------------------------------------------------
# stop — cs-cite citation extraction
# -----------------------------------------------------------------------------


def _transcript_with_cs_cite_call(tmp_path, cs_cite_command: str):
    """Build a transcript whose current turn ends with a cs-cite Bash call."""
    return _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "do the thing"}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "On it."}]},
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": cs_cite_command},
                        }
                    ]
                },
            },
        ],
    )


def _prime_injected_registry(session_id: str) -> None:
    state.append_injected(
        session_id,
        [
            {
                "id": "r1-42",
                "kind": "playbook",
                "title": "use pathlib",
                "content": "use pathlib",
                "real_id": "42",
                "ts": 0,
            },
            {
                "id": "p1-uuid",
                "kind": "profile",
                "title": "prefers anyio",
                "content": "prefers anyio",
                "real_id": "uuid-anyio",
                "ts": 0,
            },
        ],
    )


def test_stop_records_cs_cite_ids_as_cited_items(
    session_dir, tmp_path, monkeypatch
) -> None:
    """A single cs-cite call resolves ids against the session registry."""
    _prime_injected_registry("s1")
    transcript = _transcript_with_cs_cite_call(tmp_path, "cs-cite r1-42,p1-uuid")
    monkeypatch.setattr(
        "claude_smart.publish.publish_unpublished", lambda **_: ("nothing", 0)
    )
    stop.handle({"session_id": "s1", "transcript_path": str(transcript)})
    records = state.read_all("s1")
    assert records[-1]["role"] == "Assistant"
    cited = records[-1].get("cited_items")
    assert cited == [
        {"id": "r1-42", "kind": "playbook", "title": "use pathlib", "real_id": "42"},
        {
            "id": "p1-uuid",
            "kind": "profile",
            "title": "prefers anyio",
            "real_id": "uuid-anyio",
        },
    ]


def test_stop_omits_cited_items_when_no_cs_cite_call(
    session_dir, tmp_path, monkeypatch
) -> None:
    """Non-citing turns leave no cited_items key on the Assistant record."""
    _prime_injected_registry("s1")
    transcript = _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "hi"}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "ok"}]},
            },
        ],
    )
    monkeypatch.setattr(
        "claude_smart.publish.publish_unpublished", lambda **_: ("nothing", 0)
    )
    stop.handle({"session_id": "s1", "transcript_path": str(transcript)})
    records = state.read_all("s1")
    assert "cited_items" not in records[-1]


def test_stop_drops_unknown_cs_cite_ids(session_dir, tmp_path, monkeypatch) -> None:
    """Ids not present in the session registry are silently dropped."""
    _prime_injected_registry("s1")
    transcript = _transcript_with_cs_cite_call(tmp_path, "cs-cite r1-42,r9-ffff")
    monkeypatch.setattr(
        "claude_smart.publish.publish_unpublished", lambda **_: ("nothing", 0)
    )
    stop.handle({"session_id": "s1", "transcript_path": str(transcript)})
    records = state.read_all("s1")
    cited = records[-1].get("cited_items")
    assert cited == [
        {"id": "r1-42", "kind": "playbook", "title": "use pathlib", "real_id": "42"},
    ]


def test_stop_merges_multiple_cs_cite_calls_dedup(
    session_dir, tmp_path, monkeypatch
) -> None:
    """Multiple cs-cite calls within one turn merge; duplicate ids collapse."""
    _prime_injected_registry("s1")
    transcript = _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": "hi"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "cs-cite r1-42"},
                        }
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "cs-cite r1-42,p1-uuid"},
                        }
                    ]
                },
            },
        ],
    )
    monkeypatch.setattr(
        "claude_smart.publish.publish_unpublished", lambda **_: ("nothing", 0)
    )
    stop.handle({"session_id": "s1", "transcript_path": str(transcript)})
    records = state.read_all("s1")
    cited = records[-1].get("cited_items")
    assert cited == [
        {"id": "r1-42", "kind": "playbook", "title": "use pathlib", "real_id": "42"},
        {
            "id": "p1-uuid",
            "kind": "profile",
            "title": "prefers anyio",
            "real_id": "uuid-anyio",
        },
    ]


def test_stop_rejects_chained_cs_cite_commands(
    session_dir, tmp_path, monkeypatch
) -> None:
    """A Bash call that chains cs-cite with other commands is not parsed as a citation.

    Guards against false positives when Claude wraps the citation in a
    larger command (pipes, ``&&``, heredoc). The parser only accepts a
    bare ``cs-cite <ids>`` invocation.
    """
    _prime_injected_registry("s1")
    transcript = _transcript_with_cs_cite_call(tmp_path, "cs-cite r1-42 && echo done")
    monkeypatch.setattr(
        "claude_smart.publish.publish_unpublished", lambda **_: ("nothing", 0)
    )
    stop.handle({"session_id": "s1", "transcript_path": str(transcript)})
    records = state.read_all("s1")
    assert "cited_items" not in records[-1]


def test_stop_handles_missing_transcript_file(session_dir, monkeypatch) -> None:
    """``transcript_path`` points at a nonexistent file → no crash, empty record."""
    monkeypatch.setattr(
        "claude_smart.publish.publish_unpublished", lambda **_: ("nothing", 0)
    )
    stop.handle({"session_id": "s1", "transcript_path": "/does/not/exist.jsonl"})
    records = state.read_all("s1")
    assert records[-1]["role"] == "Assistant"
    assert records[-1]["content"] == ""
    assert "cited_items" not in records[-1]


def test_stop_citation_without_prior_registry_drops_all_ids(
    session_dir, tmp_path, monkeypatch
) -> None:
    """Citation with no ``.injected.jsonl`` (e.g. resumed session) → all ids drop."""
    transcript = _transcript_with_cs_cite_call(tmp_path, "cs-cite r1-42")
    monkeypatch.setattr(
        "claude_smart.publish.publish_unpublished", lambda **_: ("nothing", 0)
    )
    stop.handle({"session_id": "s1", "transcript_path": str(transcript)})
    records = state.read_all("s1")
    assert "cited_items" not in records[-1]


# -----------------------------------------------------------------------------
# session_start — emit_continue when nothing to render
# -----------------------------------------------------------------------------


def test_session_start_emits_continue_without_session_id(
    session_dir, monkeypatch
) -> None:
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    session_start.handle({})
    assert json.loads(buf.getvalue().strip()) == {
        "continue": True,
        "suppressOutput": True,
    }


def test_session_start_emits_continue_when_no_playbook_or_profile(
    session_dir, monkeypatch
) -> None:
    class StubAdapter:
        def apply_batch_defaults(self, **_kw):
            return True

        def fetch_both(self, **_kw):
            return ([], [])

    monkeypatch.setattr(
        "claude_smart.events.session_start.Adapter", lambda *a, **kw: StubAdapter()
    )
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    session_start.handle({"session_id": "s1", "source": "startup"})
    assert json.loads(buf.getvalue().strip()) == {
        "continue": True,
        "suppressOutput": True,
    }


def test_session_start_emits_additional_context_when_playbook_present(
    session_dir, monkeypatch
) -> None:
    class Stub:
        def apply_batch_defaults(self, **_kw):
            return True

        def fetch_both(self, **_kw):
            return (
                [
                    {
                        "content": "use pathlib",
                        "trigger": "file ops",
                        "rationale": "safer",
                    }
                ],
                [],
            )

    monkeypatch.setattr(
        "claude_smart.events.session_start.Adapter", lambda *a, **kw: Stub()
    )
    monkeypatch.setattr(
        "claude_smart.events.session_start.ids.resolve_project_id",
        lambda *_a, **_kw: "demo",
    )
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    session_start.handle({"session_id": "s1", "source": "startup"})
    payload = json.loads(buf.getvalue().strip())
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "use pathlib" in payload["hookSpecificOutput"]["additionalContext"]


def test_session_start_applies_claude_smart_batch_defaults(
    session_dir, monkeypatch
) -> None:
    """SessionStart must push claude-smart's 5/3 batch defaults to reflexio."""
    applied: list[dict[str, Any]] = []

    class Stub:
        def apply_batch_defaults(self, **kwargs):
            applied.append(kwargs)
            return True

        def fetch_both(self, **_kw):
            return ([], [])

    monkeypatch.setattr(
        "claude_smart.events.session_start.Adapter", lambda *a, **kw: Stub()
    )
    monkeypatch.setattr(
        "claude_smart.events.session_start.ids.resolve_project_id",
        lambda *_a, **_kw: "demo",
    )
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    session_start.handle({"session_id": "s1", "source": "startup"})
    assert applied == [{"batch_size": 5, "batch_interval": 3}]


def test_session_start_fetches_both_on_every_source(session_dir, monkeypatch) -> None:
    """Used to skip profile fetch unless source ∈ {resume,clear,compact}; now always both."""
    calls: list[dict[str, Any]] = []

    class Stub:
        def apply_batch_defaults(self, **_kw):
            return True

        def fetch_both(self, **kwargs):
            calls.append(kwargs)
            return ([], [])

    monkeypatch.setattr(
        "claude_smart.events.session_start.Adapter", lambda *a, **kw: Stub()
    )
    monkeypatch.setattr(
        "claude_smart.events.session_start.ids.resolve_project_id",
        lambda *_a, **_kw: "demo",
    )
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    for source in ("startup", "resume", "clear", "compact"):
        session_start.handle({"session_id": "s1", "source": source})
    assert len(calls) == 4
    for kw in calls:
        assert kw["project_id"] == "demo"


# -----------------------------------------------------------------------------
# pre_tool
# -----------------------------------------------------------------------------


def _stub_pretool_adapter(monkeypatch, *, playbooks=None, profiles=None, calls=None):
    class Stub:
        def search_both(self, **kwargs):
            if calls is not None:
                calls.append(kwargs)
            return (list(playbooks or []), list(profiles or []))

    monkeypatch.setattr("claude_smart.context_inject.Adapter", lambda *a, **kw: Stub())
    monkeypatch.setattr(
        "claude_smart.events.pre_tool.ids.resolve_project_id",
        lambda *_a, **_kw: "demo",
    )


def test_pre_tool_emits_continue_without_session_id(session_dir, monkeypatch) -> None:
    from claude_smart.events import pre_tool

    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    pre_tool.handle({"tool_name": "Edit", "tool_input": {"file_path": "a.py"}})
    assert json.loads(buf.getvalue().strip()) == {
        "continue": True,
        "suppressOutput": True,
    }


def test_pre_tool_emits_continue_for_unknown_tool(session_dir, monkeypatch) -> None:
    from claude_smart.events import pre_tool

    _stub_pretool_adapter(monkeypatch)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    pre_tool.handle(
        {"session_id": "s1", "tool_name": "Read", "tool_input": {"file_path": "a.py"}}
    )
    assert json.loads(buf.getvalue().strip()) == {
        "continue": True,
        "suppressOutput": True,
    }


def test_pre_tool_injects_context_when_hits_present(session_dir, monkeypatch) -> None:
    from claude_smart.events import pre_tool

    calls: list[dict[str, Any]] = []
    _stub_pretool_adapter(
        monkeypatch,
        playbooks=[{"content": "run uv sync after edits", "trigger": "pyproject.toml"}],
        profiles=[{"content": "prefers anyio over asyncio"}],
        calls=calls,
    )
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    pre_tool.handle(
        {
            "session_id": "s1",
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/x/pyproject.toml", "new_string": "dep"},
        }
    )
    payload = json.loads(buf.getvalue().strip())
    assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    markdown = payload["hookSpecificOutput"]["additionalContext"]
    assert "run uv sync after edits" in markdown
    assert "prefers anyio over asyncio" in markdown
    # Composed query must reach the adapter scoped to the project.
    assert calls[0]["project_id"] == "demo"
    assert "pyproject.toml" in calls[0]["query"]


def test_pre_tool_emits_continue_when_search_empty(session_dir, monkeypatch) -> None:
    from claude_smart.events import pre_tool

    _stub_pretool_adapter(monkeypatch, playbooks=[], profiles=[])
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    pre_tool.handle(
        {
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {"command": "uv run pytest"},
        }
    )
    assert json.loads(buf.getvalue().strip()) == {
        "continue": True,
        "suppressOutput": True,
    }
