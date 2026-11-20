"""Structured Trace Payload and Metrics models.

See Section 7 in SERVO_ARCHITECTURE_V1.md.
"""

from __future__ import annotations

import time
import uuid
from typing import List, Optional
from pydantic import BaseModel, Field


class StepTrace(BaseModel):
    """Execution telemetry for a single atomic step."""
    step: int
    sub_goal: str
    perception_source: str
    tool_call: str
    jev_pre_risk: str
    jev_post_ok: bool
    latency_ms: int
    confirmation: Optional[str] = None  # None | "approved" | "rejected"


class RunMetrics(BaseModel):
    """Aggregate execution statistics for an automation run."""
    total_latency_ms: int = 0
    uia_steps: int = 0
    mirage_fallbacks: int = 0
    system2_escalations: int = 0


class TracePayload(BaseModel):
    """Universal structured telemetry contract returned by Servo executions."""
    # Timestamp alone had 1-second resolution, so two runs in the same second shared
    # an ID and the second overwrote the first in SQLite. The random suffix makes it unique.
    run_id: str = Field(default_factory=lambda: f"srv_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}")
    mode: str = "assistant"
    status: str = "passed"  # passed, failed, escalated
    goal: str
    metrics: RunMetrics = Field(default_factory=RunMetrics)
    steps: List[StepTrace] = Field(default_factory=list)
    failure_artifact: Optional[dict] = None
