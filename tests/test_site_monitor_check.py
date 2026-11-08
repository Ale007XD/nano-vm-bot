"""site_monitor._check_url: HEAD, запрещённый сервером (405/403/…), не должен давать ложное «сайт упал»."""
import importlib.util
import pathlib
import sys
from types import SimpleNamespace

import httpx
import pytest

sys.modules["app.core.config"].settings.BOT_TIMEZONE = "UTC"

from app.core.net_guard import FetchError, UnsafeURLError  # noqa: E402

_PATH = pathlib.Path(__file__).resolve().parents[1] / "skills" / "custom" / "site_monitor.py"
_spec = importlib.util.spec_from_file_location("_site_monitor_under_test", _PATH)
sm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sm)


def _fake(responses):
    """responses: {method: status | Exception}; возвращает (fake_safe_fetch, calls)."""
    calls = []

    async def fake(url, *, method="GET", **kw):
        calls.append((method, kw.get("headers", {}).get("User-Agent")))
        r = responses[method]
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(status_code=r)

    return fake, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("head", [405, 501, 403, 404])
async def test_head_rejected_but_get_ok_is_up(monkeypatch, head):
    fake, calls = _fake({"HEAD": head, "GET": 200})
    monkeypatch.setattr(sm, "safe_fetch", fake)
    ok, status = await sm._check_url("https://example.com/")
    assert ok and status == "HTTP 200"
    assert [c[0] for c in calls] == ["HEAD", "GET"]
    assert all(c[1] for c in calls)  # User-Agent передаётся


@pytest.mark.asyncio
async def test_head_ok_does_not_call_get(monkeypatch):
    fake, calls = _fake({"HEAD": 200})
    monkeypatch.setattr(sm, "safe_fetch", fake)
    assert (await sm._check_url("https://example.com/"))[0] is True
    assert [c[0] for c in calls] == ["HEAD"]


@pytest.mark.asyncio
async def test_really_down_when_get_also_fails(monkeypatch):
    fake, _ = _fake({"HEAD": 405, "GET": 503})
    monkeypatch.setattr(sm, "safe_fetch", fake)
    ok, status = await sm._check_url("https://example.com/")
    assert not ok and status == "HTTP 503"


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [FetchError("таймаут"), httpx.ReadTimeout("t")])
async def test_head_hang_falls_back_to_get(monkeypatch, exc):
    fake, calls = _fake({"HEAD": exc, "GET": 200})
    monkeypatch.setattr(sm, "safe_fetch", fake)
    assert (await sm._check_url("https://example.com/"))[0] is True
    assert [c[0] for c in calls] == ["HEAD", "GET"]


@pytest.mark.asyncio
async def test_connect_error_is_down_after_both(monkeypatch):
    fake, _ = _fake({"HEAD": httpx.ConnectError("x"), "GET": httpx.ConnectError("x")})
    monkeypatch.setattr(sm, "safe_fetch", fake)
    ok, _status = await sm._check_url("https://example.com/")
    assert not ok


@pytest.mark.asyncio
async def test_unsafe_url_is_blocked_without_get(monkeypatch):
    fake, calls = _fake({"HEAD": UnsafeURLError("private")})
    monkeypatch.setattr(sm, "safe_fetch", fake)
    ok, status = await sm._check_url("http://10.0.0.1/")
    assert not ok and "заблокирован" in status
    assert [c[0] for c in calls] == ["HEAD"]
