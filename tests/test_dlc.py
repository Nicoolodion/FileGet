from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from plex_get import dlc


SAMPLE_DLC = Path(__file__).resolve().parent.parent / "3f3735e4f810a20c11b3c1c70a607b018a1c0806.dlc"


class _MockTransport(httpx.BaseTransport):
    def __init__(self, response_body: str) -> None:
        self._body = response_body
        self.calls: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return httpx.Response(200, text=self._body)


def test_parse_input_plain_urls() -> None:
    raw = "https://example.com/a\nhttps://example.com/b\nnot a url\n"
    assert dlc.parse_input(raw) == ["https://example.com/a", "https://example.com/b"]


def test_parse_input_dedup() -> None:
    raw = "https://example.com/a https://example.com/a\nhttps://example.com/b\n"
    assert dlc.parse_input(raw) == ["https://example.com/a", "https://example.com/b"]


def test_parse_dlc_text_via_service(monkeypatch: pytest.MonkeyPatch) -> None:
    body = '{"success": {"links": ["https://h1.example/a", "https://h2.example/b"]}}'
    transport = _MockTransport(body)
    monkeypatch.setattr(dlc, "_client", lambda: httpx.Client(transport=transport, timeout=10.0))
    urls = dlc.parse_dlc_text("any bytes")
    assert urls == ["https://h1.example/a", "https://h2.example/b"]
    assert len(transport.calls) == 1
    assert str(transport.calls[0].url).endswith("/decrypt/paste")


def test_parse_dlc_text_strips_textarea_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    body = '<textarea>{"success": {"links": ["https://h1.example/a"]}}</textarea>'
    transport = _MockTransport(body)
    monkeypatch.setattr(dlc, "_client", lambda: httpx.Client(transport=transport, timeout=10.0))
    assert dlc.parse_dlc_text("ignored") == ["https://h1.example/a"]


def test_parse_dlc_text_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    body = '{"form_errors": {"dlcfile": ["invalid container"]}}'
    transport = _MockTransport(body)
    monkeypatch.setattr(dlc, "_client", lambda: httpx.Client(transport=transport, timeout=10.0))
    with pytest.raises(dlc.DLCDecodeError):
        dlc.parse_dlc_text("ignored")


def test_parse_dlc_file_via_service(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dlc_file = tmp_path / "sample.dlc"
    dlc_file.write_bytes(b"binary-content")
    body = '{"success": {"links": ["https://example.com/x"]}}'
    transport = _MockTransport(body)
    monkeypatch.setattr(dlc, "_client", lambda: httpx.Client(transport=transport, timeout=10.0))
    assert dlc.parse_dlc_file(dlc_file) == ["https://example.com/x"]
    assert transport.calls and transport.calls[0].method == "POST"
    assert str(transport.calls[0].url).endswith("/decrypt/upload")


@pytest.mark.skipif(not SAMPLE_DLC.exists(), reason="sample dlc file not present")
def test_user_sample_dlc_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs the user's real .dlc file through the network path. Asserts it does not raise locally when the service responds."""
    body = '{"success": {"links": ["https://example.com/1", "https://example.com/2"]}}'
    transport = _MockTransport(body)
    monkeypatch.setattr(dlc, "_client", lambda: httpx.Client(transport=transport, timeout=10.0))
    text = SAMPLE_DLC.read_text(encoding="utf-8", errors="replace")
    urls = dlc.parse_dlc_text(text)
    assert urls == ["https://example.com/1", "https://example.com/2"]
