# Cognitive cascade

How Servo turns a request into actions. Code: `servo/cognition/router.py` (`CascadeRouter`).
Every component fails openly: when a model or server is missing, the request or step fails
and says why. There are no stand-ins.

```
request ─► Laya: desktop task? complexity? area?
             │ not a desktop task ─► System 2 answers as chat
             │ single ─► Intent fast path (Qwen3.5-4B on :8080 + accept() grounding) ─► execute
             ▼           (or tuned Needle when SERVO_INTENT_BACKEND=needle; no fit ─► escalate)
           planner agent for the area (apps · settings · files · web · code)
             writes steps in the step forms ─► each step: Needle agent for that tool
             ─► check_call (the call must match the step's words) ─► execute
             may hand the task to another agent with "next_agent"

execute one step  (servo/control/loop.py)
   find the target: UIA tree ─► Windows OCR text match ─► Qwen3.5-4B vision locator ─► fail
   Laya pre-check (risk) ──medium/high──► your confirmation in the copilot
   act once ─► verify: the control's own state or read-back text when there is one,
               otherwise Laya judges the observed screen change

on failure:  policy / cancelled                 → stop and report
             perception / verify / reasoning    → the planner re-plans with the reason and the
                                                  current screen (SERVO_MAX_REPLANS per tier),
                                                  then steps up from the local to the cloud planner
```

## System 1 vs. System 2 & How Needle Is Used

Servo separates **front-door intent routing** from **in-loop step execution** based on measured benchmarks across 97 held-out request cases (`73` direct single-step actions + `24` leave-alone cases in `needle_model/eval_cases.py`):

| Front-Door Intent Approach | Direct Right (`/73`) | Wrong Actions (`/73`) | `OUT` Blocked (`/24`) | Median Latency |
|---|---|---|---|---|
| Base Needle (`gate 0.7`) | 16 (21.9%) | 4 | 24 (100%) | 345 ms |
| Tuned Needle v3 (`gate 0.7`) | 54 (74.0%) | 15 | 13 (54.2%) | 345 ms |
| `JevGate + Needle v3` (`gate 0.7`) | 55 (75.3%) | 14 | 20 (83.3%) | 3,852 ms |
| **`Qwen3.5-4B` (`:8080`, `intent_backend=local_planner`)** | **71 (97.3%)** | **2** | 10 *(20+ with JevGate)* | **632 ms** |

### Why this architectural choice
1. **Front-door single-step routing uses `Qwen3.5-4B` + `JevGate` + `accept()` (`cognition/intents.py`):** Asking a 35–63 MB attention-only LoRA (`Needle 3`) to disambiguate 24 tools in a ~1,350-token prompt hit a capacity ceiling (`74–75%` direct accuracy with `14–15` wrong tool calls). Running `Qwen3.5-4B` on GPU (`:8080`) behind `JevGate` (`20/24` non-task/multi-step filtering) and `intents.accept()` (word-for-word grounding + negation checks) achieves **97.3% (`71/73`)** at **632 ms** median latency. Whenever no single tool fits, it hands off cleanly to System 2.
2. **Needle is used inside the closed loop (`cognition/needle.py` + `cognition/specialists.py`):** Once System 2 writes a concrete step form (e.g. `click [12]`, `type "hello" into [4]`, `open notepad`), `Tier1Intent` routes to a small per-group Needle specialist (only **1–4 tools** per prompt) and `check_call()` deterministically verifies the arguments against the step's text. That bounded, small-tool-set regime is where Needle 3 excels.

## Front ends

Both drive the same `CascadeRouter`:

* **The copilot** (`servo`, `daemon/copilot_app.py`): typed or spoken requests, a live trace,
  confirmations, a queue and a schedule.
* **MCP** (`servo --mcp`, `daemon/mcp_server.py`): `do(goal)` runs the whole pipeline;
  `screen()` + `step(text)` let a coding agent be the planner, with `router.run_step` running
  its step through the same Needle agent, `check_call`, gate and verification. While the copilot
  runs, MCP calls are forwarded to it (`daemon/discovery.py`) so they queue, show live and ask
  you; without it they run in the MCP process and anything needing confirmation is refused.

## Components

| Component | Code | What it decides |
|---|---|---|
| Laya | `control/jev_gate.py` (client), `decision_model/server.py` (server, :8009) | desktop task or chat; complexity single/multi/ambiguous; area (picks the planner agent); risk of a step; whether a step's outcome happened |
| Planner agents | `cognition/planners.py` | one agent per area, each with its own instructions and only the step forms it may use |
| System 2 | `cognition/system2.py` | runs a planner agent's prompt on the local Qwen3.5-4B (llama-server, `SERVO_LOCAL_PLANNER_URL`) and steps up to the cloud planner (`SERVO_PLANNER_PROVIDER`) per `SERVO_PLANNER_MODE`. A reply that is not JSON is asked again once on the same tier |
| Needle | `cognition/needle.py`, `cognition/specialists.py`, `cognition/cactus_runtime.py` | Cactus Needle 3, one agent per tool (29 tools in 8 groups: pointer, keyboard, form, window, web, files, system, observe). The step's verb picks the tool; Needle fills in the arguments |
| check_call | `cognition/needle.py` | deterministic: the tool must be the step form's tool, the control number, quoted text, path, keys and seconds must be the step's own |
| UIA | `perception/uia_sensor.py` | the window's controls, their state, and popups of the same app (`actuation/sandboxing.py: app_pids`) |
| Windows OCR | `perception/ocr_sensor.py` | text lines on screen; a label match scores ≥ `SERVO_TEXT_MATCH_THRESHOLD` (0.72) |
| Vision locator | `perception/locator.py` | Qwen3.5-4B with its vision projector points at the target |
| Mirage | `perception/mirage_sensor.py` | only the `describe screen` step |

Needle's confidence is not calibrated for planner steps (correct calls measured at
0.18–0.50), so it is only traced there. It gates only a user's own single request
(`SERVO_NEEDLE_CONFIDENCE_THRESHOLD`, 0.7); below it the request goes to a planner.

## Step forms

Planners refer to controls only by their number in the screen description. The full list,
with the regex that recognises each form, is `STEP_FORMS` in `cognition/specialists.py`:

```
click [12] · double click [12] · right click [12] · scroll up/down [in [3]]
type "hello" into [4] · type "hello" · press ctrl+s · clear [4]
turn on [7] · turn off [7] · set "03" in [9] · choose "Large" in [9] · expand [5] · collapse [5]
open notepad · open settings dateandtime · close "<window>" · switch to "<window>" · wait 2
go to github.com · search the web for <query>
read file C:/... · list folder downloads · write "text" to file C:/... · append "text" to file C:/...
run command "Get-Date" · describe screen · read text
```

`type "…" into [n]` replaces the field's text. File paths use forward slashes.

## Laya

[Laya](https://github.com/NandhaKishorM/laya) (Convai Innovations, Apache-2.0) is a System
One decision model. Zero-shot it cannot tell Servo's cases apart, so
`decision_model/server.py fit` trains a small head per question on labelled rows
(`decision_model/build_rows.py`) over Laya's frozen vega reader, and
`decision_model/server.py serve` answers on port 8009 (`SERVO_LAYA_URL`). A question whose
head did not beat the engine in cross-validation is answered by the engine; `/health` lists
which questions are fitted. `decision_model/eval_cases.py` scores held-out requests.

## Reasoning trace

Every decision is a `TraceEvent` on `servo.cognition.trace.bus`: Laya's probabilities, the
planner agent, each planner prompt and reply, each Needle call and its check, perception
results, actuation and verification. The copilot streams them live, and runs are saved
under `~/.servo/traces`.
