"""
SSRF-защита (app/core/net_guard.py) и её применение в knowledge_base / site_monitor.
Сеть и DNS не используются: резолвер и транспорт подменяются.
"""
import importlib.util
import ipaddress
import os
import sys

import httpx
import pytest

from app.core.net_guard import (
    FetchError,
    UnsafeURLError,
    assert_public_url,
    is_public_ip,
    safe_fetch,
)

PUBLIC = ipaddress.ip_address("93.184.216.34")


def _resolver(mapping):
    async def r(host, port):
        try:  # как настоящий резолвер: литеральные IP возвращаются как есть
            return [ipaddress.ip_address(host.strip("[]"))]
        except ValueError:
            pass
        v = mapping[host]
        return [ipaddress.ip_address(x) for x in (v if isinstance(v, list) else [v])]
    return r


@pytest.fixture(autouse=True)
def _no_private_override(monkeypatch):
    monkeypatch.setattr(sys.modules["app.core.config"].settings, "ALLOW_PRIVATE_URLS", False)


class TestIsPublicIp:
    @pytest.mark.parametrize("ip", [
        "127.0.0.1", "127.1.2.3", "10.0.0.1", "172.16.5.5", "172.31.255.255",
        "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
        "224.0.0.1", "255.255.255.255", "192.0.0.1", "198.18.0.1",
        "::1", "::", "fe80::1", "fc00::1", "fd12:3456::1", "ff02::1",
        "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254",
        "64:ff9b::7f00:1", "64:ff9b::a00:1", "64:ff9b:1::1",
        "2002:7f00:1::", "2002:a9fe:a9fe::",
    ])
    def test_blocked(self, ip):
        assert not is_public_ip(ipaddress.ip_address(ip))

    @pytest.mark.parametrize("ip", [
        "8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111",
        "::ffff:8.8.8.8",
    ])
    def test_allowed(self, ip):
        assert is_public_ip(ipaddress.ip_address(ip))


class TestUrlValidation:
    @pytest.mark.parametrize("url", [
        "file:///etc/passwd", "ftp://example.com/x", "gopher://example.com",
        "javascript:alert(1)", "//example.com/x", "example.com", "http://",
        "http://user:pass@example.com/", "http://@example.com/",
        "http://example.com:99999/",
    ])
    @pytest.mark.asyncio
    async def test_rejected(self, url):
        with pytest.raises(UnsafeURLError):
            await assert_public_url(url, _resolver({"example.com": "93.184.216.34"}))

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/", "http://127.0.0.1:8000/admin", "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/", "http://10.1.2.3/",
        "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/",
    ])
    @pytest.mark.asyncio
    async def test_literal_private_ips_rejected(self, url):
        with pytest.raises(UnsafeURLError):
            await assert_public_url(url)

    @pytest.mark.asyncio
    async def test_hostname_resolving_to_private_rejected(self):
        with pytest.raises(UnsafeURLError):
            await assert_public_url("http://evil.test/", _resolver({"evil.test": "10.0.0.5"}))

    @pytest.mark.asyncio
    async def test_mixed_answers_rejected(self):
        """Один публичный + один приватный A-запись — отказ (rebinding-приём)."""
        r = _resolver({"evil.test": ["93.184.216.34", "127.0.0.1"]})
        with pytest.raises(UnsafeURLError):
            await assert_public_url("http://evil.test/", r)

    @pytest.mark.asyncio
    async def test_public_ok(self):
        await assert_public_url("https://example.com/p?q=1", _resolver({"example.com": "93.184.216.34"}))


class TestSafeFetch:
    @pytest.mark.asyncio
    async def test_pins_ip_keeps_host_and_sni(self):
        seen = []

        def handler(req: httpx.Request):
            seen.append(req)
            return httpx.Response(200, text="hello")

        r = await safe_fetch(
            "https://example.com/a?b=1",
            resolver=_resolver({"example.com": "93.184.216.34"}),
            transport=httpx.MockTransport(handler),
        )
        assert r.status_code == 200 and r.text == "hello"
        req = seen[0]
        assert req.url.host == "93.184.216.34"
        assert req.headers["host"] == "example.com"
        assert req.extensions.get("sni_hostname") == "example.com"
        assert req.url.path == "/a" and req.url.query == b"b=1"

    @pytest.mark.asyncio
    async def test_nondefault_port_in_host_header(self):
        seen = []
        await safe_fetch(
            "http://example.com:8080/",
            resolver=_resolver({"example.com": "93.184.216.34"}),
            transport=httpx.MockTransport(lambda q: (seen.append(q), httpx.Response(200))[1]),
        )
        assert seen[0].headers["host"] == "example.com:8080"
        assert seen[0].url.port == 8080

    @pytest.mark.asyncio
    async def test_redirect_to_private_blocked_and_never_requested(self):
        calls = []

        def handler(req):
            calls.append(str(req.url))
            return httpx.Response(302, headers={"location": "http://metadata.test/latest"})

        r = _resolver({"example.com": "93.184.216.34", "metadata.test": "169.254.169.254"})
        with pytest.raises(UnsafeURLError):
            await safe_fetch("http://example.com/", resolver=r, transport=httpx.MockTransport(handler))
        assert len(calls) == 1  # на внутренний адрес запрос не ушёл

    @pytest.mark.asyncio
    @pytest.mark.parametrize("loc", [
        "file:///etc/passwd", "http://127.0.0.1/", "http://[::1]:6379/",
        "gopher://example.com/_x", "http://u:p@example.com/",
    ])
    async def test_redirect_to_bad_targets_blocked(self, loc):
        def handler(req):
            return httpx.Response(301, headers={"location": loc})

        with pytest.raises(UnsafeURLError):
            await safe_fetch(
                "http://example.com/",
                resolver=_resolver({"example.com": "93.184.216.34"}),
                transport=httpx.MockTransport(handler),
            )

    @pytest.mark.asyncio
    async def test_relative_redirect_followed(self):
        def handler(req):
            if req.url.path == "/a":
                return httpx.Response(302, headers={"location": "/b"})
            return httpx.Response(200, text="done")

        r = await safe_fetch(
            "http://example.com/a",
            resolver=_resolver({"example.com": "93.184.216.34"}),
            transport=httpx.MockTransport(handler),
        )
        assert r.text == "done" and r.url.endswith("/b")

    @pytest.mark.asyncio
    async def test_redirect_loop_limited(self):
        n = []

        def handler(req):
            n.append(1)
            return httpx.Response(302, headers={"location": "/again"})

        with pytest.raises(UnsafeURLError, match="редирект"):
            await safe_fetch(
                "http://example.com/",
                resolver=_resolver({"example.com": "93.184.216.34"}),
                transport=httpx.MockTransport(handler),
                max_redirects=3,
            )
        assert len(n) == 4

    @pytest.mark.asyncio
    async def test_dns_rebinding_second_lookup_not_used(self):
        """Резолв один раз на хоп: подключение идёт к проверенному IP,
        даже если повторный резолв вернул бы приватный адрес."""
        answers = iter([["93.184.216.34"], ["127.0.0.1"]])

        async def flipping(host, port):
            return [ipaddress.ip_address(x) for x in next(answers)]

        seen = []
        await safe_fetch(
            "http://rebind.test/",
            resolver=flipping,
            transport=httpx.MockTransport(lambda q: (seen.append(q), httpx.Response(200))[1]),
        )
        assert seen[0].url.host == "93.184.216.34"

    @pytest.mark.asyncio
    async def test_body_size_capped(self):
        r = await safe_fetch(
            "http://example.com/",
            resolver=_resolver({"example.com": "93.184.216.34"}),
            transport=httpx.MockTransport(lambda q: httpx.Response(200, content=b"x" * 10_000)),
            max_bytes=1000,
        )
        assert len(r.content) == 1000 and r.truncated

    @pytest.mark.asyncio
    async def test_head_has_no_body(self):
        methods = []
        r = await safe_fetch(
            "http://example.com/", method="HEAD", max_bytes=0,
            resolver=_resolver({"example.com": "93.184.216.34"}),
            transport=httpx.MockTransport(lambda q: (methods.append(q.method), httpx.Response(204))[1]),
        )
        assert methods == ["HEAD"] and r.status_code == 204 and r.content == b""

    @pytest.mark.asyncio
    async def test_post_not_supported(self):
        with pytest.raises(ValueError):
            await safe_fetch("http://example.com/", method="POST")

    @pytest.mark.asyncio
    async def test_unresolvable_host_is_fetch_error(self):
        async def nxdomain(host, port):
            raise FetchError("no such host")

        with pytest.raises(FetchError):
            await safe_fetch("http://nope.test/", resolver=nxdomain)

    @pytest.mark.asyncio
    async def test_allow_private_override(self, monkeypatch):
        monkeypatch.setattr(sys.modules["app.core.config"].settings, "ALLOW_PRIVATE_URLS", True)
        r = await safe_fetch(
            "http://intranet.test/",
            resolver=_resolver({"intranet.test": "192.168.1.10"}),
            transport=httpx.MockTransport(lambda q: httpx.Response(200, text="lan")),
        )
        assert r.text == "lan"

    @pytest.mark.asyncio
    async def test_override_requires_literal_true(self, monkeypatch):
        """Мок/truthy-значение (не True) не отключает защиту."""
        monkeypatch.setattr(sys.modules["app.core.config"].settings, "ALLOW_PRIVATE_URLS", "yes")
        with pytest.raises(UnsafeURLError):
            await safe_fetch(
                "http://intranet.test/",
                resolver=_resolver({"intranet.test": "192.168.1.10"}),
                transport=httpx.MockTransport(lambda q: httpx.Response(200)),
            )


def _load_skill(name):
    path = os.path.join("skills", "custom", f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"_t_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSkillsIntegration:
    @pytest.mark.asyncio
    async def test_site_monitor_check_blocks_internal(self):
        sm = _load_skill("site_monitor")
        ok, status = await sm._check_url("http://169.254.169.254/latest/meta-data/")
        assert ok is False and "заблокирован" in status

    def test_site_monitor_url_regex_matches_real_urls(self):
        """Регрессия: старый regex r'https?://[^\\s)]>' не находил обычные URL."""
        sm = _load_skill("site_monitor")
        assert sm._extract_urls("мониторь https://example.com/a?b=1, пожалуйста") == [
            "https://example.com/a?b=1"
        ]
        assert sm._extract_urls("(http://a.example/x)") == ["http://a.example/x"]
        assert sm._extract_urls("нет ссылок") == []

    @pytest.mark.asyncio
    async def test_site_monitor_add_rejects_internal(self):
        sm = _load_skill("site_monitor")
        out = await sm.run({"user_id": 5, "message_text": "http://127.0.0.1:8000/admin"})
        assert "⛔" in out
        assert 5 not in sm._pending_adds

    @pytest.mark.asyncio
    async def test_knowledge_base_fetch_blocks_internal(self):
        kb = _load_skill("knowledge_base")
        with pytest.raises(UnsafeURLError):
            await kb._fetch_page("http://169.254.169.254/latest/meta-data/")

    @pytest.mark.asyncio
    async def test_knowledge_base_add_reports_refusal_not_crash(self, tmp_path, monkeypatch):
        kb = _load_skill("knowledge_base")
        monkeypatch.setattr(kb, "_DB_PATH", str(tmp_path / "kb.db"))
        out = await kb._cmd_add("http://127.0.0.1:6379/", 1, "", llm_client=None)
        assert "⛔" in out
