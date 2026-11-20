# Servo

**Local-First, Closed-Loop Neuro-Symbolic Desktop Control Runtime for Windows 10/11**

Servo is a low-latency, sub-70MB desktop automation runtime built around a three-tier cybernetic servomechanism:
1. **System 2 (Pluggable Setpoint Planner):** Decomposes high-level instructions into sub-goals without handling raw screenshots or pixel coordinates.
2. **Safety Comparator (Jev Gate):** Non-autoregressive decision model validating pre-action risk and post-action UI state transitions.
3. **System 1 Reflex Arc (UIA + Mirage + Needle 3):** High-frequency, deterministic perception and kinematics running on CPU.

## Architecture

See [SERVO_ARCHITECTURE_V1.md](SERVO_ARCHITECTURE_V1.md) for the frozen, immutable architectural specification and decision records.

## Installation

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

## Running Tests

```bash
pytest
```
