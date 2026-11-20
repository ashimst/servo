"""Unit tests for Telemetry Store (SQLite + JSONL)."""

import json
from servo.telemetry.models import TracePayload, StepTrace, RunMetrics
from servo.telemetry.store import TraceStore


def test_telemetry_store_record_and_list(tmp_path):
    store = TraceStore(trace_dir=tmp_path)

    payload = TracePayload(
        run_id="srv_test_001",
        mode="harness",
        status="passed",
        goal="Verify search box entry",
        metrics=RunMetrics(total_latency_ms=120, uia_steps=1, mirage_fallbacks=0),
        steps=[
            StepTrace(
                step=1,
                sub_goal="Click Search",
                perception_source="uia",
                tool_call="click(0)",
                jev_pre_risk="low",
                jev_post_ok=True,
                latency_ms=120,
            )
        ],
    )

    store.record_run(payload)

    # Check JSONL
    assert store.jsonl_path.exists()
    with open(store.jsonl_path, "r", encoding="utf-8") as f:
        line = f.readline()
        data = json.loads(line)
        assert data["run_id"] == "srv_test_001"
        assert data["status"] == "passed"

    # Check SQLite
    assert store.sqlite_path.exists()
    runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0]["run_id"] == "srv_test_001"
    assert runs[0]["goal"] == "Verify search box entry"


def test_run_ids_are_unique_within_the_same_second(tmp_path):
    store = TraceStore(trace_dir=tmp_path)
    a, b = TracePayload(goal="a"), TracePayload(goal="b")
    assert a.run_id != b.run_id
    store.record_run(a)
    store.record_run(b)
    assert len(store.list_runs()) == 2


def test_confirmation_is_persisted_and_old_db_is_migrated(tmp_path):
    import sqlite3
    # Simulate a database created before the confirmation column existed.
    with sqlite3.connect(tmp_path / "traces.sqlite") as conn:
        conn.execute("CREATE TABLE steps (step_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, "
                     "step_index INTEGER NOT NULL, sub_goal TEXT NOT NULL, perception_source TEXT NOT NULL, "
                     "tool_call TEXT NOT NULL, jev_pre_risk TEXT NOT NULL, jev_post_ok BOOLEAN NOT NULL, "
                     "latency_ms INTEGER NOT NULL)")
    store = TraceStore(trace_dir=tmp_path)
    store.record_run(TracePayload(goal="g", steps=[StepTrace(
        step=1, sub_goal="s", perception_source="uia", tool_call="click(element_id=0)",
        jev_pre_risk="high", jev_post_ok=True, latency_ms=5, confirmation="approved",
    )]))
    with sqlite3.connect(tmp_path / "traces.sqlite") as conn:
        assert conn.execute("SELECT confirmation FROM steps").fetchone()[0] == "approved"
