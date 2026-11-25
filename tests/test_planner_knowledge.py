"""Planner inputs: recipes, redaction, media keys, screen description, prompt rules."""

import pytest

from servo.cognition.recipes import RecipeBook, parse_recipe
from servo.cognition.redact import redact_secrets


# ---------------------------------------------------------------- recipes

@pytest.mark.parametrize("goal,app,expected", [
    ("open dia and play kanye in youtube", "", "youtube"),
    ("increase the brightness", "", "brightness"),
    ("check the battery percentage of the device", "", "battery_system_info"),
    ("look up Ashim Shrestha in gemini and write the response to notepad", "", "web_research_to_notes"),
    ("the typing is not complete", "notepad.exe", "notepad"),
    ("email: a@b.c password: [redacted] login", "", "sign_in_and_secrets"),
    ("in vs code open a folder named noesis", "", "files_and_folders"),
])
def test_recipe_selection(goal, app, expected):
    ids = [r.id for r, _, _ in RecipeBook().select(goal, app)]
    assert expected in ids, ids


def test_every_recipe_parses_and_uses_known_commands():
    book = RecipeBook()
    recipes = book.recipes()
    assert len(recipes) >= 10
    for r in recipes:
        assert r.triggers, r.id
        assert "powershell -command" not in r.body.lower().replace("never wrap them in `powershell -command`", "") \
            .replace("never wrap it in `powershell -command`", ""), r.id


def test_recipes_reload_on_change(tmp_path):
    (tmp_path / "a.md").write_text("---\nid: a\ntitle: A\ntriggers: [alpha]\n---\nstep\n", encoding="utf-8")
    book = RecipeBook(tmp_path)
    assert [r.id for r, _, _ in book.select("alpha")] == ["a"]
    (tmp_path / "b.md").write_text("---\nid: b\ntitle: B\ntriggers: [beta]\n---\nstep\n", encoding="utf-8")
    assert [r.id for r, _, _ in book.select("beta")] == ["b"]


def test_trigger_matches_whole_words_only():
    r = parse_recipe("---\nid: v\ntitle: V\ntriggers: [pin]\n---\nx\n")
    book = RecipeBook.__new__(RecipeBook)
    book.recipes = lambda: [r]  # type: ignore[assignment]
    assert book.select("spinning wheel") == []
    assert book.select("enter the pin") != []


# -------------------------------------------------------------- redaction

@pytest.mark.parametrize("text,secret", [
    ("email : someone@gmail.com\npassword : Hunter2_xyz\nlogin", "Hunter2_xyz"),
    ("my password is s3cretpass", "s3cretpass"),
    ("pay with 4111 1111 1111 1111", "4111 1111 1111 1111"),
    ("use key sk-ant-api03-abcdefghijklmnopqrstuv", "sk-ant-api03-abcdefghijklmnopqrstuv"),
    ("pin: 4821", "4821"),
])
def test_redacts_secrets(text, secret):
    out, found = redact_secrets(text)
    assert secret not in out and "[redacted]" in out and found


@pytest.mark.parametrize("text", ["open the password manager", "reset password for my account",
                                  "call 9841234567", "the token count is high", "type hello"])
def test_leaves_ordinary_text(text):
    assert redact_secrets(text) == (text, [])


def test_router_redacts_before_anything_sees_it(monkeypatch, laya):
    from servo.cognition.router import CascadeRouter
    seen = []

    class Mem:
        def add_turn(self, role, text, **k): seen.append(text)

    events = []

    class T:
        run_id = "r"
        def emit(self, kind, title, detail="", **k):
            events.append((kind, title, detail))
            return type("E", (), {"seq": 0})()
        def span(self, *a, **k):
            import contextlib
            @contextlib.contextmanager
            def cm():
                yield type("S", (), {"done": lambda *a, **k: None})()
            return cm()

    from servo.control.jev_gate import JevGate
    r = CascadeRouter(tracer=T(), laya=JevGate(), memory=Mem())
    monkeypatch.setattr(r.system2, "available", lambda: False)
    r.run("login with password: Hunter2_xyz")
    assert all("Hunter2_xyz" not in t for t in seen)
    assert all("Hunter2_xyz" not in f"{t} {d}" for _, t, d in events)
    assert any("Secret removed" in t for _, t, _ in events)
    assert laya.calls and all("Hunter2_xyz" not in str(state) for state, _ in laya.calls)   # Laya saw the redacted goal


# ---------------------------------------------------------- media keys

def test_media_key_codes():
    from servo.actuation.needle_tools import media_key_vk
    assert media_key_vk(["volumeup"]) == 0xAF and media_key_vk(["volume up"]) == 0xAF
    assert media_key_vk(["ctrl", "s"]) is None and media_key_vk(["enter"]) is None


# ------------------------------------------------------- screen and prompt

def test_describe_scene_dedupes_and_shows_address():
    from servo.cognition.router import describe_scene
    from servo.perception.scene_index import SceneElement, SceneIndex

    def el(i, role, label, value=None):
        return SceneElement(element_id=i, role=role, label=label, bbox_screen=(0, 0, 1, 1),
                            bbox_norm=(0, 0, 1, 1), center_screen=(0, 0), value=value)
    scene = SceneIndex(hwnd=1, window_title="kanye - YouTube", source="uia", elements={
        0: el(0, "Edit", "Address", "https://www.youtube.com/results?search_query=kanye"),
        1: el(1, "Hyperlink", "Kanye West"), 2: el(2, "Hyperlink", "Kanye West"),
        3: el(3, "Hyperlink", "Runaway 4 minutes")})
    text = describe_scene(scene, "dia.exe")
    assert "Address bar / input: https://www.youtube.com/results" in text
    assert text.count("Hyperlink: Kanye West") == 1 and "(dia.exe)" in text


def test_plan_prompt_has_the_rules_that_fix_real_failures():
    from servo.cognition.planners import AGENTS, plan_system
    for name in AGENTS:
        flat = " ".join(plan_system(name).split())
        for rule in ["NOT confirmed", "[redacted]", "Statements and questions", "Follow-ups", "[Recipes]",
                     "never send the same step again unchanged", "Never close the Servo Copilot window"]:
            assert rule in flat, (name, rule)
    code = " ".join(plan_system("code").split())
    assert "never wrapped in powershell -command" in code
    assert "powershell -command" not in " ".join(plan_system("settings").split())


def test_plan_prompt_includes_recipes_and_records_them():
    from servo.cognition.system2 import build_plan_prompt
    meta = {}
    prompt = build_plan_prompt("search youtube for kanye and play it", memory_context="", meta_out=meta)
    assert "[Recipes" in prompt and "youtube" in meta["recipes"]


def test_recipe_steps_are_valid_step_forms():
    """Recipes go into the planner's prompt: every `step` in them must be one Servo accepts,
    or the recipe teaches the planner steps that are rejected at run time."""
    import glob
    import re
    from pathlib import Path
    from servo.cognition.specialists import step_form
    root = Path(__file__).resolve().parent.parent / "knowledge" / "recipes"
    prose = {"powershell -command"}   # quoted in a sentence, not a step
    bad = []
    for f in glob.glob(str(root / "*.md")):
        if f.endswith("README.md"):
            continue
        for step in re.findall(r"`([^`]+)`", Path(f).read_text(encoding="utf-8")):
            filled = step.replace("[n]", "[3]").replace("<", "").replace(">", "")
            if step not in prose and step_form(filled) is None:
                bad.append(f"{Path(f).name}: {step}")
    assert not bad, bad
