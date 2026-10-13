"""Durable receipt preservation across injection and publication."""

# ruff: noqa: S101 -- Assertions are the pytest contract.
from pathlib import Path

from claude_smart import state


def test_receipts_survive_disk_buffer_and_preserve_distinct_versions(
    session_dir: Path,
) -> None:
    assert session_dir.is_dir()
    entries = [
        {
            "id": "first",
            "kind": "playbook",
            "source_kind": "user_playbook",
            "real_id": "7",
            "exposure_receipt": "served-version-one",
        },
        {
            "id": "second",
            "kind": "playbook",
            "source_kind": "user_playbook",
            "real_id": "7",
            "exposure_receipt": "served-version-two",
        },
    ]
    state.append_injected("receipt-session", entries)
    state.append("receipt-session", {"role": "Assistant", "content": "answer"})
    _, interactions = state.unpublished_slice(state.read_all("receipt-session"))
    assert interactions[0]["retrieved_learnings"] == [
        {
            "kind": "user_playbook",
            "learning_id": "7",
            "exposure_receipt": "served-version-one",
        },
        {
            "kind": "user_playbook",
            "learning_id": "7",
            "exposure_receipt": "served-version-two",
        },
    ]


def test_absent_and_empty_receipts_survive_as_distinct_references() -> None:
    session = "empty-receipt-session"
    entries = [
        {
            "id": "absent",
            "kind": "playbook",
            "source_kind": "user_playbook",
            "real_id": "7",
        },
        {
            "id": "empty",
            "kind": "playbook",
            "source_kind": "user_playbook",
            "real_id": "7",
            "exposure_receipt": "",
        },
    ]
    state.append_injected(session, entries)
    state.append(session, {"role": "Assistant", "content": "answer"})
    _, turns = state.unpublished_slice(state.read_all(session))
    assert turns[0]["retrieved_learnings"] == [
        {"kind": "user_playbook", "learning_id": "7"},
        {"kind": "user_playbook", "learning_id": "7", "exposure_receipt": ""},
    ]
