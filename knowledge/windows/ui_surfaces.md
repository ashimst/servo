# Windows UI Surfaces Ontology

This document models the primary user interface surface technologies encountered in Windows desktop automation and defines when Servo's **System 1 UIA** sensor succeeds versus when **Mirage OCR / Needle** fallback is required.

---

## 1. Win32 Controls (USER32 / GDI / ComCtl32)

### Architecture
* **Technology:** Standard Windows controls (`BUTTON`, `EDIT`, `LISTBOX`, `COMBOBOX`, `STATIC`, `SysTreeView32`, `SysListView32`).
* **Window Hierarchy:** Deep, strict `HWND` trees where each distinct control typically owns an individual child `HWND`.
* **Accessibility Implementation:** Built-in Win32 UIA Proxies (`UIAutomationCore.dll`) translate classic window messages (`WM_GETTEXT`, `WM_COMMAND`, `BM_CLICK`) into UIA patterns.

### UIA Capability vs. Mirage Fallback
* **UIA Support:** **Full & Deterministic (Tier 1 Primary)**.
  - Element discovery via `AutomationId`, `Name`, `ClassName`, and `ControlType`.
  - Patterns: `InvokePattern`, `ValuePattern`, `SelectionItemPattern`.
* **When Mirage is Required:**
  - Owner-drawn controls (`BS_OWNERDRAW`, custom GDI paint in `WM_PAINT`) where text is blitted as bitmaps without updating control accessibility names.
  - Custom canvas toolbars lacking automation peers.

---

## 2. Windows Presentation Foundation (WPF)

### Architecture
* **Technology:** XAML-based retained-mode graphics rendered via DirectX / DirectWrite (`PresentationCore`, `PresentationFramework`).
* **Window Hierarchy:** Single top-level `HWND` (`HwndWrapper[...]`) containing an internal visual and logical tree of XAML elements.
* **Accessibility Implementation:** Native `AutomationPeer` hierarchy (`ButtonAutomationPeer`, `TextBoxAutomationPeer`).

### UIA Capability vs. Mirage Fallback
* **UIA Support:** **High with Architectural Caveats**.
  - `AutomationProperties.AutomationId` and `AutomationProperties.Name` provide rock-solid anchors.
* **When Mirage is Required (Virtualized Trees):**
  - **VirtualizingStackPanel / DataGrid:** WPF virtualizes off-screen list items. Off-screen elements **do not exist** in the visual or UIA tree until scrolled into view.
  - Custom templated user controls where developers neglected to override `OnCreateAutomationPeer()`.
  - Hardware-accelerated canvas controls (`D3DImage`, custom Direct3D views).

---

## 3. Electron and Chromium Apps (`Chrome_WidgetWin_1`)

### Architecture
* **Technology:** Chromium Content Shell rendering web DOM trees inside a top-level Win32 window class `Chrome_WidgetWin_1`. Examples: VS Code, Slack, Microsoft Teams, Discord, Microsoft Edge, Google Chrome.
* **Window Hierarchy:** One top-level `HWND` with one or two direct child windows (`Intermediate D3D Window`). Individual HTML buttons and inputs do **not** have `HWND`s.
* **Accessibility Implementation:** Chromium Accessibility Architecture.

### UIA Capability vs. Mirage Fallback
* **UIA Support:** **Conditional on Accessibility Activation**.
  - Chromium keeps its accessibility tree **dormant** by default to conserve memory and CPU.
  - When a UIA client (such as `servo.perception.uia_sensor`) queries the window, Chromium lazily builds the UIA tree.
* **When Mirage is Required:**
  - Lazy tree wake-up race condition: UIA queries may return empty or root-only if Chromium hasn't finished tree generation.
  - Canvas/WebGL/WebGPU applications (Figma, web games, CAD viewers) where the DOM is a blank `<canvas>`. System 1 UIA returns only the container; **Mirage OCR + Needle template matching is strictly mandatory**.

---

## 4. Modern Browser DOMs (Edge / Chrome via Playwright Tier)

### Architecture
* **Technology:** Web standards (HTML5, SVG, CSS, Shadow DOM).
* **Integration Bridge:** `servo.cognition.browser` via Chrome DevTools Protocol (CDP) / Playwright.

### UIA Capability vs. Mirage Fallback
* **UIA Support:** Bypassed in favor of direct **Playwright DOM accessibility snapshots**.
  - Native role selectors (`button[name="Submit"]`), CSS selectors, and text locators.
* **When Mirage is Required:**
  - Heavy canvas rendering or SVG graphics with missing ARIA tags.
  - Native browser UI popups (e.g., HTTP Basic Auth prompts, geolocation permission bubbles, file save dialogues) that sit outside the webpage DOM.

---

## 5. Custom Canvas / DirectX / Game Engines / Qt (Non-Accessible)

### Architecture
* **Technology:** DirectX, OpenGL, Vulkan, Skia, Qt (built without accessibility plugins), SDL.
* **Window Hierarchy:** Single opaque `HWND` presenting rendered swapchain buffers.
* **Accessibility Implementation:** Non-existent or dummy root peer.

### UIA Capability vs. Mirage Fallback
* **UIA Support:** **Zero / Perception Miss**.
  - UIA tree contains only the top-level window container without interactive child nodes.
* **When Mirage is Required:** **100% Mandatory (Primary Perception)**.
  - `servo.perception.mirage_sensor` extracts text coordinates via on-device Cactus Mirage / Windows Media OCR.
  - `servo.cognition.needle` identifies visual targets via neural feature matching.

---

## Surface Decision Matrix

| UI Surface Technology | Window Class / Signature | UIA Suitability | Mirage Fallback Trigger | Primary Servo Bridge |
| :--- | :--- | :--- | :--- | :--- |
| **Win32 Classic** | `#32770`, `Button`, `Edit` | **95%** | Owner-drawn text, custom bitmaps | `uia_sensor` |
| **WPF** | `HwndWrapper*` | **85%** | `VirtualizingStackPanel` off-screen, missing peers | `uia_sensor` -> `mirage_sensor` |
| **Electron / Chromium** | `Chrome_WidgetWin_1` | **80%** | Dormant tree, HTML5 `<canvas>`, WebGL | `uia_sensor` -> `mirage_sensor` |
| **Browser Webpage** | Inside Browser Tab | **100% (DOM)** | Native file pickers, canvas elements | `browser.py` |
| **DirectX / Custom Canvas**| Direct3D / OpenGL swapchain | **0%** | Always (no accessible tree exists) | `mirage_sensor` + `needle` |
