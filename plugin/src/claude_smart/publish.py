"""Publish-to-reflexio orchestration used by Stop, SessionEnd, and the sync CLI.

One helper — ``publish_unpublished`` — owns the read-buffer → slice → publish →
stamp-watermark sequence so the three call sites stay in sync. Returns a
``(status, interaction_count)`` tuple so callers can format appropriate
messaging without peeking at the adapter.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from claude_smart import state
from claude_smart.reflexio_adapter import Adapter

PublishStatus = Literal["nothing", "ok", "failed"]


def _publish_request_id(session_id: str, start: int, end: int) -> str:
    """Return a stable request ID for one buffered publish range."""

    name = f"claude-smart:{session_id}:{start}:{end}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def _prepare_publish_batch(
    session_id: str,
) -> tuple[int, int, list[dict[str, Any]]] | None:
    """Freeze and return one canonical unpublished record range."""

    while True:
        records = state.read_all(session_id)
        published, available = state.unpublished_slice(records)
        if not available:
            return None

        publish_end = state.pending_publish_end(records, published)
        if publish_end is None:
            publish_end = state.safe_publish_end(records, published)
            _, proposed = state.unpublished_slice(
                records[:publish_end], published_override=published
            )
            if not proposed:
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
) -> tuple[PublishStatus, int]:
    """Drain the session buffer to reflexio and stamp the high-water mark.

    Args:
        session_id (str): Claude Code session id, attached to each interaction.
        project_id (str): Stable user-scope id resolved by ``ids`` from the
            project name. ``agent_version`` is hardcoded to ``"claude-code"``
            in the adapter so skills roll up globally per agent rather than
            per project.
        force_extraction (bool): Whether to ask reflexio to run extraction
            synchronously instead of queuing for the next sweep.
        override_learning_stall (bool): Whether to bypass a recorded
            provider auth/billing stall. Automatic hooks must leave this
            False; explicit manual retries set it True.
        skip_aggregation (bool): When True, reflexio extracts preferences and
            raw project-specific skill entries but skips the rollup into
            shared skills. claude-smart passes False on every publish
            path so ``user_playbooks`` roll up into ``agent_playbooks``; aggregation
            additionally requires `aggregation_config` to be set on
            reflexio's `user_playbook_extractor_configs[0]` and
            `optimize_agent_playbooks=true` at the top level — otherwise
            the rollup silently no-ops.
        adapter (Adapter | None): Injection point for tests; a fresh
            ``Adapter()`` is constructed when omitted.

    Returns:
        tuple[PublishStatus, int]: ``("nothing", 0)`` if the buffer has no
            unpublished turns, ``("ok", n)`` after a successful publish of
            ``n`` interactions, or ``("failed", n)`` if reflexio rejected or
            was unreachable. On ``"failed"`` the watermark is not advanced,
            so the next hook retries the same batch.
    """
    batch = _prepare_publish_batch(session_id)
    if batch is None:
        return ("nothing", 0)
    published_record_offset, publish_end, interactions = batch

    client = adapter if adapter is not None else Adapter()
    ok = client.publish(
        session_id=session_id,
        project_id=project_id,
        request_id=_publish_request_id(
            session_id, published_record_offset, publish_end
        ),
        interactions=interactions,
        force_extraction=force_extraction,
        override_learning_stall=override_learning_stall,
        skip_aggregation=skip_aggregation,
    )
    if ok:
        state.append(session_id, {"published_up_to": publish_end})
        return ("ok", len(interactions))
    return ("failed", len(interactions))
