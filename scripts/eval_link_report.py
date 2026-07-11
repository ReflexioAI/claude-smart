#!/usr/bin/env python3.12
"""Report Reflexio evidence retention and local-session link health."""

from __future__ import annotations

import argparse
import json
import sqlite3
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
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def archive_rows(archive_dir: Path, table: str) -> list[dict[str, Any]]:
    path = archive_dir / f"{table}.jsonl"
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        row = record.get("row") if isinstance(record, dict) else None
        if isinstance(row, dict):
            rows.append(row)
    return rows


def percent(part: int, whole: int) -> float:
    return round(part * 100 / whole, 2) if whole else 0.0


def non_empty_json(value: Any) -> bool:
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
    tables = table_names(conn)
    archived_by_table = {
        table: archive_rows(archive_dir, table) for table in RETENTION_TARGETS
    }

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
        created = live_created + [
            row["created_at"]
            for row in archived_by_table[table]
            if row.get("created_at") is not None
        ]
        retention[table] = {
            "live_rows": live_count,
            "archived_rows": len(archived_by_table[table]),
            "min_created_at": min(created) if created else None,
            "max_created_at": max(created) if created else None,
        }

    interactions = (
        selected_rows(
            conn,
            tables,
            "interactions",
            ("citations", "retrieved_learnings"),
        )
        + archived_by_table["interactions"]
    )
    interaction_report: dict[str, Any] = {
        "total": len(interactions),
        "citations_non_empty_percent": percent(
            sum(non_empty_json(row.get("citations")) for row in interactions),
            len(interactions),
        ),
    }
    if "interactions" not in tables or "retrieved_learnings" not in columns(
        conn, "interactions"
    ):
        interaction_report["retrieved_learnings_non_empty_percent"] = "column missing"
    else:
        interaction_report["retrieved_learnings_non_empty_percent"] = percent(
            sum(non_empty_json(row.get("retrieved_learnings")) for row in interactions),
            len(interactions),
        )

    known_ids: dict[str, set[str]] = {}
    for kind, (table, id_column) in LEARNING_IDS.items():
        known_ids[kind] = {
            str(row[id_column])
            for row in selected_rows(conn, tables, table, (id_column,))
            if row.get(id_column) is not None
        }
    registry_entries: list[dict[str, Any]] = []
    if sessions_dir.is_dir():
        for path in sessions_dir.glob("*.injected.jsonl"):
            for line in path.read_text().splitlines():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    registry_entries.append(entry)
    with_id = [entry for entry in registry_entries if str(entry.get("real_id") or "")]
    resolved = sum(
        str(entry["real_id"]) in known_ids.get(kind, set())
        for entry in with_id
        if (kind := registry_kind(entry)) is not None
    )
    # Entries with an ID but an unknown kind cannot resolve and are dangling.
    registry_report = {
        "total_entries": len(registry_entries),
        "with_real_id": len(with_id),
        "with_real_id_percent": percent(len(with_id), len(registry_entries)),
        "resolved": resolved,
        "resolved_percent": percent(resolved, len(with_id)),
        "dangling": len(with_id) - resolved,
    }

    request_rows = (
        selected_rows(
            conn,
            tables,
            "requests",
            ("session_id", "evaluation_only"),
        )
        + archived_by_table["requests"]
    )
    request_sessions = {
        str(row["session_id"]) for row in request_rows if row.get("session_id")
    }
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
    evaluation_only = sum(bool(row.get("evaluation_only")) for row in request_rows)
    return {
        "retention_targets": retention,
        "interactions": interaction_report,
        "registry_links": registry_report,
        "session_join": session_report,
        "evaluation_only_requests": evaluation_only,
    }


def print_human(report: dict[str, Any]) -> None:
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
