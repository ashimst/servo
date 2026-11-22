# Playbook: Failure Recovery & Escalation Cascade

This playbook guides **System 2** through diagnosing, classifying, and recovering from runtime execution failures within the cognitive cascade.

---

## 1. The Multi-Tier Escalation Cascade

```
Goal
 │
 ├──► Tier 1 (Cactus Needle 3 / Grammar) ──► Confident & Kev Single? ──► Execute
 │                                                     │ Miss
 │                                                     ▼
 ├──► System 2 (Macro-Planner) ────────────► Emits atomic grammar subtasks
                                                       │
                                                       ▼
 ├──► Step Execution ──────────────────────► UIA Perception
                                                       │ Perception Miss
                                                       ▼
                                             Mirage OCR / Neural Needle
                                                       │
                                                       ▼
                                             Kev Pre-Check (Risk Gating)
                                                       │
                                                       ▼
                                             Trajectory Actuation (SendInput)
                                                       │
                                                       ▼
                                             Kev Post-Check (Mutation Verify)
```

---

## 2. Failure Classification & Recovery Strategies

### Failure Type A: Perception Miss (`kind: perception`)
* **Symptom:** `No UI element matched label 'X' in UIA tree.`
* **Root Causes:**
  1. Element is rendered on custom canvas / Direct3D / Electron without accessibility tree.
  2. Element is inside a collapsed menu, tab, or scrolled-out view.
  3. Label differs slightly (e.g. "Save as..." vs "Save As").
* **Recovery Action:**
  - System 1 will automatically attempt **Mirage OCR** fallback sideways during the same step.
  - If Mirage fails and escalates to System 2:
    - Check the fresh screen description in the prompt.
    - Check if the target is inside an unfocused window; emit `switch to <window>`.
    - Check if target requires scrolling; emit `scroll down`.
    - Re-ground on the exact visual string shown in the OCR transcript.

### Failure Type B: Verification Miss (`kind: verify`)
* **Symptom:** `No UI state mutation observed; expected change not verified.`
* **Root Causes:**
  1. Click landed on disabled control or non-interactive border.
  2. Window was not the foreground active window, so the first click only focused the window without triggering the control.
* **Recovery Action:**
  - Do **not** repeat the identical command without changing parameters.
  - First, explicitly ensure window activation: `switch to <window>`.
  - Try keyboard accelerator equivalent (e.g. `press enter` or `hotkey alt+s` instead of `click Save`).

### Failure Type C: Modal Dialog Block
* **Symptom:** Target window is unresponsive (`IsHungAppWindow()`), or clicks fail because a child modal dialog has seized input focus.
* **Root Causes:**
  1. "Unsaved Changes" confirmation box.
  2. License agreement, update prompt, or error dialog.
* **Recovery Action:**
  - Identify modal dialog from active windows list.
  - Address the modal dialog first: `click OK`, `click Close`, or `press escape`.
  - Resume parent task once modal dialog is dismissed.

### Failure Type D: Policy / Abort Block (`kind: policy` / `kind: abort`)
* **Symptom:** Operation rejected by Kev Gate, requires user confirmation, or attempted out-of-scope system modification.
* **Recovery Action:**
  - **Never attempt to bypass security policies or sandboxes.**
  - If user cancelled confirmation: acknowledge gracefully in `reply` and mark `done: true`.
  - If high-risk command was rejected: explain the constraint to the user and suggest a safe read-only alternative.
