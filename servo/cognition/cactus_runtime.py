"""Lazy access to Cactus Compute models.

Two packages exist and BOTH import as ``cactus`` (they cannot be installed together):

* ``cactus-compute`` >= 3.3 (github.com/cactus-compute/cactus): Needle 3 tool calling
  (``cactus.Cactus`` / ``cactus.Needle``), Whistle speech, Mirage vision
  (``cactus.Mirage``). Needle's ``complete()`` returns proposed calls WITHOUT running
  them::

      {"type": "call", "function_calls": [{"name": ..., "arguments": {...}}],
       "reasoning": "...", "confidence": 0.94, ...}

* ``cactus-engine`` (github.com/cactus-compute/engine): general LLM/VLM engine
  (``cactus_init`` / ``cactus_complete``) with a confidence probe and cloud handoff.
  Servo can use it as a local System 2 (``SERVO_PLANNER_PROVIDER=cactus``).

Everything here degrades to ``None`` when the package or weights are missing; callers
treat that as "tier unavailable" and the trace says so.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, List, Optional

from servo.config import settings

logger = logging.getLogger("servo.cactus")

_lock = threading.Lock()
_needle_error: Optional[str] = None
_mirage: Any = None
_mirage_error: Optional[str] = None


def _import_cactus() -> Any:
    import cactus  # type: ignore
    return cactus


def status() -> Dict[str, Any]:
    """Report which Cactus capabilities are importable (does not load weights)."""
    info: Dict[str, Any] = {"installed": False, "needle": False, "mirage": False, "engine": False, "version": None}
    try:
        cactus = _import_cactus()
    except Exception as e:
        info["error"] = f"cactus not installed ({e.__class__.__name__})"
        return info
    info["installed"] = True
    info["version"] = getattr(cactus, "__version__", None)
    info["needle"] = hasattr(cactus, "Cactus") or hasattr(cactus, "Needle")
    info["mirage"] = hasattr(cactus, "Mirage")
    info["engine"] = hasattr(cactus, "cactus_complete")
    if _needle_error:
        info["needle_error"] = _needle_error
    if _mirage_error:
        info["mirage_error"] = _mirage_error
    return info


NEEDLE_SYSTEM = (
    "You translate one desktop-control step into tool calls for a Windows assistant. "
    "Copy control numbers and quoted text exactly. Never invent coordinates."
)
_needles: Dict[str, Any] = {}


def get_needle(tools: List[Dict[str, Any]], key: str = "default", system: str = "",
               weights: Optional[str] = None) -> Any:
    """A cached Needle agent per specialist ``key``, bound to ``tools`` and ``system``.

    Cactus fixes the tools when an agent is created, so each specialist is its own agent
    (a few tens of MB each). ``weights`` overrides ``settings.needle_weights`` for this
    agent (the request and command agents use Servo's tuned Needle, needle_model/)."""
    global _needle_error
    with _lock:
        if key in _needles:
            return _needles[key]
        if _needle_error is not None:
            return None
        try:
            cactus = _import_cactus()
            cls = getattr(cactus, "Needle", None) or getattr(cactus, "Cactus")
            kwargs: Dict[str, Any] = {"tools": tools, "system": system or NEEDLE_SYSTEM, "stateless": True}
            if weights or settings.needle_weights:
                kwargs["weights"] = weights or settings.needle_weights
            _needles[key] = cls(**kwargs)
        except Exception as e:
            _needle_error = f"{e.__class__.__name__}: {e}"
            logger.info("Cactus Needle unavailable: %s", _needle_error)
            return None
        return _needles[key]


def needle_complete(agent: Any, text: str) -> Optional[Dict[str, Any]]:
    """Ask Needle for tool calls. Returns the response dict or None."""
    try:
        with _lock:  # one native engine instance; serialize calls
            raw = agent.complete(text=text, max_new_tokens=256)
    except Exception as e:
        logger.warning("Needle complete() failed: %s", e)
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    return raw if isinstance(raw, dict) else None


def get_mirage() -> Any:
    global _mirage, _mirage_error
    with _lock:
        if _mirage is not None or _mirage_error is not None:
            return _mirage
        try:
            cactus = _import_cactus()
            if not hasattr(cactus, "Mirage"):
                raise ImportError("installed cactus package has no Mirage (need cactus-compute >= 3.3)")
            _mirage = cactus.Mirage(weights=settings.mirage_weights or None)
        except Exception as e:
            _mirage_error = f"{e.__class__.__name__}: {e}"
            logger.info("Cactus Mirage unavailable: %s", _mirage_error)
            _mirage = None
        return _mirage


def mirage_error() -> Optional[str]:
    return _mirage_error


def needle_error() -> Optional[str]:
    return _needle_error


def reset_for_tests() -> None:
    global _needle_error, _mirage, _mirage_error
    with _lock:
        _needles.clear()
        _needle_error = _mirage = _mirage_error = None
