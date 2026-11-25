"""State-setting actions through UI Automation patterns, each verified by reading back.

Clicking a switch only flips it: the planner had to guess the current state, and the
result could only ever be "ran, effect not visible". These actions set an outcome
instead ("make it on", "choose 10 minutes", "open this section", "set this field") and
report a verdict from the control's own state afterwards:

* ``confirmed`` - the control now reports the requested state (or already did)
* ``failed``    - it reports something else
* ``unconfirmed`` - the control exposes no readable state; the action ran

Every function takes the scene element and returns ``(verdict, explanation)``.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, List, Optional, Tuple

from servo.perception.scene_index import SceneElement, SceneIndex

logger = logging.getLogger("servo.patterns")

Verdict = Tuple[str, str]

# UIA property ids
_P_IS_READONLY = 30046
_P_HAS_SCROLL = 30034
_P_V_SCROLL_PERCENT = 30055
_P_V_SCROLLABLE = 30058
# ScrollAmount
_LARGE_DECREMENT, _NO_AMOUNT, _LARGE_INCREMENT = 0, 2, 3


class PatternUnavailable(RuntimeError):
    """The control has no pattern for this action (caller may fall back to a click)."""


def _wrap(elem: Any) -> Any:
    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.uia_element_info import UIAElementInfo
    return UIAWrapper(UIAElementInfo(elem))


def wrapper_for(scene: SceneIndex, el: SceneElement) -> Any:
    """The live control behind a scene element: the sensor's handle, else hit-test its centre."""
    w = scene.live(el.element_id) if hasattr(scene, "live") else None
    if w is not None:
        return w
    from pywinauto.uia_defines import IUIA
    from ctypes import wintypes
    pt = wintypes.POINT(*el.center_screen)
    elem = IUIA().iuia.ElementFromPoint(pt)
    if elem is None:
        raise PatternUnavailable(f"No control at {el.center_screen}")
    return _wrap(elem)


def _iface(w: Any, name: str) -> Any:
    try:
        return getattr(w, name)
    except Exception:
        return None


def _wait_until(pred: Callable[[], bool], timeout: float = 1.2) -> bool:
    end = time.time() + timeout
    while True:
        try:
            if pred():
                return True
        except Exception:
            pass
        if time.time() >= end:
            return False
        time.sleep(0.05)


# ------------------------------------------------------------------ toggle

def _toggle_state(w: Any) -> Optional[str]:
    t = _iface(w, "iface_toggle")
    if t is None:
        return None
    try:
        return {0: "off", 1: "on", 2: "mixed"}.get(int(t.CurrentToggleState))
    except Exception:
        return None


def set_toggle(scene: SceneIndex, el: SceneElement, on: bool, click: Callable[[], Any]) -> Verdict:
    """Make a switch/checkbox on or off. Already in that state = confirmed, nothing done."""
    want = "on" if on else "off"
    w = wrapper_for(scene, el)
    state = _toggle_state(w)
    if state == want:
        return "confirmed", f"“{el.label}” was already {want}"
    toggled = False
    t = _iface(w, "iface_toggle")
    if t is not None:
        try:
            t.Toggle()
            toggled = True
        except Exception as e:
            logger.debug("Toggle pattern failed on %s: %s", el.label, e)
    if not toggled:
        sel = _iface(w, "iface_selection_item")
        if on and sel is not None:          # radio buttons: "on" means selected
            sel.Select()
            ok = _wait_until(lambda: bool(sel.CurrentIsSelected))
            return ("confirmed" if ok else "failed"), f"Selected “{el.label}”" if ok else f"“{el.label}” did not become selected"
        click()
    if _wait_until(lambda: _toggle_state(w) == want):
        return "confirmed", f"“{el.label}” is now {want}"
    after = _toggle_state(w)
    if after is None:
        return "unconfirmed", f"Clicked “{el.label}”; it doesn't report an on/off state"
    if after == state and not toggled:
        return "failed", f"“{el.label}” is still {after}"
    return "failed", f"“{el.label}” is {after}, not {want}"


# ------------------------------------------------------------------ expand / collapse

def _expand_state(w: Any) -> Optional[bool]:
    ec = _iface(w, "iface_expand_collapse")
    if ec is None:
        return None
    try:
        st = int(ec.CurrentExpandCollapseState)
    except Exception:
        return None
    return None if st == 3 else st != 0


def set_expanded(scene: SceneIndex, el: SceneElement, expand: bool, click: Callable[[], Any]) -> Verdict:
    word = "open" if expand else "closed"
    w = wrapper_for(scene, el)
    state = _expand_state(w)
    if state is expand:
        return "confirmed", f"“{el.label}” was already {word}"
    ec = _iface(w, "iface_expand_collapse")
    try:
        if ec is None:
            raise PatternUnavailable("no expand/collapse pattern")
        ec.Expand() if expand else ec.Collapse()
    except Exception:
        click()
    if _wait_until(lambda: _expand_state(w) is expand):
        return "confirmed", f"“{el.label}” is now {word}"
    if _expand_state(w) is None:
        return "unconfirmed", f"Clicked “{el.label}”; it doesn't report open/closed"
    return "failed", f"“{el.label}” did not become {word}"


# ------------------------------------------------------------------ set value

def _read_value(w: Any) -> Optional[str]:
    v = _iface(w, "iface_value")
    if v is not None:
        try:
            return str(v.CurrentValue)
        except Exception:
            pass
    rv = _iface(w, "iface_range_value")
    if rv is not None:
        try:
            return f"{float(rv.CurrentValue):g}"
        except Exception:
            pass
    return None


def _same_value(got: Optional[str], want: Any) -> bool:
    """Text equal, or the same number however it is padded ("03" for 3, "40.0" for 40)."""
    a, b = (got or "").strip(), str(want).strip()
    if a == b:
        return True
    try:
        return float(a) == float(b)
    except ValueError:
        return False


def set_value(scene: SceneIndex, el: SceneElement, value: str) -> Verdict:
    """Set a text field or slider directly (ValuePattern / RangeValuePattern)."""
    w = wrapper_for(scene, el)
    rv = _iface(w, "iface_range_value")
    v = _iface(w, "iface_value")
    if rv is not None and v is None:
        try:
            target = float(str(value).strip().rstrip("%"))
        except ValueError:
            return "failed", f"“{el.label}” is a slider; “{value}” is not a number"
        lo, hi = float(rv.CurrentMinimum), float(rv.CurrentMaximum)
        rv.SetValue(max(lo, min(hi, target)))
        ok = _wait_until(lambda: abs(float(rv.CurrentValue) - max(lo, min(hi, target))) < 1e-6)
        got = _read_value(w)
        return ("confirmed" if ok else "failed"), f"“{el.label}” = {got}"
    if v is None:
        raise PatternUnavailable(f"“{el.label}” has no settable value")
    try:
        if bool(w.element_info.element.GetCurrentPropertyValue(_P_IS_READONLY)):
            raise PatternUnavailable(f"“{el.label}” is read-only")
    except PatternUnavailable:
        raise
    except Exception:
        pass
    v.SetValue(str(value))
    ok = _wait_until(lambda: _same_value(_read_value(w), value))
    got = _read_value(w)
    return ("confirmed" if ok else "failed"), f"“{el.label}” = {got!r}"


# ------------------------------------------------------------------ select option

def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def _pick_option(items: List[Any], option: str) -> Optional[Any]:
    """Exact name first; otherwise a unique prefix or substring match ("10" -> "10 minutes")."""
    want = _norm(option)
    named = [(_norm(i.window_text() or i.element_info.name or ""), i) for i in items]
    exact = [i for n, i in named if n == want]
    if exact:
        return exact[0]
    for hits in ([i for n, i in named if n.startswith(want)], [i for n, i in named if want in n]):
        if len(hits) == 1:
            return hits[0]
    return None


def _option_items(w: Any, pid: int) -> List[Any]:
    """Options of a dropdown: its own children, else items in a popup of the same app."""
    items = [c for c in w.descendants() if c.element_info.control_type in ("ListItem", "MenuItem", "DataItem")]
    if items:
        return items
    from pywinauto import Desktop
    found: List[Any] = []
    for top in Desktop(backend="uia").windows(process=pid):
        try:
            found += [c for c in top.descendants() if c.element_info.control_type in ("ListItem", "MenuItem")]
        except Exception:
            continue
    return found


def _selected_option(w: Any) -> Optional[str]:
    val = _read_value(w)
    if val:
        return val
    for c in w.descendants():
        try:
            if c.element_info.control_type == "ListItem" and c.iface_selection_item.CurrentIsSelected:
                return c.window_text() or c.element_info.name
        except Exception:
            continue
    try:
        return w.window_text() or None
    except Exception:
        return None


def select_option(scene: SceneIndex, el: SceneElement, option: str, click: Callable[[], Any]) -> Verdict:
    """Choose an option in a dropdown/list ("10 minutes" in "Screen timeout")."""
    w = wrapper_for(scene, el)
    current = _selected_option(w)
    if current and _norm(current) == _norm(option):
        return "confirmed", f"“{el.label}” already shows “{current}”"
    # Editable combo boxes accept the text directly.
    v = _iface(w, "iface_value")
    if v is not None:
        try:
            if not bool(w.element_info.element.GetCurrentPropertyValue(_P_IS_READONLY)):
                v.SetValue(option)
                if _wait_until(lambda: _norm(_selected_option(w) or "") == _norm(option)):
                    return "confirmed", f"“{el.label}” now shows “{option}”"
        except Exception:
            pass
    ec = _iface(w, "iface_expand_collapse")
    try:
        if ec is None:
            raise PatternUnavailable("no expand pattern")
        ec.Expand()
    except Exception:
        click()
    time.sleep(0.35)
    pid = w.element_info.process_id
    items = _option_items(w, pid)
    choice = _pick_option(items, option)
    if choice is None:
        # Show the options closest to what was asked (shared words), not the first 15 of a
        # long list: "Pacific (US & Canada)" must surface "(UTC-08:00) Pacific Time (US & Canada)".
        words = set(re.findall(r"\w+", option.lower()))
        all_names = [i.window_text() for i in items if i.window_text()]
        ranked = sorted(all_names, key=lambda n: -len(words & set(re.findall(r"\w+", n.lower()))))
        names = ranked[:15]
        try:
            ec and ec.Collapse()
        except Exception:
            pass
        return "failed", f"No option “{option}” in “{el.label}”. Options: {', '.join(names) or 'none visible'}"
    picked = False
    try:
        choice.iface_selection_item.Select()
        picked = True
    except Exception:
        pass
    if not picked:
        choice.click_input()
    time.sleep(0.2)
    try:
        if ec is not None and int(ec.CurrentExpandCollapseState) == 1:
            ec.Collapse()
    except Exception:
        pass
    label = choice.window_text() or option
    if _wait_until(lambda: _norm(_selected_option(w) or "") == _norm(label)):
        return "confirmed", f"“{el.label}” now shows “{label}”"
    now = _selected_option(w)
    if now is None:
        return "unconfirmed", f"Chose “{label}” in “{el.label}”; it doesn't report its selection"
    return "failed", f"“{el.label}” shows “{now}”, not “{label}”"


# ------------------------------------------------------------------ scrolling

def _scrollables(hwnd: int) -> List[Any]:
    from pywinauto import Desktop
    win = Desktop(backend="uia").window(handle=hwnd)
    out = []
    for c in win.descendants():
        try:
            e = c.element_info.element
            if e.GetCurrentPropertyValue(_P_HAS_SCROLL) and e.GetCurrentPropertyValue(_P_V_SCROLLABLE):
                r = c.rectangle()
                if r.width() > 0 and r.height() > 0:
                    out.append(c)
        except Exception:
            continue
    out.sort(key=lambda c: c.rectangle().width() * c.rectangle().height(), reverse=True)
    return out


def scroll_page(hwnd: int, direction: str = "down") -> bool:
    """Scroll the window's main scrollable area one page. True if it moved."""
    for c in _scrollables(hwnd)[:2]:
        try:
            s = c.iface_scroll
            before = float(s.CurrentVerticalScrollPercent)
            s.Scroll(_NO_AMOUNT, _LARGE_INCREMENT if direction == "down" else _LARGE_DECREMENT)
            time.sleep(0.25)
            if abs(float(s.CurrentVerticalScrollPercent) - before) > 1e-6:
                return True
        except Exception:
            continue
    return False
