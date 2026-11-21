"""Unit tests for the Conversational Floating On-Screen Desktop Copilot companion with System 1/2 handoff."""

import pytest
from unittest.mock import patch, MagicMock

from servo.daemon.copilot import CopilotBrain, launch_or_focus_app
from servo.actuation.alias_mapping import CanonicalActionType


def test_copilot_brain_greetings():
    brain = CopilotBrain(user_name="Ashim")
    reply, plan = brain.process_message("hello")
    assert "Ashim" in reply
    assert plan is None


def test_copilot_brain_identity_query():
    brain = CopilotBrain(user_name="Ashim")
    reply, plan = brain.process_message("what is my name?")
    assert "Ashim" in reply
    assert plan is None


def test_copilot_brain_name_learning():
    brain = CopilotBrain(user_name="Ashim")
    reply, plan = brain.process_message("my name is Alex")
    assert "Alex" in reply
    assert brain.user_name == "Alex"
    assert plan is None


def test_copilot_brain_general_chat():
    brain = CopilotBrain(user_name="Ashim")
    reply, plan = brain.process_message("who are you?")
    assert "Servo" in reply
    assert plan is None


def test_copilot_brain_plan_open_and_write():
    brain = CopilotBrain(user_name="Ashim")
    reply, plan = brain.process_message("open notepad and write hello world")
    assert plan is not None
    assert len(plan.steps) == 2
    assert plan.steps[0].canonical_action.action_type == CanonicalActionType.LAUNCH
    assert plan.steps[0].canonical_action.app_name == "notepad"
    assert plan.steps[1].canonical_action.action_type == CanonicalActionType.TYPE
    assert plan.steps[1].canonical_action.text == "hello world"


def test_copilot_brain_plan_open_and_click():
    brain = CopilotBrain(user_name="Ashim")
    reply, plan = brain.process_message("open calculator and click 7")
    assert plan is not None
    assert len(plan.steps) == 2
    assert plan.steps[0].canonical_action.action_type == CanonicalActionType.LAUNCH
    assert plan.steps[0].canonical_action.app_name == "calculator"
    assert plan.steps[1].canonical_action.action_type == CanonicalActionType.CLICK
    assert plan.steps[1].canonical_action.target == "7"


def test_copilot_brain_plan_write_my_name():
    brain = CopilotBrain(user_name="Ashim")
    reply, plan = brain.process_message("write my name")
    assert plan is not None
    assert len(plan.steps) == 1
    assert plan.steps[0].canonical_action.action_type == CanonicalActionType.TYPE
    assert plan.steps[0].canonical_action.text == "Ashim"


def test_copilot_brain_plan_close():
    brain = CopilotBrain(user_name="Ashim")
    reply, plan = brain.process_message("close notepad")
    assert plan is not None
    assert len(plan.steps) == 1
    assert plan.steps[0].canonical_action.action_type == CanonicalActionType.CLOSE


@patch("servo.perception.windows.list_windows")
def test_copilot_brain_list_windows(mock_list):
    mock_list.return_value = [
        {"hwnd": 12345, "title": "Untitled - Notepad", "process_name": "notepad.exe"}
    ]
    brain = CopilotBrain(user_name="Ashim")
    reply, plan = brain.process_message("what windows are open?")
    assert plan is None
    assert "Notepad" in reply


@patch("servo.perception.windows.list_windows")
def test_launch_or_focus_app_existing_window(mock_list):
    mock_list.return_value = [
        {"hwnd": 12345, "title": "Untitled - Notepad", "process_name": "notepad.exe"}
    ]
    hwnd, title = launch_or_focus_app("notepad")
    assert hwnd == 12345
    assert "Notepad" in title


def test_resolve_app_executable():
    from servo.daemon.copilot import resolve_app_executable
    chrome_path = resolve_app_executable("chrome")
    assert "chrome" in chrome_path.lower()
    notepad_path = resolve_app_executable("notepad")
    assert "notepad" in notepad_path.lower()


def test_copilot_brain_dictionary_subscripting_compatibility():
    """Verify legacy dictionary subscripting syntax works on MacroPlan without TypeError."""
    brain = CopilotBrain(user_name="Ashim")
    _, plan = brain.process_message("open notepad and write hello world")
    assert plan is not None
    # Subscripting on MacroPlan
    assert plan["type"] == "launch"
    assert plan["app"] == "notepad"
    assert len(plan["steps"]) == 2
    assert len(plan["actions"]) == 2
    # Subscripting on PlanStep
    assert plan.steps[0]["type"] == "launch"
    assert plan.steps[0]["app"] == "notepad"
    assert plan.steps[1]["type"] == "type"
    assert plan.steps[1]["text"] == "hello world"
    assert plan.steps[1]["target"] == "editor"


def test_copilot_brain_dictionary_subscripting_write_and_close():
    brain = CopilotBrain(user_name="Ashim")
    _, plan_write = brain.process_message("write my name")
    assert plan_write["type"] == "type"
    assert plan_write["text"] == "Ashim"

    _, plan_close = brain.process_message("close notepad")
    assert plan_close["type"] == "close"
    assert plan_close["app"] == "notepad"


@pytest.fixture
def headless_copilot_app(monkeypatch):
    """Hermetic headless fixture for DesktopCopilotApp testing without opening Tkinter windows."""
    from servo.daemon.worker import worker

    # Neutralize worker thread lifecycle to prevent thread leakage
    monkeypatch.setattr(worker, "start", lambda: None)
    monkeypatch.setattr(worker, "stop", lambda: None)

    # Build mock Tkinter subsystem
    mock_tk = MagicMock()
    mock_root = MagicMock()
    mock_root.winfo_screenwidth.return_value = 1920
    mock_root.winfo_screenheight.return_value = 1080

    # Execute root.after(delay, fn, *args) synchronously on the spot
    mock_root.after.side_effect = lambda delay, fn, *args: fn(*args)
    mock_tk.Tk.return_value = mock_root
    mock_tk.FLAT = 0
    mock_tk.X = "x"
    mock_tk.Y = "y"
    mock_tk.TOP = "top"
    mock_tk.BOTTOM = "bottom"
    mock_tk.LEFT = "left"
    mock_tk.RIGHT = "right"
    mock_tk.BOTH = "both"
    mock_tk.DISABLED = "disabled"
    mock_tk.NORMAL = "normal"

    with patch.dict("sys.modules", {"tkinter": mock_tk, "tkinter.ttk": mock_tk}):
        from servo.daemon.copilot import DesktopCopilotApp
        app = DesktopCopilotApp()
        yield app


# =============================================================================
# Feature 15: Centralized Planning via get_planner
# =============================================================================

def test_copilot_brain_delegates_to_configured_planner(monkeypatch):
    from servo.config import settings
    from servo.planner.tool_calling import MacroPlan, PlanStep
    from servo.actuation.alias_mapping import AliasMapper

    monkeypatch.setattr(settings, "planner_provider", "openai")

    expected_plan = MacroPlan(
        goal="open notepad",
        steps=[
            PlanStep(
                step_index=1,
                tool_name="launch_app",
                arguments={"app_name": "notepad"},
                canonical_action=AliasMapper.canonicalize_action("launch_app", app_name="notepad"),
            )
        ],
    )

    mock_planner = MagicMock()
    mock_planner.decompose_macro.return_value = expected_plan

    with patch("servo.planner.get_planner", return_value=mock_planner) as mock_gp:
        brain = CopilotBrain()
        plan = brain.query_llm_for_plan("open notepad")

        mock_gp.assert_called_with(None)
        mock_planner.decompose_macro.assert_called_once()
        assert plan == expected_plan


def test_copilot_brain_provider_synonym_routing():
    from servo.planner.tool_calling import MacroPlan, PlanStep

    brain = CopilotBrain(planner_provider="ollama")
    mock_planner = MagicMock()
    mock_planner.decompose_macro.return_value = MacroPlan(goal="test", steps=[PlanStep(step_index=1, tool_name="wait")])
    with patch("servo.planner.get_planner", return_value=mock_planner) as mock_gp:
        brain.query_llm_for_plan("wait a second")
        mock_gp.assert_called_with("ollama")


def test_copilot_brain_fallback_on_planner_error():
    from servo.planner.base import PlannerError

    mock_planner = MagicMock()
    mock_planner.decompose_macro.side_effect = PlannerError("Rate limit / API error")

    with patch("servo.planner.get_planner", return_value=mock_planner):
        brain = CopilotBrain(user_name="Ashim")
        reply, plan = brain.process_message("open notepad and write hello world")

        assert plan is not None
        assert len(plan.steps) == 2
        assert plan.steps[0].canonical_action.action_type == CanonicalActionType.LAUNCH
        assert plan.steps[1].canonical_action.text == "hello world"


def test_copilot_brain_forwards_scene_context():
    from servo.perception.scene_index import SceneIndex

    mock_scene = SceneIndex(hwnd=12345, window_title="Test App", source="uia", elements={})
    mock_planner = MagicMock()
    mock_planner.decompose_macro.return_value = None

    with patch("servo.planner.get_planner", return_value=mock_planner):
        brain = CopilotBrain()
        brain.query_llm_for_plan("click Save", scene=mock_scene)

        assert mock_planner.decompose_macro.call_args[1]["initial_scene"] == mock_scene


def test_copilot_brain_conversational_bypasses_planner():
    with patch("servo.planner.get_planner") as mock_gp:
        brain = CopilotBrain(user_name="Ashim")
        reply, plan = brain.process_message("hello, how are you?")

        assert plan is None
        assert "Ashim" in reply
        mock_gp.assert_not_called()


# =============================================================================
# Feature 16: User Cancellation & Interruption
# =============================================================================

def test_copilot_app_cancellation_token_lifecycle(headless_copilot_app):
    app = headless_copilot_app

    assert hasattr(app, "_abort_requested")
    assert not app._abort_requested.is_set()

    app.stop()
    assert app._abort_requested.is_set()
    assert app.status_pill.config.called


def test_copilot_app_stop_updates_status_pill(headless_copilot_app):
    app = headless_copilot_app
    app.stop()

    app.status_pill.config.assert_called_with(text="● Cancelled", fg="#ef4444")


def test_copilot_app_mid_plan_cancellation(headless_copilot_app):
    app = headless_copilot_app
    from servo.planner.tool_calling import MacroPlan, PlanStep
    from servo.control.handoff import PlanExecutionSummary, StepExecutionReport

    plan = MacroPlan(
        goal="cancellable plan",
        steps=[
            PlanStep(step_index=1, tool_name="click_element", arguments={"target": "Step 1"}),
            PlanStep(step_index=2, tool_name="click_element", arguments={"target": "Step 2"}),
        ],
    )

    executed = []
    def fake_execute_plan(plan_arg, **kwargs):
        executed.append(1)
        app.stop()
        return PlanExecutionSummary(
            goal=plan_arg.goal,
            success=False,
            total_steps=2,
            completed_steps=1,
            total_latency_ms=100,
            step_reports=[
                StepExecutionReport(step_index=1, action_type="click_element", description="Step 1 done", success=True, latency_ms=50),
                StepExecutionReport(step_index=2, action_type="click_element", description="Aborted", success=False, latency_ms=0, error="Cancelled"),
            ],
            aborted=True,
        )

    with patch("servo.control.handoff.SystemHandoffCoordinator.execute_plan", side_effect=fake_execute_plan):
        app._execute_handoff_plan(plan)

    assert executed == [1]
    assert app._abort_requested.is_set()


def test_copilot_app_clears_abort_token_on_new_prompt(headless_copilot_app):
    app = headless_copilot_app
    app.stop()
    assert app._abort_requested.is_set()

    with patch("threading.Thread") as mock_thread:
        app._send_prompt("open notepad")
        assert not app._abort_requested.is_set()
        assert app._is_executing is True


# =============================================================================
# Feature 18: UI Status Streaming & Thread Safety
# =============================================================================

def test_copilot_ui_status_pill_transitions(headless_copilot_app):
    app = headless_copilot_app
    app._set_status("● Planning...", "#f59e0b")
    app.status_pill.config.assert_called_with(text="● Planning...", fg="#f59e0b")

    app._set_status("● Running (1/2)...", "#38bdf8")
    app.status_pill.config.assert_called_with(text="● Running (1/2)...", fg="#38bdf8")

    app._set_status("● Completed", "#10b981")
    app.status_pill.config.assert_called_with(text="● Completed", fg="#10b981")


def test_copilot_ui_step_progress_rendering(headless_copilot_app):
    app = headless_copilot_app
    from servo.planner.tool_calling import PlanStep
    from servo.control.handoff import StepExecutionReport

    step = PlanStep(step_index=1, tool_name="launch_app", arguments={"app_name": "notepad"})
    report = StepExecutionReport(
        step_index=1,
        action_type="launch_app",
        description="Launched notepad",
        success=True,
        latency_ms=120,
        perception_source="uia",
    )

    with patch.object(app, "_add_message") as mock_add:
        source_tag = f" [{report.perception_source.upper()}]"
        msg = f"  ✅ Step {step.step_index}: {report.description} ({report.latency_ms}ms){source_tag}"
        app._add_message("assistant", msg)
        mock_add.assert_called_with("assistant", "  ✅ Step 1: Launched notepad (120ms) [UIA]")


def test_copilot_ui_thread_safe_scheduling(headless_copilot_app):
    import threading

    app = headless_copilot_app
    app.root.after.reset_mock()

    def worker_target():
        app._set_status("● Step 1...", "#38bdf8")
        app._add_message("assistant", "Hello off-thread")

    t = threading.Thread(target=worker_target)
    t.start()
    t.join()

    assert app.root.after.call_count >= 2


# =============================================================================
# Feature 17: Telemetry Integration in Copilot
# =============================================================================

def test_copilot_plan_execution_persists_trace(headless_copilot_app, tmp_path, monkeypatch):
    import gc
    from servo.config import settings
    from servo.daemon import tasks
    from servo.planner.tool_calling import MacroPlan, PlanStep
    from servo.control.handoff import PlanExecutionSummary, StepExecutionReport

    monkeypatch.setattr(settings, "trace_dir", tmp_path)
    monkeypatch.setattr(tasks, "_trace_store", None)

    app = headless_copilot_app
    plan = MacroPlan(
        goal="open calculator",
        steps=[PlanStep(step_index=1, tool_name="launch_app", arguments={"app_name": "calc"})],
    )

    summary = PlanExecutionSummary(
        goal=plan.goal,
        success=True,
        total_steps=1,
        completed_steps=1,
        total_latency_ms=85,
        step_reports=[
            StepExecutionReport(
                step_index=1,
                action_type="launch_app",
                description="Launched calc",
                success=True,
                latency_ms=85,
                perception_source="uia",
                jev_risk="low",
            )
        ],
    )

    with patch("servo.control.handoff.SystemHandoffCoordinator.execute_plan", return_value=summary):
        app._execute_handoff_plan(plan)

    store = tasks.get_trace_store()
    runs = store.list_runs()
    assert len(runs) >= 1
    assert runs[0]["goal"] == "open calculator"
    assert runs[0]["status"] == "passed"
    assert runs[0]["total_latency_ms"] == 85
    gc.collect()


def test_copilot_cancelled_plan_persists_cancelled_status(headless_copilot_app, tmp_path, monkeypatch):
    import gc
    from servo.config import settings
    from servo.daemon import tasks
    from servo.planner.tool_calling import MacroPlan, PlanStep
    from servo.control.handoff import PlanExecutionSummary, StepExecutionReport

    monkeypatch.setattr(settings, "trace_dir", tmp_path)
    monkeypatch.setattr(tasks, "_trace_store", None)

    app = headless_copilot_app
    plan = MacroPlan(
        goal="cancelled task",
        steps=[PlanStep(step_index=1, tool_name="click_element", arguments={"target": "Save"})],
    )

    summary = PlanExecutionSummary(
        goal=plan.goal,
        success=False,
        total_steps=1,
        completed_steps=0,
        total_latency_ms=20,
        step_reports=[
            StepExecutionReport(
                step_index=1,
                action_type="click_element",
                description="Aborted",
                success=False,
                latency_ms=20,
                error="Cancelled by user",
            )
        ],
        aborted=True,
    )

    with patch("servo.control.handoff.SystemHandoffCoordinator.execute_plan", return_value=summary):
        app._execute_handoff_plan(plan)

    store = tasks.get_trace_store()
    runs = store.list_runs()
    assert len(runs) >= 1
    assert runs[0]["status"] == "cancelled"
    gc.collect()


# =============================================================================
# Milestone 4 Iteration 2: Verifier Feedback Fixes & Robustness Tests
# =============================================================================

def test_copilot_brain_legacy_decompose_planner_fallback_without_scene():
    """Verify that CopilotBrain falling back to a legacy planner with decompose()
    and scene=None creates a valid SceneIndex(..., source='uia') and does not
    raise Pydantic ValidationError."""
    from unittest.mock import MagicMock, patch
    from servo.daemon.copilot import CopilotBrain
    from servo.planner.base import SubGoal

    mock_planner = MagicMock(spec=["decompose"])
    mock_planner.decompose.return_value = [
        SubGoal(
            step_index=1,
            instruction="click OK button",
            action_type="click",
            target_element_hint="OK",
        )
    ]
    brain = CopilotBrain()
    with patch("servo.planner.get_planner", return_value=mock_planner):
        plan = brain.query_llm_for_plan("click OK button", scene=None)
        assert plan is not None
        assert len(plan.steps) == 1
        assert plan.steps[0].arguments["target"] == "OK"

        # Verify decompose was called with default SceneIndex having source="uia"
        args, kwargs = mock_planner.decompose.call_args
        assert args[0] == "click OK button"
        scene_arg = args[1]
        assert scene_arg.hwnd == 0
        assert scene_arg.window_title == ""
        assert scene_arg.source == "uia"


def test_copilot_brain_legacy_planner_type_action_resolution():
    """Verify that a legacy planner yielding action_type='type' produces a PlanStep with
    tool_name='type_text' and arguments containing text and target, resolving SubGoal default tool_name."""
    brain = CopilotBrain(planner_provider="passthrough")
    plan = brain.query_llm_for_plan('type "hello" into editor')
    assert plan is not None
    assert len(plan.steps) == 1
    assert plan.steps[0].tool_name == "type_text"
    assert plan.steps[0].arguments["text"] == "hello"
    assert plan.steps[0].arguments["target"] == "editor"
    assert plan.steps[0].canonical_action is not None
    assert plan.steps[0].canonical_action.action_type == CanonicalActionType.TYPE
    assert plan.steps[0].canonical_action.text == "hello"


def test_copilot_brain_legacy_planner_subgoal_default_tool_name_override():
    """Verify that SubGoal instances with default tool_name='click_element' but action_type='type'
    are properly mapped to 'type_text' rather than keeping click_element."""
    from unittest.mock import MagicMock, patch
    from servo.planner.base import SubGoal

    mock_planner = MagicMock(spec=["decompose"])
    mock_planner.decompose.return_value = [
        SubGoal(
            step_index=1,
            instruction="type search query",
            action_type="type",
            target_element_hint="search_bar",
            text_to_type="python tutorials",
            # Note: tool_name defaults to 'click_element' in SubGoal!
        )
    ]
    brain = CopilotBrain()
    with patch("servo.planner.get_planner", return_value=mock_planner):
        plan = brain.query_llm_for_plan("search python tutorials")
        assert plan is not None
        assert len(plan.steps) == 1
        assert plan.steps[0].tool_name == "type_text"
        assert plan.steps[0].arguments["text"] == "python tutorials"
        assert plan.steps[0].arguments["target"] == "search_bar"
        assert plan.steps[0].canonical_action is not None
        assert plan.steps[0].canonical_action.action_type == CanonicalActionType.TYPE
        assert plan.steps[0].canonical_action.text == "python tutorials"


def test_copilot_app_on_stop_concurrency_barrier(headless_copilot_app):
    """Verify that concurrent invocations of _on_stop via threading.Barrier emit exactly
    one cancellation message under synchronized multi-threaded race conditions."""
    import threading

    app = headless_copilot_app
    messages = []
    app._add_message = lambda role, msg: messages.append((role, msg))

    thread_count = 20
    barrier = threading.Barrier(thread_count)

    def worker():
        barrier.wait()
        app._on_stop()

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    cancellation_msgs = [m for m in messages if "Cancellation requested by user" in m[1]]
    assert len(cancellation_msgs) == 1


def test_copilot_app_closed_window_thread_safety_real_tk():
    """Verify that closing the copilot app (app._on_close()) while a background thread
    dispatches status/message updates does not raise unhandled RuntimeError/TclError."""
    import threading
    from servo.daemon.copilot import DesktopCopilotApp

    try:
        app = DesktopCopilotApp()
    except Exception:
        pytest.skip("Tkinter cannot initialize a display in this environment")

    try:
        app.root.withdraw()
        app._on_close()

        errors = []

        def dispatch_updates():
            try:
                app._set_status("● Planning...", "#f59e0b")
                app._set_status_if_idle("● Ready", "#34d399")
                app._update_target_banner("Test App", 12345)
                app._add_message("assistant", "Testing post-close message")
                app._update_input_controls(is_executing=False)
                app._on_stop()
            except Exception as e:
                errors.append(e)

        t = threading.Thread(target=dispatch_updates)
        t.start()
        t.join()

        assert not errors, f"UI dispatch methods raised unhandled errors on closed window: {errors}"
    finally:
        try:
            app.root.destroy()
        except Exception:
            pass


def test_copilot_app_closed_window_thread_safety_mocked(headless_copilot_app):
    """Verify that if root.after raises RuntimeError (main thread not in main loop)
    or widget config raises TclError/Exception, UI dispatch methods catch them cleanly."""
    import threading

    app = headless_copilot_app

    # Simulate destroyed Tkinter interpreter behavior
    app.root.after.side_effect = RuntimeError("main thread is not in main loop")
    app.status_pill.config.side_effect = Exception("application has been destroyed")
    app.target_banner.config.side_effect = Exception("application has been destroyed")
    app.send_btn.config.side_effect = Exception("application has been destroyed")
    app.stop_btn.config.side_effect = Exception("application has been destroyed")

    errors = []

    def dispatch():
        try:
            app._set_status("● Running...", "#38bdf8")
            app._set_status_if_idle("● Ready", "#34d399")
            app._update_target_banner("Target", 9999)
            app._add_message("assistant", "Step 1 done")
            app._update_input_controls(is_executing=False)
            app._update_input_controls(is_executing=True)
            app._on_stop()
        except Exception as e:
            errors.append(e)

    # Test from background thread
    t = threading.Thread(target=dispatch)
    t.start()
    t.join()

    # Test from main thread
    dispatch()

    assert not errors, f"Encountered unhandled exceptions when root is destroyed: {errors}"


def test_update_input_controls_when_tk_is_none(headless_copilot_app):
    """Verify _update_input_controls executes cleanly without AttributeError when tk is None."""
    from servo.daemon import copilot

    app = headless_copilot_app
    with patch.object(copilot, "tk", None):
        # Must execute without AttributeError: 'NoneType' object has no attribute 'DISABLED'
        app._update_input_controls(is_executing=True)
        app._update_input_controls(is_executing=False)



