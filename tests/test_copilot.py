"""Unit tests for the Conversational Floating On-Screen Desktop Copilot companion."""

import pytest
from unittest.mock import patch, MagicMock

from servo.daemon.copilot import CopilotBrain, launch_or_focus_app


def test_copilot_brain_greetings():
    brain = CopilotBrain(user_name="Ashim")
    reply, action = brain.process_message("hello")
    assert "Ashim" in reply
    assert action is None


def test_copilot_brain_identity_query():
    brain = CopilotBrain(user_name="Ashim")
    reply, action = brain.process_message("what is my name?")
    assert "Ashim" in reply
    assert action is None


def test_copilot_brain_name_learning():
    brain = CopilotBrain(user_name="Ashim")
    reply, action = brain.process_message("my name is Alex")
    assert "Alex" in reply
    assert brain.user_name == "Alex"
    assert action is None


def test_copilot_brain_general_chat():
    brain = CopilotBrain(user_name="Ashim")
    reply, action = brain.process_message("who are you?")
    assert "Servo" in reply
    assert action is None


def test_copilot_brain_write_my_name():
    brain = CopilotBrain(user_name="Ashim")
    reply, action = brain.process_message("write my name")
    assert action is not None
    assert action["type"] == "type"
    assert action["text"] == "Ashim"
    assert action["target"] == "editor"


def test_copilot_brain_open_and_write():
    brain = CopilotBrain(user_name="Ashim")
    reply, action = brain.process_message("open notepad and write hello world")
    assert action is not None
    assert action["type"] == "open_and_type"
    assert action["app"] == "notepad"
    assert action["text"] == "hello world"


def test_copilot_brain_open_and_click():
    brain = CopilotBrain(user_name="Ashim")
    reply, action = brain.process_message("open calculator and click 7")
    assert action is not None
    assert action["type"] == "open_and_click"
    assert action["app"] == "calculator"
    assert action["target"] == "7"


def test_copilot_brain_open_only():
    brain = CopilotBrain(user_name="Ashim")
    reply, action = brain.process_message("open paint")
    assert action is not None
    assert action["type"] == "open"
    assert action["app"] == "paint"


def test_copilot_brain_close_window():
    brain = CopilotBrain(user_name="Ashim")
    reply, action = brain.process_message("close notepad")
    assert action is not None
    assert action["type"] == "close"
    assert action["app"] == "notepad"


@patch("servo.perception.windows.list_windows")
def test_copilot_brain_list_windows(mock_list):
    mock_list.return_value = [
        {"hwnd": 12345, "title": "Untitled - Notepad", "process_name": "notepad.exe"}
    ]
    brain = CopilotBrain(user_name="Ashim")
    reply, action = brain.process_message("what windows are open?")
    assert action is None
    assert "Notepad" in reply


@patch("servo.perception.windows.list_windows")
def test_launch_or_focus_app_existing_window(mock_list):
    mock_list.return_value = [
        {"hwnd": 12345, "title": "Untitled - Notepad", "process_name": "notepad.exe"}
    ]
    hwnd, title = launch_or_focus_app("notepad")
    assert hwnd == 12345
    assert "Notepad" in title
