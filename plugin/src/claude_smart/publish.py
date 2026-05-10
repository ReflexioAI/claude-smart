"""Publish-to-reflexio orchestration used by Stop, SessionEnd, and the sync CLI.

One helper — ``publish_unpublished`` — owns the read-buffer → slice → publish →
stamp-watermark sequence so the three call sites stay in sync. Returns a
``(status, interaction_count)`` tuple so callers can format appropriate
messaging without peeking at the adapter.
"""

from __future__ import annotations

from typing import Literal

from claude_smart import state
from claude_smart.reflexio_adapter import Adapter

PublishStatus = Literal["nothing", "ok", "failed"]


def publish_unpublished(
    *,
    session_id: str,
    project_id: str,
    force_extraction: bool,
    skip_aggregation: bool,
    adapter: Adapter | None = None,
) -> tuple[PublishStatus, int]:
    """Drain the session buffer to reflexio and stamp the high-water mark.

    Args:
        session_id (str): Claude Code session id, attached to each interaction.
        project_id (str): Stable project name; used as reflexio's
            ``user_id`` (profiles) so profiles accumulate at the project
            level across sessions. ``agent_version`` is hardcoded to
            ``"claude-code"`` in the adapter so playbooks roll up
            globally per agent rather than per project.
        force_extraction (bool): Whether to ask reflexio to run extraction
            synchronously instead of queuing for the next sweep.
        skip_aggregation (bool): When True, reflexio extracts profiles and
            raw playbook entries but skips the rollup into agent-level
            project playbooks. claude-smart passes False on every publish
            path so user_playbooks roll up into agent_playbooks; aggregation
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
    records = state.read_all(session_id)
    _, interactions = state.unpublished_slice(records)
    if not interactions:
        return ("nothing", 0)
    client = adapter if adapter is not None else Adapter()
    ok = client.publish(
        session_id=session_id,
        project_id=project_id,
        interactions=interactions,
        force_extraction=force_extraction,
        skip_aggregation=skip_aggregation,
    )
    if ok:
        state.append(session_id, {"published_up_to": len(records)})
        return ("ok", len(interactions))
    return ("failed", len(interactions))
