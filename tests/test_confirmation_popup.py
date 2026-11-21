"""Tests for High-Risk User Confirmation & Desktop Popup Dialog."""

import sys
from unittest.mock import MagicMock, patch
import pytest

from servo.control.confirmation import (
    ConfirmationRequest,
    DesktopDialogConfirmation,
    format_confirmation_message,
)
from servo.daemon.copilot_app import CopilotSession


def test_format_confirmation_message_high_risk():
    req = ConfirmationRequest(
        sub_goal="Delete system files",
        tool_call="remove_dir('C:\\Windows\\Temp')",
        element_label="Delete Button",
        risk_level="high",
        reason="Action is potentially destructive to files",
        hwnd=1234,
        window_title="File Explorer",
        process_name="explorer.exe",
    )
    msg = format_confirmation_message(req)
    assert "HIGH-risk" in msg
    assert "Delete system files" in msg
    assert "File Explorer" in msg
    assert "Allow this action?" in msg


def test_copilot_session_high_risk_confirm_asks_user():
    session = CopilotSession()
    req = ConfirmationRequest(
        sub_goal="Format Drive",
        tool_call="format('D:')",
        element_label="Format Button",
        risk_level="high",
        reason="Destructive disk operation",
        hwnd=5678,
    )

    # Mock ask to simulate user clicking "proceed"
    with patch.object(session, "ask", return_value="proceed") as mock_ask:
        with patch("servo.control.confirmation.DesktopDialogConfirmation.__call__", return_value=True):
            res = session._confirm(req)
            assert res is True
            assert mock_ask.called
            args, kwargs = mock_ask.call_args
            assert args[0] == "confirm"
            assert args[1]["risk_level"] == "high"


def test_copilot_session_high_risk_confirm_rejection():
    session = CopilotSession()
    req = ConfirmationRequest(
        sub_goal="Kill Critical Process",
        tool_call="kill(1)",
        element_label="End Process",
        risk_level="high",
        reason="System instability",
        hwnd=0,
    )

    # Mock ask to simulate user rejecting ("stop")
    with patch.object(session, "ask", return_value="stop"):
        with patch("servo.control.confirmation.DesktopDialogConfirmation.__call__", return_value=False):
            res = session._confirm(req)
            assert res is False
