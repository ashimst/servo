"""Direct file/shell tools: grammar, risk floors, guards, execution, and router wiring."""

import sys
import threading
import time
from pathlib import Path

import pytest

from servo.actuation import system_tools as st
from servo.config import settings


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point the home folder (and so ~/.servo) at a temp dir."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("SERVO_HOME", str(tmp_path / ".servo"))
    monkeypatch.setattr(settings, "mode", "assistant")
    monkeypatch.setattr(settings, "system_tools", True)
    (tmp_path / ".servo").mkdir()
    (tmp_path / ".servo" / "api_token").write_text("secret-token")
    return tmp_path


# ---------------------------------------------------------------- grammar

@pytest.mark.parametrize("text,tool,args", [
    ("read file desktop\\notes.txt", "read_file", {"path": "desktop\\notes.txt"}),
    ("list folder downloads", "list_folder", {"path": "downloads"}),
    ('write "a, b and then c" to file out.txt', "write_file", {"text": "a, b and then c", "path": "out.txt", "append": False}),
    ('append "x" to file log.txt', "write_file", {"text": "x", "path": "log.txt", "append": True}),
    ('run command "Get-Date"', "run_command", {"command": "Get-Date", "shell": "powershell"}),
    ('run cmd command "echo "hi""', "run_command", {"command": 'echo "hi"', "shell": "cmd"}),
])
def test_grammar(text, tool, args):
    assert st.parse_system_intent(text) == (tool, args)


def test_open_file_explorer_still_launches_explorer():
    from servo.cognition.needle import parse_grammar
    assert st.parse_system_intent("open file explorer") is None
    plan = parse_grammar("read file notes.txt")
    assert plan.steps[0].tool_name == "read_file" and plan.steps[0].canonical_action is None


def test_needle_never_sees_system_tools():
    from servo.cognition.needle import NEEDLE_TOOLS
    assert not {t["name"] for t in NEEDLE_TOOLS} & set(st.SYSTEM_TOOLS)


# ------------------------------------------------------------------- risk

def test_every_command_needs_confirmation(home):
    assert st.risk_floor("run_command", {"command": "Get-Date"})[0] == "medium"
    assert st.risk_floor("run_command", {"command": "Remove-Item -Recurse C:\\x"})[0] == "high"


def test_write_risks(home):
    (home / "exists.txt").write_text("old")
    assert st.risk_floor("write_file", {"path": "new.txt", "text": "x"})[0] == "medium"
    assert st.risk_floor("write_file", {"path": "exists.txt", "text": "x"})[0] == "high"
    assert st.risk_floor("write_file", {"path": "exists.txt", "text": "x", "append": True})[0] == "medium"
    assert st.risk_floor("write_file", {"path": "run.bat", "text": "x"})[0] == "high"


def test_read_risks(home):
    assert st.risk_floor("read_file", {"path": "notes.txt"})[0] == "low"
    assert st.risk_floor("read_file", {"path": ".ssh/id_rsa"})[0] == "high"


# ----------------------------------------------------------------- guards

def test_servo_folder_is_off_limits(home):
    for fn in (lambda: st.read_file(".servo/api_token"), lambda: st.list_folder(".servo"),
               lambda: st.write_file(".servo/x.txt", "x"), lambda: st.read_file("sub/../.servo/api_token")):
        with pytest.raises(st.SystemToolError, match="off limits"):
            fn()
    with pytest.raises(st.SystemToolError, match="Servo's own folder"):
        st.run_command(f"Get-Content {home}\\.servo\\api_token")


def test_disabled_in_harness_mode(home, monkeypatch):
    monkeypatch.setattr(settings, "mode", "harness")
    with pytest.raises(st.SystemToolError, match="harness"):
        st.execute("read_file", {"path": "x"})


def test_disabled_by_setting(home, monkeypatch):
    monkeypatch.setattr(settings, "system_tools", False)
    with pytest.raises(st.SystemToolError, match="turned off"):
        st.execute("list_folder", {"path": "."})


def test_command_length_cap(home):
    with pytest.raises(st.SystemToolError, match="longer than"):
        st.run_command("x" * (st.MAX_COMMAND_CHARS + 1))


# -------------------------------------------------------------- execution

def test_file_roundtrip(home):
    st.write_file("notes.txt", "hello\n")
    st.write_file("notes.txt", "world\n", append=True)
    assert st.read_file("notes.txt").output == "hello\nworld\n"
    listing = st.list_folder(".").output
    assert "notes.txt" in listing and "[dir]  .servo" in listing


def test_binary_file_refused(home):
    (home / "b.bin").write_bytes(b"\x00\x01\x02")
    with pytest.raises(st.SystemToolError, match="binary"):
        st.read_file("b.bin")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shells")
def test_run_command_output_and_exit_code(home):
    assert st.run_command("echo servo-ok", shell="cmd").output == "servo-ok"
    with pytest.raises(st.SystemToolError, match="exited with code 3"):
        st.run_command("exit 3", shell="cmd")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shells")
def test_run_command_abort_kills_it(home):
    abort = threading.Event()
    threading.Timer(0.5, abort.set).start()
    t0 = time.monotonic()
    with pytest.raises(st.SystemToolError, match="stopped by you"):
        st.run_command("ping -n 30 127.0.0.1 >nul", shell="cmd", abort=abort)
    assert time.monotonic() - t0 < 10


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shells")
def test_run_command_timeout(home):
    with pytest.raises(st.SystemToolError, match="timed out"):
        st.run_command("ping -n 30 127.0.0.1 >nul", shell="cmd", timeout=0.5)


# ---------------------------------------------------------- router wiring

class _Tracer:
    run_id = "t"
    def emit(self, *a, **k): return type("E", (), {"seq": 0})()
    def span(self, *a, **k):
        import contextlib
        @contextlib.contextmanager
        def cm():
            yield type("S", (), {"done": lambda *a, **k: None})()
        return cm()


def _router(confirm):
    from servo.cognition.router import CascadeRouter
    from servo.control.jev_gate import JevGate
    return CascadeRouter(tracer=_Tracer(), kev=JevGate(use_fallback=True), confirmation_callback=confirm,
                         browser=object(), memory=None)


def _step(text):
    from servo.cognition.needle import parse_grammar
    return parse_grammar(text).steps[0]


def test_router_runs_read_without_asking(home):
    (home / "n.txt").write_text("contents here")
    asked = []
    out = _router(lambda req: asked.append(req) or True)._execute(_step("read file n.txt"))
    assert out.success and out.output == "contents here" and not asked


def test_router_asks_before_command_and_respects_no(home):
    asked = []
    out = _router(lambda req: asked.append(req) or False)._execute(_step('run cmd command "echo hi"'))
    assert not out.success and out.kind == "policy"
    assert asked and asked[0].risk_level == "medium" and "echo hi" in asked[0].tool_call


def test_router_write_over_existing_is_high(home):
    (home / "e.txt").write_text("old")
    asked = []
    _router(lambda req: asked.append(req) or False)._execute(_step('write "new" to file e.txt'))
    assert asked[0].risk_level == "high"
    assert (home / "e.txt").read_text() == "old"


def test_output_reaches_system2_via_completed(home, monkeypatch):
    (home / "n.txt").write_text("line one")
    r = _router(lambda req: True)
    completed, records = [], []
    status, _ = r._run_batch([_step("read file n.txt")], completed, records, 1)
    assert status == "ok"
    assert "output:" in completed[0] and "line one" in completed[0]
    assert records[0].output == "line one"
