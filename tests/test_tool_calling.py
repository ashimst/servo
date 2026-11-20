"""Tests for Structured Tool Calling and Macro Plan Generation."""

import json
import pytest

from servo.planner.tool_calling import (
    SERVO_TOOL_DEFINITIONS,
    MacroPlan,
    build_tool_planning_prompt,
    parse_llm_tool_plan,
)
from servo.actuation.alias_mapping import CanonicalActionType
from servo.perception.scene_index import SceneElement, SceneIndex


def test_tool_definitions_schema_valid():
    assert len(SERVO_TOOL_DEFINITIONS) >= 6
    names = {t["name"] for t in SERVO_TOOL_DEFINITIONS}
    assert "launch_app" in names
    assert "click_element" in names
    assert "type_text" in names
    assert "send_hotkey" in names
    assert "wait" in names
    assert "close_window" in names


def test_build_tool_planning_prompt():
    elements = {
        0: SceneElement(
            element_id=0,
            label="File",
            role="MenuItem",
            bbox_screen=(0, 0, 10, 10),
            bbox_norm=(0, 0, 10, 10),
            center_screen=(5, 5),
        )
    }
    scene = SceneIndex(hwnd=555, window_title="Notepad", source="uia", elements=elements)
    prompt = build_tool_planning_prompt("save file", scene=scene, user_name="Ashim")
    assert "Ashim" in prompt
    assert "Notepad" in prompt
    assert "File" in prompt
    assert "save file" in prompt


def test_parse_llm_tool_plan_clean_json():
    raw_json = json.dumps([
        {"step_index": 1, "tool_name": "launch_app", "arguments": {"app_name": "notepad"}, "rationale": "Open app"},
        {"step_index": 2, "tool_name": "type_text", "arguments": {"text": "hello", "target": "editor"}, "rationale": "Write note"}
    ])
    plan = parse_llm_tool_plan(raw_json, goal="test goal")
    assert len(plan.steps) == 2
    assert plan.steps[0].tool_name == "launch_app"
    assert plan.steps[0].canonical_action.action_type == CanonicalActionType.LAUNCH
    assert plan.steps[1].canonical_action.action_type == CanonicalActionType.TYPE
    assert plan.steps[1].canonical_action.text == "hello"


def test_parse_llm_tool_plan_markdown_fence():
    fenced = """Here is your plan:
```json
[
  {"step_index": 1, "tool_name": "click_element", "arguments": {"target": "Save"}}
]
```
Done!"""
    plan = parse_llm_tool_plan(fenced)
    assert len(plan.steps) == 1
    assert plan.steps[0].canonical_action.action_type == CanonicalActionType.CLICK
    assert plan.steps[0].canonical_action.target == "Save"


def test_parse_llm_tool_plan_object_with_steps():
    raw_obj = json.dumps({
        "steps": [
            {"step_index": 1, "tool_name": "send_hotkey", "arguments": {"keys": ["ctrl", "s"]}}
        ]
    })
    plan = parse_llm_tool_plan(raw_obj)
    assert len(plan.steps) == 1
    assert plan.steps[0].canonical_action.action_type == CanonicalActionType.HOTKEY
    assert plan.steps[0].canonical_action.keys == ["ctrl", "s"]


def test_parse_llm_tool_plan_empty():
    plan = parse_llm_tool_plan("")
    assert len(plan.steps) == 0
