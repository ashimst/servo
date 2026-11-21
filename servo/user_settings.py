"""Settings chosen in the copilot's setup screen, persisted in ~/.servo/settings.json.

Environment variables (SERVO_*) always win over saved values, so deployments stay
scriptable. API keys are encrypted with Windows DPAPI (per-user) when available, so
the file is useless if copied to another account or machine.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("servo.user_settings")

PATH = Path.home() / ".servo" / "settings.json"

PLAIN_FIELDS = (
    "planner_provider", "anthropic_model", "openai_base_url", "openai_model", "gemini_model",
    "kev_url", "browser_channel",
)
SECRET_FIELDS = ("anthropic_api_key", "openai_api_key", "gemini_api_key", "kev_api_key")


def _disabled() -> bool:
    return os.environ.get("SERVO_NO_USER_SETTINGS") == "1"


def _protect(value: str) -> Dict[str, str]:
    try:
        import win32crypt  # type: ignore
        blob = win32crypt.CryptProtectData(value.encode("utf-8"), "servo", None, None, None, 0)
        return {"dpapi": base64.b64encode(blob).decode("ascii")}
    except Exception:
        return {"plain": value}


def _unprotect(stored: Any) -> str:
    if isinstance(stored, str):
        return stored
    if not isinstance(stored, dict):
        return ""
    if "dpapi" in stored:
        try:
            import win32crypt  # type: ignore
            _, data = win32crypt.CryptUnprotectData(base64.b64decode(stored["dpapi"]), None, None, None, 0)
            return data.decode("utf-8")
        except Exception as e:
            logger.warning("Could not decrypt a saved key (%s); re-enter it in setup.", e)
            return ""
    return str(stored.get("plain", ""))


def read() -> Dict[str, Any]:
    if _disabled() or not PATH.exists():
        return {}
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("Ignoring unreadable %s: %s", PATH, e)
        return {}


def setup_done() -> bool:
    return bool(read().get("setup_done"))


def apply(settings: Any) -> None:
    """Load saved values into ``settings`` unless an env var already sets them."""
    data = read()
    for name in PLAIN_FIELDS + SECRET_FIELDS:
        if f"SERVO_{name.upper()}" in os.environ or name not in data:
            continue
        value = _unprotect(data[name]) if name in SECRET_FIELDS else data[name]
        if value not in (None, ""):
            try:
                setattr(settings, name, value)
            except Exception:
                pass


def save(settings: Any, updates: Dict[str, Any], mark_done: bool = True) -> None:
    data = read()
    for name, value in updates.items():
        if name in SECRET_FIELDS:
            if value is None:        # not sent: keep the existing key
                continue
            if value == "":
                data.pop(name, None)
            else:
                data[name] = _protect(str(value))
        elif name in PLAIN_FIELDS:
            data[name] = value
        else:
            continue
        if value is not None:
            try:
                setattr(settings, name, value)
            except Exception:
                pass
    if mark_done:
        data["setup_done"] = True
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def key_hint(value: Optional[str]) -> str:
    return f"…{value[-4:]}" if value and len(value) >= 8 else ("set" if value else "")
