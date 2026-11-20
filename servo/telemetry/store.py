"""Dual-storage Telemetry Store: Fast append-only JSONL + queryable SQLite.

See ADR-11 and Section 5.6 in SERVO_ARCHITECTURE_V1.md.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import List, Optional
from servo.config import settings
from servo.telemetry.models import TracePayload, StepTrace, RunMetrics


class TraceStore:
    """Manages recording execution traces to SQLite and JSONL with automatic directory creation."""

    def __init__(self, trace_dir: Optional[Path] = None) -> None:
        self.trace_dir = trace_dir or settings.trace_dir
        self.trace_dir.mkdir(parents=True, exist_ok=True)

        self.sqlite_path = self.trace_dir / "traces.sqlite"
        self.jsonl_path = self.trace_dir / "traces.jsonl"
        self._init_sqlite()

    def _init_sqlite(self) -> None:
        """Create relational schema if not already present."""
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    total_latency_ms INTEGER,
                    uia_steps INTEGER,
                    mirage_fallbacks INTEGER,
                    system2_escalations INTEGER
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS steps (
                    step_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    sub_goal TEXT NOT NULL,
                    perception_source TEXT NOT NULL,
                    tool_call TEXT NOT NULL,
                    jev_pre_risk TEXT NOT NULL,
                    jev_post_ok BOOLEAN NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    confirmation TEXT,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                """
            )
            # Migrate databases created before the confirmation column existed.
            cols = {row[1] for row in conn.execute("PRAGMA table_info(steps)")}
            if "confirmation" not in cols:
                conn.execute("ALTER TABLE steps ADD COLUMN confirmation TEXT")
            conn.commit()

    def record_run(self, payload: TracePayload) -> None:
        """Persist execution trace to both JSONL stream and SQLite database."""
        # 1. Fast JSONL append
        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(payload.model_dump_json() + "\n")

        # 2. SQLite relational insert
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, mode, status, goal, total_latency_ms, uia_steps, mirage_fallbacks, system2_escalations
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    payload.run_id,
                    payload.mode,
                    payload.status,
                    payload.goal,
                    payload.metrics.total_latency_ms,
                    payload.metrics.uia_steps,
                    payload.metrics.mirage_fallbacks,
                    payload.metrics.system2_escalations,
                ),
            )

            for step in payload.steps:
                conn.execute(
                    """
                    INSERT INTO steps (
                        run_id, step_index, sub_goal, perception_source, tool_call, jev_pre_risk, jev_post_ok,
                        latency_ms, confirmation
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        payload.run_id,
                        step.step,
                        step.sub_goal,
                        step.perception_source,
                        step.tool_call,
                        step.jev_pre_risk,
                        step.jev_post_ok,
                        step.latency_ms,
                        step.confirmation,
                    ),
                )
            conn.commit()

    def list_runs(self, limit: int = 50) -> List[dict]:
        """Query recent automation runs from SQLite."""
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM runs ORDER BY timestamp DESC LIMIT ?", (limit,)
            )
            return [dict(row) for row in cursor.fetchall()]
