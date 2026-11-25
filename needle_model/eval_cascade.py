"""Evaluate the end-to-end JevGate (Laya) -> tuned Needle (intents.read) cascade on eval_cases.py."""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
sys.stdout.reconfigure(encoding="utf-8")

import eval_cases  # noqa: E402
from eval import match  # noqa: E402
from servo.cognition import intents  # noqa: E402
from servo.config import settings  # noqa: E402
from servo.control.jev_gate import JevGate  # noqa: E402


def main():
    weights = r"C:\Users\ACER\servo-models\needle-tune\needle_tuned.cact"
    settings.needle_intent_weights = weights
    settings.needle_intent_threshold = 0.7

    jev = JevGate()
    import httpx
    for _ in range(30):
        try:
            if httpx.get("http://127.0.0.1:8009/health", timeout=2).status_code == 200:
                break
        except Exception:
            pass
        time.sleep(2)
    cases = eval_cases.DIRECT + [(t, []) for t in eval_cases.OUT]

    s1_right = 0
    s1_wrong_action = 0
    s1_false_pos = 0
    s2_handoff_direct = 0
    s2_handoff_out = 0
    chat_out = 0
    latencies = []

    print(f"Running JevGate + Needle (threshold={settings.needle_intent_threshold}) on {len(cases)} cases...", flush=True)
    for text, exp in cases:
        t0 = time.perf_counter()
        assess = jev.assess_goal(text)
        if not assess.desktop_task:
            route = "chat"
            calls = []
            why = "Laya: not a desktop task"
        elif assess.complexity != "single":
            route = "planner"
            calls = []
            why = f"Laya: complexity={assess.complexity}"
        else:
            reading = intents.read(text, "request")
            calls = reading.calls
            route = "system1" if calls else "planner"
            why = reading.why

        ms = int((time.perf_counter() - t0) * 1000)
        latencies.append(ms)

        if exp:
            if route == "system1":
                if match(calls, exp):
                    s1_right += 1
                else:
                    s1_wrong_action += 1
                    print(f"  ✗ [S1 WRONG] {text!r} -> {[(c['name'], c.get('arguments')) for c in calls]} want={exp}", flush=True)
            else:
                s2_handoff_direct += 1
                print(f"  → [HANDOFF ] {text!r} ({why})", flush=True)
        else:
            if route == "system1":
                s1_false_pos += 1
                print(f"  ✗ [S1 FP   ] {text!r} -> {[(c['name'], c.get('arguments')) for c in calls]}", flush=True)
            elif route == "chat":
                chat_out += 1
            else:
                s2_handoff_out += 1

    n_dir = len(eval_cases.DIRECT)
    n_out = len(eval_cases.OUT)
    med = sorted(latencies)[len(latencies) // 2]
    print("\n=== JEVGATE + NEEDLE CASCADE SUMMARY ===")
    print(f"DIRECT single-step ({n_dir}):")
    print(f"  System 1 right       : {s1_right:2d}/{n_dir} ({s1_right / n_dir * 100:.1f}%)")
    print(f"  System 1 wrong action: {s1_wrong_action:2d}/{n_dir}")
    print(f"  Handed off to S2     : {s2_handoff_direct:2d}/{n_dir} (safe fallback)")
    print(f"OUT leave-alone ({n_out}):")
    print(f"  Safely blocked/S2    : {chat_out + s2_handoff_out:2d}/{n_out} (chat={chat_out}, planner={s2_handoff_out})")
    print(f"  False positive (S1)  : {s1_false_pos:2d}/{n_out}")
    print(f"Total System 1 errors  : {s1_wrong_action + s1_false_pos} (vs 26 standalone)")
    print(f"Median latency         : {med} ms")


if __name__ == "__main__":
    main()
