"""Direct system tools: read/list/write files and run shell commands, without the GUI.

The GUI path (click, type) is slow and fragile for work that is really about files or
commands. These tools do that work directly, and their output goes back to System 2 so
it can act on what a command printed or a file contains.

Safety model (the GUI tools' gate applies on top, as max(Kev, floor)):

* Reached only through the deterministic command grammar (``parse_system_intent``),
  never proposed by the on-device Needle model, so a misparse cannot run a command.
* Every shell command is at least medium risk, so assistant mode always asks you and
  shows the exact command. Destructive or download-and-run commands are high.
* Writes are medium; overwriting a file, writing a script/executable, or writing into
  the Startup folder is high.
* Reads are low, except credential stores (high). Servo's own folder (``~/.servo``:
  API token, encrypted keys, chat memory) is never readable or writable here: file
  contents are sent to the System 2 provider.
* Refused entirely in harness mode, which runs without confirmation and is sandboxed
  to allowlisted windows; a shell would escape that sandbox. Also off when
  ``SERVO_SYSTEM_TOOLS=false``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from servo.config import settings

SYSTEM_TOOLS = ("read_file", "list_folder", "write_file", "run_command")

MAX_COMMAND_CHARS = 1000      # longer commands can't be read in a confirmation prompt
MAX_WRITE_CHARS = 200_000
MAX_READ_BYTES = 1_000_000
OUTPUT_CLIP = 8000            # chars of output kept for the trace and System 2

_SCRIPT_EXTS = frozenset({
    ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".exe",
    ".com", ".scr", ".msi", ".dll", ".lnk", ".reg", ".hta", ".cpl",
})
_SECRET_PARTS = (
    ".ssh", ".aws", ".azure", ".gnupg", ".kube", ".docker", ".git-credentials", ".netrc",
    "login data", "cookies", "web data", "local state", "credentials", "microsoft\\protect",
    "microsoft\\credentials", "microsoft\\vault",
)
_SECRET_EXTS = frozenset({".pem", ".key", ".pfx", ".p12", ".kdbx", ".ppk"})


class SystemToolError(Exception):
    """A system tool refused or failed; the message is shown to the user and System 2."""


@dataclass
class ToolResult:
    output: str
    summary: str


# ------------------------------------------------------------------ paths

def servo_home() -> Path:
    from servo.paths import servo_home as _home
    return _home().resolve()


def resolve_path(raw: str) -> Path:
    """'desktop\\notes.txt', '~/x', '%USERPROFILE%\\x' or an absolute path -> absolute Path.

    Relative paths are taken from the home folder, and a leading 'desktop',
    'documents' or 'downloads' maps to the real (possibly OneDrive) folder.
    """
    from servo.cognition.needle import known_folder

    s = os.path.expandvars(raw.strip().strip("'\"“”"))
    s = os.path.expanduser(s)
    m = re.match(r"^(?:the\s+|my\s+)?(desktop|documents|downloads)(?:[\\/](.*))?$", s, re.I)
    if m:
        base = Path(known_folder(m.group(1).lower()))
        p = base / m.group(2) if m.group(2) else base
    else:
        p = Path(s)
        if not p.is_absolute():
            p = Path.home() / p
    return p.resolve()


def _inside(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def _forbidden(p: Path) -> Optional[str]:
    if _inside(p, servo_home()):
        return "Servo's own folder (~/.servo) holds its API token and keys and is off limits."
    return None


def _is_secret(p: Path) -> bool:
    low = str(p).lower()
    return p.suffix.lower() in _SECRET_EXTS or any(part in low for part in _SECRET_PARTS)


def _startup_dirs() -> Tuple[Path, ...]:
    appdata = os.environ.get("APPDATA", "")
    programdata = os.environ.get("PROGRAMDATA", "")
    tail = Path("Microsoft/Windows/Start Menu/Programs/Startup")
    return tuple(Path(b, tail).resolve() for b in (appdata, programdata) if b)


# ------------------------------------------------------------------- risk

def availability() -> Optional[str]:
    """None if system tools may run now, else the reason they may not."""
    if not settings.system_tools:
        return "System tools are turned off (SERVO_SYSTEM_TOOLS=false)."
    if settings.mode == "harness":
        return "System tools are disabled in harness mode: they would escape its window sandbox."
    return None


def describe(tool: str, args: Dict[str, Any]) -> str:
    if tool == "run_command":
        return f"run_command(shell={args.get('shell', 'powershell')!r}, command={args.get('command', '')!r})"
    if tool == "write_file":
        text = str(args.get("text", ""))
        return (f"write_file(path={args.get('path', '')!r}, append={bool(args.get('append'))}, "
                f"{len(text)} chars: {text[:120]!r}{'…' if len(text) > 120 else ''})")
    return f"{tool}(path={args.get('path', '')!r})"


def risk_floor(tool: str, args: Dict[str, Any]) -> Tuple[str, str]:
    """Deterministic minimum risk for a system tool call: (level, reason)."""
    from servo.control.jev_gate import _DESTRUCTIVE_COMMAND

    if tool == "run_command":
        cmd = str(args.get("command", ""))
        m = _DESTRUCTIVE_COMMAND.search(cmd)
        if m:
            return "high", f"Destructive or download-and-run command: '{m.group(0).strip()}'"
        return "medium", "Shell commands run with your account's full permissions"
    path = resolve_path(str(args.get("path", "")))
    if tool in ("read_file", "list_folder"):
        if _is_secret(path):
            return "high", f"{path} looks like a credential store; its contents would go to the planner model"
        return "low", "Read-only"
    if tool == "write_file":
        if path.suffix.lower() in _SCRIPT_EXTS:
            return "high", f"Writes an executable or script ({path.suffix})"
        if any(_inside(path, d) for d in _startup_dirs()):
            return "high", "Writes into the Startup folder (runs at every sign-in)"
        if path.exists() and not args.get("append"):
            return "high", f"Overwrites the existing file {path}"
        return "medium", "Writes a file"
    return "high", f"Unknown system tool '{tool}'"


# -------------------------------------------------------------- execution

def _clip(text: str, n: int = OUTPUT_CLIP) -> str:
    text = text.replace("\r\n", "\n")
    return text if len(text) <= n else text[:n] + f"\n… [{len(text) - n} more characters cut]"


def read_file(path_arg: str) -> ToolResult:
    p = resolve_path(path_arg)
    why = _forbidden(p)
    if why:
        raise SystemToolError(why)
    if not p.is_file():
        raise SystemToolError(f"No such file: {p}")
    size = p.stat().st_size
    with open(p, "rb") as f:
        data = f.read(MAX_READ_BYTES)
    if b"\x00" in data[:4096]:
        raise SystemToolError(f"{p} is a binary file ({size} bytes); only text files can be read.")
    text = data.decode("utf-8", errors="replace")
    note = f" (first {MAX_READ_BYTES} of {size} bytes)" if size > MAX_READ_BYTES else ""
    return ToolResult(output=_clip(text), summary=f"Read {p}{note}")


def list_folder(path_arg: str) -> ToolResult:
    p = resolve_path(path_arg)
    why = _forbidden(p)
    if why:
        raise SystemToolError(why)
    if not p.is_dir():
        raise SystemToolError(f"No such folder: {p}")
    rows = []
    for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower()))[:500]:
        try:
            if child.is_dir():
                rows.append(f"[dir]  {child.name}")
            else:
                st = child.stat()
                rows.append(f"{st.st_size:>10}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}  {child.name}")
        except OSError:
            rows.append(f"?  {child.name}")
    return ToolResult(output=_clip("\n".join(rows) or "(empty folder)"), summary=f"Listed {p} ({len(rows)} entries)")


def write_file(path_arg: str, text: str, append: bool = False) -> ToolResult:
    p = resolve_path(path_arg)
    why = _forbidden(p)
    if why:
        raise SystemToolError(why)
    if len(text) > MAX_WRITE_CHARS:
        raise SystemToolError(f"Refusing to write more than {MAX_WRITE_CHARS} characters at once.")
    if not p.parent.is_dir():
        raise SystemToolError(f"Folder does not exist: {p.parent}")
    with open(p, "a" if append else "w", encoding="utf-8", newline="") as f:
        f.write(text)
    verb = "Appended" if append else "Wrote"
    return ToolResult(output="", summary=f"{verb} {len(text)} characters to {p}")


def _shell_argv(shell: str, command: str) -> list:
    if shell == "cmd":
        return ["cmd.exe", "/d", "/s", "/c", command]
    exe = "powershell.exe" if sys.platform == "win32" else "pwsh"
    return [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]


def _kill_tree(proc: "subprocess.Popen[bytes]") -> None:
    """Kill the shell AND what it started: killing only the shell leaves its children
    running (and holding the output pipe open)."""
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_command(
    command: str,
    shell: str = "powershell",
    abort: Optional[threading.Event] = None,
    timeout: Optional[float] = None,
) -> ToolResult:
    """Run one command, capture its output, and kill it on timeout or abort."""
    if not command.strip():
        raise SystemToolError("Empty command.")
    if len(command) > MAX_COMMAND_CHARS:
        raise SystemToolError(f"Command is longer than {MAX_COMMAND_CHARS} characters; split it up.")
    if servo_home().as_posix().lower() in command.replace("\\", "/").lower() or re.search(r"[\\/]\.servo\b", command, re.I):
        raise SystemToolError("Commands may not touch Servo's own folder (~/.servo).")
    timeout = settings.command_timeout_seconds if timeout is None else timeout
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        _shell_argv(shell, command), cwd=str(Path.home()), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags,
    )
    chunks: list = []
    reader = threading.Thread(target=lambda: chunks.append(proc.stdout.read() if proc.stdout else b""), daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    killed = ""
    while proc.poll() is None:
        if abort is not None and abort.is_set():
            killed = "stopped by you"
        elif time.monotonic() > deadline:
            killed = f"timed out after {timeout:.0f} s"
        if killed:
            _kill_tree(proc)
            break
        time.sleep(0.05)
    proc.wait()
    reader.join(timeout=2)
    raw = b"".join(chunks)
    text = raw.decode("utf-8", errors="replace")
    if "�" in text and sys.platform == "win32":
        import locale
        text = raw.decode(locale.getpreferredencoding(False), errors="replace")
    out = _clip(text.strip()) or "(no output)"
    if killed:
        raise SystemToolError(f"Command {killed}. Output so far:\n{out}")
    if proc.returncode != 0:
        raise SystemToolError(f"Command exited with code {proc.returncode}:\n{out}")
    return ToolResult(output=out, summary=f"Ran {shell} command (exit 0)")


def execute(tool: str, args: Dict[str, Any], abort: Optional[threading.Event] = None) -> ToolResult:
    why = availability()
    if why:
        raise SystemToolError(why)
    if tool == "read_file":
        return read_file(str(args.get("path", "")))
    if tool == "list_folder":
        return list_folder(str(args.get("path", "")))
    if tool == "write_file":
        return write_file(str(args.get("path", "")), str(args.get("text", "")), bool(args.get("append")))
    if tool == "run_command":
        return run_command(str(args.get("command", "")), str(args.get("shell") or "powershell"), abort=abort)
    raise SystemToolError(f"Unknown system tool '{tool}'.")


# ---------------------------------------------------------------- grammar

SYSTEM_GRAMMAR = """\
read file <path>               e.g. read file desktop\\notes.txt
list folder <path>             e.g. list folder downloads
write "<text>" to file <path>  creates or replaces the file (also: append "<text>" to file <path>)
run command "<command>"        PowerShell, output comes back to you (also: run cmd command "<command>")"""

_Q = r'"(.*)"'  # greedy: the command/text runs to the LAST quote on the line
# Not "open file …": "open file explorer" launches Explorer.
_RE_READ = re.compile(r"^(?:read|show)\s+(?:the\s+)?(?:text\s+)?file\s+(.+)$", re.I)
_RE_LIST = re.compile(r"^(?:list|show)\s+(?:the\s+)?(?:folder|directory|files\s+in)\s+(.+)$", re.I)
_RE_WRITE = re.compile(rf"^(write|append)\s+{_Q}\s+(?:to|into)\s+(?:the\s+)?file\s+(.+)$", re.I | re.S)
_RE_RUN = re.compile(rf"^run\s+(?:(powershell|cmd)\s+)?command\s+{_Q}$", re.I | re.S)


def parse_system_intent(text: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Map one command in the system grammar to (tool, arguments), or None."""
    t = text.strip()
    m = _RE_RUN.match(t)
    if m:
        return "run_command", {"command": m.group(2), "shell": (m.group(1) or "powershell").lower()}
    m = _RE_WRITE.match(t)
    if m:
        return "write_file", {"text": m.group(2), "path": m.group(3).strip().rstrip("."),
                              "append": m.group(1).lower() == "append"}
    t = t.rstrip(".")
    m = _RE_READ.match(t)
    if m:
        return "read_file", {"path": m.group(1).strip()}
    m = _RE_LIST.match(t)
    if m:
        return "list_folder", {"path": m.group(1).strip()}
    return None
