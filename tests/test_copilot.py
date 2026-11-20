"""Unit tests for the Floating On-Screen Desktop Copilot companion."""

import pytest
from unittest.mock import patch, MagicMock

from servo.daemon.copilot import parse_compound_prompt, launch_or_focus_app


def test_parse_compound_prompt_open_and_write():
    result = parse_compound_prompt("open notepad and write hello world")
    assert result["app_to_open"] == "notepad"
    assert result["action"] == "type"
    assert result["text_to_type"] == "hello world"
    assert result["target_hint"] == "editor"


def test_parse_compound_prompt_open_and_click():
    result = parse_compound_prompt("open calculator and click 7")
    assert result["app_to_open"] == "calculator"
    assert result["action"] == "click"
    assert result["target_hint"] == "7"


def test_parse_compound_prompt_open_only():
    result = parse_compound_prompt("open paint")
    assert result["app_to_open"] == "paint"
    assert result["action"] == "focus"


def test_parse_compound_prompt_type_into_target():
    result = parse_compound_prompt("type urgent bug report into Search")
    assert result["action"] == "type"
    assert result["text_to_type"] == "urgent bug report"
    assert result["target_hint"] == "Search"


def test_parse_compound_prompt_click_only():
    result = parse_compound_prompt("click Submit Button")
    assert result["action"] == "click"
    assert result["target_hint"] == "Submit Button"


def test_parse_compound_prompt_fallback():
    result = parse_compound_prompt("Close dialog")
    assert result["action"] == "click"
    assert result["target_hint"] == "Close dialog"


@patch("servo.perception.windows.list_windows")
def test_launch_or_focus_app_existing_window(mock_list):
    mock_list.return_value = [
        {"hwnd": 12345, "title": "Untitled - Notepad", "process_name": "notepad.exe"}
    ]
    hwnd, title = launch_or_focus_app("notepad")
    assert hwnd == 12345
    assert "Notepad" in title
