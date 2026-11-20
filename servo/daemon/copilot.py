"""Floating On-Screen Desktop Copilot Chat Overlay.

Always-on-top companion widget sitting on the screen edge, allowing natural
language conversational desktop control ("open notepad and write hello world",
"who are you?", "what windows are open?", "write my name", etc.).
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

from servo.config import settings
from servo.control.loop import ServoControlLoop
from servo.daemon.worker import worker
from servo.perception import windows
from servo.perception.scene_index import SceneIndex
from servo.perception.uia_sensor import UIASensor

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
    """Conversational intelligence for Servo Copilot.
    
    Blends natural conversation about anything (small talk, general questions,
    identity, explanations) with context-aware Windows desktop automation.
    """

    def __init__(self, user_name: Optional[str] = None) -> None:
        self.user_name = user_name or resolve_default_user_name()
        self.history: List[Dict[str, str]] = []
        self.active_hwnd: Optional[int] = None
        self.active_title: str = ""

    def query_llm_if_available(self, prompt: str) -> Optional[str]:
        """Try querying configured OpenAI/Ollama endpoint for open-ended conversation."""
        base_url = settings.openai_base_url.rstrip("/")
        api_key = settings.openai_api_key
        model = settings.openai_model or "llama3"

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        system_prompt = (
            f"You are Servo, a helpful and friendly on-screen desktop copilot companion. "
            f"The user's name is {self.user_name}. You are conversing inside a compact desktop "
            f"floating widget. Answer concisely, warmly, and helpfully in 1-3 sentences."
        )

        messages = [{"role": "system", "content": system_prompt}]
        for m in self.history[-6:]:
            messages.append({"role": m["role"], "content": m["text"]})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 250,
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

    def process_message(self, prompt: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Parse user message into conversational reply and optional desktop action."""
        p = prompt.strip()
        p_lower = p.lower()

        # 1. Identity & Name Recognition
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

        # 4. Desktop Actions: Compound & Single-Step Commands
        action = self._extract_action(p)
        if action:
            return action.get("reply", "Executing command..."), action

        # 5. General Knowledge & Conversational LLM Fallback
        llm_reply = self.query_llm_if_available(p)
        if llm_reply:
            return llm_reply, None

        # 6. Local Conversational Knowledge Responses
        if "python" in p_lower:
            return "Python is a versatile high-level programming language known for readability, powering everything from web backends to machine learning and desktop automation like Servo!", None

        if "weather" in p_lower:
            return "I don't have access to live meteorological sensors right now, but check your Windows taskbar or browser for the local forecast!", None

        if "joke" in p_lower:
            return "Why do programmers prefer dark mode? Because light attracts bugs! 😄", None

        if "thank" in p_lower:
            return "You're very welcome! Let me know whenever you need anything else on your desktop.", None

        # Default fallback conversational response
        return (
            f"I hear you! I'm ready to help with that. If you want me to perform an action on your screen, "
            f"you can say things like 'open notepad and write ...', 'click ...', or 'what windows are open?'."
        ), None

    def _extract_action(self, p: str) -> Optional[Dict[str, Any]]:
        """Identify if the user prompt is a desktop automation action."""
        p_strip = p.strip()

        # Pattern: "open <app> and (write|type) <text>"
        m_open_type = re.search(r"^(?:open|launch|start)\s+([a-zA-Z0-9_\s]+?)\s+and\s+(?:write|type|enter)\s+(.+)$", p_strip, re.I)
        if m_open_type:
            app_name = m_open_type.group(1).strip()
            raw_text = m_open_type.group(2).strip().strip('"\'')
            text_to_type = self._resolve_text_payload(raw_text)
            return {
                "type": "open_and_type",
                "app": app_name,
                "text": text_to_type,
                "target": "editor",
                "reply": f"Opening {app_name} and typing '{text_to_type}'...",
            }

        # Pattern: "open <app> and click <target>"
        m_open_click = re.search(r"^(?:open|launch|start)\s+([a-zA-Z0-9_\s]+?)\s+and\s+(?:click|press)\s+(.+)$", p_strip, re.I)
        if m_open_click:
            app_name = m_open_click.group(1).strip()
            target = m_open_click.group(2).strip().strip('"\'')
            return {
                "type": "open_and_click",
                "app": app_name,
                "target": target,
                "reply": f"Opening {app_name} and clicking '{target}'...",
            }

        # Pattern: "close <app>" or "close window"
        m_close = re.search(r"^(?:close|quit|exit)\s*([a-zA-Z0-9_\s]*)$", p_strip, re.I)
        if m_close:
            app_name = m_close.group(1).strip()
            return {
                "type": "close",
                "app": app_name or None,
                "reply": f"Closing {app_name or 'active window'}...",
            }

        # Pattern: "open <app>"
        m_open_only = re.search(r"^(?:open|launch|start|switch to)\s+([a-zA-Z0-9_\s]+)$", p_strip, re.I)
        if m_open_only:
            app_name = m_open_only.group(1).strip()
            if app_name.lower() in APP_EXECUTABLE_MAP or not any(w in app_name.lower() for w in ["what", "how", "why"]):
                return {
                    "type": "open",
                    "app": app_name,
                    "reply": f"Launching {app_name}...",
                }

        # Pattern: "(write|type) <text> into <hint>"
        m_type_into = re.search(r"^(?:write|type|enter)\s+(.+?)\s+(?:in|into|on)\s+(.+)$", p_strip, re.I)
        if m_type_into:
            raw_text = m_type_into.group(1).strip().strip('"\'')
            target = m_type_into.group(2).strip().strip('"\'')
            text_to_type = self._resolve_text_payload(raw_text)
            return {
                "type": "type",
                "text": text_to_type,
                "target": target,
                "reply": f"Typing '{text_to_type}' into {target}...",
            }

        # Pattern: "(write|type) <text>"
        m_type_only = re.search(r"^(?:write|type|enter)\s+(.+)$", p_strip, re.I)
        if m_type_only:
            raw_text = m_type_only.group(1).strip().strip('"\'')
            text_to_type = self._resolve_text_payload(raw_text)
            return {
                "type": "type",
                "text": text_to_type,
                "target": "editor",
                "reply": f"Typing '{text_to_type}'...",
            }

        # Pattern: "click <target>"
        m_click = re.search(r"^(?:click|press|select)\s+(.+)$", p_strip, re.I)
        if m_click:
            target = m_click.group(1).strip().strip('"\'')
            return {
                "type": "click",
                "target": target,
                "reply": f"Clicking '{target}'...",
            }

        return None

    def _resolve_text_payload(self, text: str) -> str:
        """Resolve dynamic references like 'my name' to actual values."""
        t_lower = text.lower().strip()
        if t_lower in ("my name", "my username", "my user name"):
            return self.user_name
        return text


class DesktopCopilotApp:
    """Floating On-Screen Desktop Copilot Chat Widget."""

    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        worker.start()

        self.brain = CopilotBrain()
        self.current_target_hwnd: Optional[int] = None
        self.current_target_title: str = ""

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
        self._build_ui()

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
            f"Hi {self.brain.user_name}! I'm Servo, your on-screen desktop copilot. We can chat about anything or you can tell me to control apps on your screen!",
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

        send_btn = tk.Button(
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
        send_btn.pack(side=tk.RIGHT, padx=(0, 10), pady=10)

    def _set_status(self, text: str, color: str) -> None:
        self.status_pill.config(text=text, fg=color)

    def _update_target_banner(self, title: str, hwnd: Optional[int]) -> None:
        if hwnd:
            self.target_banner.config(text=f"🎯 Target: {title} (HWND {hwnd})", fg="#38bdf8")
        else:
            self.target_banner.config(text="🎯 No active target locked", fg="#94a3b8")

    def _add_message(self, role: str, text: str) -> None:
        import tkinter as tk

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

    def _on_enter(self) -> None:
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self._send_prompt(text)

    def _send_prompt(self, prompt: str) -> None:
        self._add_message("user", prompt)
        self._set_status("● Thinking...", "#f59e0b")

        def _worker():
            try:
                reply, action = self.brain.process_message(prompt)
                if reply:
                    self.root.after(0, lambda: self._add_message("assistant", reply))

                if action:
                    self._execute_action(action)
            except Exception as e:
                self.root.after(0, lambda: self._add_message("assistant", f"❌ Error: {e}"))
            finally:
                self.root.after(0, lambda: self._set_status("● Ready", "#34d399"))

        threading.Thread(target=_worker, daemon=True).start()

    def _get_external_foreground_window(self) -> Tuple[Optional[int], str]:
        """Find the top visible window that is not this Copilot widget."""
        my_pid = os.getpid()
        copilot_hwnd = None
        try:
            copilot_hwnd = self.root.winfo_id()
        except Exception:
            pass

        # Check foreground window first
        fg = windows.get_foreground_window()
        if fg and windows.is_window(fg):
            title = windows.get_window_title(fg)
            if fg != copilot_hwnd and title != "Servo Copilot":
                for w in windows.list_windows():
                    if int(w["hwnd"]) == fg and w.get("pid") != my_pid:
                        return fg, title

        # Search visible windows list for the top external window
        for w in windows.list_windows():
            hwnd = int(w["hwnd"])
            title = w.get("title", "")
            pid = w.get("pid")
            if hwnd != copilot_hwnd and title != "Servo Copilot" and pid != my_pid and title.strip():
                return hwnd, title

        return None, ""

    def _execute_action(self, action: Dict[str, Any]) -> None:
        """Execute a desktop automation action on the system."""
        act_type = action.get("type")

        # 1. Action: Launch or Focus App
        if act_type in ("open", "open_and_type", "open_and_click"):
            app_name = action.get("app", "")
            self.root.after(0, lambda: self._set_status(f"● Launching {app_name}...", "#38bdf8"))
            hwnd, title = launch_or_focus_app(app_name)
            if not hwnd:
                self.root.after(0, lambda: self._add_message("assistant", f"⚠️ Could not find window for '{app_name}': {title}"))
                return
            self.current_target_hwnd = hwnd
            self.current_target_title = title
            self.brain.active_hwnd = hwnd
            self.brain.active_title = title
            self.root.after(0, lambda: self._update_target_banner(title, hwnd))
            self.root.after(0, lambda: self._add_message("assistant", f"🎯 Connected to '{title}' (HWND {hwnd})"))

            if act_type == "open":
                self.root.after(0, lambda: self._add_message("assistant", f"✅ Window '{title}' is ready."))
                return

        # 2. Action: Close App
        if act_type == "close":
            app_name = action.get("app")
            target_h = self.current_target_hwnd
            target_t = self.current_target_title
            if app_name:
                h, t = launch_or_focus_app(app_name)
                if h:
                    target_h, target_t = h, t

            if target_h and windows.is_window(target_h):
                if sys.platform == "win32":
                    import win32gui, win32con
                    win32gui.PostMessage(target_h, win32con.WM_CLOSE, 0, 0)
                self.root.after(0, lambda: self._add_message("assistant", f"✅ Closed window '{target_t}'."))
                self.current_target_hwnd = None
                self.current_target_title = ""
                self.root.after(0, lambda: self._update_target_banner("", None))
            else:
                self.root.after(0, lambda: self._add_message("assistant", "⚠️ No target window active to close."))
            return

        # 3. Resolve Target Window for Typing or Clicking
        target_hwnd = self.current_target_hwnd
        window_title = self.current_target_title

        if not target_hwnd or not windows.is_window(target_hwnd):
            found_h, found_t = self._get_external_foreground_window()
            if found_h:
                target_hwnd = found_h
                window_title = found_t
                self.current_target_hwnd = target_hwnd
                self.current_target_title = window_title
                self.root.after(0, lambda: self._update_target_banner(window_title, target_hwnd))
                self.root.after(0, lambda: self._add_message("assistant", f"🎯 Attached to active window: '{window_title}' (HWND {target_hwnd})"))
            else:
                self.root.after(0, lambda: self._add_message("assistant", "⚠️ No target window open or active on screen. Please open an application first!"))
                return

        # Bring target window to foreground before actuating
        if sys.platform == "win32" and target_hwnd:
            import ctypes
            ctypes.windll.user32.SetForegroundWindow(target_hwnd)
            time.sleep(0.15)

        # 4. Execute Actuation via Closed Loop Reflex Arc
        loop = ServoControlLoop(hwnd=target_hwnd, fast_mode=False)

        if act_type in ("type", "open_and_type"):
            text = action.get("text", "")
            target_hint = action.get("target", "editor")
            self.root.after(0, lambda: self._set_status("● Typing...", "#f59e0b"))
            res = loop.execute_subgoal(
                step_index=1,
                sub_goal=f"type '{text}' into {target_hint}",
                target_label_hint=target_hint,
                action_type="type",
                text_to_type=text,
                expected_outcome=f"Typed text",
            )
        elif act_type in ("click", "open_and_click"):
            target_hint = action.get("target", "")
            self.root.after(0, lambda: self._set_status(f"● Clicking '{target_hint}'...", "#f59e0b"))
            res = loop.execute_subgoal(
                step_index=1,
                sub_goal=f"click {target_hint}",
                target_label_hint=target_hint,
                action_type="click",
                expected_outcome=f"Clicked {target_hint}",
            )
        else:
            return

        latency = res.step_log.latency_ms
        if res.success:
            msg = f"✅ Done! Action completed in {latency}ms."
            if res.attempts_used > 1:
                msg += f" (Resolved via Attempt {res.attempts_used} fallback)"
            self.root.after(0, lambda: self._add_message("assistant", msg))
        else:
            err = res.step_log.error or "Action verification did not pass."
            self.root.after(0, lambda: self._add_message("assistant", f"⚠️ Failed: {err} ({latency}ms)"))

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
