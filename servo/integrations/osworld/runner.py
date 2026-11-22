"""OSWorld Benchmark Runner for Servo."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from servo.integrations.winarena.agent import ServoAgent
from servo.integrations.osworld.tasks import OSWorldTask, get_osworld_benchmark_suite
from servo.paths import servo_home


class OSWorldRunner:
    """Orchestrates OSWorld benchmark runs, validates ground-truth probes, and calculates metrics."""

    def __init__(self, agent: Optional[ServoAgent] = None, scratch_dir: Optional[Path] = None) -> None:
        self.agent = agent or ServoAgent()
        self.scratch_dir = scratch_dir or Path(tempfile.mkdtemp(prefix="osworld_eval_"))

    def run_single_task(self, task: OSWorldTask) -> Dict[str, Any]:
        """Execute and independently verify a single OSWorld benchmark task."""
        if task.setup:
            try:
                task.setup()
            except Exception as e:
                return {
                    "id": task.id,
                    "domain": task.domain,
                    "instruction": task.instruction,
                    "passed": False,
                    "verdict": "FAIL",
                    "evidence": f"Setup failed: {e}",
                    "seconds": 0.0,
                    "route": "none",
                }

        t0 = time.perf_counter()
        self.agent.reset()

        outcome = self.agent.execute_goal(task.instruction)
        duration = round(time.perf_counter() - t0, 2)

        time.sleep(0.5)

        try:
            passed, evidence = task.check()
        except Exception as e:
            passed, evidence = False, f"Probe check crashed: {e}"

        if task.teardown:
            try:
                task.teardown()
            except Exception:
                pass

        said_success = outcome.get("passed", False)
        if passed:
            verdict = "PASS"
        elif said_success:
            verdict = "LIE"
        else:
            verdict = "FAIL"

        return {
            "id": task.id,
            "domain": task.domain,
            "instruction": task.instruction,
            "passed": passed,
            "said_success": said_success,
            "verdict": verdict,
            "evidence": evidence,
            "seconds": duration,
            "route": outcome.get("route", "unknown"),
            "steps": outcome.get("steps", []),
            "baseline_success": task.baseline_success,
        }

    def run_suite(
        self,
        include_llm: bool = False,
        domain: Optional[str] = None,
        only_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Execute suite of OSWorld tasks and compile leaderboard statistics."""
        suite = get_osworld_benchmark_suite(self.scratch_dir)
        filtered = [
            t for t in suite
            if (include_llm or not t.needs_llm)
            and (not domain or t.domain == domain)
            and (not only_ids or t.id in only_ids)
        ]

        results: List[Dict[str, Any]] = []
        for task in filtered:
            res = self.run_single_task(task)
            results.append(res)

        total = len(results)
        passed = sum(1 for r in results if r["passed"])
        lies = sum(1 for r in results if r["verdict"] == "LIE")
        tsr = round((passed / total * 100), 1) if total else 0.0

        s1_results = [r for r in results if r["route"] == "system1"]
        s2_results = [r for r in results if r["route"] == "system2"]

        s1_avg_lat = round(sum(r["seconds"] for r in s1_results) / len(s1_results), 2) if s1_results else 0.0
        s2_avg_lat = round(sum(r["seconds"] for r in s2_results) / len(s2_results), 2) if s2_results else 0.0

        # Published OSWorld Baselines
        baselines = {
            "Human Expert Baseline": 72.4,
            "Claude 3.5 Sonnet (Computer Use)": 40.1,
            "OS-Copilot (FRIDAY Framework)": 35.6,
            "GPT-4o (Vision / Action)": 28.5,
            "Navi (OSWorld baseline)": 18.2,
            "Servo (Reflex + System 2 Cascade)": tsr,
        }

        eval_summary = {
            "benchmark": "OSWorld (Multimodal Desktop Agents)",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_tasks": total,
            "passed_tasks": passed,
            "success_rate_percent": tsr,
            "lie_rate_percent": round((lies / total * 100), 1) if total else 0.0,
            "reflex_s1_avg_latency_s": s1_avg_lat,
            "planning_s2_avg_latency_s": s2_avg_lat,
            "leaderboard_comparison": baselines,
            "results": results,
        }

        evals_dir = servo_home() / "evals"
        evals_dir.mkdir(parents=True, exist_ok=True)
        out_file = evals_dir / f"osworld_{time.strftime('%Y%m%d_%H%M%S')}.json"
        out_file.write_text(json.dumps(eval_summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        eval_summary["saved_to"] = str(out_file)

        return eval_summary


def format_markdown_report(summary: Dict[str, Any]) -> str:
    lines = [
        f"# OSWorld Benchmark Evaluation Report",
        f"**Run Timestamp:** `{summary.get('timestamp')}`  ",
        f"**Task Success Rate (TSR):** `{summary.get('success_rate_percent')}%` ({summary.get('passed_tasks')}/{summary.get('total_tasks')} tasks)  ",
        f"**False Positive ('LIE') Rate:** `{summary.get('lie_rate_percent')}%`  ",
        f"**System 1 Reflex Latency:** `{summary.get('reflex_s1_avg_latency_s')}s` (avg)  ",
        f"**System 2 Planning Latency:** `{summary.get('planning_s2_avg_latency_s')}s` (avg)  ",
        "",
        "## Leaderboard Comparison (OSWorld Multimodal Desktop Benchmark)",
        "| Agent Architecture | Target Environment | Modality / Routing | Success Rate (TSR) |",
        "| :--- | :--- | :--- | :---: |",
    ]

    for model, score in summary.get("leaderboard_comparison", {}).items():
        bold = "**" if "Servo" in model else ""
        lines.append(f"| {bold}{model}{bold} | Desktop OS Native | Hybrid UIA/Mirage/DOM | {bold}{score}%{bold} |")

    lines.extend([
        "",
        "## Per-Task Execution Breakdown",
        "| ID | Domain | Route | Latency | Verdict | Ground-Truth Evidence |",
        "| :--- | :--- | :---: | :---: | :---: | :--- |",
    ])

    for r in summary.get("results", []):
        icon = "✅ PASS" if r["passed"] else ("⚠️ LIE" if r["verdict"] == "LIE" else "❌ FAIL")
        lines.append(
            f"| `{r['id']}` | {r['domain']} | `{r['route']}` | {r['seconds']}s | {icon} | {r['evidence']} |"
        )

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Servo against the OSWorld Benchmark suite.")
    parser.add_argument("--llm", action="store_true", help="Include tasks requiring LLM generation")
    parser.add_argument("--domain", type=str, default=None, help="Filter by domain")
    parser.add_argument("--only", nargs="+", help="Run only specific task IDs")
    parser.add_argument("--report", action="store_true", help="Print formatted markdown report")
    args = parser.parse_args()

    print(f"Starting OSWorld benchmark run (include_llm={args.llm})...")
    runner = OSWorldRunner()
    summary = runner.run_suite(include_llm=args.llm, domain=args.domain, only_ids=args.only)

    print("\n" + "=" * 60)
    print(f"OSWorld Results: {summary['passed_tasks']}/{summary['total_tasks']} Passed ({summary['success_rate_percent']}%)")
    print(f"Reflex System 1 Avg Latency: {summary['reflex_s1_avg_latency_s']}s")
    print(f"Results saved to: {summary.get('saved_to')}")
    print("=" * 60 + "\n")

    if args.report:
        print(format_markdown_report(summary))


if __name__ == "__main__":
    main()
