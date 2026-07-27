"""Tests for the per-session JSONL buffer."""

from __future__ import annotations

import json
import multiprocessing as mp
import os

import pytest

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
# injected citation registry
# -----------------------------------------------------------------------------


def test_append_injected_roundtrip(session_dir) -> None:
    state.append_injected(
        "s1",
        [
            {
                "id": "s1-ab12",
                "kind": "playbook",
                "source_kind": "user_playbook",
                "real_id": "11",
                "title": "t1",
                "content": "c1",
            },
            {
                "id": "p1-cd34",
                "kind": "profile",
                "real_id": "p1",
                "title": "t2",
                "content": "c2",
            },
        ],
    )
    registry = state.read_injected("s1")
    assert registry["s1-ab12"]["title"] == "t1"
    assert registry["p1-cd34"]["kind"] == "profile"
    assert state.read_all("s1") == [
        {
            "retrieved_learning_refs": [
                {"kind": "user_playbook", "learning_id": "11"},
                {"kind": "profile", "learning_id": "p1"},
            ]
        }
    ]


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
        [{"id": "s1-ab12", "kind": "playbook", "title": "old", "content": "c"}],
    )
    state.append_injected(
        "s1",
        [{"id": "s1-ab12", "kind": "playbook", "title": "new", "content": "c"}],
    )
    registry = state.read_injected("s1")
    assert registry["s1-ab12"]["title"] == "new"


def test_read_injected_different_fingerprints_do_not_collide(session_dir) -> None:
    """Cross-injection disambiguation: same rank + different fingerprints coexist."""
    state.append_injected(
        "s1",
        [{"id": "s1-0100", "kind": "playbook", "title": "older", "content": "c"}],
    )
    state.append_injected(
        "s1",
        [{"id": "s1-0200", "kind": "playbook", "title": "newer", "content": "c"}],
    )
    registry = state.read_injected("s1")
    assert registry["s1-0100"]["title"] == "older"
    assert registry["s1-0200"]["title"] == "newer"


def test_read_injected_skips_malformed_lines(session_dir) -> None:
    path = state.injected_path("s1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"id":"s1-ab12","kind":"playbook","title":"ok","content":"c"}\n'
        "not-json\n"
        '{"id":"p1-cd34","kind":"profile","title":"ok2","content":"c"}\n'
    )
    registry = state.read_injected("s1")
    assert set(registry.keys()) == {"s1-ab12", "p1-cd34"}


def test_read_injected_drops_entries_without_id(session_dir) -> None:
    path = state.injected_path("s1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"kind":"playbook","title":"ok","content":"c"}\n'
        '{"id":"","kind":"playbook","title":"ok","content":"c"}\n'
        '{"id":"s1-ab12","kind":"playbook","title":"ok","content":"c"}\n'
    )
    registry = state.read_injected("s1")
    assert set(registry.keys()) == {"s1-ab12"}


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


def test_unpublished_slice_excludes_local_keys_from_wire_turns(session_dir) -> None:
    """Raw host and cited_items keys never land in Reflexio wire turns."""
    records = [
        {"role": "User", "content": "hi", "host": "codex"},
        {
            "role": "Assistant",
            "content": "ok",
            "host": "codex",
            "cited_items": [{"id": "s1-ab12", "kind": "playbook", "title": "t"}],
        },
    ]
    _, turns = state.unpublished_slice(records)
    for turn in turns:
        assert "host" not in turn
        assert "cited_items" not in turn


def test_unpublished_slice_maps_cited_items_to_citations() -> None:
    """Resolved cited_items become a wire-shaped ``citations`` list on the turn."""
    records = [
        {"role": "User", "content": "hi"},
        {
            "role": "Assistant",
            "content": "ok",
            "cited_items": [
                {
                    "id": "s1-ab12",
                    "kind": "playbook",
                    "title": "rule X",
                    "real_id": "pb_42",
                },
                {
                    "id": "p1-cd34",
                    "kind": "profile",
                    "title": "user role",
                    "real_id": "prof_7",
                },
            ],
        },
    ]
    _, turns = state.unpublished_slice(records)
    assert turns[-1]["citations"] == [
        {"kind": "playbook", "real_id": "pb_42", "tag": "s1-ab12", "title": "rule X"},
        {
            "kind": "profile",
            "real_id": "prof_7",
            "tag": "p1-cd34",
            "title": "user role",
        },
    ]


def test_unpublished_slice_omits_citations_when_empty() -> None:
    """Empty / unresolvable cited_items → no ``citations`` key on the turn.

    Producing a key with ``[]`` would inflate every published Assistant
    record; absence is meaningful.
    """
    records = [
        {"role": "User", "content": "hi"},
        {
            "role": "Assistant",
            "content": "no real_id",
            "cited_items": [{"id": "s1-ab12", "kind": "playbook", "title": "t"}],
        },
        {
            "role": "Assistant",
            "content": "empty list",
            "cited_items": [],
        },
        {
            "role": "Assistant",
            "content": "no cited_items at all",
        },
    ]
    _, turns = state.unpublished_slice(records)
    for turn in turns[1:]:
        assert "citations" not in turn


def test_to_wire_citations_filters_invalid_kinds() -> None:
    """Items with unknown ``kind`` are dropped (server has a Literal there)."""
    items = [
        {"id": "s1-ab12", "kind": "playbook", "title": "ok", "real_id": "pb_1"},
        {
            "id": "x1-0001",
            "kind": "agent_playbook",
            "title": "explicit",
            "real_id": "ap_1",
        },
        {"id": "y1-0002", "kind": "", "title": "junk2", "real_id": "z_1"},
    ]
    result = state._to_wire_citations(items)
    assert [c["kind"] for c in result] == ["playbook", "agent_playbook"]
    assert result[0]["real_id"] == "pb_1"
    assert result[1]["real_id"] == "ap_1"


def test_to_wire_citations_preserves_explicit_playbook_source_kind() -> None:
    wire = state._to_wire_citations(
        [
            {
                "id": "s1-1",
                "kind": "playbook",
                "source_kind": "agent_playbook",
                "real_id": "20",
                "title": "Agent",
            },
            {
                "id": "s2-1",
                "kind": "playbook",
                "source_kind": "user_playbook",
                "real_id": "101",
                "title": "User",
            },
            {"id": "p1-1", "kind": "profile", "real_id": "p1", "title": "Profile"},
        ]
    )

    assert [item["kind"] for item in wire] == [
        "agent_playbook",
        "user_playbook",
        "profile",
    ]


def test_to_wire_citations_drops_unresolved_real_id() -> None:
    """Entries without ``real_id`` (unresolved injections) cannot round-trip."""
    items = [
        {"id": "s1-ab12", "kind": "playbook", "title": "no real_id"},
        {"id": "p1-cd34", "kind": "profile", "title": "empty", "real_id": ""},
        {"id": "s1-9999", "kind": "playbook", "title": "ok", "real_id": "pb_9"},
    ]
    result = state._to_wire_citations(items)
    assert len(result) == 1
    assert result[0]["real_id"] == "pb_9"


def test_to_wire_citations_handles_non_list_input() -> None:
    """None / dict / str inputs return ``[]`` without raising."""
    assert state._to_wire_citations(None) == []
    assert state._to_wire_citations({"id": "s1-ab12"}) == []
    assert state._to_wire_citations("oops") == []


def test_to_wire_citations_skips_non_dict_items() -> None:
    """A list that mixes dicts and junk only emits the dict entries."""
    items = [
        "a-string",
        None,
        42,
        {"id": "s1-ab12", "kind": "playbook", "title": "ok", "real_id": "pb_1"},
    ]
    result = state._to_wire_citations(items)
    assert len(result) == 1
    assert result[0]["tag"] == "s1-ab12"


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


class TestWirePayloadContract:
    """The wire dict must contain only fields the server's model accepts.

    ``unpublished_slice`` used to build the payload with a denylist, so buffer
    bookkeeping rode along. ``user_id`` was harmless (the server treats it as a
    benign request-level key and does not warn), but ``synthesised_by`` was
    reported, and a denylist rots every time a hook adds a record key.
    """

    def test_bookkeeping_keys_never_reach_the_wire(self):
        _, turns = state.unpublished_slice(
            [
                {
                    "ts": 1,
                    "role": "User",
                    "content": "x",
                    "user_id": "p",
                    "host": "h",
                    "synthesised_by": "s",
                }
            ]
        )
        assert turns, "expected one wire turn"
        # Assert a LITERAL set, not `<= _INTERACTION_DATA_FIELDS`: that is the
        # same constant the slicer filters by, so it could never fail while the
        # comprehension exists. Proven tautological by runtime mutation.
        assert set(turns[0]) == {"role", "content"}, turns[0]
        assert turns[0]["content"] == "x"

    def test_allowlist_covers_every_installed_model_field(self):
        """The allowlist must not be MISSING anything the model declares.

        Subset, not equality. The plugin is deliberately forward-compatible:
        it talks to a deployed server that can be newer than the pinned
        ``reflexio-ai`` release, so knowing a field the installed library has
        not caught up to yet is correct — the server accepts it, and an older
        server merely reports it as unrecognised.

        Equality broke CI for exactly that reason: the pinned PyPI 0.2.28 has
        no ``retrieved_learnings``, which this plugin has been sending (and the
        server accepting) for some time. What would be a real bug is the other
        direction — a field the installed model declares that the allowlist
        omits, because the slicer would then silently drop it.
        """
        interaction_data = pytest.importorskip(
            "reflexio.models.api_schema.domain.entities"
        ).InteractionData
        missing = set(interaction_data.model_fields) - state._INTERACTION_DATA_FIELDS
        assert not missing, (
            f"allowlist omits InteractionData field(s) {sorted(missing)};"
            " the slicer would silently drop them"
        )

    def test_created_at_is_never_emitted_even_when_present(self):
        """A literal `created_at` in the buffer must not reach the wire.

        The field is in the model-contract set (it is a real InteractionData
        field), so filtering on that set alone let it through. Only `ts` was
        tested, which could not catch this. Backdating an interaction hides it
        from the extractor permanently — see `_WIRE_FIELDS`.
        """
        _, turns = state.unpublished_slice(
            [{"ts": 1, "role": "User", "content": "x", "created_at": 999}]
        )
        assert "created_at" not in turns[0], turns[0]

    def test_wire_fields_excludes_created_at_but_contract_set_keeps_it(self):
        assert "created_at" in state._INTERACTION_DATA_FIELDS
        assert "created_at" not in state._WIRE_FIELDS
        assert state._WIRE_FIELDS < state._INTERACTION_DATA_FIELDS

    def test_buffer_timestamp_is_not_sent(self):
        """`created_at` must stay off the wire.

        Carrying the buffer's `ts` across was implemented and reverted: the
        extractor bookmark is keyed on interaction `created_at`, so a batch
        recovered after the bookmark moved was stored and then never extracted
        — permanent silent loss of learning data on the offline-recovery path
        this buffer exists for. The server stamping drain time is the lesser
        evil until ingest ordering stops depending on caller-supplied time.
        """
        _, turns = state.unpublished_slice(
            [{"ts": 1700000000, "role": "User", "content": "x", "user_id": "p"}]
        )
        assert "created_at" not in turns[0], turns[0]
