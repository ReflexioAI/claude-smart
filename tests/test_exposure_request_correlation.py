"""The search hook must send the request_id the next publish will use.

WHY THIS IS A CONTRACT AND NOT A DETAIL. Reflexio records an exposure event for
every playbook a search serves, and the offline tuner resolves that event to a
session ONLY through ``request_id``, against the request publish retained --
there is no reverse lookup from a session. An exposure carrying no request_id
can never be joined to the conversation it influenced, so the tuner sees a
playbook with no evidence and abstains.

Measured against production on 2026-09-22: all 95,481 stored exposure events
carry neither ``request_id`` nor ``session_id``, and the server has since begun
refusing uncorrelated batches outright, so such a search now contributes
nothing at all.

The invariant these tests pin is agreement: the id the SEARCH sends and the id
the PUBLISH uses must be the same string for the same buffered range. Testing
either side alone cannot catch a drift between them.
"""

from __future__ import annotations

from typing import Any

from claude_smart import publish, state
from claude_smart.reflexio_adapter import PublishResult


class _RecordingAdapter:
    def __init__(self) -> None:
        self.publish_calls: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []

    def publish(self, **kwargs: Any) -> PublishResult:
        self.publish_calls.append(kwargs)
        return PublishResult(True, kwargs.get("request_id"))

    def search_all(self, **kwargs: Any) -> tuple[list, list, list]:
        self.search_calls.append(kwargs)
        return ([], [], [])


def test_appending_records_does_not_change_the_pending_id(session_dir):
    """THE REASON THE KEY CHANGED, pinned directly.

    The id used to include the range END, which does not exist while the hook
    is running. This asserts the property that made it unusable: growing the
    unpublished range must leave the pending id alone.
    """
    session = "sess-grow"

    before_any_records = publish.request_id_for_next_publish(session)
    state.append(session, {"role": "User", "content": "how do I escalate?"})
    after_one = publish.request_id_for_next_publish(session)
    state.append(session, {"role": "Agent", "content": "Open the account first."})
    after_two = publish.request_id_for_next_publish(session)

    assert before_any_records == after_one == after_two


def test_the_id_survives_the_turn_it_was_minted_before(session_dir):
    """THE INVARIANT, and the ORDER here is the whole test.

    The hook computes the id BEFORE the agent answers, then the turn appends
    records, then the publish goes out. So the id must not depend on anything
    the turn changes -- which is exactly what keying on the range END did.

    Computing the id after appending would fix ``end`` first and the assertion
    would hold for any formula: a version of this test that seeded the records
    up front passed against a deliberately end-keyed mutant, proving nothing.
    """
    session = "sess-agree"

    # 1. The hook runs first, and must commit to an id now.
    sent_at_search = publish.request_id_for_next_publish(session)

    # 2. Only then does the turn happen and extend the range.
    state.append(session, {"role": "User", "content": "how do I escalate?"})
    state.append(session, {"role": "Agent", "content": "Open the account first."})

    adapter = _RecordingAdapter()
    status, _ = publish.publish_unpublished(
        session_id=session,
        project_id="proj",
        adapter=adapter,
        force_extraction=False,
        skip_aggregation=False,
    )

    assert status == "ok"
    assert adapter.publish_calls, "nothing was published"
    assert adapter.publish_calls[0]["request_id"] == sent_at_search


def test_a_failed_publish_reuses_the_id_rather_than_orphaning_it(session_dir):
    """A retry after a failure is the SAME logical batch.

    The watermark only advances on success, so a second attempt starts where
    the first did and must reuse the id -- otherwise the exposures the first
    search recorded point at a request that is never published, and the retry
    mints a request nothing points at.
    """
    session = "sess-retry"
    state.append(session, {"role": "User", "content": "escalate this please"})

    first = publish.request_id_for_next_publish(session)
    # No successful publish happened, so the watermark has not moved.
    second = publish.request_id_for_next_publish(session)
    assert first == second


def test_a_successful_publish_moves_the_id_on(session_dir):
    """The converse, and the half that would fail if the key were a constant:
    once a range is published the next search must NOT reuse its id."""
    session = "sess-advance"
    state.append(session, {"role": "User", "content": "escalate this please"})
    state.append(session, {"role": "Agent", "content": "Opening the account."})

    before = publish.request_id_for_next_publish(session)
    adapter = _RecordingAdapter()
    status, _ = publish.publish_unpublished(
        session_id=session,
        project_id="proj",
        adapter=adapter,
        force_extraction=False,
        skip_aggregation=False,
    )
    assert status == "ok"

    state.append(session, {"role": "User", "content": "and the refund?"})
    after = publish.request_id_for_next_publish(session)

    assert after != before, "a new range must not reuse the published range's id"
