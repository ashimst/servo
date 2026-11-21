"""Tests for the Alias Mapping Engine (Action and Element Resolution)."""

import pytest
from unittest.mock import MagicMock
from servo.actuation.alias_mapping import (
    AliasMapper,
    CanonicalAction,
    CanonicalActionType,
)
from servo.actuation.needle_tools import ActuationError, NeedleToolSurface
from servo.actuation.sandboxing import is_hotkey_blacklisted
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


def test_resolve_element_role_fallback_multiple_disambiguates_by_area():
    # Simulate a window with a small search box Edit and a large main text Document
    small_search = SceneElement(
        element_id=0,
        label="Find",
        role="Edit",
        bbox_screen=(100, 100, 300, 130),  # area = 200 * 30 = 6000
        bbox_norm=(100, 100, 300, 130),
        center_screen=(200, 115),
        is_keyboard_focusable=True,
    )
    large_doc = SceneElement(
        element_id=1,
        label="",
        role="Document",
        bbox_screen=(100, 150, 900, 800),  # area = 800 * 650 = 520000
        bbox_norm=(100, 150, 900, 800),
        center_screen=(500, 475),
        is_keyboard_focusable=True,
    )
    scene = SceneIndex(hwnd=1001, window_title="Test App", source="uia", elements={0: small_search, 1: large_doc})
    el, err = AliasMapper.resolve_element(scene, "editor", action_type=CanonicalActionType.TYPE)
    assert el is not None
    assert el.element_id == 1
    assert el.role == "Document"
    assert err is None


def test_resolve_element_ambiguous_returns_diagnostic():
    scene = _make_scene([("Help Topics", "Button"), ("Help Center", "Button")])
    el, err = AliasMapper.resolve_element(scene, "Help")
    assert el is None
    assert "Ambiguous target 'Help'" in err


def test_canonicalize_action_invoke():
    for alias in [
        "invoke",
        "uia_invoke",
        "element_invoke",
        "pattern_invoke",
        "trigger",
        "invoke_element",
        "activate_element",
        "pattern_activate",
        "uia_activate",
    ]:
        action = AliasMapper.canonicalize_action(alias, target="OK")
        assert action.action_type == CanonicalActionType.INVOKE
        assert action.target == "OK"

    # Support "activate" routing to INVOKE when prefer_invoke=True or uia=True
    action_pref = AliasMapper.canonicalize_action("activate", target="OK", prefer_invoke=True)
    assert action_pref.action_type == CanonicalActionType.INVOKE

    action_uia = AliasMapper.canonicalize_action("activate", target="OK", uia=True)
    assert action_uia.action_type == CanonicalActionType.INVOKE

    # Default "activate" routes to CLICK for backward compatibility
    action_click = AliasMapper.canonicalize_action("activate", target="OK")
    assert action_click.action_type == CanonicalActionType.CLICK


def test_canonicalize_action_parameter_synonyms():
    # text synonyms: value, content, input -> text
    for key in ["value", "content", "input"]:
        kwargs = {key: "test_text", "target": "box"}
        action = AliasMapper.canonicalize_action("type", **kwargs)
        assert action.action_type == CanonicalActionType.TYPE
        assert action.text == "test_text"

    # target synonyms: element, control, label, selector -> target
    for key in ["element", "control", "label", "selector"]:
        kwargs = {key: "SubmitBtn"}
        action = AliasMapper.canonicalize_action("click", **kwargs)
        assert action.action_type == CanonicalActionType.CLICK
        assert action.target == "SubmitBtn"

    # target synonym for invoke
    for key in ["element", "control", "label"]:
        kwargs = {key: "MenuOption"}
        action = AliasMapper.canonicalize_action("invoke", **kwargs)
        assert action.action_type == CanonicalActionType.INVOKE
        assert action.target == "MenuOption"

    # app_name synonyms: app, application, process -> app_name
    for key in ["app", "application", "process"]:
        kwargs = {key: "notepad"}
        action = AliasMapper.canonicalize_action("launch", **kwargs)
        assert action.action_type == CanonicalActionType.LAUNCH
        assert action.app_name == "notepad"


def test_canonicalize_action_hotkey_normalization():
    # Individual key name normalization via sandboxing.normalize_keys
    action = AliasMapper.canonicalize_action("hotkey", keys=["control", "return"])
    assert action.action_type == CanonicalActionType.HOTKEY
    assert action.keys == ["ctrl", "enter"]

    # String combo normalization
    action_str = AliasMapper.canonicalize_action("shortcut", keys="ctrl+alt+del")
    assert action_str.keys == ["ctrl", "alt", "del"]

    # More key aliases (option -> alt, windows -> win, escape -> esc, delete -> del)
    action_multi = AliasMapper.canonicalize_action("send_hotkey", keys=["Option", "Escape", "Delete"])
    assert action_multi.keys == ["alt", "esc", "del"]


def test_canonicalize_action_missing_required_args():
    # TYPE requires non-empty text
    with pytest.raises(ValueError, match="Action 'type' requires a non-empty 'text' parameter"):
        AliasMapper.canonicalize_action("type", target="input_box", text="")

    with pytest.raises(ValueError, match="Action 'type' requires a non-empty 'text' parameter"):
        AliasMapper.canonicalize_action("type_text", target="input_box")

    # HOTKEY requires non-empty keys
    with pytest.raises(ValueError, match="Action 'hotkey' requires a non-empty 'keys' parameter"):
        AliasMapper.canonicalize_action("hotkey", keys=[])

    with pytest.raises(ValueError, match="Action 'hotkey' requires a non-empty 'keys' parameter"):
        AliasMapper.canonicalize_action("send_hotkey")

    # CLICK requires non-empty target
    with pytest.raises(ValueError, match="Action 'click' requires a non-empty 'target' parameter"):
        AliasMapper.canonicalize_action("click")

    # INVOKE requires non-empty target
    with pytest.raises(ValueError, match="Action 'invoke' requires a non-empty 'target' parameter"):
        AliasMapper.canonicalize_action("invoke")

    # LAUNCH requires app_name
    with pytest.raises(ValueError, match="Action 'launch' requires an 'app_name' parameter"):
        AliasMapper.canonicalize_action("launch")

    # Unknown alias provides available types
    with pytest.raises(ValueError, match="Unknown action alias 'nonexistent_tool'"):
        AliasMapper.canonicalize_action("nonexistent_tool")


def test_map_to_needle_tool_dispatch():
    scene = _make_scene([("Button1", "Button")])
    tools = MagicMock(spec=NeedleToolSurface)
    tools.click.return_value = "clicked"
    tools.invoke.return_value = "invoked"
    tools.type_text.return_value = "typed"
    tools.hotkey.return_value = "hotkeyed"
    tools.wait.return_value = "waited"

    # Click
    act_click = AliasMapper.canonicalize_action("click", target="Button1")
    fn, sig = AliasMapper.map_to_needle_tool(act_click, tools, scene, element_id=0)
    assert sig == "click(0, button='left')"
    assert fn() == "clicked"
    tools.click.assert_called_with(scene, 0, button="left")

    # Double click
    act_dbl = AliasMapper.canonicalize_action("double_click", target="Button1")
    fn, sig = AliasMapper.map_to_needle_tool(act_dbl, tools, scene, element_id=0)
    assert sig == "click(0, button='double')"
    assert fn() == "clicked"
    tools.click.assert_called_with(scene, 0, button="double")

    # Right click
    act_rt = AliasMapper.canonicalize_action("right_click", target="Button1")
    fn, sig = AliasMapper.map_to_needle_tool(act_rt, tools, scene, element_id=0)
    assert sig == "click(0, button='right')"
    assert fn() == "clicked"
    tools.click.assert_called_with(scene, 0, button="right")

    # Invoke
    act_inv = AliasMapper.canonicalize_action("invoke", target="Button1")
    fn, sig = AliasMapper.map_to_needle_tool(act_inv, tools, scene, element_id=0)
    assert sig == "invoke(0)"
    assert fn() == "invoked"
    tools.invoke.assert_called_with(scene, 0)

    # Type
    act_type = AliasMapper.canonicalize_action("type", target="Button1", text="abc", press_enter=True, clear_first=True)
    fn, sig = AliasMapper.map_to_needle_tool(act_type, tools, scene, element_id=0)
    assert sig == "type_text(0, text='abc')"
    assert fn() == "typed"
    tools.type_text.assert_called_with(scene, 0, text="abc", press_enter=True, clear_first=True)

    # Hotkey
    act_hk = AliasMapper.canonicalize_action("hotkey", keys=["ctrl", "s"])
    fn, sig = AliasMapper.map_to_needle_tool(act_hk, tools, scene)
    assert sig == "hotkey(['ctrl', 's'])"
    assert fn() == "hotkeyed"
    tools.hotkey.assert_called_with(["ctrl", "s"])

    # Wait
    act_wait = AliasMapper.canonicalize_action("wait", duration_seconds=1.5)
    fn, sig = AliasMapper.map_to_needle_tool(act_wait, tools, scene)
    assert sig == "wait(1.5)"
    assert fn() == "waited"
    tools.wait.assert_called_with(1.5)


def test_map_to_needle_tool_missing_element_id_raises():
    scene = _make_scene([("Button1", "Button")])
    tools = MagicMock(spec=NeedleToolSurface)

    act_click = AliasMapper.canonicalize_action("click", target="Button1")
    with pytest.raises(ValueError, match="Action 'click' requires an element_id"):
        AliasMapper.map_to_needle_tool(act_click, tools, scene, element_id=None)

    act_inv = AliasMapper.canonicalize_action("invoke", target="Button1")
    with pytest.raises(ValueError, match="Action 'invoke' requires an element_id"):
        AliasMapper.map_to_needle_tool(act_inv, tools, scene, element_id=None)

    act_type = AliasMapper.canonicalize_action("type", target="Button1", text="abc")
    with pytest.raises(ValueError, match="Action 'type' requires an element_id"):
        AliasMapper.map_to_needle_tool(act_type, tools, scene, element_id=None)


def test_map_to_needle_tool_unsupported_action_raises():
    scene = _make_scene([("Button1", "Button")])
    tools = MagicMock(spec=NeedleToolSurface)

    act_launch = AliasMapper.canonicalize_action("launch", app_name="calc")
    with pytest.raises(ValueError, match="cannot be dispatched directly to NeedleToolSurface"):
        AliasMapper.map_to_needle_tool(act_launch, tools, scene)


def test_needle_tool_surface_invoke():
    scene = _make_scene([("Submit", "Button")])
    surface = NeedleToolSurface(hwnd=1001)

    # Mock _resolve and _uia_invoke
    element = scene.get_by_id(0)
    surface._resolve = MagicMock(return_value=(element, 150, 150))

    # Success case
    surface._uia_invoke = MagicMock(return_value=True)
    res = surface.invoke(scene, element_id=0)
    assert "Invoked element 0 ('Submit') via UIA pattern." in res

    # Failure case: no UIA pattern
    surface._uia_invoke = MagicMock(return_value=False)
    with pytest.raises(ActuationError, match="no supported UIA pattern found"):
        surface.invoke(scene, element_id=0)


def test_hyphen_delimited_hotkeys_and_blacklist():
    """Verify hyphen-delimited hotkeys are split and properly intercepted by blacklist."""
    # ctrl-alt-del should split into ['ctrl', 'alt', 'del'] and trigger blacklist
    action = AliasMapper.canonicalize_action("hotkey", keys="ctrl-alt-del")
    assert action.action_type == CanonicalActionType.HOTKEY
    assert action.keys == ["ctrl", "alt", "del"]
    assert is_hotkey_blacklisted(action.keys) is True

    # ctrl - shift - esc with whitespace around hyphens
    action_taskmgr = AliasMapper.canonicalize_action("shortcut", keys="ctrl - shift - esc")
    assert action_taskmgr.keys == ["ctrl", "shift", "esc"]
    assert is_hotkey_blacklisted(action_taskmgr.keys) is True

    # alt-f4
    action_alt_f4 = AliasMapper.canonicalize_action("key_combo", keys="alt-f4")
    assert action_alt_f4.keys == ["alt", "f4"]
    assert is_hotkey_blacklisted(action_alt_f4.keys) is True

    # Non-blacklisted hyphenated hotkeys like ctrl-c
    action_copy = AliasMapper.canonicalize_action("hotkey", keys="ctrl-c")
    assert action_copy.keys == ["ctrl", "c"]
    assert is_hotkey_blacklisted(action_copy.keys) is False

    # Mixed separators: "ctrl+shift-esc"
    action_mixed = AliasMapper.canonicalize_action("hotkey", keys="ctrl+shift-esc")
    assert action_mixed.keys == ["ctrl", "shift", "esc"]
    assert is_hotkey_blacklisted(action_mixed.keys) is True


def test_multispace_and_tab_action_normalization():
    """Verify actions with irregular whitespace, multiple spaces, tabs, and hyphens normalize properly."""
    # double click with multiple spaces
    act_dbl = AliasMapper.canonicalize_action("double   click", target="btn")
    assert act_dbl.action_type == CanonicalActionType.DOUBLE_CLICK
    assert act_dbl.button == "double"

    # right click with multiple spaces
    act_rt = AliasMapper.canonicalize_action("right   click", target="btn")
    assert act_rt.action_type == CanonicalActionType.RIGHT_CLICK
    assert act_rt.button == "right"

    # uia invoke with multiple spaces
    act_inv = AliasMapper.canonicalize_action("uia   invoke", target="btn")
    assert act_inv.action_type == CanonicalActionType.INVOKE

    # tabbed action names
    act_tab = AliasMapper.canonicalize_action("double\tclick", target="btn")
    assert act_tab.action_type == CanonicalActionType.DOUBLE_CLICK

    act_tab_inv = AliasMapper.canonicalize_action("pattern\tactivate", target="btn")
    assert act_tab_inv.action_type == CanonicalActionType.INVOKE

    # multi-space and hyphens mixed
    act_mixed = AliasMapper.canonicalize_action("  open   -  app  ", app_name="notepad")
    assert act_mixed.action_type == CanonicalActionType.LAUNCH
    assert act_mixed.app_name == "notepad"


def test_launch_app_name_strict_whitespace_validation():
    """Verify launch action rejects empty and whitespace-only app_name and sanitizes valid names."""
    # Whitespace-only app_name raises ValueError
    with pytest.raises(ValueError, match="Action 'launch' requires an 'app_name' parameter"):
        AliasMapper.canonicalize_action("launch", app_name="   ")

    with pytest.raises(ValueError, match="Action 'launch' requires an 'app_name' parameter"):
        AliasMapper.canonicalize_action("launch", app_name="\t\n  ")

    # Whitespace-only target fallback also raises ValueError
    with pytest.raises(ValueError, match="Action 'launch' requires an 'app_name' parameter"):
        AliasMapper.canonicalize_action("launch", target="   ")

    # Leading/trailing whitespace is sanitized
    act_clean = AliasMapper.canonicalize_action("launch", app_name="  notepad.exe  ")
    assert act_clean.action_type == CanonicalActionType.LAUNCH
    assert act_clean.app_name == "notepad.exe"

    # Target fallback is also sanitized
    act_target = AliasMapper.canonicalize_action("launch", target="  calc.exe  ")
    assert act_target.action_type == CanonicalActionType.LAUNCH
    assert act_target.app_name == "calc.exe"


def test_parameter_type_guarding_and_coercion():
    """Verify type guarding and coercion for keys, target, and duration_seconds."""
    # keys: non-string and non-iterable raise ValueError
    with pytest.raises(ValueError, match="Action 'hotkey' keys parameter"):
        AliasMapper.canonicalize_action("hotkey", keys=123)

    # keys: iterable containing non-string raises ValueError
    with pytest.raises(ValueError, match="Action 'hotkey' keys parameter"):
        AliasMapper.canonicalize_action("hotkey", keys=[123])

    with pytest.raises(ValueError, match="Action 'hotkey' keys parameter"):
        AliasMapper.canonicalize_action("hotkey", keys=["ctrl", 123])

    # target: non-string target coerced to string
    act_click_int = AliasMapper.canonicalize_action("click", target=123)
    assert act_click_int.action_type == CanonicalActionType.CLICK
    assert act_click_int.target == "123"

    act_inv_int = AliasMapper.canonicalize_action("invoke", target=456)
    assert act_inv_int.action_type == CanonicalActionType.INVOKE
    assert act_inv_int.target == "456"

    # target: whitespace-only target rejected
    for action_name in ["click", "double_click", "right_click", "invoke"]:
        with pytest.raises(ValueError, match=f"Action '{action_name}' requires a non-empty 'target' parameter"):
            AliasMapper.canonicalize_action(action_name, target="   ")

    # duration_seconds: non-numeric raises ValueError for wait
    with pytest.raises(ValueError, match="Action 'wait' requires a numeric 'duration_seconds' parameter"):
        AliasMapper.canonicalize_action("wait", duration_seconds="abc")

    with pytest.raises(ValueError, match="Action 'wait' requires a numeric 'duration_seconds' parameter"):
        AliasMapper.canonicalize_action("wait", seconds="invalid_time")

    # duration_seconds: negative raises ValueError
    with pytest.raises(ValueError, match="Action 'wait' requires a non-negative 'duration_seconds' parameter"):
        AliasMapper.canonicalize_action("wait", duration_seconds=-0.5)

    # duration_seconds: numeric string and int safely converted
    act_wait_str = AliasMapper.canonicalize_action("wait", duration_seconds="2.5")
    assert act_wait_str.duration_seconds == 2.5

    act_wait_int = AliasMapper.canonicalize_action("wait", duration_seconds=3)
    assert act_wait_int.duration_seconds == 3.0

    act_wait_sec = AliasMapper.canonicalize_action("wait", seconds="1.75")
    assert act_wait_sec.duration_seconds == 1.75


