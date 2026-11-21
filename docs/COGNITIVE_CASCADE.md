# Cognitive cascade

Servo handles each request with the cheapest tier that can do it, and escalates only
when a tier knows it is out of its depth. Code: `servo/cognition/router.py`.

```
request ─► Tier 1: command grammar → Cactus Needle 3 ──confident + Kev says "single"──► execute
               │ reasoning miss (no confident parse, or Kev says multi/ambiguous)
               ▼
           System 2 (LLM) writes plain-language subtasks ──each one back down to Tier 1──► execute

execute one step:
   perceive   UIA tree (desktop) or Playwright DOM (web)
   └─ perception miss ──► Mirage on a screenshot (sideways, same step, no LLM)
   Kev pre-check (risk + scope) ──medium/high──► your confirmation in the copilot
   actuate once ──► Kev post-check (did the expected change happen?)

on failure:  policy / cancelled          → stop and report
             perception / verify / reasoning → System 2 re-plans with what was tried,
                                               why it failed and the current screen
                                               (at most SERVO_MAX_REPLANS times)
```

Two rules keep this safe on a real desktop:

* **Decide before acting.** Every tier exposes a confidence signal that is checked
  *before* anything is clicked. A failed attempt never triggers a blind retry of the
  same input; it triggers a new decision.
* **Separate the two kinds of miss.** A target that is not in the accessibility tree is
  a perception problem and goes sideways to Mirage. A request that needs several steps
  or judgement is a reasoning problem and goes up to System 2.

## Tier contracts

| Tier | Input | Output | Confidence signal | Escalates when |
|---|---|---|---|---|
| Command grammar (`cognition/needle.py`) | one command | tool call(s) | exact match → 0.95; rejects matches that swallowed a sentence | no match |
| Cactus Needle 3 (`cactus.Needle(...).complete()`) | one command + tool schemas | `function_calls`, `reasoning`, `confidence` | calibrated `confidence` ≥ `SERVO_NEEDLE_CONFIDENCE_THRESHOLD` (0.7) | below threshold or no call → reasoning miss |
| Kev routing (`control/jev_gate.py`) | the request | P(desktop task), complexity ∈ {single, multi, ambiguous} | calibrated probabilities | complexity ≠ single → System 2 |
| UIA (`perception/uia_sensor.py`) | target label | exactly one element | unique match | none/ambiguous → Mirage / System 2 |
| Playwright DOM (`cognition/browser.py`) | target label | one locator | exact role/label/placeholder match 0.93–0.95, fuzzy 0.7–0.78; count must be 1 | 0 → Mirage; >1 → System 2 with candidates |
| Mirage (`perception/mirage_sensor.py`) | screenshot + target | detections with boxes | fuzzy label score ≥ `SERVO_MIRAGE_MATCH_THRESHOLD` (0.72), ties penalized | no confident match → System 2 with what Mirage saw |
| Kev pre-check | sub-goal, tool call, element | risk (low/medium/high), scope | P(high) ≥ `SERVO_KEV_HIGH_RISK_FLOOR` counts as high; keyword floor can only raise risk | out of scope → stop; medium/high → ask you |
| Kev post-check | expected outcome + observed delta | transition ok? | P(ok) | not ok → System 2 re-plan |
| System 2 (`cognition/system2.py`) | goal, completed steps, failure, screen description | `{thought, reply, subtasks[], done}` | — (cactus-engine adds `confidence`/`cloud_handoff`) | bounded by `SERVO_MAX_REPLANS` |

System 2 never emits tool calls. Its subtasks use this grammar, and Tier 1 grounds each
one against the live screen at execution time:

```
open <app> · close <app> · go to <web address> · search the web for <query>
click <label> · double click <label> · right click <label>
type "<text>" into <label> · type "<text>" · press <keys> · wait <seconds>
```

## Models and their real APIs

* **Cactus Needle 3 and Mirage** come from `cactus-compute` ≥ 3.3
  ([github.com/cactus-compute/cactus](https://github.com/cactus-compute/cactus)).
  `Needle.complete()` proposes calls without running them and returns a calibrated
  `confidence`. Mirage's `describe(image, sections=["objects","text"], fields=["bbox"])`
  returns engine JSON whose detection schema Cactus does not document, so
  `parse_detections` accepts the plausible shapes and drops anything it cannot place.
* **cactus-engine** (local LLM with confidence probe and cloud handoff) can be System 2
  via `SERVO_PLANNER_PROVIDER=cactus`. It imports as `cactus` too, so it cannot be
  installed in the same environment as `cactus-compute`.
* **Kev** ([github.com/jaredpalmer/kev](https://github.com/jaredpalmer/kev)) is an open
  implementation of TypeSafe's System One decision API. Run it with
  `uv run --extra serve python -m kev.serve --run jaredpalmer/kev-4b --port 8009` and set
  `SERVO_KEV_URL=http://127.0.0.1:8009`. Request: `{state, model, questions: {id: {type,
  instructions, criteria}}}`; answers carry per-option `probabilities`. Without a server,
  every Kev question falls back to heuristics and the trace labels it as such.
* **Playwright** drives a persistent Edge/Chrome profile (`SERVO_BROWSER_CHANNEL`,
  `~/.servo/browser-profile`).

## Reasoning trace

Every decision above is a `TraceEvent` on `servo.cognition.trace.bus`: route choices
and why, each tier tried with its confidence, perception results (UIA labels, DOM
locator strategies, Mirage candidates), Kev probabilities, the exact System 2 prompt,
response and visible model reasoning (Anthropic summarized thinking, `reasoning_content`
/ `<think>` from OpenAI-compatible servers, Gemini thought parts, cactus-engine
`thinking`), actuation, and verification. The copilot streams them live.

## Calibrating the thresholds

The defaults are starting points. Set them against a small labeled set: about 30
requests, each tagged with the tier that *should* handle it. Run them with a dry-run
actuator, read the trace's confidences, and pick the thresholds that put each request
in its labeled tier.
