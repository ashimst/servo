from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from servo.cognition.system2 import System2, System2Result, System2Unavailable, parse_api_keys
from servo.cognition.trace import Tracer
from servo.config import settings


def test_parse_api_keys():
    assert parse_api_keys(None) == []
    assert parse_api_keys("") == []
    assert parse_api_keys("   ") == []
    assert parse_api_keys("sk-123") == ["sk-123"]
    assert parse_api_keys("sk-123, sk-456,sk-789") == ["sk-123", "sk-456", "sk-789"]
    assert parse_api_keys("k1; k2\nk3\r\nk4") == ["k1", "k2", "k3", "k4"]


def test_failover_switches_to_backup_key_on_failure():
    tr = Tracer(run_id="test-failover")
    s2 = System2(provider="openai", tracer=tr)

    call_history = []

    def mock_openai(system, user, json_mode, api_key="", model="", base_url=""):
        call_history.append((api_key, model, base_url))
        if api_key == "bad_key":
            raise System2Unavailable("429 Rate limit exceeded on bad_key")
        return System2Result(
            raw='{"thought": "fixed", "reply": "ok", "subtasks": [], "done": true}',
            thought="fixed",
            reply="ok",
            provider="openai",
            model=model,
        )

    with patch.object(settings, "openai_api_key", "bad_key,good_key"), \
         patch.object(s2, "_openai", side_effect=mock_openai):
        res = s2._complete("system prompt", "user prompt", json_mode=True)
        assert res.reply == "ok"
        assert len(call_history) == 2
        assert call_history[0][0] == "bad_key"
        assert call_history[1][0] == "good_key"

    # Verify failover trace was emitted
    failover_spans = [e for e in tr.events if "Failover:" in (e.title or "")]
    assert len(failover_spans) >= 1
    assert "bad_key" in str(failover_spans[0]) or "openai" in str(failover_spans[0])


def test_failover_cascades_to_local_default_model_when_cloud_fails():
    tr = Tracer(run_id="test-failover-local")
    s2 = System2(provider="anthropic", tracer=tr)

    def mock_anthropic(system, user, max_tokens, api_key="", model=""):
        raise System2Unavailable("Anthropic API rate limit 429")

    def mock_openai(system, user, json_mode, api_key="", model="", base_url=""):
        assert model == "llama3.2:1b"
        assert "11434" in base_url
        return System2Result(
            raw='{"thought": "local fallback", "reply": "ran locally", "subtasks": [], "done": true}',
            thought="local fallback",
            reply="ran locally",
            provider="openai",
            model=model,
        )

    with patch.object(settings, "anthropic_api_key", "sk-ant-test"), \
         patch.object(settings, "default_local_model", "llama3.2:1b"), \
         patch.object(s2, "_anthropic", side_effect=mock_anthropic), \
         patch.object(s2, "_openai", side_effect=mock_openai):
        res = s2._complete("system", "user", json_mode=True)
        assert res.reply == "ran locally"
        assert res.model == "llama3.2:1b"


def test_all_candidates_failing_raises_informative_error():
    tr = Tracer(run_id="test-failover-err")
    s2 = System2(provider="openai", tracer=tr)

    with patch.object(settings, "openai_api_key", "key1"), \
         patch.object(s2, "_openai", side_effect=System2Unavailable("Connection refused")):
        with pytest.raises(System2Unavailable) as exc:
            s2._complete("system", "user", json_mode=True)
        assert "All System 2 providers/keys in failover pool failed" in str(exc.value)
