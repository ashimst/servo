"""Full run records: saved per run, listed newest first, pruned, and safe to load by id."""

from servo.cognition.router import RunOutcome, StepRecord
from servo.cognition.trace import TraceEvent
from servo.config import settings
from servo.telemetry import run_record as rr


def _out(run_id, success=True, verification="confirmed"):
    return RunOutcome(run_id=run_id, goal=f"goal {run_id}", success=success, reply="r", route="system1",
                      steps=[StepRecord(command="open notepad", tool="launch_app", call="launch_app(app_name='notepad')",
                                        tier="uia", success=success, verification=verification,
                                        error=None if success else "did not appear")])


def test_record_keeps_calls_verdicts_rounds_and_events(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "trace_dir", tmp_path)
    events = [TraceEvent(run_id="a", kind="model", tier="system2", title="System 2 · plan", detail="thinking",
                         data={"subtasks": ["open notepad"], "knowledge": {"recipes": ["notepad"]}})]
    rec = rr.build_record(_out("a", verification="unconfirmed"), events)
    rr.save_run(rec)
    loaded = rr.load_run("a")
    assert loaded["steps"][0]["call"] == "launch_app(app_name='notepad')"
    assert loaded["verdicts"] == {"confirmed": 0, "unconfirmed": 1, "failed": 0}
    assert loaded["rounds"][0]["thought"] == "thinking" and loaded["rounds"][0]["knowledge"]["recipes"] == ["notepad"]
    assert loaded["events"][0]["title"] == "System 2 · plan"


def test_list_newest_first_and_failed_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "trace_dir", tmp_path)
    rr.save_run(rr.build_record(_out("one"), []))
    rr.save_run(rr.build_record(_out("two", success=False), []))
    assert [r["run_id"] for r in rr.list_runs()] == ["two", "one"]
    assert [r["run_id"] for r in rr.list_runs(failed_only=True)] == ["two"]


def test_prune_keeps_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "trace_dir", tmp_path)
    monkeypatch.setattr(rr, "MAX_RUNS", 3)
    import os
    for i in range(5):
        p = rr.save_run(rr.build_record(_out(f"r{i}"), []))
        os.utime(p, (1, 1))  # ancient mtimes must not matter: order of recording decides
    assert len(list(rr.runs_dir().glob("*.json"))) == 3
    assert rr.load_run("r0") is None and rr.load_run("r4") is not None
    assert len(rr.list_runs()) == 3


def test_ids_cannot_escape_the_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "trace_dir", tmp_path)
    assert rr.load_run("../../secret") is None
    assert rr.load_run("a/b") is None
