#!/usr/bin/env python3
"""Tier A claude-smart enabled/disabled replay evaluation.

Artifacts are intentionally outside any repo:
~/.claude-smart/evals/tier_a_replay/<run_id>/
"""

from __future__ import annotations

import hashlib
import argparse
import json
import math
import os
import random
import re
import shutil
import sqlite3
import subprocess
import textwrap
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


HOME = Path.home()
STATE_ROOT = HOME / ".claude-smart"
SESSIONS_DIR = STATE_ROOT / "sessions"
DB_PATH = HOME / ".reflexio" / "data" / "reflexio.db"
EVAL_ROOT = STATE_ROOT / "evals" / "tier_a_replay"
PLUGIN = "claude-smart@reflexioai"
REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = Path(os.environ.get("CLAUDE_SMART_PLUGIN_ROOT", str(REPO_ROOT / "plugin"))).expanduser()
VIEWER_TEMPLATE = Path(__file__).with_name("viewer_template.html")
READ_ONLY_TOOLS = "Read,Glob,Grep,LS"
DEFAULT_USER_ID = os.environ.get("CLAUDE_SMART_EVAL_USER_ID") or os.environ.get("REFLEXIO_USER_ID") or "default"


def default_read_only_dirs() -> list[str]:
    return [str(REPO_ROOT)]


RULE_URL_RE = re.compile(r"http://localhost:3001/rules/([A-Za-z0-9_-]+)")
TRUE_MARKER_LINE_RE = re.compile(
    r"(?m)^✨\s+(?:"
    r"\d+\s+claude-smart learning(?:s)? applied\s+\[cs:[^\]]+\]"
    r"|(?:Applied|claude-smart rules? applied|claude-smart rule applied):\s+.+"
    r")\s*$",
    re.I,
)
MIN_PREFLIGHT_OVERLAP = 0.5
SMOKE_CASE_COUNT = 3


@dataclass
class Config:
    case_limit: int = 3
    min_overlap: float = 0.5
    samples_per_arm: int = 1
    judge_passes: int = 1
    dedupe: bool = True
    label: str = "smoke"
    availability_probe: bool = True
    read_only_tools: bool = True
    read_only_dirs: list[str] = field(default_factory=default_read_only_dirs)
    replay_model: str = ""
    judge_model: str = ""


@dataclass
class Case:
    case_id: str
    source_type: str
    session_id: str
    user_id: str
    target_user: str
    prefix: str
    expected_ids: list[str]
    cited_records: list[dict[str, Any]] = field(default_factory=list)


def sh(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def git_status(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    proc = sh(["git", "status", "--short"], cwd=path, timeout=10)
    return proc.stdout if proc.returncode == 0 else ""


def git_root_for(path: Path) -> Path | None:
    if not path.exists():
        return None
    proc = sh(["git", "rev-parse", "--show-toplevel"], cwd=path, timeout=10)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return Path(proc.stdout.strip()).resolve()


def parent_git_root(path: Path) -> Path | None:
    inner = git_root_for(path)
    for parent in path.resolve().parents:
        root = git_root_for(parent)
        if root is not None and root != inner:
            return root
    return None


def repo_status_roots() -> tuple[Path, Path | None]:
    repo_root = Path(os.environ.get("TIER_A_REPLAY_REPO_STATUS_ROOT", str(REPO_ROOT))).expanduser()
    outer_env = os.environ.get("TIER_A_REPLAY_OUTER_REPO_STATUS_ROOT")
    outer_root = Path(outer_env).expanduser() if outer_env else parent_git_root(repo_root)
    return repo_root, outer_root


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def injected_registry(session_id: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for rec in read_jsonl(SESSIONS_DIR / f"{session_id}.injected.jsonl"):
        item_id = rec.get("id")
        if isinstance(item_id, str):
            out[item_id] = rec
    return out


def trim(text: str, limit: int = 6000) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def render_prefix(turns: list[dict[str, Any]], before_index: int, max_turns: int = 8) -> str:
    relevant = [r for r in turns[:before_index] if r.get("role") in {"User", "Assistant"}]
    relevant = relevant[-max_turns:]
    lines: list[str] = []
    for rec in relevant:
        role = rec.get("role", "Unknown")
        content = str(rec.get("content", "")).strip()
        if content:
            lines.append(f"{role}: {content[:1500]}")
    return "\n\n".join(lines)


def previous_user(turns: list[dict[str, Any]], before_index: int) -> dict[str, Any] | None:
    for rec in reversed(turns[:before_index]):
        if rec.get("role") in {"User", "user"} and str(rec.get("content", "")).strip():
            return rec
    return None


def ids_from_marker(content: str) -> list[str]:
    if not TRUE_MARKER_LINE_RE.search(content):
        return []
    return sorted(set(RULE_URL_RE.findall(content)))


def cases_from_sessions() -> list[Case]:
    cases: list[Case] = []
    seen: set[tuple[str, str, str]] = set()
    for path in sorted(SESSIONS_DIR.glob("*.jsonl")):
        if path.name.endswith(".injected.jsonl"):
            continue
        session_id = path.stem
        rows = read_jsonl(path)
        registry = injected_registry(session_id)
        for idx, rec in enumerate(rows):
            if rec.get("role") != "Assistant":
                continue
            user = previous_user(rows, idx)
            if not user:
                continue
            content = str(rec.get("content", ""))
            cited_items = rec.get("cited_items")
            expected: list[str] = []
            cited_records: list[dict[str, Any]] = []
            source = ""
            if isinstance(cited_items, list) and cited_items:
                for item in cited_items:
                    if isinstance(item, dict) and isinstance(item.get("id"), str):
                        expected.append(item["id"])
                        cited_records.append(item)
                source = "cited_items"
            if not expected:
                expected = ids_from_marker(content)
                cited_records = [registry[i] for i in expected if i in registry]
                if expected:
                    source = "marker"
            expected = sorted(set(expected))
            if not expected:
                continue
            key = (session_id, str(user.get("content")), ",".join(expected))
            if key in seen:
                continue
            seen.add(key)
            case_id = f"{source}-{session_id[:8]}-{idx}"
            cases.append(
                Case(
                    case_id=case_id,
                    source_type=source,
                    session_id=session_id,
                    user_id=str(user.get("user_id") or rec.get("user_id") or DEFAULT_USER_ID),
                    target_user=str(user.get("content", "")).strip(),
                    prefix=render_prefix(rows, idx),
                    expected_ids=expected,
                    cited_records=cited_records,
                )
            )
    return cases


def cases_from_db(limit: int = 3) -> list[Case]:
    cases: list[Case] = []
    if not DB_PATH.exists():
        return cases
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT i.interaction_id, i.request_id, i.role, i.content, i.citations,
               r.session_id, r.user_id
        FROM interactions i
        JOIN requests r ON i.request_id = r.request_id
        WHERE coalesce(i.citations, '') NOT IN ('', '[]', 'null')
        ORDER BY i.interaction_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in rows:
        try:
            citations = json.loads(row["citations"])
        except Exception:
            citations = []
        expected = []
        records = []
        for item in citations if isinstance(citations, list) else []:
            if isinstance(item, dict) and isinstance(item.get("tag"), str):
                expected.append(item["tag"])
                records.append(item)
        if not expected:
            continue
        prior = conn.execute(
            """
            SELECT content FROM interactions
            WHERE request_id = ? AND lower(role) = 'user'
            ORDER BY interaction_id DESC
            LIMIT 1
            """,
            (row["request_id"],),
        ).fetchone()
        if prior is None:
            prior = conn.execute(
                """
                SELECT i.content FROM interactions i
                JOIN requests r ON i.request_id = r.request_id
                WHERE r.session_id = ? AND lower(i.role) = 'user'
                  AND i.interaction_id < ?
                ORDER BY i.interaction_id DESC
                LIMIT 1
                """,
                (row["session_id"], row["interaction_id"]),
            ).fetchone()
        if prior is None:
            continue
        cases.append(
            Case(
                case_id=f"db-{row['session_id'][:8]}-{row['interaction_id']}",
                source_type="db",
                session_id=str(row["session_id"]),
                user_id=str(row["user_id"] or DEFAULT_USER_ID),
                target_user=str(prior["content"]).strip(),
                prefix="",
                expected_ids=sorted(set(expected)),
                cited_records=records,
            )
        )
    conn.close()
    return cases


def select_smoke_cases() -> list[Case]:
    session_cases = cases_from_sessions()
    db_cases = cases_from_db()
    session_cases = [c for c in session_cases if not c.target_user.lstrip().startswith("/")]
    db_cases = [c for c in db_cases if not c.target_user.lstrip().startswith("/")]
    selected: list[Case] = []
    for wanted, pool in (
        ("cited_items", session_cases),
        ("db", db_cases),
        ("marker", session_cases),
    ):
        for case in pool:
            if case.source_type == wanted and case.target_user:
                selected.append(case)
                break
    if len(selected) < 3:
        for case in session_cases + db_cases:
            if case.case_id not in {c.case_id for c in selected}:
                selected.append(case)
            if len(selected) >= 3:
                break
    return selected[:3]


def all_replay_candidates() -> list[Case]:
    cases = cases_from_sessions() + cases_from_db(1000)
    out: list[Case] = []
    seen_ids: set[str] = set()
    seen_replay_keys: set[tuple[str, str, str, tuple[str, ...]]] = set()
    seen_turn_keys: set[tuple[str, str, tuple[str, ...]]] = set()
    for case in cases:
        if not case.target_user or case.target_user.lstrip().startswith("/"):
            continue
        expected = expected_real_ids(case)
        if not expected:
            continue
        if case.case_id in seen_ids:
            continue
        turn_key = (case.session_id, case.target_user, tuple(expected))
        if turn_key in seen_turn_keys:
            continue
        replay_key = (case.user_id, case.target_user, case.prefix, tuple(expected))
        if replay_key in seen_replay_keys:
            continue
        seen_ids.add(case.case_id)
        seen_replay_keys.add(replay_key)
        seen_turn_keys.add(turn_key)
        out.append(case)
    return out


def preflight_and_select_cases(run_dir: Path, config: Config) -> tuple[list[Case], list[dict[str, Any]]]:
    preflight_rows: list[dict[str, Any]] = []
    for case in all_replay_candidates():
        preflight = run_hook_preflight(case, run_dir)
        preflight_rows.append({"case": asdict(case), "preflight": preflight})
        (run_dir / "candidate_preflights.json").write_text(json.dumps(preflight_rows, indent=2), encoding="utf-8")

    eligible = [
        row for row in preflight_rows
        if row["preflight"].get("returncode") == 0
        and row["preflight"].get("has_context")
        and row["preflight"].get("overlap_fraction", 0) >= config.min_overlap
    ]
    eligible.sort(
        key=lambda row: (
            row["preflight"].get("overlap_fraction", 0),
            len(row["preflight"].get("overlap_real_ids", [])),
        ),
        reverse=True,
    )
    selected_ids = {row["case"]["case_id"] for row in eligible[:config.case_limit]}
    selected = [case for case in all_replay_candidates() if case.case_id in selected_ids]
    selected.sort(
        key=lambda case: next(
            (
                row["preflight"].get("overlap_fraction", 0),
                len(row["preflight"].get("overlap_real_ids", [])),
            )
            for row in eligible
            if row["case"]["case_id"] == case.case_id
        ),
        reverse=True,
    )
    return selected, preflight_rows


def db_counts() -> dict[str, Any]:
    out: dict[str, Any] = {"exists": DB_PATH.exists()}
    if not DB_PATH.exists():
        return out
    out["size"] = DB_PATH.stat().st_size
    # Hash the main DB file only. WAL may legitimately move while the service is running.
    h = hashlib.sha256()
    with DB_PATH.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    out["sha256"] = h.hexdigest()
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    for table in ("requests", "interactions", "profiles", "user_playbooks", "agent_playbooks"):
        try:
            out[f"{table}_count"] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except Exception as exc:
            out[f"{table}_count_error"] = str(exc)
    conn.close()
    return out


def session_counts() -> dict[str, Any]:
    files = list(SESSIONS_DIR.glob("*.jsonl")) if SESSIONS_DIR.exists() else []
    return {
        "exists": SESSIONS_DIR.exists(),
        "jsonl_count": len(files),
        "plain_count": len([p for p in files if not p.name.endswith(".injected.jsonl")]),
        "injected_count": len([p for p in files if p.name.endswith(".injected.jsonl")]),
    }


def log_offsets() -> dict[str, int]:
    out: dict[str, int] = {}
    for name in ("hook.log", "backend.log", "install.log", "dashboard.log"):
        path = STATE_ROOT / name
        if path.exists():
            out[str(path)] = path.stat().st_size
    return out


def log_tails(offsets: dict[str, int], run_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path_text, offset in offsets.items():
        path = Path(path_text)
        if not path.exists():
            continue
        with path.open("rb") as fh:
            fh.seek(min(offset, path.stat().st_size))
            data = fh.read(200_000)
        text = data.decode("utf-8", errors="replace")
        name = path.name.replace(".", "_") + "_delta.txt"
        (run_dir / name).write_text(text, encoding="utf-8")
        out[path_text] = name
    return out


def runtime_identity() -> dict[str, Any]:
    out: dict[str, Any] = {}
    plugin_root_txt = HOME / ".reflexio" / "plugin-root.txt"
    out["plugin_root_txt"] = plugin_root_txt.read_text().strip() if plugin_root_txt.exists() else ""
    for label, path in {
        "npm_plugin": HOME / ".nvm/versions/node/v24.16.0/lib/node_modules/claude-smart/plugin/.claude-plugin/plugin.json",
        "codex_plugin_root_txt_plugin": Path(out["plugin_root_txt"]) / ".claude-plugin" / "plugin.json" if out["plugin_root_txt"] else Path("/missing"),
    }.items():
        try:
            out[label] = json.loads(path.read_text())
            out[label]["path"] = str(path)
        except Exception as exc:
            out[label] = {"error": str(exc), "path": str(path)}
    pid_path = STATE_ROOT / "backend.pid"
    if pid_path.exists():
        pid = pid_path.read_text().strip()
        out["backend_pid"] = pid
        ps = sh(["ps", "-p", pid, "-o", "pid,lstart,command"], timeout=10)
        out["backend_ps"] = ps.stdout.strip()
        pse = sh(["ps", "eww", "-p", pid], timeout=10)
        out["backend_env_paths"] = "\n".join(
            line for line in pse.stdout.replace(" ", "\n").splitlines()
            if any(k in line for k in ("PYTHONPATH", "VIRTUAL_ENV", "REFLEXIO", "CLAUDE", "plugin"))
        )
    health = sh(["curl", "-sS", "-A", "Mozilla/5.0 smoke-eval", "http://localhost:8071/health"], timeout=10)
    out["health_stdout"] = health.stdout.strip()
    out["health_returncode"] = health.returncode
    lsof = sh(["lsof", "-nP", "-iTCP:8071", "-sTCP:LISTEN"], timeout=10)
    out["lsof_8071"] = lsof.stdout.strip()
    return out


def build_context(case: Case, *, memory_context: str = "") -> str:
    memory_block = ""
    if memory_context.strip():
        memory_block = textwrap.dedent(
            f"""

            Context retrieved from claude-smart for this turn:
            {memory_context.strip()}
            """
        )
    return textwrap.dedent(
        f"""
        You are replaying a prior coding-assistant turn for an evaluation.
        Prior transcript context follows. Use it as background only; do not continue
        the old assistant answer, and do not reveal that this is an evaluation.

        Hard constraints:
        - You may read local files when needed to answer accurately, including absolute paths referenced by the user or transcript.
        - Do not modify files anywhere, including inside or outside the current temporary directory.
        - Do not create commits, PRs, pushes, deployments, releases, package publishes, or external side effects.
        - Use only read-only inspection tools; do not use shell commands or write-capable tools.
        - Be concise but complete.

        Prior transcript:
        {case.prefix or "(none)"}
        {memory_block}
        """
    ).strip()


def parse_stream_text(raw: str) -> str:
    final_results: list[str] = []
    message_parts: list[str] = []
    for line in raw.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Claude stream-json shapes vary; collect likely text/result fields.
        if isinstance(obj, dict):
            if obj.get("type") == "result" and isinstance(obj.get("result"), str):
                final_results.append(obj["result"])
            msg = obj.get("message")
            if isinstance(msg, dict):
                for c in msg.get("content", []) if isinstance(msg.get("content"), list) else []:
                    if isinstance(c, dict) and isinstance(c.get("text"), str):
                        message_parts.append(c["text"])
            delta = obj.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("text"), str):
                message_parts.append(delta["text"])
    if final_results:
        return final_results[-1].strip()
    if message_parts:
        return "".join(message_parts).strip()
    return raw.strip()


def has_true_marker(text: str) -> bool:
    """Detect a real standalone claude-smart marker, not examples quoted in a review."""
    return bool(TRUE_MARKER_LINE_RE.search(text))


def injected_ids(state_dir: Path) -> list[str]:
    ids: set[str] = set()
    for path in state_dir.glob("*.injected.jsonl"):
        for rec in read_jsonl(path):
            if isinstance(rec.get("id"), str):
                ids.add(rec["id"])
    return sorted(ids)


def injected_records(state_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in state_dir.glob("*.injected.jsonl"):
        records.extend(read_jsonl(path))
    return records


def expected_real_ids(case: Case) -> list[str]:
    ids: set[str] = set()
    for rec in case.cited_records:
        real_id = rec.get("real_id")
        if isinstance(real_id, str) and real_id:
            ids.add(real_id)
    return sorted(ids)


def real_ids(records: list[dict[str, Any]]) -> list[str]:
    ids: set[str] = set()
    for rec in records:
        real_id = rec.get("real_id")
        if isinstance(real_id, str) and real_id:
            ids.add(real_id)
    return sorted(ids)


def parse_hook_context(stdout: str) -> str:
    for line in stdout.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        hook_output = obj.get("hookSpecificOutput") if isinstance(obj, dict) else None
        if isinstance(hook_output, dict) and isinstance(hook_output.get("additionalContext"), str):
            return hook_output["additionalContext"]
    return ""


def run_hook_preflight(case: Case, run_dir: Path) -> dict[str, Any]:
    preflight_dir = run_dir / case.case_id / "preflight"
    state_dir = preflight_dir / "state"
    preflight_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    session_id = str(uuid.uuid4())
    env = dict(os.environ)
    env["CLAUDE_SMART_READ_ONLY"] = "1"
    env["CLAUDE_SMART_STATE_DIR"] = str(state_dir)
    env["REFLEXIO_USER_ID"] = case.user_id
    env["CLAUDE_SMART_CITATIONS"] = "on"
    payload = {
        "session_id": session_id,
        "prompt": case.target_user,
        "cwd": str(preflight_dir),
    }
    proc = subprocess.run(
        ["bash", str(PLUGIN_ROOT / "scripts/hook_entry.sh"), "claude-code", "user-prompt"],
        cwd=preflight_dir,
        env=env,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    (preflight_dir / "payload.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (preflight_dir / "stdout.json").write_text(proc.stdout, encoding="utf-8")
    (preflight_dir / "stderr.txt").write_text(proc.stderr, encoding="utf-8")
    context = parse_hook_context(proc.stdout)
    (preflight_dir / "additional_context.md").write_text(context, encoding="utf-8")
    records = injected_records(state_dir)
    (preflight_dir / "injected_records.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    expected = expected_real_ids(case)
    retrieved = real_ids(records)
    overlap = sorted(set(expected) & set(retrieved))
    hook_log = state_dir / "hook.log"
    return {
        "returncode": proc.returncode,
        "stdout_path": str(preflight_dir / "stdout.json"),
        "stderr_path": str(preflight_dir / "stderr.txt"),
        "context_path": str(preflight_dir / "additional_context.md"),
        "hook_log_path": str(hook_log) if hook_log.is_file() else None,
        "retrieved_learning_context": context,
        "injected_records_path": str(preflight_dir / "injected_records.json"),
        "retrieved_learning_records": records,
        "state_dir": str(state_dir),
        "session_id": session_id,
        "expected_real_ids": expected,
        "retrieved_real_ids": retrieved,
        "overlap_real_ids": overlap,
        "overlap_fraction": round(len(overlap) / len(expected), 3) if expected else 0.0,
        "has_context": bool(context.strip()),
    }


def stream_init_metadata(raw: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {"plugins": [], "plugin_sources": [], "tools": []}
    for line in raw.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "system" and obj.get("subtype") == "init":
            plugins = obj.get("plugins") if isinstance(obj.get("plugins"), list) else []
            tools = obj.get("tools") if isinstance(obj.get("tools"), list) else []
            metadata["plugins"] = plugins
            metadata["plugin_sources"] = sorted(
                p.get("source", "") for p in plugins if isinstance(p, dict) and isinstance(p.get("source"), str)
            )
            metadata["tools"] = sorted(t for t in tools if isinstance(t, str))
            break
    return metadata


def stream_result_metadata(raw: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for line in raw.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            out = {
                "is_error": obj.get("is_error"),
                "api_error_status": obj.get("api_error_status"),
                "terminal_reason": obj.get("terminal_reason"),
                "result_excerpt": str(obj.get("result", ""))[:500],
            }
    return out


def claude_availability_probe(run_dir: Path, *, model: str = "", name: str = "default") -> dict[str, Any]:
    probe_dir = run_dir / f"_availability_probe_{name}"
    probe_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "claude",
        "-p",
        "--no-session-persistence",
        *(["--model", model] if model else []),
        "--output-format",
        "json",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--tools",
        "",
        "--settings",
        json.dumps({"enabledPlugins": {PLUGIN: False}}),
        "--",
        "Say OK only.",
    ]
    proc = sh(cmd, cwd=probe_dir, timeout=60)
    (probe_dir / "cmd.json").write_text(json.dumps(cmd, indent=2), encoding="utf-8")
    (probe_dir / "stdout.json").write_text(proc.stdout, encoding="utf-8")
    (probe_dir / "stderr.txt").write_text(proc.stderr, encoding="utf-8")
    result: dict[str, Any] = {"returncode": proc.returncode, "stdout_path": str(probe_dir / "stdout.json"), "stderr_path": str(probe_dir / "stderr.txt")}
    try:
        payload = json.loads(proc.stdout)
        if isinstance(payload, dict):
            result.update(
                {
                    "is_error": payload.get("is_error"),
                    "api_error_status": payload.get("api_error_status"),
                    "result_excerpt": str(payload.get("result", ""))[:500],
                    "terminal_reason": payload.get("terminal_reason"),
                    "modelUsage": payload.get("modelUsage"),
                }
            )
    except json.JSONDecodeError:
        result["parse_error"] = "stdout was not JSON"
        result["raw_stdout_excerpt"] = proc.stdout[:500]
    result["available"] = proc.returncode == 0 and result.get("is_error") is not True
    return result


def run_arm(case: Case, run_dir: Path, arm: str, disable_plugin: bool) -> dict[str, Any]:
    arm_dir = run_dir / case.case_id / arm
    arm_dir.mkdir(parents=True, exist_ok=True)
    state_dir = arm_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["PWD"] = str(arm_dir)
    settings: dict[str, Any] = {"enabledPlugins": {PLUGIN: False}}
    memory_context = ""
    if not disable_plugin:
        preflight_context = run_dir / case.case_id / "preflight" / "additional_context.md"
        memory_context = preflight_context.read_text(encoding="utf-8") if preflight_context.exists() else ""

    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    replay_tools = READ_ONLY_TOOLS if config.get("read_only_tools", True) else ""
    read_only_dirs = config.get("read_only_dirs") or []
    replay_model = str(config.get("replay_model") or "")
    cmd = [
        "claude",
        "-p",
        "--verbose",
        "--no-session-persistence",
        *(["--model", replay_model] if replay_model else []),
        "--output-format",
        "stream-json",
        "--include-hook-events",
        *(["--add-dir", *read_only_dirs] if replay_tools else []),
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--tools",
        replay_tools,
        "--permission-mode",
        "dontAsk",
        "--append-system-prompt",
        build_context(case, memory_context=memory_context),
        "--settings",
        json.dumps(settings),
    ]
    cmd.append(case.target_user)
    started = time.time()
    try:
        proc = sh(cmd, cwd=arm_dir, env=env, timeout=240)
    except subprocess.TimeoutExpired as exc:
        return {"arm": arm, "timeout": True, "error": str(exc), "cmd": cmd, "model": replay_model}
    duration = time.time() - started
    (arm_dir / "stdout.jsonl").write_text(proc.stdout, encoding="utf-8")
    (arm_dir / "stderr.txt").write_text(proc.stderr, encoding="utf-8")
    (arm_dir / "cmd.json").write_text(json.dumps(cmd, indent=2), encoding="utf-8")
    text = parse_stream_text(proc.stdout)
    (arm_dir / "assistant.txt").write_text(text, encoding="utf-8")
    ids = injected_ids(state_dir)
    init = stream_init_metadata(proc.stdout)
    result_meta = stream_result_metadata(proc.stdout)
    return {
        "arm": arm,
        "returncode": proc.returncode,
        "duration_s": round(duration, 2),
        "state_dir": str(state_dir),
        "stdout_path": str(arm_dir / "stdout.jsonl"),
        "stderr_path": str(arm_dir / "stderr.txt"),
        "assistant_path": str(arm_dir / "assistant.txt"),
        "assistant_excerpt": text[:1000],
        "init": init,
        "result": result_meta,
        "injected_ids": ids,
        "memory_context_supplied": bool(memory_context.strip()),
        "has_claude_smart_context": bool(memory_context.strip()),
        "has_marker": has_true_marker(text),
        "expected_intersection": sorted(set(case.expected_ids) & set(ids)),
        "model": replay_model,
    }


def run_structured_claude(
    *,
    prompt: str,
    schema: dict[str, Any],
    cwd: Path,
    model: str = "",
    timeout: int = 180,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    proc = sh(
        [
            "claude",
            "-p",
            "--safe-mode",
            "--no-session-persistence",
            *(["--model", model] if model else []),
            "--output-format",
            "json",
            "--tools",
            "",
            "--json-schema",
            json.dumps(schema),
            prompt,
        ],
        cwd=cwd,
        timeout=timeout,
    )
    result: dict[str, Any] = {"returncode": proc.returncode, "model": model}
    try:
        envelope = json.loads(proc.stdout)
        if isinstance(envelope, dict):
            result["modelUsage"] = envelope.get("modelUsage")
        payload = envelope.get("structured_output") if isinstance(envelope, dict) else None
        if isinstance(payload, dict):
            result.update(payload)
        else:
            result["parse_error"] = "missing structured_output"
            result["raw"] = proc.stdout[:1000]
    except Exception as exc:
        result["parse_error"] = str(exc)
        result["raw"] = proc.stdout[:1000]
    return proc, result


def run_response_judge(
    case: Case,
    enabled: dict[str, Any],
    disabled: dict[str, Any],
    run_dir: Path,
    *,
    comparison_id: str,
) -> dict[str, Any]:
    enabled_text = Path(enabled["assistant_path"]).read_text(encoding="utf-8") if enabled.get("assistant_path") else ""
    disabled_text = Path(disabled["assistant_path"]).read_text(encoding="utf-8") if disabled.get("assistant_path") else ""
    a_first = random.choice([True, False])
    a_text, b_text = (enabled_text, disabled_text) if a_first else (disabled_text, enabled_text)
    schema = {
        "type": "object",
        "properties": {
            "winner": {"type": "string", "enum": ["A", "B", "tie", "inconclusive"]},
            "material_difference": {"type": "boolean"},
            "rationale": {"type": "string"},
        },
        "required": ["winner", "material_difference", "rationale"],
        "additionalProperties": False,
    }
    prompt = textwrap.dedent(
        f"""
        You are a blind response-quality judge. Compare two fresh assistant responses to the same user request.
        Decide which response is better for the user's likely goal.
        Judge only the visible response quality: correctness, usefulness, honesty, specificity, and fit to the request.
        You are not being given any retrieved learning or treatment labels. Do not speculate about memory, claude-smart, or why either response differs.

        User request:
        {case.target_user}

        Response A:
        {a_text[:8000]}

        Response B:
        {b_text[:8000]}
        """
    ).strip()
    judge_dir = run_dir / case.case_id / comparison_id / "response_judge"
    judge_dir.mkdir(parents=True, exist_ok=True)
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    proc, result = run_structured_claude(
        prompt=prompt,
        schema=schema,
        cwd=judge_dir,
        model=str(config.get("judge_model") or ""),
    )
    (judge_dir / "stdout.json").write_text(proc.stdout, encoding="utf-8")
    (judge_dir / "stderr.txt").write_text(proc.stderr, encoding="utf-8")
    result["judge_type"] = "blind_response_quality"
    result["enabled_was_A"] = a_first
    winner = result.get("winner")
    if winner in {"A", "B"}:
        result["winner_arm"] = "enabled" if (winner == "A") == a_first else "disabled"
    else:
        result["winner_arm"] = winner
    return result


def run_attribution_judge(
    case: Case,
    enabled: dict[str, Any],
    disabled: dict[str, Any],
    response_judge: dict[str, Any],
    run_dir: Path,
    *,
    comparison_id: str,
) -> dict[str, Any]:
    enabled_text = Path(enabled["assistant_path"]).read_text(encoding="utf-8") if enabled.get("assistant_path") else ""
    disabled_text = Path(disabled["assistant_path"]).read_text(encoding="utf-8") if disabled.get("assistant_path") else ""
    context_path = run_dir / case.case_id / "preflight" / "additional_context.md"
    retrieved_context = context_path.read_text(encoding="utf-8") if context_path.exists() else ""
    schema = {
        "type": "object",
        "properties": {
            "learning_relevant": {"type": "boolean"},
            "learning_helped_enabled": {"type": "boolean"},
            "learning_hurt_enabled": {"type": "boolean"},
            "attribution": {"type": "string", "enum": ["none", "weak", "moderate", "strong", "negative", "inconclusive"]},
            "rationale": {"type": "string"},
        },
        "required": ["learning_relevant", "learning_helped_enabled", "learning_hurt_enabled", "attribution", "rationale"],
        "additionalProperties": False,
    }
    prompt = textwrap.dedent(
        f"""
        You are an attribution judge for a memory ablation.
        The blind response-quality judge has already compared outputs without seeing retrieved learning.
        Your job is only to decide whether the enabled response's quality difference can reasonably be attributed to the retrieved learning.

        Definitions:
        - learning_helped_enabled=true only if the retrieved learning plausibly caused a better enabled response.
        - learning_hurt_enabled=true only if the retrieved learning plausibly caused a worse enabled response.
        - attribution should be strong/moderate/weak only when the response difference aligns with specific retrieved learning content.
        - Use none when the difference is unrelated to learning, even if enabled won.
        - Use inconclusive when the outputs differ but causality is not clear.

        Original user request:
        {case.target_user}

        Retrieved claude-smart learning supplied only to the enabled arm:
        {retrieved_context[:8000]}

        Blind response-quality verdict:
        {json.dumps(response_judge, indent=2)}

        Enabled response:
        {enabled_text[:8000]}

        Disabled response:
        {disabled_text[:8000]}
        """
    ).strip()
    judge_dir = run_dir / case.case_id / comparison_id / "attribution_judge"
    judge_dir.mkdir(parents=True, exist_ok=True)
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    proc, result = run_structured_claude(
        prompt=prompt,
        schema=schema,
        cwd=judge_dir,
        model=str(config.get("judge_model") or ""),
    )
    (judge_dir / "stdout.json").write_text(proc.stdout, encoding="utf-8")
    (judge_dir / "stderr.txt").write_text(proc.stderr, encoding="utf-8")
    result["judge_type"] = "learning_attribution"
    return result


def parse_args() -> Config:
    parser = argparse.ArgumentParser(description="Tier A claude-smart replay ablation")
    parser.add_argument("--case-limit", type=int, default=3, help="Maximum eligible cases to replay")
    parser.add_argument("--min-overlap", type=float, default=0.5, help="Minimum current/historical real-id overlap")
    parser.add_argument("--samples-per-arm", type=int, default=1, help="Generation samples per arm per case")
    parser.add_argument("--judge-passes", type=int, default=1, help="Judge passes per generated pair")
    parser.add_argument("--label", default="smoke", help="Run label stored in metadata")
    parser.add_argument("--no-availability-probe", action="store_true", help="Skip the initial Claude availability/rate-limit probe")
    parser.add_argument("--no-read-tools", action="store_true", help="Disable read-only replay tools and run generation arms with no tools")
    parser.add_argument(
        "--read-dir",
        action="append",
        default=None,
        help="Directory to expose read-only to replay arms via claude --add-dir; repeat for multiple directories",
    )
    parser.add_argument("--replay-model", default="", help="Claude model alias/name for enabled/disabled replay arms")
    parser.add_argument("--judge-model", default="", help="Claude model alias/name for response and attribution judges")
    args = parser.parse_args()
    if args.case_limit < 1:
        raise SystemExit("--case-limit must be >= 1")
    if not 0 <= args.min_overlap <= 1:
        raise SystemExit("--min-overlap must be between 0 and 1")
    if args.samples_per_arm < 1:
        raise SystemExit("--samples-per-arm must be >= 1")
    if args.judge_passes < 1:
        raise SystemExit("--judge-passes must be >= 1")
    return Config(
        case_limit=args.case_limit,
        min_overlap=args.min_overlap,
        samples_per_arm=args.samples_per_arm,
        judge_passes=args.judge_passes,
        label=args.label,
        availability_probe=not args.no_availability_probe,
        read_only_tools=not args.no_read_tools,
        read_only_dirs=args.read_dir if args.read_dir is not None else default_read_only_dirs(),
        replay_model=args.replay_model,
        judge_model=args.judge_model,
    )


def compute_aggregate(rows: list[dict[str, Any]], candidate_preflights: list[dict[str, Any]], config: Config) -> dict[str, Any]:
    judged = [
        row for row in rows
        if not row.get("response_judge", row.get("judge", {})).get("skipped")
        and row.get("response_judge", row.get("judge", {})).get("returncode") == 0
    ]
    attributed = [row for row in judged if row.get("attribution_judge", {}).get("returncode") == 0]
    enabled_wins = sum(1 for row in judged if row.get("response_judge", row.get("judge", {})).get("winner_arm") == "enabled")
    disabled_wins = sum(1 for row in judged if row.get("response_judge", row.get("judge", {})).get("winner_arm") == "disabled")
    ties = sum(1 for row in judged if row.get("response_judge", row.get("judge", {})).get("winner_arm") in {"tie", "inconclusive"})
    material = sum(1 for row in judged if row.get("response_judge", row.get("judge", {})).get("material_difference") is True)
    helped = sum(1 for row in attributed if row.get("attribution_judge", {}).get("learning_helped_enabled") is True)
    hurt = sum(1 for row in attributed if row.get("attribution_judge", {}).get("learning_hurt_enabled") is True)
    relevant = sum(1 for row in attributed if row.get("attribution_judge", {}).get("learning_relevant") is True)
    attribution_counts: dict[str, int] = {}
    for row in attributed:
        label = str(row.get("attribution_judge", {}).get("attribution", "missing"))
        attribution_counts[label] = attribution_counts.get(label, 0) + 1
    decisive = enabled_wins + disabled_wins
    eligible = [
        row for row in candidate_preflights
        if row["preflight"].get("returncode") == 0
        and row["preflight"].get("has_context")
        and row["preflight"].get("overlap_fraction", 0) >= config.min_overlap
    ]
    return {
        "label": config.label,
        "case_limit": config.case_limit,
        "min_overlap": config.min_overlap,
        "samples_per_arm": config.samples_per_arm,
        "judge_passes": config.judge_passes,
        "replay_model": config.replay_model,
        "judge_model": config.judge_model,
        "candidate_preflights": len(candidate_preflights),
        "eligible_candidates": len(eligible),
        "eligible_candidates_overlap_eq_1_0": sum(1 for row in eligible if row["preflight"].get("overlap_fraction") == 1.0),
        "comparisons": len(rows),
        "judged_comparisons": len(judged),
        "attribution_judged_comparisons": len(attributed),
        "skipped_comparisons": len(rows) - len(judged),
        "enabled_wins": enabled_wins,
        "disabled_wins": disabled_wins,
        "ties_or_inconclusive": ties,
        "material_difference": material,
        "learning_relevant": relevant,
        "learning_helped_enabled": helped,
        "learning_hurt_enabled": hurt,
        "attribution_counts": attribution_counts,
        "enabled_win_rate": round(enabled_wins / len(judged), 3) if judged else None,
        "enabled_win_rate_ties_excluded": round(enabled_wins / decisive, 3) if decisive else None,
        "material_difference_rate": round(material / len(judged), 3) if judged else None,
        "learning_helped_enabled_rate": round(helped / len(attributed), 3) if attributed else None,
        "learning_hurt_enabled_rate": round(hurt / len(attributed), 3) if attributed else None,
        "enabled_win_rate_ties_excluded_wilson_95": wilson_interval(enabled_wins, decisive) if decisive else None,
        "material_difference_wilson_95": wilson_interval(material, len(judged)) if judged else None,
        "learning_helped_enabled_wilson_95": wilson_interval(helped, len(attributed)) if attributed else None,
        "learning_hurt_enabled_wilson_95": wilson_interval(hurt, len(attributed)) if attributed else None,
    }


def wilson_interval(successes: int, total: int, z: float = 1.96) -> dict[str, float]:
    if total <= 0:
        return {"low": math.nan, "high": math.nan}
    p = successes / total
    denom = 1 + z**2 / total
    centre = (p + z**2 / (2 * total)) / denom
    half = z * math.sqrt((p * (1 - p) + z**2 / (4 * total)) / total) / denom
    return {"low": round(max(0.0, centre - half), 3), "high": round(min(1.0, centre + half), 3)}


def write_impact_summary(run_dir: Path, rows: list[dict[str, Any]], aggregate: dict[str, Any]) -> None:
    summary = {
        "run_dir": str(run_dir),
        "aggregate": aggregate,
        "enabled_wins": [
            row["case"]["case_id"]
            for row in rows
            if row.get("response_judge", {}).get("winner_arm") == "enabled"
        ],
        "disabled_wins": [
            row["case"]["case_id"]
            for row in rows
            if row.get("response_judge", {}).get("winner_arm") == "disabled"
        ],
        "helped_cases": [
            row["case"]["case_id"]
            for row in rows
            if row.get("attribution_judge", {}).get("learning_helped_enabled") is True
        ],
        "hurt_cases": [
            row["case"]["case_id"]
            for row in rows
            if row.get("attribution_judge", {}).get("learning_hurt_enabled") is True
        ],
        "skipped_cases": [
            row["case"]["case_id"]
            for row in rows
            if row.get("response_judge", {}).get("skipped") is True
        ],
    }
    (run_dir / "impact_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

    lines = [
        "# Tier A Impact Summary",
        "",
        f"Run: `{run_dir}`",
        "",
        "## Summary",
        f"- Replay model: `{aggregate.get('replay_model') or 'default'}`; judge model: `{aggregate.get('judge_model') or 'default'}`",
        f"- Candidate preflights: {aggregate.get('candidate_preflights')}; eligible cases: {aggregate.get('eligible_candidates')}",
        f"- Judged: {aggregate.get('judged_comparisons')}/{aggregate.get('comparisons')}; skipped: {aggregate.get('skipped_comparisons')}",
        f"- Blind judge: enabled wins {aggregate.get('enabled_wins')}, disabled wins {aggregate.get('disabled_wins')}, ties/inconclusive {aggregate.get('ties_or_inconclusive')}",
        f"- Material differences: {aggregate.get('material_difference')} ({aggregate.get('material_difference_rate')})",
        f"- Attribution: helped enabled {aggregate.get('learning_helped_enabled')}, hurt enabled {aggregate.get('learning_hurt_enabled')}, relevant {aggregate.get('learning_relevant')}",
        f"- Attribution buckets: `{json.dumps(aggregate.get('attribution_counts', {}), sort_keys=True)}`",
        "",
        "## Case Outcomes",
    ]
    for row in rows:
        response_judge = row.get("response_judge", {})
        attribution_judge = row.get("attribution_judge", {})
        lines.append(
            "- `{case_id}`: winner `{winner}`, material `{material}`, attribution `{attribution}`, helped `{helped}`, hurt `{hurt}`".format(
                case_id=row["case"]["case_id"],
                winner=response_judge.get("winner_arm") or response_judge.get("reason"),
                material=response_judge.get("material_difference"),
                attribution=attribution_judge.get("attribution") or attribution_judge.get("reason"),
                helped=attribution_judge.get("learning_helped_enabled"),
                hurt=attribution_judge.get("learning_hurt_enabled"),
            )
        )
    lines.extend(["", "## Helped Cases"])
    lines.extend(f"- `{case_id}`" for case_id in summary["helped_cases"])
    if not summary["helped_cases"]:
        lines.append("- None")
    lines.extend(["", "## Hurt Cases"])
    lines.extend(f"- `{case_id}`" for case_id in summary["hurt_cases"])
    if not summary["hurt_cases"]:
        lines.append("- None")
    (run_dir / "impact_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    config = parse_args()
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir = EVAL_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config.json").write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")
    if VIEWER_TEMPLATE.exists():
        shutil.copy2(VIEWER_TEMPLATE, run_dir / "viewer.html")

    repo_status_root, outer_repo_status_root = repo_status_roots()
    repo_before = git_status(repo_status_root)
    outer_before = git_status(outer_repo_status_root)
    before = {
        "db": db_counts(),
        "sessions": session_counts(),
        "logs": log_offsets(),
        "runtime": runtime_identity(),
        "repo_status_root": str(repo_status_root),
        "repo_status": repo_before,
        "outer_repo_status_root": str(outer_repo_status_root) if outer_repo_status_root else None,
        "outer_repo_status": outer_before,
    }
    (run_dir / "preflight.json").write_text(json.dumps(before, indent=2), encoding="utf-8")

    if config.availability_probe:
        replay_probe = claude_availability_probe(run_dir, model=config.replay_model, name="replay")
        judge_probe = claude_availability_probe(run_dir, model=config.judge_model, name="judge")
        availability = {
            "available": replay_probe.get("available") is True and judge_probe.get("available") is True,
            "replay_model": config.replay_model,
            "judge_model": config.judge_model,
            "replay_probe": replay_probe,
            "judge_probe": judge_probe,
        }
        (run_dir / "availability.json").write_text(json.dumps(availability, indent=2), encoding="utf-8")
        if not availability.get("available"):
            after = {
                "db": db_counts(),
                "sessions": session_counts(),
                "logs": log_offsets(),
                "repo_status_root": str(repo_status_root),
                "repo_status": git_status(repo_status_root),
                "outer_repo_status_root": str(outer_repo_status_root) if outer_repo_status_root else None,
                "outer_repo_status": git_status(outer_repo_status_root),
            }
            after["log_delta_files"] = log_tails(before["logs"], run_dir)
            (run_dir / "postflight.json").write_text(json.dumps(after, indent=2), encoding="utf-8")
            report = [
                "# Tier A Replay Report",
                "",
                f"Run dir: `{run_dir}`",
                "",
                "## Summary",
                f"- Label: `{config.label}`",
                "- Status: Claude unavailable before evaluation; no cases replayed.",
                f"- Availability: `{json.dumps(availability, sort_keys=True)}`",
                f"- Runtime health: {before['runtime'].get('health_stdout')}",
            ]
            (run_dir / "report.md").write_text("\n".join(report), encoding="utf-8")
            print(json.dumps({"run_dir": str(run_dir), "error": "claude unavailable", "availability": availability}, indent=2))
            return 3

    cases, candidate_preflights = preflight_and_select_cases(run_dir, config)
    (run_dir / "cases.json").write_text(json.dumps([asdict(c) for c in cases], indent=2), encoding="utf-8")
    if not cases:
        print(json.dumps({"run_dir": str(run_dir), "error": "no Tier A cases passed current retrieval preflight"}))
        return 2

    rows: list[dict[str, Any]] = []
    for case in cases:
        preflight = next(
            row["preflight"] for row in candidate_preflights
            if row["case"]["case_id"] == case.case_id
        )
        for sample_index in range(1, config.samples_per_arm + 1):
            comparison_id = f"sample_{sample_index:02d}"
            enabled = run_arm(case, run_dir, f"{comparison_id}/enabled", disable_plugin=False)
            disabled = run_arm(case, run_dir, f"{comparison_id}/disabled", disable_plugin=True)
            validations = {
                "disabled_no_injected_ids": not disabled.get("injected_ids"),
                "disabled_no_marker": not disabled.get("has_marker"),
                "enabled_memory_context_supplied": enabled.get("memory_context_supplied") is True,
                "preflight_context_nonempty": preflight.get("has_context") is True,
                "preflight_overlap_fraction": preflight.get("overlap_fraction", 0),
                "preflight_matches_history": preflight.get("overlap_fraction", 0) >= config.min_overlap,
                "enabled_plugin_absent_for_clean_replay": PLUGIN not in enabled.get("init", {}).get("plugin_sources", []),
                "disabled_plugin_absent": PLUGIN not in disabled.get("init", {}).get("plugin_sources", []),
                "tool_surface_equal": enabled.get("init", {}).get("tools", []) == disabled.get("init", {}).get("tools", []),
                "plugin_surface_equal": enabled.get("init", {}).get("plugin_sources", []) == disabled.get("init", {}).get("plugin_sources", []),
                "both_returned_zero": enabled.get("returncode") == 0 and disabled.get("returncode") == 0,
            }
            for judge_pass in range(1, config.judge_passes + 1):
                judge_id = f"{comparison_id}/judge_{judge_pass:02d}"
                if (
                    validations["both_returned_zero"]
                    and validations["disabled_no_injected_ids"]
                    and validations["disabled_no_marker"]
                    and validations["enabled_memory_context_supplied"]
                    and validations["preflight_context_nonempty"]
                    and validations["preflight_matches_history"]
                    and validations["enabled_plugin_absent_for_clean_replay"]
                    and validations["disabled_plugin_absent"]
                    and validations["tool_surface_equal"]
                    and validations["plugin_surface_equal"]
                ):
                    response_judge = run_response_judge(case, enabled, disabled, run_dir, comparison_id=judge_id)
                    attribution_judge = run_attribution_judge(
                        case,
                        enabled,
                        disabled,
                        response_judge,
                        run_dir,
                        comparison_id=judge_id,
                    )
                else:
                    response_judge = {"skipped": True, "reason": "replay fidelity/control validation failed"}
                    attribution_judge = {"skipped": True, "reason": "response judge skipped"}
                rows.append(
                    {
                        "case": asdict(case),
                        "preflight": preflight,
                        "sample_index": sample_index,
                        "judge_pass": judge_pass,
                        "enabled": enabled,
                        "disabled": disabled,
                        "validations": validations,
                        "response_judge": response_judge,
                        "attribution_judge": attribution_judge,
                        "judge": response_judge,
                    }
                )
                (run_dir / "results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
                with (run_dir / "dataset.jsonl").open("w", encoding="utf-8") as fh:
                    for row in rows:
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    after = {
        "db": db_counts(),
        "sessions": session_counts(),
        "logs": log_offsets(),
        "repo_status_root": str(repo_status_root),
        "repo_status": git_status(repo_status_root),
        "outer_repo_status_root": str(outer_repo_status_root) if outer_repo_status_root else None,
        "outer_repo_status": git_status(outer_repo_status_root),
    }
    after["log_delta_files"] = log_tails(before["logs"], run_dir)
    (run_dir / "postflight.json").write_text(json.dumps(after, indent=2), encoding="utf-8")

    errors = []
    db_drift = []
    if before["repo_status"] != after["repo_status"] or before["outer_repo_status"] != after["outer_repo_status"]:
        errors.append("repo status changed")
    for table in ("requests_count", "interactions_count", "profiles_count", "user_playbooks_count", "agent_playbooks_count"):
        if before["db"].get(table) != after["db"].get(table):
            db_drift.append(f"db {table} changed: {before['db'].get(table)} -> {after['db'].get(table)}")
    if before["sessions"].get("jsonl_count") != after["sessions"].get("jsonl_count"):
        errors.append("main ~/.claude-smart/sessions file count changed")

    aggregate = compute_aggregate(rows, candidate_preflights, config)
    (run_dir / "aggregate.json").write_text(json.dumps(aggregate, indent=2), encoding="utf-8")

    report = ["# Tier A Replay Report", ""]
    report.append(f"Run dir: `{run_dir}`")
    report.append("")
    report.append("## Summary")
    report.append(f"- Label: `{config.label}`")
    report.append(f"- Replay model: `{config.replay_model or 'default'}`")
    report.append(f"- Judge model: `{config.judge_model or 'default'}`")
    report.append(f"- Cases run: {len(rows)}")
    report.append(f"- Candidate preflights: {len(candidate_preflights)}")
    report.append(f"- Preflight overlap threshold: {config.min_overlap}")
    report.append(f"- Judged comparisons: {aggregate['judged_comparisons']}/{aggregate['comparisons']}")
    report.append(f"- Enabled wins: {aggregate['enabled_wins']}")
    report.append(f"- Disabled wins: {aggregate['disabled_wins']}")
    report.append(f"- Tie/inconclusive: {aggregate['ties_or_inconclusive']}")
    report.append(f"- Enabled win rate (ties excluded): {aggregate['enabled_win_rate_ties_excluded']} {aggregate['enabled_win_rate_ties_excluded_wilson_95']}")
    report.append(f"- Material differences: {aggregate['material_difference']}")
    report.append(f"- Material difference rate: {aggregate['material_difference_rate']} {aggregate['material_difference_wilson_95']}")
    report.append(f"- Attribution-judged comparisons: {aggregate['attribution_judged_comparisons']}/{aggregate['comparisons']}")
    report.append(f"- Attribution judge marked learning relevant: {aggregate['learning_relevant']}")
    report.append(f"- Attribution judge marked learning helped enabled: {aggregate['learning_helped_enabled']}")
    report.append(f"- Learning-helped rate: {aggregate['learning_helped_enabled_rate']} {aggregate['learning_helped_enabled_wilson_95']}")
    report.append(f"- Attribution judge marked learning hurt enabled: {aggregate['learning_hurt_enabled']}")
    report.append(f"- Learning-hurt rate: {aggregate['learning_hurt_enabled_rate']} {aggregate['learning_hurt_enabled_wilson_95']}")
    report.append(f"- Attribution buckets: `{json.dumps(aggregate['attribution_counts'], sort_keys=True)}`")
    report.append(f"- Harness errors: {errors or 'none'}")
    report.append(f"- Allowed runtime DB drift: {db_drift or 'none'}")
    report.append(f"- Runtime health: {before['runtime'].get('health_stdout')}")
    report.append(f"- Backend: `{before['runtime'].get('backend_ps', '').splitlines()[-1] if before['runtime'].get('backend_ps') else 'unknown'}`")
    report.append("")
    report.append("## Cases")
    for row in rows:
        c = row["case"]
        report.append(f"### {c['case_id']} ({c['source_type']}) sample {row['sample_index']} judge {row['judge_pass']}")
        report.append(f"- Expected ids: `{', '.join(c['expected_ids'])}`")
        report.append(f"- Expected real ids: `{', '.join(row['preflight'].get('expected_real_ids', []))}`")
        report.append(f"- Retrieved real ids: `{', '.join(row['preflight'].get('retrieved_real_ids', []))}`")
        report.append(f"- Overlap real ids: `{', '.join(row['preflight'].get('overlap_real_ids', []))}` ({row['preflight'].get('overlap_fraction')})")
        report.append(f"- Enabled context path: `{row['preflight'].get('context_path')}`")
        report.append(f"- Disabled injected ids: `{', '.join(row['disabled'].get('injected_ids', []))}`")
        report.append(f"- Validations: `{json.dumps(row['validations'], sort_keys=True)}`")
        if row.get("response_judge"):
            report.append(f"- Blind response judge: `{json.dumps(row['response_judge'], sort_keys=True)}`")
        if row.get("attribution_judge"):
            report.append(f"- Learning attribution judge: `{json.dumps(row['attribution_judge'], sort_keys=True)}`")
        report.append("")
    report.append("## Log Deltas")
    for path, rel in after["log_delta_files"].items():
        report.append(f"- `{path}` -> `{rel}`")
    (run_dir / "report.md").write_text("\n".join(report), encoding="utf-8")
    write_impact_summary(run_dir, rows, aggregate)
    print(json.dumps({"run_dir": str(run_dir), "errors": errors, "db_drift": db_drift, "cases": len(rows)}, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
