"""Tests for the Alias Mapping Engine (Action and Element Resolution)."""

import pytest
from servo.actuation.alias_mapping import (
    AliasMapper,
    CanonicalAction,
    CanonicalActionType,
)
from servo.perception.scene_index import SceneElement, SceneIndex


def _make_scene(elements_data):
    elements = {}
    for idx, (label, role) in enumerate(elements_data):
        elements[idx] = SceneElement(
            element_id=idx,
            label=label,
            role=role,
            bbox_screen=(100, 100, 200, 200),
            bbox_norm=(100, 100, 200, 200),
            center_screen=(150, 150),
            is_keyboard_focusable=True,
            value="",
        )
    return SceneIndex(hwnd=1001, window_title="Test App", source="uia", elements=elements)


def test_canonicalize_action_click():
    for alias in ["click", "press", "tap", "hit", "select", "activate", "click_element"]:
        action = AliasMapper.canonicalize_action(alias, target="Submit")
        assert action.action_type == CanonicalActionType.CLICK
        assert action.target == "Submit"
        assert action.button == "left"


def test_canonicalize_action_double_and_right_click():
    action_dbl = AliasMapper.canonicalize_action("dblclick", target="Item 1")
    assert action_dbl.action_type == CanonicalActionType.DOUBLE_CLICK
    assert action_dbl.button == "double"

    action_rt = AliasMapper.canonicalize_action("context_menu", target="Folder")
    assert action_rt.action_type == CanonicalActionType.RIGHT_CLICK
    assert action_rt.button == "right"


def test_canonicalize_action_type():
    for alias in ["type", "write", "enter", "input", "fill", "set_text", "send_keys"]:
        action = AliasMapper.canonicalize_action(alias, target="editor", text="hello")
        assert action.action_type == CanonicalActionType.TYPE
        assert action.text == "hello"


def test_canonicalize_action_hotkey():
    action = AliasMapper.canonicalize_action("shortcut", keys=["ctrl", "s"])
    assert action.action_type == CanonicalActionType.HOTKEY
    assert action.keys == ["ctrl", "s"]

    # String keys split
    action_str = AliasMapper.canonicalize_action("hotkey", keys="ctrl+alt+del")
    assert action_str.keys == ["ctrl", "alt", "del"]


def test_canonicalize_action_lifecycle():
    action_open = AliasMapper.canonicalize_action("launch", app_name="calc")
    assert action_open.action_type == CanonicalActionType.LAUNCH
    assert action_open.app_name == "calc"

    action_close = AliasMapper.canonicalize_action("exit", app_name="calc")
    assert action_close.action_type == CanonicalActionType.CLOSE


def test_canonicalize_action_unknown_raises():
    with pytest.raises(ValueError, match="Unknown action alias"):
        AliasMapper.canonicalize_action("teleport_to_mars")


def test_resolve_element_exact_match():
    scene = _make_scene([("Save", "Button"), ("Save As...", "Button")])
    el, err = AliasMapper.resolve_element(scene, "Save")
    assert el is not None
    assert el.label == "Save"
    assert err is None


def test_resolve_element_punctuation_cleaning():
    scene = _make_scene([("Save As...", "Button"), ("Open", "Button")])
    el, err = AliasMapper.resolve_element(scene, "Save As")
    assert el is not None
    assert el.label == "Save As..."
    assert err is None


def test_resolve_element_role_fallback_for_editor():
    scene = _make_scene([("File", "MenuItem"), ("Edit", "MenuItem"), ("Text editor", "Document")])
    el, err = AliasMapper.resolve_element(scene, "editor", action_type=CanonicalActionType.TYPE)
    assert el is not None
    assert el.role == "Document"
    assert el.label == "Text editor"
    assert err is None


def test_resolve_element_ambiguous_returns_diagnostic():
    scene = _make_scene([("Help Topics", "Button"), ("Help Center", "Button")])
    el, err = AliasMapper.resolve_element(scene, "Help")
    assert el is None
    assert "Ambiguous target 'Help'" in err
