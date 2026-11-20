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
