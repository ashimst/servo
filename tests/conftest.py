"""Shared test configuration.

Everything here runs before any servo module is imported, so module-level paths
(``Path.home() / ".servo" / ...``) already point at the sandbox.
"""

import os
import tempfile

# A private Servo data folder for the whole test session. Without it, tests wrote fake
# runs into the developer's real ~/.servo/traces, chat memory and graph.json. (Only
# SERVO_HOME: overriding USERPROFILE also moved the home of browsers Playwright starts.)
os.environ["SERVO_HOME"] = tempfile.mkdtemp(prefix="servo-test-home-")

# Tests must not pick up the developer's saved copilot setup (~/.servo/settings.json).
os.environ["SERVO_NO_USER_SETTINGS"] = "1"
# Web steps must never drive the developer's real browser (user mode types into it).
os.environ["SERVO_BROWSER_MODE"] = "playwright"
os.environ.pop("SERVO_PREFERRED_BROWSER", None)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _never_show_real_dialogs(monkeypatch):
    """Unit tests must never pop a real confirmation dialog on the developer's desktop."""
    from servo.config import settings
    monkeypatch.setattr(settings, "confirmation_channel", "deny")


@pytest.fixture(autouse=True)
def _no_real_start_menu(monkeypatch):
    """The Start-menu app list (Get-StartApps) is this machine's; tests use a fixed one."""
    from servo.perception import app_identity
    monkeypatch.setattr(app_identity, "start_apps", lambda refresh=False: [])


@pytest.fixture(autouse=True)
def _never_move_the_real_copilot(monkeypatch):
    """Tests must not find (and move aside) a copilot open on the developer's desktop."""
    from servo.actuation import overlay
    monkeypatch.setattr(overlay, "own_windows", lambda: set())


# --------------------------------------------------------------------- models
#
# Servo has no rule-based stand-ins: every decision comes from Laya, every instruction is
# read by Cactus Needle, plans come from System 2, vision from Mirage. Unit tests never
# reach the real models (no network, no native engines): by default each one is
# unavailable, and a test that needs one installs a fake below.


class FakeLaya:
    """Answers System One questions in Laya's wire shape. Set the attributes per test.

    ``risk``: "low" | "medium" | "high" or a {"low": p, "medium": p, "high": p} dict.
    ``unavailable``: a message to raise LayaUnavailable with (Laya down / unconfigured).
    """

    def __init__(self):
        self.risk = "low"
        self.scope = 0.95
        self.outcome = "confirmed"
        self.desktop = 0.95
        self.complexity = "single"
        self.domain = "apps"
        self.unavailable = None
        self.calls = []

    def answers(self, state, questions):
        from servo.control.jev_gate import LayaUnavailable
        self.calls.append((state, questions))
        if self.unavailable:
            raise LayaUnavailable(self.unavailable)
        out = {}
        for key in questions:
            if key == "risk_level":
                probs = self.risk if isinstance(self.risk, dict) else {
                    lv: (0.9 if lv == self.risk else 0.05) for lv in ("low", "medium", "high")}
                out[key] = {"type": "score", "probabilities": {
                    str(i): probs.get(lv, 0.0) for i, lv in enumerate(("low", "medium", "high"))}}
            elif key == "scope_valid":
                out[key] = {"type": "noul", "noul": self.scope}
            elif key == "desktop_task":
                out[key] = {"type": "noul", "noul": self.desktop}
            elif key == "complexity":
                out[key] = {"type": "choice", "choice": self.complexity, "confidence": 0.9,
                            "probabilities": {self.complexity: 0.9}}
            elif key == "domain":
                out[key] = {"type": "choice", "choice": self.domain, "confidence": 0.9,
                            "probabilities": {self.domain: 0.9}}
            elif key == "outcome":
                out[key] = {"type": "choice", "choice": self.outcome, "confidence": 0.9,
                            "probabilities": {self.outcome: 0.9}}
        return out


class FakeNeedle:
    """Cactus Needle stand-in for tests: ``calls`` maps an instruction to its tool calls.

    Servo has one Needle agent per tool (key ``"tool:<name>"``). A key of ``calls`` is the
    instruction, or ``(agent key, instruction)`` to answer only on one tool's agent. A value
    is a list of (name, arguments) pairs, or a full response dict. Instructions not in the
    map get no tool calls, as a real Needle that finds no tool would.
    """

    def __init__(self, calls=None, confidence=0.95):
        self.calls = dict(calls or {})
        self.confidence = confidence
        self.seen = []        # instructions, in order
        self.asked = []       # (agent key, instruction), in order
        self.agents = []      # agent keys requested from get_needle, in order
        self.tools = {}       # agent key -> the tool names it was built with
        self.systems = {}     # agent key -> its system prompt

    def agent(self, key):
        return _FakeNeedleAgent(self, key)

    def complete(self, text, key="default"):
        self.seen.append(text)
        self.asked.append((key, text))
        got = self.calls.get((key, text), self.calls.get(text))
        if isinstance(got, dict):
            return got
        return {"function_calls": [{"name": n, "arguments": a} for n, a in (got or [])],
                "confidence": self.confidence, "reasoning": "fake"}


class _FakeNeedleAgent:
    def __init__(self, fake, key):
        self.fake, self.key = fake, key

    def complete(self, text):
        return self.fake.complete(text, self.key)


@pytest.fixture(autouse=True)
def _no_real_models(monkeypatch):
    from servo.cognition import cactus_runtime, system2
    from servo.control import jev_gate

    def no_laya(self, state, questions):
        raise jev_gate.LayaUnavailable("Laya is not reachable in unit tests (use the `laya` fixture)")

    monkeypatch.setattr(jev_gate.LayaClient, "ask", no_laya)
    monkeypatch.setattr(cactus_runtime, "get_needle", lambda tools, key="default", system="": None)
    monkeypatch.setattr(cactus_runtime, "needle_error", lambda: "not available in unit tests")
    monkeypatch.setattr(cactus_runtime, "get_mirage", lambda: None)
    monkeypatch.setattr(cactus_runtime, "mirage_error", lambda: "not available in unit tests")
    monkeypatch.setattr(system2, "_reachable", lambda base_url, api_key="": False)

    from servo.perception import locator, ocr_sensor

    def no_locator(img, description, timeout=None):
        raise locator.LocatorUnavailable("the vision locator is not reachable in unit tests (monkeypatch locator.locate)")

    def no_ocr(img):
        raise ocr_sensor.OcrUnavailable("Windows OCR is not used in unit tests (monkeypatch ocr_sensor.read_lines)")

    monkeypatch.setattr(locator, "locate", no_locator)
    monkeypatch.setattr(ocr_sensor, "read_lines", no_ocr)


@pytest.fixture(autouse=True)
def _private_desktop_graph(monkeypatch, tmp_path):
    """The desktop knowledge graph lives in this test's tmp folder, never ~/.servo."""
    from servo.cognition import desktop_graph
    monkeypatch.setattr(desktop_graph, "DEFAULT_DB", tmp_path / "desktop_graph.sqlite")
    monkeypatch.setattr(desktop_graph, "_graph", None)


@pytest.fixture
def laya(monkeypatch):
    """A fake Laya server behind every JevGate (LayaClient.ask)."""
    from servo.control import jev_gate
    fake = FakeLaya()
    monkeypatch.setattr(jev_gate.LayaClient, "ask", lambda self, state, questions: fake.answers(state, questions))
    return fake


@pytest.fixture
def needle(monkeypatch):
    """A fake Cactus Needle behind Tier1Intent.propose: one fake answers for every per-tool agent.

    ``fake.agents`` records the key of each get_needle call (e.g. "tool:click")."""
    from servo.cognition import cactus_runtime
    fake = FakeNeedle()

    def get_needle(tools, key="default", system=""):
        fake.agents.append(key)
        fake.tools[key] = [t["name"] for t in tools]
        fake.systems[key] = system
        return fake.agent(key)

    monkeypatch.setattr(cactus_runtime, "get_needle", get_needle)
    monkeypatch.setattr(cactus_runtime, "needle_complete", lambda agent, text: agent.complete(text))
    return fake
