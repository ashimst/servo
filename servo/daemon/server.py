"""FastAPI REST and FastMCP unified daemon server (Servo v2).

See ADR-12, ADR-13, ADR-14, ADR-15 in SERVO_ARCHITECTURE_V2.md and SERVO_HARDENING_NOTES.md.
Exposes MCP tools, REST endpoints, Whistle voice ingestion, and HUD overlays.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import sys
from typing import Any, AsyncIterator, Dict, List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from servo import __version__
from servo.config import get_hardware_profile, settings
from servo.daemon.auth import get_api_token, require_token, token_path
from servo.daemon.tasks import TaskRequestError, get_trace_store, run_goal, sense_scene
from servo.daemon.ui import hud_overlay
from servo.daemon.voice import VoiceUnavailableError, voice_manager
from servo.daemon.worker import worker
from servo.perception import windows
from servo.perception.scene_index import SceneIndex

try:
    from fastmcp import FastMCP
    mcp = FastMCP(
        "Servo Desktop Control Runtime",
        instructions=(
            "Servo: closed-loop desktop control for Windows. Call list_windows_tool to find the "
            "target HWND, get_scene_index_tool to see element labels, then run_task_tool with one "
            "precise command per call, e.g. click \"Save\" or type \"hello\" into \"Search\"."
        ),
    )
except ImportError:
    mcp = None


@contextlib.asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Lifespan handler managing STA COM worker thread lifecycle."""
    get_api_token()  # make sure the token exists before the first request
    worker.start()
    yield
    worker.stop()


app = FastAPI(
    title="Servo Desktop Control Runtime v2",
    version=__version__,
    description="Local-First, Closed-Loop Neuro-Symbolic Desktop Control Runtime",
    lifespan=lifespan,
)

ModeField = Optional[Literal["assistant", "harness"]]


class TaskRequest(BaseModel):
    goal: str
    hwnd: Optional[int] = None
    mode: ModeField = None
    fast_mode: bool = False


class VoiceDataRequest(BaseModel):
    audio_b64: str
    hwnd: Optional[int] = None
    mode: ModeField = None


# =============================================================================
# FastMCP Tools (stdio transport: only the launching client can reach them)
# =============================================================================

if mcp is not None:
    @mcp.tool()
    def list_windows_tool() -> List[Dict[str, Any]]:
        """List visible top-level windows (hwnd, title, process) to choose a target."""
        return windows.list_windows()

    @mcp.tool()
    def run_task_tool(goal: str, hwnd: int) -> Dict[str, Any]:
        """Execute one precise desktop command on the window `hwnd`.

        The calling agent acts as the planner (passthrough), so send one action per call:
        click "Label" or type "text" into "Label". Labels come from get_scene_index_tool.
        """
        try:
            return run_goal(goal=goal, hwnd=hwnd, planner="passthrough")
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def get_scene_index_tool(hwnd: Optional[int] = None) -> Dict[str, Any]:
        """Get the current SceneIndex (element labels and IDs) for a window (default: foreground)."""
        try:
            return sense_scene(hwnd)
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def assert_ui_state_tool(expected: str, hwnd: Optional[int] = None) -> Dict[str, Any]:
        """Assert whether the current window title or element labels contain the expected text."""
        try:
            scene_data = sense_scene(hwnd)
            elements = scene_data.get("elements", {})
            labels = [el.get("label", "") for el in elements.values() if isinstance(el, dict)]
            matched = any(expected.lower() in l.lower() for l in labels)
            if expected.lower() in scene_data.get("window_title", "").lower():
                matched = True
            return {"matched": matched, "expected": expected, "found_labels_sample": labels[:10]}
        except Exception as e:
            return {"matched": False, "error": str(e)}

    @mcp.tool()
    def voice_task_tool(audio_path: str, hwnd: int) -> Dict[str, Any]:
        """Transcribe an audio file with Cactus Whistle and run the command on window `hwnd`."""
        try:
            return voice_manager.ingest_voice_task(audio_data=audio_path, hwnd=hwnd)
        except Exception as e:
            return {"success": False, "error": str(e)}


# =============================================================================
# FastAPI REST Endpoints (bearer token required except /health)
# =============================================================================

def _bad_request(e: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(e))


@app.get("/health")
def health() -> Dict[str, Any]:
    """Unauthenticated liveness check. Deliberately minimal."""
    return {"status": "healthy", "service": "servo", "version": __version__}


@app.get("/status", dependencies=[Depends(require_token)])
def status() -> Dict[str, Any]:
    """Hardware profile and effective configuration."""
    return {
        "version": __version__,
        "hardware": get_hardware_profile(),
        "config": {
            "mode": settings.mode,
            "device": settings.device,
            "fast_mode": settings.fast_mode,
            "planner_provider": settings.planner_provider,
        },
    }


@app.get("/windows", dependencies=[Depends(require_token)])
def list_windows() -> Dict[str, Any]:
    """List visible top-level windows so callers can pass an explicit hwnd."""
    return {"windows": windows.list_windows()}


@app.get("/scene", dependencies=[Depends(require_token)])
def get_scene(hwnd: Optional[int] = None) -> Dict[str, Any]:
    """Query current SceneIndex for the specified or foreground HWND."""
    try:
        return sense_scene(hwnd)
    except Exception as e:
        raise _bad_request(e)


@app.post("/task", dependencies=[Depends(require_token)])
def run_task(req: TaskRequest) -> Dict[str, Any]:
    """Plan (with SERVO_PLANNER_PROVIDER) and execute a goal on window `hwnd`."""
    try:
        return run_goal(goal=req.goal, hwnd=req.hwnd, mode=req.mode, fast_mode=req.fast_mode)
    except TaskRequestError as e:
        raise _bad_request(e)


@app.get("/traces", dependencies=[Depends(require_token)])
def get_traces(limit: int = 50) -> Dict[str, Any]:
    """Retrieve recent automation traces from SQLite."""
    return {"runs": get_trace_store().list_runs(limit=max(1, min(limit, 500)))}


@app.post("/voice/task", dependencies=[Depends(require_token)])
def voice_task(req: VoiceDataRequest) -> Dict[str, Any]:
    """Ingest base64-encoded audio, transcribe via Whistle, and execute."""
    try:
        audio_bytes = base64.b64decode(req.audio_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid base64 audio: {e}")
    try:
        return voice_manager.ingest_voice_task(audio_data=audio_bytes, hwnd=req.hwnd, mode=req.mode)
    except VoiceUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except TaskRequestError as e:
        raise _bad_request(e)


# Served SVG must not be able to run script even if a label slips through escaping.
_SVG_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    "X-Content-Type-Options": "nosniff",
}


@app.get("/hud/overlay", dependencies=[Depends(require_token)])
def get_hud_overlay(hwnd: Optional[int] = None, highlight_id: Optional[int] = None) -> Response:
    """Return an SVG overlay of detected elements for visual grounding."""
    try:
        scene = SceneIndex(**sense_scene(hwnd))
    except Exception as e:
        raise _bad_request(e)
    svg_content = hud_overlay.generate_svg_overlay(scene, highlight_id=highlight_id)
    return Response(content=svg_content, media_type="image/svg+xml", headers=_SVG_HEADERS)


def main() -> None:
    """CLI entrypoint for running the Servo daemon or MCP server."""
    import argparse
    import logging

    parser = argparse.ArgumentParser(description="Servo Desktop Control Runtime Daemon")
    parser.add_argument("--mcp", action="store_true", help="Run as FastMCP server over stdio")
    parser.add_argument("--port", type=int, default=8000, help="Port for REST server (default: 8000)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host address (default: 127.0.0.1)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    if args.mcp:
        if mcp is None:
            print("FastMCP is not installed. Install with: pip install servo[mcp]", file=sys.stderr)
            sys.exit(1)
        worker.start()
        try:
            mcp.run()
        finally:
            worker.stop()
    else:
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            print(f"WARNING: binding to {args.host} exposes desktop control beyond this machine.", file=sys.stderr)
        get_api_token()
        print(f"Servo REST API token: {token_path()} (or SERVO_API_TOKEN)", file=sys.stderr)
        import uvicorn
        uvicorn.run("servo.daemon.server:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
