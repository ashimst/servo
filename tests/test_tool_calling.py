"""Tests for Structured Tool Calling and Macro Plan Generation."""

import json
import pytest

from servo.planner.tool_calling import (
    SERVO_TOOL_DEFINITIONS,
    MacroPlan,
    build_tool_planning_prompt,
    parse_deterministic_intent,
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


def test_parse_llm_tool_plan_trailing_commas():
    raw_trailing = """
    [
      {"step_index": 1, "tool_name": "launch_app", "arguments": {"app_name": "notepad",},},
    ]
    """
    plan = parse_llm_tool_plan(raw_trailing, goal="open notepad")
    assert len(plan.steps) == 1
    assert plan.steps[0].tool_name == "launch_app"
    assert plan.steps[0].canonical_action.action_type == CanonicalActionType.LAUNCH
    assert plan.steps[0].canonical_action.app_name == "notepad"


def test_parse_llm_tool_plan_single_quotes_and_booleans():
    raw_sq = "{'steps': [{'step_index': 1, 'tool_name': 'type_text', 'arguments': {'text': 'hello', 'clear_first': True}}]}"
    plan = parse_llm_tool_plan(raw_sq)
    assert len(plan.steps) == 1
    assert plan.steps[0].canonical_action.action_type == CanonicalActionType.TYPE
    assert plan.steps[0].canonical_action.text == "hello"


def test_parse_llm_tool_plan_conversational_preamble_postamble():
    raw = """
    Sure thing! Here is the decomposition for your desktop request:
    ```json
    [
      {"step_index": 1, "tool_name": "launch_app", "arguments": {"app_name": "calculator"}},
      {"step_index": 2, "tool_name": "click_element", "arguments": {"target": "7"}}
    ]
    ```
    Hope this helps automate your task! Let me know if you need changes.
    """
    plan = parse_llm_tool_plan(raw)
    assert len(plan.steps) == 2
    assert plan.steps[0].canonical_action.action_type == CanonicalActionType.LAUNCH
    assert plan.steps[1].canonical_action.action_type == CanonicalActionType.CLICK
    assert plan.steps[1].canonical_action.target == "7"


def test_parse_llm_tool_plan_flat_arguments():
    raw_flat = json.dumps([
        {"step_index": 1, "tool_name": "type_text", "text": "my note", "target": "editor"},
        {"step_index": 2, "tool_name": "click_element", "target": "Save", "button": "left"}
    ])
    plan = parse_llm_tool_plan(raw_flat)
    assert len(plan.steps) == 2
    assert plan.steps[0].arguments["text"] == "my note"
    assert plan.steps[0].canonical_action.action_type == CanonicalActionType.TYPE
    assert plan.steps[0].canonical_action.text == "my note"
    assert plan.steps[1].arguments["target"] == "Save"
    assert plan.steps[1].canonical_action.action_type == CanonicalActionType.CLICK
    assert plan.steps[1].canonical_action.target == "Save"


def test_parse_llm_tool_plan_root_structure_variations():
    # sub_goals root
    p1 = parse_llm_tool_plan(json.dumps({"sub_goals": [{"tool_name": "click_element", "target": "OK"}]}))
    assert len(p1.steps) == 1

    # plan root
    p2 = parse_llm_tool_plan(json.dumps({"plan": [{"tool_name": "close_window", "app_name": "notepad"}]}))
    assert len(p2.steps) == 1

    # actions root
    p3 = parse_llm_tool_plan(json.dumps({"actions": [{"tool_name": "send_hotkey", "keys": ["ctrl", "s"]}]}))
    assert len(p3.steps) == 1

    # single step dict
    p4 = parse_llm_tool_plan(json.dumps({"tool_name": "wait", "duration_seconds": 1.0}))
    assert len(p4.steps) == 1
    assert p4.steps[0].canonical_action.action_type == CanonicalActionType.WAIT


def test_parse_llm_tool_plan_heuristic_regex_fallback():
    raw_pseudo = "I suggest: launch_app(app_name='notepad') and then type_text(text='hello world')"
    plan = parse_llm_tool_plan(raw_pseudo)
    assert len(plan.steps) == 2
    assert plan.steps[0].tool_name == "launch_app"
    assert plan.steps[0].canonical_action.action_type == CanonicalActionType.LAUNCH
    assert plan.steps[1].tool_name == "type_text"
    assert plan.steps[1].canonical_action.text == "hello world"


def test_parse_llm_tool_plan_deterministic_intent_fallback():
    raw_garbage = "I'm sorry, I am unable to generate a plan for you."
    plan = parse_llm_tool_plan(raw_garbage, goal="open notepad and write hello world")
    assert len(plan.steps) == 2
    assert plan.steps[0].canonical_action.action_type == CanonicalActionType.LAUNCH
    assert plan.steps[0].canonical_action.app_name == "notepad"
    assert plan.steps[1].canonical_action.action_type == CanonicalActionType.TYPE
    assert plan.steps[1].canonical_action.text == "hello world"


def test_plan_step_and_macro_plan_to_subgoals():
    raw_json = json.dumps([
        {"step_index": 1, "tool_name": "launch_app", "arguments": {"app_name": "notepad"}, "rationale": "Open"},
        {"step_index": 2, "tool_name": "type_text", "arguments": {"text": "hello", "target": "editor"}, "rationale": "Type"},
    ])
    macro_plan = parse_llm_tool_plan(raw_json, goal="test goal")
    subgoals = macro_plan.to_subgoals()
    assert len(subgoals) == 2

    # Step 1
    assert subgoals[0].step_index == 1
    assert subgoals[0].action_type == "launch"
    assert subgoals[0].target_element_hint == "notepad"
    assert subgoals[0].tool_name == "launch_app"
    assert subgoals[0].arguments == {"app_name": "notepad"}

    # Step 2
    assert subgoals[1].step_index == 2
    assert subgoals[1].action_type == "type"
    assert subgoals[1].text_to_type == "hello"
    assert subgoals[1].tool_name == "type_text"


def test_macro_plan_dictionary_access():
    raw_json = json.dumps([
        {"step_index": 1, "tool_name": "launch_app", "arguments": {"app_name": "notepad"}},
        {"step_index": 2, "tool_name": "type_text", "arguments": {"text": "hello", "target": "editor"}}
    ])
    plan = parse_llm_tool_plan(raw_json, goal="launch notepad")

    # Dict access on plan
    assert plan["type"] == "launch"
    assert plan["app"] == "notepad"
    assert plan["app_name"] == "notepad"
    assert len(plan["steps"]) == 2
    assert len(plan["actions"]) == 2
    assert len(plan["sub_goals"]) == 2
    assert "type" in plan
    assert "app" in plan
    assert plan.get("type") == "launch"
    assert plan.get("nonexistent", "fallback") == "fallback"

    # Dict access on individual step
    step0 = plan.steps[0]
    assert step0["type"] == "launch"
    assert step0["app"] == "notepad"
    assert step0["app_name"] == "notepad"

    step1 = plan.steps[1]
    assert step1["type"] == "type"
    assert step1["text"] == "hello"
    assert step1["target"] == "editor"
    assert step1.get("text") == "hello"
    assert "target" in step1

    with pytest.raises(KeyError):
        _ = plan["completely_missing_key_xyz"]


def test_parse_deterministic_intent_hotkey_vs_click_disambiguation():
    # 1. "press ctrl+s" -> send_hotkey with keys=["ctrl", "s"]
    p_ctrl_s = parse_deterministic_intent("press ctrl+s")
    assert p_ctrl_s is not None
    assert len(p_ctrl_s.steps) == 1
    assert p_ctrl_s.steps[0].tool_name == "send_hotkey"
    assert p_ctrl_s.steps[0].arguments["keys"] == ["ctrl", "s"]
    assert p_ctrl_s.steps[0].canonical_action.action_type == CanonicalActionType.HOTKEY
    assert p_ctrl_s.steps[0].canonical_action.keys == ["ctrl", "s"]

    # 2. "press enter" -> send_hotkey with keys=["enter"]
    p_enter = parse_deterministic_intent("press enter")
    assert p_enter is not None
    assert len(p_enter.steps) == 1
    assert p_enter.steps[0].tool_name == "send_hotkey"
    assert p_enter.steps[0].arguments["keys"] == ["enter"]
    assert p_enter.steps[0].canonical_action.action_type == CanonicalActionType.HOTKEY
    assert p_enter.steps[0].canonical_action.keys == ["enter"]

    # 3. "press alt+f4" -> send_hotkey with keys=["alt", "f4"]
    p_alt_f4 = parse_deterministic_intent("press alt+f4")
    assert p_alt_f4 is not None
    assert len(p_alt_f4.steps) == 1
    assert p_alt_f4.steps[0].tool_name == "send_hotkey"
    assert p_alt_f4.steps[0].arguments["keys"] == ["alt", "f4"]
    assert p_alt_f4.steps[0].canonical_action.action_type == CanonicalActionType.HOTKEY
    assert p_alt_f4.steps[0].canonical_action.keys == ["alt", "f4"]

    # 4. "press Submit Button" -> click_element with target="Submit Button"
    p_submit = parse_deterministic_intent("press Submit Button")
    assert p_submit is not None
    assert len(p_submit.steps) == 1
    assert p_submit.steps[0].tool_name == "click_element"
    assert p_submit.steps[0].arguments["target"] == "Submit Button"
    assert p_submit.steps[0].canonical_action.action_type == CanonicalActionType.CLICK
    assert p_submit.steps[0].canonical_action.target == "Submit Button"

    # 5. "click Save" -> click_element with target="Save"
    p_save = parse_deterministic_intent("click Save")
    assert p_save is not None
    assert len(p_save.steps) == 1
    assert p_save.steps[0].tool_name == "click_element"
    assert p_save.steps[0].arguments["target"] == "Save"

    # 6. "press the enter key" -> send_hotkey with keys=["enter"]
    p_enter_key = parse_deterministic_intent("press the enter key")
    assert p_enter_key is not None
    assert p_enter_key.steps[0].tool_name == "send_hotkey"
    assert p_enter_key.steps[0].arguments["keys"] == ["enter"]


def test_parse_llm_tool_plan_multi_fence_prioritizes_json():
    raw_multi = """
    Thinking:
    ```text
    The user wants to open notepad and click the File menu.
    ```
    Plan:
    ```json
    [
      {"step_index": 1, "tool_name": "launch_app", "arguments": {"app_name": "notepad"}},
      {"step_index": 2, "tool_name": "click_element", "arguments": {"target": "File"}}
    ]
    ```
    """
    plan = parse_llm_tool_plan(raw_multi)
    assert len(plan.steps) == 2
    assert plan.steps[0].tool_name == "launch_app"
    assert plan.steps[0].arguments["app_name"] == "notepad"
    assert plan.steps[0].canonical_action.action_type == CanonicalActionType.LAUNCH
    assert plan.steps[1].tool_name == "click_element"
    assert plan.steps[1].arguments["target"] == "File"
    assert plan.steps[1].canonical_action.action_type == CanonicalActionType.CLICK


def test_parse_llm_tool_plan_unquoted_keys():
    # Array of objects with unquoted keys
    raw_unquoted = """
    [
      {step_index: 1, tool_name: "launch_app", arguments: {app_name: "notepad"}},
      {step_index: 2, tool_name: "type_text", arguments: {text: "hello", target: "editor"}}
    ]
    """
    plan = parse_llm_tool_plan(raw_unquoted)
    assert len(plan.steps) == 2
    assert plan.steps[0].tool_name == "launch_app"
    assert plan.steps[0].arguments["app_name"] == "notepad"
    assert plan.steps[1].tool_name == "type_text"
    assert plan.steps[1].arguments["text"] == "hello"

    # Single-object with unquoted keys and single quotes
    raw_single = "{tool_name: 'click_element', target: 'Submit'}"
    plan_single = parse_llm_tool_plan(raw_single)
    assert len(plan_single.steps) == 1
    assert plan_single.steps[0].tool_name == "click_element"
    assert plan_single.steps[0].arguments["target"] == "Submit"


def test_parse_deterministic_intent_compound_trailing_enter():
    # 1. "open notepad and write hello world and press enter"
    p1 = parse_deterministic_intent("open notepad and write hello world and press enter")
    assert p1 is not None
    assert len(p1.steps) == 2
    assert p1.steps[0].tool_name == "launch_app"
    assert p1.steps[0].arguments["app_name"] == "notepad"
    assert p1.steps[1].tool_name == "type_text"
    assert p1.steps[1].arguments["text"] == "hello world"
    assert p1.steps[1].arguments.get("press_enter") is True
    assert p1.steps[1].canonical_action.press_enter is True
    assert p1.steps[1]["press_enter"] is True

    # Variations with hit enter and send the enter key
    p1_hit = parse_deterministic_intent("open notepad and type hello world and hit enter")
    assert p1_hit is not None
    assert p1_hit.steps[1].arguments["text"] == "hello world"
    assert p1_hit.steps[1].arguments.get("press_enter") is True

    p1_send = parse_deterministic_intent("open notepad and write 'hello world' and send the enter key")
    assert p1_send is not None
    assert p1_send.steps[1].arguments["text"] == "hello world"
    assert p1_send.steps[1].arguments.get("press_enter") is True


def test_parse_deterministic_intent_type_only_trailing_enter():
    # 1. "write hello world and press enter"
    p2 = parse_deterministic_intent("write hello world and press enter")
    assert p2 is not None
    assert len(p2.steps) == 1
    assert p2.steps[0].tool_name == "type_text"
    assert p2.steps[0].arguments["text"] == "hello world"
    assert p2.steps[0].arguments.get("press_enter") is True
    assert p2.steps[0].canonical_action.press_enter is True
    assert p2.steps[0]["press_enter"] is True

    # Variations
    p2_type = parse_deterministic_intent("type test note and hit enter")
    assert p2_type is not None
    assert p2_type.steps[0].arguments["text"] == "test note"
    assert p2_type.steps[0].arguments.get("press_enter") is True

    p2_into = parse_deterministic_intent("write hello into editor and press enter")
    assert p2_into is not None
    assert p2_into.steps[0].arguments["text"] == "hello"
    assert p2_into.steps[0].arguments["target"] == "editor"
    assert p2_into.steps[0].arguments.get("press_enter") is True


def test_parse_llm_tool_plan_unquoted_keys_with_colons_commas_braces_in_strings():
    # String literal containing colons and commas: "Time: 12:00, Status: OK"
    raw_colons_commas = '[{tool_name: "type_text", arguments: {text: "Time: 12:00, Status: OK", target: "editor"}}]'
    p1 = parse_llm_tool_plan(raw_colons_commas)
    assert len(p1.steps) == 1
    assert p1.steps[0].tool_name == "type_text"
    assert p1.steps[0].arguments["text"] == "Time: 12:00, Status: OK"
    assert p1.steps[0].canonical_action.text == "Time: 12:00, Status: OK"

    # String literal containing braces and colons: "prefix {note: urgent} suffix"
    raw_braces = '[{tool_name: "type_text", arguments: {text: "prefix {note: urgent} suffix", target: "editor"}}]'
    p2 = parse_llm_tool_plan(raw_braces)
    assert len(p2.steps) == 1
    assert p2.steps[0].tool_name == "type_text"
    assert p2.steps[0].arguments["text"] == "prefix {note: urgent} suffix"

    # Single-quoted string literal containing colons and commas
    raw_sq = "[{tool_name: 'type_text', arguments: {text: 'Time: 12:00, Status: OK', target: 'editor'}}]"
    p3 = parse_llm_tool_plan(raw_sq)
    assert len(p3.steps) == 1
    assert p3.steps[0].tool_name == "type_text"
    assert p3.steps[0].arguments["text"] == "Time: 12:00, Status: OK"



