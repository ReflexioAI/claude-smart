#!/usr/bin/env python3.12
"""Report Reflexio evidence retention and local-session link health."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

RETENTION_TARGETS = (
    "profiles",
    "interactions",
    "requests",
    "user_playbooks",
    "agent_playbooks",
    "agent_success_evaluation_result",
    "retrieved_learning_evaluation",
    "offline_tuner_reward_label",
    "share_links",
    "agent_playbook_source_user_playbooks",
    "playbook_optimization_jobs",
    "playbook_optimization_candidates",
    "playbook_optimization_evaluations",
    "playbook_optimization_events",
    "playbook_retrieval_logs",
    "skills",
)
LEARNING_IDS = {
    "profile": ("profiles", "profile_id"),
    "user_playbook": ("user_playbooks", "user_playbook_id"),
    "agent_playbook": ("agent_playbooks", "agent_playbook_id"),
}


def parse_args() -> argparse.Namespace:
    """Parse report paths and output-format flags."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=Path("~/.reflexio/data/reflexio.db").expanduser()
    )
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=Path("~/.reflexio/data/archive").expanduser(),
    )
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        default=Path("~/.claude-smart/sessions").expanduser(),
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )
    return parser.parse_args()


def table_names(conn: sqlite3.Connection) -> set[str]:
    """Return all table names visible in the SQLite database."""
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return column names for one SQLite table."""
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def iter_archive_rows(archive_dir: Path, table: str) -> Iterator[dict[str, Any]]:
    """Yield valid archived rows for one table without loading the file."""
    path = archive_dir / f"{table}.jsonl"
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as archive_file:
        for line in archive_file:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            row = record.get("row") if isinstance(record, dict) else None
            if isinstance(row, dict):
                yield row


def percent(part: int, whole: int) -> float:
    """Return a two-decimal percentage with a zero-safe denominator."""
    return round(part * 100 / whole, 2) if whole else 0.0


def non_empty_json(value: Any) -> bool:
    """Return whether a value contains a non-empty JSON list or object."""
    if value is None or value == "":
        return False
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(parsed, (list, dict)) and bool(parsed)


def selected_rows(
    conn: sqlite3.Connection,
    tables: set[str],
    table: str,
    selected_columns: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Read only the requested columns that exist on a live table."""
    if table not in tables:
        return []
    available = columns(conn, table)
    selected = [column for column in selected_columns if column in available]
    if not selected:
        return []
    return [
        dict(row) for row in conn.execute(f"SELECT {', '.join(selected)} FROM {table}")
    ]


def registry_kind(entry: dict[str, Any]) -> str | None:
    """Map a local injection entry to a canonical Reflexio learning kind."""
    if entry.get("kind") == "profile":
        return "profile"
    if entry.get("kind") == "playbook" and entry.get("source_kind") in {
        "user_playbook",
        "agent_playbook",
    }:
        return str(entry["source_kind"])
    return None


def build_report(
    conn: sqlite3.Connection, archive_dir: Path, sessions_dir: Path
) -> dict[str, Any]:
    """Build all report sections from live, archived, and local-session data."""
    tables = table_names(conn)

    retention: dict[str, dict[str, Any]] = {}
    for table in RETENTION_TARGETS:
        live_count = 0
        live_created: list[Any] = []
        if table in tables:
            live_count = int(
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            if "created_at" in columns(conn, table):
                minimum, maximum = conn.execute(
                    f"SELECT MIN(created_at), MAX(created_at) FROM {table}"
                ).fetchone()
                live_created = [
                    value for value in (minimum, maximum) if value is not None
                ]
        archived_count = 0
        archived_min: Any | None = None
        archived_max: Any | None = None
        for row in iter_archive_rows(archive_dir, table):
            archived_count += 1
            created_at = row.get("created_at")
            if created_at is not None:
                archived_min = (
                    created_at
                    if archived_min is None
                    else min(archived_min, created_at)
                )
                archived_max = (
                    created_at
                    if archived_max is None
                    else max(archived_max, created_at)
                )
        created = live_created + [
            value for value in (archived_min, archived_max) if value is not None
        ]
        retention[table] = {
            "live_rows": live_count,
            "archived_rows": archived_count,
            "min_created_at": min(created) if created else None,
            "max_created_at": max(created) if created else None,
        }

    live_interactions = selected_rows(
        conn,
        tables,
        "interactions",
        ("citations", "retrieved_learnings"),
    )
    interaction_total = len(live_interactions)
    citations_non_empty = sum(
        non_empty_json(row.get("citations")) for row in live_interactions
    )
    retrieved_non_empty = sum(
        non_empty_json(row.get("retrieved_learnings")) for row in live_interactions
    )
    for row in iter_archive_rows(archive_dir, "interactions"):
        interaction_total += 1
        citations_non_empty += non_empty_json(row.get("citations"))
        retrieved_non_empty += non_empty_json(row.get("retrieved_learnings"))
    interaction_report: dict[str, Any] = {
        "total": interaction_total,
        "citations_non_empty_percent": percent(citations_non_empty, interaction_total),
    }
    if "interactions" not in tables or "retrieved_learnings" not in columns(
        conn, "interactions"
    ):
        interaction_report["retrieved_learnings_non_empty_percent"] = "column missing"
    else:
        interaction_report["retrieved_learnings_non_empty_percent"] = percent(
            retrieved_non_empty,
            interaction_total,
        )

    known_ids: dict[str, set[str]] = {}
    for kind, (table, id_column) in LEARNING_IDS.items():
        known_ids[kind] = {
            str(row[id_column])
            for row in selected_rows(conn, tables, table, (id_column,))
            if row.get(id_column) is not None
        }
    registry_total = 0
    with_id = 0
    resolved = 0
    if sessions_dir.is_dir():
        for path in sessions_dir.glob("*.injected.jsonl"):
            with path.open("r", encoding="utf-8") as registry_file:
                for line in registry_file:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    registry_total += 1
                    real_id = str(entry.get("real_id") or "")
                    if not real_id:
                        continue
                    with_id += 1
                    kind = registry_kind(entry)
                    if kind is not None and real_id in known_ids.get(kind, set()):
                        resolved += 1
    # Entries with an ID but an unknown kind cannot resolve and are dangling.
    registry_report = {
        "total_entries": registry_total,
        "with_real_id": with_id,
        "with_real_id_percent": percent(with_id, registry_total),
        "resolved": resolved,
        "resolved_percent": percent(resolved, with_id),
        "dangling": with_id - resolved,
    }

    live_request_rows = selected_rows(
        conn,
        tables,
        "requests",
        ("session_id", "evaluation_only"),
    )
    request_sessions = {
        str(row["session_id"]) for row in live_request_rows if row.get("session_id")
    }
    evaluation_only = sum(bool(row.get("evaluation_only")) for row in live_request_rows)
    for row in iter_archive_rows(archive_dir, "requests"):
        if row.get("session_id"):
            request_sessions.add(str(row["session_id"]))
        evaluation_only += bool(row.get("evaluation_only"))
    local_sessions = (
        {
            path.name.removesuffix(".jsonl")
            for path in sessions_dir.glob("*.jsonl")
            if not path.name.endswith(".injected.jsonl")
        }
        if sessions_dir.is_dir()
        else set()
    )
    session_report = {
        "request_sessions": len(request_sessions),
        "local_sessions": len(local_sessions),
        "requests_with_local_percent": percent(
            len(request_sessions & local_sessions), len(request_sessions)
        ),
        "local_with_requests_percent": percent(
            len(request_sessions & local_sessions), len(local_sessions)
        ),
    }
    return {
        "retention_targets": retention,
        "interactions": interaction_report,
        "registry_links": registry_report,
        "session_join": session_report,
        "evaluation_only_requests": evaluation_only,
    }


def print_human(report: dict[str, Any]) -> None:
    """Print the report in compact operator-readable sections."""
    print("Evidence window")
    for table, values in report["retention_targets"].items():
        print(
            f"  {table}: live={values['live_rows']} archived={values['archived_rows']} "
            f"created_at={values['min_created_at']}..{values['max_created_at']}"
        )
    interactions = report["interactions"]
    print("\nInteractions")
    print(f"  citations non-empty: {interactions['citations_non_empty_percent']}%")
    retrieved = interactions["retrieved_learnings_non_empty_percent"]
    print(
        f"  retrieved_learnings non-empty: {retrieved}{'%' if isinstance(retrieved, float) else ''}"
    )
    links = report["registry_links"]
    print("\nRegistry links")
    print(
        f"  entries={links['total_entries']} with real_id={links['with_real_id_percent']}% resolved={links['resolved_percent']}% dangling={links['dangling']}"
    )
    sessions = report["session_join"]
    print("\nSession join")
    print(
        f"  request sessions with local file={sessions['requests_with_local_percent']}% local files with request={sessions['local_with_requests_percent']}%"
    )
    print(f"\nEvaluation-only requests: {report['evaluation_only_requests']}")


def main() -> int:
    """Run the read-only report and always return a non-gating exit code."""
    args = parse_args()
    try:
        uri = f"file:{args.db.resolve().as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            report = build_report(conn, args.archive_dir, args.sessions_dir)
        if args.json:
            print(json.dumps(report, sort_keys=True))
        else:
            print_human(report)
    except Exception as exc:  # Reporting must never gate operator workflows.
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"Report unavailable: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
