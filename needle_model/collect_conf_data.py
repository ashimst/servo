"""Collect (prompt, generated_calls, is_correct) examples from needle_tuned.cact for ConfidenceHead training."""
from __future__ import annotations

import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cactus  # noqa: E402
from servo.cognition.intents import COMMAND_SYSTEM, COMMAND_TOOLS, REQUEST_SYSTEM, REQUEST_TOOLS  # noqa: E402


def match(calls, exp):
    if len(calls) != len(exp):
        return False
    used = set()
    for e in exp:
        name = e["name"]
        want = e.get("arguments") or {}
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


def main():
    weights = r"C:\Users\ACER\servo-models\needle-tune\needle_tuned.cact"
    train_path = os.path.join(os.path.dirname(__file__), "train.jsonl")
    out_path = os.path.join(os.path.dirname(__file__), "conf_train.jsonl")

    random.seed(42)
    rows = [json.loads(l) for l in open(train_path, encoding="utf-8")]
    req_pos = [r for r in rows if r["system"] == REQUEST_SYSTEM and r["answers"]]
    req_neg = [r for r in rows if r["system"] == REQUEST_SYSTEM and not r["answers"]]
    cmd_pos = [r for r in rows if r["system"] == COMMAND_SYSTEM and r["answers"]]
    cmd_neg = [r for r in rows if r["system"] == COMMAND_SYSTEM and not r["answers"]]

    sample_req = random.sample(req_pos, min(400, len(req_pos))) + req_neg
    sample_cmd = random.sample(cmd_pos, min(200, len(cmd_pos))) + cmd_neg
    print(f"Sampling {len(sample_req)} requests and {len(sample_cmd)} commands...", flush=True)

    collected = []
    t0 = time.time()
    for label, tools, system, items in [
        ("request", REQUEST_TOOLS, REQUEST_SYSTEM, sample_req),
        ("command", COMMAND_TOOLS, COMMAND_SYSTEM, sample_cmd),
    ]:
        agent = cactus.Needle(tools=tools, system=system, weights=weights, stateless=True)
        n_pos = n_neg = n_skip = 0
        for idx, r in enumerate(items):
            resp = agent.complete(r["query"], max_new_tokens=160)
            raw = resp.get("function_calls") or []
            if not raw:
                n_skip += 1
                continue
            ok = bool(r["answers"]) and match(raw, r["answers"])
            collected.append({
                "query": r["query"],
                "tools": tools,
                "system": system,
                "answers": raw,
                "label": 1.0 if ok else 0.0,
            })
            if ok:
                n_pos += 1
            else:
                n_neg += 1
            if (idx + 1) % 200 == 0:
                print(f"  [{label}] {idx + 1}/{len(items)} ({time.time() - t0:.0f}s) pos={n_pos} neg={n_neg} skip={n_skip}", flush=True)
        agent.close()
        print(f"Finished {label}: pos={n_pos} neg={n_neg} skip_empty={n_skip}", flush=True)

    random.shuffle(collected)
    with open(out_path, "w", encoding="utf-8") as f:
        for c in collected:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"Wrote {len(collected)} calibration rows to {out_path} in {time.time() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
