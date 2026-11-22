# Playbook: Windows Edge Cases & Timing Quirks

This playbook documents specific physical edge cases encountered in desktop automation and how Servo handles them reliably.

---

## 1. Application Launch Latency & Splash Screens

### The Problem
* Launching modern desktop applications (Visual Studio, Photoshop, LibreOffice, Steam) takes 1 to 10 seconds.
* During launch, a transient splash screen appears (`WS_EX_TOOLWINDOW` or borderless `#32770`), then disappears before the main application window is created and interactive.
* Dispatching clicks while the splash screen is up either drops clicks or sends them into the desktop background.

### Servo Handling
* **Two-Stage Launch Verification:**
  1. `servo.daemon.copilot.resolve_app_executable()` launches the process.
  2. `servo.perception.windows` waits for the main application window (`WS_VISIBLE`, non-zero client rect, non-splash class).
  3. `servo.control.settling` ensures CPU and UI message queues have settled for at least `settling_base_ms` before issuing the first input.

---

## 2. Multi-Monitor Coordinate Spaces & Per-Monitor DPI v2

### The Problem
* Multiple monitors can have different DPI scaling factors (e.g. 100% on a 1080p monitor, 150% on a 4K display).
* Mouse coordinates sent via `SendInput()` must match the physical pixel or normalized mouse space depending on API mode.
* Minimized windows restored on secondary monitors may have negative X or Y coordinates.

### Servo Handling
* Servo initializes Per-Monitor DPI Aware v2 during boot (`init_windows_dpi_awareness()` in `servo/config.py`).
* All window rects are queried in physical desktop pixels and transformed via `GetDpiForWindow()`.

---

## 3. Chromium / Electron Dormant Accessibility Trees

### The Problem
* Chrome, Edge, VS Code, Slack, and Discord lazily initialize their UIA tree. On first launch or after prolonged idle, UIA returns 0 elements.

### Servo Handling
* Servo pings the top-level window with a UIA wake event (`UIA_InvokePattern` or focus event).
* If UIA remains unpopulated after 50ms, the reflex loop automatically falls sideways to **Mirage OCR / Cactus Needle 3** to find visual buttons without waiting for Chromium's renderer to build its tree.

---

## 4. Virtualized Lists (WPF, WinUI, Web)

### The Problem
* Data tables, file lists, and message histories only render DOM/UIA nodes for currently visible items.
* Querying for an element that is row 100 of a 500-item table returns `element_not_found`.

### Servo Handling
* System 2 recognizes virtualized containers. If the desired item is not in the visible list, System 2 emits `scroll down` or `press pagedown` to bring subsequent batches into the viewport before searching again.
