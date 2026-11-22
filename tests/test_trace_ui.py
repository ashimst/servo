"""The copilot page renders a run's story, honest checklist and outcome; past runs replay."""

import time

import pytest

pytest.importorskip("playwright")


def _events(run_id):
    from servo.cognition.trace import TraceEvent
    E = lambda **k: TraceEvent(run_id=run_id, **k)  # noqa: E731
    return [
        E(kind="goal", tier="copilot", title="play kanye on youtube"),
        E(kind="route", tier="router", status="ok", title="Reasoning miss → System 2", detail="needs a plan"),
        E(kind="model", tier="system2", status="ok", title="System 2 · plan", detail="Search YouTube, then click the video.",
          data={"subtasks": ["search youtube for kanye", "click Runaway"], "knowledge": {"recipes": ["youtube"]}}),
        E(seq=900001, kind="plan", tier="system2", title="Round 1 · 2 steps",
          data={"subtasks": ["search youtube for kanye", "click Runaway"]}),
        E(kind="progress", tier="router", status="ok", title="search youtube for kanye", data={"plan": 900001, "index": 0}),
        E(kind="perception", tier="uia", status="ok", title="UIA · 89 controls"),
        E(kind="progress", tier="router", status="unconfirmed", title="click Runaway",
          detail="no visible change", data={"plan": 900001, "index": 1}),
        E(kind="result", tier="copilot", status="ok", title="Ran 2 steps, but I couldn't confirm this one worked",
          duration_ms=4200, data={"route": "system2", "rounds": 1}),
    ]


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    from servo.daemon.copilot_app import CopilotSession, _free_port, _start_server, create_app
    session = CopilotSession()
    port = _free_port(0)
    server = _start_server(create_app(session), port)
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="msedge")
        except Exception:
            pytest.skip("no Edge/Chromium available for Playwright")
        pg = browser.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"http://127.0.0.1:{port}/")
        pg.wait_for_selector("#feed")
        yield pg, session, errors
        browser.close()
    server.should_exit = True


def test_run_story_and_outcome(page):
    pg, session, errors = page
    pg.wait_for_function("document.querySelector('#conn') && !document.querySelector('#conn').classList.contains('down')",
                         timeout=5000)
    for ev in _events("r1"):
        session._on_trace(ev)
        time.sleep(0.02)
    pg.wait_for_selector(".outcome", timeout=5000)
    outcome = pg.inner_text(".outcome")
    assert "1 confirmed" in outcome and "1 not confirmed" in outcome
    assert "Search YouTube, then click the video." in pg.inner_text(".plan .thought")
    assert "recipe: youtube" in pg.inner_text(".plan .chips")
    assert pg.locator(".plan li.unconfirmed").count() == 1
    assert "Reasoning miss" in pg.inner_text(".story")
    # trace starts collapsed and filtered to key events
    assert pg.get_attribute(".trace", "open") is None
    assert "key-only" in pg.get_attribute(".trace", "class")
    assert not errors, errors


def test_runs_tab_replays_a_saved_run(page, monkeypatch):
    pg, session, errors = page
    from servo.cognition.router import RunOutcome, StepRecord
    from servo.telemetry.run_record import build_record, save_run
    out = RunOutcome(run_id="saved1", goal="open notepad", success=False, reply="I got stuck", route="system1",
                     steps=[StepRecord(command="open notepad", tool="launch_app", tier="uia", success=False,
                                       error="did not appear")])
    save_run(build_record(out, _events("saved1")))
    if pg.is_visible("#setup"):  # first-run setup sheet (the test SERVO_HOME has no settings)
        pg.click("#closeSetupXBtn")
    pg.click("#tabRuns")
    pg.wait_for_selector(".run-row", timeout=5000)
    assert "open notepad" in pg.inner_text("#runsList")
    pg.click(".run-row >> nth=0")
    pg.wait_for_selector("text=replay of a run", timeout=5000)
    assert not errors, errors
