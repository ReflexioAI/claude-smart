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

_LOGGER = logging.getLogger(__name__)

_ENV_URL = "REFLEXIO_URL"
_DEFAULT_URL = "http://localhost:8071/"
_SEARCH_MODE_HYBRID = "hybrid"  # reflexio.models.config_schema.SearchMode.HYBRID


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
                agent_version=project_id,
                session_id=session_id,
                wait_for_response=False,
                force_extraction=force_extraction,
                skip_aggregation=skip_aggregation,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("publish_interaction failed: %s", exc)
            return False

    def delete_all(self) -> tuple[dict[str, int], list[tuple[str, str]]] | None:
        """Delete all interactions, profiles, and user playbooks from reflexio.

        Returns:
            tuple[dict[str, int], list[tuple[str, str]]] | None: A
                ``(counts, errors)`` pair on reachable reflexio, or ``None``
                if the client could not be constructed at all. ``counts``
                maps entity name → deleted row count (``0`` for entities
                whose delete raised). ``errors`` is a list of
                ``(entity_name, exception_message)`` tuples for every
                individual failure, so the caller can distinguish "deleted
                nothing" from "delete raised" and surface it to the user.
        """
        client = self._get_client()
        if client is None:
            return None
        counts: dict[str, int] = {}
        errors: list[tuple[str, str]] = []
        for name, method in (
            ("interactions", "delete_all_interactions"),
            ("profiles", "delete_all_profiles"),
            ("user_playbooks", "delete_all_user_playbooks"),
        ):
            try:
                response = getattr(client, method)()
            except Exception as exc:  # noqa: BLE001 — adapter must never raise.
                _LOGGER.warning("%s failed: %s", method, exc)
                counts[name] = 0
                errors.append((name, str(exc)))
                continue
            counts[name] = getattr(response, "deleted_count", 0) or 0
        return counts, errors

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

    # -----------------------------------------------------------------
    # Reads (used by SessionStart)
    # -----------------------------------------------------------------

    def fetch_playbooks(self, top_k: int = 10) -> list[Any]:
        """Fetch CURRENT playbooks globally (across projects) for SessionStart priming.

        Playbooks are not scoped by ``agent_version``/``user_id`` on retrieval
        so lessons learned in one project are available in every project.
        """
        client = self._get_client()
        if client is None:
            return []
        try:
            response = client.search_user_playbooks(
                user_id=None,
                status_filter=[None],  # None => CURRENT in reflexio's filter API
                top_k=top_k,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("search_user_playbooks failed: %s", exc)
            return []
        return _extract_items(response, "user_playbooks")

    def fetch_project_profiles(self, project_id: str, top_k: int = 20) -> list[Any]:
        """Fetch profiles extracted for this project (across sessions)."""
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

    def search_playbooks(self, *, query: str, top_k: int = 5) -> list[Any]:
        """Query-aware CURRENT playbook search via reflexio hybrid retrieval.

        Playbooks are retrieved globally (no ``agent_version`` / ``user_id``
        filter) so lessons learned in one project are findable from any
        project.

        Args:
            query (str): Free-text query; routed through BM25 + vector RRF.
            top_k (int): Cap on results. Defaults to 5 for just-in-time inject.

        Returns:
            list[Any]: Playbook records (dicts or dataclasses), possibly empty.
        """
        client = self._get_client()
        if client is None:
            return []
        try:
            response = client.search_user_playbooks(
                user_id=None,
                query=query,
                status_filter=[None],
                top_k=top_k,
                search_mode=_SEARCH_MODE_HYBRID,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("search_playbooks failed: %s", exc)
            return []
        return _extract_items(response, "user_playbooks")

    def search_profiles(
        self, *, project_id: str, query: str, top_k: int = 5
    ) -> list[Any]:
        """Query-aware profile search scoped to this project.

        Args:
            project_id (str): reflexio user_id — profiles are project-scoped
                so they persist across sessions in the same repo.
            query (str): Free-text query.
            top_k (int): Cap on results.

        Returns:
            list[Any]: Profile records, possibly empty.
        """
        client = self._get_client()
        if client is None:
            return []
        try:
            response = client.search_user_profiles(
                user_id=project_id,
                query=query,
                top_k=top_k,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("search_user_profiles failed: %s", exc)
            return []
        return _extract_items(response, "user_profiles")

    # -----------------------------------------------------------------
    # Parallel fan-out
    # -----------------------------------------------------------------

    def search_both(
        self,
        *,
        project_id: str,
        query: str,
        top_k: int = 5,
    ) -> tuple[list[Any], list[Any]]:
        """Run ``search_playbooks`` + ``search_profiles`` concurrently.

        Playbook retrieval is global (``project_id`` is not forwarded to it);
        profile retrieval stays project-scoped.

        Returns:
            tuple[list[Any], list[Any]]: ``(playbooks, profiles)``. Each leg
                absorbs its own exceptions and returns ``[]`` on failure, so
                this wrapper never raises.
        """
        return self._fan_out(
            playbook_call=lambda: self.search_playbooks(query=query, top_k=top_k),
            profile_call=lambda: self.search_profiles(
                project_id=project_id, query=query, top_k=top_k
            ),
        )

    def fetch_both(
        self,
        *,
        project_id: str,
        playbook_top_k: int = 10,
        profile_top_k: int = 20,
    ) -> tuple[list[Any], list[Any]]:
        """Parallel broad fetch for SessionStart (empty-query, recency order).

        Playbooks are fetched globally; profiles are scoped to ``project_id``.
        """
        return self._fan_out(
            playbook_call=lambda: self.fetch_playbooks(top_k=playbook_top_k),
            profile_call=lambda: self.fetch_project_profiles(
                project_id, top_k=profile_top_k
            ),
        )

    @staticmethod
    def _fan_out(*, playbook_call, profile_call) -> tuple[list[Any], list[Any]]:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pb_future = pool.submit(playbook_call)
            pr_future = pool.submit(profile_call)
        return pb_future.result(), pr_future.result()


def _extract_items(response: Any, field: str) -> list[Any]:
    """Pull a list field from a reflexio response object or dict, tolerating shape drift."""
    if response is None:
        return []
    if isinstance(response, dict):
        value = response.get(field)
    else:
        value = getattr(response, field, None)
    return list(value) if value else []
