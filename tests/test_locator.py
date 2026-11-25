"""Vision locator client: Qwen3.5-4B with its vision projector behind llama-server."""

import httpx
import pytest
from PIL import Image

from servo.perception import locator

# The real client, captured before conftest replaces it for each test.
_REAL_LOCATE = locator.locate


def _serve(monkeypatch, handler):
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_point_is_scaled_from_the_0_1000_grid(monkeypatch):
    seen = {}

    def handler(request):
        import json
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        answer = '```json\n[{"point_2d": [500, 250], "label": "Save"}]\n```'
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})
    _serve(monkeypatch, handler)
    x, y, ms = _REAL_LOCATE(Image.new("RGB", (200, 100)), "Save button")
    assert (x, y) == (100, 25) and ms >= 0
    assert seen["url"].endswith("/chat/completions")
    content = seen["body"]["messages"][0]["content"]
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,") and "Save button" in content[1]["text"]


def test_answer_without_a_point_is_unavailable(monkeypatch):
    _serve(monkeypatch, lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "I can't see it"}}]}))
    with pytest.raises(locator.LocatorUnavailable, match="no point"):
        _REAL_LOCATE(Image.new("RGB", (10, 10)), "x")


def test_server_errors_are_unavailable(monkeypatch):
    _serve(monkeypatch, lambda r: httpx.Response(503, json={}))
    with pytest.raises(locator.LocatorUnavailable, match="failed"):
        _REAL_LOCATE(Image.new("RGB", (10, 10)), "x")


def test_parse_point_reads_the_coordinates_not_the_key_name():
    assert locator.parse_point('[{"point_2d": [976, 22]}]') == (976.0, 22.0)
    assert locator.parse_point('{"bbox_2d": [100, 200, 300, 400]}') == (200.0, 300.0)
    assert locator.parse_point("no idea") is None
    assert locator.parse_point('{"point_2d": [1500, 20]}') is None   # off the 0-1000 grid
