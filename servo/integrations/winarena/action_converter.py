"""Converts Servo internal tool calls and step records into standard WindowsAgentArena / OSWorld pyautogui actions."""

from __future__ import annotations

import ast
import re
from typing import Any, Dict, List, Optional


def parse_call_args(call_str: str) -> Dict[str, Any]:
    """Parse tool call arguments from a string like:
    click_element(target='7') or launch_app(app_name='calculator')
    """
    call_str = call_str.strip()
    match = re.match(r"^(\w+)\((.*)\)$", call_str, re.DOTALL)
    if not match:
        return {}
    _, args_part = match.groups()
    try:
        expr = ast.parse(f"call({args_part})", mode="eval")
        kwargs: Dict[str, Any] = {}
        for kw in expr.body.keywords:  # type: ignore[attr-defined]
            kwargs[kw.arg] = ast.literal_eval(kw.value)
        return kwargs
    except Exception:
        # Fallback simple regex extraction for key='value' or key="value"
        pairs = re.findall(r"(\w+)=(['\"]?)(.*?)\2(?:,|$)", args_part)
        return {k: v for k, _, v in pairs}


def tool_call_to_pyautogui(call_str: str, default_coords: Optional[tuple[int, int]] = None) -> str:
    """Translate a Servo tool call string into valid Python code matching the
    WindowsAgentArena / OSWorld pyautogui action space.
    """
    call_str = call_str.strip()
    if not call_str:
        return "pyautogui.sleep(0.1)"

    m = re.match(r"^(\w+)\((.*)\)$", call_str, re.DOTALL)
    if not m:
        return f"# Unrecognized call: {call_str}"

    tool_name = m.group(1).lower()
    args = parse_call_args(call_str)

    # 1. Clicks
    if tool_name in ("click", "click_element"):
        x = args.get("x")
        y = args.get("y")
        if x is not None and y is not None:
            return f"pyautogui.click({int(x)}, {int(y)})"
        elif default_coords:
            return f"pyautogui.click({default_coords[0]}, {default_coords[1]})"
        else:
            target = args.get("target", "")
            return f"# click('{target}') resolved via UIA/Mirage\npyautogui.click()"

    if tool_name == "double_click":
        x = args.get("x")
        y = args.get("y")
        if x is not None and y is not None:
            return f"pyautogui.doubleClick({int(x)}, {int(y)})"
        return "pyautogui.doubleClick()"

    if tool_name == "right_click":
        x = args.get("x")
        y = args.get("y")
        if x is not None and y is not None:
            return f"pyautogui.rightClick({int(x)}, {int(y)})"
        return "pyautogui.rightClick()"

    # 2. Typing
    if tool_name in ("type_text", "write", "type"):
        text = args.get("text", "")
        # Sanitize single-quotes
        escaped = repr(str(text))
        return f"pyautogui.write({escaped}, interval=0.05)"

    # 3. Hotkeys
    if tool_name in ("send_hotkey", "hotkey"):
        keys = args.get("keys", [])
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split("+")]
        formatted = ", ".join(repr(str(k).lower()) for k in keys)
        return f"pyautogui.hotkey({formatted})"

    # 4. App Launch
    if tool_name == "launch_app":
        app = args.get("app_name", "")
        return f"import subprocess; subprocess.Popen({repr(str(app))}, shell=True)"

    # 5. Browser URL Navigation
    if tool_name == "open_url":
        url = args.get("url", "")
        browser = args.get("browser", None)
        if browser:
            return f"import webbrowser; webbrowser.get({repr(str(browser))}).open({repr(str(url))})"
        return f"import webbrowser; webbrowser.open({repr(str(url))})"

    # 6. Shell & System Commands
    if tool_name == "run_command":
        cmd = args.get("command", "")
        shell = args.get("shell", "powershell")
        if shell == "powershell":
            return f"import subprocess; subprocess.run(['powershell', '-NoProfile', '-Command', {repr(str(cmd))}], capture_output=True)"
        return f"import subprocess; subprocess.run({repr(str(cmd))}, shell=True, capture_output=True)"

    # 7. File Operations
    if tool_name == "read_file":
        path = args.get("path", "")
        return f"import pathlib; pathlib.Path({repr(str(path))}).read_text(encoding='utf-8', errors='replace')"

    if tool_name == "write_file":
        path = args.get("path", "")
        content = args.get("content", "")
        return f"import pathlib; pathlib.Path({repr(str(path))}).write_text({repr(str(content))}, encoding='utf-8')"

    # 8. Wait / Sleep
    if tool_name == "wait":
        secs = args.get("duration_seconds", args.get("seconds", 1.0))
        return f"import time; time.sleep({float(secs)})"

    # 9. Scroll
    if tool_name == "scroll":
        direction = str(args.get("direction", "down")).lower()
        amount = int(args.get("amount", 3)) * (-100 if "down" in direction else 100)
        return f"pyautogui.scroll({amount})"

    return f"# {call_str}"


def steps_to_pyautogui_script(steps: List[Any]) -> str:
    """Format a list of StepRecord or tool calls into an executable Python script
    suitable for WindowsAgentArena trace recording.
    """
    lines = [
        "# WindowsAgentArena Generated Trajectory from Servo Runtime",
        "import pyautogui",
        "pyautogui.PAUSE = 0.2",
        "",
    ]
    for s in steps:
        call = getattr(s, "call", "") or str(s)
        action_line = tool_call_to_pyautogui(call)
        lines.append(action_line)
    lines.append("")
    lines.append("# DONE")
    return "\n".join(lines)
