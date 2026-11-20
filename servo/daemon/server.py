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
from fastapi.responses import HTMLResponse, Response
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


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Servo Desktop Control Center</title>
<style>
  :root { --bg: #0f172a; --panel: #1e293b; --border: #334155; --text: #f8fafc; --muted: #94a3b8; --accent: #3b82f6; --success: #10b981; }
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  body { background: var(--bg); color: var(--text); padding: 24px; }
  .container { max-width: 1200px; margin: 0 auto; display: flex; flex-direction: column; gap: 20px; }
  .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 16px; }
  .badge { background: #064e3b; color: #34d399; padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 18px; }
  .card h2 { font-size: 15px; margin-bottom: 12px; color: #e2e8f0; font-weight: 600; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  @media(max-width: 800px) { .grid { grid-template-columns: 1fr; } }
  label { display: block; font-size: 11px; color: var(--muted); margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px; }
  input, select { width: 100%; background: #0b1120; border: 1px solid var(--border); border-radius: 6px; padding: 9px 12px; color: #fff; font-size: 13px; }
  input:focus, select:focus { outline: none; border-color: var(--accent); }
  .btn { background: var(--accent); color: #fff; border: none; border-radius: 6px; padding: 9px 16px; font-size: 13px; font-weight: 600; cursor: pointer; }
  .btn:hover { background: #2563eb; }
  .btn-outline { background: transparent; border: 1px solid var(--border); color: #cbd5e1; }
  .btn-outline:hover { background: #334155; }
  .row { display: flex; gap: 10px; align-items: center; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th, td { text-align: left; padding: 8px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-size: 11px; text-transform: uppercase; }
  tr:hover { background: rgba(255,255,255,0.03); cursor: pointer; }
  .table-box { max-height: 260px; overflow-y: auto; }
  pre { background: #090d16; border: 1px solid var(--border); border-radius: 6px; padding: 12px; font-family: Consolas, monospace; font-size: 12px; color: #38bdf8; max-height: 180px; overflow: auto; }
  .svg-box { border: 1px dashed var(--border); border-radius: 6px; min-height: 240px; display: flex; align-items: center; justify-content: center; background: #0b1120; overflow: auto; }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div>
      <h1>Servo Control Center</h1>
      <p style="color:var(--muted); font-size:13px; margin-top:4px;">Local-First, Closed-Loop Neuro-Symbolic Desktop Control</p>
    </div>
    <span class="badge" id="status-badge">Online</span>
  </div>

  <div class="card">
    <div class="row">
      <div style="flex:1;">
        <label>REST API Bearer Token</label>
        <input type="password" id="token-input" placeholder="Paste ~/.servo/api_token or SERVO_API_TOKEN">
      </div>
      <button class="btn btn-outline" style="margin-top:20px;" onclick="saveToken()">Save Token</button>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <h2>1. Select Target Window</h2>
      <div class="row" style="margin-bottom:12px;">
        <select id="win-select"></select>
        <button class="btn btn-outline" onclick="loadWindows()">Refresh</button>
      </div>
      <div class="row">
        <button class="btn" onclick="inspectWindow()">Inspect Elements & View Overlay</button>
      </div>
    </div>

    <div class="card">
      <h2>2. Execute Goal / Action</h2>
      <div style="margin-bottom:12px;">
        <label>Instruction</label>
        <input type="text" id="goal-input" value='Click "File"'>
      </div>
      <div class="row">
        <button class="btn" style="background:var(--success);" onclick="runGoal()">Execute Action</button>
        <span id="run-status" style="font-size:12px; color:var(--muted);"></span>
      </div>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <h2>Visual HUD Grounding (SVG Overlay)</h2>
      <div class="svg-box" id="overlay-container">
        <span style="color:var(--muted); font-size:13px;">Click 'Inspect' to load visual overlay</span>
      </div>
    </div>

    <div class="card">
      <h2>Detected UI Elements (Scene Index)</h2>
      <div class="table-box">
        <table>
          <thead><tr><th>ID</th><th>Label</th><th>Role</th><th>Screen BBox</th></tr></thead>
          <tbody id="elements-body"><tr><td colspan="4" style="color:var(--muted); text-align:center;">No elements loaded</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>Execution Trace & Telemetry</h2>
    <pre id="trace-output">// Execution traces and feedback will appear here...</pre>
  </div>
</div>

<script>
  let token = localStorage.getItem('servo_token') || '';
  if (token) document.getElementById('token-input').value = token;

  function saveToken() {
    token = document.getElementById('token-input').value.trim();
    localStorage.setItem('servo_token', token);
    alert('Token saved locally!');
    loadWindows();
  }

  function getHeaders() {
    return token ? { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' } : { 'Content-Type': 'application/json' };
  }

  async function loadWindows() {
    try {
      const res = await fetch('/windows', { headers: getHeaders() });
      if (!res.ok) throw new Error('Auth failed. Check token.');
      const data = await res.json();
      const sel = document.getElementById('win-select');
      sel.innerHTML = '';
      (data.windows || []).forEach(w => {
        const opt = document.createElement('option');
        opt.value = w.hwnd;
        opt.textContent = `[HWND ${w.hwnd}] ${w.process_name} - ${w.title || 'Untitled'}`;
        sel.appendChild(opt);
      });
    } catch(e) {
      document.getElementById('trace-output').textContent = 'Error: ' + e.message;
    }
  }

  async function inspectWindow() {
    const hwnd = document.getElementById('win-select').value;
    if (!hwnd) return;
    try {
      // Load Elements
      const res = await fetch('/scene?hwnd=' + hwnd, { headers: getHeaders() });
      const scene = await res.json();
      const tbody = document.getElementById('elements-body');
      tbody.innerHTML = '';
      const elements = Object.values(scene.elements || {});
      elements.forEach(el => {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td>${el.element_id}</td><td><b>${el.label || ''}</b></td><td>${el.role}</td><td>${el.bbox_screen.join(', ')}</td>`;
        tr.onclick = () => {
          document.getElementById('goal-input').value = el.role === 'Edit' ? `Type "Hello" into "${el.label}"` : `Click "${el.label}"`;
        };
        tbody.appendChild(tr);
      });

      // Load SVG Overlay
      const svgRes = await fetch('/hud/overlay?hwnd=' + hwnd, { headers: getHeaders() });
      const svgText = await svgRes.text();
      document.getElementById('overlay-container').innerHTML = svgText;
    } catch(e) {
      document.getElementById('trace-output').textContent = 'Error: ' + e.message;
    }
  }

  async function runGoal() {
    const hwnd = parseInt(document.getElementById('win-select').value, 10);
    const goal = document.getElementById('goal-input').value.trim();
    if (!hwnd || !goal) return;
    const status = document.getElementById('run-status');
    status.textContent = 'Running closed-loop action...';
    try {
      const res = await fetch('/task', {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({ goal: goal, hwnd: hwnd })
      });
      const data = await res.json();
      status.textContent = data.success ? 'Success!' : 'Completed with warning/failure';
      document.getElementById('trace-output').textContent = JSON.stringify(data, null, 2);
      inspectWindow();
    } catch(e) {
      status.textContent = 'Failed';
      document.getElementById('trace-output').textContent = 'Error: ' + e.message;
    }
  }

  loadWindows();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    """Embedded interactive Web GUI Dashboard for Servo."""
    return _DASHBOARD_HTML


def main() -> None:
    """CLI entrypoint for running the Servo daemon, GUI, or MCP server."""
    import argparse
    import logging

    parser = argparse.ArgumentParser(description="Servo Desktop Control Runtime Daemon")
    parser.add_argument("--copilot", action="store_true", help="Launch floating on-screen desktop Copilot chat companion")
    parser.add_argument("--gui", action="store_true", help="Launch native desktop GUI inspector")
    parser.add_argument("--mcp", action="store_true", help="Run as FastMCP server over stdio")
    parser.add_argument("--port", type=int, default=8000, help="Port for REST server (default: 8000)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host address (default: 127.0.0.1)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    if args.copilot:
        from servo.daemon.copilot import launch_copilot
        launch_copilot()
        return

    if args.gui:
        from servo.daemon.ui import launch_desktop_gui
        launch_desktop_gui()
        return

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
        print(f"Servo Web Dashboard available at: http://{args.host}:{args.port}/", file=sys.stderr)
        import uvicorn
        uvicorn.run("servo.daemon.server:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
