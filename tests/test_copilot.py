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
