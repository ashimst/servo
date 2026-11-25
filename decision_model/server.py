"""Servo's Laya decision server: Laya's vega engine plus heads fitted on Servo's own labels.

Laya (https://github.com/NandhaKishorM/laya, Apache-2.0) answers typed questions in one
forward pass. Zero-shot it cannot tell Servo's cases apart, so ``fit`` trains a small head
per question on ``rows.jsonl`` (see build_rows.py) over Laya's frozen reader, and
calibrates it; ``serve`` answers Servo's ``/v1/systemone`` requests with those heads.

Runs in its own environment (it needs torch; Servo does not):

    uv venv --python 3.12 .venv-laya
    uv pip install --python .venv-laya "laya[serve]" --torch-backend=cpu
    .venv-laya/Scripts/python -I decision_model/server.py fit      # writes decision_model/policy/
    .venv-laya/Scripts/python -I decision_model/server.py serve    # http://127.0.0.1:8009

A question with no served head is answered by the engine itself, and ``/health`` lists
which questions are served by a fitted head, so nothing pretends to be fitted when it isn't.
"""

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROWS = HERE / "rows.jsonl"
POLICY = HERE / "policy"


def _agent(threads: int):
    import torch
    import laya

    torch.set_num_threads(threads)
    return laya.Agent(vegaml=True, device="cpu")


def fit(args) -> None:
    rows = [json.loads(line) for line in ROWS.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [{"state": r["state"], "questions": r["questions"], "gold": r["gold"]} for r in rows]
    t = time.time()
    agent = _agent(args.threads)
    print(f"engine loaded in {time.time() - t:.0f}s; fitting {len(rows)} rows", flush=True)
    t = time.time()
    # All question definitions in laya's own spelling. Without this, fit() rebuilds the set
    # from vegaml's translated copies, where a yes/no is "boolean" with no criteria, and its
    # row-count check raises KeyError('criteria') (laya 0.4.x).
    questions = {}
    for r in rows:
        questions.update(r["questions"])
    report = agent.fit(rows, questions=questions)
    print(f"fitted in {time.time() - t:.0f}s", flush=True)
    for qid, r in sorted(report["heads"].items()):
        print(f"  {qid:14s} fitted={r.get('fitted')} served={r.get('used_by_auto')} "
              + " ".join(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}"
                         for k, v in r.items() if k not in ("fitted", "used_by_auto") and not isinstance(v, (dict, list))))
    print("  serving:", report["serving"])
    POLICY.mkdir(exist_ok=True)
    agent.save_calibration(str(POLICY))
    (POLICY / "fit_report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(f"saved to {POLICY}")


def serve(args) -> None:
    import uvicorn
    from fastapi import FastAPI, HTTPException, Request

    t = time.time()
    agent = _agent(args.threads)
    serving = []
    if (POLICY / "fit_report.json").exists():
        agent.load_calibration(str(POLICY))
        serving = json.loads((POLICY / "fit_report.json").read_text(encoding="utf-8")).get("serving", [])
    else:
        print("WARNING: no fitted policy; answering with the zero-shot engine", file=sys.stderr)
    print(f"ready in {time.time() - t:.0f}s; fitted heads serve {serving}", flush=True)
    lock = threading.Lock()
    app = FastAPI(title="Servo Laya decisions")

    @app.get("/health")
    def health():
        return {"status": "ok", "engine": "laya vega", "fitted_questions": serving}

    @app.post("/v1/systemone")
    async def systemone(request: Request):
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("questions"), dict):
            raise HTTPException(400, "body must be an object with a 'questions' object")
        with lock:
            t0 = time.perf_counter()
            out = agent.predict(body.get("state"), body["questions"])
        ms = (time.perf_counter() - t0) * 1000
        return {"model": "laya-vega-servo", "answers": out["answers"],
                "usage": {"latency_ms": round(ms, 1)}}

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("command", choices=["fit", "serve"])
    p.add_argument("--host", default=os.environ.get("LAYA_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("LAYA_PORT", "8009")))
    p.add_argument("--threads", type=int, default=int(os.environ.get("LAYA_THREADS", "8")))
    args = p.parse_args()
    fit(args) if args.command == "fit" else serve(args)


if __name__ == "__main__":
    main()
