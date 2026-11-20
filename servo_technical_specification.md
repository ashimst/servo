# Servo — Technical Architecture & v1 Implementation Specification

**Project:** Servo (Noesis Frameworks)  
**Paradigm:** Local-First, Closed-Loop Neuro-Symbolic Desktop Control Runtime  
**Target OS (v1):** Windows 10/11 (`pywinauto` UIA + Win32)  
**Core Memory Budget (System 1):** < 70 MB active (Cactus Mirage 22 MB + Needle 3 8–29 MB + Jev)

---

## 1. Core Paradigm: Closed-Loop Control

Servo replaces slow, high-VRAM VLM-in-the-loop desktop agents with a **three-tier servomechanism**:

1. **Reference Setpoint (System 2 — Pluggable LLM / External Agent):** Decomposes a high-level goal into bounded sub-goals, then steps out of the loop. Never processes raw screenshots or pixel coordinates.
2. **Feedback Sensor (`perception/`):** Samples the target window via Windows UI Automation (`pywinauto`) first, falling back to **Cactus Mirage (22 MB)** when controls are sparse or unlabeled. Outputs a normalized integer `element_id` map.
3. **Comparator & Safety Gate (`control/jev_gate.py`):** Non-autoregressive **Jev** evaluates pre-action risk (`low | medium | high`) and post-action state transition (`transition_ok: bool`).
4. **Actuator (`actuation/`):** **Cactus Needle 3** selects a typed `@cactus.tool` using integer `element_id`s; deterministic Python computes pixel centers and drives smooth mouse trajectories and keyboard inputs.

```text
[User / Coding Agent (MCP) / Watchdog Trigger]
                      │
                      ▼
       ┌─────────────────────────────┐
       │  System 2 (Pluggable LLM)   │ ◄── Escalation only (Compressed State Diff)
       └──────────────┬──────────────┘
                      │ Macro Sub-Goal Plan
                      ▼
       ┌─────────────────────────────┐
       │   Servo Control Loop (v1)   │
       │  1. Sense: UIA -> Mirage    │
       │  2. Gate:  Jev (Pre-Risk)   │
       │  3. Act:   Needle 3 -> Win32│
       │  4. Check: Jev (Post-Diff)  │
       └──────────────┬──────────────┘
                      ▼
        [Structured JSON Trace Output]
```

---

## 2. Perception, Scoping & Watchdog (`perception/`)

### 2.1 Hybrid Perception Strategy (UIA-First, Mirage Fallback)
* **Primary Pass (UIA):** Query the target window's accessibility tree via `pywinauto` (`backend="uia"`). Filter to interactive, visible controls (`Button`, `Edit`, `CheckBox`, `MenuItem`, `TabItem`, `Hyperlink`) with non-zero bounding boxes.
* **Fallback Trigger (Mirage 22 MB):** Automatically invoke `cactus.Mirage().describe()` when:
  * UIA returns fewer than `N` interactive elements (e.g., `< 3` on Electron, Canvas, WebGL, or custom-drawn apps), OR
  * Attempt 1 (UIA) fails to locate the target element or fails post-action verification.
* **Unified Scene Index (`scene_index.py`):** Every detected element—regardless of source—is mapped to a sequential integer ID (`0, 1, 2...`) so Needle 3 never handles raw pixel coordinates:

```json
{
  "hwnd": 198432,
  "window_title": "Settings",
  "source": "uia",
  "elements": {
    "0": {"label": "Search settings", "role": "Edit", "bbox": [120, 45, 480, 82]},
    "1": {"label": "Network & internet", "role": "ListItem", "bbox": [20, 140, 260, 185]}
  }
}
```

### 2.2 Window Scoping & Coordinate Abstraction
* **v1 Scope:** Lock capture and UIA queries to a target `HWND` (active or specified window) so Mirage's `0–1000` normalized grid maximizes pixel precision on small icons.
* **Future-Proofing:** Wrap all coordinate conversions in a `ViewportTransform(origin_x, origin_y, width, height)` class. Supporting full-desktop or multi-monitor capture in v2 only requires changing the viewport bounds from `GetWindowRect(hwnd)` to virtual screen metrics.

### 2.3 Event-Driven Watchdog (`watchdog.py`)
* **Active Mode:** Step-triggered sensing strictly before and after each actuation step.
* **Idle Mode:** Lightweight Win32 `SetWinEventHook` (window focus/creation events) + low-frequency downscaled pixel-diff checks to enable proactive, OpenClaw-style triggers (e.g., reacting to build-complete notifications or modal popups).

---

## 3. Control Loop, Jev Gating & Safety (`control/`)

### 3.1 Execution Modes

| Mode | Behavior | Safety Policy |
| :--- | :--- | :--- |
| **`assistant`** | General-purpose personal desktop automation. | Jev pauses execution and triggers a UI/tray confirmation prompt on `medium` or `high` risk actions (deletes, checkouts, OS settings, sending messages). |
| **`harness`** | Autonomous execution for CI, UI testing, or coding agents. | Runs without human prompts, strictly sandboxed to a whitelisted `HWND`, process name, or URL prefix (e.g., `localhost:*`). Out-of-bounds actions abort immediately. |

### 3.2 Dual-Gate Jev Verification (`jev_gate.py`)
1. **Pre-Action Gate:** Given `(sub_goal, proposed_tool_call, target_element_label)`, Jev classifies `risk_level` (`low | medium | high`) and `scope_valid` (`bool`).
2. **Post-Action Gate:** After actuation, Servo re-senses the window and computes a lightweight state diff (changed/added/removed element labels + title). Jev classifies `transition_ok` (`yes | no`) against the sub-goal's expected outcome.

### 3.3 2-Step Local Recovery Ladder
Before waking System 2:
1. **Attempt 1 (Fast Path):** UIA perception $\rightarrow$ Needle 3 tool call $\rightarrow$ Jev post-check.
2. **Attempt 2 (Visual Fallback):** If Attempt 1 fails (element not found or `transition_ok == False`), force a **Mirage (22 MB)** visual pass, rebuild `scene_index`, and retry Needle 3 once.
3. **Escalation:** If Attempt 2 fails, package a **compressed state diff** (failed sub-goal, last 2 actions, current element list, and cropped failure screenshot) and hand off to the pluggable System 2 backend.

---

## 4. Pluggable System 2 Interface (`planner/`)

System 2 is abstracted behind a minimal `PlannerBackend` protocol:

```python
from typing import Protocol

class PlannerBackend(Protocol):
    def decompose(self, goal: str, initial_scene: dict) -> list[SubGoal]: ...
    def recover(
        self,
        failed_subgoal: SubGoal,
        state_diff: dict,
        screenshot_b64: str | None,
    ) -> list[SubGoal]: ...
```

Supported adapters out of the box:
* **OpenAI-Compatible HTTP:** Connects to any cloud API or local server (Ollama, llama.cpp, vLLM).
* **Passthrough / External Agent (MCP Mode):** When an external coding agent drives Servo via MCP, Servo executes the requested atomic or multi-step flow locally and returns the structured trace directly to the caller as System 2.

---

## 5. Actuation & Kinematics (`actuation/`)

### 5.1 Needle 3 Tool Surface (`@cactus.tool`)
Keep the tool schema small, typed, and integer-indexed:
* `click(element_id: int, button: Literal["left", "right", "double"] = "left")`
* `type_text(element_id: int, text: str, press_enter: bool = False)`
* `hotkey(keys: list[str])`
* `scroll(direction: Literal["up", "down"], amount: int = 3)`
* `drag(source_id: int, target_id: int)`
* `wait(seconds: float)`

### 5.2 Deterministic Cursor Trajectory (`trajectory.py`)
* **Default (`fast_mode=False`):** Read live cursor via `win32api.GetCursorPos()`, compute Euclidean distance to the target element's pixel center, and interpolate `10–40` steps along a cubic ease-out curve ($1 - (1 - t)^3$) with `5ms` sleep intervals.
* **Fast Mode (`fast_mode=True`):** Direct `pywinauto.mouse.click(coords=(tx, ty))` for maximum harness throughput.

---

## 6. Runtime, Daemon & Resource Policy (`runtime/`)

* **Single-Process Daemon (`FastAPI` + `FastMCP`):**
  * Exposes an **MCP Server** (`run_task`, `assert_ui_state`, `get_scene_index`) for coding agents and external tools.
  * Exposes a local **REST/WebSocket API** powering a lightweight **PyWebView / System Tray UI** (live bounding-box overlay, Jev confirmation popups, trace inspector).
* **Startup Resource Probe (v1 Simple Policy):**
  * On boot, inspect CPU core count, available RAM, and free GPU VRAM (`pynvml` / `torch.cuda` if present).
  * Default System 1 (`Mirage`, `Jev`, `Needle 3`) to **CPU** with `cactus.set_threads()` tuned to physical cores—leaving 100% of GPU VRAM free for local System 2 models or apps under test. Expose a simple config flag (`device: auto | cpu | gpu`) for manual override.
* **Voice Hook (v1.5):** Reserve a clean task-queue ingestion endpoint so **Cactus Whistle (16.9 MB)** can be plugged in later as a push-to-talk trigger in `assistant` mode.

---

## 7. General-Purpose Output Contract & Telemetry

Every Servo execution returns a **Structured Trace Payload** and persists it to local SQLite + JSONL under `~/.servo/traces/`:

```json
{
  "run_id": "srv_20261120_164801",
  "mode": "harness",
  "status": "passed",
  "goal": "Submit login form with invalid password and verify error banner",
  "metrics": {
    "total_latency_ms": 642,
    "uia_steps": 3,
    "mirage_fallbacks": 1,
    "system2_escalations": 0
  },
  "steps": [
    {
      "step": 1,
      "sub_goal": "Type invalid password",
      "perception_source": "uia",
      "tool_call": "type_text(element_id=2, text='wrong_pass')",
      "jev_pre_risk": "low",
      "jev_post_ok": true,
      "latency_ms": 145
    }
  ],
  "failure_artifact": null
}
```

*(On failure, `failure_artifact` contains the failed step index, UIA/Mirage state diff, and a base64-encoded cropped screenshot of the target window.)*

---

## 8. Repository Layout & v1 Build Roadmap

Anchor at `C:\Users\ACER\noesis\servo\`:

```text
servo/
├── pyproject.toml
├── servo/
│   ├── config.py             # Modes (assistant/harness), allowlists, hardware probe
│   ├── perception/
│   │   ├── viewport.py       # HWND bounds + 0-1000 <-> pixel coordinate transforms
│   │   ├── uia_sensor.py     # pywinauto accessibility extractor
│   │   ├── mirage_sensor.py  # Cactus Mirage 22MB fallback wrapper
│   │   ├── scene_index.py    # Unified integer element_id map + state diffing
│   │   └── watchdog.py       # Win32 focus/event hooks for idle triggers
│   ├── control/
│   │   ├── jev_gate.py       # Pre-action risk & post-action transition classifier
│   │   └── loop.py           # Closed-loop state machine & 2-step recovery ladder
│   ├── actuation/
│   │   ├── trajectory.py     # Cubic ease-out kinematics + fast_mode
│   │   └── needle_tools.py   # @cactus.tool bindings for Needle 3
│   ├── planner/
│   │   ├── base.py           # PlannerBackend protocol
│   │   └── openai_compat.py  # Pluggable LLM adapter
│   ├── telemetry/
│   │   └── store.py          # SQLite + JSONL trace logger
│   └── daemon/
│       ├── server.py         # FastAPI + FastMCP unified server
│       └── ui.py             # System tray + lightweight PyWebView dashboard
└── tests/
    └── test_notepad_loop.py  # Deterministic local smoke test
```

### Phased v1 Build Order

1. **Milestone 1 — The Reflex Arc (Days 1–3):** Build `viewport.py`, `uia_sensor.py`, `mirage_sensor.py`, `scene_index.py`, `trajectory.py`, and `needle_tools.py`. Verify a single-step click + type in Notepad and a local Chrome tab with zero LLM calls.
2. **Milestone 2 — The Closed Loop (Days 4–6):** Wire `jev_gate.py` (pre-risk + post-verify) and `loop.py` with the 2-step `UIA -> Mirage -> Escalate` ladder and `assistant`/`harness` mode enforcement.
3. **Milestone 3 — Pluggable System 2 & Traces (Days 7–9):** Add `openai_compat.py` planner decomposition and `store.py` (SQLite + JSONL structured trace output).
4. **Milestone 4 — Daemon, MCP & Tray UI (Days 10–14):** Wrap the engine in `FastAPI` + `FastMCP`, add `watchdog.py`, and launch the lightweight PyWebView/Tray overlay for live inspection and confirmation popups.