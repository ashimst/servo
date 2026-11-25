"""Servo as an MCP server (`servo --mcp`, stdio): the same engine as the copilot.

Tools:
  windows()                 open windows (title, process)
  screen(window?)           the numbered screen description Servo's planners see
  step(text, window?)       ONE step in Servo's step forms ("click [12]", 'type "hi" into [4]'),
                            run by the Needle agent for its tool, checked against the step's
                            words, gated by Laya and verified. Numbers refer to the last screen()
  do(goal, window?)         Servo plans and runs the whole goal

While the copilot is open, every call is forwarded to it (servo/daemon/discovery.py): it queues
behind the user's own requests, shows live with its trace, and risky steps ask the user there.
Without the copilot the call runs in this process, and any action that needs confirmation is
refused, because nobody is there to approve it.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from servo.daemon import discovery
from servo.perception import windows

logger = logging.getLogger("servo.mcp")

INSTRUCTIONS = (
    "Servo operates the user's Windows desktop. Either call do(goal) and let Servo plan, or plan "
    "yourself: call screen() to get the numbered controls, then step() with ONE step per call in "
    "Servo's step forms, e.g. click [12] · type \"hello\" into [4] · press ctrl+s · choose \"Large\" in [9] · "
    "turn on [7] · open notepad · go to github.com · wait 2. Control numbers change every time the "
    "screen changes: call screen() again after each step. Use window to pick a window by title."
)

STANDALONE_NOTE = ("The copilot is not open, so this ran without it: actions that need the user's "
                   "confirmation are refused. Open the copilot (servo) to approve them.")


class _Standalone:
    """Runs calls in this process when no copilot is open."""

    def __init__(self) -> None:
        self.hwnd: Optional[int] = None
        self.numbered: Dict[int, Dict[str, Any]] = {}

    def _router(self, tracer: Any) -> Any:
        from servo.cognition.router import CascadeRouter
        from servo.daemon.copilot import launch_or_focus_app

        def no_one_to_confirm(req: Any) -> bool:
            tracer.emit("decision", "Needs the user's confirmation · refused",
                        f"{req.tool_call}: the copilot is not open, so nobody can approve a "
                        f"{req.risk_level}-risk action.", tier="mcp", status="fail")
            return False

        target = self.hwnd if self.hwnd and windows.is_window(self.hwnd) else None
        return CascadeRouter(tracer=tracer, confirmation_callback=no_one_to_confirm, app_launcher=launch_or_focus_app,
                             target_hwnd=target)

    def screen(self, hwnd: Optional[int]) -> str:
        from servo.cognition.trace import NullTracer
        self.hwnd = hwnd or self.hwnd
        with discovery.com_thread():
            text, self.numbered = self._router(NullTracer()).screen()
        return text

    def run(self, kind: str, text: str, hwnd: Optional[int]) -> Dict[str, Any]:
        from servo.cognition.router import new_run_id
        from servo.cognition.trace import Tracer
        self.hwnd = hwnd or self.hwnd
        with discovery.com_thread():
            router = self._router(Tracer(new_run_id()))
            out = router.run_step(text, self.numbered) if kind == "step" else router.run(text)
        self.hwnd = getattr(router, "hwnd", None) or self.hwnd
        return {**out.model_dump(), "note": STANDALONE_NOTE}


_standalone = _Standalone()


def _copilot() -> Optional[Dict[str, Any]]:
    """The running copilot, if it answers."""
    info = discovery.read()
    if not info:
        return None
    try:
        r = httpx.get(f"http://127.0.0.1:{info['port']}/api/state", headers={"X-Servo-Token": info["token"]},
                      timeout=2.0)
        return info if r.status_code == 200 else None
    except httpx.HTTPError:
        return None


def _forward(info: Dict[str, Any], path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    r = httpx.post(f"http://127.0.0.1:{info['port']}/api/mcp/{path}", json=body,
                   headers={"X-Servo-Token": info["token"]}, timeout=float(body.get("timeout", 600)) + 30)
    if r.status_code != 200:
        detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else r.text
        return {"success": False, "reply": f"The copilot refused the call ({r.status_code}): {detail}"}
    return r.json()


def _hwnd(window: Optional[str]) -> Optional[int]:
    return windows.find_by_title(window) if window else None


# ------------------------------------------------------------------ tool bodies

def list_open_windows() -> List[Dict[str, Any]]:
    return [{"title": w["title"], "process": w["process"]} for w in windows.list_windows()]


def get_screen(window: Optional[str] = None) -> str:
    info = _copilot()
    if info:
        res = _forward(info, "screen", {"window": window})
        return str(res.get("screen") or res.get("reply") or "")
    return _standalone.screen(_hwnd(window))


def run_step(text: str, window: Optional[str] = None) -> Dict[str, Any]:
    info = _copilot()
    if info:
        return _forward(info, "step", {"text": text, "window": window})
    return _standalone.run("step", text, _hwnd(window))


def run_goal(goal: str, window: Optional[str] = None) -> Dict[str, Any]:
    info = _copilot()
    if info:
        return _forward(info, "do", {"text": goal, "window": window})
    return _standalone.run("goal", goal, _hwnd(window))


def build_server() -> Any:
    from fastmcp import FastMCP

    mcp = FastMCP("Servo", instructions=INSTRUCTIONS)

    @mcp.tool(name="windows")
    def windows_tool() -> List[Dict[str, Any]]:
        """List the open windows (title and process)."""
        return list_open_windows()

    @mcp.tool()
    def screen(window: Optional[str] = None) -> str:
        """The numbered controls of the target window (or the window titled `window`), the
        same description Servo's planners see. Call it before step() and after each step."""
        return get_screen(window)

    @mcp.tool()
    def step(text: str, window: Optional[str] = None) -> Dict[str, Any]:
        """Run ONE step in Servo's step forms, e.g. click [12], type "hello" into [4],
        press ctrl+s, choose "Large" in [9], open notepad, go to github.com, wait 2.
        Control numbers refer to the last screen()."""
        return run_step(text, window)

    @mcp.tool()
    def do(goal: str, window: Optional[str] = None) -> Dict[str, Any]:
        """Let Servo plan and run a whole goal in plain language."""
        return run_goal(goal, window)

    return mcp


def main() -> None:
    try:
        server = build_server()
    except ImportError:
        raise SystemExit("FastMCP is not installed: pip install servo[mcp]")
    server.run()
