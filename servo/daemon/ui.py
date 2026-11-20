"""Desktop UI, System Tray notifications, Transparent HUD Overlay, and Native GUI.

See ADR-14 in SERVO_ARCHITECTURE_V2.md.
Renders real-time bounding boxes, element tags, and provides an interactive desktop GUI.
"""

from __future__ import annotations

import html
import sys
import threading
from typing import Any, Dict, List, Optional
from servo.perception.scene_index import SceneIndex, SceneElement


class ServoHUDOverlay:
    """Manages rendering transparent bounding box overlays on top of the active window."""

    def __init__(self) -> None:
        self._overlay_active = False
        self._lock = threading.Lock()

    def generate_svg_overlay(self, scene: SceneIndex, highlight_id: Optional[int] = None) -> str:
        """Generate a lightweight SVG overlay representing detected elements."""
        w = int(scene.viewport.get("width", 800))
        h = int(scene.viewport.get("height", 600))
        lines = [f'<svg width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg">']
        lines.append('<style>.box { fill: none; stroke-width: 2; } .txt { font-family: sans-serif; font-size: 12px; fill: white; }</style>')

        for el in scene.elements.values():
            s_left, s_top, s_right, s_bottom = el.bbox_screen
            x = s_left - int(scene.viewport.get("x", 0))
            y = s_top - int(scene.viewport.get("y", 0))
            bw = max(2, s_right - s_left)
            bh = max(2, s_bottom - s_top)

            is_highlight = el.element_id == highlight_id
            stroke_color = "#10b981" if is_highlight else "#3b82f6"
            fill_color = "rgba(16, 185, 129, 0.2)" if is_highlight else "rgba(59, 130, 246, 0.15)"

            lines.append(
                f'<rect x="{x}" y="{y}" width="{bw}" height="{bh}" '
                f'stroke="{stroke_color}" fill="{fill_color}" class="box"/>'
            )
            lines.append(
                f'<rect x="{x}" y="{max(0, y - 18)}" width="{min(120, bw)}" height="18" fill="{stroke_color}"/>'
            )
            label_text = html.escape(f"[{el.element_id}] {el.label[:12]}", quote=True)
            lines.append(f'<text x="{x + 4}" y="{max(12, y - 5)}" class="txt">{label_text}</text>')

        lines.append("</svg>")
        return "\n".join(lines)


class ServoTrayUI:
    """Provides a desktop tray notification and emergency pause toggle."""

    def __init__(self, on_pause: Optional[callable] = None, on_abort: Optional[callable] = None) -> None:
        self.on_pause = on_pause
        self.on_abort = on_abort
        self.hud = ServoHUDOverlay()

    def show_alert(self, title: str, message: str) -> None:
        """Display an OS notification message."""
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(0, message, title, 0x40 | 0x1)  # MB_ICONINFORMATION | MB_OKCANCEL
            except Exception:
                pass
        print(f"[{title}] {message}")


def launch_desktop_gui() -> None:
    """Launch the interactive native desktop GUI for inspecting and driving Windows controls."""
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except ImportError:
        print("Tkinter is required for the desktop GUI.", file=sys.stderr)
        return

    from servo.perception import windows
    from servo.daemon.tasks import sense_scene, run_goal
    from servo.daemon.worker import worker

    worker.start()

    root = tk.Tk()
    root.title("Servo Desktop Control Runtime — Control Center")
    root.geometry("880x640")
    root.minsize(720, 480)

    # Style
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass

    # Header Frame
    header_frame = ttk.Frame(root, padding=10)
    header_frame.pack(fill=tk.X)
    ttk.Label(header_frame, text="Servo Desktop Control", font=("Segoe UI", 16, "bold")).pack(side=tk.LEFT)
    ttk.Label(header_frame, text="Closed-Loop Neuro-Symbolic Automation", font=("Segoe UI", 10, "italic")).pack(side=tk.LEFT, padx=10, pady=4)

    # Window Selection Frame
    win_frame = ttk.LabelFrame(root, text="Target Window Selection", padding=10)
    win_frame.pack(fill=tk.X, padx=10, pady=5)

    window_list_data: List[Dict[str, Any]] = []
    window_combo = ttk.Combobox(win_frame, state="readonly", width=70)
    window_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

    def refresh_windows():
        nonlocal window_list_data
        try:
            window_list_data = windows.list_windows()
            display_values = [f"HWND: {w['hwnd']} | {w['process_name']} | {w['title']}" for w in window_list_data]
            window_combo["values"] = display_values
            if display_values:
                window_combo.current(0)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to list windows: {e}")

    refresh_btn = ttk.Button(win_frame, text="Refresh Windows", command=refresh_windows)
    refresh_btn.pack(side=tk.LEFT, padx=5)

    # Action Frame
    action_frame = ttk.LabelFrame(root, text="Action & Goal Dispatch", padding=10)
    action_frame.pack(fill=tk.X, padx=10, pady=5)

    ttk.Label(action_frame, text="Goal:").pack(side=tk.LEFT, padx=5)
    goal_entry = ttk.Entry(action_frame, width=45)
    goal_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
    goal_entry.insert(0, 'Click "File"')

    fast_var = tk.BooleanVar(value=False)
    fast_check = ttk.Checkbutton(action_frame, text="Fast Mode", variable=fast_var)
    fast_check.pack(side=tk.LEFT, padx=5)

    def get_selected_hwnd() -> Optional[int]:
        idx = window_combo.current()
        if idx >= 0 and idx < len(window_list_data):
            return int(window_list_data[idx]["hwnd"])
        return None

    # Treeview Frame for Elements
    elem_frame = ttk.LabelFrame(root, text="Detected Window Elements (Scene Index)", padding=10)
    elem_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

    cols = ("id", "label", "role", "bbox", "value")
    tree = ttk.Treeview(elem_frame, columns=cols, show="headings", height=8)
    tree.heading("id", text="ID")
    tree.heading("label", text="Element Label")
    tree.heading("role", text="Role")
    tree.heading("bbox", text="Screen BBox")
    tree.heading("value", text="Value")

    tree.column("id", width=50, anchor=tk.CENTER)
    tree.column("label", width=220)
    tree.column("role", width=120)
    tree.column("bbox", width=180)
    tree.column("value", width=150)

    tree_scroll = ttk.Scrollbar(elem_frame, orient=tk.VERTICAL, command=tree.yview)
    tree.configure(yscrollcommand=tree_scroll.set)
    tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def on_element_select(event):
        sel = tree.selection()
        if sel:
            item = tree.item(sel[0])
            label = item["values"][1]
            role = item["values"][2]
            if role == "Edit":
                goal_entry.delete(0, tk.END)
                goal_entry.insert(0, f'Type "Hello" into "{label}"')
            else:
                goal_entry.delete(0, tk.END)
                goal_entry.insert(0, f'Click "{label}"')

    tree.bind("<<TreeviewSelect>>", on_element_select)

    def inspect_scene():
        hwnd = get_selected_hwnd()
        if hwnd is None:
            messagebox.showwarning("Warning", "Please select a target window first.")
            return
        try:
            tree.delete(*tree.get_children())
            scene_data = sense_scene(hwnd)
            elements = scene_data.get("elements", {})
            for el in elements.values():
                tree.insert("", tk.END, values=(
                    el.get("element_id"),
                    el.get("label"),
                    el.get("role"),
                    str(el.get("bbox_screen")),
                    el.get("value") or "",
                ))
            log(f"Sensed {len(elements)} controls in '{scene_data.get('window_title')}'.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to sense scene: {e}")

    inspect_btn = ttk.Button(win_frame, text="Inspect Controls", command=inspect_scene)
    inspect_btn.pack(side=tk.LEFT, padx=5)

    # Log Output Console
    log_frame = ttk.LabelFrame(root, text="Execution Trace & Feedback", padding=10)
    log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

    log_text = tk.Text(log_frame, height=6, bg="#1e293b", fg="#f8fafc", font=("Consolas", 9))
    log_text.pack(fill=tk.BOTH, expand=True)

    def log(msg: str):
        log_text.insert(tk.END, f"{msg}\n")
        log_text.see(tk.END)

    def execute_goal():
        hwnd = get_selected_hwnd()
        if hwnd is None:
            messagebox.showwarning("Warning", "Please select a target window.")
            return
        goal = goal_entry.get().strip()
        if not goal:
            messagebox.showwarning("Warning", "Please enter a goal instruction.")
            return

        log(f">> Dispatching goal: '{goal}' to HWND {hwnd}...")

        def _worker():
            try:
                res = run_goal(goal=goal, hwnd=hwnd, fast_mode=fast_var.get())
                status = "SUCCESS" if res.get("success") else "FAILED"
                latency = res.get("metrics", {}).get("total_latency_ms", 0)
                root.after(0, lambda: log(f"[{status}] Latency: {latency}ms | Run ID: {res.get('run_id')}"))
                # Refresh elements
                root.after(0, inspect_scene)
            except Exception as e:
                root.after(0, lambda: log(f"[ERROR] {e}"))

        threading.Thread(target=_worker, daemon=True).start()

    exec_btn = ttk.Button(action_frame, text="Execute Action", command=execute_goal)
    exec_btn.pack(side=tk.LEFT, padx=5)

    refresh_windows()
    log("Servo GUI initialized. Select a window above and click 'Inspect Controls'.")

    try:
        root.mainloop()
    finally:
        worker.stop()


hud_overlay = ServoHUDOverlay()
tray_ui = ServoTrayUI()

if __name__ == "__main__":
    launch_desktop_gui()
