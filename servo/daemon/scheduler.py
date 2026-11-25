"""Queued and scheduled goals for the copilot.

* Queue: a request sent while another is running waits its turn instead of failing.
* Schedule: "in 10 minutes …", "at 15:30 …", "tomorrow at 9am …",
  "every day at 9:00 …", "every 30 minutes …" run the goal later, through the same
  cascade, gate and confirmations as a typed request.

Jobs persist in ``settings.schedule_file`` (``~/.servo/schedule.json``) and fire only
while the copilot is running; this is not the Windows Task Scheduler. Jobs that came
due while Servo was off are not run late: a one-shot job more than
``MISSED_GRACE_S`` overdue is dropped as missed, and a recurring job skips ahead to
its next time. A scheduled run that reaches a risky step still asks you, and if
nobody answers it stops, so nothing risky happens unattended.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Literal, Optional, Tuple

from pydantic import BaseModel

logger = logging.getLogger(__name__)

MIN_INTERVAL_S = 60
MISSED_GRACE_S = 600
MAX_JOBS = 200

Kind = Literal["queued", "once", "daily", "interval"]


class Job(BaseModel):
    id: str
    goal: str
    kind: Kind
    next_run: float              # epoch seconds
    interval_s: int = 0          # interval jobs
    time_of_day: str = ""        # daily jobs, "HH:MM"
    created: float = 0.0
    last_run: float = 0.0
    runs: int = 0

    def describe(self) -> str:
        when = datetime.fromtimestamp(self.next_run).strftime("%a %d %b %H:%M")
        if self.kind == "queued":
            return f"[{self.id}] queued: {self.goal}"
        if self.kind == "daily":
            return f"[{self.id}] every day at {self.time_of_day} (next {when}): {self.goal}"
        if self.kind == "interval":
            return f"[{self.id}] every {_human_interval(self.interval_s)} (next {when}): {self.goal}"
        return f"[{self.id}] at {when}: {self.goal}"


def _human_interval(s: int) -> str:
    if s % 3600 == 0:
        h = s // 3600
        return f"{h} hour{'s' if h != 1 else ''}"
    m = s // 60
    return f"{m} minute{'s' if m != 1 else ''}"


# ----------------------------------------------------------------- parsing

_UNIT_S = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400}
_UNIT = r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)"
_TIME = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"
_SEP = r"[\s,:]+(?:then\s+|please\s+)?"


def _unit_seconds(unit: str) -> int:
    u = unit.lower().rstrip("s")
    return _UNIT_S.get(u, _UNIT_S.get(u[:3], 60))


def _clock(h: str, m: Optional[str], ampm: Optional[str]) -> Optional[Tuple[int, int]]:
    hour, minute = int(h), int(m or 0)
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    if not ampm and m is None:
        return None  # a bare "at 3" is too ambiguous to schedule
    return hour, minute


def _next_at(hour: int, minute: int, now: datetime, day_offset: Optional[int] = None) -> datetime:
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if day_offset is not None:
        return target + timedelta(days=day_offset)
    return target if target > now else target + timedelta(days=1)


class ScheduleRequest(BaseModel):
    goal: str
    kind: Kind
    next_run: float
    interval_s: int = 0
    time_of_day: str = ""


def parse_schedule(text: str, now: Optional[datetime] = None) -> Optional[ScheduleRequest]:
    """Recognise a scheduling prefix. Returns None for an ordinary request."""
    now = now or datetime.now()
    t = text.strip()

    m = re.match(rf"^in\s+(\d+)\s*{_UNIT}{_SEP}(.+)$", t, re.I | re.S)
    if m:
        secs = int(m.group(1)) * _unit_seconds(m.group(2))
        if secs > 0:
            return ScheduleRequest(goal=m.group(3).strip(), kind="once", next_run=(now + timedelta(seconds=secs)).timestamp())

    m = re.match(rf"^(?:every\s+day|daily|each\s+day)\s+at\s+{_TIME}{_SEP}(.+)$", t, re.I | re.S)
    if m:
        hm = _clock(m.group(1), m.group(2), m.group(3))
        if hm:
            return ScheduleRequest(goal=m.group(4).strip(), kind="daily", time_of_day=f"{hm[0]:02d}:{hm[1]:02d}",
                                   next_run=_next_at(*hm, now).timestamp())

    m = re.match(rf"^every\s+(\d+)?\s*{_UNIT}{_SEP}(.+)$", t, re.I | re.S)
    if m:
        secs = int(m.group(1) or 1) * _unit_seconds(m.group(2))
        secs = max(secs, MIN_INTERVAL_S)
        return ScheduleRequest(goal=m.group(3).strip(), kind="interval", interval_s=secs,
                               next_run=(now + timedelta(seconds=secs)).timestamp())

    m = re.match(rf"^(today|tomorrow)?\s*at\s+{_TIME}{_SEP}(.+)$", t, re.I | re.S)
    if m:
        hm = _clock(m.group(2), m.group(3), m.group(4))
        if hm:
            day = {"today": 0, "tomorrow": 1}.get((m.group(1) or "").lower())
            when = _next_at(*hm, now, day)
            if when > now:
                return ScheduleRequest(goal=m.group(5).strip(), kind="once", next_run=when.timestamp())
    return None


class WhenError(ValueError):
    """A time phrase Servo can't turn into a schedule (it says which and why)."""


_WORD_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "ten": 10, "fifteen": 15,
             "twenty": 20, "thirty": 30, "forty five": 45, "half an": 0.5, "a half": 0.5}
_PART_OF_DAY = {"morning": "am", "evening": "pm", "afternoon": "pm", "night": "pm", "tonight": "pm"}


def _amount(s: str) -> float:
    s = s.strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)
    if s in _WORD_NUM:
        return float(_WORD_NUM[s])
    raise WhenError(f"“{s}” is not an amount I can count")


def parse_when(phrase: str, now: Optional[datetime] = None) -> Tuple[Kind, float, int, str]:
    """A time phrase Needle copied from the request -> (kind, next_run, interval_s, time_of_day).

    'in 10 minutes', 'in half an hour', 'at 5pm', 'tomorrow at 9am', 'tonight at 10',
    'every day at 9:00', 'each morning at 8', 'every 30 minutes', 'every hour'.
    Raises WhenError for a phrase it can't place exactly (no guessing 'in a few minutes')."""
    now = now or datetime.now()
    w = " ".join(phrase.lower().replace(",", " ").split())
    w = re.sub(r"\bnoon\b", "12:00", w)
    w = re.sub(r"\bmidnight\b", "0:00", w)

    m = re.fullmatch(rf"in (\d+(?:\.\d+)?|an?|one|two|three|four|five|ten|fifteen|twenty|thirty|half an) {_UNIT}", w)
    if m:
        secs = int(_amount(m.group(1)) * _unit_seconds(m.group(2)))
        if secs <= 0:
            raise WhenError(f"“{phrase}” is not in the future")
        return "once", (now + timedelta(seconds=secs)).timestamp(), 0, ""

    m = re.fullmatch(rf"every (?:(\d+) )?{_UNIT}", w)
    if m:
        secs = max(int(m.group(1) or 1) * _unit_seconds(m.group(2)), MIN_INTERVAL_S)
        return "interval", (now + timedelta(seconds=secs)).timestamp(), secs, ""

    m = re.fullmatch(rf"(?:every|each) (day|morning|evening|afternoon|night)(?: at)? {_TIME}|daily at {_TIME}", w)
    if m:
        if m.group(1):
            part, h, mi, ap = m.group(1), m.group(2), m.group(3), m.group(4)
        else:
            part, h, mi, ap = "day", m.group(5), m.group(6), m.group(7)
        ap = ap or _PART_OF_DAY.get(part)
        hm = _clock(h, mi or ("00" if ap else None), ap)
        if not hm:
            raise WhenError(f"“{phrase}” has no exact time of day (say e.g. 9am or 21:00)")
        return "daily", _next_at(*hm, now).timestamp(), 0, f"{hm[0]:02d}:{hm[1]:02d}"

    m = re.fullmatch(rf"(today|tomorrow|tonight|this (?:morning|evening|afternoon))? ?(?:at )?{_TIME}", w)
    if m and (m.group(1) or w.startswith("at ") or m.group(4) or m.group(3)):
        day = m.group(1) or ""
        ap = m.group(4) or _PART_OF_DAY.get(day.split()[-1] if day else "", None)
        hm = _clock(m.group(2), m.group(3) or ("00" if ap else None), ap)
        if not hm:
            raise WhenError(f"“{phrase}” has no exact time (say e.g. 5pm or 17:00)")
        offset = 1 if day == "tomorrow" else (0 if day else None)
        when = _next_at(*hm, now, offset)
        if when <= now:
            raise WhenError(f"{when:%H:%M} today has already passed")
        return "once", when.timestamp(), 0, ""

    raise WhenError(f"I can't place “{phrase}” in time; say e.g. “in 10 minutes”, “at 5pm”, "
                    "“tomorrow at 9am”, “every day at 8:00” or “every 30 minutes”")


_LIST = re.compile(r"^(?:list|show|what(?:'s| is| are))\s+(?:my\s+|the\s+)?(?:scheduled\s+tasks?|schedule|queue|queued\s+tasks?)\??$", re.I)
_CANCEL = re.compile(r"^(?:cancel|remove|delete|unschedule)\s+(?:scheduled\s+task\s+|task\s+|job\s+)?\[?([0-9a-f]{4,8})\]?$", re.I)
_CANCEL_ALL = re.compile(r"^(?:cancel|clear|remove)\s+(?:all\s+)?(?:scheduled\s+tasks|the\s+schedule|the\s+queue|queue|schedule)$", re.I)
_QUEUE = re.compile(r"^(?:queue|after\s+(?:this|that)(?:\s+is\s+done)?,?)\s+(.+)$", re.I | re.S)


# --------------------------------------------------------------- scheduler

class Scheduler:
    """Persists jobs and starts them through ``start_run(goal) -> bool`` when idle.

    ``start_run`` returns False if a run is already in progress; the job then stays
    due and is tried again on the next tick, so jobs never overlap.
    """

    def __init__(
        self,
        start_run: Callable[[str], bool],
        path: Optional[Path] = None,
        notify: Optional[Callable[[str], None]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        from servo.config import settings

        self.start_run = start_run
        self.path = Path(path or settings.schedule_file)
        self.notify = notify or (lambda msg: None)
        self.clock = clock
        self._lock = threading.RLock()
        self._jobs: List[Job] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._load()

    # ------------------------------------------------------------ storage

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            jobs = [Job(**j) for j in raw.get("jobs", [])]
        except FileNotFoundError:
            return
        except Exception as e:
            logger.warning("Ignoring unreadable schedule file %s: %s", self.path, e)
            return
        now = self.clock()
        kept: List[Job] = []
        for j in jobs:
            if j.kind == "queued":
                continue  # a queue belongs to the session that made it
            if j.next_run < now - MISSED_GRACE_S:
                if j.kind == "once":
                    self.notify(f"Missed while Servo was off, not run: {j.goal}")
                    continue
                self._advance(j, now)
            kept.append(j)
        self._jobs = kept
        self._save()

    def _save(self) -> None:
        durable = [j.model_dump() for j in self._jobs if j.kind != "queued"]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"jobs": durable}, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            logger.warning("Could not save schedule: %s", e)

    # ------------------------------------------------------------ jobs

    def jobs(self) -> List[Job]:
        with self._lock:
            return sorted(self._jobs, key=lambda j: (j.kind != "queued", j.next_run, j.created))

    def add(self, req: ScheduleRequest) -> Job:
        if not req.goal.strip():
            raise ValueError("Say what to do, e.g. “in 10 minutes open notepad”.")
        if parse_schedule(req.goal) is not None:
            raise ValueError("A scheduled request can't itself be a schedule.")
        with self._lock:
            if len(self._jobs) >= MAX_JOBS:
                raise ValueError(f"Too many scheduled tasks ({MAX_JOBS}); cancel some first.")
            job = Job(id=secrets.token_hex(3), goal=req.goal.strip(), kind=req.kind, next_run=req.next_run,
                      interval_s=req.interval_s, time_of_day=req.time_of_day, created=self.clock())
            self._jobs.append(job)
            self._save()
        return job

    def enqueue(self, goal: str) -> Job:
        return self.add(ScheduleRequest(goal=goal, kind="queued", next_run=self.clock()))

    def cancel(self, job_id: str) -> Optional[Job]:
        with self._lock:
            for j in self._jobs:
                if j.id == job_id.lower():
                    self._jobs.remove(j)
                    self._save()
                    return j
        return None

    def clear(self) -> int:
        with self._lock:
            n = len(self._jobs)
            self._jobs = []
            self._save()
            return n

    def handle_command(self, text: str) -> Optional[str]:
        """Schedule-management and scheduling phrases. Returns a reply, or None to run normally."""
        t = text.strip()
        if _LIST.match(t):
            jobs = self.jobs()
            return "\n".join(j.describe() for j in jobs) if jobs else "Nothing is scheduled or queued."
        m = _CANCEL.match(t)
        if m:
            j = self.cancel(m.group(1))
            return f"Cancelled: {j.goal}" if j else f"No scheduled task with id {m.group(1)}."
        if _CANCEL_ALL.match(t):
            n = self.clear()
            return f"Cleared {n} scheduled or queued task{'s' if n != 1 else ''}."
        req = parse_schedule(t, now=datetime.fromtimestamp(self.clock()))
        if req:
            j = self.add(req)
            return f"Scheduled. {j.describe()}\nSay “cancel {j.id}” to remove it."
        return None

    @staticmethod
    def queue_request(text: str) -> Optional[str]:
        """'queue X' / 'after this, X' -> X."""
        m = _QUEUE.match(text.strip())
        return m.group(1).strip() if m else None

    # ------------------------------------------------------------ firing

    def _advance(self, job: Job, now: float) -> None:
        if job.kind == "interval":
            step = max(job.interval_s, MIN_INTERVAL_S)
            while job.next_run <= now:
                job.next_run += step
        elif job.kind == "daily":
            h, m = (int(x) for x in job.time_of_day.split(":"))
            job.next_run = _next_at(h, m, datetime.fromtimestamp(now)).timestamp()

    def tick(self) -> Optional[Job]:
        """Start the most overdue job if idle. Returns the job started, if any."""
        now = self.clock()
        with self._lock:
            due = [j for j in self.jobs() if j.next_run <= now]
            if not due:
                return None
            job = due[0]
            if not self.start_run(job.goal):
                return None  # busy: try again next tick
            job.last_run, job.runs = now, job.runs + 1
            if job.kind in ("queued", "once"):
                self._jobs.remove(job)
            else:
                self._advance(job, now)
            self._save()
            return job

    def start(self, interval: float = 1.0) -> None:
        if self._thread and self._thread.is_alive():
            return

        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    self.tick()
                except Exception:
                    logger.exception("scheduler tick failed")

        self._stop.clear()
        self._thread = threading.Thread(target=loop, name="servo-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
