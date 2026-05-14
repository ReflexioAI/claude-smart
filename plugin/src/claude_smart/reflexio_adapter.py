"""Thin wrapper over ``reflexio.ReflexioClient`` for claude-smart's read/write paths.

Exists so hook handlers (a) don't import reflexio directly at module scope
— import failures shouldn't crash hooks — and (b) can be stubbed in tests.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Sequence

from claude_smart import runtime

_LOGGER = logging.getLogger(__name__)

_ENV_URL = "REFLEXIO_URL"
_DEFAULT_URL = "http://localhost:8071/"
_SEARCH_MODE_HYBRID = "hybrid"  # reflexio.models.config_schema.SearchMode.HYBRID
_UNIFIED_ENTITY_TYPES = ("profiles", "user_playbooks", "agent_playbooks")
_AGENT_PLAYBOOK_APPROVAL_STATUSES = ("pending", "approved")
_REJECTED_AGENT_PLAYBOOK_STATUS = "rejected"


@dataclass
class Adapter:
    """Wraps the reflexio client and absorbs connection errors.

    All methods degrade to a neutral no-op return (empty list / False) on
    connection failure so a missing or down reflexio server never crashes
    a Claude Code hook.
    """

    url: str = ""

    def __post_init__(self) -> None:
        self.url = self.url or os.environ.get(_ENV_URL, _DEFAULT_URL)
        self._client: Any | None = None

    # -----------------------------------------------------------------
    # Client lazy-initialization
    # -----------------------------------------------------------------

    def _get_client(self) -> Any | None:
        """Return the ReflexioClient, or None if reflexio is unreachable/unimportable."""
        if self._client is not None:
            return self._client
        try:
            from reflexio import ReflexioClient  # type: ignore[import-not-found]
        except ImportError as exc:
            _LOGGER.debug("reflexio not importable: %s", exc)
            return None
        try:
            self._client = ReflexioClient(url_endpoint=self.url)
        except Exception as exc:  # noqa: BLE001 — adapter must never raise.
            _LOGGER.warning("Failed to construct ReflexioClient: %s", exc)
            return None
        return self._client

    # -----------------------------------------------------------------
    # Writes
    # -----------------------------------------------------------------

    def publish(
        self,
        *,
        session_id: str,
        project_id: str,
        interactions: Sequence[dict[str, Any]],
        force_extraction: bool = False,
        skip_aggregation: bool = False,
    ) -> bool:
        """Publish buffered interactions to reflexio. Returns True on success."""
        if not interactions:
            return True
        client = self._get_client()
        if client is None:
            return False
        try:
            client.publish_interaction(
                user_id=project_id,
                interactions=list(interactions),
                agent_version=runtime.agent_version(),
                session_id=session_id,
                wait_for_response=False,
                force_extraction=force_extraction,
                skip_aggregation=skip_aggregation,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("publish_interaction failed: %s", exc)
            return False

    def apply_extraction_defaults(self, *, window_size: int, stride_size: int) -> bool:
        """Push claude-smart's preferred extraction defaults to the reflexio server.

        Reads the current ``Config`` and only issues a ``set_config`` when the
        server-side values differ, so steady state is a single cheap GET.

        Reflexio persists ``Config`` to disk, so once these values land they
        survive backend restarts. The flip side: if an operator customizes
        ``window_size``/``stride_size`` via the dashboard, this call will
        overwrite those values back to the claude-smart defaults on the next
        SessionStart. To change the defaults, edit the constants at the call
        site in ``events/session_start.py``.

        Args:
            window_size (int): Desired ``Config.window_size`` on the server.
            stride_size (int): Desired ``Config.stride_size`` on the
                server. Must be ``<= window_size`` (reflexio enforces this).

        Returns:
            bool: True if the server is already at the target values or the
                write succeeded; False if reflexio is unreachable or the call
                raised.
        """
        client = self._get_client()
        if client is None:
            return False
        try:
            config = client.get_config()
            if (
                getattr(config, "window_size", None) == window_size
                and getattr(config, "stride_size", None) == stride_size
            ):
                return True
            config.window_size = window_size
            config.stride_size = stride_size
            client.set_config(config)
            return True
        except Exception as exc:  # noqa: BLE001 — adapter must never raise.
            _LOGGER.warning("apply_extraction_defaults failed: %s", exc)
            return False

    def apply_optimizer_defaults(
        self, *, script_path: str, timeout_seconds: int = 300
    ) -> bool:
        """Push claude-smart's shared skill optimizer defaults to reflexio.

        Idempotent compare-then-write: reads ``Config``, only issues a
        ``set_config`` when the server-side values differ from the desired
        dict below. Called unconditionally from SessionStart; the caller's
        only escape hatch is ``CLAUDE_SMART_ENABLE_OPTIMIZER=0``.
        """
        client = self._get_client()
        if client is None:
            return False
        try:
            config = client.get_config()
            opt = getattr(config, "playbook_optimizer_config", None)
            if opt is None:
                return False

            desired = {
                "enabled": True,
                "optimize_user_playbooks": False,
                "optimize_agent_playbooks": True,
                "auto_update_user_playbooks": True,
                "min_commit_windows": 1,
                "max_metric_calls": 15,
                "assistant_script_path": script_path,
                "assistant_script_args": [],
                "webhook_url": None,
                "webhook_timeout_seconds": timeout_seconds,
            }
            if all(getattr(opt, key, None) == value for key, value in desired.items()):
                return True
            for key, value in desired.items():
                setattr(opt, key, value)
            client.set_config(config)
            return True
        except Exception as exc:  # noqa: BLE001 — adapter must never raise.
            _LOGGER.warning("apply_optimizer_defaults failed: %s", exc)
            return False

    # -----------------------------------------------------------------
    # Stall-state reads/writes (used by SessionStart banner)
    # -----------------------------------------------------------------

    def fetch_stall_state(self) -> Any | None:
        """Fetch the current learning-stall snapshot from reflexio.

        Returns:
            Any | None: ``StallStateResponse``-shaped object (attribute access
                for stalled/reason/etc), or None when the reflexio server is
                unreachable. The caller must tolerate either case.
        """
        client = self._get_client()
        if client is None:
            return None
        try:
            return client.get_stall_state()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("get_stall_state failed: %s", exc)
            return None

    def mark_stall_notified(self) -> None:
        """Idempotently flip ``notified_in_cc`` on the active stall row.

        Returns:
            None
        """
        client = self._get_client()
        if client is None:
            return
        try:
            client.mark_stall_notified()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("mark_stall_notified failed: %s", exc)

    # -----------------------------------------------------------------
    # Broad reads (used by /show)
    # -----------------------------------------------------------------

    def fetch_user_playbooks(self, *, project_id: str, top_k: int = 10) -> list[Any]:
        """Fetch CURRENT user playbooks for ``project_id``.

        User playbooks are project-scoped: filtering by ``user_id=project_id``
        mirrors the publish path (``publish_interaction(user_id=project_id, …)``)
        so each project only sees the playbooks it produced.

        Args:
            project_id (str): reflexio ``user_id`` for this repo.
            top_k (int): Cap on results.

        Returns:
            list[Any]: User playbook records, possibly empty.
        """
        client = self._get_client()
        if client is None:
            return []
        try:
            response = client.search_user_playbooks(
                user_id=project_id,
                status_filter=[None],  # None => CURRENT in reflexio's filter API
                top_k=top_k,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("search_user_playbooks failed: %s", exc)
            return []
        return _extract_items(response, "user_playbooks")

    def fetch_agent_playbooks(self, top_k: int = 10) -> list[Any]:
        """Fetch CURRENT agent playbooks globally (shared across projects).

        Agent playbooks have no ``user_id`` field — they are aggregated from
        user playbooks across every project so that distilled lessons travel
        with the agent, not with a single repo. Filter by ``agent_version``
        so we only pull in playbooks produced by claude-code sessions.

        Args:
            top_k (int): Cap on results.

        Returns:
            list[Any]: Agent playbook records, possibly empty.
        """
        client = self._get_client()
        if client is None:
            return []
        try:
            response = client.search_agent_playbooks(
                agent_version=runtime.agent_version(),
                status_filter=[None],
                top_k=top_k,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("search_agent_playbooks failed: %s", exc)
            return []
        return _filter_rejected_agent_playbooks(
            _extract_items(response, "agent_playbooks")
        )

    def fetch_project_profiles(self, project_id: str, top_k: int = 20) -> list[Any]:
        """Fetch preferences extracted for this project (across sessions)."""
        client = self._get_client()
        if client is None:
            return []
        try:
            response = client.search_user_profiles(
                user_id=project_id,
                query="",
                top_k=top_k,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("search_user_profiles failed: %s", exc)
            return []
        return _extract_items(response, "user_profiles")

    # -----------------------------------------------------------------
    # Query-aware unified search (used by PreToolUse / UserPromptSubmit)
    # -----------------------------------------------------------------

    def search_all(
        self, *, project_id: str, query: str, top_k: int = 5
    ) -> tuple[list[Any], list[Any], list[Any]]:
        """Unified hybrid search → ``(user_playbooks, agent_playbooks, preferences)``.

        One round trip to ``/api/search`` fans out all three legs server-side.
        Reflexio's unified ``user_id`` filter scopes ``user_playbooks`` and
        preferences to this project; ``agent_playbooks`` carry no ``user_id``
        column so the same filter silently no-ops on that leg, leaving them
        global across projects.

        Args:
            project_id (str): reflexio ``user_id`` for this repo.
            query (str): Free-text query routed through BM25 + vector RRF.
            top_k (int): Cap on results per entity type.

        Returns:
            tuple[list[Any], list[Any], list[Any]]: ``(user_playbooks,
                agent_playbooks, preferences)``. Returns three empty lists on
                connection failure or any unified-search error so this
                wrapper never raises.
        """
        client = self._get_client()
        if client is None:
            return [], [], []
        try:
            response = client.search(
                query=query,
                user_id=project_id,
                agent_version=runtime.agent_version(),
                entity_types=list(_UNIFIED_ENTITY_TYPES),
                agent_playbook_status_filter=list(_AGENT_PLAYBOOK_APPROVAL_STATUSES),
                enable_agent_answer=False,
                top_k=top_k,
                search_mode=_SEARCH_MODE_HYBRID,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("unified search failed: %s", exc)
            return [], [], []
        return (
            _extract_items(response, "user_playbooks"),
            _filter_rejected_agent_playbooks(
                _extract_items(response, "agent_playbooks")
            ),
            _extract_items(response, "profiles"),
        )

    # -----------------------------------------------------------------
    # Broad fetch for explicit audit views (no query → can't use unified /api/search)
    # -----------------------------------------------------------------

    def fetch_all(
        self,
        *,
        project_id: str,
        user_playbook_top_k: int = 10,
        agent_playbook_top_k: int = 10,
        profile_top_k: int = 20,
    ) -> tuple[list[Any], list[Any], list[Any]]:
        """Parallel broad fetch for /show → ``(user_playbooks,
        agent_playbooks, preferences)``.

        Unified search rejects empty queries, so explicit audit views use
        per-entity endpoints. User playbooks and preferences are scoped to
        ``project_id``; agent playbooks are global (filtered only by
        ``agent_version``).

        Each leg absorbs its own exceptions and returns ``[]`` on failure,
        so this wrapper never raises.
        """
        with ThreadPoolExecutor(max_workers=3) as pool:
            up_future = pool.submit(
                self.fetch_user_playbooks,
                project_id=project_id,
                top_k=user_playbook_top_k,
            )
            ap_future = pool.submit(self.fetch_agent_playbooks, agent_playbook_top_k)
            pr_future = pool.submit(
                self.fetch_project_profiles, project_id, profile_top_k
            )
        return up_future.result(), ap_future.result(), pr_future.result()


def _extract_items(response: Any, field: str) -> list[Any]:
    """Pull a list field from a reflexio response object or dict, tolerating shape drift."""
    if response is None:
        return []
    if isinstance(response, dict):
        value = response.get(field)
    else:
        value = getattr(response, field, None)
    return list(value) if value else []


def _filter_rejected_agent_playbooks(items: list[Any]) -> list[Any]:
    """Drop rejected shared skills defensively, even if an older backend ignores filters."""
    return [
        item
        for item in items
        if _agent_playbook_status(item) != _REJECTED_AGENT_PLAYBOOK_STATUS
    ]


def _agent_playbook_status(item: Any) -> str:
    if isinstance(item, dict):
        value = item.get("playbook_status")
    else:
        value = getattr(item, "playbook_status", None)
    return str(value or "").lower()
