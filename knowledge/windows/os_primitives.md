# Windows OS Primitives Ontology

This document models the fundamental Windows operating system abstractions that Servo interacts with, detailing rules, constraints, and operational invariants for desktop automation.

---

## 1. Window Management (`HWND`), Focus & Z-Order Rules

### Window Handles (`HWND`)
* An `HWND` is an opaque pointer identifying a Win32 window object.
* Top-level windows belong to a desktop and can be enumerated via `EnumWindows()`.
* Window state includes: `WS_VISIBLE`, `WS_MINIMIZE`, `WS_MAXIMIZE`, and `WS_EX_TOPMOST`.

### Focus Restrictions (Foreground Lock)
* Windows enforces the **Foreground Activation Lock** (`LockSetForegroundWindow` / `SPI_SETFOREGROUNDLOCKTIMEOUT`).
* A background process cannot arbitrarily call `SetForegroundWindow(hwnd)` unless:
  1. The calling process is the foreground process.
  2. The calling process received the last input event.
  3. The calling process is granted permission via `AllowSetForegroundWindow`.
* **Servo Mitigation:**
  - Servo uses the standard `AttachThreadInput` bridging technique: attach input queues of the current thread and target window thread, call `BringWindowToTop(hwnd)`, `ShowWindow(hwnd, SW_RESTORE)`, `SetForegroundWindow(hwnd)`, and detach.
  - If a window is minimized, `SW_RESTORE` (9) is mandatory before attempting clicks; otherwise coordinate transforms fail or click into desktop space.

---

## 2. User Account Control (UAC) & Secure Desktop Isolation

### Security Boundary
* When Windows requires administrator consent or credential validation, it spawns `consent.exe` on an isolated session desktop called the **Secure Desktop**.
* User Interface Privilege Isolation (UIPI) forbids standard-user or non-elevated automation tools from injecting input (`SendInput`, `PostMessage`, `WM_LBUTTONDOWN`) into elevated processes.
* The Secure Desktop prevents screen capture from standard sessions; screens appear black or frozen to standard screen APIs.

### Servo Operational Invariant
* **Hard Rule:** Servo **cannot and does not** click inside UAC consent prompts.
* When a task requires administrative elevation:
  1. Servo's Jev Gate marks the operation as `risk: high`.
  2. In `assistant` mode, Servo prompts the user for confirmation *before* running the elevating command.
  3. If a UAC prompt appears, Servo pauses execution, alerts the user visually/audibly via Copilot UI, and waits for manual user approval on the physical keyboard.

---

## 3. Common Dialogs: File Open / Save

### Anatomy
* Classic File Dialogs: Win32 class `#32770`.
* Modern Common Item Dialog (`IFileDialog`): Introduced in Vista/7, enhanced in 10/11.
* Structure:
  - Address Bar / Breadcrumb: `ToolbarWindow32` or `Breadcrumb Parent`.
  - File List / Shell View: `DirectUIHWND` containing items.
  - File Name Input: `ComboBox` -> `Edit` (Control ID `1148` or `1152`).
  - Open / Save Button: `Button` (Control ID `1` / `IDOK`).
  - Cancel Button: `Button` (Control ID `2` / `IDCANCEL`).

### Automation Strategy
* **Direct Path Typing:** Instead of clicking through folders, Servo focuses the File Name input (`Edit` or `ComboBox`), types the absolute path (e.g. `C:\Projects\servo\output.txt`), and presses `Enter`.
* **Settling Window:** File dialogs block the parent window modally. Servo must detect that the dialog has closed before resuming actions on the main application.

---

## 4. System Tray (Notification Area) & Taskbar

### Anatomy
* Primary Taskbar: `Shell_TrayWnd`.
* System Tray Area: `TrayNotifyWnd` containing notification icons (`ToolbarWindow32`).
* Overflow Area: `NotifyIconOverflowWindow` (the pop-up chevron holding hidden background tray icons).

### Behavior
* Icons interact via right-click (context menu) or double-click / left-click (restore window).
* Hovering an icon updates tooltip text (useful for verifying background daemon status).
* Windows 11 clusters tray icons differently; UIA element names frequently match the app name, but secondary tools require opening the overflow flyout (`Button[name="Notification Chevron"]`).

---

## 5. Windows Clipboard Architecture

### Mechanism
* Global shared memory object controlled via Win32 APIs: `OpenClipboard`, `EmptyClipboard`, `SetClipboardData`, `GetClipboardData`, `CloseClipboard`.
* Supported formats: `CF_UNICODETEXT` (13), `CF_TEXT` (1), `CF_HDROP` (file lists), `CF_BITMAP` (2).

### Synchronization & Locking Contention
* Opening the clipboard is exclusive. If another application (e.g. clipboard history `Win+V`, cloud sync, Office clipboard) holds the lock, `OpenClipboard()` fails with `ERROR_ACCESS_DENIED`.
* **Servo Mitigation:** Servo implements exponential retry backoff (up to 5 attempts across 250ms) when reading/writing clipboard text, and restores original clipboard contents when performing transient paste operations.

---

## 6. Standard Windows Keyboard Accelerators & Hotkeys

| Hotkey | Target OS Scope | Servo Usage |
| :--- | :--- | :--- |
| `Alt+F4` | Active Window | Gracefully close application or prompt exit |
| `Win+R` | OS Shell | Open Run prompt for fast headless process launching |
| `Alt+Tab` / `Win+Tab` | OS Shell | Desktop task switching |
| `Win+D` / `Win+M` | Desktop | Minimize all windows to reveal desktop |
| `Ctrl+Shift+Esc` | Task Manager | Process inspection / unsticking |
| `Escape` | Active Context | Cancel modal dialog, dismiss context menu, abort prompt |
| `Ctrl+C` / `Ctrl+V` | Active Focus | Standard copy / paste clipboard transactions |
| `Enter` | Dialog Default | Trigger default action (OK / Open / Save) |
