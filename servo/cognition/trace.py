"""Reasoning trace bus.

Every routing decision, perception pass, model call, safety verdict and actuation
emits a ``TraceEvent``. The copilot UI subscribes to the bus and renders the full
reasoning trace live; the router also keeps the events for the run record.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Literal, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("servo.trace")

TraceKind = Literal[
    "goal",        # user goal received
    "route",       # router decision (which tier, why)
    "tier",        # a tier was tried (with confidence)
    "perception",  # UIA / Mirage / DOM sensing result
    "model",       # a model call: prompt, response, reasoning, confidence
    "decision",    # Kev decision (risk, scope, verification)
    "action",      # actuation dispatched
    "verify",      # post-action verification
    "escalate",    # escalation (perception miss / reasoning miss / verify fail)
    "plan",        # System 2 produced or revised subtasks
    "result",      # run finished
    "message",     # assistant chat message
    "error",
]

Tier = Literal["router", "needle", "uia", "browser", "mirage", "kev", "system2", "copilot"]

_seq = itertools.count(1)


class TraceEvent(BaseModel):
    seq: int = Field(default_factory=lambda: next(_seq))
    run_id: str
    ts: float = Field(default_factory=time.time)
    kind: str
    tier: str = "router"
    title: str
    detail: str = ""
    status: Literal["info", "ok", "warn", "fail", "running", "skipped"] = "info"
    confidence: Optional[float] = None
    duration_ms: Optional[int] = None
    data: Dict[str, Any] = Field(default_factory=dict)
    parent: Optional[int] = None


Subscriber = Callable[[TraceEvent], None]


class TraceBus:
    """Thread-safe fan-out of trace events to subscribers (UI sockets, recorders)."""

    def __init__(self) -> None:
        self._subs: List[Subscriber] = []
        self._lock = threading.Lock()

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subs.append(fn)

        def _unsubscribe() -> None:
            with self._lock:
                if fn in self._subs:
                    self._subs.remove(fn)

        return _unsubscribe

    def publish(self, event: TraceEvent) -> None:
        with self._lock:
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(event)
            except Exception as e:  # a broken UI socket must never break a run
                logger.debug("trace subscriber failed: %s", e)


bus = TraceBus()


class Tracer:
    """Run-scoped handle used by the router and tiers to emit events."""

    def __init__(self, run_id: str, target_bus: Optional[TraceBus] = None) -> None:
        self.run_id = run_id
        self.bus = target_bus or bus
        self.events: List[TraceEvent] = []
        self._parents: List[int] = []

    def emit(
        self,
        kind: str,
        title: str,
        detail: str = "",
        *,
        tier: str = "router",
        status: str = "info",
        confidence: Optional[float] = None,
        duration_ms: Optional[int] = None,
        **data: Any,
    ) -> TraceEvent:
        ev = TraceEvent(
            run_id=self.run_id,
            kind=kind,
            tier=tier,
            title=title,
            detail=detail,
            status=status,  # type: ignore[arg-type]
            confidence=None if confidence is None else round(float(confidence), 3),
            duration_ms=duration_ms,
            data=_jsonable(data),
            parent=self._parents[-1] if self._parents else None,
        )
        self.events.append(ev)
        self.bus.publish(ev)
        return ev

    @contextmanager
    def span(self, kind: str, title: str, *, tier: str = "router", **data: Any) -> Iterator["SpanHandle"]:
        """Emit a 'running' event, nest children under it, and close it with timing."""
        t0 = time.perf_counter()
        start = self.emit(kind, title, tier=tier, status="running", **data)
        handle = SpanHandle(start)
        self._parents.append(start.seq)
        try:
            yield handle
        except Exception as e:
            handle.status = "fail"
            handle.detail = handle.detail or f"{e.__class__.__name__}: {e}"
            raise
        finally:
            self._parents.pop()
            closing = start.model_copy(update={
                "status": handle.status,
                "detail": handle.detail or start.detail,
                "confidence": handle.confidence,
                "duration_ms": int((time.perf_counter() - t0) * 1000),
                "data": {**start.data, **_jsonable(handle.data)},
                "ts": time.time(),
            })
            # Same seq: the UI replaces the running row in place.
            self.bus.publish(closing)
            for i, ev in enumerate(self.events):
                if ev.seq == closing.seq:
                    self.events[i] = closing
                    break


class SpanHandle:
    def __init__(self, start: TraceEvent) -> None:
        self.start = start
        self.status = "ok"
        self.detail = ""
        self.confidence: Optional[float] = None
        self.data: Dict[str, Any] = {}

    def done(self, status: str = "ok", detail: str = "", confidence: Optional[float] = None, **data: Any) -> None:
        self.status = status
        if detail:
            self.detail = detail
        if confidence is not None:
            self.confidence = round(float(confidence), 3)
        self.data.update(data)


class NullTracer(Tracer):
    """Tracer that records nothing; default for library callers outside the copilot."""

    def __init__(self) -> None:
        super().__init__("null", TraceBus())


def _jsonable(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str) and len(value) > 20000:
            return value[:20000] + "…[truncated]"
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v, depth + 1) for v in list(value)[:500]]
    if hasattr(value, "model_dump"):
        try:
            return _jsonable(value.model_dump(), depth + 1)
        except Exception:
            pass
    return str(value)
