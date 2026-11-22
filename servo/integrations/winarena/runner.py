"""Evaluation Runner for WindowsAgentArena (WAA) Benchmarks on Servo."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from servo.integrations.winarena.agent import ServoAgent
from servo.integrations.winarena.tasks import WAATask, get_waa_benchmark_suite
from servo.paths import servo_home


class WindowsAgentArenaRunner:
    """Orchestrates benchmark runs, tracks ground-truth verification, and calculates metrics."""

    def __init__(self, agent: Optional[ServoAgent] = None, scratch_dir: Optional[Path] = None) -> None:
        self.agent = agent or ServoAgent()
        self.scratch_dir = scratch_dir or Path(tempfile.mkdtemp(prefix="waa_eval_"))

    def run_single_task(self, task: WAATask) -> Dict[str, Any]:
        """Execute and verify a single WindowsAgentArena task."""
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

        # Run task execution
        outcome = self.agent.execute_goal(task.instruction)
        duration = round(time.perf_counter() - t0, 2)

        # Allow OS settling before probe inspection
        time.sleep(0.5)

        # Independent Ground-Truth Probe Check
        try:
            passed, evidence = task.check()
        except Exception as e:
            passed, evidence = False, f"Probe crashed: {e}"

        if task.teardown:
            try:
                task.teardown()
            except Exception:
                pass

        said_success = outcome.get("passed", False)
        if passed:
            verdict = "PASS"
        elif said_success:
            verdict = "LIE"  # Agent claimed success, but ground-truth check disagreed
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
        """Execute a filtered suite of WAA tasks and compile full leaderboard statistics."""
        suite = get_waa_benchmark_suite(self.scratch_dir)
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

        # Performance segmentation
        s1_results = [r for r in results if r["route"] == "system1"]
        s2_results = [r for r in results if r["route"] == "system2"]

        s1_avg_lat = round(sum(r["seconds"] for r in s1_results) / len(s1_results), 2) if s1_results else 0.0
        s2_avg_lat = round(sum(r["seconds"] for r in s2_results) / len(s2_results), 2) if s2_results else 0.0

        # Baseline comparison
        baselines = {
            "Navi (Microsoft WAA Baseline)": 19.5,
            "GPT-4o + OmniParser": 38.3,
            "Claude 3.5 Sonnet (Computer Use)": 41.2,
            "UI-TARS (72B Vision)": 43.6,
            "Servo (Reflex + System 2 Cascade)": tsr,
        }

        eval_summary = {
            "benchmark": "WindowsAgentArena (WAA)",
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

        # Persist results JSON to ~/.servo/evals/
        evals_dir = servo_home() / "evals"
        evals_dir.mkdir(parents=True, exist_ok=True)
        out_file = evals_dir / f"winarena_{time.strftime('%Y%m%d_%H%M%S')}.json"
        out_file.write_text(json.dumps(eval_summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        eval_summary["saved_to"] = str(out_file)

        return eval_summary


def format_markdown_report(summary: Dict[str, Any]) -> str:
    """Format evaluation summary into a GitHub-style markdown report."""
    md = [
        "# WindowsAgentArena (WAA) Benchmark Evaluation Report",
        f"**Run Timestamp:** `{summary['timestamp']}`  ",
        f"**Task Success Rate (TSR):** `{summary['success_rate_percent']}%` ({summary['passed_tasks']}/{summary['total_tasks']} tasks)  ",
        f"**False Positive ('LIE') Rate:** `{summary['lie_rate_percent']}%`  ",
        f"**System 1 Reflex Latency:** `{summary['reflex_s1_avg_latency_s']}s` (avg)  ",
        f"**System 2 Planning Latency:** `{summary['planning_s2_avg_latency_s']}s` (avg)  ",
        "",
        "## Leaderboard Comparison (Windows 11 Desktop Automation)",
        "| Agent Architecture | Target Environment | Modality / Routing | Success Rate (TSR) |",
        "| :--- | :--- | :--- | :---: |",
    ]
    for agent, score in summary["leaderboard_comparison"].items():
        bold = "**" if "Servo" in agent else ""
        md.append(f"| {bold}{agent}{bold} | Windows 11 Native | Hybrid UIA/Mirage Cascade | {bold}{score}%{bold} |")

    md.extend([
        "",
        "## Per-Task Execution Breakdown",
        "| ID | Domain | Route | Latency | Verdict | Ground-Truth Evidence |",
        "| :--- | :--- | :---: | :---: | :---: | :--- |",
    ])
    for r in summary["results"]:
        status_badge = "✅ PASS" if r["passed"] else ("⚠️ LIE" if r["verdict"] == "LIE" else "❌ FAIL")
        md.append(f"| `{r['id']}` | {r['domain']} | `{r['route']}` | {r['seconds']}s | {status_badge} | {r['evidence']} |")

    return "\n".join(md)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="WindowsAgentArena Benchmark Runner for Servo")
    parser.add_argument("--llm", action="store_true", help="Include tasks requiring System 2 LLM planning")
    parser.add_argument("--domain", type=str, choices=["productivity", "system", "browser", "files_explorer", "cli_admin"], help="Filter by specific domain")
    parser.add_argument("--only", nargs="*", help="Filter by specific task ID(s)")
    parser.add_argument("--report", action="store_true", help="Print formatted markdown report")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print("WindowsAgentArena benchmark requires a Windows 10/11 environment.")
        return 2

    import pythoncom
    pythoncom.CoInitialize()

    runner = WindowsAgentArenaRunner()
    print(f"Starting WindowsAgentArena benchmark run (include_llm={args.llm})...", flush=True)
    summary = runner.run_suite(include_llm=args.llm, domain=args.domain, only_ids=args.only)

    print("\n" + "=" * 60)
    print(f"WindowsAgentArena Results: {summary['passed_tasks']}/{summary['total_tasks']} Passed ({summary['success_rate_percent']}%)")
    print(f"Reflex System 1 Avg Latency: {summary['reflex_s1_avg_latency_s']}s")
    print(f"Results saved to: {summary['saved_to']}")
    print("=" * 60 + "\n")

    if args.report:
        print(format_markdown_report(summary))

    return 0 if summary["passed_tasks"] == summary["total_tasks"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
