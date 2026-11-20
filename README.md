# Servo

**Local-First, Closed-Loop Neuro-Symbolic Desktop Control Runtime for Windows 10/11**

Servo is a low-latency, sub-70MB desktop automation runtime built around a three-tier cybernetic servomechanism:
1. **System 2 (Pluggable Setpoint Planner):** Decomposes high-level instructions into sub-goals without handling raw screenshots or pixel coordinates.
2. **Safety Comparator (Jev Gate):** Non-autoregressive decision model validating pre-action risk and post-action UI state transitions.
3. **System 1 Reflex Arc (UIA + Mirage + Needle 3):** High-frequency, deterministic perception and kinematics running on CPU.

## Architecture

- [SERVO_ARCHITECTURE_V1.md](SERVO_ARCHITECTURE_V1.md): core architecture and decision records
- [SERVO_ARCHITECTURE_V2.md](SERVO_ARCHITECTURE_V2.md): voice, HUD overlay, MCP + REST surfaces
- [SERVO_HARDENING_NOTES.md](SERVO_HARDENING_NOTES.md): v0.2 safety/correctness fixes, the reasons for them, and breaking changes

## Installation

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev,mcp]"          # add ,anthropic or ,cactus as needed
```

## Running

```bash
servo                 # REST API on 127.0.0.1:8000
servo --mcp           # MCP server over stdio
```

The REST API requires `Authorization: Bearer <token>` on every endpoint except `/health`.
The token is read from `SERVO_API_TOKEN`, or generated on first start and stored in
`~/.servo/api_token`.

Actions always target an explicit window: call `GET /windows` (or the MCP
`list_windows_tool`) and pass its `hwnd`.

Key settings (environment variables, prefix `SERVO_`):

| Variable | Default | Meaning |
|---|---|---|
| `SERVO_MODE` | `assistant` | `assistant`: medium/high-risk actions need confirmation. `harness`: no prompts, but only on allowlisted windows |
| `SERVO_CONFIRMATION_CHANNEL` | `dialog` | `dialog`: Yes/No dialog on the desktop (default No; no answer = No). `deny`: refuse all medium/high-risk actions |
| `SERVO_CONFIRMATION_TIMEOUT_SECONDS` | `60` | How long the dialog waits before counting as "No" |
| `SERVO_WHITELISTED_HWNDS` / `SERVO_WHITELISTED_PROCESS_NAMES` | empty | Harness-mode allowlist (JSON list, e.g. `'["notepad.exe"]'`); harness mode refuses to act if both are empty |
| `SERVO_PLANNER_PROVIDER` | `openai` | `openai` (any OpenAI-compatible endpoint, e.g. Ollama), `anthropic`, `gemini`, `passthrough` |
| `SERVO_JEV_URL` | empty | Jev/Kev decision model endpoint; heuristic gate is used when empty |

## Running Tests

```bash
pytest                # unit tests (no real input is sent)
pytest -m desktop     # end-to-end tests that launch apps and drive the real mouse/keyboard
```
