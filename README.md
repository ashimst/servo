# Servo

**Local-First, Closed-Loop Neuro-Symbolic Desktop Control Runtime for Windows 10 & 11**

Servo is an ultra-low-latency desktop automation engine built around a three-tier cybernetic servomechanism:

1. **System 2 (Setpoint Planner):** Breaks high-level user instructions into atomic sub-goals without needing raw screenshots or pixel coordinates. Supports OpenAI-compatible endpoints, Anthropic Claude, Google Gemini, and direct MCP callers.
2. **Safety Comparator (Jev Gate):** Non-autoregressive decision model validating pre-action risk levels (`low`, `medium`, `high`) and verifying post-action UI transitions.
3. **System 1 Reflex Arc (UIA + Mirage + Needle 3):** High-frequency, deterministic perception and kinematics running on CPU within a strict memory budget.

---

## Architecture Specifications

* [`SERVO_ARCHITECTURE_V1.md`](SERVO_ARCHITECTURE_V1.md) — Baseline architecture specification and Architectural Decision Records (ADRs).
* [`SERVO_ARCHITECTURE_V2.md`](SERVO_ARCHITECTURE_V2.md) — Voice ingestion, transparent HUD overlay, and dual FastMCP/REST protocols.
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
* `pip install -e ".[cactus]"` — Adds local Cactus edge models (Needle, Mirage, Whistle).
* `pip install -e ".[dev]"` — Installs pytest test runner.

---

## How to Run

### 1. Run as a Model Context Protocol (MCP) Server

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

### 2. Run as a Local REST Server

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
| `SERVO_PLANNER_PROVIDER` | `openai` | System 2 backend: `openai`, `anthropic`, `gemini`, or `passthrough`. |
| `SERVO_OPENAI_BASE_URL` | `http://localhost:11434/v1` | URL for local Ollama or OpenAI-compatible service. |
| `SERVO_FAST_MODE` | `false` | When `true`, snaps cursor directly to targets (for automated CI). |

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
