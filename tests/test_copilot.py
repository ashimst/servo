"""The copilot's app launching and its (model-free) brain state.

Conversation and planning are the cascade router's (Laya -> Needle / System 2); the
copilot brain no longer answers or plans anything itself.
"""

from unittest.mock import patch

import pytest

from servo.daemon import copilot
from servo.daemon.copilot import CopilotBrain, launch_or_focus_app, resolve_default_user_name


def test_copilot_brain_holds_state_only():
    brain = CopilotBrain(user_name="Alex", planner_provider="anthropic")
    assert brain.user_name == "Alex" and brain.planner_provider == "anthropic"
    assert brain.history == [] and brain.active_hwnd is None and brain.active_title == ""
    for gone in ("process_message", "query_llm_for_plan", "query_llm_for_chat", "conversational_reply",
                 "offline_chat_reply", "_parse_deterministic_plan"):
        assert not hasattr(brain, gone), gone


@pytest.mark.parametrize("env,expected", [("alex", "alex"), ("Administrator", "User"), ("user", "User")])
def test_default_user_name_is_the_os_user(monkeypatch, env, expected):
    monkeypatch.setenv("USERNAME", env)
    assert resolve_default_user_name() == expected
    assert CopilotBrain().user_name == expected


def test_default_user_name_without_env(monkeypatch):
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.setattr(copilot.getpass, "getuser", lambda: "default")
    assert resolve_default_user_name() == "User"


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
