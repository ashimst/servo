"""How `servo --mcp` finds a running copilot: the copilot writes its port and per-launch
token to ~/.servo/copilot.json on start and deletes it on exit. The file lives in the
user's own profile, the same place as the rest of Servo's state."""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from servo.paths import servo_home


def path() -> Path:
    return servo_home() / "copilot.json"


def publish(port: int, token: str) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"port": port, "token": token, "pid": os.getpid()}), encoding="utf-8")


def withdraw() -> None:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
        if data.get("pid") == os.getpid():
            path().unlink()
    except (OSError, ValueError):
        pass


def read() -> Optional[Dict[str, Any]]:
    """The running copilot's {port, token, pid}, or None when there is none."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
        return data if data.get("port") and data.get("token") else None
    except (OSError, ValueError):
        return None


@contextlib.contextmanager
def com_thread() -> Iterator[None]:
    """UI Automation needs COM initialised on the calling thread."""
    com = False
    if sys.platform == "win32":
        try:
            import pythoncom  # type: ignore
            pythoncom.CoInitialize()
            com = True
        except ImportError:
            pass
    try:
        yield
    finally:
        if com:
            import pythoncom  # type: ignore
            pythoncom.CoUninitialize()
