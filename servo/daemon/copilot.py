"""Floating On-Screen Desktop Copilot Chat Overlay.

Always-on-top companion widget sitting on the screen edge, allowing natural
language conversational desktop control ("open notepad and write hello world").
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from servo.control.loop import ServoControlLoop
from servo.daemon.worker import worker
from servo.perception import windows
from servo.perception.scene_index import SceneIndex
from servo.perception.uia_sensor import UIASensor


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
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
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
    for _ in range(20):
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


def parse_compound_prompt(prompt: str) -> Dict[str, Any]:
    """Parse natural user prompts into app targets, actions, and text payloads."""
    p = prompt.strip()
    result: Dict[str, Any] = {
        "app_to_open": None,
        "action": "click",
        "target_hint": "",
        "text_to_type": "",
    }

    # Pattern: "open <app> and (write|type) <text>"
    m_open_type = re.search(r"(?:open|launch|start)\s+([a-zA-Z0-9_\s]+?)\s+and\s+(?:write|type|enter)\s+(.+)", p, re.I)
    if m_open_type:
        result["app_to_open"] = m_open_type.group(1).strip()
        result["action"] = "type"
        raw_text = m_open_type.group(2).strip().strip('"\'')
        result["text_to_type"] = raw_text
        result["target_hint"] = "editor"
        return result

    # Pattern: "open <app> and click <target>"
    m_open_click = re.search(r"(?:open|launch|start)\s+([a-zA-Z0-9_\s]+?)\s+and\s+(?:click|press)\s+(.+)", p, re.I)
    if m_open_click:
        result["app_to_open"] = m_open_click.group(1).strip()
        result["action"] = "click"
        result["target_hint"] = m_open_click.group(2).strip().strip('"\'')
        return result

    # Pattern: "open <app>"
    m_open_only = re.search(r"^(?:open|launch|start)\s+([a-zA-Z0-9_\s]+)$", p, re.I)
    if m_open_only:
        result["app_to_open"] = m_open_only.group(1).strip()
        result["action"] = "focus"
        return result

    # Pattern: "(write|type) <text> (in|into|on) <hint>"
    m_type_into = re.search(r"(?:write|type|enter)\s+(.+?)\s+(?:in|into|on)\s+(.+)", p, re.I)
    if m_type_into:
        result["action"] = "type"
        result["text_to_type"] = m_type_into.group(1).strip().strip('"\'')
        result["target_hint"] = m_type_into.group(2).strip().strip('"\'')
        return result

    # Pattern: "(write|type) <text>"
    m_type_only = re.search(r"^(?:write|type|enter)\s+(.+)$", p, re.I)
    if m_type_only:
        result["action"] = "type"
        result["text_to_type"] = m_type_only.group(1).strip().strip('"\'')
        result["target_hint"] = "editor"
        return result

    # Pattern: "click <hint>"
    m_click = re.search(r"^(?:click|press|select)\s+(.+)$", p, re.I)
    if m_click:
        result["action"] = "click"
        result["target_hint"] = m_click.group(1).strip().strip('"\'')
        return result

    # Default fallback: try clicking or finding prompt in active window
    result["target_hint"] = p
    return result


class DesktopCopilotApp:
    """Floating On-Screen Desktop Copilot Chat Widget."""

    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        worker.start()

        self.root = tk.Tk()
        self.root.title("Servo Copilot")

        # Keep on top of other windows
        self.root.attributes("-topmost", True)

        # Geometry: 380px wide, docked on the right side of the screen
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = 380
        height = 600
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
        header = tk.Frame(self.root, bg=self.panel_color, height=50)
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

        # Quick Actions Frame (Chips)
        chips_frame = tk.Frame(self.root, bg=self.bg_color)
        chips_frame.pack(fill=tk.X, padx=10, pady=6)

        quick_chips = [
            ("📝 Open Notepad & Write", "open notepad and write hello world"),
            ("🧮 Open Calc", "open calculator and click 7"),
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
                padx=8,
                pady=4,
                cursor="hand2",
                command=lambda c=cmd: self._send_prompt(c),
            )
            btn.pack(side=tk.LEFT, padx=3)

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
            "Hi! I'm Servo, your on-screen desktop copilot. Tell me what to do on your computer!",
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
        self._set_status("● Working...", "#f59e0b")

        def _worker():
            try:
                self._execute_instruction(prompt)
            except Exception as e:
                self.root.after(0, lambda: self._add_message("assistant", f"❌ Error: {e}"))
            finally:
                self.root.after(0, lambda: self._set_status("● Ready", "#34d399"))

        threading.Thread(target=_worker, daemon=True).start()

    def _execute_instruction(self, prompt: str) -> None:
        parsed = parse_compound_prompt(prompt)
        app_name = parsed["app_to_open"]
        action = parsed["action"]
        target_hint = parsed["target_hint"]
        text_to_type = parsed["text_to_type"]

        target_hwnd: Optional[int] = None
        window_title: str = ""

        # Step 1: Open or Focus App if specified
        if app_name:
            self.root.after(0, lambda: self._add_message("assistant", f"🚀 Launching/focusing '{app_name}'..."))
            target_hwnd, window_title = launch_or_focus_app(app_name)
            if not target_hwnd:
                self.root.after(0, lambda: self._add_message("assistant", f"⚠️ Could not find window: {window_title}"))
                return
            self.root.after(0, lambda: self._add_message("assistant", f"🎯 Connected to '{window_title}' (HWND {target_hwnd})"))

            if action == "focus":
                self.root.after(0, lambda: self._add_message("assistant", f"✅ Window '{window_title}' is ready."))
                return
        else:
            # Use current active window (excluding copilot window)
            fg = windows.get_foreground_window()
            if fg and windows.is_window(fg):
                target_hwnd = fg
                window_title = windows.get_window_title(fg)
            else:
                self.root.after(0, lambda: self._add_message("assistant", "⚠️ No target application specified or active."))
                return

        # Step 2: Execute Action via Closed-Loop Reflex Arc
        self.root.after(0, lambda: self._add_message("assistant", f"⚡ Executing {action} on target: '{target_hint or text_to_type}'..."))

        loop = ServoControlLoop(hwnd=target_hwnd, fast_mode=False)

        res = loop.execute_subgoal(
            step_index=1,
            sub_goal=prompt,
            target_label_hint=target_hint or text_to_type,
            action_type=action,
            text_to_type=text_to_type,
            expected_outcome=f"Completed {action}",
        )

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
