# Servo

**Local-First, Closed-Loop Neuro-Symbolic Desktop Control Runtime for Windows 10 & 11**

Servo operates your Windows desktop from plain-language requests, fully on your own
machine (a 4 GB GPU helps, a CPU works):

1. **Laya** ([github.com/NandhaKishorM/laya](https://github.com/NandhaKishorM/laya)), a
   small decision model, routes each request (chat or desktop task, how complex, which
   area), rates each step's risk, and judges whether a step did what it should. A keyword
   and shell-command floor can only raise its risk level. Without a Laya server every step
   fails rather than running ungated.
2. **Planner agents** (apps, Windows Settings, files, web, code) write the steps, on a local
   Qwen3.5-4B with an optional cloud planner as step-up.
3. **Cactus Needle 3**, one agent per tool, turns each step into a tool call, which is
   checked against the step's own words before it runs.
4. **Perception and action**: UI Automation finds the control; Windows OCR and then the
   Qwen3.5-4B vision model find what UIA can't. Every step is verified after it runs.

How these fit together: [docs/COGNITIVE_CASCADE.md](docs/COGNITIVE_CASCADE.md).

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
* `pip install -e ".[cactus]"` — Cactus Needle 3 (on-device tool calling) and Mirage (screen descriptions), from `cactus-compute` >= 3.3. Required for desktop tasks.
* `pip install -e ".[ui]"` — pywebview for a frameless, always-on-top copilot window (otherwise it opens as an Edge app window).
* `pip install -e ".[anthropic]"` — Claude as the cloud planner.
* `pip install -e ".[dev]"` — pytest, and Playwright for the copilot UI tests.

Local models: Qwen3.5-4B served by `llama-server` (llama.cpp) is both the local planner and
the vision locator, and Laya runs its own server:

```powershell
llama-server -m Qwen3.5-4B-Q4_K_M.gguf --mmproj Qwen3.5-4B-mmproj-F16.gguf --port 8080 `
  -ngl 99 -c 8192 -fa on --jinja --alias qwen --chat-template-kwargs '{"enable_thinking":false}'
python decision_model/server.py serve      # Laya on :8009, in Laya's own environment
```

---

## How to Run

### 1. The copilot

```powershell
servo              # floating window, docked right, always on top
servo --no-window  # serve the UI only (prints the URL)
```

On first launch the copilot asks which reasoning model to use (Claude, Ollama on this PC,
Gemini, any OpenAI-compatible server, or none), tests the connection, and saves it to
`~/.servo/settings.json` with keys encrypted for your Windows account. The button at the
top right always shows what's active; click it to change.

Ask in plain language:
* *"open notepad and write hello world"*
* *"search youtube for kanye"*, *"open gmail in chrome"* (opens in **your** browser)
* *"increase the volume a bit"*, *"mute"*, *"next track"*
* *"switch to Untitled - Notepad"*, then *"press ctrl+s"* (acts on the window you were last using, never on the copilot)
* *"open notepad, write a haiku about rain and save it"* (multi-step: goes to System 2)
* *"dia is my default browser"* (remembered, not executed)
* *"why did that fail?"* (diagnoses the last failed run, down to the code)

Laya routes each request: a single direct action goes straight to Needle, anything else to
the planner agent for its area. UIA finds the target, then Windows OCR, then the vision
model, and Laya gates every action. Each run shows a **reasoning trace**: Laya's
probabilities, the planner agent with its prompt and reply, every Needle call and its
check, and every action and verification. Medium- and high-risk actions ask
for approval inside the copilot. See [docs/COGNITIVE_CASCADE.md](docs/COGNITIVE_CASCADE.md).

#### Honest results

Every step ends **confirmed** (the effect was observed, e.g. typed text read back from
the control), **not confirmed** (it ran but the effect isn't visible, like a click on a
playing video), or **failed** (the effect is provably absent). Unconfirmed steps don't
trigger blind retries; the reply names them ("Ran 3 steps, but I couldn't confirm…"),
and System 2 is told to check the screen. Launching never "succeeds" by focusing the
copilot, and closing an app that isn't open no longer launches it first.

#### Web pages in your browser

Web steps open a **new tab in your own browser**: the one you name in the request, else
the one the request just opened, else your saved preference (*"dia is my default
browser"*), else the Windows default. Servo types the address like a person would and
verifies it in the address bar, then drives the page through UI Automation like any
desktop app. Local and private-network addresses (where Servo's own copilot lives) are
refused unless your request names them.

#### Knowledge: recipes

[`knowledge/recipes/`](knowledge/recipes/) holds short, tested procedures (YouTube,
Spotify, Notepad, volume, brightness, battery and system facts, Settings pages, web
research to notes, sign-ins, files and folders, browser tabs). The best matches for a
request are injected into the planner's prompt. Add your own: drop a `.md` file with
`id`, `title` and `triggers` frontmatter; it's picked up on the next request.
Credentials pasted into a request are replaced with `[redacted]` before memory, traces
or any model see them; Servo never types passwords.

#### Runs, traces and self-diagnosis

Each run card shows the story first (how it was routed, the planner's thinking and the
recipes it used, an honest checklist, an outcome line with the reason for a failure)
and the full trace one click away. The **Runs** tab lists past runs; open one to replay
its trace, or press **Diagnose** on a failed one. Diagnosis reads the saved run record
and the code graph and points to the function that produced the error, with file and
line (also: *"why did that fail?"*, or `python -m servo.cognition.diagnose [run_id]`).
Full run records live in `~/.servo/traces/runs/` (newest 500).

#### Files and shell, without the GUI

For work that is really about files or commands, Servo skips the GUI:

* *"read file C:/Users/me/Desktop/notes.txt"*, *"list folder downloads"*
* *`write "buy milk" to file C:/Users/me/Desktop/todo.txt`* (or `append "…" to file …`)
* *`run command "Get-PSDrive C"`* (PowerShell)

The output shows in the reply, and the planner sees it next round, so a multi-step goal
can act on what a command printed. Only the files and code planner agents get file steps,
only the code agent may run commands, and they are gated:

| Action | Risk | Effect in assistant mode |
| :--- | :--- | :--- |
| Read a file, list a folder | low | runs |
| Read a credential store (`.ssh`, `*.pem`, browser `Login Data`…) | high | asks |
| Write a new file, append | medium | asks |
| Overwrite a file, write a script/executable, write into Startup | high | asks |
| Any shell command | medium | asks, showing the exact command |
| Destructive or download-and-run command (`del`, `rm -rf`, `Remove-Item`, `iex`…) | high | asks |

`~/.servo` (keys, chat memory, run records) is never readable or writable this way, because
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

### 2. MCP server for coding agents

```powershell
servo --mcp        # stdio
```

```json
{ "mcpServers": { "servo": { "command": "C:\Projects\servo\.venv\Scripts\servo.exe", "args": ["--mcp"] } } }
```

Tools:
* `windows()`: the open windows (title, process).
* `screen(window?)`: the numbered controls of the target window, the same description Servo's planners see.
* `step(text, window?)`: ONE step in Servo's step forms (`click [12]`, `type "hi" into [4]`, `press ctrl+s`,
  `open notepad`, `go to github.com`…), with numbers from the last `screen()`. The Needle agent for the
  step's tool runs it, `check_call` compares the call with the step, and Laya gates and verifies it.
* `do(goal, window?)`: Servo plans and runs the whole goal.

While the copilot is open, every MCP call is forwarded to it: it queues behind your own
requests, shows live in the copilot with its trace, and risky steps ask you there. Without
the copilot the call runs in the MCP process, and any action that needs your confirmation
is refused.

---

## Configuration

Configure runtime options via environment variables (prefix `SERVO_`):

| Variable | Default | Description |
| :--- | :--- | :--- |
| `SERVO_MODE` | `assistant` | `assistant` (prompts on medium/high risk) or `harness` (autonomous sandboxed CI). |
| `SERVO_CONFIRMATION_CHANNEL` | `dialog` | `dialog` (desktop Yes/No modal) or `deny` (automatically refuses risky actions). |
| `SERVO_WHITELISTED_PROCESS_NAMES` | `[]` | JSON array of permitted processes in harness mode (e.g., `'["notepad.exe"]'`). |
| `SERVO_PLANNER_MODE` | `local_first` | `local_first` (local planner, cloud step-up), `local_only`, `cloud_first` or `cloud_only`. |
| `SERVO_LOCAL_PLANNER_URL` | `http://127.0.0.1:8080/v1` | The local planner: llama-server with Qwen3.5-4B. |
| `SERVO_PLANNER_PROVIDER` | `openai` | Cloud planner: `openai` (any OpenAI-compatible server), `anthropic`, `gemini`, or `none`. |
| `SERVO_ANTHROPIC_MODEL` | `claude-opus-5-5` | Claude model for System 2; `SERVO_SYSTEM2_EFFORT` sets reasoning depth (default `medium`). |
| `SERVO_LAYA_URL` | `http://127.0.0.1:8009` | Laya decision server (`decision_model/server.py serve`). Without it every step fails. |
| `SERVO_NEEDLE_CONFIDENCE_THRESHOLD` | `0.7` | Minimum Needle confidence to act on a user's single request without a planner. Planner steps are checked by `check_call` instead. |
| `SERVO_TEXT_MATCH_THRESHOLD` | `0.72` | Minimum match between the target and text on screen (Windows OCR) to click it. |
| `SERVO_LOCATOR_URL` | `http://127.0.0.1:8080/v1` | Vision locator: the local Qwen3.5-4B server with its vision projector loaded. |
| `SERVO_MAX_REPLANS` | `4` | How many times a planner tier may re-plan after failures before stepping up to the cloud. |
| `SERVO_PREFERRED_BROWSER` | *(empty)* | `dia`, `chrome`, `edge`, `arc`, … Empty = the Windows default. Also set by saying *"X is my default browser"*. |
| `SERVO_HOME` | `~/.servo` | Where Servo keeps its token, settings, traces, memory, graph and schedule. |
| `SERVO_DIAGNOSE_SEND_CODE` | `false` | Let self-diagnosis send code snippets (not just function names) to the planner model. |
| `SERVO_OPENAI_BASE_URL` | *(empty)* | Base URL of the OpenAI-compatible cloud planner. |
| `SERVO_FAST_MODE` | `false` | When `true`, snaps cursor directly to targets (for automated CI). |
| `SERVO_SYSTEM_TOOLS` | `true` | Allow the gated file and shell commands (never available in harness mode). |
| `SERVO_COMMAND_TIMEOUT_SECONDS` | `120` | Kill a shell command (and its children) after this long. |
| `SERVO_SCHEDULE_FILE` | `~/.servo/schedule.json` | Where scheduled requests are saved. |

---

## Running Tests

Verify the installation against the full test suite:

```powershell
# Run unit and integration tests (mocked OS events)
pytest -v

# Run live Windows desktop tests (drives real windows like Notepad)
pytest -v -m desktop
```

Unit tests run in a private `SERVO_HOME` and never touch your real `~/.servo` or your browser.

### Real-desktop task suite

The pass rate that matters: real requests run on your desktop, each checked by an
independent probe (Notepad's actual text, Calculator's display, the browser's address
bar, WMI brightness, the battery level), never by Servo's own report. A task where Servo
said "done" but the probe disagrees is reported as **LIE**.

```powershell
python -m servo.evals.task_suite           # single-step tasks (Laya + Needle, no planner)
python -m servo.evals.task_suite --llm     # plus multi-step tasks (uses the planner)
python -m servo.evals.task_suite --only notepad_hello youtube_search
```

It moves the mouse, types, and opens apps and browser tabs, so run it when you're not
using the machine. Results are saved to `~/.servo/evals/`.

---

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
