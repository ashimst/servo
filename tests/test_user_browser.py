"""Web tasks in the user's own browser: grammar, preference, URL safety, routing."""

import pytest

from servo.cognition import user_browser as ub
from servo.config import settings


@pytest.mark.parametrize("text,url,browser", [
    ("open gmail in chrome", "https://mail.google.com", "chrome"),
    ("open youtube.com in dia", "https://youtube.com", "dia"),
    ("search youtube for kanye west", "https://www.youtube.com/results?search_query=kanye+west", ""),
    ("search for lo-fi beats on spotify", "https://open.spotify.com/search/lo-fi+beats", ""),
    ("find usb hub on amazon in edge", "https://www.amazon.com/s?k=usb+hub", "edge"),
])
def test_site_grammar(text, url, browser):
    from servo.cognition.needle import parse_grammar
    step = parse_grammar(text).steps[0]
    assert step.tool_name == "open_url" and step.arguments["url"] == url
    assert step.arguments.get("browser", "") == browser


def test_unknown_site_in_browser_is_not_guessed():
    from servo.cognition.needle import parse_site_intent
    assert parse_site_intent("open email in dia") is None   # goes to System 2, not a guess


@pytest.mark.parametrize("text,name", [
    ("dia is my default browser", "dia"), ("Dia is my preferred browser.", "dia"),
    ("use chrome as my browser", "chrome"), ("always use edge", "edge"),
    ("my browser is arc", "arc"), ("set firefox as my default browser", "firefox"),
])
def test_preference_statements(text, name):
    assert ub.parse_browser_preference(text) == name


@pytest.mark.parametrize("text", ["open dia", "use the search box", "dia is great", "open chrome and go to gmail"])
def test_not_preferences(text):
    assert ub.parse_browser_preference(text) is None


def test_choose_browser_order(monkeypatch):
    monkeypatch.setattr(settings, "preferred_browser", "dia")
    monkeypatch.setattr(ub, "windows_default_browser", lambda: "arc.exe")
    assert ub.choose_browser("chrome", "edge")[0] == "chrome"
    assert ub.choose_browser("", "edge")[0] == "edge"
    assert ub.choose_browser()[0] == "dia"
    monkeypatch.setattr(settings, "preferred_browser", "")
    assert ub.choose_browser()[0] == "arc"


@pytest.mark.parametrize("url,goal,blocked", [
    ("https://youtube.com", "", False),
    ("http://127.0.0.1:8765/", "play music", True),          # the copilot's own page
    ("http://localhost:3000/", "open localhost:3000", False),  # user asked by name
    ("http://192.168.1.1/", "", True),
    ("file:///C:/Users/x/.servo/api_token", "", True),
    ("javascript:alert(1)", "", True),
])
def test_url_safety(url, goal, blocked):
    assert (ub.url_problem(url, goal) is not None) == blocked


def test_blank_tab_titles():
    assert ub._is_blank_tab_title("Profile 1: New Tab")
    assert ub._is_blank_tab_title("New Tab - Google Chrome")
    assert not ub._is_blank_tab_title("Profile 1: Example Domain")


def test_send_keys_escaping():
    assert ub.escape_send_keys("https://a.com/?q=(x)+y%") == "https://a.com/?q={(}x{)}{+}y{%}"


# ------------------------------------------------------------- routing

class _Tracer:
    run_id = "t"
    def emit(self, *a, **k): return type("E", (), {"seq": 0})()


def _router(monkeypatch, verdict="confirmed"):
    from servo.cognition import router as r
    from servo.control.jev_gate import JevGate
    monkeypatch.setattr(settings, "browser_mode", "user")
    nav = []
    monkeypatch.setattr(ub, "navigate", lambda hwnd, url: nav.append((hwnd, url)) or (verdict, "Page", ""))
    monkeypatch.setattr(r.windows, "get_foreground_window", lambda: 0)
    launched = []
    rt = r.CascadeRouter(tracer=_Tracer(), kev=JevGate(use_fallback=True), browser=object(), memory=None,
                         app_launcher=lambda name: launched.append(name) or (42, name))
    return rt, nav, launched


def test_open_url_goes_to_named_browser(monkeypatch):
    from servo.cognition.needle import parse_grammar
    rt, nav, launched = _router(monkeypatch)
    rt.goal = "open gmail in dia"
    out = rt._execute(parse_grammar("open gmail in dia").steps[0])
    assert out.success and out.verification == "confirmed"
    assert launched == ["dia"] and nav == [(42, "https://mail.google.com")]
    assert rt.context == "desktop" and rt.recent_browser == "dia"


def test_local_address_refused_without_launching(monkeypatch):
    from servo.cognition.needle import _step
    rt, nav, launched = _router(monkeypatch)
    rt.goal = "play music"
    out = rt._execute(_step(1, "open_url", {"url": "http://127.0.0.1:8765/"}, "x"))
    assert not out.success and out.kind == "policy" and not launched and not nav


def test_failed_navigation_is_a_failure(monkeypatch):
    from servo.cognition.needle import _step
    rt, _, _ = _router(monkeypatch, verdict="failed")
    rt.goal = "open youtube"
    assert not rt._execute(_step(1, "open_url", {"url": "https://youtube.com"}, "x")).success


def test_preference_is_saved_not_executed(monkeypatch):
    from servo import user_settings
    saved = {}
    monkeypatch.setattr(user_settings, "save", lambda s, upd, mark_done=True: saved.update(upd))
    rt, nav, launched = _router(monkeypatch)
    out = rt.run("dia is my default browser")
    assert out.route == "preference" and saved == {"preferred_browser": "dia"}
    assert not nav and not launched
