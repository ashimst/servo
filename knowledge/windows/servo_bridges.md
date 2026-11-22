# Servo-to-Windows Architecture Bridges

This document establishes the explicit mappings between Windows OS and UI behaviors and the dedicated **Servo sub-systems** that sense, decide, and actuate upon them.

---

## Architecture Bridge Map

```
Windows Desktop Surface / OS Primitive
       │
       ├──► UIA Accessibility Tree ────────► servo.perception.uia_sensor (COM TreeWalker, Patterns)
       ├──► Pixel / Bitmap Rendering ─────► servo.perception.mirage_sensor (Cactus Mirage / WinRT OCR)
       ├──► Native Win32 Windowing ───────► servo.perception.windows (EnumWindows, SetForeground, DPI)
       ├──► Target Stability & Lifetime ──► servo.perception.watchdog + servo.control.settling
       ├──► Mouse / Keyboard Input ───────► servo.actuation.trajectory (Bezier curves, SendInput scancodes)
       ├──► Shell / Filesystem Actions ───► servo.actuation.system_tools (Gated PowerShell/CMD, file I/O)
       └──► Safety & Privilege Boundary ──► servo.control.jev_gate (Pre-action risk, Post-action verify)
```

---

## 1. Perception Bridges

### `servo.perception.uia_sensor`
* **Role:** High-speed, deterministic COM-based UI Automation tree walker (`CUIAutomation8`).
* **Bridges To:** Win32 standard controls, WPF XAML automation peers, Electron/Chromium accessibility DOMs.
* **Control Patterns Used:**
  - `IUIAutomationInvokePattern` (`click`, `button`)
  - `IUIAutomationValuePattern` (`type`, `set_text`)
  - `IUIAutomationTogglePattern` / `SelectionItemPattern` (`checkbox`, `radio`, `tab`)
  - `IUIAutomationTextPattern` (Rich text inspection)
* **Latency Budget:** < 30ms per query.

### `servo.perception.mirage_sensor`
* **Role:** Visual perception fallback for canvas controls, Direct3D/OpenGL views, owner-drawn controls, and dormant accessibility trees.
* **Bridges To:** On-device **Cactus Mirage** neural visual model and **Windows Media OCR (WinRT)**.
* **Behavior:** Takes window or viewport screenshot, extracts bounding boxes and text spans, computes Levenshtein fuzzy match scores against target labels.
* **Latency Budget:** < 120ms local CPU inference.

### `servo.perception.windows`
* **Role:** Win32 native API bindings for window lifecycle and coordinate systems.
* **Bridges To:**
  - `EnumWindows()` / `GetWindowTextW()`: Window discovery and targeting.
  - `GetWindowRect()` / `GetDpiForWindow()`: Per-Monitor DPI v2 physical-to-logical pixel transformations.
  - `SetForegroundWindow()` / `BringWindowToTop()`: Window focus restoration.
  - `IsIconic()` / `ShowWindow(SW_RESTORE)`: Minimized window recovery.

---

## 2. Control & Settling Bridges

### `servo.perception.watchdog`
* **Role:** Passive window state observer.
* **Bridges To:**
  - Detects if target `HWND` has crashed, closed, or become unresponsive (`IsHungAppWindow()`).
  - Emits telemetry events if target window changes unexpectedly.

### `servo.control.settling`
* **Role:** Adaptive UI settling observer.
* **Bridges To:** Post-action screen settling. Checks whether animations, page transitions, or dropdowns are still moving before dispatching subsequent inputs (`settling_base_ms` = 100ms, `settling_max_ms` = 750ms).

---

## 3. Kinematic Actuation Bridges

### `servo.actuation.trajectory`
* **Role:** Human-like, continuous kinematic trajectory generator for mouse and keyboard.
* **Bridges To:**
  - Win32 `SendInput()` API.
  - Generates Bezier curves with randomized micro-jitter and acceleration/deceleration to prevent anti-bot detection in modern apps and web views.
  - Synthetic keyboard events with realistic keydown/keyup dwell intervals and hardware scan codes.

### `servo.actuation.system_tools`
* **Role:** Direct operating system interaction bypassing GUI overhead.
* **Bridges To:**
  - Local file read/write (`read_file`, `write_file`).
  - Gated PowerShell and CMD execution (`run_command`).
  - Common Windows system settings URIs (`ms-settings:*`).

---

## 4. Safety & Policy Bridges

### `servo.control.jev_gate` (Kev Gate)
* **Role:** Non-autoregressive safety comparator and invariant validator.
* **Bridges To:**
  - Evaluates pre-action risk levels (`low`, `medium`, `high`).
  - Gating actions: High risk triggers user confirmation popups via Copilot UI.
  - Verifies post-action state mutations (`jev_post_ok`).

### `servo.actuation.sandboxing`
* **Role:** Process and window boundary enforcement.
* **Bridges To:**
  - Strictly limits actuation to `whitelisted_hwnds` and `whitelisted_process_names` when running in `harness` mode.
