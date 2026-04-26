"""Tests for the per-session JSONL buffer."""

from __future__ import annotations

import json
import multiprocessing as mp
import os

from claude_smart import state


def test_append_and_read_roundtrip(session_dir) -> None:
    state.append("s1", {"role": "User", "content": "hi"})
    state.append("s1", {"role": "Assistant", "content": "hello"})
    assert state.read_all("s1") == [
        {"role": "User", "content": "hi"},
        {"role": "Assistant", "content": "hello"},
    ]


def test_read_all_skips_malformed_lines(session_dir) -> None:
    path = state.session_path("s1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"role":"User","content":"ok"}\nnot-json\n{"role":"Assistant","content":"ok"}\n'
    )
    records = state.read_all("s1")
    assert len(records) == 2
    assert records[0]["role"] == "User"
    assert records[1]["role"] == "Assistant"


def test_unpublished_slice_respects_watermark() -> None:
    records = [
        {"role": "User", "content": "u1"},
        {"role": "Assistant", "content": "a1"},
        {"published_up_to": 2},
        {"role": "User", "content": "u2"},
        {"role": "Assistant", "content": "a2"},
    ]
    watermark, turns = state.unpublished_slice(records)
    assert watermark == 2
    assert [t["content"] for t in turns] == ["u2", "a2"]


def test_unpublished_slice_attaches_tools_to_next_assistant() -> None:
    records = [
        {"role": "User", "content": "u1"},
        {
            "role": "Assistant_tool",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "status": "success",
        },
        {
            "role": "Assistant_tool",
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/x"},
            "status": "error",
        },
        {"role": "Assistant", "content": "a1"},
    ]
    _, turns = state.unpublished_slice(records)
    assert turns[-1]["role"] == "Assistant"
    assert turns[-1]["tools_used"] == [
        {
            "tool_name": "Bash",
            "status": "success",
            "tool_data": {"input": {"command": "ls"}},
        },
        {
            "tool_name": "Read",
            "status": "error",
            "tool_data": {"input": {"file_path": "/tmp/x"}},
        },
    ]


def test_unpublished_slice_omits_tool_data_when_input_missing() -> None:
    """Legacy records without ``tool_input`` still publish, without a tool_data key."""
    records = [
        {"role": "User", "content": "u1"},
        {"role": "Assistant_tool", "tool_name": "Bash", "status": "success"},
        {"role": "Assistant", "content": "a1"},
    ]
    _, turns = state.unpublished_slice(records)
    assert turns[-1]["tools_used"] == [
        {"tool_name": "Bash", "status": "success"},
    ]


def test_unpublished_slice_silent_assistant_placeholder_pins_tools() -> None:
    """Option A: an empty-content Assistant record still owns its tool runs."""
    records = [
        {"role": "User", "content": "u1"},
        {
            "role": "Assistant_tool",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "status": "success",
        },
        {"role": "Assistant", "content": ""},  # Stop-hook placeholder
        {"role": "User", "content": "u2"},
        {
            "role": "Assistant_tool",
            "tool_name": "Edit",
            "tool_input": {"file_path": "/a"},
            "status": "success",
        },
        {"role": "Assistant", "content": "done"},
    ]
    _, turns = state.unpublished_slice(records)
    assert turns[0] == {"role": "User", "content": "u1"}
    assert turns[1]["role"] == "Assistant"
    assert turns[1]["content"] == ""
    assert turns[1]["tools_used"] == [
        {
            "tool_name": "Bash",
            "status": "success",
            "tool_data": {"input": {"command": "ls"}},
        },
    ]
    assert turns[2] == {"role": "User", "content": "u2"}
    assert turns[3]["tools_used"] == [
        {
            "tool_name": "Edit",
            "status": "success",
            "tool_data": {"input": {"file_path": "/a"}},
        },
    ]


# -----------------------------------------------------------------------------
# injected registry (cs-cite)
# -----------------------------------------------------------------------------


def test_append_injected_roundtrip(session_dir) -> None:
    state.append_injected(
        "s1",
        [
            {"id": "r1-ab12", "kind": "playbook", "title": "t1", "content": "c1"},
            {"id": "p1-cd34", "kind": "profile", "title": "t2", "content": "c2"},
        ],
    )
    registry = state.read_injected("s1")
    assert registry["r1-ab12"]["title"] == "t1"
    assert registry["p1-cd34"]["kind"] == "profile"


def test_append_injected_empty_iter_is_noop(session_dir) -> None:
    state.append_injected("s1", iter([]))
    assert not state.injected_path("s1").exists()
    assert state.read_injected("s1") == {}


def test_read_injected_missing_file_returns_empty(session_dir) -> None:
    assert state.read_injected("never-existed") == {}


def test_read_injected_last_entry_wins_on_duplicate_id(session_dir) -> None:
    """Same id injected twice → the later metadata shadows the earlier one."""
    state.append_injected(
        "s1",
        [{"id": "r1-ab12", "kind": "playbook", "title": "old", "content": "c"}],
    )
    state.append_injected(
        "s1",
        [{"id": "r1-ab12", "kind": "playbook", "title": "new", "content": "c"}],
    )
    registry = state.read_injected("s1")
    assert registry["r1-ab12"]["title"] == "new"


def test_read_injected_different_fingerprints_do_not_collide(session_dir) -> None:
    """Cross-injection disambiguation: same rank + different fingerprints coexist."""
    state.append_injected(
        "s1",
        [{"id": "r1-0100", "kind": "playbook", "title": "older", "content": "c"}],
    )
    state.append_injected(
        "s1",
        [{"id": "r1-0200", "kind": "playbook", "title": "newer", "content": "c"}],
    )
    registry = state.read_injected("s1")
    assert registry["r1-0100"]["title"] == "older"
    assert registry["r1-0200"]["title"] == "newer"


def test_read_injected_skips_malformed_lines(session_dir) -> None:
    path = state.injected_path("s1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"id":"r1-ab12","kind":"playbook","title":"ok","content":"c"}\n'
        "not-json\n"
        '{"id":"p1-cd34","kind":"profile","title":"ok2","content":"c"}\n'
    )
    registry = state.read_injected("s1")
    assert set(registry.keys()) == {"r1-ab12", "p1-cd34"}


def test_read_injected_drops_entries_without_id(session_dir) -> None:
    path = state.injected_path("s1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"kind":"playbook","title":"ok","content":"c"}\n'
        '{"id":"","kind":"playbook","title":"ok","content":"c"}\n'
        '{"id":"r1-ab12","kind":"playbook","title":"ok","content":"c"}\n'
    )
    registry = state.read_injected("s1")
    assert set(registry.keys()) == {"r1-ab12"}


def test_unpublished_slice_truncates_overlong_tool_fields_to_cap() -> None:
    """Top-level string fields over the cap are truncated; the result still
    round-trips through ``json.dumps`` so publish never sends invalid JSON.
    """
    long_cmd = "x" * 5000
    long_edit = "y" * 5000
    records = [
        {"role": "User", "content": "u1"},
        {
            "role": "Assistant_tool",
            "tool_name": "Bash",
            "tool_input": {"command": long_cmd, "description": "short"},
            "status": "success",
        },
        {
            "role": "Assistant_tool",
            "tool_name": "Edit",
            "tool_input": {"file_path": "/a/b.py", "new_string": long_edit},
            "status": "success",
        },
        {"role": "Assistant", "content": "a1"},
    ]
    _, turns = state.unpublished_slice(records)
    tools = turns[-1]["tools_used"]
    assert (
        len(tools[0]["tool_data"]["input"]["command"]) == state._TOOL_DATA_FIELD_MAX_LEN
    )
    assert tools[0]["tool_data"]["input"]["description"] == "short"
    assert (
        len(tools[1]["tool_data"]["input"]["new_string"])
        == state._TOOL_DATA_FIELD_MAX_LEN
    )
    assert tools[1]["tool_data"]["input"]["file_path"] == "/a/b.py"
    json.dumps(turns[-1])  # sanity: publish-ready


def test_unpublished_slice_includes_truncated_tool_output() -> None:
    """``tool_output`` is folded into ``tool_data.output`` and capped at the same
    256-char limit as ``tool_input`` fields, so reflexio sees concrete failure
    text without inflating the extractor prompt.
    """
    long_output = "z" * 5000
    records = [
        {"role": "User", "content": "u1"},
        {
            "role": "Assistant_tool",
            "tool_name": "Bash",
            "tool_input": {"command": "ls /nope"},
            "tool_output": long_output,
            "status": "error",
        },
        {
            "role": "Assistant_tool",
            "tool_name": "Bash",
            "tool_input": {"command": "echo ok"},
            "tool_output": "",
            "status": "success",
        },
        {"role": "Assistant", "content": "a1"},
    ]
    _, turns = state.unpublished_slice(records)
    tools = turns[-1]["tools_used"]
    assert tools[0]["tool_data"]["input"] == {"command": "ls /nope"}
    assert len(tools[0]["tool_data"]["output"]) == state._TOOL_DATA_FIELD_MAX_LEN
    # Empty output collapses — only the input key is present.
    assert tools[1]["tool_data"] == {"input": {"command": "echo ok"}}


def test_unpublished_slice_strips_cited_items(session_dir) -> None:
    """``cited_items`` is dashboard-only metadata; reflexio must not receive it."""
    records = [
        {"role": "User", "content": "hi"},
        {
            "role": "Assistant",
            "content": "ok",
            "cited_items": [{"id": "r1-ab12", "kind": "playbook", "title": "t"}],
        },
    ]
    _, turns = state.unpublished_slice(records)
    assert "cited_items" not in turns[-1]


def _append_worker(state_dir: str, session_id: str, payload: str) -> None:
    # Child processes inherit env after fork, so CLAUDE_SMART_STATE_DIR is
    # already set. Belt-and-suspenders: reassert it.
    os.environ["CLAUDE_SMART_STATE_DIR"] = state_dir
    from claude_smart import state as s  # fresh import in child

    s.append(session_id, {"role": "User", "content": payload})


def test_append_concurrent_writes_do_not_corrupt_jsonl(session_dir) -> None:
    """Under flock, concurrent appends of large payloads must stay line-atomic."""
    big = "x" * 128 * 1024  # 128 KB — above any stdio buffer
    ctx = mp.get_context("fork")
    procs = [
        ctx.Process(target=_append_worker, args=(str(session_dir), "s1", big))
        for _ in range(8)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0

    path = state.session_path("s1")
    raw_lines = path.read_text().splitlines()
    assert len(raw_lines) == 8
    for line in raw_lines:
        record = json.loads(line)  # must parse — no interleaving
        assert record == {"role": "User", "content": big}
