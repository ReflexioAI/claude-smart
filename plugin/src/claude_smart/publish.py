"""Publish-to-reflexio orchestration used by Stop, SessionEnd, and the sync CLI.

One helper — ``publish_unpublished`` — owns the read-buffer → slice → publish →
stamp-watermark sequence so the three call sites stay in sync. Returns a
``(status, interaction_count)`` tuple so callers can format appropriate
messaging without peeking at the adapter.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from filelock import FileLock, Timeout

from claude_smart import hook_log, runtime, state
from claude_smart.reflexio_adapter import PUBLISH_RESERVE_SECONDS, Adapter

PublishStatus = Literal[
    "nothing", "ok", "recovered", "failed", "queued", "deferred", "parked"
]

# A frozen batch the server rejects with the same logical reason this many
# times is parked so the rest of the session can drain. Only logical
# rejections count: HTTP 200 ``success: false`` and a 409 request-ID conflict.
# Transport errors, 402 billing stops, other 4xx and 5xx outages keep retrying
# indefinitely. A conflict the stored-request read also confirms is parked at
# once: the server compared the whole payload, so no retry can succeed.
POISON_REJECTION_LIMIT = 5
# The server's catch-all also reports a storage outage as a 200 rejection, so
# the identical streak must span this long before a batch is parked.
POISON_MIN_SPAN_SECONDS = 3600


def _publish_request_id(session_id: str, start: int, end: int) -> str:
    """Return a stable request ID for one buffered publish range."""

    name = f"claude-smart:{session_id}:{start}:{end}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def _prepare_publish_batch(
    session_id: str,
    through_record: int | None = None,
) -> tuple[int, int, list[dict[str, Any]]] | None:
    """Freeze and return one canonical unpublished record range."""

    while True:
        records = state.read_all(session_id)
        published, available = state.unpublished_slice(records)
        if through_record is not None:
            if published >= through_record:
                return None
            _, available = state.unpublished_slice(
                records[:through_record], published_override=published
            )
        if not available:
            return None

        publish_end = state.pending_publish_end(records, published)
        if publish_end is None:
            next_starts = [
                start
                for start, _, _ in state.reserved_publish_requests(records)
                if start > published
            ]
            # A closed response may have been cancelled. Retire its unattached
            # refs with that response rather than handing them to the next one.
            publish_end = (
                min(next_starts)
                if next_starts
                else state.safe_publish_end(records, published)
            )
            if through_record is not None:
                publish_end = min(publish_end, through_record)
            _, proposed = state.unpublished_slice(
                records[:publish_end], published_override=published
            )
            if not proposed:
                if next_starts:
                    # A closed search-only range has no response to publish.
                    # Keep its records locally without moving its references
                    # onto a later prompt or inventing a server-side request.
                    state.append(session_id, {"published_up_to": publish_end})
                    continue
                return None
            state.append(
                session_id,
                {"publish_attempt": {"start": published, "end": publish_end}},
            )

        canonical = state.read_all(session_id)
        if state.published_record_offset(canonical) != published:
            continue
        canonical_end = state.pending_publish_end(canonical, published)
        if canonical_end is None:
            continue
        if through_record is not None and canonical_end > through_record:
            return None
        _, interactions = state.unpublished_slice(
            canonical[:canonical_end], published_override=published
        )
        if interactions:
            return published, canonical_end, interactions


def publish_unpublished(
    *,
    session_id: str,
    project_id: str,
    force_extraction: bool,
    skip_aggregation: bool,
    override_learning_stall: bool = False,
    adapter: Adapter | None = None,
    max_batches: int | None = None,
    through_record: int | None = None,
    deadline: float | None = None,
) -> tuple[PublishStatus, int]:
    """Serialize drains across automatic workers and explicit manual publishers.

    ``deadline`` is a ``time.monotonic()`` instant after which the caller may
    be killed; no batch is sent unless ``PUBLISH_RESERVE_SECONDS`` remain.
    """
    path = state.session_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(path) + ".publish.lock", timeout=0):
            return _publish_unpublished_locked(
                session_id=session_id,
                project_id=project_id,
                force_extraction=force_extraction,
                skip_aggregation=skip_aggregation,
                override_learning_stall=override_learning_stall,
                adapter=adapter,
                max_batches=max_batches,
                through_record=through_record,
                deadline=deadline,
            )
    except Timeout:
        return "failed", 0


def _publish_unpublished_locked(
    *,
    session_id: str,
    project_id: str,
    force_extraction: bool,
    skip_aggregation: bool,
    override_learning_stall: bool,
    adapter: Adapter | None,
    max_batches: int | None,
    through_record: int | None,
    deadline: float | None,
) -> tuple[PublishStatus, int]:
    """Drain the session buffer to reflexio and stamp the high-water mark.

    Args:
        session_id (str): Claude Code session id, attached to each interaction.
        project_id (str): Stable user-scope id resolved by ``ids`` from the
            project name. ``agent_version`` is hardcoded to ``"claude-code"``
            in the adapter so skills roll up globally per agent rather than
            per project.
        force_extraction (bool): Whether an explicit manual operation may
            bypass extraction size and usefulness gates. Publication remains
            asynchronous; automatic hooks leave this False.
        override_learning_stall (bool): Whether to bypass a recorded
            provider auth/billing stall. Automatic hooks must leave this
            False; explicit manual retries set it True.
        skip_aggregation (bool): When True, reflexio extracts preferences and
            raw project-specific skill entries but skips the rollup into
            shared skills. claude-smart passes False on every publish
            path to permit ``user_playbooks`` to roll up into ``agent_playbooks``.
            The server's ``user_playbook_extractor_config.aggregation_config``
            owns aggregation policy; optimizer opt-ins do not enable rollups.
        adapter (Adapter | None): Injection point for tests; a fresh
            ``Adapter()`` is constructed when omitted.

    Returns:
        tuple[PublishStatus, int]: ``("nothing", 0)`` if the buffer has no
            unpublished turns, ``("ok", n)`` after a successful publish of
            ``n`` interactions, ``("recovered", n)`` when storage was verified
            after a lost acknowledgement (extraction options are not confirmed),
            or ``("failed", n)`` if reflexio rejected or
            was unreachable. On ``"failed"`` the watermark is not advanced,
            so the next hook retries the same batch. ``("deferred", n)``
            means the deadline left too little time to send the next batch;
            ``("parked", n)`` means a batch was parked after repeated
            identical rejections; ``n`` counts other batches published.
    """
    total = 0
    batches = 0
    recovered = False
    parked = False
    reserved_starts = {
        start
        for start, _, _ in state.reserved_publish_requests(state.read_all(session_id))
    }
    while (batch := _prepare_publish_batch(session_id, through_record)) is not None:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining < PUBLISH_RESERVE_SECONDS:
            _log_drain_event(
                "publish-deferred",
                session_id,
                project_id,
                batch,
                remaining_ms=round(remaining * 1000),
            )
            return "deferred", total
        status, count = _publish_batch(
            session_id,
            project_id,
            force_extraction,
            skip_aggregation,
            override_learning_stall,
            adapter,
            batch,
            deadline,
        )
        total += count
        batches += 1
        recovered = recovered or status == "recovered"
        parked = parked or status == "parked"
        if status == "deferred":
            return status, total
        if status == "failed":
            return status, total
        if max_batches is not None and batches >= max_batches:
            break
        # Drain only response reservations present when this call began. Hooks
        # can append concurrently; legacy batches retain their one-call boundary.
        watermark = state.published_record_offset(state.read_all(session_id))
        if not any(start >= watermark for start in reserved_starts):
            break
    # Parking skips interactions for good, so it outranks any later success.
    return (
        "parked"
        if parked
        else "recovered"
        if recovered
        else "ok"
        if total
        else "nothing",
        total,
    )


def _publish_batch(
    session_id: str,
    project_id: str,
    force_extraction: bool,
    skip_aggregation: bool,
    override_learning_stall: bool,
    adapter: Adapter | None,
    batch: tuple[int, int, list[dict[str, Any]]],
    deadline: float | None = None,
) -> tuple[PublishStatus, int]:
    """Publish one immutable response range, including legacy retry identities."""
    published_record_offset, publish_end, interactions = batch

    client = adapter if adapter is not None else Adapter()
    request_id = _publish_request_id(session_id, published_record_offset, publish_end)
    for start, reserved_id, _ in state.reserved_publish_requests(
        state.read_all(session_id)
    ):
        if start == published_record_offset:
            request_id = reserved_id
            break
    result = client.publish(
        session_id=session_id,
        project_id=project_id,
        request_id=request_id,
        interactions=interactions,
        force_extraction=force_extraction,
        override_learning_stall=override_learning_stall,
        skip_aggregation=skip_aggregation,
        deadline=deadline,
    )
    if result:
        marker: dict[str, Any] = {"published_up_to": publish_end}
        confirmed_request_id = result.request_id
        if isinstance(confirmed_request_id, str) and confirmed_request_id:
            marker["request_id"] = confirmed_request_id
        if result.request_ids:
            marker["request_ids"] = list(result.request_ids)
        state.append(session_id, marker)
        if result.no_op:
            return ("nothing", 0)
        return ("recovered" if result.recovered else "ok", len(interactions))
    if result.error_type == "DeadlineReached":
        _log_drain_event(
            "publish-deferred", session_id, project_id, batch, request_id=request_id
        )
        return ("deferred", 0)
    confirmed_conflict = result.error_type == "RequestIdConflict"
    if (
        confirmed_conflict
        or result.error_type == "ServerRejected"
        or result.error_reason == "request_id_conflict"
    ) and _park_if_poisoned(
        session_id,
        project_id,
        batch,
        request_id,
        ": ".join(filter(None, (result.error_reason, result.server_message))) or None,
        immediate=confirmed_conflict,
    ):
        return ("parked", 0)
    return ("failed", len(interactions))


def _park_if_poisoned(
    session_id: str,
    project_id: str,
    batch: tuple[int, int, list[dict[str, Any]]],
    request_id: str,
    reason: str | None,
    *,
    immediate: bool = False,
) -> bool:
    """Record one logical rejection; park the batch after a sustained streak.

    The streak is durable in the session buffer because every Stop is a new
    process. A different reason for the same batch restarts it. Parking writes
    the dead-letter record and the watermark in one append, so the range is
    never skipped without the record of why.
    """
    start, end, _ = batch
    now = time.time()
    identity = {"request_id": request_id, "start": start, "end": end}
    state.append(
        session_id, {"publish_rejection": {**identity, "reason": reason, "ts": now}}
    )
    streak: list[float] = []
    for record in state.read_all(session_id):
        rejection = record.get("publish_rejection")
        if not isinstance(rejection, dict) or any(
            rejection.get(key) != value for key, value in identity.items()
        ):
            continue
        ts = rejection.get("ts")
        if rejection.get("reason") != reason or not isinstance(ts, (int, float)):
            streak = []
            continue
        streak.append(float(ts))
    if not immediate and (
        len(streak) < POISON_REJECTION_LIMIT
        or now - streak[0] < POISON_MIN_SPAN_SECONDS
    ):
        return False
    state.append(
        session_id,
        {
            "published_up_to": end,
            "publish_dead_letter": {
                **identity,
                "reason": reason,
                "rejections": len(streak),
                "first_rejected_at": streak[0],
                "parked_at": now,
            },
        },
    )
    _log_drain_event(
        "publish-parked",
        session_id,
        project_id,
        batch,
        request_id=request_id,
        server_message=reason,
        rejections=len(streak),
    )
    return True


def _log_drain_event(
    event: str,
    session_id: str,
    project_id: str,
    batch: tuple[int, int, list[dict[str, Any]]],
    **extra: Any,
) -> None:
    """Record drain control decisions; diagnostics never affect publication."""
    start, end, _ = batch
    try:
        hook_log.log_event(
            event=event,
            host=runtime.host(),
            session_id=session_id,
            project_id=project_id,
            extra={"start": start, "end": end, **extra},
        )
    except Exception:  # noqa: BLE001 — telemetry must never affect publication.
        return
