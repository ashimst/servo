# Servo

**Local-First, Closed-Loop Neuro-Symbolic Desktop Control Runtime for Windows 10 & 11**

Servo is an ultra-low-latency desktop automation engine built around a three-tier cybernetic servomechanism:

1. **System 2 (Setpoint Planner):** Breaks high-level user instructions into atomic sub-goals without needing raw screenshots or pixel coordinates. Supports OpenAI-compatible endpoints, Anthropic Claude, Google Gemini, and direct MCP callers.
2. **Safety Comparator (Kev Gate):** Non-autoregressive decision model ([Kev](https://github.com/jaredpalmer/kev), the open implementation of TypeSafe's Jev API) validating pre-action risk levels (`low`, `medium`, `high`) and verifying post-action UI transitions. Without a Kev server, a deterministic keyword and shell-command floor gates every action.
3. **System 1 Reflex Arc (UIA + Mirage + Needle 3):** High-frequency, deterministic perception and kinematics running on CPU within a strict memory budget.

---

## Architecture Specifications

* [`SERVO_ARCHITECTURE_V1.md`](SERVO_ARCHITECTURE_V1.md) — Baseline architecture specification and Architectural Decision Records (ADRs).
* [`SERVO_ARCHITECTURE_V2.md`](SERVO_ARCHITECTURE_V2.md) — Voice ingestion, transparent HUD overlay, and dual FastMCP/REST protocols.
* [`SERVO_ARCHITECTURE_V3.md`](SERVO_ARCHITECTURE_V3.md) — System 1 / System 2 Cognitive Handoff Architecture, Structured Tool Calling, and Action/Element Alias Mapping.
* [`SERVO_HARDENING_NOTES.md`](SERVO_HARDENING_NOTES.md) — Safety verification, confirmation gates, and edge-case handling.

---

## Getting Started

### Prerequisites

* **Operating System:** Windows 10 or Windows 11 (64-bit)
* **Python:** Python 3.10 to 3.12

### Installation

Clone the repository and set up a virtual environment:

```powershell
git clone https://github.com/ashimst/servo.git
cd servo

python -m venv .venv
.\.venv\Scripts\activate

# Install core runtime with MCP support
pip install -e ".[dev,mcp]"
```

Optional extras:
* `pip install -e ".[cactus]"` — Cactus Needle 3 (on-device tool calling) and Mirage (vision fallback), from `cactus-compute` >= 3.3.
* `pip install -e ".[browser]"` — Playwright browser tier. Uses your installed Edge/Chrome; run `playwright install chromium` only if you want the bundled build.
* `pip install -e ".[ui]"` — pywebview for a frameless, always-on-top copilot window (otherwise it opens as an Edge app window).
* `pip install -e ".[anthropic]"` — Claude as System 2.
* `pip install -e ".[dev]"` — pytest and Playwright for the test suite.

---

## How to Run

### 1. Floating On-Screen Desktop Copilot (Recommended)

```powershell
servo --copilot            # floating window, docked right, always on top
servo --copilot --no-window   # serve the UI only (prints the URL)
```

On first launch the copilot asks which reasoning model to use (Claude, Ollama on this PC,
Gemini, any OpenAI-compatible server, or none), tests the connection, and saves it to
`~/.servo/settings.json` with keys encrypted for your Windows account. The button at the
top right always shows what's active; click it to change. Direct commands work without
any model.

Ask in plain language:
* *"open notepad and write hello world"*
* *"search the web for weather in Kathmandu"* (Playwright drives the browser)
* *"click Save"* (acts on the window you were last using, never on the copilot)
* *"open notepad, write a haiku about rain and save it"* (multi-step: goes to System 2)

Requests run through a cost-and-latency cascade: the command grammar and Cactus Needle
handle direct commands on-device, UIA or the browser DOM find the target, Mirage looks at
the pixels when the accessibility tree has nothing, Kev gates every action, and only
requests that need reasoning reach the LLM. Each run shows a **reasoning trace**: which
tier handled what and why, confidences, Kev's verdicts, the exact LLM prompt and its
visible reasoning, and every action and verification. Medium- and high-risk actions ask
for approval inside the copilot. See [docs/COGNITIVE_CASCADE.md](docs/COGNITIVE_CASCADE.md).

#### Files and shell, without the GUI

For work that is really about files or commands, Servo skips the GUI:

* *"read file desktop\notes.txt"*, *"list folder downloads"*
* *`write "buy milk" to file desktop\todo.txt`* (or `append "…" to file …`)
* *`run command "Get-PSDrive C"`* (PowerShell; `run cmd command "…"` for cmd)

The output shows in the reply, and System 2 sees it next round, so a multi-step goal
can act on what a command printed. These commands are parsed only by the command
grammar (the on-device model never proposes them), and they are gated:

| Action | Risk | Effect in assistant mode |
| :--- | :--- | :--- |
| Read a file, list a folder | low | runs |
| Read a credential store (`.ssh`, `*.pem`, browser `Login Data`…) | high | asks |
| Write a new file, append | medium | asks |
| Overwrite a file, write a script/executable, write into Startup | high | asks |
| Any shell command | medium | asks, showing the exact command |
| Destructive or download-and-run command (`del`, `rm -rf`, `Remove-Item`, `iex`…) | high | asks |

`~/.servo` (API token, keys, chat memory) is never readable or writable this way, because
file contents go to the System 2 provider. Commands time out after
`SERVO_COMMAND_TIMEOUT_SECONDS`, and Stop kills the command and everything it started.
System tools are disabled in harness mode (it runs without confirmation) and can be
turned off with `SERVO_SYSTEM_TOOLS=false`.

#### Queue and schedule

* Press Enter while a request is running and the new one **queues** behind it
  (or say *"after this, …"*).
* *"in 10 minutes …"*, *"at 15:30 …"*, *"tomorrow at 9am …"*, *"every day at 9:00 …"*,
  *"every 30 minutes …"* schedule a request.
* *"list scheduled tasks"*, *"cancel ab12cd"*, *"clear the schedule"* manage them.

Scheduled requests run through the same cascade and gate, but only while the copilot
is running (this is not the Windows Task Scheduler). A job that came due while Servo
was off is not run late: one-shot jobs are dropped, recurring ones skip ahead. If a
scheduled run reaches a risky step and nobody answers, it stops.

---

### 2. Run as a Model Context Protocol (MCP) Server

Connect your favorite coding agent (Cursor, Claude Desktop, Antigravity) directly to Servo over stdio:

```powershell
servo --mcp
```

#### Claude Desktop Configuration (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "servo": {
      "command": "C:\\Projects\\servo\\.venv\\Scripts\\servo.exe",
      "args": ["--mcp"]
    }
  }
}
```

Exposed MCP Tools:
* `list_windows_tool`: Discovers interactive open windows and their `hwnd`.
* `get_scene_index_tool(hwnd)`: Extracts visible UI elements with normalized integer IDs.
* `run_task_tool(goal, hwnd, mode)`: Executes an automation step through the closed loop.
* `assert_ui_state_tool(expected, hwnd)`: Checks UI state against expectations.

---

### 3. Run as a Local REST Server

Launch the FastAPI daemon with local bearer authentication:

```powershell
servo --port 8000
```

By default, Servo listens on `127.0.0.1:8000`. On first launch, a bearer token is generated and stored in `~/.servo/api_token`. Pass this token in the header:
```http
Authorization: Bearer <your-token>
```

#### Core Endpoints:
* `GET /health` — Health check and hardware resource profile (public).
* `GET /windows` — List active visible windows with HWND and process names.
* `GET /scene?hwnd=<id>` — Query integer-indexed control tree for a target window.
* `POST /task` — Execute a goal on a window with safety gating.
* `GET /hud/overlay?hwnd=<id>` — Returns real-time SVG visual bounding boxes.
* `GET /traces` — Query SQLite telemetry log of recent runs.

---

## Configuration

Configure runtime options via environment variables (prefix `SERVO_`):

| Variable | Default | Description |
| :--- | :--- | :--- |
| `SERVO_MODE` | `assistant` | `assistant` (prompts on medium/high risk) or `harness` (autonomous sandboxed CI). |
| `SERVO_CONFIRMATION_CHANNEL` | `dialog` | `dialog` (desktop Yes/No modal) or `deny` (automatically refuses risky actions). |
| `SERVO_WHITELISTED_PROCESS_NAMES` | `[]` | JSON array of permitted processes in harness mode (e.g., `'["notepad.exe"]'`). |
| `SERVO_PLANNER_PROVIDER` | `openai` | System 2 backend: `openai` (Ollama or any OpenAI-compatible server), `anthropic`, `gemini`, `cactus` (local cactus-engine), or `none`. |
| `SERVO_ANTHROPIC_MODEL` | `claude-opus-5-5` | Claude model for System 2; `SERVO_SYSTEM2_EFFORT` sets reasoning depth (default `medium`). |
| `SERVO_KEV_URL` | *(empty)* | Kev server base URL (e.g. `http://127.0.0.1:8009`). Empty = heuristic gate. |
| `SERVO_NEEDLE_CONFIDENCE_THRESHOLD` | `0.7` | Minimum Needle confidence to act without System 2. |
| `SERVO_MIRAGE_MATCH_THRESHOLD` | `0.72` | Minimum Mirage label match to click a visual target. |
| `SERVO_MAX_REPLANS` | `2` | How many times System 2 may re-plan after a failure. |
| `SERVO_BROWSER_CHANNEL` | `msedge` | Browser for the Playwright tier: `msedge`, `chrome`, or `chromium`. |
| `SERVO_OPENAI_BASE_URL` | `http://localhost:11434/v1` | URL for local Ollama or OpenAI-compatible service. |
| `SERVO_FAST_MODE` | `false` | When `true`, snaps cursor directly to targets (for automated CI). |
| `SERVO_SYSTEM_TOOLS` | `true` | Allow the gated file and shell commands (never available in harness mode). |
| `SERVO_COMMAND_TIMEOUT_SECONDS` | `120` | Kill a shell command (and its children) after this long. |
| `SERVO_SCHEDULE_FILE` | `~/.servo/schedule.json` | Where scheduled requests are saved. |
| `SERVO_ALLOWED_HOSTS` | `[]` | Extra `Host` names the REST server accepts besides loopback, e.g. `'["192.168.1.20"]'` when binding to a LAN address. Requests with any other `Host` get 403 (DNS-rebinding guard). |

---

## Running Tests

Verify the installation against the full test suite:

```powershell
# Run unit and integration tests (mocked OS events)
pytest -v

# Run live Windows desktop tests (drives real windows like Notepad)
pytest -v -m desktop
```

---

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
