"""Unit tests for Viewport coordinate transforms and DPI normalization."""

import pytest
from servo.perception.viewport import WindowBounds, ViewportTransform


def test_window_bounds_dimensions():
    bounds = WindowBounds(
        hwnd=1001,
        left=100,
        top=100,
        right=900,
        bottom=700,
        client_left=110,
        client_top=140,
        client_right=890,
        client_bottom=690,
    )
    assert bounds.width == 800
    assert bounds.height == 600
    assert bounds.client_width == 780
    assert bounds.client_height == 550


def test_coordinate_normalization():
    # Mock viewport bounds
    bounds = WindowBounds(
        hwnd=1001,
        left=0,
        top=0,
        right=1000,
        bottom=1000,
        client_left=0,
        client_top=0,
        client_right=1000,
        client_bottom=1000,
    )
    # Create transform with overridden bounds
    vt = ViewportTransform.__new__(ViewportTransform)
    vt.hwnd = 1001
    vt.bounds = bounds

    # Origin
    norm_x, norm_y = vt.screen_to_norm(0, 0)
    assert norm_x == 0
    assert norm_y == 0

    # Center
    norm_x, norm_y = vt.screen_to_norm(500, 500)
    assert norm_x == 500
    assert norm_y == 500

    # Bbox conversion
    norm_bbox = vt.bbox_screen_to_norm((100, 200, 300, 400))
    assert norm_bbox == (100, 200, 300, 400)
