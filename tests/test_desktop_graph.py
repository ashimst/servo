"""Desktop knowledge graph (SQLite + FTS5): pages, controls, procedures, failures, retrieval."""

import json

import pytest

from servo.cognition import desktop_graph as dg
from servo.cognition.desktop_graph import DesktopGraph, node_id


@pytest.fixture
def graph(tmp_path):
    return DesktopGraph(path=tmp_path / "g.sqlite")


CONTROLS = [
    {"label": "Time zone", "role": "ComboBox", "bbox": (500, 300, 700, 330), "value": "UTC"},
    {"label": "Set time automatically", "role": "Button", "bbox": (500, 200, 560, 230), "toggle": "on"},
    {"label": "Date & time", "role": "Text", "bbox": (0, 0, 10, 10)},          # plain text: not a control
    {"label": "", "role": "Button", "bbox": (0, 0, 10, 10)},                   # unnamed: skipped
]


def _types(g):
    return {r["name"]: r["type"] for r in g._db.execute("SELECT name, type FROM nodes")}


def test_observe_page_records_app_page_and_controls(graph):
    pid = graph.observe_page("Settings", "Date & time", CONTROLS, uri="ms-settings:dateandtime",
                             window_box=(0, 0, 1000, 1000))
    types = _types(graph)
    assert types["Settings"] == "app" and types["Date & time"] == "page"
    assert types["Time zone"] == "control" and types["Set time automatically"] == "control"
    assert "" not in types
    assert graph.stats()["control"] == 2 and graph.stats()["edges"] == 3      # has_page + 2 × has_control
    cid = node_id("control", "Settings", "Date & time", "ComboBox", "Time zone", "")
    props = json.loads(graph._db.execute("SELECT props FROM nodes WHERE id=?", (cid,)).fetchone()["props"])
    assert props["rel_center"] == [0.6, 0.315] and props["value"] == "UTC" and props["page"] == "Date & time"
    assert pid == node_id("page", "Settings", "Date & time")


def test_context_for_names_the_controls_page_and_app(graph):
    graph.observe_page("Settings", "Date & time", CONTROLS, uri="ms-settings:dateandtime")
    ctx = graph.context_for("change my time zone")
    assert ctx.startswith("[Known about this computer")
    assert "ComboBox “Time zone” is on Settings › Date & time (ms-settings:dateandtime)" in ctx
    assert graph.context_for("") == "" and graph.context_for("zzqx") == ""


STEPS = [
    {"call": "launch_app(app_name='ms-settings:dateandtime')", "tool": "launch_app", "verification": "confirmed",
     "app": "Settings", "page": "Date & time", "target_label": "", "target_role": ""},
    {"call": "select_option(target='ComboBox: Time zone', option='Nepal')", "tool": "select_option",
     "verification": "confirmed", "app": "Settings", "page": "Date & time",
     "target_label": "Time zone", "target_role": "ComboBox"},
]


def test_verified_run_becomes_a_procedure(graph):
    graph.observe_page("Settings", "Date & time", CONTROLS)
    pid = graph.record_run("set my time zone to Nepal", STEPS, success=True)
    row = graph._db.execute("SELECT * FROM nodes WHERE id=?", (pid,)).fetchone()
    assert row["type"] == "procedure" and row["verified"] == 1
    assert json.loads(row["props"])["steps"] == [s["call"] for s in STEPS]
    cid = node_id("control", "Settings", "Date & time", "ComboBox", "Time zone", "")
    assert graph._db.execute("SELECT verified FROM nodes WHERE id=?", (cid,)).fetchone()[0] == 1
    assert "Done before: “set my time zone to Nepal”" in graph.context_for("time zone Nepal")


def test_unverified_or_failed_runs_are_not_procedures(graph):
    unconfirmed = [dict(STEPS[0]), {**STEPS[1], "verification": "unconfirmed"}]
    assert graph.record_run("set my time zone", unconfirmed, success=True) is None
    assert graph.record_run("set my time zone", STEPS, success=False) is None
    assert "procedure" not in graph.stats()


def test_failed_step_becomes_a_failure(graph):
    graph.observe_page("Settings", "Date & time", CONTROLS)
    failed = [dict(STEPS[0]), {**STEPS[1], "verification": "failed", "error": "Time zone still reads UTC"}]
    assert graph.record_run("set my time zone to Nepal", failed, success=False) is None
    row = graph._db.execute("SELECT * FROM nodes WHERE type='failure'").fetchone()
    assert row["failed"] == 1 and json.loads(row["props"])["error"] == "Time zone still reads UTC"
    edge = graph._db.execute("SELECT dst FROM edges WHERE src=? AND type='failed_on'", (row["id"],)).fetchone()
    assert edge["dst"] == node_id("control", "Settings", "Date & time", "ComboBox", "Time zone", "")
    assert "Failed before:" in graph.context_for("time zone Nepal")


def test_search_ranks_verified_controls_higher(graph):
    graph.observe_page("Settings", "Display", [{"label": "Brightness", "role": "Slider"}])
    graph.observe_page("Control Panel", "Power", [{"label": "Brightness", "role": "Slider"}])
    graph.record_run("set brightness to 40", [
        {"call": "set_value(target='Slider: Brightness', value='40')", "tool": "set_value",
         "verification": "confirmed", "app": "Control Panel", "page": "Power",
         "target_label": "Brightness", "target_role": "Slider"}], success=True)
    controls = graph.search("brightness", kinds=("control",))
    assert [json.loads(r["props"])["app"] for r in controls] == ["Control Panel", "Settings"]


def test_capabilities_are_searchable(graph):
    from servo.cognition.needle import NEEDLE_TOOLS
    graph.add_capabilities(NEEDLE_TOOLS)
    assert graph.stats()["capability"] == len(NEEDLE_TOOLS)
    assert any(r["name"] == "run_command" for r in graph.search("PowerShell command"))


def test_singleton_lives_in_the_test_folder(tmp_path):
    g = dg.desktop_graph()
    assert g.path == tmp_path / "desktop_graph.sqlite"          # conftest: never the real ~/.servo graph
    assert dg.desktop_graph() is g and g.stats()["capability"] > 0


# ------------------------------------------------------------- router + planner wiring

def test_router_records_runs_and_planner_reads_them(laya, needle):
    from servo.cognition import system2
    from servo.cognition.router import CascadeRouter, StepOutcome
    from servo.cognition.trace import TraceBus, Tracer
    from servo.control.jev_gate import JevGate

    class Planner(system2.System2):
        def __init__(self):
            super().__init__(provider="none")
            self.plans = [system2.System2Result(subtasks=["turn on [4]"])]

        def available(self):
            return True

        def plan(self, goal, screen="", completed=None, failure=None, round_no=1, agent="apps"):
            return self.plans.pop(0) if self.plans else system2.System2Result(done=True, reply="Done.")

    laya.complexity, laya.domain = "multi", "settings"
    needle.calls["turn on [4]"] = [("turn_on", {"control": 4})]
    r = CascadeRouter(tracer=Tracer("t", TraceBus()), laya=JevGate(), memory=None, system2=Planner())
    r._execute = lambda step: StepOutcome(success=True)

    def screen(kind=None):
        r._numbered = {4: {"role": "Button", "label": "Bluetooth", "context": None, "bbox": (0, 0, 1, 1)}}
        return "[4] Button: Bluetooth"
    r._screen_summary = screen
    out = r.run("turn on the Bluetooth switch")
    assert out.success and out.steps[0].target == "Button: Bluetooth"    # StepRecord carries the target
    assert {"target", "app", "page"} <= set(out.steps[0].model_dump())
    g = dg.desktop_graph()
    assert g.stats().get("procedure") == 1
    block, meta = system2.knowledge_context("Bluetooth switch")
    assert "Done before: “turn on the Bluetooth switch”" in block and meta.get("desktop_graph")


def test_observe_page_from_the_router(monkeypatch):
    from servo.cognition.router import CascadeRouter
    from servo.cognition.trace import NullTracer
    from servo.perception.scene_index import SceneElement, SceneIndex

    r = CascadeRouter(tracer=NullTracer(), memory=None)
    r.hwnd = None
    monkeypatch.setattr(r, "_app_name", lambda: "Notepad")
    el = SceneElement(element_id=0, role="Button", label="Save", bbox_screen=(0, 0, 9, 9), bbox_norm=(0, 0, 1, 1),
                      center_screen=(4, 4))
    r._observe_page(SceneIndex(hwnd=1, window_title="Untitled - Notepad", source="uia", elements={0: el}))
    assert "Button “Save” is on Notepad › Untitled - Notepad" in dg.desktop_graph().context_for("save")
