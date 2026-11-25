"""General control skills: state in the scene, verified state actions, smarter resolution."""

import pytest

from servo.actuation.alias_mapping import AliasMapper, CanonicalActionType as A
from servo.perception.scene_index import SceneElement, SceneIndex


def el(i, role, label, box=(0, 0, 10, 10), **kw):
    return SceneElement(element_id=i, role=role, label=label, bbox_screen=box, bbox_norm=(0, 0, 1, 1),
                        center_screen=((box[0] + box[2]) // 2, (box[1] + box[3]) // 2), **kw)


def scene(*els):
    return SceneIndex(hwnd=1, window_title="W", source="uia", elements={e.element_id: e for e in els})


# ------------------------------------------------------------------ Needle's set/choose tools
# (the regex control grammar is gone; tool mapping is covered in test_cascade.py)

def test_settings_page_links_open_without_a_command():
    from servo.cognition.needle import to_steps
    from servo.daemon import copilot
    s = to_steps("open_settings", {"page": "display"})[0]
    assert s.tool_name == "launch_app" and s.arguments == {"app_name": "ms-settings:display"}
    assert copilot._settings_page("ms-settings:display") and copilot._settings_page("ms-settings:x&calc") is None


@pytest.mark.parametrize("name,args,tool", [
    ("turn_on", {"control": 7}, "set_toggle"),
    ("turn_off", {"control": 7}, "set_toggle"),
    ("expand", {"control": 12}, "set_expanded"),
    ("collapse", {"control": 12}, "set_expanded"),
    ("set_value", {"value": "40", "control": 3}, "set_value"),
])
def test_needle_form_calls_carry_canonical_state_actions(name, args, tool):
    from servo.cognition.needle import to_steps
    step = to_steps(name, args)[0]
    assert step.tool_name == tool
    assert step.canonical_action.action_type.value == tool and step.canonical_action.target == str(args["control"])


# ------------------------------------------------------------------ resolution

def test_nested_duplicate_resolves_to_the_edit_for_typing():
    """Save As: 'File name:' is a combo box with an edit of the same name inside it."""
    combo = el(0, "ComboBox", "File name:", box=(100, 500, 600, 530))
    edit = el(1, "Edit", "File name:", box=(102, 502, 580, 528))
    s = scene(combo, edit)
    assert AliasMapper.resolve_element(s, "File name", A.TYPE)[0] is edit
    assert AliasMapper.resolve_element(s, "File name", A.CLICK)[0] is combo


def test_repeated_labels_resolve_by_row_context():
    a = el(0, "Button", "Show more settings", box=(900, 100, 940, 130), context="Advanced options", expanded=False)
    b = el(1, "Button", "Show more settings", box=(900, 200, 940, 230), context="Privacy", expanded=False)
    s = scene(a, b)
    assert AliasMapper.resolve_element(s, "Show more settings (Privacy)", A.CLICK)[0] is b
    assert AliasMapper.resolve_element(s, "Privacy", A.SET_EXPANDED)[0] is b
    found, why = AliasMapper.resolve_element(s, "Show more settings", A.CLICK)
    assert found is None and "(Privacy)" in why and "(Advanced options)" in why   # ambiguous: say which


def test_text_label_names_the_control_on_its_row():
    label = el(0, "Text", "Screen brightness", box=(100, 300, 300, 320), interactive=False)
    slider = el(1, "Slider", "Slider_1", box=(400, 295, 800, 325))
    other = el(2, "Button", "Apply", box=(400, 600, 480, 630))
    assert AliasMapper.resolve_element(scene(label, slider, other), "Screen brightness", A.SET_VALUE)[0] is slider


def test_controls_win_over_plain_text_with_the_same_words():
    heading = el(0, "Text", "Bluetooth", box=(0, 0, 100, 20), interactive=False)
    switch = el(1, "Button", "Bluetooth", box=(500, 0, 560, 20), toggle="off")
    assert AliasMapper.resolve_element(scene(heading, switch), "Bluetooth", A.SET_TOGGLE)[0] is switch


def test_disabled_duplicates_lose():
    a = el(0, "Button", "OK", box=(0, 0, 50, 20), enabled=False)
    b = el(1, "Button", "OK", box=(100, 0, 150, 20))
    assert AliasMapper.resolve_element(scene(a, b), "OK", A.CLICK)[0] is b


# ------------------------------------------------------------------ what the planner sees

def test_scene_description_shows_state_values_and_text():
    from servo.cognition.router import describe_scene
    s = scene(el(0, "Button", "Bluetooth", toggle="on"),
              el(1, "ComboBox", "Text size", value="Medium", expanded=False),
              el(2, "Button", "Show more settings", context="Advanced options", expanded=False),
              el(3, "Button", "Show more settings", context="Privacy", expanded=True),
              el(4, "Button", "Delete", enabled=False),
              el(5, "Text", "Device name   DESKTOP-1", interactive=False))
    text = describe_scene(s)
    assert "Button: Bluetooth [on]" in text
    assert "ComboBox: Text size = Medium [collapsed]" in text
    assert "Show more settings (Advanced options) [collapsed]" in text and "(Privacy) [expanded]" in text
    assert "Button: Delete [disabled]" in text
    assert "Text on screen:\nDevice name DESKTOP-1" in text


def test_sensor_drops_window_chrome_and_scrollbar_parts():
    from servo.perception.uia_sensor import _is_noise
    for label in ["Minimize Settings", "Maximize", "Restore Settings", "Vertical Small Decrease", "Page down"]:
        assert _is_noise("Button", label, "Settings"), label
    for label in ["Close", "Restore defaults", "Page setup", "Minimal mode", "Minimize to tray"]:
        assert not _is_noise("Button", label, "Settings"), label


def test_row_context_comes_from_the_text_beside_a_control():
    from servo.perception.uia_sensor import _add_row_context
    els = {0: el(0, "Button", "Show more settings", box=(900, 100, 940, 130)),
           1: el(1, "Button", "Show more settings", box=(900, 200, 940, 230)),
           2: el(2, "Button", "Unique", box=(900, 300, 940, 330))}
    texts = [("Advanced options", (100, 105, 400, 125), None), ("Privacy", (100, 205, 300, 225), None),
             ("Something", (100, 305, 300, 325), None)]
    _add_row_context(els, texts)
    assert (els[0].context, els[1].context, els[2].context) == ("Advanced options", "Privacy", None)


# ------------------------------------------------------------------ verified state actions

class FakeToggle:
    def __init__(self, state):
        self.CurrentToggleState = state

    def Toggle(self):
        self.CurrentToggleState = 1 - self.CurrentToggleState


class FakeExpand:
    def __init__(self, state, stuck=False):
        self.CurrentExpandCollapseState, self.stuck = state, stuck

    def Expand(self):
        if not self.stuck:
            self.CurrentExpandCollapseState = 1

    def Collapse(self):
        self.CurrentExpandCollapseState = 0


class Live:
    def __init__(self, **ifaces):
        for k, v in ifaces.items():
            setattr(self, k, v)


def _scene_with(live):
    e = el(0, "Button", "Bluetooth")
    s = scene(e)
    s._live[0] = live
    return s, e


def test_set_toggle_flips_once_and_confirms(monkeypatch):
    from servo.actuation import patterns
    monkeypatch.setattr(patterns.time, "sleep", lambda s: None)
    t = FakeToggle(0)
    s, e = _scene_with(Live(iface_toggle=t))
    assert patterns.set_toggle(s, e, True, click=lambda: None)[0] == "confirmed" and t.CurrentToggleState == 1
    verdict, why = patterns.set_toggle(s, e, True, click=lambda: None)
    assert verdict == "confirmed" and "already on" in why and t.CurrentToggleState == 1   # no double flip


def test_set_toggle_without_readable_state_is_unconfirmed(monkeypatch):
    from servo.actuation import patterns
    monkeypatch.setattr(patterns.time, "sleep", lambda s: None)
    clicks = []
    s, e = _scene_with(Live())
    assert patterns.set_toggle(s, e, True, click=lambda: clicks.append(1))[0] == "unconfirmed" and clicks == [1]


def test_set_expanded_reports_failure_when_nothing_opens(monkeypatch):
    from servo.actuation import patterns
    monkeypatch.setattr(patterns.time, "sleep", lambda s: None)
    monkeypatch.setattr(patterns, "_wait_until", lambda pred, timeout=1.2: pred())
    s, e = _scene_with(Live(iface_expand_collapse=FakeExpand(0, stuck=True)))
    assert patterns.set_expanded(s, e, True, click=lambda: None)[0] == "failed"
    s, e = _scene_with(Live(iface_expand_collapse=FakeExpand(0)))
    assert patterns.set_expanded(s, e, True, click=lambda: None)[0] == "confirmed"


def test_pick_option_prefers_exact_then_unique_prefix():
    from servo.actuation.patterns import _pick_option

    class Item:
        def __init__(self, name):
            self.name = name
            self.element_info = type("E", (), {"name": name})()

        def window_text(self):
            return self.name

    items = [Item("1 minute"), Item("10 minutes"), Item("15 minutes"), Item("Never")]
    assert _pick_option(items, "10 minutes").name == "10 minutes"
    assert _pick_option(items, "never").name == "Never"
    assert _pick_option(items, "1 minute").name == "1 minute"
    assert _pick_option(items, "1") is None                        # 1 minute / 10 / 15: ambiguous
    assert _pick_option(items, "nev").name == "Never"              # unique prefix


def test_pattern_verdict_becomes_the_step_verdict(monkeypatch, laya):
    from servo.control import loop as loop_mod
    lp = loop_mod.ServoControlLoop.__new__(loop_mod.ServoControlLoop)
    calls = []
    monkeypatch.setattr(lp, "_run_pattern_action",
                        lambda scene, el, a, state, value: calls.append((a, state)) or ("confirmed", "now on"),
                        raising=False)
    lp.hwnd, lp.mode, lp.confirmation_callback, lp.should_abort = 1, "harness", None, None
    lp.tracer = loop_mod.NullTracer()

    class Settle:
        def settle_and_diff(self, scene, fn):
            from servo.perception.scene_index import SceneDelta
            return scene, SceneDelta()

    from servo.control.jev_gate import JevGate
    lp.jev, lp.settling = JevGate(), Settle()
    monkeypatch.setattr(loop_mod.windows, "get_process_name", lambda h: "systemsettings.exe")
    target = el(0, "Button", "Bluetooth", toggle="off")
    res = lp._run_reflex_pass(scene(target), target, 1, "turn on Bluetooth", "set_toggle", "", "", "uia",
                              lambda: scene(target), state="on")
    assert calls == [("set_toggle", "on")]
    assert res.success and res.step_log.verification == "confirmed"


# ------------------------------------------------------------------ declined commands

def test_declined_command_replans_once_through_the_interface(monkeypatch):
    from servo.cognition.router import CascadeRouter
    from servo.cognition.trace import NullTracer
    r = CascadeRouter(tracer=NullTracer())
    monkeypatch.setattr(r.system2, "available", lambda: True)
    counters = {}
    declined = {"tool": "run_command", "error": "Action rejected by user confirmation (declined or timed out)."}
    assert r._declined_command_can_replan(dict(declined), counters)
    assert not r._declined_command_can_replan(dict(declined), counters)          # second decline: stop
    gui = {"tool": "click_element", "error": "Action rejected by user confirmation"}
    assert not CascadeRouter(tracer=NullTracer())._declined_command_can_replan(gui, {})


# ------------------------------------------------------------------ fixes from the first held-out run

def test_click_on_a_toggle_is_judged_by_its_state(monkeypatch):
    """The 'File name extensions' menu item: the menu closed (screen changed), the setting didn't."""
    from servo.actuation import patterns
    from servo.control import loop as loop_mod
    lp = loop_mod.ServoControlLoop.__new__(loop_mod.ServoControlLoop)
    monkeypatch.setattr(patterns, "_wait_until", lambda pred, timeout=1.0: pred())
    item = el(0, "MenuItem", "File name extensions", toggle="off")
    s = scene(item)
    s._live[0] = Live(iface_toggle=FakeToggle(0))                 # still off after the click
    assert lp._stateful_click_verdict(s, item)[0] == "failed"
    s._live[0] = Live(iface_toggle=FakeToggle(1))                 # flipped
    assert lp._stateful_click_verdict(s, item)[0] == "confirmed"
    s._live[0] = Live()                                           # gone (menu closed): not a success
    assert lp._stateful_click_verdict(s, item)[0] == "unconfirmed"
    assert lp._stateful_click_verdict(scene(el(1, "Button", "OK")), el(1, "Button", "OK")) is None


def test_after_a_declined_command_further_commands_are_refused_without_asking(monkeypatch):
    from servo.cognition.router import CascadeRouter
    from servo.cognition.trace import NullTracer
    from servo.cognition.needle import _step
    asked = []
    r = CascadeRouter(tracer=NullTracer(), confirmation_callback=lambda req: asked.append(req) or False)
    r._commands_declined = True
    out = r._system(_step(1, "run_command", {"command": "hostname", "shell": "powershell"}, "x"))
    assert not out.success and out.kind == "reasoning" and asked == []


def test_descriptive_labels_fall_back_to_the_plain_label():
    s = scene(el(0, "ListItem", "System"), el(1, "ListItem", "Apps"))
    assert AliasMapper.resolve_element(s, "System in Settings left navigation", A.CLICK)[0].label == "System"
    assert AliasMapper.resolve_element(s, "Save on the toolbar", A.CLICK)[0] is None


def test_sensor_reports_a_closed_window_immediately(monkeypatch):
    from servo.perception import uia_sensor
    monkeypatch.setattr(uia_sensor.sys, "platform", "win32")
    import servo.perception.windows as w
    monkeypatch.setattr(w, "is_window", lambda h: False)
    sensor = uia_sensor.UIASensor.__new__(uia_sensor.UIASensor)
    sensor.hwnd = 99
    assert sensor.sense().sense_error == uia_sensor.WINDOW_CLOSED


# ------------------------------------------------------------------ fixes from the second held-out run

def test_side_by_side_duplicates_take_their_section_heading():
    """Two 'Turn my screen off after' dropdowns under 'Plugged in' and 'On battery'."""
    from servo.perception.uia_sensor import _add_row_context
    els = {0: el(0, "ComboBox", "Turn my screen off after", box=(1549, 624, 1722, 673)),
           1: el(1, "ComboBox", "Turn my screen off after", box=(1434, 892, 1722, 942))}
    texts = [("Plugged in", (544, 582, 1772, 606), None),
             ("Turn off the screen after 15 minutes to save energy", (617, 651, 1188, 671), None),
             ("On battery", (544, 852, 1772, 876), None)]
    _add_row_context(els, texts)
    assert (els[0].context, els[1].context) == ("Plugged in", "On battery")


def test_row_title_beats_description_and_status_words():
    from servo.perception.uia_sensor import _add_row_context
    els = {0: el(0, "Button", "Show more settings", box=(1722, 501, 1762, 541)),
           1: el(1, "Button", "Show more settings", box=(1722, 682, 1762, 722))}
    texts = [("Screen, sleep, & hibernate timeouts", (597, 500, 872, 524), None),
             ("Choose what happens when your device is idle", (597, 524, 1047, 544), None),
             ("Energy saver", (597, 682, 695, 706), None), ("Reduce power consumption", (597, 706, 1219, 726), None),
             ("Off", (1696, 694, 1717, 714), None)]
    _add_row_context(els, texts)
    assert (els[0].context, els[1].context) == ("Screen, sleep, & hibernate timeouts", "Energy saver")


def test_dropdown_shows_its_current_choice_as_value():
    from servo.perception.uia_sensor import _fold_dropdown_selection
    els = {0: el(0, "ComboBox", "Turn my screen off after", box=(1549, 624, 1722, 673), value="Turn my screen off after"),
           1: el(1, "ListItem", "2 hours", box=(1569, 636, 1669, 660), selected=True),
           2: el(2, "ListItem", "Never", box=(100, 100, 200, 120), selected=True)}
    _fold_dropdown_selection(els)
    assert els[0].value == "2 hours" and 1 not in els and 2 in els


def test_popup_menus_of_the_target_app_do_not_count_as_covering(monkeypatch):
    from servo.actuation import sandboxing as sb
    monkeypatch.setattr(sb.sys, "platform", "win32")
    pids = {10: 1, 20: 1, 30: 2, 40: 1}
    monkeypatch.setattr(sb, "_pid_of", lambda h: pids[h])

    class U:
        def GetWindow(self, h, cmd):
            return 10 if h == 20 else 0          # 20 is owned by the target (10)

        def GetWindowLongW(self, h, idx):
            return {40: 0x80000000}.get(h, 0x00C00000)   # 40: borderless popup; others: captioned

    monkeypatch.setattr(sb.ctypes, "windll", type("W", (), {"user32": U()})(), raising=False)
    monkeypatch.setattr(sb.windows, "get_root_window", lambda h: h)
    monkeypatch.setattr(sb, "app_pids", lambda target: {pids[target]})
    monkeypatch.setattr(sb, "_class_name", lambda h: "Shell_TrayWnd" if h == 60 else "#32768")
    assert sb.is_popup_of(20, 10)          # owned menu
    assert sb.is_popup_of(40, 10)          # same app, borderless popup
    assert not sb.is_popup_of(30, 10)      # another app
    pids[50] = 1
    assert not sb.is_popup_of(50, 10)      # same app, but a full captioned window
    pids[60] = 1
    assert not sb.is_popup_of(60, 10)      # the taskbar: explorer.exe like File Explorer, but the shell's


def test_store_app_frames_come_before_their_inner_core_window(monkeypatch):
    from servo.daemon import copilot
    import win32gui
    monkeypatch.setattr(copilot.windows, "list_windows", lambda: [
        {"hwnd": 1, "title": "Settings", "process": "systemsettings.exe"},
        {"hwnd": 2, "title": "Settings", "process": "applicationframehost.exe"}])
    monkeypatch.setattr(win32gui, "GetClassName", lambda h: {1: "Windows.UI.Core.CoreWindow", 2: "ApplicationFrameWindow"}[h])
    assert [w["hwnd"] for w in copilot._app_windows()] == [2, 1]


# ------------------------------------------------------------------ fixes from the third held-out run

def test_untargeted_typing_goes_to_the_focused_field():
    """After Rename, the in-place name box has focus; the address bar is bigger."""
    address = el(0, "Edit", "Address", box=(0, 0, 1200, 40))
    rename = el(1, "Edit", "draft.txt", box=(300, 400, 500, 420), has_focus=True)
    s = scene(address, rename)
    assert AliasMapper.resolve_element(s, "editor", A.TYPE)[0] is rename
    assert AliasMapper.resolve_element(s, "", A.TYPE)[0] is rename
    assert AliasMapper.resolve_element(s, "Address", A.TYPE)[0] is address   # a named target still wins


def test_open_menus_are_merged_into_the_scene_first(monkeypatch):
    from servo.perception import uia_sensor
    main = scene(el(0, "Button", "View"))
    menu = SceneIndex(hwnd=2, window_title="", source="uia",
                      elements={0: el(0, "MenuItem", "File name extensions", toggle="off")})
    monkeypatch.setattr(uia_sensor.sys, "platform", "win32")
    monkeypatch.setattr(uia_sensor, "_open_popups", lambda h: [2])
    monkeypatch.setattr(uia_sensor.UIASensor, "_sense_once", lambda self: menu if self.hwnd == 2 else main)
    monkeypatch.setattr(uia_sensor.UIASensor, "__init__", lambda self, h: setattr(self, "hwnd", h))
    sensor = uia_sensor.UIASensor(1)
    merged = sensor._with_open_popups(main)
    assert [e.label for e in merged.elements.values()] == ["File name extensions", "View"]
    assert sorted(merged.elements) == [0, 1]


# ------------------------------------------------- planner step ↔ Needle call check

from servo.cognition.needle import check_call  # noqa: E402


@pytest.mark.parametrize("step,name,args", [
    ("click [12]", "click", {"control": 12}),
    ("click [12]", "click", {"control": "12"}),
    ("double click [27]", "double_click", {"control": 27}),
    ("right click [13]", "right_click", {"control": 13}),
    ("scroll up", "scroll_up", {}),
    ("scroll up in [3]", "scroll_up", {"control": 3}),
    ("scroll down in [3]", "scroll_down", {"control": 3}),
    ('type "hello" into [4]', "type_into", {"text": "hello", "control": 4}),
    ('type "hello"', "type_text", {"text": "hello"}),
    ('type "hello" and press enter', "type_text", {"text": "hello", "press_enter": True}),
    ("clear [4]", "clear", {"control": 4}),
    ("turn on [18]", "turn_on", {"control": 18}),
    ("turn off [18]", "turn_off", {"control": 18}),
    ('set "03" in [2]', "set_value", {"value": "03", "control": 2}),
    ('choose "125%" in [23]', "choose", {"option": "125%", "control": 23}),
    ("expand [5]", "expand", {"control": 5}),
    ("collapse [5]", "collapse", {"control": 5}),
    ("wait 2", "wait", {"seconds": 2}),
    ("open notepad", "open_app", {"name": "notepad"}),
    ("open settings display", "open_settings", {"page": "display"}),
    ('close "draft.txt - Notepad"', "close_window", {"window": "draft.txt - Notepad"}),
    ('switch to "Downloads - File Explorer"', "switch_to", {"window": "Downloads - File Explorer"}),
    ("go to github.com", "open_web", {"address": "github.com"}),
    ("read file C:/notes.txt", "read_file", {"path": "C:/notes.txt"}),
    (r"read file C:\Users\me\notes.txt", "read_file", {"path": r"C:\Users\me\notes.txt"}),
    ("list folder downloads", "list_folder", {"path": "Downloads"}),
    ('write "hi" to file C:/out.txt', "write_file", {"text": "hi", "path": "C:/out.txt"}),
    ('append "hi" to file C:/out.txt', "append_file", {"text": "hi", "path": "C:/out.txt"}),
    ('run command "Get-Date"', "run_command", {"command": "Get-Date"}),
])
def test_matching_calls_pass(step, name, args):
    assert check_call(step, name, args) is None


@pytest.mark.parametrize("step,name,args,why", [
    ("wait 2", "close_window", {"window": ""}, "wait"),                           # measured on Needle 3
    ("double click [27]", "click", {"control": 27}, "double_click"),              # measured: a plain click
    ("right click [13]", "right_click", {"control": 14}, "[13]"),
    ("scroll up", "scroll_down", {}, "scroll_up"),
    ("scroll down", "scroll_down", {"control": 3}, "doesn't mention"),
    ("turn on [18]", "turn_off", {"control": 18}, "turn_on"),
    ('set "03" in [2]', "set_value", {"value": "2", "control": 3}, "[2]"),         # measured: value in control
    ('set "03" in [2]', "set_value", {"value": "3", "control": 2}, "03"),
    ("set [2] to 03", "set_value", {"value": "03", "control": 2}, "no quoted text"),  # the old form
    ('run command "Get-Date"', "run_command", {"command": "Get-Time"}, "Get-Date"),
    ('choose "125%" in [23]', "choose", {"option": "23", "control": 125}, "[23]"),  # measured: swapped
    ("click [12]", "click", {"control": 13}, "[12]"),
    ("click Save", "click", {"control": 7}, "doesn't mention"),
    ('type "hello" into [4]', "type_into", {"text": "hell", "control": 4}, "hello"),
    ("wait 3", "wait", {"seconds": 1}, "3"),
    # window titles: a quoted title must be the call's window
    ('switch to "Downloads - File Explorer"', "switch_to", {"window": "Documents - File Explorer"}, "Downloads"),
    ('close "draft.txt - Notepad"', "close_window", {"window": "Notepad"}, "draft.txt - Notepad"),
    ('close "draft.txt - Notepad"', "close_window", {}, "window"),
    # file paths: the call's path must be the one in the step
    ("read file C:/notes.txt", "read_file", {"path": "C:/other.txt"}, "path"),
    ("list folder downloads", "list_folder", {"path": ""}, "path"),
    ('write "hi" to file C:/out.txt', "write_file", {"text": "hi", "path": "C:/Users/out.txt"}, "path"),
])
def test_mismatched_calls_are_named(step, name, args, why):
    problem = check_call(step, name, args)
    assert problem and why in problem
