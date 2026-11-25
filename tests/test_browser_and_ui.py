"""Playwright drives the copilot UI end to end (Servo itself no longer uses Playwright).

Runs headless with the installed Edge (or bundled Chromium); skipped when Playwright is
not installed.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

pytest.importorskip("playwright")

from servo.cognition import cactus_runtime  # noqa: E402
from servo.cognition.router import CascadeRouter, RunOutcome  # noqa: E402
from servo.cognition.trace import TraceBus, Tracer  # noqa: E402
from servo.config import settings  # noqa: E402
from servo.control.confirmation import ConfirmationRequest  # noqa: E402
from servo.control.jev_gate import JevGate  # noqa: E402

# ----------------------------------------------------------------- UI e2e

class FakeRouter:
    """Emits what the real router emits for a two-step task with a risky step."""

    def __init__(self, *, tracer, confirmation_callback, ask=None, step_mode=False, **_):
        self.tr, self.confirm, self.ask, self.step_mode = tracer, confirmation_callback, ask, step_mode
        self.hwnd, self.context = None, "desktop"

    def run(self, text):
        tr = self.tr
        tr.emit("goal", text, tier="copilot")
        with tr.span("tier", "Tier 1 · 2 clauses", tier="needle") as sp:
            sp.done("ok", "All 2 clauses parsed → 2 steps", confidence=0.95)
        tr.emit("decision", "Laya · route the request", "Heuristic: 2 action verbs", tier="laya", status="ok", confidence=0.9)
        plan = tr.emit("plan", "Round 1 · 2 steps", "click Save; press enter", tier="needle",
                       subtasks=["click Save", "press enter"], round=1)
        tr.emit("progress", "click Save", "", tier="router", status="running", plan=plan.seq, index=0)
        tr.emit("perception", "UIA · 0 controls match “Save”", "No UI element found", tier="uia", status="warn", confidence=0.0)
        tr.emit("escalate", "Perception miss → Mirage", "Looking at the pixels instead.", tier="mirage", status="warn")
        tr.emit("perception", "Mirage · 14 detections", "“Save” → “Save” at (812, 64)", tier="mirage", status="ok",
                confidence=0.93, ranked=[["Save", 1.0], ["Save as", 0.82]])
        tr.emit("decision", "Laya pre-check · medium risk", "Medium risk keyword detected: 'Save'", tier="laya", status="warn")
        ok = self.confirm(ConfirmationRequest(sub_goal="Click Save", tool_call="click(element_id=3)", element_label="Save",
                                              risk_level="medium", reason="Medium risk keyword detected: 'Save'", hwnd=0))
        tr.emit("progress", "click Save", "", tier="router", status="ok" if ok else "skipped", plan=plan.seq, index=0)
        if self.step_mode and self.ask("step", {"command": "press enter", "index": 2, "total": 2},
                                       ["next", "run_all", "stop"], None) == "stop":
            tr.emit("result", "Stopped.", tier="copilot", status="warn")
            return RunOutcome(run_id=tr.run_id, goal=text, success=False, reply="Stopped.", route="system1", aborted=True)
        tr.emit("progress", "press enter", "", tier="router", status="running", plan=plan.seq, index=1)
        tr.emit("progress", "press enter", "", tier="router", status="ok", plan=plan.seq, index=1)
        tr.emit("model", "Model reasoning", "The file is unsaved, so clicking Save is the right move.", tier="system2", reasoning=True)
        reply = "Saved." if ok else "Skipped saving."
        tr.emit("result", reply, tier="copilot", status="ok")
        return RunOutcome(run_id=tr.run_id, goal=text, success=True, reply=reply, route="system1")


@pytest.fixture
def copilot_server(monkeypatch, tmp_path):
    from servo import user_settings
    from servo.daemon.copilot_app import CopilotSession, _free_port, _start_server, create_app

    monkeypatch.setattr(cactus_runtime, "get_needle", lambda tools: None)
    # Real setup flow, but against a throwaway settings file.
    monkeypatch.delenv("SERVO_NO_USER_SETTINGS", raising=False)
    monkeypatch.setattr(user_settings, "PATH", tmp_path / "settings.json")
    for name in ("planner_provider", "gemini_api_key", "gemini_model", "openai_base_url", "openai_model"):
        monkeypatch.setattr(settings, name, getattr(settings, name))
    session = CopilotSession(router_factory=FakeRouter)
    port = _free_port(0)
    server = _start_server(create_app(session), port)
    yield session, f"http://127.0.0.1:{port}/"
    server.should_exit = True


def test_api_requires_token(copilot_server):
    _, url = copilot_server
    assert httpx.post(url + "api/send", json={"text": "x"}).status_code == 403
    assert httpx.post(url + "api/send", json={"text": "x"}, headers={"X-Servo-Token": "nope"}).status_code == 403


def test_ui_shows_trace_ladder_and_confirmation(copilot_server, tmp_path):
    from playwright.sync_api import expect, sync_playwright

    session, url = copilot_server
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel=settings.browser_channel or None, headless=True)
        except Exception:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as e:
                pytest.skip(f"no Playwright browser: {e}")
        first = True
        for scheme in ("dark", "light"):
            page = browser.new_page(viewport={"width": 440, "height": 820}, color_scheme=scheme)
            page.goto(url)
            if first:
                # First launch asks which reasoning model to use, and tests it before saving.
                setup = page.locator("#setup")
                expect(setup).to_be_visible(timeout=10000)
                expect(page.locator("#modelLabel")).not_to_have_text("Claude")
                page.screenshot(path=str(Path(os.environ.get("SERVO_UI_SCREENSHOT_DIR", tmp_path)) / f"copilot-setup-{scheme}.png"))
                page.get_by_text("Gemini", exact=True).click()
                page.get_by_role("button", name="Test and save").click()
                expect(page.locator("#testResult")).to_have_text("No Gemini API key.")
                expect(setup).to_be_visible()                       # failed test does not save
                page.get_by_text("No reasoning model", exact=True).click()
                page.get_by_role("button", name="Test and save").click()
                expect(setup).to_be_hidden()
                expect(page.locator("#modelLabel")).to_have_text("No reasoning model")
                first = False
            else:
                expect(page.locator("#setup")).to_be_hidden()        # saved: not asked again
            step_by_step = scheme == "light"
            if step_by_step:
                page.get_by_text("Step by step").click()
                expect(page.locator("#stepMode")).to_be_checked()
            page.get_by_role("textbox", name="Request").fill("click Save, then press enter")
            page.keyboard.press("Enter")

            card = page.locator(".confirm").first
            expect(card).to_be_visible(timeout=10000)
            expect(card).to_contain_text("medium-risk")
            for name in ("Proceed", "Skip this step", "Stop"):
                expect(card.get_by_role("button", name=name)).to_be_visible()
            card.get_by_role("button", name="Proceed").click()
            expect(page.locator(".confirm.closed h3").first).to_have_text("Proceeded")

            if step_by_step:
                step = page.locator(".confirm.step")
                expect(step).to_contain_text("Next: press enter")
                expect(step).to_contain_text("Step 2 of 2")
                step.get_by_role("button", name="Next step").click()

            expect(page.locator(".reply").last).to_have_text("Saved.", timeout=10000)
            expect(page.locator(".plan li.ok")).to_have_count(2)            # live checklist
            expect(page.locator(".rung.r-needle.used")).to_have_count(1)
            expect(page.locator(".rung.r-mirage.used")).to_have_count(1)
            expect(page.locator(".ev.reasoning .detail")).to_contain_text("unsaved")
            expect(page.locator(".ev.k-progress")).to_have_count(0)          # progress stays out of the trace
            expect(page.locator(".rung.r-system2.used")).to_have_count(1)
            page.wait_for_timeout(400)  # let the ladder transitions settle for the screenshot
            shot = Path(os.environ.get("SERVO_UI_SCREENSHOT_DIR", tmp_path)) / f"copilot-{scheme}.png"
            page.screenshot(path=str(shot))
            page.close()
        browser.close()


def test_config_test_does_not_save(copilot_server):
    from servo import user_settings

    session, url = copilot_server
    h = {"X-Servo-Token": session.token}
    before = settings.planner_provider
    r = httpx.post(url + "api/config/test", json={"provider": "gemini", "api_key": ""}, headers=h).json()
    assert r["ok"] is False
    assert settings.planner_provider == before and not user_settings.PATH.exists()
    r = httpx.post(url + "api/config", json={"provider": "gemini", "api_key": "AIza-test-key-1234", "model": "gemini-x"}, headers=h).json()
    assert r["reasoning"]["provider"] == "gemini" and r["setup_done"]
    view = httpx.get(url + "api/config", headers=h).json()
    assert view["gemini"]["key"] == "…1234"                       # never echoed back in full
    assert "AIza-test-key-1234" not in user_settings.PATH.read_text()
