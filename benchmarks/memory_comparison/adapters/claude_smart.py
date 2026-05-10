"""claude-smart retrieval adapter for the benchmark.

Reuses ``claude_smart.reflexio_adapter.Adapter`` to query the reflexio
backend for the skills and preferences extracted during a scenario run.
Publish is non-blocking (matches production), so the adapter exposes a
``wait_for_extraction`` poll that waits until at least one preference or
project-specific skill materializes for the scenario.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parents[2]  # benchmarks/memory_comparison/adapters -> repo root
_SRC_DIR = _REPO_ROOT / "src"

if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from claude_smart.reflexio_adapter import Adapter  # noqa: E402


def wait_for_extraction(
    *, project_dir: Path, session_id: str, timeout_s: float = 60.0
) -> bool:  # pyright: ignore[reportUnusedParameter]
    """Poll reflexio until extraction produces at least one record.

    Reflexio's ``publish_interaction`` is fire-and-forget in production
    (the reflexio hook in claude-smart passes ``wait_for_response=False``
    so user turns aren't blocked on LLM extraction). For a benchmark we
    still need to know when the data is queryable.

    Args:
        project_dir (Path): Scenario project_id (used as reflexio user_id).
        session_id (str): Reflexio session_id (kept for symmetry with the
            claude-mem adapter; not consumed by the read path).
        timeout_s (float): Max wait budget before giving up.

    Returns:
        bool: True once any preference or project-specific skill has
            materialized, False on timeout. Callers still run their scored
            query either way.
    """
    _ = session_id
    deadline = time.monotonic() + timeout_s
    adapter = Adapter()
    target = str(project_dir)
    while time.monotonic() < deadline:
        # Both user playbooks and preferences are scoped to this scenario via
        # user_id=project_id (the publish path writes the same key). Agent
        # playbooks are global; we don't gate readiness on them here because
        # they're produced asynchronously by the optimizer well after extraction.
        user_pb, _agent_pb, profiles = adapter.fetch_all(
            project_id=target,
            user_playbook_top_k=50,
            agent_playbook_top_k=1,
            profile_top_k=1,
        )
        if user_pb or profiles:
            return True
        time.sleep(1.0)
    _LOGGER.warning(
        "claude-smart extraction did not materialize for session=%s within %ss",
        session_id,
        timeout_s,
    )
    return False


def _render(items: list[Any], label: str) -> list[str]:
    """Flatten skill/preference records into plain strings for scoring.

    Args:
        items (list[Any]): Records returned by ``Adapter.search_*``.
        label (str): Storage tag (``playbook`` or ``profile``) prepended to
            each line so the judge can tell them apart.

    Returns:
        list[str]: One string per item, non-empty fields joined.
    """
    out: list[str] = []
    for item in items:
        content = _get(item, "content") or _get(item, "text") or ""
        trigger = _get(item, "trigger") or ""
        rationale = _get(item, "rationale") or ""
        parts = [p for p in (content, trigger, rationale) if p]
        if parts:
            out.append(f"[{label}] " + " | ".join(parts))
    return out


def _get(obj: Any, field: str) -> str:
    """Read a field from a dict or dataclass-like record, empty string if absent."""
    if obj is None:
        return ""
    if isinstance(obj, dict):
        value = obj.get(field)
    else:
        value = getattr(obj, field, None)
    return str(value) if value else ""


def retrieve(
    *,
    project_dir: Path,
    session_id: str,
    probe_query: str,
    top_k: int = 5,
) -> list[str]:
    """Search skills and preferences via reflexio, return flat text list.

    Both legs of the fan-out tolerate failure (the adapter returns ``[]`` on
    backend error), so this function never raises.

    Args:
        project_dir (Path): Scenario scratch dir = project_id.
        session_id (str): Claude session id (unused by retrieval).
        probe_query (str): Retrieval query from the scenario.
        top_k (int): Results per entity type.

    Returns:
        list[str]: Rendered memory lines, shared skills first, then
            project-specific skills, then preferences.
    """
    _ = session_id
    adapter = Adapter()
    # User playbooks and preferences are scoped to this scenario server-side
    # (user_id=project_id mirrors the publish path). Agent playbooks are
    # global by design — they're aggregated cross-project lessons and join
    # the result set unfiltered.
    user_pb, agent_pb, profiles = adapter.search_all(
        project_id=str(project_dir),
        query=probe_query,
        top_k=top_k,
    )
    return (
        _render(agent_pb, "playbook")
        + _render(user_pb, "playbook")
        + _render(profiles, "profile")
    )
