"""General fixes from the first WindowsAgentArena smoke run (none of them benchmark-specific)."""

import pytest

from servo.actuation.alias_mapping import AliasMapper, CanonicalActionType as A
from servo.actuation.patterns import _same_value
from servo.perception.scene_index import SceneElement, SceneIndex
from servo.perception.uia_sensor import _add_calendar_months, _clean


def el(i, role, label, box=(0, 0, 10, 10), **kw):
    return SceneElement(element_id=i, role=role, label=label, bbox_screen=box, bbox_norm=(0, 0, 1, 1),
                        center_screen=((box[0] + box[2]) // 2, (box[1] + box[3]) // 2), **kw)


def scene(*els):
    return SceneIndex(hwnd=1, window_title="W", source="uia", elements={e.element_id: e for e in els})


# ------------------------------------------------- "TabItem: Details" as a target

def test_target_copied_with_its_role_prefix_resolves():
    s = scene(el(1, "TabItem", "General"), el(2, "TabItem", "Details"), el(3, "Text", "Details", (0, 50, 10, 60), interactive=False))
    got, err = AliasMapper.resolve_element(s, "TabItem: Details", A.CLICK)
    assert err is None and got.element_id == 2
    got, _ = AliasMapper.resolve_element(s, "Button: Details", A.CLICK)  # wrong role: still the label
    assert got is not None and got.label == "Details"


def test_a_label_with_a_colon_is_not_mistaken_for_a_role():
    s = scene(el(1, "Button", "Note: read me"))
    got, err = AliasMapper.resolve_element(s, "Note: read me", A.CLICK)
    assert got is not None and got.element_id == 1


# ------------------------------------------------- custom value controls (a timer's hours picker)

def test_a_named_custom_value_control_takes_set_value():
    s = scene(el(1, "Custom", "hours", value="00"), el(2, "Button", "hours up"), el(3, "Edit", "Timer name"))
    got, err = AliasMapper.resolve_element(s, "hours", A.SET_VALUE)
    assert err is None and got.element_id == 1


@pytest.mark.parametrize("got,want,same", [("03", "3", True), ("3", "3", True), ("40.0", "40", True),
                                           ("abc", "abc", True), ("03", "4", False), ("x", "y", False)])
def test_values_compare_as_numbers_when_they_are_numbers(got, want, same):
    assert _same_value(got, want) is same


# ------------------------------------------------- calendars and invisible marks

def test_direction_marks_are_stripped_from_labels():
    assert _clean("‎November‎ ‎23‎, ‎2026") == "November 23, 2026"


def test_calendar_days_get_their_month():
    labels = ["29", "30", "31", "1", "2", "3"] + [str(d) for d in range(4, 31)] + ["1", "2", "3"]
    els = {i: el(i, "DataItem", lab) for i, lab in enumerate(labels)}
    _add_calendar_months(els, list(els), "November 2026")
    ctx = [(e.label, e.context) for e in els.values()]
    assert ctx[0] == ("29", "October 2026")
    assert ctx[5] == ("3", "November 2026")
    assert ctx[-1] == ("3", "December 2026")
    s = scene(*els.values())
    got, err = AliasMapper.resolve_element(s, "3 (November 2026)", A.CLICK)
    assert err is None and got.element_id == 5


def test_calendar_month_rolls_over_the_year():
    els = {0: el(0, "DataItem", "31"), 1: el(1, "DataItem", "1")}
    _add_calendar_months(els, [0, 1], "January 2024")
    assert els[0].context == "December 2023" and els[1].context == "January 2024"
