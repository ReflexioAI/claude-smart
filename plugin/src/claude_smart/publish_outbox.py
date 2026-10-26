"""Durable automatic publish intents; session JSONL remains the payload owner.

One detached worker owns publication at a time. The queue gate serializes
enqueue/start with the worker's empty-check/release, preventing a lost wakeup.
Only explicit intents are recovered; legacy buffers are never swept implicitly.
"""

from __future__ import annotations

import hashlib
from contextlib import suppress
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from claude_smart import env_config, hook_log, publish, runtime, state
from claude_smart.reflexio_adapter import Adapter

_MAX_BATCHES = 64


def _root() -> Path:
    return state.state_dir() / "publish-outbox"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _environment_digest() -> str:
    try:
        return hashlib.sha256(env_config.CLAUDE_SMART_ENV_PATH.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "absent"


def read_only() -> bool:
    """A fresh file-level stop overrides an inherited writable environment."""
    if env_config.env_truthy(env_config.CLAUDE_SMART_READ_ONLY_ENV):
        return True
    try:
        lines = env_config.CLAUDE_SMART_ENV_PATH.read_text().splitlines()
    except FileNotFoundError:
        return False
    for line in lines:
        entry = env_config.parse_env_line(line)
        if entry and entry[0] == env_config.CLAUDE_SMART_READ_ONLY_ENV:
            return entry[1].strip().lower() in {"1", "true", "yes", "on"}
    return False


def _binding(adapter: Adapter) -> str:
    # Credentials and even URLs (which can contain secrets) never enter the queue.
    return _digest(adapter.url.rstrip("/") + "\0" + _digest(adapter.api_key))


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".intent-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Invalid publish intent")
    return value


def _spawn_worker(token: str) -> None:
    options: dict[str, Any] = (
        {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
        }
        if os.name == "nt"
        else {"start_new_session": True}
    )
    subprocess.Popen(
        [sys.executable, "-m", "claude_smart.publish_outbox", token],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **options,
    )


def _ensure_worker_locked(root: Path, *, skipped: set[str] | None = None) -> None:
    owner = FileLock(root / "worker.lock", timeout=0)
    try:
        owner.acquire()
    except Timeout:
        return
    owner.release()
    claim = root / "launch.claim"
    try:
        if 0 <= time.time() - _read(claim)["created_at"] < 10:
            return
    except (OSError, ValueError, KeyError, TypeError):
        pass
    token = uuid.uuid4().hex
    _atomic_write(
        claim,
        dict(token=token, created_at=time.time(), skipped=sorted(skipped or ())),
    )
    try:
        _spawn_worker(token)
    except OSError:
        if _read(claim)["token"] == token:
            claim.unlink()
        raise


def enqueue(session_id: str, project_id: str) -> bool:
    """Persist a coalesced intent before waking the worker; never acknowledge HTTP."""
    root = _root()
    try:
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        binding = _binding(Adapter())
        path = root / (_digest(session_id) + ".json")
        with FileLock(root / "gate.lock", timeout=0.2):
            environment = _environment_digest()
            if path.exists():
                previous = _read(path)
                if (
                    previous["binding"] != binding
                    or previous["project_id"] != project_id
                    or previous["environment"] != environment
                ):
                    return False  # Held destination; never silently redirect pending turns.
            records = state.read_all(session_id)
            through_record = max(
                (
                    index + 1
                    for index, record in enumerate(records)
                    if record.get("role") == "Assistant"
                ),
                default=0,
            )
            _atomic_write(
                path,
                dict(
                    session_id=session_id,
                    project_id=project_id,
                    binding=binding,
                    environment=environment,
                    generation=uuid.uuid4().hex,
                    through_record=through_record,
                ),
            )
            try:
                _ensure_worker_locked(root)
            except OSError:
                pass  # Intent is durable; a later hook recovers failed spawning.
        return True
    except (OSError, ValueError, KeyError, Timeout):
        return False


def kick() -> None:
    """Recover existing explicit intents on startup, without scanning transcripts."""
    root = _root()
    if not root.exists():
        return
    try:
        if read_only():
            return
        with FileLock(root / "gate.lock", timeout=0.2):
            if next(root.glob("*.json"), None) is not None:
                _ensure_worker_locked(root)
    except (OSError, Timeout):
        return


def _pending(session_id: str, through_record: int) -> bool:
    records = state.read_all(session_id)
    published = state.published_record_offset(records)
    if published >= through_record:
        return False
    end = min(state.safe_publish_end(records, published), through_record)
    return bool(state.unpublished_slice(records[:end], published_override=published)[1])


def _select(
    root: Path, binding: str, skipped: set[str]
) -> tuple[Path, dict[str, Any]] | None:
    for path in sorted(root.glob("*.json")):
        try:
            job = _read(path)
            token = str(job["generation"])
            if (
                job["binding"] == binding
                and job["environment"] == _environment_digest()
                and token not in skipped
                and isinstance(job["session_id"], str)
                and isinstance(job["project_id"], str)
                and type(job["through_record"]) is int
                and job["through_record"] >= 0
            ):
                return path, job
        except (OSError, ValueError, KeyError):
            continue  # Malformed/held intents remain inspectable, never discarded.
    return None


def run_worker(launch_token: str | None = None) -> None:
    """Drain a bounded round; keep owner until work and exit handshake finish."""
    root = _root()
    if not root.exists():
        return
    owner = FileLock(root / "worker.lock", timeout=0)
    acquired = False
    skipped: set[str] = set()
    try:
        with FileLock(root / "gate.lock", timeout=5):
            claim = root / "launch.claim"
            if launch_token is not None:
                if not claim.exists():
                    return
                admission = _read(claim)
                if admission["token"] != launch_token:
                    return  # Superseded launch must not steal its successor's owner.
                # Continuations resume the same recovery round. A new explicit
                # wake creates a claim without exclusions and may retry failures.
                skipped.update(admission.get("skipped", []))
            elif claim.exists():
                return  # Explicit local drains also respect an admitted child.
            owner.acquire()
            acquired = True
            if launch_token is not None:
                claim.unlink()
        adapter = Adapter()
        binding = _binding(adapter)
        for _ in range(_MAX_BATCHES):
            with FileLock(root / "gate.lock", timeout=5):
                selected = None if read_only() else _select(root, binding, skipped)
                if selected is None:
                    owner.release()
                    acquired = False
                    return
                path, job = selected
            # Recheck after releasing queue gate, immediately before dispatch.
            if read_only() or job["environment"] != _environment_digest():
                skipped.add(job["generation"])
                continue
            started = time.monotonic()
            status: publish.PublishStatus = "failed"
            count = 0
            try:
                status, count = publish.publish_unpublished(
                    session_id=job["session_id"],
                    project_id=job["project_id"],
                    force_extraction=False,
                    skip_aggregation=False,
                    adapter=adapter,
                    max_batches=1,
                    through_record=job["through_record"],
                )
            finally:
                with suppress(Exception):
                    hook_log.log_event(
                        event="publish-worker",
                        host=runtime.HOST_CODEX,
                        session_id=job["session_id"],
                        project_id=job["project_id"],
                        publish_status=status,
                        publish_count=count,
                        extra=dict(
                            stage="publication",
                            duration_ms=round((time.monotonic() - started) * 1000, 3),
                        ),
                    )
            with FileLock(root / "gate.lock", timeout=5):
                current = _read(path)
                if current["generation"] != job["generation"]:
                    continue
                if status == "failed":
                    skipped.add(job["generation"])
                elif not _pending(job["session_id"], job["through_record"]):
                    path.unlink()
                elif status == "nothing":
                    skipped.add(job["generation"])
        # Healthy queues larger than one round get a continuation without a
        # future user prompt. Failed or mismatched jobs await the next hook.
        with FileLock(root / "gate.lock", timeout=5):
            again = not read_only() and _select(root, binding, skipped) is not None
            owner.release()
            acquired = False
            if again:
                _ensure_worker_locked(root, skipped=skipped)
    except (OSError, ValueError, KeyError, Timeout):
        return
    finally:
        if acquired:
            owner.release()


if __name__ == "__main__":
    runtime.set_host(runtime.HOST_CODEX)
    run_worker(sys.argv[1] if len(sys.argv) > 1 else None)
