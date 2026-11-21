"""Saved copilot setup: env precedence, key encryption, keep-existing-key semantics."""

from types import SimpleNamespace

from servo import user_settings


def _fresh(monkeypatch, tmp_path):
    monkeypatch.delenv("SERVO_NO_USER_SETTINGS", raising=False)
    monkeypatch.setattr(user_settings, "PATH", tmp_path / "settings.json")
    return SimpleNamespace(planner_provider="openai", anthropic_api_key="", anthropic_model="claude-opus-5-5")


def test_round_trip_and_key_not_in_plaintext(monkeypatch, tmp_path):
    s = _fresh(monkeypatch, tmp_path)
    user_settings.save(s, {"planner_provider": "anthropic", "anthropic_api_key": "sk-ant-secret-9999"})
    raw = user_settings.PATH.read_text()
    assert "sk-ant-secret-9999" not in raw or '"plain"' in raw  # DPAPI on Windows; plain only off-Windows
    t = SimpleNamespace(planner_provider="openai", anthropic_api_key="")
    user_settings.apply(t)
    assert t.planner_provider == "anthropic" and t.anthropic_api_key == "sk-ant-secret-9999"
    assert user_settings.setup_done()


def test_none_keeps_saved_key_and_empty_removes_it(monkeypatch, tmp_path):
    s = _fresh(monkeypatch, tmp_path)
    user_settings.save(s, {"anthropic_api_key": "sk-ant-keepme-0000"})
    user_settings.save(s, {"planner_provider": "anthropic", "anthropic_api_key": None})
    t = SimpleNamespace(anthropic_api_key="")
    user_settings.apply(t)
    assert t.anthropic_api_key == "sk-ant-keepme-0000"
    user_settings.save(s, {"anthropic_api_key": ""})
    assert "anthropic_api_key" not in user_settings.read()


def test_env_var_wins(monkeypatch, tmp_path):
    s = _fresh(monkeypatch, tmp_path)
    user_settings.save(s, {"planner_provider": "gemini"})
    monkeypatch.setenv("SERVO_PLANNER_PROVIDER", "anthropic")
    t = SimpleNamespace(planner_provider="anthropic")
    user_settings.apply(t)
    assert t.planner_provider == "anthropic"


def test_disabled_in_tests_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(user_settings, "PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text('{"setup_done": true}')
    monkeypatch.setenv("SERVO_NO_USER_SETTINGS", "1")
    assert user_settings.setup_done() is False


def test_find_window_ignores_other_browsers(monkeypatch):
    """Placement must only ever touch the copilot's own window, never a browser tab showing it."""
    from servo.daemon import copilot_app
    from servo.perception import windows

    monkeypatch.setattr(windows, "list_windows", lambda: [
        {"hwnd": 1, "title": "Profile 1: Servo Copilot", "process": "dia.exe"},
        {"hwnd": 2, "title": "Servo Copilot", "process": "chrome.exe"},
        {"hwnd": 3, "title": "Servo Copilot", "process": "msedge.exe"},
    ])
    assert copilot_app.find_window("Servo Copilot", timeout=0.2) == 3
    monkeypatch.setattr(windows, "list_windows", lambda: [
        {"hwnd": 1, "title": "Profile 1: Servo Copilot", "process": "dia.exe"},
    ])
    assert copilot_app.find_window("Servo Copilot", timeout=0.2) is None
