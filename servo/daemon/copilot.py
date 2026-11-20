"""Floating On-Screen Desktop Copilot Chat Overlay (System 2 + System 1).

Always-on-top companion widget sitting on the screen edge, providing conversational
chat, LLM macro planning (System 2), alias mapping, and verified reflex arc execution (System 1).
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:
    tk = None  # type: ignore
    ttk = None  # type: ignore

from servo.actuation.alias_mapping import AliasMapper, CanonicalAction, CanonicalActionType
from servo.config import settings
from servo.control.handoff import PlanExecutionSummary, StepExecutionReport, SystemHandoffCoordinator
from servo.control.loop import ServoControlLoop
from servo.daemon.worker import worker
from servo.perception import windows
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
    "edge": "msedge.exe",
    "microsoft edge": "msedge.exe",
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
}


def launch_or_focus_app(app_name: str) -> Tuple[Optional[int], str]:
    """Find an existing window for the app or launch it and return (hwnd, title)."""
    app_key = app_name.strip().lower()
    exe_target = APP_EXECUTABLE_MAP.get(app_key, f"{app_key}.exe")

    # 1. Check if window is already open
    open_wins = windows.list_windows()
    for w in open_wins:
        proc = w.get("process_name", "").lower()
        title = w.get("title", "").lower()
        if (app_key in proc) or (app_key in title) or (exe_target in proc):
            hwnd = int(w["hwnd"])
            if sys.platform == "win32":
                import ctypes
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            return hwnd, w.get("title", app_name)

    # 2. Launch the application
    try:
        if sys.platform == "win32":
            subprocess.Popen([exe_target], shell=True)
        else:
            subprocess.Popen([exe_target])
    except Exception as e:
        return None, f"Failed to launch '{app_name}': {e}"

    # Wait for the window to appear
    for _ in range(25):
        time.sleep(0.2)
        fresh_wins = windows.list_windows()
        for w in fresh_wins:
            proc = w.get("process_name", "").lower()
            title = w.get("title", "").lower()
            if (app_key in proc) or (app_key in title) or (exe_target in proc):
                hwnd = int(w["hwnd"])
                if sys.platform == "win32":
                    import ctypes
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
                return hwnd, w.get("title", app_name)

    # Fallback to foreground window
    fg_hwnd = windows.get_foreground_window()
    if fg_hwnd and windows.is_window(fg_hwnd):
        return fg_hwnd, windows.get_window_title(fg_hwnd)

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

    def process_message(
        self,
        prompt: str,
        scene: Optional[SceneIndex] = None,
    ) -> Tuple[str, Optional[MacroPlan]]:
        """Process user input: either a conversational response or a multi-step MacroPlan."""
        p = prompt.strip()
        p_lower = p.lower()

        # 1. Identity & Name Learning
        m_name = re.search(r"^(?:my name is|call me|i am|i'm)\s+([a-zA-Z0-9_\s]+)$", p, re.I)
        if m_name and "how" not in p_lower and "what" not in p_lower:
            name = m_name.group(1).strip()
            self.user_name = name
            return f"Nice to meet you, {name}! I'll remember that. What would you like me to do on your computer?", None

        if any(q in p_lower for q in ["what is my name", "what's my name", "who am i", "do you know my name"]):
            return f"Your name is {self.user_name}! Let me know what you'd like me to help you with.", None

        # 2. Window & Desktop Introspection
        if any(w in p_lower for w in ["what windows are open", "what's on my screen", "what is on my screen", "list windows", "what is open", "show windows"]):
            wins = windows.list_windows()
            if not wins:
                return "I couldn't find any visible interactive application windows right now.", None
            top_wins = wins[:6]
            lines = ["Here are the open windows on your screen:"]
            for w in top_wins:
                t = w.get("title", "Untitled")
                h = w.get("hwnd")
                is_active = (h == self.active_hwnd)
                marker = " 🎯 [Active]" if is_active else ""
                lines.append(f"• {t} (HWND {h}){marker}")
            if len(wins) > 6:
                lines.append(f"...and {len(wins) - 6} more.")
            return "\n".join(lines), None

        # 3. Conversational Greetings & Small Talk
        if re.match(r"^(?:hi|hello|hey|good\s+(?:morning|afternoon|evening)|howdy)\b", p_lower):
            return f"Hello {self.user_name}! I'm Servo, your on-screen desktop copilot. Ask me anything or tell me what to do on your computer!", None

        if re.match(r"^(?:how are you|how's it going|how are you doing)\b", p_lower):
            return "I'm doing great and all systems are running smoothly! Ready to write notes, open apps, or chat about anything. What's on your mind?", None

        if re.match(r"^(?:who are you|what are you)\b", p_lower):
            return "I'm Servo, a local-first closed-loop desktop control copilot! You can chat with me about anything, or tell me to interact directly with your screen (typing, clicking, and managing apps).", None

        if re.match(r"^(?:what can you do|help|capabilities)\b", p_lower):
            return (
                "Here are some things you can ask me to do:\n"
                "• 'open notepad and write hello world'\n"
                "• 'write my name'\n"
                "• 'open calculator and click 7'\n"
                "• 'what windows are open?'\n"
                "• 'close notepad'\n"
                "• Or just ask me any general question to chat!"
            ), None

        # 4. Attempt System 2 LLM Plan Generation
        llm_plan = self.query_llm_for_plan(p, scene=scene)
        if llm_plan and llm_plan.steps:
            return f"Decomposed goal into {len(llm_plan.steps)} execution steps:", llm_plan

        # 5. Deterministic Zero-Config Intent Parser (Small-Model Fallback)
        plan = self._parse_deterministic_plan(p)
        if plan and plan.steps:
            return f"Decomposed goal into {len(plan.steps)} execution steps:", plan

        # 6. Conversational LLM or Knowledge Fallback
        llm_chat = self.query_llm_for_chat(p)
        if llm_chat:
            return llm_chat, None

        if "python" in p_lower:
            return "Python is a versatile programming language known for readability, powering backend services, machine learning, and automation runtimes like Servo!", None

        if "joke" in p_lower:
            return "Why do programmers prefer dark mode? Because light attracts bugs! 😄", None

        if "thank" in p_lower:
            return "You're very welcome! Let me know whenever you need anything else on your desktop.", None

        return (
            f"I hear you! I'm ready to help with that. If you'd like me to perform an action on your desktop, "
            f"try: 'open notepad and write ...', 'click ...', or 'what windows are open?'."
        ), None

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


class DesktopCopilotApp:
    """Floating On-Screen Desktop Copilot Chat Widget with System 1/2 Handoff."""

    def __init__(self) -> None:
        global tk, ttk
        if tk is None or "tkinter" in sys.modules:
            try:
                import tkinter as tk
                from tkinter import ttk
            except ImportError:
                pass

        if tk is None:
            raise RuntimeError(
                "Tkinter is required to run the DesktopCopilotApp UI. "
                "Please ensure Tkinter is installed in your Python environment."
            )

        worker.start()

        self.brain = CopilotBrain()
        self.current_target_hwnd: Optional[int] = None
        self.current_target_title: str = ""
        self._abort_requested = threading.Event()
        self._abort_lock = threading.Lock()
        self._is_executing = False

        self.root = tk.Tk()
        self.root.title("Servo Copilot")

        # Keep on top of other windows
        self.root.attributes("-topmost", True)

        # Geometry: 380px wide, docked on the right side of the screen
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = 380
        height = 620
        x_pos = screen_w - width - 24
        y_pos = (screen_h - height) // 2
        self.root.geometry(f"{width}x{height}+{x_pos}+{y_pos}")

        self.bg_color = "#0f172a"
        self.panel_color = "#1e293b"
        self.text_color = "#f8fafc"
        self.accent_color = "#3b82f6"
        self.user_bubble_color = "#2563eb"
        self.bot_bubble_color = "#334155"

        self.root.configure(bg=self.bg_color)
        self.stop_btn: Optional[tk.Button] = None
        self.send_btn: Optional[tk.Button] = None
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        # Header Frame
        header = tk.Frame(self.root, bg=self.panel_color, height=52)
        header.pack(fill=tk.X, side=tk.TOP)

        title_label = tk.Label(
            header,
            text="🤖 Servo Copilot",
            font=("Segoe UI", 12, "bold"),
            bg=self.panel_color,
            fg=self.text_color,
        )
        title_label.pack(side=tk.LEFT, padx=14, pady=12)

        self.status_pill = tk.Label(
            header,
            text="● Ready",
            font=("Segoe UI", 9, "bold"),
            bg=self.panel_color,
            fg="#34d399",
        )
        self.status_pill.pack(side=tk.RIGHT, padx=14, pady=12)

        # Active Window Banner
        self.target_banner = tk.Label(
            self.root,
            text="🎯 No active target locked",
            font=("Segoe UI", 8),
            bg="#111827",
            fg="#94a3b8",
            padx=10,
            pady=3,
            anchor="w",
        )
        self.target_banner.pack(fill=tk.X)

        # Quick Actions Frame (Chips)
        chips_frame = tk.Frame(self.root, bg=self.bg_color)
        chips_frame.pack(fill=tk.X, padx=10, pady=6)

        quick_chips = [
            ("📝 Notepad & Write", "open notepad and write hello world"),
            ("🧮 Calc", "open calculator and click 7"),
            ("🖥️ Open Windows", "what windows are open?"),
        ]
        for label, cmd in quick_chips:
            btn = tk.Button(
                chips_frame,
                text=label,
                font=("Segoe UI", 8),
                bg=self.panel_color,
                fg="#94a3b8",
                activebackground=self.accent_color,
                activeforeground="#fff",
                bd=0,
                padx=6,
                pady=4,
                cursor="hand2",
                command=lambda c=cmd: self._send_prompt(c),
            )
            btn.pack(side=tk.LEFT, padx=2)

        # Chat Message Stream Frame
        chat_container = tk.Frame(self.root, bg=self.bg_color)
        chat_container.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        self.chat_canvas = tk.Canvas(chat_container, bg=self.bg_color, highlightthickness=0)
        scrollbar = ttk.Scrollbar(chat_container, orient="vertical", command=self.chat_canvas.yview)
        self.scrollable_frame = tk.Frame(self.chat_canvas, bg=self.bg_color)

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.chat_canvas.configure(scrollregion=self.chat_canvas.bbox("all")),
        )
        self.chat_canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw", width=340)
        self.chat_canvas.configure(yscrollcommand=scrollbar.set)

        self.chat_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Welcome message
        self._add_message(
            "assistant",
            f"Hi {self.brain.user_name}! I'm Servo, your on-screen desktop copilot. Ask me anything or tell me to control apps on your screen!",
        )

        # Bottom Input Area
        input_frame = tk.Frame(self.root, bg=self.panel_color, height=60)
        input_frame.pack(fill=tk.X, side=tk.BOTTOM, padx=10, pady=10)

        self.entry = tk.Entry(
            input_frame,
            font=("Segoe UI", 10),
            bg="#0b1120",
            fg="#f8fafc",
            insertbackground="#fff",
            bd=1,
            relief=tk.FLAT,
        )
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 6), pady=10)
        self.entry.bind("<Return>", lambda e: self._on_enter())
        self.entry.focus_set()

        self.stop_btn = tk.Button(
            input_frame,
            text="⏹",
            font=("Segoe UI", 11, "bold"),
            bg="#475569",
            fg="#cbd5e1",
            activebackground="#ef4444",
            activeforeground="#fff",
            bd=0,
            padx=10,
            pady=6,
            cursor="hand2",
            state=tk.DISABLED,
            command=self._on_stop,
        )
        self.stop_btn.pack(side=tk.RIGHT, padx=(0, 4), pady=10)

        self.send_btn = tk.Button(
            input_frame,
            text="➤",
            font=("Segoe UI", 11, "bold"),
            bg=self.accent_color,
            fg="#fff",
            activebackground="#2563eb",
            activeforeground="#fff",
            bd=0,
            padx=12,
            pady=6,
            cursor="hand2",
            command=self._on_enter,
        )
        self.send_btn.pack(side=tk.RIGHT, padx=(0, 6), pady=10)

    def _set_status(self, text: str, color: str) -> None:
        def _apply():
            try:
                if self.status_pill:
                    self.status_pill.config(text=text, fg=color)
            except Exception:
                pass

        try:
            if threading.current_thread() is threading.main_thread():
                _apply()
            else:
                self.root.after(0, _apply)
        except Exception:
            pass

    def _set_status_if_idle(self, text: str, color: str) -> None:
        try:
            if not self._is_executing:
                self._set_status(text, color)
        except Exception:
            pass

    def _update_input_controls(self, is_executing: bool) -> None:
        if tk is None:
            return

        def _update():
            try:
                if is_executing:
                    if self.send_btn:
                        self.send_btn.config(state=tk.DISABLED, bg="#475569")
                    if self.stop_btn:
                        self.stop_btn.config(state=tk.NORMAL, bg="#ef4444", fg="#ffffff")
                    if self.entry:
                        self.entry.config(state=tk.DISABLED)
                else:
                    if self.send_btn:
                        self.send_btn.config(state=tk.NORMAL, bg=self.accent_color)
                    if self.stop_btn:
                        self.stop_btn.config(state=tk.DISABLED, bg="#475569", fg="#cbd5e1")
                    if self.entry:
                        self.entry.config(state=tk.NORMAL)
                        self.entry.focus_set()
            except Exception:
                pass

        try:
            if threading.current_thread() is threading.main_thread():
                _update()
            else:
                self.root.after(0, _update)
        except Exception:
            pass

    def _on_stop(self) -> None:
        """Request immediate cancellation of active execution."""
        with self._abort_lock:
            already_aborted = self._abort_requested.is_set()
            self._abort_requested.set()
        self._set_status("● Cancelled", "#ef4444")
        if not already_aborted:
            self._add_message("assistant", "🛑 Cancellation requested by user...")

        def _disable_stop():
            try:
                if self.stop_btn and tk is not None:
                    self.stop_btn.config(state=tk.DISABLED)
            except Exception:
                pass

        try:
            if threading.current_thread() is threading.main_thread():
                _disable_stop()
            else:
                self.root.after(0, _disable_stop)
        except Exception:
            pass

    def _on_abort(self) -> None:
        self._on_stop()

    def stop(self) -> None:
        self._on_stop()

    def cancel_execution(self) -> None:
        self._on_stop()

    def _on_close(self) -> None:
        """Ensure active execution is aborted when window is closed."""
        if self._is_executing:
            self._abort_requested.set()
        try:
            self.root.destroy()
        except Exception:
            pass

    def _update_target_banner(self, title: str, hwnd: Optional[int]) -> None:
        def _apply():
            try:
                if self.target_banner:
                    if hwnd:
                        self.target_banner.config(text=f"🎯 Target: {title} (HWND {hwnd})", fg="#38bdf8")
                    else:
                        self.target_banner.config(text="🎯 No active target locked", fg="#94a3b8")
            except Exception:
                pass

        try:
            if threading.current_thread() is threading.main_thread():
                _apply()
            else:
                self.root.after(0, _apply)
        except Exception:
            pass

    def _add_message(self, role: str, text: str) -> None:
        def _apply():
            if tk is None:
                return
            try:
                is_user = role == "user"
                frame = tk.Frame(self.scrollable_frame, bg=self.bg_color)
                frame.pack(fill=tk.X, pady=4, anchor="e" if is_user else "w")

                bubble_bg = self.user_bubble_color if is_user else self.bot_bubble_color
                fg_color = "#ffffff"

                bubble = tk.Label(
                    frame,
                    text=text,
                    font=("Segoe UI", 9),
                    bg=bubble_bg,
                    fg=fg_color,
                    wraplength=260,
                    justify=tk.LEFT,
                    padx=10,
                    pady=8,
                )
                bubble.pack(side=tk.RIGHT if is_user else tk.LEFT, padx=5)

                self.root.update_idletasks()
                self.chat_canvas.yview_moveto(1.0)
            except Exception:
                pass

        try:
            if threading.current_thread() is threading.main_thread():
                _apply()
            else:
                self.root.after(0, _apply)
        except Exception:
            pass

    def _on_enter(self) -> None:
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self._send_prompt(text)

    def _send_prompt(self, prompt: str) -> None:
        if self._is_executing:
            return

        with self._abort_lock:
            self._abort_requested.clear()
        self._is_executing = True
        self._update_input_controls(is_executing=True)

        self._add_message("user", prompt)
        self._set_status("● Planning...", "#f59e0b")

        def _worker():
            if sys.platform == "win32":
                try:
                    import pythoncom
                    pythoncom.CoInitialize()
                except Exception:
                    pass
            try:
                # SENSE active scene if window is attached
                scene = None
                if self.current_target_hwnd and windows.is_window(self.current_target_hwnd):
                    try:
                        sensor = UIASensor(self.current_target_hwnd)
                        scene = sensor.sense()
                    except Exception:
                        scene = None

                if self._abort_requested.is_set():
                    self._set_status("● Cancelled", "#ef4444")
                    self._add_message("assistant", "🛑 Plan generation cancelled by user.")
                    return

                reply, plan = self.brain.process_message(prompt, scene=scene)
                if reply:
                    self._add_message("assistant", reply)

                if self._abort_requested.is_set():
                    self._set_status("● Cancelled", "#ef4444")
                    self._add_message("assistant", "🛑 Execution cancelled by user before starting.")
                    return

                if plan:
                    self._execute_handoff_plan(plan)
            except Exception as e:
                self._set_status("● Error", "#ef4444")
                self._add_message("assistant", f"❌ Error: {e}")
            finally:
                if sys.platform == "win32":
                    try:
                        import pythoncom
                        pythoncom.CoUninitialize()
                    except Exception:
                        pass
                self._is_executing = False
                self._update_input_controls(is_executing=False)
                try:
                    self.root.after(3500, lambda: self._set_status_if_idle("● Ready", "#34d399"))
                except Exception:
                    pass

        threading.Thread(target=_worker, daemon=True).start()

    def _execute_handoff_plan(self, plan: MacroPlan) -> None:
        """Execute a System 2 plan using the SystemHandoffCoordinator."""
        total_steps = len(plan.steps)

        def on_plan_start(p: MacroPlan) -> None:
            self._set_status(f"● Running (0/{total_steps})...", "#38bdf8")

        def on_step_start(step: PlanStep) -> None:
            self._set_status(f"● Step {step.step_index}: {step.tool_name}...", "#38bdf8")

        def on_step_progress(step: PlanStep, report: StepExecutionReport) -> None:
            source_tag = f" [{report.perception_source.upper()}]" if report.perception_source else ""
            if report.success:
                msg = f"  ✅ Step {step.step_index}: {report.description} ({report.latency_ms}ms){source_tag}"
            elif "cancelled" in (report.error or "").lower() or self._abort_requested.is_set():
                msg = f"  🛑 Step {step.step_index} interrupted: {report.error}"
            else:
                msg = f"  ⚠️ Step {step.step_index} failed: {report.error} ({report.latency_ms}ms)"
            self._add_message("assistant", msg)

        def on_plan_complete(summary: PlanExecutionSummary) -> None:
            if summary.aborted:
                self._set_status("● Cancelled", "#ef4444")
                final_msg = f"🛑 Execution aborted by user ({summary.completed_steps}/{summary.total_steps} steps completed)."
            elif summary.success:
                self._set_status("● Completed", "#10b981")
                final_msg = f"🎉 Plan completed successfully ({summary.completed_steps}/{summary.total_steps} steps in {summary.total_latency_ms}ms)!"
            else:
                self._set_status("● Error", "#ef4444")
                final_msg = f"⚠️ Plan stopped: completed {summary.completed_steps}/{summary.total_steps} steps."
            self._add_message("assistant", final_msg)

        coordinator = SystemHandoffCoordinator(
            target_hwnd=self.current_target_hwnd,
            fast_mode=False,
            should_abort=self._abort_requested,
            on_plan_start=on_plan_start,
            on_step_start=on_step_start,
            on_step_progress=on_step_progress,
            on_plan_complete=on_plan_complete,
        )

        summary = coordinator.execute_plan(plan, app_launcher=launch_or_focus_app)

        if summary.active_hwnd:
            self.current_target_hwnd = summary.active_hwnd
            self.current_target_title = summary.active_title
            self.brain.active_hwnd = summary.active_hwnd
            self.brain.active_title = summary.active_title
            self._update_target_banner(summary.active_title, summary.active_hwnd)

        # Telemetry: Persist execution run and step traces to TraceStore (SQLite + JSONL)
        try:
            from servo.daemon.tasks import record_plan_trace
            record_plan_trace(summary, mode="assistant")
        except Exception as te:
            logger.debug("Failed to record copilot telemetry trace: %s", te)

    def run(self) -> None:
        try:
            self.root.mainloop()
        finally:
            worker.stop()


def launch_copilot() -> None:
    """Entrypoint for starting the Floating On-Screen Copilot."""
    app = DesktopCopilotApp()
    app.run()


if __name__ == "__main__":
    launch_copilot()
