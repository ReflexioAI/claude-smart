"""Thin wrapper over ``reflexio.ReflexioClient`` for claude-smart's read/write paths.

Exists so hook handlers (a) don't import reflexio directly at module scope
— import failures shouldn't crash hooks — and (b) can be stubbed in tests.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
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
# Worst case for one stable publish attempt: the POST plus one stored-request
# verification read. Hook drains reserve this before sending a batch.
PUBLISH_ATTEMPT_SECONDS = 2 * _HTTP_TIMEOUT_SECONDS
# A deadline-bound publish sends a request only when the attempt and the local
# outcome write still fit before the host kills the hook. Sending without that
# margin is how a stored request lost its watermark.
PUBLISH_RESERVE_SECONDS = PUBLISH_ATTEMPT_SECONDS + 2
# GetRequests does not return these fields, so a replay cannot compare them.
_UNVERIFIABLE_FIELDS = ("citations", "image_encoding")
_SERVER_MESSAGE_LIMIT = 200
# Reflexio >= #807 answers a reused request_id naming different content with
# HTTP 409 and this reason; an identical retry succeeds with ``replayed``.
_REQUEST_ID_CONFLICT = "request_id_conflict"
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
    recovered: bool = False
    no_op: bool = False
    server_message: str | None = None
    error_reason: str | None = None
    unverified_fields: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.ok


class _StoredPublishMismatch(ValueError):
    """The stored request under this ID is decidably not this payload."""


@dataclass(frozen=True)
class _StoredPublish:
    """A stored request matched on every field the read API exposes."""

    unverified_fields: tuple[str, ...] = ()


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

    def finalize_session(self, *, session_id: str, project_id: str) -> bool:
        """Accept a judged completion hint, including an already-published tail.

        Older backends may lack the endpoint. Never republish saved messages or
        substitute force extraction; the hook must remain safe and nonblocking.
        """
        client = self._get_client()
        if client is None:
            return False
        try:
            response = client._make_request(
                "POST",
                "/api/finalize_session",
                json={"user_id": project_id, "session_id": session_id},
            )
            if isinstance(response, dict) and response.get("success") is True:
                return True
            _LOGGER.warning(
                "Session completion hint was not accepted; saved messages remain stored."
            )
        except Exception as exc:  # noqa: BLE001 — hooks must survive older/unavailable servers.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in {404, 405, 501}:
                _LOGGER.warning(
                    "Backend does not support session finalization; update Reflexio to enable judged short-session learning. Saved messages remain stored."
                )
            else:
                _LOGGER.warning(
                    "Session completion hint failed (%s); saved messages remain stored.",
                    type(exc).__name__,
                )
        return False

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
        deadline: float | None = None,
    ) -> PublishResult:
        """Publish interactions and return this call's confirmed request ID.

        With a ``time.monotonic()`` ``deadline``, no stable request is sent
        unless ``PUBLISH_RESERVE_SECONDS`` remain; the batch then fails as
        ``DeadlineReached`` and keeps its frozen request identities.
        """
        if not interactions:
            return PublishResult(True)
        client = self._get_client()
        if client is None:
            result = PublishResult(False, error_type="ClientUnavailable")
            self._log_publish(
                result, session_id, project_id, len(interactions), request_id
            )
            return result
        result, _response = self._attempt_publish(
            client,
            session_id=session_id,
            project_id=project_id,
            request_id=request_id,
            interactions=interactions,
            force_extraction=force_extraction,
            override_learning_stall=override_learning_stall,
            skip_aggregation=skip_aggregation,
            deadline=deadline,
        )
        self._log_publish(
            result,
            session_id,
            project_id,
            0 if result.no_op else len(interactions),
            request_id,
        )
        return result

    def _log_publish(
        self,
        result: PublishResult,
        session_id: str,
        project_id: str,
        count: int,
        request_id: str | None = None,
    ) -> None:
        """Record useful transport diagnostics without credentials or payloads."""
        try:
            from urllib.parse import urlsplit

            from claude_smart import hook_log

            url = None
            scheme = None
            hostname = None
            try:
                url = urlsplit(self.url)
                scheme = url.scheme
                hostname = url.hostname
            except ValueError:
                pass
            try:
                port = url.port if url is not None else None
            except ValueError:
                port = None
            extra: dict[str, Any] = {
                "backend_scheme": scheme,
                "backend_host": hostname,
                "backend_port": port,
                "error_type": result.error_type,
                "http_status": result.http_status,
            }
            if not result.ok:
                extra["request_id"] = result.request_id or request_id
                extra["server_message"] = result.server_message
                extra["error_reason"] = result.error_reason
            if result.unverified_fields:
                extra["unverified_fields"] = list(result.unverified_fields)
            hook_log.log_event(
                event="publish-result",
                host=runtime.host(),
                session_id=session_id,
                project_id=project_id,
                publish_status=(
                    "nothing"
                    if result.no_op
                    else "recovered"
                    if result.recovered
                    else "ok"
                )
                if result.ok
                else "failed",
                publish_count=count,
                extra=extra,
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
        deadline: float | None = None,
    ) -> tuple[PublishResult, Any]:
        """Run the publish and return ``(result, raw response)``.

        Warning diagnostics use a guarded helper and cannot change the outcome.
        """
        try:
            interaction_list = _storage_safe_interactions(interactions)
            if not interaction_list:
                return PublishResult(True, no_op=True), None
            raw_request = getattr(client, "_make_request", None)
            if len(interaction_list) > 1000 and (
                request_id is None or not callable(raw_request)
            ):
                return PublishResult(
                    False, error_type="StablePublishingUnavailable"
                ), None
            if request_id is not None and callable(raw_request):
                confirmed_ids: list[str] = []
                recovered = False
                unverified: set[str] = set()
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
                    if len(interaction_list) > 1000:
                        try:
                            stored = _stored_publish_matches(raw_request, payload)
                        except Exception:  # noqa: BLE001 — a read outage must not block fresh writes.
                            stored = None
                        if stored:
                            recovered = True
                            unverified.update(stored.unverified_fields)
                            confirmed_ids.append(chunk_id)
                            continue
                    if (
                        deadline is not None
                        and deadline - time.monotonic() < PUBLISH_RESERVE_SECONDS
                    ):
                        # Accepted earlier chunks are recovered by the next
                        # drain's preflight read under the same identities.
                        return PublishResult(
                            False, chunk_id, error_type="DeadlineReached"
                        ), raw_response
                    try:
                        raw_response = raw_request(
                            "POST",
                            "/api/publish_interaction",
                            json=payload,
                            params=None,
                        )
                    except Exception as exc:  # noqa: BLE001
                        response = getattr(exc, "response", None)
                        reason, message = _http_error_details(response)
                        try:
                            stored = _stored_publish_matches(raw_request, payload)
                        except _StoredPublishMismatch:
                            if reason != _REQUEST_ID_CONFLICT:
                                raise exc from None
                            stored = None
                        except Exception:  # noqa: BLE001 — preserve the publish failure.
                            raise exc from None
                        if stored:
                            recovered = True
                            unverified.update(stored.unverified_fields)
                            confirmed_ids.append(chunk_id)
                            continue
                        if reason == _REQUEST_ID_CONFLICT:
                            # The server compared the full payload and the
                            # read confirms the stored request is not this one.
                            return PublishResult(
                                False,
                                chunk_id,
                                error_type="RequestIdConflict",
                                http_status=getattr(response, "status_code", None),
                                server_message=message,
                                error_reason=reason,
                            ), raw_response
                        raise
                    _log_publish_warnings(raw_response)
                    result = _confirmed_publish(raw_response, chunk_id)
                    recovered = recovered or result.recovered
                    if not result:
                        conflict = replace(result, error_type="RequestIdConflict")
                        try:
                            stored = _stored_publish_matches(raw_request, payload)
                        except _StoredPublishMismatch:
                            if result.error_reason == _REQUEST_ID_CONFLICT:
                                return conflict, raw_response
                            return result, raw_response
                        except Exception:  # noqa: BLE001 — preserve rejection diagnostics.
                            return result, raw_response
                        if not stored:
                            if result.error_reason == _REQUEST_ID_CONFLICT:
                                return conflict, raw_response
                            return result, raw_response
                        recovered = True
                        unverified.update(stored.unverified_fields)
                    confirmed_ids.append(chunk_id)
                return PublishResult(
                    True,
                    confirmed_ids[-1],
                    request_ids=tuple(confirmed_ids)
                    if len(confirmed_ids) > 1
                    else None,
                    recovered=recovered,
                    unverified_fields=tuple(sorted(unverified)),
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
            _log_publish_warnings(response)
            response_request_id = getattr(response, "request_id", None)
            if isinstance(response_request_id, str) and response_request_id:
                return _confirmed_publish(response, response_request_id), response
            return _confirmed_publish(response), response
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("publish_interaction failed: %s", exc)
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            reason, message = _http_error_details(response)
            return PublishResult(
                False,
                error_type=type(exc).__name__,
                http_status=status if isinstance(status, int) else None,
                server_message=message,
                error_reason=reason,
            ), None

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
        request_id: str | None = None,
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
            request_id (str | None): Durable identity reserved for the response
                that will publish this retrieved context.

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
                request_id=request_id,
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


def _log_publish_warnings(response: Any) -> None:
    """Keep every chunk's field-drop diagnostics without affecting acceptance."""
    try:
        for warning in _publish_warnings(response):
            _LOGGER.warning("reflexio dropped part of the payload: %s", warning)
    except Exception:  # noqa: BLE001 — response properties and handlers may raise.
        return


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


def _canonical_learning_reference(ref: dict[str, Any]) -> dict[str, Any]:
    """Compare submitted receipts with their server-derived stored digests."""
    canonical = dict(ref)
    receipt = canonical.pop("exposure_receipt", None)
    if receipt is not None:
        if not isinstance(receipt, str) or len(receipt) > 8_192:
            raise ValueError("Stored publish receipt has invalid shape")
        canonical["exposure_receipt_digest"] = hashlib.sha256(
            receipt.encode()
        ).hexdigest()
    digest = canonical.get("exposure_receipt_digest")
    if digest is None:
        canonical.pop("exposure_receipt_digest", None)
    elif (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise ValueError("Stored publish receipt digest has invalid shape")
    return canonical


def _stored_publish_matches(
    raw_request: Any, payload: dict[str, Any]
) -> _StoredPublish | None:
    """Confirm the stored request before recovering a lost acknowledgement.

    Returns ``None`` when nothing is stored under the request ID, and raises on
    any identity, count or visible-field mismatch. GetRequests exposes complete
    text/tool/link fields, but not citations or image_encoding. The request ID
    names one frozen buffer range, so a stored request whose every visible field
    matches is that range's earlier delivery; nonempty omitted fields are
    reported as unverified rather than blocking recovery. Refusing them wedged
    every later turn behind a request the server had already stored. Unknown
    fields still refuse: the read API's coverage of them is unknown.
    """
    import json

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
        return None
    if len(sessions) != 1 or sessions[0].get("session_id") != payload["session_id"]:
        raise _StoredPublishMismatch(
            "Stored publish confirmation returned another session"
        )
    requests = sessions[0].get("requests")
    if not isinstance(requests, list) or len(requests) != 1:
        raise _StoredPublishMismatch(
            "Stored publish confirmation returned another request"
        )
    stored = requests[0]
    request = stored.get("request", {})
    identity_fields = (
        "request_id",
        "user_id",
        "session_id",
        "source",
        "agent_version",
        "evaluation_only",
    )
    if any(request.get(key) != payload[key] for key in identity_fields):
        raise _StoredPublishMismatch("Stored publish request identity does not match")
    interactions = stored.get("interactions")
    if not isinstance(interactions, list) or len(interactions) != len(
        payload["interaction_data_list"]
    ):
        raise _StoredPublishMismatch("Stored publish interaction count does not match")

    from reflexio.models.api_schema.common import ToolUsed
    from reflexio.models.api_schema.domain.entities import InteractionData

    omitted: set[str] = set(_UNVERIFIABLE_FIELDS)
    visible = (set(InteractionData.model_fields) | {"retrieved_learnings"}) - omitted
    expected = payload["interaction_data_list"]
    unverified = tuple(
        key for key in _UNVERIFIABLE_FIELDS if any(item.get(key) for item in expected)
    )
    for item in expected:
        unverifiable = (
            bool(
                set(item)
                - (set(InteractionData.model_fields) | {"retrieved_learnings"})
            )
            or any(
                set(tool) - (set(ToolUsed.model_fields) | {"status"})
                for tool in item.get("tools_used", [])
            )
            or any(
                set(ref)
                - {"kind", "learning_id", "exposure_receipt", "exposure_receipt_digest"}
                for ref in item.get("retrieved_learnings", [])
            )
        )
        if unverifiable:
            raise ValueError("Stored publish contains fields that cannot be verified")
    # Ingestion IDs preserve payload order even for backdated timestamps.
    ordered = sorted(interactions, key=lambda item: item["interaction_id"])
    for item, original in zip(ordered, expected, strict=True):
        if (
            item.get("request_id") != payload["request_id"]
            or item.get("user_id") != payload["user_id"]
        ):
            raise _StoredPublishMismatch(
                "Stored publish interaction identity does not match"
            )
        # Real schema defaults normalize omitted content, tool status and lists.
        # Caller-provided timestamps are part of the payload; server defaults are not.
        excluded = omitted if "created_at" in original else omitted | {"created_at"}
        observed = InteractionData(
            **{key: item[key] for key in visible if key in item}
        ).model_dump(mode="json", exclude=excluded)
        normalized = InteractionData(**original).model_dump(
            mode="json", exclude=excluded
        )
        # Retrieved identities may also predate the installed SDK. Compare
        # these known wire pairs directly; missing nonempty links never confirm.
        for target, source in ((observed, item), (normalized, original)):
            refs = source.get("retrieved_learnings", [])
            if not isinstance(refs, list) or len(refs) > 1000:
                raise ValueError("Stored publish learning links have invalid shape")
            for ref in refs:
                if (
                    not isinstance(ref, dict)
                    or not {"kind", "learning_id"}.issubset(ref)
                    or set(ref)
                    - {
                        "kind",
                        "learning_id",
                        "exposure_receipt",
                        "exposure_receipt_digest",
                    }
                    or ref["kind"] not in {"profile", "user_playbook", "agent_playbook"}
                    or not isinstance(ref["learning_id"], str)
                    or not ref["learning_id"]
                    or len(ref["learning_id"]) > 1000
                ):
                    raise ValueError(
                        "Stored publish learning link does not match the wire contract"
                    )
            target["retrieved_learnings"] = [
                _canonical_learning_reference(ref) for ref in refs
            ]
        # Some installed SDKs predate ToolUsed.status even when the backend
        # exposes it. Compare raw View status using the pinned backend's coercion;
        # a legacy View that omits status cannot verify that unsupported field.
        for index, (stored_tool, original_tool) in enumerate(
            zip(item.get("tools_used", []), original.get("tools_used", []), strict=True)
        ):
            observed_tool = observed["tools_used"][index]
            expected_tool = normalized["tools_used"][index]
            if "status" in stored_tool:
                for target, source in (
                    (observed_tool, stored_tool),
                    (expected_tool, original_tool),
                ):
                    status = source.get("status")
                    target["status"] = "" if status is None else str(status)[:100]
            else:
                observed_tool.pop("status", None)
                expected_tool.pop("status", None)
        # JSON equality must distinguish booleans from integer tool values.
        if json.dumps(observed, sort_keys=True, separators=(",", ":")) != json.dumps(
            normalized, sort_keys=True, separators=(",", ":")
        ):
            raise _StoredPublishMismatch(
                "Stored publish interaction payload does not match"
            )
    if unverified:
        _LOGGER.warning(
            "Recovered stored publish %s without verifying %s; the read API "
            "does not return them",
            payload["request_id"],
            ", ".join(unverified),
        )
    return _StoredPublish(unverified)


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


def _has_publish_content(item: dict[str, Any]) -> bool:
    """Mirror pinned InteractionData.carries_content before tool expansion.

    Unlike model construction this supports more than 1,000 unsplit tools.
    Whitespace-only placeholders carry no learning data and are retired locally.
    """
    if item.get("user_action", "none") != "none":
        return True
    fields = (
        "content",
        "shadow_content",
        "expert_content",
        "interacted_image_url",
        "image_encoding",
        "tools_used",
        "citations",
        "retrieved_learnings",
    )
    carries_content = any(
        value.strip() if isinstance(value, str) else value
        for value in (item.get(field) for field in fields)
    )
    if not carries_content and set(item) - {
        *fields,
        "role",
        "created_at",
        "user_action",
        "user_action_description",
    }:
        raise ValueError("Empty interaction contains unrecognized fields")
    return bool(carries_content)


def _storage_safe_interactions(
    interactions: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep complete tool activity within the server's per-message limit."""
    result = []
    for original in interactions:
        item = _escape_nulls(original)
        if not _has_publish_content(item):
            continue
        tools = item.get("tools_used", [])
        if len(tools) <= 1000:
            result.append(item)
            continue
        result.append({**item, "tools_used": tools[:1000]})
        result.extend(
            {
                "role": item.get("role", "Assistant"),
                "content": "Tool activity continued.",
                "tools_used": tools[start : start + 1000],
            }
            for start in range(1000, len(tools), 1000)
        )
    return result


def _confirmed_publish(response: Any, request_id: str | None = None) -> PublishResult:
    """HTTP success alone does not confirm that interactions were saved."""
    success = (
        dict.get(response, "success")
        if isinstance(response, dict)
        else getattr(response, "success", None)
    )
    message = (
        dict.get(response, "message")
        if isinstance(response, dict)
        else getattr(response, "message", None)
    )
    if success is not True:
        reason = (
            dict.get(response, "error_reason")
            if isinstance(response, dict)
            else getattr(response, "error_reason", None)
        )
        return PublishResult(
            False,
            request_id,
            error_type="ServerRejected"
            if success is False
            else "InvalidPublishAcknowledgement",
            http_status=200,
            server_message=sanitize_server_message(message),
            error_reason=reason if reason == _REQUEST_ID_CONFLICT else None,
        )
    if message == "Interaction queued for processing":
        # Legacy routes acknowledge a background task before durable storage.
        # Stable raw callers must verify the stored request before retiring it.
        return PublishResult(
            False, request_id, error_type="StorageNotConfirmed", http_status=200
        )
    replayed = (
        dict.get(response, "replayed")
        if isinstance(response, dict)
        else getattr(response, "replayed", None)
    )
    # A replay confirms the original stored publish; its extraction options
    # were not rerun, exactly like a verified lost-acknowledgement recovery.
    return PublishResult(True, request_id, recovered=replayed is True)


_REDACTIONS = (
    # Pydantic echoes the offending value; that can be conversation content.
    (re.compile(r"input_value=.*?(?=, input_type=|$)"), "input_value=[redacted]"),
    (re.compile(r"(?i)\b(bearer|basic)\s+\S+"), r"\1 [redacted]"),
    (re.compile(r"(\w+://)[^\s/@]+@"), r"\1[redacted]@"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization)"
            r"(\s*[=:]\s*)\S+"
        ),
        r"\1\2[redacted]",
    ),
)


def sanitize_server_message(message: Any) -> str | None:
    """Return a short, single-line server reason safe for local diagnostics.

    Echoed values and credential-shaped tokens are redacted before truncation,
    so the bounded text never carries a payload fragment or a secret.
    """
    if not isinstance(message, str) or not message.strip():
        return None
    text = " ".join(
        "".join(char if char.isprintable() else " " for char in message).split()
    )
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    if len(text) > _SERVER_MESSAGE_LIMIT:
        text = text[: _SERVER_MESSAGE_LIMIT - 1] + "…"
    return text


def _http_error_details(response: Any) -> tuple[str | None, str | None]:
    """Return ``(reason, sanitized message)`` from an HTTP failure body.

    FastAPI wraps errors in ``detail``: a string, or for a request-ID conflict
    ``{"reason", "request_id", "message"}``.
    """
    try:
        body = response.json() if response is not None else None
    except Exception:  # noqa: BLE001 — non-JSON bodies carry no structured reason.
        return None, None
    if not isinstance(body, dict):
        return None, None
    detail = body.get("detail")
    if isinstance(detail, dict):
        reason = detail.get("reason")
        return (
            reason if isinstance(reason, str) and len(reason) <= 64 else None,
            sanitize_server_message(detail.get("message")),
        )
    return None, sanitize_server_message(detail or body.get("message"))


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
