from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from app.config import settings

DB_PATH = Path(__file__).resolve().parent.parent / "incidentiq.db"
DEFAULT_DATABASE_URL = "sqlite:///./incidentiq.db"


def configured_db_path() -> Path:
    database_url = settings.database_url
    if database_url == DEFAULT_DATABASE_URL:
        return DB_PATH
    if not database_url.startswith("sqlite:///"):
        raise ValueError("Only SQLite DATABASE_URL values are supported.")

    raw_path = database_url[len("sqlite:///"):]
    if not raw_path or raw_path == ":memory:":
        raise ValueError("DATABASE_URL must point to a persistent SQLite file.")
    if re.match(r"^/[A-Za-z]:/", raw_path):
        raw_path = raw_path[1:]
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    return path


def get_connection() -> sqlite3.Connection:
    database_path = configured_db_path()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(get_connection()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                severity TEXT NOT NULL,
                services TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                logs TEXT,
                root_cause TEXT,
                recommended_resolution TEXT,
                evidence TEXT,
                confidence REAL DEFAULT 0.0,
                human_investigation_required INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                historical_matches TEXT,
                outcome_notes TEXT,
                memory_status TEXT DEFAULT 'unavailable'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id TEXT,
                title TEXT,
                content TEXT NOT NULL,
                bank_id TEXT,
                category TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                resolution TEXT,
                success INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS incident_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_id TEXT UNIQUE NOT NULL,
                incident_id TEXT NOT NULL,
                description TEXT NOT NULL,
                rationale TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                decision_note TEXT NOT NULL DEFAULT '',
                execution_note TEXT NOT NULL DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                decided_at TEXT,
                executed_at TEXT
            )
            """
        )
        conn.commit()


def create_incident(payload: dict[str, Any]) -> dict[str, Any]:
    init_db()
    with closing(get_connection()) as conn:
        conn.execute(
            """
            INSERT INTO incidents (
                incident_id, title, summary, severity, services, source, status, logs,
                root_cause, recommended_resolution, evidence, confidence,
                human_investigation_required, historical_matches, outcome_notes, memory_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["incident_id"],
                payload["title"],
                payload["summary"],
                payload["severity"],
                payload.get("services", ""),
                payload.get("source", "manual"),
                payload.get("status", "open"),
                payload.get("logs", ""),
                payload.get("root_cause"),
                payload.get("recommended_resolution"),
                json.dumps(payload.get("evidence", [])),
                payload.get("confidence", 0.0),
                1 if payload.get("human_investigation_required") else 0,
                json.dumps(payload.get("historical_matches", [])),
                payload.get("outcome_notes"),
                payload.get("memory_status", "unavailable"),
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM incidents WHERE incident_id = ?",
            (payload["incident_id"],),
        ).fetchone()
        return dict(row)


def list_incidents() -> list[dict[str, Any]]:
    init_db()
    with closing(get_connection()) as conn:
        rows = conn.execute(
            "SELECT * FROM incidents ORDER BY created_at DESC"
        ).fetchall()
        return [
            {
                **dict(row),
                "evidence": json.loads(row["evidence"] or "[]"),
                "historical_matches": json.loads(row["historical_matches"] or "[]"),
                "human_investigation_required": bool(row["human_investigation_required"]),
            }
            for row in rows
        ]


def get_incident(incident_id: str) -> dict[str, Any] | None:
    init_db()
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT * FROM incidents WHERE incident_id = ?",
            (incident_id,),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["evidence"] = json.loads(data["evidence"] or "[]")
        data["historical_matches"] = json.loads(data["historical_matches"] or "[]")
        data["human_investigation_required"] = bool(data["human_investigation_required"])
        return data


def update_incident(incident_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    init_db()
    if not updates:
        return get_incident(incident_id)
    fields = []
    values = []
    for key, value in updates.items():
        if key == "evidence":
            value = json.dumps(value)
        if key == "historical_matches":
            value = json.dumps(value)
        if key == "human_investigation_required":
            value = int(bool(value))
        fields.append(f"{key} = ?")
        values.append(value)
    values.append(incident_id)
    with closing(get_connection()) as conn:
        conn.execute(
            f"UPDATE incidents SET {', '.join(fields)}, updated_at = CURRENT_TIMESTAMP WHERE incident_id = ?",
            tuple(values),
        )
        conn.commit()
    return get_incident(incident_id)


def insert_memory_event(incident_id: str, title: str, content: str, bank_id: str, category: str, resolution: str | None = None, success: bool = False) -> None:
    init_db()
    with closing(get_connection()) as conn:
        conn.execute(
            """
            INSERT INTO memory_events (incident_id, title, content, bank_id, category, resolution, success)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (incident_id, title, content, bank_id, category, resolution, 1 if success else 0),
        )
        conn.commit()


def read_memory_events(category: str | None = None, bank_id: str | None = None) -> list[dict[str, Any]]:
    init_db()
    query = "SELECT * FROM memory_events"
    params: list[Any] = []
    filters = []
    if category is not None:
        filters.append("category = ?")
        params.append(category)
    if bank_id is not None:
        filters.append("bank_id = ?")
        params.append(bank_id)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY created_at DESC, id DESC"
    with closing(get_connection()) as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]


def create_incident_action(
    action_id: str,
    incident_id: str,
    description: str,
    rationale: str,
) -> dict[str, Any]:
    init_db()
    with closing(get_connection()) as conn:
        conn.execute(
            """
            INSERT INTO incident_actions (action_id, incident_id, description, rationale)
            VALUES (?, ?, ?, ?)
            """,
            (action_id, incident_id, description, rationale),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM incident_actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        return dict(row)


def list_incident_actions(incident_id: str) -> list[dict[str, Any]]:
    init_db()
    with closing(get_connection()) as conn:
        rows = conn.execute(
            "SELECT * FROM incident_actions WHERE incident_id = ? ORDER BY created_at, id",
            (incident_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_incident_action(action_id: str) -> dict[str, Any] | None:
    init_db()
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT * FROM incident_actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        return dict(row) if row is not None else None


def transition_incident_action(
    action_id: str,
    expected_status: str,
    status: str,
    note_field: str,
    note: str,
    timestamp_field: str,
    timestamp: str,
) -> dict[str, Any] | None:
    allowed_fields = {
        "decision_note": "decided_at",
        "execution_note": "executed_at",
    }
    if allowed_fields.get(note_field) != timestamp_field:
        raise ValueError("Invalid incident action transition fields.")
    init_db()
    with closing(get_connection()) as conn:
        cursor = conn.execute(
            f"""
            UPDATE incident_actions
            SET status = ?, {note_field} = ?, {timestamp_field} = ?
            WHERE action_id = ? AND status = ?
            """,
            (status, note, timestamp, action_id, expected_status),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM incident_actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        return dict(row) if row is not None else None
