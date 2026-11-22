"""Copilot brain and app-launch helpers.

The floating UI lives in servo.daemon.copilot_app; routing lives in
servo.cognition.router. This module keeps the conversational shortcuts, the legacy
process_message API, and Windows app resolution/launching.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from servo.actuation.alias_mapping import AliasMapper, CanonicalAction, CanonicalActionType
from servo.config import settings
from servo.control.handoff import PlanExecutionSummary, StepExecutionReport, SystemHandoffCoordinator
from servo.control.loop import ServoControlLoop
from servo.daemon.worker import worker
from servo.perception import app_identity, windows
from servo.perception.scene_index import SceneIndex
from servo.perception.uia_sensor import UIASensor
from servo.planner.tool_calling import (
    MacroPlan,
    PlanStep,
    SYSTEM_PROMPT_TEMPLATE,
    build_tool_planning_prompt,
    parse_llm_tool_plan,
)

logger = logging.getLogger("servo.copilot")

# Common application executable mappings on Windows
APP_EXECUTABLE_MAP = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "google-chrome": "chrome.exe",
    "edge": "msedge.exe",
    "microsoft edge": "msedge.exe",
    "msedge": "msedge.exe",
    "firefox": "firefox.exe",
    "mozilla firefox": "firefox.exe",
    "brave": "brave.exe",
    "terminal": "wt.exe",
    "cmd": "cmd.exe",
    "command prompt": "cmd.exe",
    "powershell": "powershell.exe",
    "paint": "mspaint.exe",
    "mspaint": "mspaint.exe",
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
    "task manager": "taskmgr.exe",
    "taskmgr": "taskmgr.exe",
    "code": "Code.exe",
    "vs code": "Code.exe",
    "vscode": "Code.exe",
}


def resolve_app_executable(app_name: str) -> str:
    """Resolve full executable path for Windows apps using Registry App Paths and known locations."""
    app_key = app_name.strip().lower()

    # 0. Check Windows Context Catalog (Start Menu & App Paths)
    try:
        from servo.cognition.windows_context import windows_context
        installed, app_info = windows_context.is_installed(app_name)
        if installed and app_info and app_info.executable and os.path.exists(app_info.executable):
            return app_info.executable
    except Exception:
        pass

    exe_name = APP_EXECUTABLE_MAP.get(app_key, f"{app_key}.exe")

    # 1. Check if already absolute or in PATH
    if os.path.isabs(exe_name) and os.path.exists(exe_name):
        return exe_name
    which_p = shutil.which(exe_name)
    if which_p and os.path.exists(which_p):
        return which_p

    # 2. Check Windows App Paths in Registry (HKCU and HKLM)
    if sys.platform == "win32":
        import winreg
        for target in (exe_name, f"{app_key}.exe"):
            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    key_path = f"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\{target}"
                    with winreg.OpenKey(root, key_path) as k:
                        val, _ = winreg.QueryValueEx(k, "")
                        val = val.strip('\"\'')
                        if os.path.exists(val):
                            return val
                except OSError:
                    pass

    # 3. Known fallback locations for common desktop applications
    candidates = [
        rf"C:\Program Files\Google\Chrome\Application\{exe_name}",
        rf"C:\Program Files (x86)\Google\Chrome\Application\{exe_name}",
        rf"C:\Program Files\Microsoft\Edge\Application\{exe_name}",
        rf"C:\Program Files (x86)\Microsoft\Edge\Application\{exe_name}",
        os.path.expandvars(rf"%LOCALAPPDATA%\Google\Chrome\Application\{exe_name}"),
        os.path.expandvars(rf"%LOCALAPPDATA%\Microsoft\Edge\Application\{exe_name}"),
        os.path.expandvars(rf"%LOCALAPPDATA%\Programs\{app_key}\{exe_name}"),
        os.path.expandvars(rf"%PROGRAMFILES%\{exe_name}"),
        os.path.expandvars(rf"%PROGRAMFILES(X86)%\{exe_name}"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c

    return exe_name


# Apps whose window belongs to a different process than the executable we start.
_PROCESS_ALIASES: Dict[str, Tuple[str, ...]] = {
    "terminal": ("windowsterminal.exe",),
    "windows terminal": ("windowsterminal.exe",),
    "calculator": ("calculatorapp.exe",),
    "calc": ("calculatorapp.exe", "calculator.exe"),
    "settings": ("systemsettings.exe",),
    "paint": ("mspaint.exe",),
    "photos": ("photos.exe",),
    "file explorer": ("explorer.exe",),
    "explorer": ("explorer.exe",),
}


def _app_record(app_name: str):
    """Catalog entry for the app (with its AppUserModelIDs), or None."""
    try:
        from servo.cognition.windows_context import windows_context
        installed, info = windows_context.is_installed(app_name)
        return info if installed else None
    except Exception:
        return None


def _app_ids(info: Any) -> Tuple[str, ...]:
    if info is None:
        return ()
    ids = list(getattr(info, "app_ids", None) or [])
    if getattr(info, "app_id", None) and info.app_id not in ids:
        ids.append(info.app_id)
    return tuple(ids)


def _matches_app(w: Dict[str, Any], app_key: str, exe_filename: str, app_ids: Tuple[str, ...] = ()) -> bool:
    """Match a window to an app by its APP IDENTITY or PROCESS, not its title.

    Title matching made "open notepad" focus an Edge tab whose title contained the
    word "notepad". Titles are used only when the process name is unknown.

    Installed web apps (Facebook, YouTube…) run inside msedge.exe/chrome.exe. Such a
    window matches only its own app, through its AppUserModelID; it is never "Edge"
    (before, "open edge" could pick the Facebook app and type URLs into it).
    """
    proc = str(w.get("process") or w.get("process_name") or "").lower()
    title = str(w.get("title") or "").lower()
    if "servo copilot" in title:
        return False
    hwnd = int(w.get("hwnd") or 0)
    aid = None
    if hwnd and (app_ids or proc in app_identity.BROWSER_PROCESSES):
        aid = w.get("app_id")
        if aid is None:
            aid = app_identity.window_app_id(hwnd, proc)
            w["app_id"] = aid
        if app_ids and aid and aid in app_ids:
            return True
        if app_identity.is_web_app_window(hwnd, proc, aid):
            return False
    if proc:
        if proc in _PROCESS_ALIASES.get(app_key, ()):
            return True
        stem = proc[:-4] if proc.endswith(".exe") else proc
        return proc == exe_filename or stem == app_key.replace(" ", "") or (len(app_key) >= 4 and app_key.replace(" ", "") in stem)
    return app_key in title


# App names come from the user, Needle or an LLM (which may have read a hostile web
# page). Shell metacharacters have no place in an app name, so refuse them outright.
_UNSAFE_APP_NAME = re.compile(r'["&|^<>%;`$\r\n]')


def find_app_window(app_name: str) -> Optional[int]:
    """HWND of an already-open window for the app, or None. Never launches anything."""
    if _UNSAFE_APP_NAME.search(app_name):
        return None
    app_key = app_name.strip().lower()
    try:
        exe_filename = os.path.basename(resolve_app_executable(app_name)).lower()
    except Exception:
        exe_filename = f"{app_key}.exe"
    ids = _app_ids(_app_record(app_name))
    for w in windows.list_windows():
        if _matches_app(w, app_key, exe_filename, ids):
            return int(w["hwnd"])
    return None


def launch_or_focus_app(app_name: str) -> Tuple[Optional[int], str]:
    """Find an existing window for the app or launch it and return (hwnd, title)."""
    if _UNSAFE_APP_NAME.search(app_name):
        return None, f"Refusing to launch '{app_name}': app names cannot contain shell characters."
    app_key = app_name.strip().lower()
    full_exe_path = resolve_app_executable(app_name)
    exe_filename = os.path.basename(full_exe_path).lower()
    record = _app_record(app_name)
    ids = _app_ids(record)

    def _focus(w: Dict[str, Any]) -> Tuple[int, str]:
        hwnd = int(w["hwnd"])
        if sys.platform == "win32":
            # Plain SetForegroundWindow is often refused (foreground lock): the app was
            # found but stayed behind, and the next step typed into another window.
            from servo.actuation.sandboxing import activate_window
            if not activate_window(hwnd):
                logger.info("Found %s (HWND %s) but Windows kept it in the background", app_name, hwnd)
        return hwnd, str(w.get("title") or app_name)

    # 1. Already open?
    for w in windows.list_windows():
        if _matches_app(w, app_key, exe_filename, ids):
            return _focus(w)

    # 1.5. If the app is not installed, fail fast without waiting 8 seconds
    if sys.platform == "win32":
        if not (os.path.isabs(full_exe_path) and os.path.exists(full_exe_path)) and not shutil.which(full_exe_path):
            try:
                from servo.cognition.windows_context import windows_context
                installed, _ = windows_context.is_installed(app_name)
                if not installed:
                    return None, f"Application '{app_name}' is not installed on this system."
            except Exception:
                pass

    # 2. Launch it. Store apps and installed web apps start through their AppUserModelID,
    #    like the Start menu does: they have no exe of their own (web apps) or one under
    #    WindowsApps that Windows refuses to start directly.
    by_id = bool(ids) and (
        getattr(record, "kind", "desktop") in ("store", "web_app")
        or "\\windowsapps\\" in full_exe_path.lower()
        or not (os.path.isabs(full_exe_path) and os.path.exists(full_exe_path))
    )
    try:
        if by_id:
            app_identity.launch(ids[0])
        elif sys.platform == "win32":
            if os.path.isabs(full_exe_path) and os.path.exists(full_exe_path):
                subprocess.Popen([full_exe_path])
            else:
                # ShellExecute resolves App Paths / PATH like `start` did, without a shell.
                os.startfile(full_exe_path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen([full_exe_path])
    except Exception as e:
        return None, f"Failed to launch '{app_name}': {e}"

    # 3. Wait for its window (Store apps like Notepad/Calculator run under a different
    #    process than the stub we launched, so match by name, for up to 8 s).
    for _ in range(40):
        time.sleep(0.2)
        for w in windows.list_windows():
            if _matches_app(w, app_key, exe_filename, ids):
                return _focus(w)

    return None, f"Window for '{app_name}' did not appear in time."


def resolve_default_user_name() -> str:
    """Detect current user name from environment or git defaults."""
    env_user = os.environ.get("USERNAME") or os.environ.get("USER") or getpass.getuser()
    if env_user and env_user.lower() not in ("user", "default", "administrator", "acer"):
        return env_user
    return "Ashim"


class CopilotBrain:
    """Conversational intelligence and System 2 Macro Planner for Servo Copilot."""

    def __init__(
        self,
        user_name: Optional[str] = None,
        planner_provider: Optional[str] = None,
    ) -> None:
        self.user_name = user_name or resolve_default_user_name()
        self.planner_provider = planner_provider
        self.history: List[Dict[str, str]] = []
        self.active_hwnd: Optional[int] = None
        self.active_title: str = ""

    def query_llm_for_plan(self, prompt: str, scene: Optional[SceneIndex] = None) -> Optional[MacroPlan]:
        """Attempt to generate a structured tool plan using configured System 2 LLM planner."""
        try:
            from servo.planner import get_planner

            planner = get_planner(self.planner_provider)
            if hasattr(planner, "decompose_macro"):
                plan = planner.decompose_macro(prompt, initial_scene=scene)
                if plan and plan.steps:
                    if self.user_name and self.user_name != "User":
                        for step in plan.steps:
                            if step.arguments.get("text") in ("User", "my name", "my username"):
                                step.arguments["text"] = self.user_name
                                if step.canonical_action:
                                    step.canonical_action.text = self.user_name
                    return plan
            elif hasattr(planner, "decompose"):
                subgoals = list(planner.decompose(prompt, scene or SceneIndex(hwnd=0, window_title="", source="uia")))
                if subgoals:
                    steps = []
                    for sg in subgoals:
                        tool_name = (
                            "type_text" if sg.action_type == "type"
                            else ("click_element" if sg.action_type == "click"
                            else (sg.tool_name if sg.tool_name and sg.tool_name != "click_element" else sg.action_type))
                        )
                        arguments = dict(sg.arguments) if sg.arguments else {}
                        if sg.action_type == "type":
                            if sg.target_element_hint and "target" not in arguments:
                                arguments["target"] = sg.target_element_hint
                            if sg.text_to_type and "text" not in arguments:
                                arguments["text"] = sg.text_to_type
                        elif sg.action_type == "click":
                            if sg.target_element_hint and "target" not in arguments:
                                arguments["target"] = sg.target_element_hint
                        else:
                            if sg.target_element_hint and "target" not in arguments:
                                arguments["target"] = sg.target_element_hint
                            if sg.text_to_type and "text" not in arguments:
                                arguments["text"] = sg.text_to_type

                        canon = sg.canonical_action
                        if canon is None:
                            try:
                                canon = AliasMapper.canonicalize_action(raw_action=tool_name, **arguments)
                            except Exception:
                                canon = None

                        steps.append(
                            PlanStep(
                                step_index=sg.step_index,
                                tool_name=tool_name,
                                arguments=arguments,
                                rationale=sg.instruction,
                                canonical_action=canon,
                            )
                        )
                    plan = MacroPlan(goal=prompt, steps=steps)
                    if self.user_name:
                        for step in plan.steps:
                            if step.arguments.get("text") in ("User", "my name", "my username"):
                                step.arguments["text"] = self.user_name
                                if step.canonical_action:
                                    step.canonical_action.text = self.user_name
                    return plan
        except Exception as e:
            logger.debug(
                "System 2 planner query failed (%s: %s); falling back to deterministic intent parser.",
                e.__class__.__name__,
                e,
            )
        return None

    def query_llm_for_chat(self, prompt: str) -> Optional[str]:
        """Attempt to converse via configured LLM endpoints for general questions."""
        base_url = settings.openai_base_url.rstrip("/")
        api_key = settings.openai_api_key
        model = settings.openai_model or "llama3"

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        system_prompt = (
            f"You are Servo, a helpful and friendly on-screen desktop copilot companion. "
            f"The user's name is {self.user_name}. You live in a compact floating desktop widget. "
            f"Answer concisely, warmly, and helpfully in 1-3 sentences."
        )

        messages = [{"role": "system", "content": system_prompt}]
        for m in self.history[-4:]:
            messages.append({"role": m["role"], "content": m["text"]})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 200,
        }

        try:
            with httpx.Client(timeout=3.0) as client:
                res = client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
                if res.status_code == 200:
                    data = res.json()
                    return data["choices"][0]["message"]["content"].strip()
        except Exception:
            pass
        return None

    def conversational_reply(self, prompt: str) -> Optional[str]:
        """Answer identity, small talk and desktop introspection locally (no model, no actuation)."""
        p = prompt.strip()
        p_lower = p.lower()

        m_name = re.search(r"^(?:my name is|call me|i am|i'm)\s+([a-zA-Z0-9_\s]+)$", p, re.I)
        if m_name and "how" not in p_lower and "what" not in p_lower:
            name = m_name.group(1).strip()
            self.user_name = name
            return f"Nice to meet you, {name}! I'll remember that. What would you like me to do on your computer?"

        if any(q in p_lower for q in ["what is my name", "what's my name", "who am i", "do you know my name"]):
            return f"Your name is {self.user_name}! Let me know what you'd like me to help you with."

        if any(w in p_lower for w in ["what windows are open", "what's on my screen", "what is on my screen", "list windows", "what is open", "show windows"]):
            wins = windows.list_windows()
            if not wins:
                return "I couldn't find any visible application windows right now."
            lines = ["Here are the open windows on your screen:"]
            for w in wins[:6]:
                marker = " (active target)" if w.get("hwnd") == self.active_hwnd else ""
                lines.append(f"• {w.get('title', 'Untitled')}{marker}")
            if len(wins) > 6:
                lines.append(f"…and {len(wins) - 6} more.")
            return "\n".join(lines)

        if re.match(r"^(?:hi|hello|hey|good\s+(?:morning|afternoon|evening)|howdy)\b", p_lower):
            return f"Hello {self.user_name}! I'm Servo. Tell me what to do on your computer, or just ask me something."

        if re.match(r"^(?:how are you|how's it going|how are you doing)\b", p_lower):
            return "All systems are running. What should we do?"

        if re.match(r"^(?:who are you|what are you)\b", p_lower):
            return ("I'm Servo, a local-first desktop copilot. Simple commands run on small on-device models; "
                    "anything that needs reasoning goes to a larger model, and you can watch every decision in the trace.")

        if re.match(r"^(?:what can you do|help|capabilities)\b", p_lower):
            return (
                "Try:\n"
                "• open notepad and write hello world\n"
                "• open calculator and click 7\n"
                "• search the web for weather in Kathmandu\n"
                "• go to github.com\n"
                "• what windows are open?"
            )
        return None

    def offline_chat_reply(self, prompt: str) -> str:
        p_lower = prompt.lower()
        if "joke" in p_lower:
            return "Why do programmers prefer dark mode? Because light attracts bugs."
        if "thank" in p_lower:
            return "You're welcome."
        return ("I can act on your screen — try “open notepad and write …”, “click …” or “search the web for …”. "
                "For open-ended chat, configure a System 2 model (SERVO_PLANNER_PROVIDER).")

    def process_message(
        self,
        prompt: str,
        scene: Optional[SceneIndex] = None,
    ) -> Tuple[str, Optional[MacroPlan]]:
        """Legacy entry point: a reply, or a MacroPlan. Cheap tiers first.

        The copilot UI uses servo.cognition.router.CascadeRouter instead; this remains for
        API callers and tests. Order: local conversation → command grammar → System 2 LLM.
        """
        p = prompt.strip()
        quick = self.conversational_reply(p)
        if quick:
            return quick, None

        plan = self._parse_deterministic_plan(p)
        if plan and plan.steps:
            return f"Decomposed goal into {len(plan.steps)} execution steps:", plan

        llm_plan = self.query_llm_for_plan(p, scene=scene)
        if llm_plan and llm_plan.steps:
            return f"Decomposed goal into {len(llm_plan.steps)} execution steps:", llm_plan

        llm_chat = self.query_llm_for_chat(p)
        if llm_chat:
            return llm_chat, None
        return self.offline_chat_reply(p), None

    def _parse_deterministic_plan(self, p: str) -> Optional[MacroPlan]:
        """Deterministic System 2 plan decomposition for offline and edge execution."""
        from servo.planner.tool_calling import parse_deterministic_intent

        parsed = parse_deterministic_intent(p, user_name=self.user_name)
        if parsed and parsed.steps:
            return parsed

        p_strip = p.strip()
        steps: List[PlanStep] = []

        # 1. "open <app> and (write|type) <text>"
        m_open_type = re.search(r"^(?:open|launch|start)\s+([a-zA-Z0-9_\s]+?)\s+and\s+(?:write|type|enter)\s+(.+)$", p_strip, re.I)
        if m_open_type:
            app = m_open_type.group(1).strip()
            raw_text = m_open_type.group(2).strip().strip('"\'')
            text = self.user_name if raw_text.lower() in ("my name", "my username") else raw_text
            steps.append(PlanStep(
                step_index=1,
                tool_name="launch_app",
                arguments={"app_name": app},
                rationale=f"Launch application '{app}'",
                canonical_action=AliasMapper.canonicalize_action("launch_app", app_name=app),
            ))
            steps.append(PlanStep(
                step_index=2,
                tool_name="type_text",
                arguments={"text": text, "target": "editor"},
                rationale=f"Type '{text}' into editor",
                canonical_action=AliasMapper.canonicalize_action("type_text", text=text, target="editor"),
            ))
            return MacroPlan(goal=p, steps=steps)

        # 2. "open <app> and click <target>"
        m_open_click = re.search(r"^(?:open|launch|start)\s+([a-zA-Z0-9_\s]+?)\s+and\s+(?:click|press)\s+(.+)$", p_strip, re.I)
        if m_open_click:
            app = m_open_click.group(1).strip()
            target = m_open_click.group(2).strip().strip('"\'')
            steps.append(PlanStep(
                step_index=1,
                tool_name="launch_app",
                arguments={"app_name": app},
                rationale=f"Launch application '{app}'",
                canonical_action=AliasMapper.canonicalize_action("launch_app", app_name=app),
            ))
            steps.append(PlanStep(
                step_index=2,
                tool_name="click_element",
                arguments={"target": target},
                rationale=f"Click '{target}'",
                canonical_action=AliasMapper.canonicalize_action("click_element", target=target),
            ))
            return MacroPlan(goal=p, steps=steps)

        # 3. "close <app>" or "close window"
        m_close = re.search(r"^(?:close|quit|exit)\s*([a-zA-Z0-9_\s]*)$", p_strip, re.I)
        if m_close:
            app = m_close.group(1).strip() or None
            steps.append(PlanStep(
                step_index=1,
                tool_name="close_window",
                arguments={"app_name": app} if app else {},
                rationale=f"Close application '{app or 'active window'}'",
                canonical_action=AliasMapper.canonicalize_action("close_window", app_name=app),
            ))
            return MacroPlan(goal=p, steps=steps)

        # 4. "open <app>"
        m_open_only = re.search(r"^(?:open|launch|start|switch to)\s+([a-zA-Z0-9_\s]+)$", p_strip, re.I)
        if m_open_only:
            app = m_open_only.group(1).strip()
            if app.lower() in APP_EXECUTABLE_MAP or not any(w in app.lower() for w in ["what", "how", "why"]):
                steps.append(PlanStep(
                    step_index=1,
                    tool_name="launch_app",
                    arguments={"app_name": app},
                    rationale=f"Launch application '{app}'",
                    canonical_action=AliasMapper.canonicalize_action("launch_app", app_name=app),
                ))
                return MacroPlan(goal=p, steps=steps)

        # 5. "write <text> into <target>"
        m_type_into = re.search(r"^(?:write|type|enter)\s+(.+?)\s+(?:in|into|on)\s+(.+)$", p_strip, re.I)
        if m_type_into:
            raw_text = m_type_into.group(1).strip().strip('"\'')
            target = m_type_into.group(2).strip().strip('"\'')
            text = self.user_name if raw_text.lower() in ("my name", "my username") else raw_text
            steps.append(PlanStep(
                step_index=1,
                tool_name="type_text",
                arguments={"text": text, "target": target},
                rationale=f"Type '{text}' into '{target}'",
                canonical_action=AliasMapper.canonicalize_action("type_text", text=text, target=target),
            ))
            return MacroPlan(goal=p, steps=steps)

        # 6. "write <text>"
        m_type_only = re.search(r"^(?:write|type|enter)\s+(.+)$", p_strip, re.I)
        if m_type_only:
            raw_text = m_type_only.group(1).strip().strip('"\'')
            text = self.user_name if raw_text.lower() in ("my name", "my username") else raw_text
            steps.append(PlanStep(
                step_index=1,
                tool_name="type_text",
                arguments={"text": text, "target": "editor"},
                rationale=f"Type '{text}'",
                canonical_action=AliasMapper.canonicalize_action("type_text", text=text, target="editor"),
            ))
            return MacroPlan(goal=p, steps=steps)

        # 7. "click <target>"
        m_click = re.search(r"^(?:click|press|select)\s+(.+)$", p_strip, re.I)
        if m_click:
            target = m_click.group(1).strip().strip('"\'')
            steps.append(PlanStep(
                step_index=1,
                tool_name="click_element",
                arguments={"target": target},
                rationale=f"Click '{target}'",
                canonical_action=AliasMapper.canonicalize_action("click_element", target=target),
            ))
            return MacroPlan(goal=p, steps=steps)

        return None


def launch_copilot() -> None:
    """Entrypoint for the floating on-screen copilot (web UI in a native window)."""
    from servo.daemon.copilot_app import run_copilot
    run_copilot()


if __name__ == "__main__":
    launch_copilot()
