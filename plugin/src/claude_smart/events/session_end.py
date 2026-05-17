"""SessionEnd hook — flush any remaining interactions; extraction runs async."""

from __future__ import annotations

from typing import Any

from claude_smart import ids, publish


def handle(payload: dict[str, Any]) -> tuple[publish.PublishStatus, int] | None:
    """Flush any remaining buffered turns to reflexio.

    Args:
        payload (dict[str, Any]): Claude Code hook payload.

    Returns:
        tuple[PublishStatus, int] | None: The ``(status, count)`` tuple
            from ``publish.publish_unpublished`` so the dispatcher's
            forensic hook log captures the publish outcome. ``None``
            when the payload had no ``session_id``.
    """
    session_id = payload.get("session_id")
    if not session_id:
        return None
    project_id = ids.resolve_project_id(payload.get("cwd"))
    return publish.publish_unpublished(
        session_id=session_id,
        project_id=project_id,
        force_extraction=False,
        skip_aggregation=False,
    )
