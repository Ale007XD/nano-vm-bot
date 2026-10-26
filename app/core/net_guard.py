"""
SSRF-защита для исходящих запросов по URL, пришедшим от пользователей.

Без зависимостей от aiogram/БД — покрывается unit-тестами.

Что делает safe_fetch:
  * только http/https, без user:pass@ в URL;
  * резолвит хост САМ и требует, чтобы ВСЕ адреса были публичными
    (loopback, RFC1918, link-local/169.254.169.254, CGNAT, multicast,
    зарезервированные, а также IPv4, вложенный в IPv6: ::ffff:, 6to4, Teredo, NAT64);
  * соединяется с проверенным IP (pinning), а не с именем — исключает
    DNS rebinding между проверкой и подключением; TLS проверяется по имени
    хоста (sni_hostname), заголовок Host сохраняется;
  * редиректы идут вручную, каждый хоп проходит ту же проверку;
  * trust_env=False: переменные HTTP(S)_PROXY не могут обойти проверку;
  * лимит размера тела и общий дедлайн (против медленных/бесконечных ответов).

ALLOW_PRIVATE_URLS=true в настройках отключает проверку адресов
(мониторинг собственной локальной сети админом). По умолчанию выключено.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional, Union
from urllib.parse import urljoin, urlsplit

import httpx

MAX_REDIRECTS = 5
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
_ALLOWED_SCHEMES = {"http", "https"}
_REDIRECT_CODES = {301, 302, 303, 307, 308}

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
Resolver = Callable[[str, int], Awaitable[List[IPAddress]]]

_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_NAT64_WK = ipaddress.ip_network("64:ff9b::/96")


class UnsafeURLError(ValueError):
    """URL запрещён политикой (схема, адрес, редирект, размер)."""


class FetchError(RuntimeError):
    """Обычная сетевая/HTTP-ошибка при получении страницы."""


def _embedded_v4(ip: ipaddress.IPv6Address) -> List[ipaddress.IPv4Address]:
    out: List[ipaddress.IPv4Address] = []
    if ip.ipv4_mapped:
        out.append(ip.ipv4_mapped)
    if ip.sixtofour:
        out.append(ip.sixtofour)
    if ip.teredo:
        out.extend(ip.teredo)
    if ip in _NAT64_WK:
        out.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return out


def is_public_ip(ip: IPAddress) -> bool:
    """True, только если адрес глобально маршрутизируемый и не multicast."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip in _NAT64_LOCAL:
            return False
        for emb in _embedded_v4(ip):
            if not is_public_ip(emb):
                return False
    return bool(ip.is_global) and not ip.is_multicast


def _allow_private() -> bool:
    try:
        from app.core.config import settings

        return getattr(settings, "ALLOW_PRIVATE_URLS", False) is True
    except Exception:
        return False


async def resolve_host(host: str, port: int) -> List[IPAddress]:
    """Резолв хоста во все его адреса. Литералы IP возвращаются как есть."""
    literal = host.strip("[]")
    try:
        return [ipaddress.ip_address(literal)]
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchError(f"не удалось определить адрес хоста {host!r}: {e}") from e
    seen: Dict[str, IPAddress] = {}
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%")[0])
        seen.setdefault(str(addr), addr)
    return list(seen.values())


@dataclass
class _Target:
    scheme: str
    host: str
    port: int
    ips: List[IPAddress]
    path_query: str
    host_header: str


async def _check(url: str, resolver: Resolver, allow_private: bool) -> _Target:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise UnsafeURLError(f"некорректный URL: {e}") from e

    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"схема {scheme!r} не разрешена (только http/https)")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURLError("логин/пароль в URL не разрешены")
    host = parts.hostname
    if not host:
        raise UnsafeURLError("в URL нет хоста")

    port = port or (443 if scheme == "https" else 80)
    ips = await resolver(host, port)
    if not ips:
        raise FetchError(f"хост {host!r} не резолвится")
    if not allow_private:
        bad = [str(i) for i in ips if not is_public_ip(i)]
        if bad:
            raise UnsafeURLError(
                f"адрес {host!r} указывает на непубличный IP ({bad[0]})"
            )

    default_port = 443 if scheme == "https" else 80
    host_header = host if port == default_port else f"{host}:{port}"
    if ":" in host and not host.startswith("["):
        host_header = f"[{host}]" + ("" if port == default_port else f":{port}")
    path_query = parts.path or "/"
    if parts.query:
        path_query += "?" + parts.query
    return _Target(scheme, host, port, ips, path_query, host_header)


async def assert_public_url(url: str, resolver: Optional[Resolver] = None) -> None:
    """Проверить URL без запроса (для раннего отказа при добавлении)."""
    await _check(url, resolver or resolve_host, _allow_private())


@dataclass
class SafeResponse:
    url: str
    status_code: int
    headers: httpx.Headers
    content: bytes
    truncated: bool
    encoding: str

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="replace")


def _pinned_url(t: _Target, ip: IPAddress) -> str:
    ip_s = f"[{ip}]" if isinstance(ip, ipaddress.IPv6Address) else str(ip)
    return f"{t.scheme}://{ip_s}:{t.port}{t.path_query}"


async def _fetch_hop(
    client: httpx.AsyncClient,
    method: str,
    t: _Target,
    headers: Dict[str, str],
    max_bytes: int,
):
    """Один запрос к проверенному IP. Перебирает адреса при ошибке соединения."""
    last_exc: Optional[Exception] = None
    for ip in t.ips:
        req_headers = {**headers, "Host": t.host_header}
        ext = {"sni_hostname": t.host} if t.scheme == "https" else {}
        try:
            async with client.stream(
                method, _pinned_url(t, ip), headers=req_headers, extensions=ext
            ) as resp:
                body = bytearray()
                truncated = False
                if method != "HEAD" and max_bytes > 0:
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            del body[max_bytes:]
                            truncated = True
                            break
                return (
                    resp.status_code,
                    resp.headers,
                    bytes(body),
                    truncated,
                    resp.charset_encoding or resp.encoding or "utf-8",
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            last_exc = e
            continue
    assert last_exc is not None
    raise last_exc


async def safe_fetch(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 20.0,
    total_timeout: float = 30.0,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = MAX_REDIRECTS,
    resolver: Optional[Resolver] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> SafeResponse:
    """GET/HEAD с SSRF-защитой. Бросает UnsafeURLError / FetchError / httpx.*Error."""
    method = method.upper()
    if method not in ("GET", "HEAD"):
        raise ValueError("safe_fetch поддерживает только GET и HEAD")
    resolver = resolver or resolve_host
    allow_private = _allow_private()
    base_headers = dict(headers or {})

    async def _run() -> SafeResponse:
        current = url
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            for _hop in range(max_redirects + 1):
                target = await _check(current, resolver, allow_private)
                status, hdrs, body, truncated, enc = await _fetch_hop(
                    client, method, target, base_headers, max_bytes
                )
                loc = hdrs.get("location")
                if status in _REDIRECT_CODES and loc:
                    current = urljoin(current, loc)
                    continue
                return SafeResponse(current, status, hdrs, body, truncated, enc)
        raise UnsafeURLError(f"слишком много редиректов (>{max_redirects})")

    try:
        return await asyncio.wait_for(_run(), timeout=total_timeout)
    except asyncio.TimeoutError as e:
        raise FetchError(f"превышено общее время ожидания {total_timeout}s") from e
