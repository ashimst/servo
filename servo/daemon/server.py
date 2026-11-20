"""FastAPI REST/WebSocket and FastMCP unified daemon server (Servo v2).

See ADR-12, ADR-13, ADR-14, ADR-15 in SERVO_ARCHITECTURE_V2.md.
Exposes MCP tools, REST endpoints, Whistle voice ingestion, and HUD overlays.
"""

from __future__ import annotations

import base64
import contextlib
import sys
import time
import uuid
from typing import Any, AsyncIterator, Dict, Optional
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel

from servo.config import settings, get_hardware_profile
from servo.daemon.worker import worker, TaskJob
from servo.daemon.voice import voice_manager
from servo.daemon.ui import hud_overlay
from servo.telemetry.store import TraceStore
from servo.telemetry.models import TracePayload, StepTrace, RunMetrics
from servo.perception.scene_index import SceneIndex

# Initialize FastMCP Server
try:
    from fastmcp import FastMCP
    mcp = FastMCP(
        "Servo Desktop Control Runtime",
        instructions="Servo v2: Local-First, Closed-Loop Neuro-Symbolic Desktop Control Runtime for Windows 10/11.",
    )
except ImportError:
    mcp = None


trace_store = TraceStore()


@contextlib.asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Modern lifespan handler managing STA COM worker thread lifecycle."""
    worker.start()
    yield
    worker.stop()


app = FastAPI(
    title="Servo Desktop Control Runtime v2",
    version="0.2.0",
    description="Local-First, Closed-Loop Neuro-Symbolic Desktop Control Runtime",
    lifespan=lifespan,
)


class TaskRequest(BaseModel):
    goal: str
    hwnd: Optional[int] = None
    mode: str = "assistant"
    fast_mode: bool = False


class VoiceDataRequest(BaseModel):
    audio_b64: str
    hwnd: Optional[int] = None
    mode: str = "assistant"


# =============================================================================
# Core Execution Helper Functions
# =============================================================================

def execute_sense_scene(hwnd: Optional[int] = None) -> Dict[str, Any]:
    """Sense target window or active foreground window."""
    target_hwnd = hwnd
    if target_hwnd is None and sys.platform == "win32":
        import ctypes
        target_hwnd = ctypes.windll.user32.GetForegroundWindow()

    if not target_hwnd:
        raise ValueError("No active target window found or HWND specified.")

    job = TaskJob(
        job_id=f"sense_{uuid.uuid4().hex[:8]}",
        action_type="sense_scene",
        hwnd=target_hwnd,
    )
    result = worker.submit_job(job, timeout=5.0)
    if not result.success:
        raise RuntimeError(result.error or "Failed to sense scene.")
    return result.data or {}


def execute_task_goal(
    goal: str,
    hwnd: Optional[int] = None,
    mode: str = "assistant",
    fast_mode: bool = False,
) -> Dict[str, Any]:
    """Run an automation goal through the closed-loop reflex arc."""
    target_hwnd = hwnd
    if target_hwnd is None and sys.platform == "win32":
        import ctypes
        target_hwnd = ctypes.windll.user32.GetForegroundWindow()

    if not target_hwnd:
        raise ValueError("No active target window found.")

    t0 = time.perf_counter()
    job = TaskJob(
        job_id=f"task_{uuid.uuid4().hex[:8]}",
        action_type="execute_subgoal",
        hwnd=target_hwnd,
        params={
            "step_index": 1,
            "sub_goal": goal,
            "target_label_hint": goal,
            "action_type": "type" if "type" in goal.lower() or "enter" in goal.lower() else "click",
            "fast_mode": fast_mode,
        },
    )
    res = worker.submit_job(job, timeout=15.0)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    # Persist trace
    payload = TracePayload(
        mode=mode,
        status="passed" if res.success else "failed",
        goal=goal,
        metrics=RunMetrics(total_latency_ms=elapsed_ms, uia_steps=1),
        steps=[
            StepTrace(
                step=1,
                sub_goal=goal,
                perception_source="uia",
                tool_call=f"action('{goal}')",
                jev_pre_risk="low",
                jev_post_ok=res.success,
                latency_ms=elapsed_ms,
            )
        ],
    )
    trace_store.record_run(payload)

    return {
        "success": res.success,
        "run_id": payload.run_id,
        "latency_ms": elapsed_ms,
        "data": res.data,
        "error": res.error,
    }


# =============================================================================
# FastMCP Tools (for external coding agents and MCP clients)
# =============================================================================

if mcp is not None:
    @mcp.tool()
    def run_task_tool(goal: str, mode: str = "assistant", hwnd: Optional[int] = None) -> Dict[str, Any]:
        """Execute a desktop automation goal through Servo's closed-loop reflex arc."""
        try:
            return execute_task_goal(goal=goal, hwnd=hwnd, mode=mode)
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def get_scene_index_tool(hwnd: Optional[int] = None) -> Dict[str, Any]:
        """Get the current normalized SceneIndex (element ID map) for a target window."""
        try:
            return execute_sense_scene(hwnd=hwnd)
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def assert_ui_state_tool(expected: str, hwnd: Optional[int] = None) -> Dict[str, Any]:
        """Assert whether the current window UI reflects the expected label or state."""
        try:
            scene_data = execute_sense_scene(hwnd=hwnd)
            elements = scene_data.get("elements", {})
            labels = [el.get("label", "") for el in elements.values() if isinstance(el, dict)]
            matched = any(expected.lower() in l.lower() for l in labels)
            title = scene_data.get("window_title", "")
            if expected.lower() in title.lower():
                matched = True
            return {"matched": matched, "expected": expected, "found_labels_sample": labels[:10]}
        except Exception as e:
            return {"matched": False, "error": str(e)}

    @mcp.tool()
    def voice_task_tool(audio_path: str, hwnd: Optional[int] = None) -> Dict[str, Any]:
        """Ingest an audio command using Cactus Whistle and run through Servo."""
        try:
            return voice_manager.ingest_voice_task(audio_data=audio_path, hwnd=hwnd)
        except Exception as e:
            return {"success": False, "error": str(e)}


# =============================================================================
# FastAPI REST Endpoints (for Web UI, trays, and local dashboards)
# =============================================================================

@app.get("/health")
def health() -> Dict[str, Any]:
    """Health check endpoint and hardware profile."""
    return {
        "status": "healthy",
        "service": "servo",
        "version": "0.2.0",
        "hardware": get_hardware_profile(),
        "config": {
            "mode": settings.mode,
            "device": settings.device,
            "fast_mode": settings.fast_mode,
        },
    }


@app.get("/scene")
def get_scene(hwnd: Optional[int] = None) -> Dict[str, Any]:
    """Query current SceneIndex for the specified or foreground HWND."""
    try:
        return execute_sense_scene(hwnd=hwnd)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/task")
def run_task(req: TaskRequest) -> Dict[str, Any]:
    """Execute a task goal through Servo closed-loop reflex arc."""
    try:
        return execute_task_goal(
            goal=req.goal,
            hwnd=req.hwnd,
            mode=req.mode,
            fast_mode=req.fast_mode,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/traces")
def get_traces(limit: int = 50) -> Dict[str, Any]:
    """Retrieve recent automation traces from SQLite."""
    return {"runs": trace_store.list_runs(limit=limit)}


@app.post("/voice/task")
def voice_task(req: VoiceDataRequest) -> Dict[str, Any]:
    """Ingest base64-encoded audio, transcribe via Whistle, and execute."""
    try:
        audio_bytes = base64.b64decode(req.audio_b64)
        return voice_manager.ingest_voice_task(audio_data=audio_bytes, hwnd=req.hwnd, mode=req.mode)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/hud/overlay")
def get_hud_overlay(hwnd: Optional[int] = None, highlight_id: Optional[int] = None) -> Response:
    """Return real-time SVG overlay for visual grounding of detected elements."""
    try:
        scene_data = execute_sense_scene(hwnd=hwnd)
        scene = SceneIndex(**scene_data)
        svg_content = hud_overlay.generate_svg_overlay(scene, highlight_id=highlight_id)
        return Response(content=svg_content, media_type="image/svg+xml")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


def main() -> None:
    """CLI entrypoint for running the Servo daemon or MCP server."""
    import argparse
    parser = argparse.ArgumentParser(description="Servo Desktop Control Runtime Daemon")
    parser.add_argument("--mcp", action="store_true", help="Run as FastMCP server over stdio")
    parser.add_argument("--port", type=int, default=8000, help="Port for REST server (default: 8000)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host address (default: 127.0.0.1)")
    args = parser.parse_args()

    if args.mcp:
        if mcp is None:
            print("FastMCP is not installed. Install with: pip install fastmcp")
            sys.exit(1)
        worker.start()
        try:
            mcp.run()
        finally:
            worker.stop()
    else:
        import uvicorn
        uvicorn.run("servo.daemon.server:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
