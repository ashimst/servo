"""Real-desktop task suite: the goals users actually asked for, checked independently.

Each task runs through the same CascadeRouter the copilot uses, on the real desktop.
Then a separate probe checks the actual result (Notepad's text, Calculator's display,
the browser's title, the WMI brightness), never Servo's own report. So the pass rate
measures what really happened, and "Servo said done but it wasn't" shows up as a
LIE row.

    python -m servo.evals.task_suite                # on-device tasks only (no LLM)
    python -m servo.evals.task_suite --llm          # also tasks that need System 2
    python -m servo.evals.task_suite --only notepad_hello youtube_search

It moves the mouse, types, opens apps and browser tabs. Run it when you're not
using the machine. Results go to <SERVO_HOME>/evals/<timestamp>.json.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

Check = Callable[["Ctx"], Tuple[bool, str]]


@dataclass
class Ctx:
    outcome: Any = None
    before: Dict[str, Any] = field(default_factory=dict)
    scratch: Path = Path(tempfile.gettempdir())


@dataclass
class Task:
    id: str
    goal: str
    check: Check
    needs_llm: bool = False
    setup: Optional[Callable[[Ctx], None]] = None
    note: str = ""


# ------------------------------------------------------------------ probes

def _windows_of(process: str) -> List[Dict[str, Any]]:
    from servo.perception import windows
    return [w for w in windows.list_windows() if str(w.get("process") or "").lower() == process]


def _notepad_text() -> str:
    from pywinauto import Desktop
    from servo.perception import windows
    fg = windows.get_foreground_window()
    wins = _windows_of("notepad.exe")
    wins.sort(key=lambda w: int(w["hwnd"]) != fg)
    for w in wins:
        try:
            doc = Desktop(backend="uia").window(handle=int(w["hwnd"])).child_window(
                control_type="Document", found_index=0).wrapper_object()
            return str(doc.iface_value.CurrentValue or "")
        except Exception:
            continue
    return ""


def _calculator_display() -> str:
    from servo.perception.uia_sensor import UIASensor
    for w in _windows_of("applicationframehost.exe") + _windows_of("calculatorapp.exe"):
        if "calculator" not in str(w.get("title") or "").lower():
            continue
        scene = UIASensor(int(w["hwnd"])).sense()
        for e in scene.elements.values():
            if e.label.lower().startswith("display is"):
                return e.label
    from pywinauto import Desktop
    for w in _windows_of("applicationframehost.exe"):
        if "calculator" in str(w.get("title") or "").lower():
            try:
                el = Desktop(backend="uia").window(handle=int(w["hwnd"])).child_window(auto_id="CalculatorResults")
                return el.window_text()
            except Exception:
                pass
    return ""


def _browser_titles() -> List[str]:
    from servo.cognition.user_browser import BROWSER_PROCESSES
    from servo.perception import windows
    return [str(w.get("title") or "") for w in windows.list_windows()
            if str(w.get("process") or "").lower() in BROWSER_PROCESSES]


def _wait_for(pred: Callable[[], Any], timeout: float = 8.0) -> Any:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(0.4)
    return last


def _brightness() -> Optional[int]:
    import subprocess
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness).CurrentBrightness"],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        return int(out.splitlines()[0]) if out else None
    except Exception:
        return None


def _battery() -> Optional[int]:
    import subprocess
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-CimInstance Win32_Battery).EstimatedChargeRemaining"],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        return int(out.splitlines()[0]) if out else None
    except Exception:
        return None


def _reply(ctx: Ctx) -> str:
    return str(getattr(ctx.outcome, "reply", "") or "")


# ------------------------------------------------------------------ checks

def check_notepad_contains(text: str) -> Check:
    def check(ctx: Ctx) -> Tuple[bool, str]:
        got = _notepad_text()
        return (text.lower() in got.lower(), f"Notepad text ends with: {got[-120:]!r}")
    return check


def check_calculator(expected: str) -> Check:
    def check(ctx: Ctx) -> Tuple[bool, str]:
        disp = _wait_for(_calculator_display, 4) or ""
        return (bool(re.search(rf"\b{re.escape(expected)}\b", disp)), f"Calculator display: {disp!r}")
    return check


def check_browser_title(*words: str, avoid: str = "") -> Check:
    def check(ctx: Ctx) -> Tuple[bool, str]:
        def hit():
            for t in _browser_titles():
                low = t.lower()
                if all(w.lower() in low for w in words) and not (avoid and avoid.lower() in low):
                    return t
            return ""
        t = _wait_for(hit, 10)
        return (bool(t), f"browser title: {t!r}" if t else f"no browser title with {words}; titles: {_browser_titles()[:4]}")
    return check


def check_reply(pattern: str) -> Check:
    def check(ctx: Ctx) -> Tuple[bool, str]:
        r = _reply(ctx)
        return (bool(re.search(pattern, r, re.I)), f"reply: {r[:160]!r}")
    return check


def check_route(route: str) -> Check:
    def check(ctx: Ctx) -> Tuple[bool, str]:
        got = getattr(ctx.outcome, "route", "")
        return (got == route, f"route: {got}")
    return check


def check_ran_ok(min_steps: int = 1) -> Check:
    def check(ctx: Ctx) -> Tuple[bool, str]:
        o = ctx.outcome
        n = len(getattr(o, "steps", []) or [])
        return (bool(o and o.success and n >= min_steps), f"success={getattr(o, 'success', None)} steps={n}")
    return check


def check_battery(ctx: Ctx) -> Tuple[bool, str]:
    actual = _battery()
    r = _reply(ctx)
    if actual is None:
        return (bool(re.search(r"no battery|desktop|not (?:found|available)", r, re.I)), f"no battery; reply: {r[:120]!r}")
    nums = [int(n) for n in re.findall(r"\b(\d{1,3})\s*%", r)]
    return (any(abs(n - actual) <= 2 for n in nums), f"actual {actual}%; reply: {r[:120]!r}")


def check_brightness_up(ctx: Ctx) -> Tuple[bool, str]:
    before, after = ctx.before.get("brightness"), _brightness()
    if before is None:
        return (bool(re.search(r"not support|external|can't|cannot", _reply(ctx), re.I)),
                f"brightness not readable; reply: {_reply(ctx)[:120]!r}")
    if before >= 100:  # already at max: the right answer is to say so, not to change anything
        return (bool(re.search(r"max|already|100", _reply(ctx), re.I)) and after == before,
                f"already at {before}; reply: {_reply(ctx)[:120]!r}")
    return (after is not None and after > before, f"brightness {before} -> {after}")


def check_youtube_playing(ctx: Ctx) -> Tuple[bool, str]:
    """A video page is open (address bar shows youtube.com/watch). YouTube hides its player
    controls when the mouse isn't over the video, so a Pause button is extra evidence only."""
    from servo.perception.uia_sensor import UIASensor
    from servo.perception import windows
    from servo.cognition.user_browser import BROWSER_PROCESSES, address_bar_text
    for w in windows.list_windows():
        if str(w.get("process") or "").lower() not in BROWSER_PROCESSES:
            continue
        hwnd = int(w["hwnd"])
        bar = address_bar_text(hwnd).lower()
        # Chrome/Edge show the URL (/watch); Dia shows "youtube.com / <page title>". The
        # results page is titled "<query> - YouTube", the home page just "YouTube".
        page = bar.split(" / ", 1)[1].strip() if " / " in bar else ""
        page = re.sub(r"^\(\d+\)\s*", "", page)
        on_video = "youtube.com/watch" in bar or (
            bar.startswith("youtube.com") and page and page not in ("youtube", "kanye - youtube")
            and not page.endswith("search") and "results" not in bar)
        if on_video:
            scene = UIASensor(hwnd).sense()
            pause = [e.label for e in scene.elements.values() if re.match(r"pause\b", e.label, re.I)]
            return (True, f"video page open: {w['title']!r}" + (f"; shows {pause[0]!r}" if pause else ""))
    return (False, f"no browser tab on a youtube.com/watch page; titles: {_browser_titles()[:4]}")


def check_notepad_composed(word: str, min_words: int = 8) -> Check:
    def check(ctx: Ctx) -> Tuple[bool, str]:
        got = _notepad_text()
        lines = [l.strip() for l in re.split(r"[\r\n]+", got) if l.strip()]
        dupes = len(lines) != len(set(lines))
        ok = word.lower() in got.lower() and len(got.split()) >= min_words and not dupes
        return (ok, ("REPEATED LINES (text typed more than once); " if dupes else "") + f"Notepad text: {got[-160:]!r}")
    return check


# ------------------------------------------------------------------- tasks

def _setup_brightness(ctx: Ctx) -> None:
    ctx.before["brightness"] = _brightness()


def _setup_file(ctx: Ctx) -> None:
    (ctx.scratch / "servo_suite_note.txt").write_text("suite marker 7781\n", encoding="utf-8")


def tasks(scratch: Path) -> List[Task]:
    stamp = time.strftime("%H%M%S")
    return [
        # on-device (no LLM)
        Task("notepad_hello", f"open notepad and write hello servo {stamp}", check_notepad_contains(f"hello servo {stamp}")),
        Task("calculator_7", "open calculator and click 7", check_calculator("7")),
        Task("volume_up", "increase the volume a bit", check_ran_ok(5), note="no ground-truth probe for volume"),
        Task("volume_down", "turn the volume down a bit", check_ran_ok(5), note="restores the previous task"),
        Task("youtube_search", "search youtube for kanye", check_browser_title("kanye", "youtube")),
        Task("site_in_browser", "open wikipedia in dia", check_browser_title("wikipedia")),
        Task("read_file", f"read file {scratch / 'servo_suite_note.txt'}", check_reply(r"suite marker 7781"),
             setup=_setup_file),
        Task("preference", "dia is my default browser", check_route("preference")),
        # need System 2
        Task("battery", "check the battery percentage of the device", check_battery, needs_llm=True),
        Task("brightness", "increase the brightness", check_brightness_up, needs_llm=True, setup=_setup_brightness),
        Task("youtube_play", "play kanye on youtube", check_youtube_playing, needs_llm=True),
        Task("notepad_haiku", "open notepad and write a short poem about rain", check_notepad_composed("rain"),
             needs_llm=True),
    ]


# ------------------------------------------------------------------ runner

def _confirm_policy(log: List[str]) -> Callable[[Any], bool]:
    """Approve only read-only commands and ordinary medium-risk steps; refuse high risk."""
    readonly = re.compile(r"^(?:\(?\s*Get-|netsh\s+\S+\s+show|Start-Process\s+ms-settings:|\(Get-CimInstance)", re.I)

    def confirm(req: Any) -> bool:
        call = str(req.tool_call)
        m = re.search(r"command='(.*)'\)$", call) or re.search(r'command="(.*)"\)$', call)
        if req.risk_level == "high":
            ok = False
        elif m:
            ok = bool(readonly.match(m.group(1).strip())) or "WmiSetBrightness" in m.group(1)
        else:
            ok = True
        log.append(f"{'APPROVED' if ok else 'DENIED'} {req.risk_level}: {call[:140]}")
        return ok
    return confirm


def run_task(task: Task, scratch: Path) -> Dict[str, Any]:
    from servo.cognition.router import CascadeRouter, new_run_id
    from servo.cognition.trace import Tracer
    from servo.daemon.copilot import launch_or_focus_app

    ctx = Ctx(scratch=scratch)
    if task.setup:
        task.setup(ctx)
    confirms: List[str] = []
    t0 = time.perf_counter()
    try:
        router = CascadeRouter(tracer=Tracer(new_run_id()), app_launcher=launch_or_focus_app,
                               confirmation_callback=_confirm_policy(confirms))
        ctx.outcome = router.run(task.goal)
    except Exception as e:
        return {"id": task.id, "goal": task.goal, "passed": False, "said_success": None,
                "evidence": f"crashed: {e.__class__.__name__}: {e}", "seconds": round(time.perf_counter() - t0, 1)}
    time.sleep(0.8)
    try:
        passed, evidence = task.check(ctx)
    except Exception as e:
        passed, evidence = False, f"check failed: {e}"
    o = ctx.outcome
    return {
        "id": task.id, "goal": task.goal, "passed": passed, "evidence": evidence,
        "said_success": bool(o.success), "reply": o.reply, "route": o.route, "run_id": o.run_id,
        "steps": [f"{s.verification:11} {s.call or s.command}" + (f"  ERR {s.error}" if s.error else "")
                  for s in o.steps],
        "confirms": confirms, "seconds": round(time.perf_counter() - t0, 1), "note": task.note,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--llm", action="store_true", help="also run tasks that need System 2 (uses your planner API)")
    ap.add_argument("--only", nargs="*", help="task ids to run")
    args = ap.parse_args(argv)
    if sys.platform != "win32":
        print("The task suite drives the Windows desktop.")
        return 2
    import pythoncom
    pythoncom.CoInitialize()

    from servo.paths import servo_home
    scratch = Path(tempfile.mkdtemp(prefix="servo-suite-"))
    chosen = [t for t in tasks(scratch) if (args.llm or not t.needs_llm) and (not args.only or t.id in args.only)]
    results = []
    for t in chosen:
        print(f"→ {t.id}: {t.goal}", flush=True)
        r = run_task(t, scratch)
        verdict = "PASS" if r["passed"] else ("LIE " if r.get("said_success") else "FAIL")
        r["verdict"] = verdict.strip()
        print(f"  {verdict} ({r['seconds']}s) {r['evidence']}", flush=True)
        if not r["passed"]:
            for s in r.get("steps", []):
                print(f"      {s}")
            print(f"      reply: {str(r.get('reply', ''))[:200]!r}")
        results.append(r)
    passed = sum(r["passed"] for r in results)
    lies = sum(1 for r in results if r.get("verdict") == "LIE")
    print(f"\n{passed}/{len(results)} passed" + (f" · {lies} reported success but the probe disagreed" if lies else ""))
    out = servo_home() / "evals" / f"{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"passed": passed, "total": len(results), "results": results}, indent=1,
                              ensure_ascii=False, default=str), encoding="utf-8")
    print(f"Saved {out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
