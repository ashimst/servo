"""UI Automation (UIA) sensor wrapping pywinauto.

Extracts semantic accessibility tree and visible interactive elements.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from typing import Any, Dict, List, Optional, Set
from servo.perception.viewport import ViewportTransform
from servo.perception.scene_index import SceneIndex, SceneElement

logger = logging.getLogger(__name__)

# Roles that are targets only when they open/close (a Settings section is a Group), or, for a
# custom control, when it has a value you can set (a timer's "hours" picker).
MAYBE_INTERACTIVE_TYPES = {"Group", "Pane", "Custom"}
# Plain text worth reading: headings, descriptions, status lines ("Night light: Off").
READABLE_TEXT_TYPES = {"Text"}
MAX_TEXT_ITEMS = 60
WINDOW_CLOSED = "window closed"

# Window chrome and scrollbar parts: never what a task is about, and they crowded the
# list the planner sees ("Vertical Small Decrease", "Minimize Settings").
_NOISE = re.compile(
    r"^(?:vertical|horizontal)\s+(?:small|large)\s+(?:increase|decrease)$"
    r"|^(?:line|page)\s+(?:up|down|left|right)$|^(?:scroll|column|row)\s+(?:left|right|up|down)$",
    re.I,
)
_CAPTION = re.compile(r"^(minimize|maximize|restore)(?:\s+(.*))?$", re.I)

# UI Automation property ids (UIAutomationClient.h).
_P_ENABLED = 30010
_P_HAS_FOCUS = 30008
_P_HAS_EXPAND, _P_EXPAND_STATE = 30028, 30070
_P_HAS_SELECTION_ITEM, _P_IS_SELECTED = 30036, 30079
_P_HAS_TOGGLE, _P_TOGGLE_STATE = 30041, 30086
_P_HAS_VALUE, _P_VALUE = 30043, 30045
_P_HAS_RANGE, _P_RANGE_VALUE = 30033, 30047

_SELECTABLE = {"ListItem", "TabItem", "TreeItem", "RadioButton", "DataItem", "MenuItem"}
_TOGGLABLE = {"CheckBox", "Button", "ListItem", "MenuItem", "TreeItem", "DataItem", "SplitButton"}
_EXPANDABLE = {"ComboBox", "MenuItem", "TreeItem", "Button", "SplitButton", "Group", "Pane", "ListItem", "DataItem"}
_VALUED = {"ComboBox", "Edit", "Spinner", "Custom"}
_P_IS_READONLY = 30046


def _is_noise(role: str, label: str, window_title: str = "") -> bool:
    """Scrollbar parts, and the window's own Minimize/Maximize/Restore buttons ("Minimize"
    or "Minimize <window title>") - not "Restore defaults" or "Minimize to tray"."""
    if role != "Button":
        return False
    label = (label or "").strip()
    if _NOISE.match(label):
        return True
    m = _CAPTION.match(label)
    if not m:
        return False
    rest = (m.group(2) or "").strip().lower()
    return not rest or (bool(window_title) and rest == window_title.strip().lower())


# Invisible direction marks that some apps wrap dates in ("‎November‎ ‎2026"):
# they made labels impossible to type back.
_BIDI = re.compile("[‎‏‪-‮⁦-⁩]")


def _clean(text: str) -> str:
    return _BIDI.sub("", text or "").strip()


_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December"]


def _add_calendar_months(elements: Dict[int, SceneElement], day_ids: List[int], header: str) -> None:
    """A month calendar's day cells are just "1".."31", and the leading/trailing days of the
    neighbouring months repeat numbers. Give each its month ("3 (January 2024)") from the
    header, so a date can be named exactly."""
    m = re.match(r"^\s*([A-Za-z]+)\s+(\d{4})\s*$", header or "")
    if not day_ids or not m or m.group(1).capitalize() not in _MONTHS:
        return
    month, year = _MONTHS.index(m.group(1).capitalize()), int(m.group(2))

    def shift(k: int) -> str:
        mi, yi = (month + k) % 12, year + (month + k) // 12
        return f"{_MONTHS[mi]} {yi}"

    offset = -1
    prev = None
    for i in day_ids:
        e = elements.get(i)
        if e is None or not e.label.isdigit():
            continue
        n = int(e.label)
        if n == 1 and (prev is None or prev > 1):
            offset += 1
        elif prev is None:
            offset = -1 if n > 1 else 0
        prev = n
        e.context = shift(offset)


def _label_from_parent(ctrl: Any) -> Optional[str]:
    """An unnamed button inside a named control ("hours" picker's arrows) is named after it
    and its place there: "hours up" / "hours down" (or "left" / "right")."""
    try:
        parent = ctrl.parent()
        pname = (parent.window_text() or parent.element_info.name or "").strip()
        if not pname or len(pname) > 40:
            return None
        siblings = [c for c in parent.children() if c.element_info.control_type == ctrl.element_info.control_type]
        if len(siblings) != 2:
            return None
        a, b = (s.rectangle() for s in siblings)
        me = ctrl.rectangle()
        vertical = abs(a.mid_point().y - b.mid_point().y) >= abs(a.mid_point().x - b.mid_point().x)
        if vertical:
            first = me.mid_point().y <= max(a.mid_point().y, b.mid_point().y) - 1
            return f"{pname} {'up' if first else 'down'}"
        first = me.mid_point().x <= max(a.mid_point().x, b.mid_point().x) - 1
        return f"{pname} {'left' if first else 'right'}"
    except Exception:
        return None


def _prop(elem: Any, pid: int) -> Any:
    try:
        return elem.GetCurrentPropertyValue(pid)
    except Exception:
        return None


def _control_state(elem: Any, role: str) -> Dict[str, Any]:
    """Toggle/selection/expansion/value/enabled state, read through UIA properties.

    Only properties a role can have are queried, to keep sensing fast.
    """
    out: Dict[str, Any] = {}
    enabled = _prop(elem, _P_ENABLED)
    if enabled is not None:
        out["enabled"] = bool(enabled)
    if role in ("Edit", "Document", "ComboBox") and _prop(elem, _P_HAS_FOCUS):
        out["focus"] = True
    if role in _TOGGLABLE and _prop(elem, _P_HAS_TOGGLE):
        out["toggle"] = {0: "off", 1: "on", 2: "mixed"}.get(_prop(elem, _P_TOGGLE_STATE))
    if role in _SELECTABLE and _prop(elem, _P_HAS_SELECTION_ITEM):
        out["selected"] = bool(_prop(elem, _P_IS_SELECTED))
    if role in _EXPANDABLE and _prop(elem, _P_HAS_EXPAND):
        st = _prop(elem, _P_EXPAND_STATE)
        if st in (0, 1, 2):            # 3 = leaf node: nothing to open
            out["expanded"] = st != 0
    if role in _VALUED and _prop(elem, _P_HAS_VALUE):
        v = _prop(elem, _P_VALUE)
        if v is not None:
            out["value"] = str(v)
        if role == "Custom":
            out["settable"] = not _prop(elem, _P_IS_READONLY)
    elif role in ("Slider", "ProgressBar") and _prop(elem, _P_HAS_RANGE):
        v = _prop(elem, _P_RANGE_VALUE)
        if v is not None:
            out["value"] = f"{float(v):g}"
    return out


def row_partner_text(box: Any, texts: List[Any], exclude: str = "") -> Optional[str]:
    """The title of the control's row: the leftmost, top-most text on that row.

    Rows often have a title and a description line, plus a status word right next to the
    control ("Off"); the title is the one that starts the row.
    """
    cy = (box[1] + box[3]) / 2
    row = [t for t in texts if t[1][1] - 14 <= cy <= t[1][3] + 14 and t[1][2] <= box[0] + 4
           and t[0].strip().lower() != exclude.lower()]
    if row:
        return min(row, key=lambda t: (t[1][0] // 20, t[1][1]))[0]
    above = [(box[1] - t[1][3], t[0]) for t in texts
             if 0 <= box[1] - t[1][3] <= 40 and t[1][0] <= box[2] and box[0] <= t[1][2]
             and t[0].strip().lower() != exclude.lower()]
    return min(above)[1] if above else None


def section_heading(box: Any, texts: List[Any], exclude: str = "", reach: int = 500) -> Optional[str]:
    """The nearest heading above the control whose span covers it ("Plugged in")."""
    cands = [(box[1] - t[1][3], t[0]) for t in texts
             if 0 <= box[1] - t[1][3] <= reach and t[1][0] <= box[0] and t[1][2] >= box[2]
             and t[0].strip().lower() != exclude.lower()]
    return min(cands)[1] if cands else None


def _add_row_context(elements: Dict[int, SceneElement], texts: List[Any]) -> None:
    """Make repeated labels distinguishable: "Show more settings (Taskbar behaviors)",
    "Turn my screen off after (Plugged in)" vs "(On battery)"."""
    from collections import defaultdict
    groups: Dict[str, List[SceneElement]] = defaultdict(list)
    for e in elements.values():
        if e.interactive:
            groups[e.label.lower()].append(e)
    for label, group in groups.items():
        if len(group) < 2:
            continue
        rows = [row_partner_text(e.bbox_screen, texts, exclude=label) for e in group]
        heads = [section_heading(e.bbox_screen, texts, exclude=label) for e in group]
        distinct = lambda xs: all(xs) and len(set(x.lower() for x in xs)) == len(xs)  # noqa: E731
        # A shared section heading that tells them apart ("Plugged in" / "On battery") is the
        # clearest name; otherwise the row title ("Taskbar behaviors").
        if distinct(heads):
            chosen = heads
        elif distinct(rows):
            chosen = rows
        else:
            chosen = [", ".join(x for x in (h, r) if x) or None for r, h in zip(rows, heads)]
        for e, ctx in zip(group, chosen):
            if ctx:
                e.context = ctx[:80]


def _fold_dropdown_selection(elements: Dict[int, SceneElement]) -> None:
    """A collapsed dropdown shows its choice as a separate selected item inside it: make that
    the dropdown's value ("= 2 hours") and drop the duplicate item."""
    drop: List[int] = []
    for c in elements.values():
        if c.role != "ComboBox" or (c.value and c.value.strip().lower() != c.label.strip().lower()):
            continue
        l, t, r, b = c.bbox_screen
        inside = [e for e in elements.values() if e.role == "ListItem" and e.selected
                  and l <= e.bbox_screen[0] and e.bbox_screen[2] <= r and t <= e.bbox_screen[1] and e.bbox_screen[3] <= b]
        if len(inside) == 1:
            c.value = inside[0].label
            drop.append(inside[0].element_id)
    for i in drop:
        elements.pop(i, None)


def _open_popups(hwnd: int) -> List[int]:
    """Visible menus/flyouts/dropdown lists that belong to the window's app (see is_popup_of)."""
    import ctypes
    from ctypes import wintypes
    from servo.actuation.sandboxing import is_popup_of
    from servo.perception import windows
    root = windows.get_root_window(hwnd)
    found: List[int] = []
    user32 = ctypes.windll.user32

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(h, _l):  # type: ignore[no-untyped-def]
        h = int(h or 0)
        if h and h != root and user32.IsWindowVisible(h) and is_popup_of(h, root):
            r = wintypes.RECT()
            user32.GetWindowRect(h, ctypes.byref(r))
            if r.right - r.left > 20 and r.bottom - r.top > 20:
                found.append(h)
        return True

    user32.EnumWindows(_cb, 0)
    return found[:3]


def _inside_control(box: Any, elements: Dict[int, SceneElement]) -> bool:
    """Text drawn inside a control is that control's caption, not separate content."""
    for e in elements.values():
        l, t, r, b = e.bbox_screen
        if l <= box[0] and t <= box[1] and box[2] <= r and box[3] <= b and e.role not in ("Group", "Pane"):
            return True
    return False

# A browser-hosted window with fewer interactive controls than this is probably showing
# only its frame (back/refresh/close) because page accessibility is still off.
_CHROMIUM_EMPTY = 15
_nudged: Set[int] = set()


def _is_chromium_host(hwnd: int) -> bool:
    try:
        from servo.perception import windows
        from servo.perception.app_identity import BROWSER_PROCESSES
        proc = windows.get_process_name(hwnd)
        return proc in BROWSER_PROCESSES and proc != "firefox.exe"
    except Exception:
        return False


def _request_accessibility(hwnd: int) -> bool:
    """Ask for the window's MSAA client object, as screen readers do.

    Chromium treats this request as "an assistive tool is running" and builds the
    page's accessibility tree, which UI Automation then exposes.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        import comtypes
        iid = comtypes.GUID("{618736E0-3C3D-11CF-810C-00AA00389B71}")  # IID_IAccessible
        ptr = ctypes.c_void_p()
        ctypes.oledll.oleacc.AccessibleObjectFromWindow(
            hwnd, ctypes.c_long(-4), ctypes.byref(iid), ctypes.byref(ptr))  # OBJID_CLIENT
        if ptr.value:
            # Release the reference we were handed (IUnknown::Release is vtable slot 2).
            vtbl = ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0],
                               ctypes.POINTER(ctypes.c_void_p))
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(ptr)
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("Accessibility request failed for HWND %s: %s", hwnd, e)
        return False

# Whitelisted interactive control roles for UIA filtering
INTERACTIVE_CONTROL_TYPES = {
    "Button",
    "Edit",
    "CheckBox",
    "RadioButton",
    "ComboBox",
    "ListItem",
    "MenuItem",
    "TabItem",
    "Hyperlink",
    "Document",
    "SplitButton",
    "TreeItem",
    "Slider",
    "Spinner",
    "DataItem",
}


class UIASensor:
    """Extracts interactive controls from target window via Windows UI Automation."""

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self.viewport = ViewportTransform(hwnd)

    def sense(self) -> SceneIndex:
        """Query accessibility tree and return normalized SceneIndex.

        Chromium (Edge, Chrome, installed web apps such as Facebook) keeps a page's
        accessibility tree switched off until it believes an assistive tool is asking.
        Until then only the window frame is visible (7 controls for a full Facebook
        feed). If a browser-hosted window looks empty, ask once the way screen readers
        do, then sense again.
        """
        try:
            from servo.perception import windows as _w
            if sys.platform == "win32" and not _w.is_window(self.hwnd):
                return SceneIndex(hwnd=self.hwnd, window_title="", source="uia", sense_error=WINDOW_CLOSED)
        except Exception:
            pass
        scene = self._with_open_popups(self._sense_once())
        page_hidden = len(scene.elements) < _CHROMIUM_EMPTY or not any(
            e.role == "Document" for e in scene.elements.values())
        if page_hidden and self.hwnd not in _nudged and _is_chromium_host(self.hwnd):
            _nudged.add(self.hwnd)
            if _request_accessibility(self.hwnd):
                for _ in range(4):
                    time.sleep(0.35)
                    again = self._sense_once()
                    if len(again.elements) > len(scene.elements):
                        logger.info("Chromium page accessibility enabled for HWND %s (%d -> %d controls)",
                                    self.hwnd, len(scene.elements), len(again.elements))
                        return again
        return scene

    def _with_open_popups(self, scene: SceneIndex) -> SceneIndex:
        """Add the app's open menus, dropdown lists and flyouts to the scene, first.

        They are separate top-level windows, so after "click View" the menu's items were
        invisible to the planner (it clicked View again) and to the resolver.
        """
        if scene.sense_error or sys.platform != "win32":
            return scene
        try:
            popups = _open_popups(self.hwnd)
        except Exception:
            return scene
        if not popups:
            return scene
        merged: Dict[int, SceneElement] = {}
        live: Dict[int, Any] = {}
        nid = 0
        sources = [UIASensor(p)._sense_once() for p in popups] + [scene]
        for src in sources:
            if src.sense_error:
                continue
            for e in src.elements.values():
                merged[nid] = e.model_copy(update={"element_id": nid})
                handle = src.live(e.element_id)
                if handle is not None:
                    live[nid] = handle
                nid += 1
        out = scene.model_copy(update={"elements": merged})
        out._live.clear()
        out._live.update(live)
        return out

    def _sense_once(self) -> SceneIndex:
        self.viewport.refresh()
        elements_map = {}
        window_title = "Unknown Window"
        sense_error: Optional[str] = None

        if sys.platform != "win32":
            return SceneIndex(
                hwnd=self.hwnd,
                window_title="Non-Windows Stub",
                source="uia",
                viewport={"x": 0, "y": 0, "width": 800, "height": 600},
                elements={},
            )

        try:
            from pywinauto import Application
            app = Application(backend="uia").connect(handle=self.hwnd)
            win = app.window(handle=self.hwnd)
            window_title = win.window_text() or f"HWND:{self.hwnd}"

            # Query descendants with safety filter
            element_id = 0
            descendants = win.descendants()

            live: Dict[int, Any] = {}
            texts: List[Any] = []
            calendar_days: List[int] = []
            calendar_header = ""
            for ctrl in descendants:
                try:
                    role = ctrl.element_info.control_type or "Unknown"
                    is_text = role in READABLE_TEXT_TYPES
                    if role not in INTERACTIVE_CONTROL_TYPES and role not in MAYBE_INTERACTIVE_TYPES and not is_text:
                        continue

                    # Filter visible controls with positive dimensions
                    if not ctrl.is_visible():
                        continue

                    rect = ctrl.rectangle()
                    width = rect.width()
                    height = rect.height()
                    if width <= 0 or height <= 0:
                        continue

                    label = _clean(ctrl.window_text() or ctrl.element_info.name or "")
                    if is_text:
                        if label:
                            texts.append((label, (rect.left, rect.top, rect.right, rect.bottom), ctrl))
                        continue
                    if _is_noise(role, label, window_title):
                        continue
                    state = _control_state(ctrl.element_info.element, role)
                    # Groups/panes only count when they open and close (Settings sections);
                    # a custom control only when it is a named value you can set.
                    if role == "Custom":
                        if not (label and state.get("settable")):
                            continue
                    elif role in MAYBE_INTERACTIVE_TYPES and state.get("expanded") is None:
                        continue
                    # If label is empty, name it after its labelled parent ("hours up"), then
                    # its automation id.
                    if not label:
                        label = (_label_from_parent(ctrl) or ctrl.element_info.automation_id
                                 or f"{role}_{element_id}").strip()

                    screen_bbox = (rect.left, rect.top, rect.right, rect.bottom)
                    norm_bbox = self.viewport.bbox_screen_to_norm(screen_bbox)
                    center_screen = ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)

                    value: Optional[str] = state.get("value")
                    if value is None and role in ("Edit", "Document", "ComboBox"):
                        try:
                            value = ctrl.iface_value.CurrentValue if hasattr(ctrl, "iface_value") else ctrl.window_text()
                        except Exception:
                            value = ctrl.window_text()
                    if value is not None:
                        value = _clean(value) if role != "Document" else value
                    try:
                        cls = ctrl.element_info.class_name or ""
                    except Exception:
                        cls = ""
                    if cls == "CalendarViewDayItem":
                        calendar_days.append(element_id)
                    elif role == "Button" and (ctrl.element_info.automation_id or "") == "HeaderButton":
                        calendar_header = label

                    is_focusable = False
                    try:
                        is_focusable = bool(ctrl.element_info.element.CurrentIsKeyboardFocusable)
                    except Exception:
                        pass

                    elements_map[element_id] = SceneElement(
                        element_id=element_id,
                        label=label,
                        role=role,
                        bbox_screen=screen_bbox,
                        bbox_norm=norm_bbox,
                        center_screen=center_screen,
                        is_keyboard_focusable=is_focusable,
                        value=value,
                        toggle=state.get("toggle"),
                        selected=state.get("selected"),
                        expanded=state.get("expanded"),
                        enabled=state.get("enabled", True),
                        has_focus=bool(state.get("focus")),
                    )
                    live[element_id] = ctrl
                    element_id += 1
                except Exception:
                    continue

            _fold_dropdown_selection(elements_map)
            _add_row_context(elements_map, texts)
            _add_calendar_months(elements_map, calendar_days, calendar_header)
            # Plain text last, minus what already labels a control (a button's caption).
            labels = {e.label.lower() for e in elements_map.values()}
            kept = 0
            for text, box, ctrl in texts:
                if kept >= MAX_TEXT_ITEMS or text.lower() in labels or _inside_control(box, elements_map):
                    continue
                labels.add(text.lower())
                elements_map[element_id] = SceneElement(
                    element_id=element_id, label=text[:300], role="Text", bbox_screen=box,
                    bbox_norm=self.viewport.bbox_screen_to_norm(box),
                    center_screen=((box[0] + box[2]) // 2, (box[1] + box[3]) // 2), interactive=False,
                )
                live[element_id] = ctrl
                element_id += 1
                kept += 1

        except Exception as e:
            # Previously the error text was written into window_title, which made the
            # next diff report "title changed" and the post-action gate count a COM
            # failure as a successful UI transition. Flag it explicitly instead.
            logger.warning("UIA sensing failed for HWND %s: %s", self.hwnd, e)
            sense_error = str(e) or e.__class__.__name__

        b = self.viewport.bounds
        scene = SceneIndex(
            hwnd=self.hwnd,
            window_title=window_title,
            source="uia",
            viewport={
                "x": b.left,
                "y": b.top,
                "width": b.width,
                "height": b.height,
                "client_width": b.client_width,
                "client_height": b.client_height,
            },
            elements=elements_map,
            sense_error=sense_error,
        )
        if sense_error is None:
            scene._live.update(live)
        return scene
