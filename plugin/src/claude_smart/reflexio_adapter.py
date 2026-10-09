"""Thin wrapper over ``reflexio.ReflexioClient`` for claude-smart's read/write paths.

Exists so hook handlers (a) don't import reflexio directly at module scope
— import failures shouldn't crash hooks — and (b) can be stubbed in tests.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from claude_smart import env_config, runtime

_LOGGER = logging.getLogger(__name__)

_ENV_URL = "REFLEXIO_URL"
_ENV_API_KEY = "REFLEXIO_API_KEY"
# Cap every HTTP round-trip from a hook so a hung backend can't stall the
# Claude Code / Codex session. reflexio's client default is 300s, which is
# fine for batch workloads but unacceptable on the hook path — a single
# unhealthy POST would freeze the user's prompt. 5s matches the precedent
# in ``ids.py`` for short-lived hook HTTP calls.
_HTTP_TIMEOUT_SECONDS = 5
_SEARCH_MODE_HYBRID = "hybrid"  # reflexio.models.config_schema.SearchMode.HYBRID
_SOURCE = "claude-smart"
_UNIFIED_ENTITY_TYPES = ("profiles", "user_playbooks", "agent_playbooks")
_AGENT_PLAYBOOK_APPROVAL_STATUSES = ("pending", "approved")
_REJECTED_AGENT_PLAYBOOK_STATUS = "rejected"
_LOCAL_8071_URLS = {
    "http://localhost:8071",
    "http://localhost:8071/",
    "http://127.0.0.1:8071",
    "http://127.0.0.1:8071/",
}


def _default_url() -> str:
    return f"http://localhost:{os.environ.get('BACKEND_PORT', '8071')}/"


def _configured_url() -> str:
    url = os.environ.get(_ENV_URL, "")
    backend_port = os.environ.get("BACKEND_PORT", "")
    if url in _LOCAL_8071_URLS and backend_port not in {"", "8071"}:
        return _default_url()
    return url or _default_url()


@dataclass(frozen=True)
class PublishResult:
    """Outcome and server-confirmed lineage for one publish call."""

    ok: bool
    request_id: str | None = None
    error_type: str | None = None
    http_status: int | None = None
    request_ids: tuple[str, ...] | None = None

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class Adapter:
    """Wraps the reflexio client and absorbs connection errors.

    All methods degrade to a neutral no-op result (an empty list or a falsey
    result) on connection failure so a missing or down reflexio server never
    crashes a host hook.
    """

    url: str = ""
    api_key: str = ""
    read_errors: list[str] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        env_config.load_reflexio_env()
        self.url = self.url or _configured_url()
        self.api_key = self.api_key or os.environ.get(_ENV_API_KEY, "")
        self._client: Any | None = None

    # -----------------------------------------------------------------
    # Client lazy-initialization
    # -----------------------------------------------------------------

    def _get_client(self) -> Any | None:
        """Return the Reflexio client, or None when it cannot be loaded."""
        if self._client is not None:
            return self._client
        try:
            from reflexio import ReflexioClient  # type: ignore[import-not-found]
        except ImportError as exc:
            _LOGGER.debug("reflexio not importable: %s", exc)
            return None
        try:
            self._client = ReflexioClient(
                url_endpoint=self.url,
                api_key=self.api_key,
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
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
        request_id: str | None = None,
        interactions: Sequence[dict[str, Any]],
        force_extraction: bool = False,
        override_learning_stall: bool = False,
        skip_aggregation: bool = False,
    ) -> PublishResult:
        """Publish interactions and return this call's confirmed request ID."""
        if not interactions:
            return PublishResult(True)
        client = self._get_client()
        if client is None:
            result = PublishResult(False, error_type="ClientUnavailable")
            self._log_publish(result, session_id, project_id, len(interactions))
            return result
        result, response = self._attempt_publish(
            client,
            session_id=session_id,
            project_id=project_id,
            request_id=request_id,
            interactions=interactions,
            force_extraction=force_extraction,
            override_learning_stall=override_learning_stall,
            skip_aggregation=skip_aggregation,
        )
        self._log_publish(result, session_id, project_id, len(interactions))
        if result.ok:
            # Deliberately outside `_attempt_publish` and every try inside it.
            # The publish has already been accepted, and `publish_unpublished`
            # advances the buffer watermark only on a truthy result — so a raise
            # while reading diagnostics would report a *successful* publish as
            # failed and re-send the same batch on every later hook. The nested
            # guard covers the logging handler too, not just the extraction.
            try:
                for warning in _publish_warnings(response):
                    _LOGGER.warning("reflexio dropped part of the payload: %s", warning)
            except Exception as exc:  # noqa: BLE001 — diagnostics must never fail a publish.
                _LOGGER.debug("could not read publish warnings: %s", exc)
        return result

    def _log_publish(
        self, result: PublishResult, session_id: str, project_id: str, count: int
    ) -> None:
        """Record useful transport diagnostics without credentials or payloads."""
        try:
            import json
            from pathlib import Path
            from urllib.parse import urlsplit

            from claude_smart import hook_log

            url = urlsplit(self.url)
            manifest = (
                Path(__file__).resolve().parents[2] / ".codex-plugin" / "plugin.json"
            )
            version = json.loads(manifest.read_text()).get("version", "unknown")
            hook_log.log_event(
                event="publish-result",
                host=runtime.host(),
                session_id=session_id,
                project_id=project_id,
                publish_status="ok" if result.ok else "failed",
                publish_count=count,
                extra={
                    "plugin_version": version,
                    "backend_scheme": url.scheme,
                    "backend_host": url.hostname,
                    "backend_port": url.port,
                    "error_type": result.error_type,
                    "http_status": result.http_status,
                },
            )
        except Exception:  # noqa: BLE001 — telemetry must never affect publication.
            return

    def _attempt_publish(
        self,
        client: Any,
        *,
        session_id: str,
        project_id: str,
        request_id: str | None,
        interactions: Sequence[dict[str, Any]],
        force_extraction: bool,
        override_learning_stall: bool,
        skip_aggregation: bool,
    ) -> tuple[PublishResult, Any]:
        """Run the publish and return ``(result, raw response)``.

        Split out of ``publish`` so the warning read has somewhere to live that
        is outside this method's ``except`` — see the caller.
        """
        try:
            interaction_list = _storage_safe_interactions(interactions)
            raw_request = getattr(client, "_make_request", None)
            if len(interaction_list) > 1000 and (
                request_id is None or not callable(raw_request)
            ):
                return PublishResult(
                    False, error_type="StablePublishingUnavailable"
                ), None
            if request_id is not None and callable(raw_request):
                confirmed_ids: list[str] = []
                raw_response: Any = None
                for index, start in enumerate(range(0, len(interaction_list), 1000)):
                    chunk = interaction_list[start : start + 1000]
                    chunk_id = (
                        request_id
                        if index == 0
                        else str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL, f"claude-smart:{request_id}:{index}"
                            )
                        )
                    )
                    payload: dict[str, Any] = {
                        "request_id": chunk_id,
                        "user_id": project_id,
                        "interaction_data_list": chunk,
                        "agent_version": runtime.agent_version(),
                        "session_id": session_id,
                        "skip_aggregation": skip_aggregation,
                        "force_extraction": force_extraction,
                        "evaluation_only": False,
                        "override_learning_stall": override_learning_stall,
                        "source": _SOURCE,
                    }
                    if len(interaction_list) > 1000 and _stored_publish_matches(
                        raw_request, payload
                    ):
                        confirmed_ids.append(chunk_id)
                        continue
                    try:
                        raw_response = raw_request(
                            "POST",
                            "/api/publish_interaction",
                            json=payload,
                            params=None,
                        )
                    except Exception as exc:  # noqa: BLE001
                        if len(interaction_list) > 1000 and _stored_publish_matches(
                            raw_request, payload
                        ):
                            confirmed_ids.append(chunk_id)
                            continue
                        if len(
                            interaction_list
                        ) > 1000 or not _needs_raw_retrieved_learning_publish(chunk):
                            raise
                        _LOGGER.warning(
                            "Could not confirm retrieved-learning links; retrying "
                            "the base interaction with the same request ID: %s",
                            exc,
                        )
                        fallback_payload = {
                            **payload,
                            "interaction_data_list": _without_retrieved_learnings(
                                chunk
                            ),
                        }
                        raw_response = raw_request(
                            "POST",
                            "/api/publish_interaction",
                            json=fallback_payload,
                            params=None,
                        )
                    result = _confirmed_publish(raw_response, chunk_id)
                    if not result:
                        if len(interaction_list) <= 1000 or not _stored_publish_matches(
                            raw_request, payload
                        ):
                            return result, raw_response
                    confirmed_ids.append(chunk_id)
                return PublishResult(
                    True,
                    confirmed_ids[-1],
                    request_ids=tuple(confirmed_ids)
                    if len(confirmed_ids) > 1
                    else None,
                ), raw_response
            if _needs_raw_retrieved_learning_publish(interaction_list):
                _LOGGER.warning(
                    "Stable raw publishing is unavailable; publishing "
                    "without optional retrieved-learning links"
                )
                interaction_list = _without_retrieved_learnings(interaction_list)
            kwargs = {
                "user_id": project_id,
                "interactions": interaction_list,
                "agent_version": runtime.agent_version(),
                "session_id": session_id,
                "wait_for_response": False,
                "force_extraction": force_extraction,
                "skip_aggregation": skip_aggregation,
                "source": _SOURCE,
                "override_learning_stall": override_learning_stall,
            }
            response = client.publish_interaction(**kwargs)
            response_request_id = getattr(response, "request_id", None)
            if isinstance(response_request_id, str) and response_request_id:
                return _confirmed_publish(response, response_request_id), response
            return _confirmed_publish(response), response
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("publish_interaction failed: %s", exc)
            status = getattr(getattr(exc, "response", None), "status_code", None)
            return PublishResult(
                False,
                error_type=type(exc).__name__,
                http_status=status if isinstance(status, int) else None,
            ), None

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
        dict below. SessionStart calls this only for local Reflexio URLs by
        default; ``CLAUDE_SMART_ENABLE_OPTIMIZER=1`` forces hosted URLs, and
        ``CLAUDE_SMART_ENABLE_OPTIMIZER=0`` disables it everywhere.
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

        User playbooks are scoped by the resolved project id. Filtering mirrors
        the publish path
        (``publish_interaction(user_id=project_id, …)``).

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
            response = client.get_user_playbooks(
                user_id=project_id,
                status_filter=[None],  # None => CURRENT in reflexio's filter API
                limit=top_k,
            )
        except Exception as exc:  # noqa: BLE001
            self._record_read_error("fetch_user_playbooks", exc)
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
            response = client.get_agent_playbooks(
                agent_version=runtime.agent_version(),
                status_filter=[None],
                limit=top_k,
            )
        except Exception as exc:  # noqa: BLE001
            self._record_read_error("fetch_agent_playbooks", exc)
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
            response = client.get_profiles(
                user_id=project_id,
                top_k=top_k,
                status_filter=[None],
            )
        except Exception as exc:  # noqa: BLE001
            self._record_read_error("fetch_project_profiles", exc)
            return []
        return _extract_items(response, "user_profiles")

    # -----------------------------------------------------------------
    # Query-aware unified search (used by PreToolUse / UserPromptSubmit)
    # -----------------------------------------------------------------

    def search_all(
        self,
        *,
        project_id: str,
        query: str,
        top_k: int = 5,
        session_id: str | None = None,
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
            session_id (str | None): Claude Code session id. When set, the
                server skips results it already returned to this session
                and backfills next-best matches, so repeated hook searches
                within one session stop re-injecting the same rules.

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
                session_id=session_id,
            )
        except Exception as exc:  # noqa: BLE001
            self._record_read_error("unified search", exc)
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

    def _record_read_error(self, operation: str, exc: Exception) -> None:
        message = f"{operation}: {exc}"
        self.read_errors.append(message)
        _LOGGER.debug("%s failed: %s", operation, exc)


def _publish_warnings(response: Any) -> list[str]:
    """Pull ``warnings`` off a publish response, tolerating any shape.

    Defensive rather than total: ``getattr`` swallows only ``AttributeError``,
    a mapping can override ``get``, and ``str`` runs a caller-supplied
    ``__str__``. The caller wraps this so those cannot fail an accepted
    publish. ``_extract_items`` is not reused because its ``list(value)``
    raises on a non-iterable.

    Both publish paths land here: the raw ``_make_request`` path returns a
    parsed JSON dict, the client path a response object.
    """
    if isinstance(response, dict):
        value = response.get("warnings")
    else:
        value = getattr(response, "warnings", None)
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value]


def _stored_publish_matches(raw_request: Any, payload: dict[str, Any]) -> bool:
    """Confirm the exact stored request before recovering a lost acknowledgement.

    GetRequests exposes complete text/tool/link fields, but not citations or
    image_encoding. Nonempty omitted fields cannot safely confirm a replay.
    """
    response = raw_request(
        "POST",
        "/api/get_requests",
        json={key: payload[key] for key in ("request_id", "user_id", "session_id")}
        | {"top_k": 1},
        params=None,
    )
    if not isinstance(response, dict) or response.get("success") is not True:
        raise ValueError("Stored publish confirmation query failed")
    sessions = response.get("sessions")
    if not isinstance(sessions, list):
        raise ValueError("Stored publish confirmation query is incomplete")
    if not sessions:
        return False
    if len(sessions) != 1 or sessions[0].get("session_id") != payload["session_id"]:
        raise ValueError("Stored publish confirmation returned another session")
    requests = sessions[0].get("requests")
    if not isinstance(requests, list) or len(requests) != 1:
        raise ValueError("Stored publish confirmation returned another request")
    stored = requests[0]
    request = stored.get("request", {})
    for key in (
        "request_id",
        "user_id",
        "session_id",
        "source",
        "agent_version",
        "evaluation_only",
    ):
        if request.get(key) != payload[key]:
            raise ValueError("Stored publish request identity does not match")
    interactions = stored.get("interactions")
    if not isinstance(interactions, list) or len(interactions) != len(
        payload["interaction_data_list"]
    ):
        raise ValueError("Stored publish interaction count does not match")

    from reflexio.models.api_schema.common import ToolUsed
    from reflexio.models.api_schema.domain.entities import InteractionData

    omitted = {"created_at", "citations", "image_encoding"}
    visible = set(InteractionData.model_fields) - omitted
    expected = payload["interaction_data_list"]
    for item in expected:
        if any(item.get(key) for key in ("citations", "image_encoding")) or set(
            item
        ) - set(InteractionData.model_fields):
            raise ValueError("Stored publish contains fields that cannot be verified")
        if any(
            set(tool) - set(ToolUsed.model_fields)
            for tool in item.get("tools_used", [])
        ):
            raise ValueError(
                "Stored publish contains tool fields that cannot be verified"
            )
        if any(
            set(ref) - {"kind", "learning_id"}
            for ref in item.get("retrieved_learnings", [])
        ):
            raise ValueError(
                "Stored publish contains learning fields that cannot be verified"
            )
    ordered = sorted(
        interactions, key=lambda item: (item["created_at"], item["interaction_id"])
    )
    for item, original in zip(ordered, expected, strict=True):
        if (
            item.get("request_id") != payload["request_id"]
            or item.get("user_id") != payload["user_id"]
        ):
            raise ValueError("Stored publish interaction identity does not match")
        # Real schema defaults normalize omitted content, tool status and lists.
        observed = InteractionData(
            **{key: item[key] for key in visible if key in item}
        ).model_dump(mode="json", exclude=omitted)
        normalized = InteractionData(**original).model_dump(
            mode="json", exclude=omitted
        )
        if observed != normalized:
            raise ValueError("Stored publish interaction payload does not match")
    return True


def _escape_nulls(value: Any) -> Any:
    """Represent null characters visibly; PostgreSQL JSON cannot store them."""
    if isinstance(value, str):
        return value.replace("\x00", "\\u0000")
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            escaped_key = _escape_nulls(key)
            if escaped_key in result:
                raise ValueError("Null escaping would overwrite a mapping key")
            result[escaped_key] = _escape_nulls(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_escape_nulls(item) for item in value]
    return value


def _storage_safe_interactions(
    interactions: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep complete tool activity within the server's per-message limit."""
    result = []
    for original in interactions:
        item = _escape_nulls(original)
        tools = item.get("tools_used", [])
        if len(tools) <= 1000:
            result.append(item)
            continue
        result.append({**item, "tools_used": tools[:1000]})
        for start in range(1000, len(tools), 1000):
            result.append(
                {
                    "role": item.get("role", "Assistant"),
                    "content": "Tool activity continued.",
                    "tools_used": tools[start : start + 1000],
                }
            )
    return result


def _confirmed_publish(response: Any, request_id: str | None = None) -> PublishResult:
    """HTTP success alone does not confirm that interactions were saved."""
    success = (
        dict.get(response, "success")
        if isinstance(response, dict)
        else getattr(response, "success", None)
    )
    if success is False:
        return PublishResult(
            False, request_id, error_type="ServerRejected", http_status=200
        )
    return PublishResult(True, request_id)


def _extract_items(response: Any, field: str) -> list[Any]:
    """Pull a list field from a reflexio response object or dict."""
    if response is None:
        return []
    if isinstance(response, dict):
        value = response.get(field)
    else:
        value = getattr(response, field, None)
    return list(value) if value else []


def _needs_raw_retrieved_learning_publish(
    interactions: Sequence[dict[str, Any]],
) -> bool:
    """Return whether links require a caller-stable raw request.

    The public client does not accept a caller-supplied request ID, and older
    clients also strip this field. The authenticated helper preserves both.
    """
    return any(item.get("retrieved_learnings") for item in interactions)


def _without_retrieved_learnings(
    interactions: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Copy interactions without the optional compatibility-only field."""

    return [
        {
            key: value
            for key, value in interaction.items()
            if key != "retrieved_learnings"
        }
        for interaction in interactions
    ]


def _filter_rejected_agent_playbooks(items: list[Any]) -> list[Any]:
    """Drop rejected shared skills defensively, even if a backend ignores filters."""
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
