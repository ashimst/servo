"""Full run records: everything needed to understand a run after the fact.

The old SQLite trace kept a tool name per step ("run_command()") with no arguments,
no error text, no verdict, no plan and no screen, so a failed run couldn't be
diagnosed. A run record keeps:

* the outcome: goal (already secret-redacted), route, success, reply, latency, replans
* every step: the exact call, tier, verdict (confirmed / unconfirmed / failed), note,
  error and output
* every planning round: the planner's thought, subtasks, recipes used
* the complete event stream the copilot showed live (prompts, model responses,
  screen descriptions, gate decisions)

Stored as ``<SERVO_HOME>/traces/runs/<run_id>.json`` plus a one-line-per-run index
``runs.jsonl``. The newest ``MAX_RUNS`` are kept.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_RUNS = 500
_lock = threading.Lock()


def runs_dir() -> Path:
    from servo.config import settings
    return Path(settings.trace_dir) / "runs"


def _index() -> Path:
    return runs_dir() / "runs.jsonl"


def build_record(outcome: Any, events: List[Any], extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    ev = [e.model_dump() if hasattr(e, "model_dump") else dict(e) for e in events]
    rounds = []
    for e in ev:
        if e.get("kind") == "model" and e.get("tier") == "system2" and e.get("data", {}).get("subtasks") is not None:
            d = e["data"]
            rounds.append({"title": e.get("title"), "thought": e.get("detail"), "subtasks": d.get("subtasks"),
                           "knowledge": d.get("knowledge"), "provider": d.get("provider"), "model": d.get("model"),
                           "latency_ms": e.get("duration_ms"), "status": e.get("status")})
    steps = [s.model_dump() if hasattr(s, "model_dump") else dict(s) for s in getattr(outcome, "steps", [])]
    for s in steps:
        if s.get("output") and len(s["output"]) > 4000:
            s["output"] = s["output"][:4000] + "…"
    counts = {"confirmed": 0, "unconfirmed": 0, "failed": 0}
    for s in steps:
        v = s.get("verification") or ("confirmed" if s.get("success") else "failed")
        if not s.get("success"):
            v = "failed"
        counts[v] = counts.get(v, 0) + 1
    return {
        "run_id": outcome.run_id,
        "ts": time.time(),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "goal": outcome.goal,
        "route": outcome.route,
        "success": outcome.success,
        "aborted": outcome.aborted,
        "reply": outcome.reply,
        "latency_ms": outcome.latency_ms,
        "replans": outcome.replans,
        "verdicts": counts,
        "steps": steps,
        "rounds": rounds,
        "events": ev,
        **(extra or {}),
    }


def save_run(record: Dict[str, Any]) -> Optional[Path]:
    try:
        d = runs_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{record['run_id']}.json"
        with _lock:
            path.write_text(json.dumps(record, ensure_ascii=False, default=str), encoding="utf-8")
            summary = {k: record[k] for k in ("run_id", "ts", "time", "goal", "route", "success", "aborted",
                                              "reply", "latency_ms", "replans", "verdicts")}
            summary["steps"] = len(record.get("steps", []))
            with open(_index(), "a", encoding="utf-8") as f:
                f.write(json.dumps(summary, ensure_ascii=False) + "\n")
            _prune(d)
        return path
    except Exception as e:  # a recording failure must never break a run
        logger.warning("Could not save run record: %s", e)
        return None


def _prune(d: Path) -> None:
    """Keep the newest MAX_RUNS runs, by the order they were recorded in the index.

    (Not file mtimes: those can be touched by copies or editors and would make a
    just-saved run look old.)
    """
    lines = [l for l in _index().read_text(encoding="utf-8").splitlines() if l.strip()]
    if len(lines) <= MAX_RUNS:
        return
    kept = lines[-MAX_RUNS:]
    keep_ids = {json.loads(l).get("run_id") for l in kept}
    for p in d.glob("*.json"):
        if p.stem not in keep_ids:
            p.unlink(missing_ok=True)
    _index().write_text("\n".join(kept) + "\n", encoding="utf-8")


def list_runs(limit: int = 50, failed_only: bool = False) -> List[Dict[str, Any]]:
    try:
        lines = _index().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if failed_only and r.get("success"):
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


def load_run(run_id: str) -> Optional[Dict[str, Any]]:
    if not run_id.replace("_", "").replace("-", "").isalnum():
        return None  # never let an id become a path
    try:
        return json.loads((runs_dir() / f"{run_id}.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
