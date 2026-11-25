"""Run one WindowsAgentArena task with Servo, INSIDE the benchmark VM.

WAA's agents normally sit outside the VM, see screenshots and send back pyautogui code.
Servo works on the live accessibility tree and checks its own effects, so it has to run
where the apps are. The WAA-side agent (``waa_agent.py``) starts this module in the VM
through WAA's own server and polls the status file; WAA's evaluator then scores the VM's
final state exactly as for every other agent.

    python -m servo.integrations.winarena.vm_runner --goal-b64 <base64 utf-8> --status C:\\servo_waa\\status.json

The status file is JSON: {"state": "running"|"done"|"error", "success", "reply", "steps", ...}.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict


def _write(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    # ASCII only: WAA's /execute decodes command output as cp1252, and a UTF-8 curly quote
    # in Servo's reply would make the poller's `type` call fail.
    tmp.write_text(json.dumps(data, ensure_ascii=True, default=str), encoding="ascii")
    os.replace(tmp, path)


def load_planner_config(path: str) -> None:
    """Planner provider/model/key exported by export_planner.py, as SERVO_* env vars.

    Must run before servo.config is imported (settings are read at import)."""
    if not path:
        return
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for k, v in data.items():
        if k.startswith("SERVO_") and isinstance(v, str):
            os.environ[k] = v


# Processes that are part of the benchmark's own plumbing, never the app a task is about.
_PLUMBING = {"python.exe", "pythonw.exe", "cmd.exe", "conhost.exe", "powershell.exe", "pwsh.exe",
             "socat.exe", "caddy.exe", "tasklist.exe", "taskkill.exe", "openconsole.exe",
             "windowsterminal.exe", "backgroundtaskhost.exe", "runtimebroker.exe", "dllhost.exe",
             "searchprotocolhost.exe", "searchfilterhost.exe", "wmiprvse.exe", "audiodg.exe"}


def _visible_windows() -> Dict[int, int]:
    """{hwnd: pid} of visible, titled top-level windows."""
    import win32gui
    import win32process
    out: Dict[int, int] = {}

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd).strip():
            out[hwnd] = win32process.GetWindowThreadProcessId(hwnd)[1]
        return True

    win32gui.EnumWindows(cb, None)
    return out


def _starting_apps(max_age_s: float = 25.0) -> Dict[int, str]:
    """{pid: exe} of app processes started in the last few seconds that show no window yet."""
    import ctypes
    import datetime as dt
    import win32api
    import win32con
    import win32process
    owners = set(_visible_windows().values())
    now = dt.datetime.now(dt.timezone.utc)
    me = os.getpid()
    out: Dict[int, str] = {}
    for pid in win32process.EnumProcesses():
        if pid in (0, 4, me) or pid in owners:
            continue
        try:
            h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        except Exception:
            continue
        try:
            created = win32process.GetProcessTimes(h)["CreationTime"]
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(len(buf))
            ctypes.windll.kernel32.QueryFullProcessImageNameW(int(h), 0, buf, ctypes.byref(size))
            exe = buf.value
        except Exception:
            continue
        finally:
            win32api.CloseHandle(h)
        name = exe.rsplit("\\", 1)[-1].lower()
        if not exe or name in _PLUMBING or exe.lower().startswith(r"c:\windows\system32"):
            continue
        try:
            age = (now - created.astimezone(dt.timezone.utc)).total_seconds()
        except Exception:
            continue
        if 0 <= age <= max_age_s:
            out[pid] = exe
    return out


def wait_for_starting_apps(timeout_s: float = 30.0, settle_s: float = 2.0) -> Dict[str, Any]:
    """The benchmark opens a task's file or app and hands over at once; a large app (an office
    suite, a browser) may not have a window yet. Wait until no app that just started is still
    windowless and the set of windows has stopped changing, so the task is planned against the
    screen the user would see. Returns at once when nothing is starting."""
    t0 = time.time()
    try:
        starting = _starting_apps()
    except Exception:
        return {"waited_s": 0.0, "starting": []}
    if not starting:
        return {"waited_s": 0.0, "starting": []}
    names = sorted({e.rsplit("\\", 1)[-1] for e in starting.values()})
    first = last = set(_visible_windows())
    stable_since = time.time()
    while time.time() - t0 < timeout_s:
        time.sleep(0.5)
        now_windows = set(_visible_windows())
        if now_windows != last:
            last, stable_since = now_windows, time.time()
        # Done once a new window has appeared (or nothing is starting any more) and the
        # desktop has been still for a moment; a launcher stub that never shows a window
        # only ages out of _starting_apps.
        ready = bool(now_windows - first) or not _starting_apps()
        if ready and time.time() - stable_since >= settle_s:
            break
    return {"waited_s": round(time.time() - t0, 1), "starting": names}


def run(goal: str, status: Path) -> Dict[str, Any]:
    # A throwaway benchmark VM: assistant mode with every confirmation approved below (harness
    # mode would sandbox Servo to an allowlist of windows, but a WAA task may use any app),
    # web pages open in the VM's own browser, and nothing from the developer's machine is read.
    os.environ.setdefault("SERVO_NO_USER_SETTINGS", "1")
    os.environ.setdefault("SERVO_MODE", "assistant")
    # Nobody can answer "ask me to continue" in a benchmark, so give one task the same
    # budget WAA gives every agent (15 steps; a Servo round is one planner decision).
    os.environ.setdefault("SERVO_MAX_ROUNDS", "15")
    # Each task starts with an empty conversation: nothing from an earlier task (a file it
    # saved, an app it used) may leak into this one.
    memory = status.with_suffix(".memory.json")
    memory.unlink(missing_ok=True)
    os.environ["SERVO_CHAT_MEMORY_PATH"] = str(memory)
    t0 = time.time()
    _write(status, {"state": "running", "goal": goal, "started": t0})
    settled: Dict[str, Any] = {}
    try:
        import pythoncom
        pythoncom.CoInitialize()
        settled = wait_for_starting_apps()
        from servo.cognition.router import CascadeRouter, new_run_id
        from servo.cognition.trace import Tracer
        from servo.daemon.copilot import launch_or_focus_app

        router = CascadeRouter(tracer=Tracer(new_run_id()), app_launcher=launch_or_focus_app,
                               confirmation_callback=lambda req: True)
        out = router.run(goal)
        result = {
            "state": "done", "goal": goal, "success": bool(out.success), "reply": out.reply,
            "route": out.route, "run_id": out.run_id, "seconds": round(time.time() - t0, 1),
            "waited_for_apps": settled,
            "steps": [{"call": s.call or s.command, "verification": s.verification, "error": s.error}
                      for s in out.steps],
        }
    except Exception as e:  # noqa: BLE001 - always leave a final status for the poller
        result = {"state": "error", "goal": goal, "success": False, "reply": f"{e.__class__.__name__}: {e}",
                  "trace": traceback.format_exc()[-4000:], "seconds": round(time.time() - t0, 1)}
    _write(status, result)
    return result


def main(argv: Any = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--goal-b64", required=True, help="the task instruction, base64-encoded UTF-8")
    ap.add_argument("--status", required=True, help="where to write the status JSON")
    ap.add_argument("--planner-config", default="", help="JSON of SERVO_* planner settings (export_planner.py)")
    a = ap.parse_args(argv)
    load_planner_config(a.planner_config)
    goal = base64.b64decode(a.goal_b64).decode("utf-8")
    res = run(goal, Path(a.status))
    return 0 if res.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
