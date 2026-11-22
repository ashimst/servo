"""Servo Copilot: floating web UI + session runtime.

* A small FastAPI app on 127.0.0.1 serves the UI and streams trace events over a
  WebSocket. Every request needs a per-launch random token embedded in the page, so
  other local web pages cannot drive the desktop through it.
* The window is pywebview (frameless, always on top) when installed, otherwise
  Microsoft Edge in ``--app`` mode pinned topmost via Win32.
* Runs go through ``CascadeRouter``; risky actions ask for confirmation inside the UI.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from servo import __version__
from servo.cognition import browser as browser_mod
from servo.cognition import cactus_runtime
from servo.cognition.router import CascadeRouter, RunOutcome, new_run_id
from servo.cognition.system2 import System2, list_openai_models
from servo.cognition.trace import TraceEvent, Tracer, bus
from servo.config import settings
from servo import user_settings
from servo.control.confirmation import ConfirmationRequest
from servo.control.jev_gate import JevGate
from servo.daemon.auth import LocalHostGuard
from servo.daemon.scheduler import Scheduler
from servo.perception import windows

logger = logging.getLogger("servo.copilot")

WINDOW_TITLE = "Servo Copilot"
UI_FILE = Path(__file__).with_name("copilot_ui") / "index.html"


# =========================================================== foreground tracking

class ForegroundTracker:
    """Remembers the last foreground window that is not the copilot itself.

    When you type into the copilot, the copilot has focus; "click Save" must act on the
    window you were using before, never on the copilot.
    """

    def __init__(self, is_own: Callable[[int], bool], interval: float = 0.3) -> None:
        self.is_own = is_own
        self.interval = interval
        self.last_hwnd: Optional[int] = None
        self.last_title: str = ""
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if sys.platform != "win32" or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="servo-fg", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def observe(self) -> None:
        hwnd = windows.get_foreground_window()
        if not hwnd or not windows.is_window(hwnd) or self.is_own(hwnd):
            return
        title = windows.get_window_title(hwnd)
        if title and title not in ("Program Manager",):
            self.last_hwnd, self.last_title = hwnd, title

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.observe()
            except Exception:
                pass
            self._stop.wait(self.interval)


# ===================================================================== session

class CopilotSession:
    def __init__(self, router_factory: Optional[Callable[..., Any]] = None) -> None:
        from servo.daemon.copilot import CopilotBrain, launch_or_focus_app
        from servo.cognition.memory import chat_memory

        self.token = secrets.token_urlsafe(24)
        self.brain = CopilotBrain()
        self.memory = chat_memory
        self.launcher = launch_or_focus_app
        self.router_factory = router_factory or CascadeRouter
        self.abort = threading.Event()
        self.busy = False
        self.current_run: Optional[str] = None
        self.history: List[Dict[str, str]] = self.memory.get_recent_history()
        self.context = "desktop"
        self.target_hwnd: Optional[int] = None
        self._lock = threading.Lock()
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._confirms: Dict[str, Dict[str, Any]] = {}
        self.step_mode = False
        self.tracker = ForegroundTracker(self.is_own_window)
        self._unsub = bus.subscribe(self._on_trace)
        # Queue + schedule. The firing thread starts in run_copilot(), not here.
        self.scheduler = Scheduler(self._start_scheduled, notify=self._notice)
        # Clicks and screenshots under the copilot panel move it aside first.
        from servo.actuation.overlay import register_own_windows
        register_own_windows(self.own_hwnds)

    # ---------------------------------------------------------------- plumbing

    @staticmethod
    def is_own_window(hwnd: int) -> bool:
        # Also treats a browser tab showing the copilot page as "own", so commands never
        # target the copilot itself, wherever it is displayed.
        try:
            return WINDOW_TITLE in windows.get_window_title(hwnd)
        except Exception:
            return False

    def own_hwnds(self) -> Set[int]:
        out: Set[int] = set()
        try:
            for w in windows.list_windows():
                if WINDOW_TITLE in str(w.get("title") or ""):
                    out.add(int(w["hwnd"]))
        except Exception:
            pass
        return out

    def add_listener(self, fn: Callable[[Dict[str, Any]], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(fn)

        def _remove() -> None:
            with self._lock:
                if fn in self._listeners:
                    self._listeners.remove(fn)
        return _remove

    def broadcast(self, msg: Dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(msg)
            except Exception:
                pass

    def _on_trace(self, ev: TraceEvent) -> None:
        self.broadcast({"type": "trace", "event": ev.model_dump()})

    # ------------------------------------------------------------------ state

    def target(self) -> Dict[str, Any]:
        hwnd = self.target_hwnd if self.target_hwnd and windows.is_window(self.target_hwnd) else self.tracker.last_hwnd
        title = windows.get_window_title(hwnd) if hwnd and windows.is_window(hwnd) else ""
        return {"hwnd": hwnd, "title": title, "context": self.context}

    def capabilities(self) -> Dict[str, Any]:
        cactus = cactus_runtime.status()
        kev = JevGate()
        s2 = System2(user_name=self.brain.user_name).describe()

        def cactus_detail(key: str, label: str) -> str:
            if cactus.get(f"{key}_error"):
                return str(cactus[f"{key}_error"])
            if not cactus.get("installed"):
                return "pip install servo[cactus]"
            if not cactus.get(key):
                return f"installed cactus package has no {label} (need cactus-compute >= 3.3)"
            return f"cactus-compute {cactus.get('version') or ''}".strip()

        mirage_avail = bool(cactus.get("mirage")) or bool(cactus.get("windows_ocr"))
        mirage_detail = "cactus-compute (Mirage)" if cactus.get("mirage") else ("Windows Media OCR (Native WinRT)" if cactus.get("windows_ocr") else cactus_detail("mirage", "Mirage"))

        return {
            "needle": {"available": bool(cactus.get("needle")), "detail": cactus_detail("needle", "Needle"),
                       "threshold": settings.needle_confidence_threshold},
            "mirage": {"available": mirage_avail, "detail": mirage_detail},
            "kev": {"available": kev.model_enabled, "detail": settings.kev_url or "heuristic gate (set SERVO_KEV_URL)"},
            "browser": {"available": browser_mod.playwright_installed(), "detail": f"Playwright · {settings.browser_channel}"},
            "system2": {"available": s2["available"], "detail": f"{s2['provider']} · {s2['model']}".strip(" ·")},
        }

    def state(self) -> Dict[str, Any]:
        s2 = System2(user_name=self.brain.user_name)
        return {
            "version": __version__,
            "setup_done": user_settings.setup_done(),
            "reasoning": {
                "provider": s2.provider,
                "label": provider_label(s2.provider),
                "model": s2.describe()["model"],
                "ready": s2.available(),
                "problem": "" if s2.available() else s2.unavailable_reason(),
            },
            "user_name": self.brain.user_name,
            "busy": self.busy,
            "step_mode": self.step_mode,
            "run_id": self.current_run,
            "mode": settings.mode,
            "target": self.target(),
            "capabilities": self.capabilities(),
        }

    # -------------------------------------------------------------------- runs

    def submit(self, text: str) -> Dict[str, Any]:
        """Front door for a typed/spoken request: schedule, queue, or run now.

        Returns {"run_id": id} when a run started, or {"notice": text, "queued": bool}
        when the request was scheduled, queued behind the current run, or was a
        schedule-management command ("list scheduled tasks", "cancel ab12cd").
        """
        text = text.strip()
        if not text:
            raise ValueError("Type a request first.")
        reply = self.scheduler.handle_command(text)
        if reply is not None:
            self.broadcast({"type": "notice", "text": text, "reply": reply})
            return {"run_id": None, "notice": reply, "queued": False}
        goal = self.scheduler.queue_request(text) or text
        if self.busy:
            job = self.scheduler.enqueue(goal)
            ahead = sum(1 for j in self.scheduler.jobs() if j.kind == "queued") - 1
            reply = f"Queued [{job.id}]: it starts when the current request finishes" + (
                f" ({ahead} ahead of it)." if ahead else ".")
            self.broadcast({"type": "notice", "text": text, "reply": reply})
            return {"run_id": None, "notice": reply, "queued": True}
        try:
            return {"run_id": self.send(goal)}
        except RuntimeError:  # lost a race with a run that just started
            job = self.scheduler.enqueue(goal)
            reply = f"Queued [{job.id}]: it starts when the current request finishes."
            self.broadcast({"type": "notice", "text": text, "reply": reply})
            return {"run_id": None, "notice": reply, "queued": True}

    def _start_scheduled(self, goal: str) -> bool:
        """Scheduler callback: start ``goal`` if idle; False means try again later."""
        if self.busy:
            return False
        try:
            self.send(goal)
            return True
        except RuntimeError:
            return False

    def _notice(self, reply: str) -> None:
        self.broadcast({"type": "notice", "text": "", "reply": reply})

    def send(self, text: str) -> str:
        text = text.strip()
        if not text:
            raise ValueError("Type a request first.")
        with self._lock:
            if self.busy:
                raise RuntimeError("A request is already running. Stop it or wait for it to finish.")
            self.busy = True
            self.abort.clear()
            run_id = new_run_id()
            self.current_run = run_id
        # The router records both turns (redacted, with route); recording here too saved
        # every turn twice and fed duplicates to System 2.
        self.broadcast({"type": "run_started", "run_id": run_id, "text": text})
        threading.Thread(target=self._run, args=(run_id, text), name=f"servo-run-{run_id}", daemon=True).start()
        return run_id

    def _run(self, run_id: str, text: str) -> None:
        com = False
        if sys.platform == "win32":
            try:
                import pythoncom  # type: ignore
                pythoncom.CoInitialize()
                com = True
            except Exception:
                pass
        tracer = Tracer(run_id)
        outcome: Optional[RunOutcome] = None
        try:
            self.tracker.observe()
            router = self.router_factory(
                tracer=tracer,
                user_name=self.brain.user_name,
                brain=self.brain,
                should_abort=self.abort,
                confirmation_callback=self._confirm,
                ask=self.ask,
                step_mode=self.step_mode,
                app_launcher=self.launcher,
                target_hwnd=self.target_hwnd if self.target_hwnd and windows.is_window(self.target_hwnd) else self.tracker.last_hwnd,
                exclude_hwnds=self.own_hwnds,
                history=list(self.history),
                context=self.context,
            )
            self._active_router = router
            outcome = router.run(text)
            self.target_hwnd = getattr(router, "hwnd", None) or self.target_hwnd
            self.context = getattr(router, "context", self.context)
            self.brain.active_hwnd = self.target_hwnd
            if outcome is not None:
                self.history = self.memory.get_recent_history()
        except Exception as e:
            logger.exception("run failed")
            tracer.emit("error", "Run crashed", f"{e.__class__.__name__}: {e}", tier="copilot", status="fail")
            tracer.emit("result", f"Something went wrong: {e}", tier="copilot", status="fail")
        finally:
            with self._lock:
                self.busy = False
                self.current_run = None
            for c in list(self._confirms.values()):
                c["answer"] = c["answer"] or "stop"
                c["event"].set()
            self.broadcast({"type": "run_finished", "run_id": run_id,
                            "outcome": outcome.model_dump() if outcome else None, "state": self.state()})
            if com:
                try:
                    import pythoncom  # type: ignore
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

    def stop(self) -> None:
        self.abort.set()
        for c in list(self._confirms.values()):
            c["answer"] = "stop"
            c["event"].set()
        self.broadcast({"type": "stopping", "run_id": self.current_run})

    # ------------------------------------------------------------ asking you

    def ask(self, kind: str, payload: Dict[str, Any], options: List[str], timeout: Optional[float] = -1) -> str:
        """Show buttons in the copilot and wait for one to be pressed.

        kind: "confirm" (proceed / skip / stop), "step" (next / run_all / stop) or
        "continue" (continue / stop). timeout -1 = the confirmation timeout from
        settings; None = wait until answered or stopped. Returns "" on timeout.
        """
        cid = secrets.token_hex(6)
        slot: Dict[str, Any] = {"event": threading.Event(), "answer": ""}
        self._confirms[cid] = slot
        wait_s = settings.confirmation_timeout_seconds if timeout == -1 else timeout
        self.broadcast({"type": "ask", "id": cid, "run_id": self.current_run, "kind": kind,
                        "payload": payload, "options": options, "timeout": wait_s})
        try:
            slot["event"].wait(wait_s)
            answer = slot["answer"] if slot["answer"] in options else ""
            if self.abort.is_set():
                answer = "stop" if "stop" in options else ""
            return answer
        finally:
            self._confirms.pop(cid, None)
            self.broadcast({"type": "ask_closed", "id": cid, "answer": slot["answer"] or "timeout"})

    def answer(self, cid: str, choice: str) -> bool:
        slot = self._confirms.get(cid)
        if slot is None:
            return False
        slot["answer"] = choice
        slot["event"].set()
        return True

    def _confirm(self, req: ConfirmationRequest) -> bool:
        """Confirmation for risky actions.

        For high-risk tasks, explicitly prompts the user with both an interactive
        chat prompt and an OS-level top-most modal popup dialog so the user is directly
        alerted on their desktop even if the Copilot window is unfocused.
        """
        if req.risk_level == "high" and sys.platform == "win32":
            from servo.control.confirmation import DesktopDialogConfirmation
            popup_dialog = DesktopDialogConfirmation(timeout_seconds=30.0)
            popup_res: List[Optional[bool]] = [None]

            def _show_popup():
                try:
                    popup_res[0] = popup_dialog(req)
                except Exception as e:
                    logger.debug("Desktop confirmation popup error: %s", e)

            t = threading.Thread(target=_show_popup, name="servo-popup-confirm", daemon=True)
            t.start()

            choice = self.ask("confirm", req.model_dump(), ["proceed", "skip", "stop"], timeout=30.0)
            if choice == "proceed":
                return True
            if choice in ("skip", "stop"):
                return False

            t.join(timeout=0.5)
            if popup_res[0] is not None:
                return bool(popup_res[0])
            return False

        return self.ask("confirm", req.model_dump(), ["proceed", "skip", "stop"]) == "proceed"


# ======================================================================== app

class SendBody(BaseModel):
    text: str


class CancelBody(BaseModel):
    id: Optional[str] = None  # None = cancel everything


class VoiceTranscribeBody(BaseModel):
    audio_b64: str
    auto_send: bool = False


class ConfigBody(BaseModel):
    provider: str
    model: Optional[str] = None
    api_key: Optional[str] = None   # None = keep the saved key; "" = remove it
    base_url: Optional[str] = None
    kev_url: Optional[str] = None


PROVIDER_LABELS = {"anthropic": "Claude", "openai": "Ollama / OpenAI-compatible", "gemini": "Gemini",
                   "cactus": "Cactus (on-device)", "none": "Off"}


def provider_label(p: str) -> str:
    if p == "openai" and "11434" in (settings.openai_base_url or ""):
        return "Ollama"
    return PROVIDER_LABELS.get(p, p)


def config_updates(body: ConfigBody) -> Dict[str, Any]:
    p = body.provider.strip().lower()
    if p not in PROVIDER_LABELS:
        raise ValueError(f"Unknown provider '{body.provider}'.")
    up: Dict[str, Any] = {"planner_provider": p}
    model = (body.model or "").strip() or None
    if p == "anthropic":
        up["anthropic_model"] = model or settings.anthropic_model
        up["anthropic_api_key"] = body.api_key
    elif p == "openai":
        up["openai_model"] = model or settings.openai_model
        up["openai_base_url"] = (body.base_url or "").strip() or settings.openai_base_url
        up["openai_api_key"] = body.api_key
    elif p == "gemini":
        up["gemini_model"] = model or settings.gemini_model
        up["gemini_api_key"] = body.api_key
    if body.kev_url is not None:
        up["kev_url"] = body.kev_url.strip()
    return up


def config_view() -> Dict[str, Any]:
    ollama_models = list_openai_models("http://localhost:11434/v1")
    return {
        "provider": settings.planner_provider,
        "setup_done": user_settings.setup_done(),
        "anthropic": {"model": settings.anthropic_model, "key": user_settings.key_hint(settings.anthropic_api_key)
                      or ("from environment" if os.environ.get("ANTHROPIC_API_KEY") else ""),
                      "sdk": _has_module("anthropic")},
        "openai": {"model": settings.openai_model, "base_url": settings.openai_base_url,
                   "key": user_settings.key_hint(settings.openai_api_key)},
        "gemini": {"model": settings.gemini_model, "key": user_settings.key_hint(settings.gemini_api_key)},
        "ollama": {"running": ollama_models is not None, "models": ollama_models or []},
        "kev_url": settings.kev_url,
    }


def _has_module(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


class AnswerBody(BaseModel):
    id: str
    choice: str


class StepModeBody(BaseModel):
    on: bool


def create_app(session: CopilotSession) -> FastAPI:
    app = FastAPI(title="Servo Copilot", docs_url=None, redoc_url=None, openapi_url=None)
    # The page at / embeds the session token, so a DNS-rebound origin must not reach it.
    app.add_middleware(LocalHostGuard)

    def check(token: Optional[str]) -> None:
        if not token or not secrets.compare_digest(token, session.token):
            raise HTTPException(status_code=403, detail="Bad or missing copilot token.")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        html = UI_FILE.read_text(encoding="utf-8")
        return html.replace("__SERVO_TOKEN__", session.token)

    @app.get("/api/state")
    def get_state(x_servo_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        check(x_servo_token)
        return session.state()

    @app.get("/api/memory")
    def get_memory(x_servo_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        check(x_servo_token)
        return {
            "entries_count": len(session.memory.entries),
            "summary": session.memory.summary.model_dump(),
            "recent_turns": session.memory.get_recent_history(limit=15),
            "storage_path": str(session.memory.storage_path),
        }

    @app.post("/api/memory/clear")
    def clear_memory(x_servo_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        check(x_servo_token)
        session.memory.clear()
        session.history = []
        return {"ok": True, "message": "Chat memory cleared."}

    @app.post("/api/send")
    def send(body: SendBody, x_servo_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        check(x_servo_token)
        try:
            return session.submit(body.text)
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=409, detail=str(e))

    @app.get("/api/runs")
    def get_runs(limit: int = 50, failed: bool = False,
                 x_servo_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        check(x_servo_token)
        from servo.telemetry.run_record import list_runs
        return {"runs": list_runs(limit=max(1, min(limit, 500)), failed_only=failed)}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, x_servo_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        check(x_servo_token)
        from servo.telemetry.run_record import load_run
        rec = load_run(run_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"No run {run_id}.")
        return rec

    @app.get("/api/runs/{run_id}/diagnose")
    def diagnose_run(run_id: str, explain: bool = False,
                     x_servo_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        check(x_servo_token)
        from servo.cognition.diagnose import diagnose
        d = diagnose(run_id, explain=explain)
        if not d.found:
            raise HTTPException(status_code=404, detail=d.cause)
        return {**d.model_dump(), "text": d.to_text()}

    @app.get("/api/schedule")
    def get_schedule(x_servo_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        check(x_servo_token)
        return {"jobs": [dict(j.model_dump(), description=j.describe()) for j in session.scheduler.jobs()]}

    @app.post("/api/schedule/cancel")
    def cancel_schedule(body: CancelBody, x_servo_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        check(x_servo_token)
        if body.id:
            job = session.scheduler.cancel(body.id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"No scheduled task with id {body.id}.")
            return {"ok": True, "cancelled": 1}
        return {"ok": True, "cancelled": session.scheduler.clear()}

    @app.get("/api/voice/status")
    def voice_status(x_servo_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        check(x_servo_token)
        backend = "none"
        available = False
        try:
            import cactus  # type: ignore
            if hasattr(cactus, "transcribe") or hasattr(cactus, "Whistle"):
                backend = "whistle"
                available = True
        except ImportError:
            pass
        if not available:
            try:
                import faster_whisper  # type: ignore
                backend = "faster_whisper"
                available = True
            except ImportError:
                pass
        return {
            "available": available,
            "backend": backend,
            "local": True,
        }

    @app.post("/api/voice/transcribe")
    def voice_transcribe(body: VoiceTranscribeBody, x_servo_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        check(x_servo_token)
        if not body.audio_b64:
            raise HTTPException(status_code=400, detail="Empty audio data.")
        try:
            audio_bytes = base64.b64decode(body.audio_b64, validate=True)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid base64 audio: {e}")
        try:
            from servo.daemon.voice import voice_manager, VoiceUnavailableError
            text = voice_manager.transcribe_audio(audio_bytes)
            result: Dict[str, Any] = {"run_id": None}
            if body.auto_send and text:
                result = session.submit(text)
            return {"text": text, "status": "ok", **result}
        except VoiceUnavailableError as e:
            if "empty" in str(e).lower():
                return {"text": "", "status": "ok", "run_id": None}
            raise HTTPException(status_code=503, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"ASR transcription error: {e}")

    @app.post("/api/stop")
    def stop(x_servo_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        check(x_servo_token)
        session.stop()
        return {"ok": True}

    @app.get("/api/graph")
    def get_graph(x_servo_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        check(x_servo_token)
        from servo.cognition.graph_memory import graph_memory
        import collections
        return {
            "version": graph_memory.graph.version,
            "generated_at": graph_memory.graph.generated_at,
            "stats": {
                "nodes_count": len(graph_memory.graph.nodes),
                "edges_count": len(graph_memory.graph.edges),
                "types": dict(collections.Counter(n.type for n in graph_memory.graph.nodes.values())),
            },
            "nodes": [n.model_dump() for n in graph_memory.graph.nodes.values()],
            "edges": [e.model_dump() for e in graph_memory.graph.edges],
        }

    @app.get("/api/graph/consistency")
    def get_graph_consistency(x_servo_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        check(x_servo_token)
        from servo.cognition.graph_memory import graph_memory
        return graph_memory.audit_consistency()

    @app.post("/api/graph/rebuild")
    def rebuild_graph(x_servo_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        check(x_servo_token)
        from servo.cognition.graph_memory import graph_memory
        g = graph_memory.rebuild_graph()
        return {
            "ok": True,
            "nodes_count": len(g.nodes),
            "edges_count": len(g.edges),
            "generated_at": g.generated_at,
        }

    @app.get("/api/config")
    def get_config(x_servo_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        check(x_servo_token)
        return config_view()

    @app.post("/api/config/test")
    def test_config(body: ConfigBody, x_servo_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        """Try the given settings without saving them."""
        check(x_servo_token)
        try:
            updates = config_updates(body)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        saved = {k: getattr(settings, k) for k in updates}
        try:
            for k, v in updates.items():
                if v is not None:
                    setattr(settings, k, v)
            return System2(provider=updates["planner_provider"]).probe()
        finally:
            for k, v in saved.items():
                setattr(settings, k, v)

    @app.post("/api/config")
    def save_config(body: ConfigBody, x_servo_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        check(x_servo_token)
        try:
            user_settings.save(settings, config_updates(body))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        state = session.state()
        session.broadcast({"type": "state", "state": state})
        return state

    @app.post("/api/answer")
    def answer(body: AnswerBody, x_servo_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        check(x_servo_token)
        return {"ok": session.answer(body.id, body.choice)}

    @app.post("/api/step_mode")
    def step_mode(body: StepModeBody, x_servo_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        """Pause before every step. Applies to the current run and later ones."""
        check(x_servo_token)
        session.step_mode = body.on
        router = getattr(session, "_active_router", None)
        if router is not None and session.busy:
            router.step_mode = body.on   # also applies to the run in progress
        return {"step_mode": session.step_mode}

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        token = socket.query_params.get("token")
        if not token or not secrets.compare_digest(token, session.token):
            await socket.close(code=4403)
            return
        await socket.accept()
        loop = asyncio.get_running_loop()
        queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
        remove = session.add_listener(lambda m: loop.call_soon_threadsafe(queue.put_nowait, m))
        try:
            await socket.send_json({"type": "hello", "state": session.state()})
            while True:
                msg = await queue.get()
                await socket.send_json(msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            remove()

    return app


# ===================================================================== window

WIN_W, WIN_H = 420, 760  # in DIPs (scaled by the window's DPI when placed)


def place_window(hwnd: int, width: int = WIN_W, height: int = WIN_H, topmost: bool = True) -> Optional[tuple]:
    """Dock the window to the right edge of its monitor's work area, in PHYSICAL pixels.

    Browsers and pywebview interpret positions in DPI-scaled units, which put the window
    partly off-screen on scaled displays (e.g. 125%). Measuring the monitor and the
    window's own DPI here avoids that, and the size is clamped so the composer at the
    bottom is always visible.
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    try:
        dpi = user32.GetDpiForWindow(hwnd) or 96
    except Exception:
        dpi = 96
    scale = dpi / 96.0
    mon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not mon or not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
        return None
    work = info.rcWork
    margin = int(12 * scale)
    work_w, work_h = work.right - work.left, work.bottom - work.top
    w = min(int(width * scale), work_w - 2 * margin)
    h = min(int(height * scale), work_h - 2 * margin)
    x = work.right - w - margin
    y = work.top + (work_h - h) // 2
    insert_after = -1 if topmost else 0  # HWND_TOPMOST / HWND_TOP
    # Only un-maximize when needed: SW_RESTORE on a normal Edge window makes Edge re-apply
    # its remembered bounds, silently undoing the move below.
    if user32.IsZoomed(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetWindowPos(hwnd, insert_after, x, y, w, h, 0x0040)  # SWP_SHOWWINDOW
    return x, y, w, h


def place_window_stable(hwnd: int, attempts: int = 12, topmost: bool = True) -> bool:
    """Place, then re-place until the window stays put.

    Edge restores its remembered bounds after the page loads, which can undo the first
    placement; keep checking for a few seconds.
    """
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    stable = 0
    for _ in range(attempts):
        target = place_window(hwnd, topmost=topmost)
        time.sleep(0.35)
        if not target or not windows.is_window(hwnd):
            return False
        r = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        if abs(r.left - target[0]) <= 2 and abs(r.top - target[1]) <= 2 and abs((r.right - r.left) - target[2]) <= 2:
            stable += 1
            if stable >= 3:
                return True
        else:
            stable = 0
    return False


# Processes that can host the copilot window: Edge app mode, or pywebview (this Python).
_HOST_PROCESSES = ("msedge.exe", os.path.basename(sys.executable).lower(), "python.exe", "pythonw.exe")


def find_window(title: str, timeout: float = 15.0) -> Optional[int]:
    """Find OUR copilot window: exact title, owned by Edge or this Python.

    Never matches a browser tab that merely shows the copilot page (e.g. "Profile 1:
    Servo Copilot" in another browser), so placement can't move the user's windows.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for w in windows.list_windows():
            if str(w.get("title") or "") == title and str(w.get("process") or "").lower() in _HOST_PROCESSES:
                return int(w["hwnd"])
        time.sleep(0.15)
    return None


def _start_server(app: FastAPI, port: int) -> Any:
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, name="servo-copilot-http", daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return server


def _free_port(preferred: int) -> int:
    import socket
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    return preferred


def run_copilot(open_window: bool = True, port: Optional[int] = None) -> None:
    from servo.daemon.worker import worker

    worker.start()
    session = CopilotSession()
    session.tracker.start()
    session.scheduler.start()
    app = create_app(session)
    port = _free_port(port or settings.copilot_port)
    server = _start_server(app, port)
    url = f"http://127.0.0.1:{port}/"
    print(f"Servo Copilot UI: {url}", file=sys.stderr)

    try:
        if not open_window:
            _wait_forever()
            return
        try:
            import webview  # type: ignore
        except ImportError:
            webview = None
        if webview is not None:
            holder: Dict[str, Any] = {}

            class Api:
                def close(self) -> None:
                    holder["w"].destroy()

                def minimize(self) -> None:
                    holder["w"].minimize()

                def pin(self, on: bool) -> bool:
                    holder["w"].on_top = bool(on)
                    return bool(on)

                def dock(self) -> None:
                    hwnd = find_window(WINDOW_TITLE, timeout=2)
                    if hwnd:
                        place_window(hwnd, topmost=bool(holder["w"].on_top))

            holder["w"] = webview.create_window(
                WINDOW_TITLE, url, width=WIN_W, height=WIN_H, on_top=True, frameless=True,
                easy_drag=False, js_api=Api(), min_size=(340, 480), background_color="#151923",
            )

            def _on_shown() -> None:
                hwnd = find_window(WINDOW_TITLE, timeout=5)
                if hwnd:
                    place_window_stable(hwnd)

            holder["w"].events.shown += _on_shown
            webview.start()
            return
        edge = _find_edge()
        if edge:
            from servo.paths import servo_home
            profile = servo_home() / "copilot-shell"
            _ensure_edge_mic_permission(profile, url)
            proc = subprocess.Popen([
                edge, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
                "--disable-features=Translate",
                f"--unsafely-treat-insecure-origin-as-secure={url.rstrip('/')}",
                "--autoplay-policy=no-user-gesture-required",
                "--enable-features=SpeechRecognition",
                "--use-fake-ui-for-media-stream",
            ])
            hwnd = find_window(WINDOW_TITLE)
            if hwnd:
                place_window_stable(hwnd)
            while proc.poll() is None or (hwnd and windows.is_window(hwnd)):
                time.sleep(0.5)
            return
        import webbrowser
        webbrowser.open(url)
        _wait_forever()
    finally:
        session.stop()
        session.tracker.stop()
        server.should_exit = True
        try:
            browser_mod.browser_tier.close()
        except Exception:
            pass
        worker.stop()


def _ensure_edge_mic_permission(profile_dir: Path, url: str) -> None:
    """Pre-grant microphone permission in the isolated Edge copilot profile so voice input never blocks."""
    try:
        import json
        from urllib.parse import urlparse
        parsed = urlparse(url)
        port = parsed.port or 8765
        pref_file = profile_dir / "Default" / "Preferences"
        pref_file.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if pref_file.exists():
            try:
                data = json.loads(pref_file.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        content_settings = data.setdefault("profile", {}).setdefault("content_settings", {})
        exceptions = content_settings.setdefault("exceptions", {})
        mic = exceptions.setdefault("media_stream_mic", {})
        mic[f"http://127.0.0.1:{port},*"] = {"setting": 1}
        mic[f"http://localhost:{port},*"] = {"setting": 1}
        mic["http://127.0.0.1:*"] = {"setting": 1}
        mic["http://localhost:*"] = {"setting": 1}
        default_settings = data.setdefault("profile", {}).setdefault("default_content_setting_values", {})
        default_settings["media_stream_mic"] = 1
        pref_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug("Could not pre-configure Edge mic permissions: %s", e)


def _find_edge() -> Optional[str]:
    if sys.platform != "win32":
        return None
    from servo.daemon.copilot import resolve_app_executable
    path = resolve_app_executable("msedge")
    return path if os.path.isabs(path) and os.path.exists(path) else None


def _wait_forever() -> None:
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
