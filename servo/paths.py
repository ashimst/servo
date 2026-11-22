"""Where Servo keeps its data: ``SERVO_HOME`` if set, else ``~/.servo``.

Every persistent path (token, settings, traces, chat memory, graph, schedule, browser
profiles) derives from here, so tests and alternate profiles can redirect all of it
with one variable instead of overriding the user's home folder.
"""

from __future__ import annotations

import os
from pathlib import Path


def servo_home() -> Path:
    override = os.environ.get("SERVO_HOME", "").strip()
    return Path(override) if override else Path.home() / ".servo"
