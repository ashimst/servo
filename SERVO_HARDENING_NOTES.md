# Servo v0.2 Hardening Notes

This document records a set of safety and correctness fixes made after a code review of
Servo v2. For each change it states **what was wrong**, **what changed**, and **why**,
plus any behaviour change callers must adapt to. Where a fix departs from an ADR in
[SERVO_ARCHITECTURE_V1.md](SERVO_ARCHITECTURE_V1.md) or
[SERVO_ARCHITECTURE_V2.md](SERVO_ARCHITECTURE_V2.md), the departure is called out.

**Guiding principle:** Servo sends real mouse and keyboard input to a user's desktop.
When it is unsure what to act on, or whether an action is allowed, it must **stop and
report** rather than guess. Several defects below were "graceful fallbacks" that
turned uncertainty into real clicks on arbitrary targets.

---

## Breaking changes at a glance

| Area | Before | After |
|---|---|---|
| `POST /task`, MCP `run_task_tool`, voice | `hwnd` optional, defaulted to the foreground window | `hwnd` **required**; use `GET /windows` / `list_windows_tool` to find it |
| REST API | No authentication | `Authorization: Bearer <token>` required on everything except `/health` |
| `/health` | Returned hardware profile and config | Liveness only; details moved to authenticated `GET /status` |
| `mode` in requests | Accepted and silently ignored | Must match `SERVO_MODE` or the request is rejected (400) |
| Assistant mode, medium/high risk | Executed when no confirmation UI was wired up | A Yes/No dialog appears on the automated desktop; runs only on "Yes" (see §11) |
| Confirmation callback signature | `callback(sub_goal, pre_risk) -> bool` | `callback(ConfirmationRequest) -> bool` |
| `SERVO_TASK_TIMEOUT_SECONDS` default | 120 | 300 (a human may be answering dialogs) |
| Harness mode | No restriction | Requires `SERVO_WHITELISTED_HWNDS` or `SERVO_WHITELISTED_PROCESS_NAMES`; refuses otherwise |
| Planner / Mirage / voice failures | Invented a plan / regions / command and executed it | Raise an error; nothing is actuated |
| Out-of-bounds targets | Clamped to the window edge and clicked | Rejected (`SandboxViolationException`) |
| `SERVO_JEV_URL` default | `http://localhost:8000/v1/systemone` | empty (heuristic gate unless configured) |
| Anthropic planner | Raw HTTP, retired model `claude-3-5-sonnet-20241022` | Official SDK (`pip install servo[anthropic]`), default `claude-opus-5-5` |
| Tests | `pytest` ran the Notepad test (real mouse/keyboard) | Desktop tests are opt-in: `pytest -m desktop` |

---

## 1. The main task path executed arbitrary clicks

**Problem.** `POST /task` never called a planner. It passed the whole goal sentence
(for example "click the save button") as the label to search for. That almost never
matches a control's label, and `_run_reflex_pass` then fell back to the *first element
in the scene* and clicked it. For "type" goals, `text_to_type` was never set, so an
empty string was typed. `planner_provider` and every planner adapter were dead code.

**Change.**
- New `run_plan` worker job ([worker.py](servo/daemon/worker.py)): sense, then
  `get_planner().decompose()`, then run each sub-goal through the control loop. It stops at
  the first failure, and the planner gets one chance to `recover()`.
- `get_planner()` factory ([planner/__init__.py](servo/planner/__init__.py)) honours
  `SERVO_PLANNER_PROVIDER`. MCP's `run_task_tool` uses the `passthrough` planner, because
  the calling agent *is* System 2 (ADR-10).
- `ServoControlLoop.resolve_target()` ([loop.py](servo/control/loop.py)) accepts an exact
  label match or a **unique** partial match. Anything else returns `None`, and the step
  fails and escalates. The "first element" fallback is gone.

**Why.** An automation tool that clicks *something* when it cannot find the thing you
asked for does damage that is hard to see and undo. Failing loudly costs a retry;
guessing can cost a deleted file or a sent message.

## 2. Synthetic fallbacks were executed as real input

**Problem.** Three components produced made-up data when a dependency was missing, and
the control loop acted on it:
- `MirageSensor` returned three fixed regions ("Visual Area Center", …) without Cactus.
- Planner adapters returned "click `labels[0]`" on *any* error (missing key, network,
  bad JSON).
- `VoiceIngestionManager` returned the hard-coded command "Click search settings and
  type bluetooth", or a phrase built from the file name.

**Change.** Each component now raises (`PlannerError`, `VoiceUnavailableError`) or
returns an empty scene flagged with `sense_error`. Planners validate model output
through a shared `parse_subgoals()` ([base.py](servo/planner/base.py)) and raise on
malformed or empty plans. `recover()` returns `[]`: the old stubs re-issued the same
failed action, which is a second click on something that already failed verification.

**Why.** Fallback data is fine for display and tests, but never as input to an
actuator. Tests that relied on the synthetic output were rewritten to assert the error.

## 3. Safety controls were fail-open

**Problem.**
- `confirmation_callback` was never passed by the worker, and a missing callback meant
  "proceed", so medium- and high-risk actions ran unconfirmed in assistant mode.
- The `mode` field on requests was ignored; only the global setting applied.
- `whitelisted_hwnds` / `whitelisted_process_names` were defined in config but never read.
  Harness mode was identical to assistant mode minus the (already non-functional) prompt.
- Jev's `scope_valid` was computed and never checked.

**Change.**
- In assistant mode, a medium/high-risk action is **refused** unless a callback exists
  and returns `True`. Callback exceptions count as rejection.
- `check_target_allowed()` ([sandboxing.py](servo/actuation/sandboxing.py)) enforces the
  allowlists in harness mode. **With no allowlist configured, harness mode refuses to
  act**: "no allowlist" must not mean "the whole desktop".
- `scope_valid == False` aborts the step.
- The mode comes only from `SERVO_MODE`. A request that names a different mode is
  rejected; letting a client choose `harness` would let any caller disable confirmations.

**Why.** Each check silently did nothing when its input was missing. Safety checks have
to fail closed, or they only protect the configurations nobody forgets.

The confirmation channel itself is described in §11.

## 4. Actions could run twice

**Problem.** If attempt 1 (UIA) actuated but post-verification saw no change, attempt 2
(Mirage) actuated again, after a fresh risk check and, potentially, a second approval.

**Change.** `ExecutionResult.actuated` tracks whether input reached the OS. The ladder
now moves to Mirage **only when UIA could not resolve the target**. Once anything has
been actuated, a failed verification escalates instead of retrying. Actuation exceptions
are also treated as "possibly actuated", so they escalate rather than retry.

**ADR departure (ADR-01 / ADR-09).** ADR-01 also routed windows with fewer than 3 UIA
controls straight to Mirage. That rule is removed: a small dialog with a perfectly
resolvable "OK" button was being sent to the visual fallback. UIA is now used whenever
it can resolve the target.

**Why.** "Retry on failed verification" is only safe for idempotent actions, and
clicks and typing generally are not. Typing the same text twice or pressing "Send" twice
is worse than reporting an unverified step.

## 5. Risk heuristic and post-action verification

**Problem.** Keywords were matched as substrings: "More inform**ation**" (`format`),
"B**order**", "Re**send**" were all rated *high*. Iteration over `set`s made the reported
reason nondeterministic. Post-verification treated *any* UI change as success with 0.92
confidence and never looked at `expected_outcome`; its second branch was unreachable. A
malformed model response defaulted to `risk_level="low"`.

**Change** ([jev_gate.py](servo/control/jev_gate.py)).
- Keywords match at word starts with suffixes allowed (`format` matches "formatting" but
  not "information"); the lists are ordered tuples.
- For `type` actions, success requires the typed text to appear in an observed value
  change. If the target exposes no readable value, the step is accepted with low
  confidence (0.5) and labelled unverified.
- For clicks, a change that mentions words from `expected_outcome` gets 0.85; an
  unrelated change gets 0.55 and is labelled unverified.
- Malformed model answers fall back to the heuristic instead of defaulting to "low".
- The default `SERVO_JEV_URL` is now empty. It used to point at port 8000, which is
  Servo's own daemon port.

**Why.** Confidence numbers feed decisions and traces. They should reflect what was
actually checked.

## 6. Sandbox and actuation correctness

| Problem | Change | Why |
|---|---|---|
| Out-of-bounds targets were **clamped** to the window edge | `validate_target()` raises | Clamping turns "click X" into "click whatever is at the edge", a different action from the one that was gated. Spec: "Out-of-bounds actions abort immediately." |
| A physical click lands on whatever window is on top | `point_hits_target()` hit-tests with `WindowFromPoint`; if covered, use UIA Invoke or fail | Coordinates inside the target's rectangle can still belong to an overlapping app. |
| `SetForegroundWindow` failures were ignored before typing | `ensure_target_in_foreground()` verifies and raises | Synthetic keystrokes go to the foreground window, so if activation fails they go to another app. |
| UIA fallbacks used the *first* control containing the point | `find_innermost_control()` picks the smallest | `descendants()` is parent-first, so "first" was usually a container pane. |
| Tools returned "Clicked…/Typed…" even when every method failed | Tools raise `ActuationError` | The loop and traces must know when nothing happened. |
| `send_keys` only escaped `{}` | All of `+^%~(){}[]` escaped | Typing "a+b" sent Shift+B; "50%" sent an Alt chord. |
| Hotkey blacklist bypassed by aliases or extra keys ("control+alt+delete", "shift+alt+f4"); `win` mapped to an invalid `{WIN}` | Alias normalisation; a combo is blocked if it *contains* a blocked set; any combo with the Windows key is blocked | Win shortcuts act on the OS shell (Win+R launches arbitrary commands), never on the target window. |
| Corner-slam failsafe skipped in `fast_mode` and only fired on mid-move slams | Checked before every move; any cursor in the corner aborts | The operator's physical abort must work in the mode most likely to run unattended. |
| `scroll` ignored which window was under the cursor | Takes an element and hit-tests | Wheel events go to the window under the cursor. |

## 7. Daemon, auth, and worker

- **Bearer token** ([auth.py](servo/daemon/auth.py)). Binding to `127.0.0.1` keeps out
  remote hosts but not local processes or browser pages (DNS rebinding, pages served
  from the daemon's own origin). The token is read from `SERVO_API_TOKEN`, or generated
  once into `~/.servo/api_token`. MCP runs over stdio and is reachable only by the
  process that launched it, so it needs no token.
- **Explicit HWND for actions.** Over MCP/REST, the foreground window is usually the
  caller's own terminal or IDE. Read-only `get_scene` still defaults to the foreground.
  New `GET /windows` and `list_windows_tool` support discovery.
- **SVG overlay injection.** Labels come from arbitrary app/web content and were written
  unescaped into SVG served from the daemon's origin. They are now `html.escape`d, and
  the response carries `Content-Security-Policy: default-src 'none'`.
- **Timeout cancellation.** A timed-out job used to keep running and could act after
  the caller had been told it failed. It is now marked cancelled: a queued job is skipped,
  and a running plan stops before its next sub-goal. An individual action already in
  progress cannot be interrupted.
- **Version** unified at 0.2.0 (`pyproject.toml`, `servo.__version__`, API).

## 8. Perception and telemetry

- **Sensing errors.** `UIASensor` used to write `"Error: …"` into `window_title`, so the
  next diff saw "title changed" and the post-gate counted a COM failure as success.
  Scenes now carry `sense_error`, and `SceneDelta.compute_diff` reports no change when
  either side has one.
- **Duplicate labels.** Labels are diffed as multisets, and values are keyed by
  `label`, `label#2`, …, so two "Name" fields no longer collapse into one.
- **Values for Document/ComboBox controls** are read, so typing into editors
  (e.g. Notepad's document) can be verified.
- **Trace IDs** had 1-second resolution and used `INSERT OR REPLACE`, so concurrent runs
  overwrote each other. IDs now include a random suffix and use a plain `INSERT`.
- **Trace content** is built from the real step logs. Previously every run was recorded
  as one `uia` step with `jev_pre_risk="low"`.
- `SettlingEngine` and `JevGate` used `x or default`, which turned an explicit `0` into the
  default. They now use `is None`.

## 9. Planner adapters

- Shared prompt (`build_planning_prompt`) lists real element labels and the supported
  actions (`click`, `type`), and allows an empty plan when the goal is not achievable.
- **Anthropic** uses the official `anthropic` SDK (optional extra, `>=2.12.0`), defaults
  to `claude-opus-5-5` (override with `SERVO_ANTHROPIC_MODEL`), enables the server-side
  refusal fallback (`fallbacks="default"`), and handles `refusal` / `max_tokens` stop
  reasons explicitly.
- **Gemini** sends the API key in the `x-goog-api-key` header instead of the URL query,
  where it could leak into logs and exception messages.
- Unsupported `action_type`s (`hotkey`, `scroll`, …) are rejected by the loop instead of
  silently becoming clicks.

## 10. Packaging and tests

- `pytest` was removed from runtime dependencies (it is in `[dev]`). New `[anthropic]` extra.
- The Notepad end-to-end test launches an app and sends real input. It is marked
  `desktop` and excluded by default (`addopts = -m 'not desktop'`); run it explicitly with
  `pytest -m desktop` on a machine you are not using.
- New regression tests cover each fix above: arbitrary-click fallback, double actuation,
  fail-closed confirmation, harness allowlist, keyword false positives, typed-text
  verification, hotkey bypasses, `send_keys` escaping, out-of-bounds rejection, SVG
  escaping, duplicate-label diffs, sensing-error diffs, unique trace IDs, auth, explicit
  HWND, mode override, and timed-out job cancellation.

## 11. Confirmation channel for assistant mode

**Problem.** After §3 made confirmation fail closed, nothing could supply a "yes": the
worker had no confirmation channel, so in assistant mode every medium/high-risk action
from REST, MCP, or voice was refused. Assistant mode could not save a file or press Submit.

**Change.** New [confirmation.py](servo/control/confirmation.py), wired in by the worker
for every run:

- **`SERVO_CONFIRMATION_CHANNEL=dialog`** (default) shows a native, topmost Windows
  Yes/No dialog on the desktop being automated. **`deny`** refuses every such action, for
  headless or unattended deployments that should never wait on a human.
- The dialog shows the risk level and reason, the **exact tool call** (including the
  text to type), the element label, and the target window title and process.
- **"No" is the default button.** The dialog takes focus when it appears; a user typing in
  another app could otherwise approve it with a stray Enter.
- **No answer within `SERVO_CONFIRMATION_TIMEOUT_SECONDS` (60 s) means "No".** It uses
  `MessageBoxTimeoutW`. That function is undocumented but has been in user32 since
  Windows XP and is present on current builds. If it is missing, Servo refuses rather
  than showing a dialog with no timeout, which could block the worker forever.
- If the dialog cannot be displayed (service session, locked desktop, non-Windows), the
  action is refused.
- The callback receives a `ConfirmationRequest` instead of `(sub_goal, pre_risk)`, so any
  channel has the full context needed to decide.
- **Late approvals are ignored.** The loop checks `should_abort` *after* the human answers
  and *before* actuating. If the caller timed out while the dialog was open, a later
  "Yes" does not act on a run that was already reported as failed. The default
  `task_timeout_seconds` was raised from 120 s to 300 s to leave room for a person to answer.
- Each step's trace records `confirmation` (`approved` / `rejected` / empty), and
  existing SQLite databases get the new column automatically.
- Unit tests stub the Win32 call and force `deny` through an autouse fixture, so the test
  suite can never pop a real dialog.

**Why this design.** Assistant mode means a person is at the desktop being automated,
so that desktop is where the decision belongs. A native dialog needs no extra
dependency, works whether the request came from REST, MCP, or voice, and blocks only
the automation worker, which is serialized anyway. Making the remote caller (often
an AI agent) approve its own action would defeat the purpose.

**Limitations.**
- Another process running as the same user could send input to the dialog. The dialog
  protects against Servo acting without consent, not against malware already running on
  the machine.
- While a dialog is open the worker is blocked, so other requests queue behind it, and
  `/scene` calls may time out.
- One dialog per risky step: a plan with several medium-risk steps asks several times.

## Not addressed

- **Element IDs are positional** and reassigned on every scan. This is safe inside one
  loop pass (scene and actuation use the same snapshot), but clients must not reuse IDs
  across calls. The MCP surface works with labels for this reason.
- **URL-prefix allowlisting** for harness mode (mentioned in the technical spec) is not
  implemented; only HWND and process-name allowlists are.
- An action already executing cannot be interrupted by a timeout; only subsequent steps
  are cancelled.
