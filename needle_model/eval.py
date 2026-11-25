"""Score Needle's request and command agents on the held-out cases (eval_cases.py).

For each threshold it shows how many requests Needle acts on, how many of those are right,
and how many wrong; everything it doesn't act on goes on to the planner (or normal routing).
A call acts only through servo.cognition.intents.accept (confidence, engine flags, copied values).

usage: python needle_model/eval.py [--weights tuned.cact] [--agent requests|commands|both] [--show]
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
sys.stdout.reconfigure(encoding="utf-8")

import cactus  # noqa: E402

import eval_cases  # noqa: E402
from servo.cognition.intents import (COMMAND_SYSTEM, COMMAND_TOOLS, REQUEST_SYSTEM, REQUEST_TOOLS,  # noqa: E402
                                     accept)

THRESHOLDS = (0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7)


def match(calls, exp):
    if len(calls) != len(exp):
        return False
    used = set()
    for name, want in exp:
        for i, c in enumerate(calls):
            if i in used or c.get("name") != name:
                continue
            got = c.get("arguments") or {}
            if all((str(v).lower() in str(got.get(k, "")).lower()) if isinstance(v, str) else got.get(k) == v
                   for k, v in want.items()):
                used.add(i)
                break
        else:
            return False
    return True


def run(label, tools, system, cases, weights, show):
    agent = cactus.Needle(tools=tools, system=system, weights=weights, stateless=True)
    rows = []
    for text, exp in cases:
        t0 = time.perf_counter()
        r = agent.complete(text, max_new_tokens=200)
        ms = int((time.perf_counter() - t0) * 1000)
        calls, _ = accept(text, r, 0.0)
        conf = r.get("confidence")
        rows.append({"text": text, "exp": exp, "raw": r.get("function_calls") or [], "calls": calls,
                     "conf": conf if isinstance(conf, (int, float)) else None, "ok": match(calls, exp), "ms": ms})
    agent.close()
    inn = [r for r in rows if r["exp"]]
    print(f"\n== {label}  ({len(inn)} actions, {len(rows) - len(inn)} to leave alone; "
          f"median {sorted(r['ms'] for r in rows)[len(rows) // 2]} ms)")
    print(f"   raw tool+args right {sum(match(r['raw'], r['exp']) for r in inn)}/{len(inn)}")
    for t in THRESHOLDS:
        acted = [r for r in rows if r["calls"] and (r["conf"] is None or r["conf"] >= t)]
        right = sum(r["ok"] for r in acted)
        print(f"   gate {t:.1f}: acts on {len(acted):3d}   right {right:3d}/{len(inn)}   wrong {len(acted) - right:2d}")
        if rows[0]["conf"] is None:
            break
    if show:
        for r in rows:
            if not r["ok"] and (r["calls"] or r["exp"]):
                got = "; ".join(f"{c['name']}{c.get('arguments') or ''}" for c in r["raw"]) or "-"
                c = f"{r['conf']:.2f}" if r["conf"] is not None else "n/a"
                print(f"   ✗ [{'act ' if r['calls'] else 'drop'} {c}] {r['text']!r} -> {got}   want {r['exp'] or '-'}")
    return rows


def main():
    args = sys.argv[1:]
    weights = args[args.index("--weights") + 1] if "--weights" in args else None
    which = args[args.index("--agent") + 1] if "--agent" in args else "both"
    show = "--show" in args
    out = {}
    if which in ("requests", "both"):
        cases = eval_cases.DIRECT + [(t, []) for t in eval_cases.OUT]
        out["requests"] = run("requests", REQUEST_TOOLS, REQUEST_SYSTEM, cases, weights, show)
    if which in ("commands", "both"):
        cases = eval_cases.COMMANDS + [(t, []) for t in eval_cases.COMMANDS_OUT]
        out["commands"] = run("commands", COMMAND_TOOLS, COMMAND_SYSTEM, cases, weights, show)
    tag = os.path.splitext(os.path.basename(weights))[0] if weights else "base"
    with open(os.path.join(os.path.dirname(__file__), f"eval_{tag}.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    main()
