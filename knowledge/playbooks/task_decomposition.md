# Playbook: Task Decomposition & Planning Cadence

This playbook instructs **System 2** on how to decompose complex user goals into atomic, grounded subtasks conforming strictly to the Servo Tier 1 grammar.

---

## 1. Planning Principles

### Principle 1: Decompose WHAT, Not HOW
* System 2 decides **WHAT** to do in plain language; it never outputs raw coordinates, element IDs, or internal API calls.
* Every command emitted by System 2 must match the Servo Command Grammar:
  - `click <label>` / `click "Exact Label"`
  - `double click <label>` / `right click <label>`
  - `type "<text>"` / `type "<text>" into <field>`
  - `press <key>` / `hotkey <combo>` (e.g. `hotkey ctrl+s`)
  - `read file <path>` / `write "<content>" to file <path>`
  - `run command "<cmd>"`
  - `go to <url>` / `search the web for <query>`

### Principle 2: Step Cadence & Horizon Bounding
* **Rule:** Return only the subtasks you can confidently formulate from the **current** screen description.
* **If future steps depend on dynamic runtime feedback** (e.g., search results, dialog confirmations, file contents, API outputs):
  - **STOP** the subtask list at the decision point.
  - Execute the prerequisite step.
  - Allow the cascade loop to re-call System 2 with the fresh screen description and command output.

### Principle 3: Headless Preference Over GUI Clicking
* When the user's request involves file operations, directory listings, system inspection, or text retrieval:
  - **Always prefer** headless system tools (`read file`, `list folder`, `write to file`, `run command`) over opening Notepad/Explorer and clicking menus.
  - Headless tools are deterministic, instant, and avoid UI coordinate drift.

---

## 2. Standard Goal Decomposition Templates

### Scenario A: Web Search & Data Extraction
1. `go to https://www.bing.com` (or use browser tool)
2. `type "Windows 11 shortcuts" into Search`
3. `press enter`
4. *(Stop here: observe search results on next turn before attempting to click a link)*

### Scenario B: Saving Application Document
1. `hotkey ctrl+s`
2. *(Wait for Common File Dialog `#32770` to appear in screen state)*
3. `type "C:\Projects\servo\report.md" into "File name"`
4. `click Save`

### Scenario C: File Modification
1. `read file C:\Projects\servo\notes.txt`
2. *(Observe output in "Already done" block on next turn)*
3. `write "Updated content" to file C:\Projects\servo\notes.txt`

---

## 3. Mandatory Safety Guardrail

> [!IMPORTANT]
> **Safety Invariant:** Safety rules, permission checks, privilege boundaries (`assistant` vs. `harness`), and Jev risk floors are **strictly hardcoded in Servo Python code** (`servo/control/jev_gate.py`, `servo/actuation/sandboxing.py`, `servo/cognition/router.py`) and reinforced in the base system prompt.
> System 2 and the cognitive cascade must **never** rely solely on knowledge graph retrieval for security or safety invariants.
