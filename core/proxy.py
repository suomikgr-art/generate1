# Credits: TSun x Kittens
"""
core/proxy.py
~~~~~~~~~~~~~
Thread-safe proxy loading, round-robin rotation, and alive checking.

Supported proxies.txt formats:
  - ip:port
  - ip:port:username:password
  - username:password:ip:port
  - ip:port@username:password
  - username:password@ip:port
  - http://username:password@ip:port
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import os
import threading
import time
from typing import Callable
from urllib.parse import quote, urlsplit

import requests

import config.settings as settings


PROXY_FILE = os.path.join(settings.PROJECT_ROOT, "proxies.txt")
OFFLINE_PROXY_FILE = os.path.join(settings.PROJECT_ROOT, "offline_proxies.txt")
PROXY_TEST_URL = "https://100067.connect.garena.com"
PROXY_TEST_TIMEOUT = 8


@dataclass(frozen=True)
class ProxyEntry:
    """A normalized proxy entry safe to pass into requests."""

    url: str
    label: str
    source: str

    def as_requests_proxies(self) -> dict[str, str]:
        return {
            "http": self.url,
            "https": self.url,
        }


@dataclass(frozen=True)
class ProxyCheckResult:
    proxy: ProxyEntry
    is_alive: bool
    message: str
    elapsed: float


@dataclass(frozen=True)
class ProxyCheckSummary:
    alive: list[ProxyCheckResult]
    dead: list[ProxyCheckResult]
    invalid_lines: list[str]
    proxy_file: str
    offline_file: str


_LOCK = threading.Lock()
_PROXIES: list[ProxyEntry] | None = None
_INVALID_LINES: list[str] = []
_NEXT_INDEX = 0


def _clean_proxy_value(raw_value: str) -> str:
    value = raw_value.strip()
    while value.endswith(","):
        value = value[:-1].strip()
    return value


def _split_scheme(value: str) -> tuple[str, str] | None:
    if "://" not in value:
        return "http", value

    scheme, body = value.split("://", 1)
    scheme = scheme.lower()
    if scheme not in ("http", "https") or not body:
        return None
    return scheme, body


def _masked_label_from_url(proxy_url: str) -> str | None:
    parsed = urlsplit(proxy_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        return None
    return f"{parsed.hostname}:{port}"


def _from_url(proxy_url: str, source: str) -> ProxyEntry | None:
    label = _masked_label_from_url(proxy_url)
    if not label:
        return None
    return ProxyEntry(url=proxy_url, label=label, source=source)


def _build_proxy(
    scheme: str,
    host: str,
    port: str,
    source: str,
    username: str | None = None,
    password: str | None = None,
) -> ProxyEntry | None:
    if not _is_valid_host_port(host, port):
        return None

    if username is None and password is None:
        return ProxyEntry(url=f"{scheme}://{host}:{port}", label=f"{host}:{port}", source=source)

    if not username or not password:
        return None

    safe_user = quote(username, safe="")
    safe_password = quote(password, safe="")
    return ProxyEntry(
        url=f"{scheme}://{safe_user}:{safe_password}@{host}:{port}",
        label=f"{host}:{port}",
        source=source,
    )


def _parse_at_format(scheme: str, body: str, source: str) -> ProxyEntry | None:
    left, right = body.rsplit("@", 1)

    # username:password@ip:port
    right_parts = right.split(":")
    if len(right_parts) == 2 and _is_valid_host_port(right_parts[0], right_parts[1]):
        user_parts = left.split(":", 1)
        if len(user_parts) == 2:
            return _build_proxy(
                scheme,
                right_parts[0],
                right_parts[1],
                source,
                user_parts[0],
                user_parts[1],
            )

    # ip:port@username:password
    host_parts = left.split(":")
    auth_parts = right.split(":", 1)
    if len(host_parts) == 2 and len(auth_parts) == 2:
        return _build_proxy(
            scheme,
            host_parts[0],
            host_parts[1],
            source,
            auth_parts[0],
            auth_parts[1],
        )

    return None


def _parse_colon_format(scheme: str, body: str, source: str) -> ProxyEntry | None:
    parts = body.split(":")

    # ip:port
    if len(parts) == 2:
        return _build_proxy(scheme, parts[0], parts[1], source)

    # ip:port:username:password
    if len(parts) >= 4 and _is_valid_host_port(parts[0], parts[1]):
        return _build_proxy(scheme, parts[0], parts[1], source, parts[2], ":".join(parts[3:]))

    # username:password:ip:port
    if len(parts) >= 4 and _is_valid_host_port(parts[-2], parts[-1]):
        username = parts[0]
        password = ":".join(parts[1:-2])
        return _build_proxy(scheme, parts[-2], parts[-1], source, username, password)

    return None


def _parse_proxy_value(raw_value: str) -> ProxyEntry | None:
    source = _clean_proxy_value(raw_value)
    if not source or source.startswith("#"):
        return None

    split_value = _split_scheme(source)
    if not split_value:
        return None

    scheme, body = split_value

    if "@" in body:
        proxy = _parse_at_format(scheme, body, source)
        if proxy:
            return proxy

    proxy = _parse_colon_format(scheme, body, source)
    if proxy:
        return proxy

    if "://" in source:
        return _from_url(source, source)

    return None


def _parse_proxy_line(raw_line: str) -> ProxyEntry | None:
    return _parse_proxy_value(raw_line)


def _is_valid_host_port(host: str, port: str) -> bool:
    if not host or not port.isdigit():
        return False
    port_number = int(port)
    return 1 <= port_number <= 65535


def _iter_proxy_values(raw_line: str) -> list[str]:
    stripped = raw_line.strip()
    if not stripped or stripped.startswith("#"):
        return []
    return [part.strip() for part in stripped.split(",") if part.strip()]


def _read_proxy_file_details() -> tuple[list[ProxyEntry], list[str]]:
    if not os.path.exists(PROXY_FILE):
        return [], []

    proxies: list[ProxyEntry] = []
    invalid_lines: list[str] = []
    seen_sources: set[str] = set()

    with open(PROXY_FILE, "r", encoding="utf-8") as file:
        for raw_line in file:
            for value in _iter_proxy_values(raw_line):
                proxy = _parse_proxy_value(value)
                if proxy:
                    if proxy.source not in seen_sources:
                        proxies.append(proxy)
                        seen_sources.add(proxy.source)
                else:
                    invalid_lines.append(value)

    return proxies, invalid_lines


def _read_proxy_file() -> tuple[list[ProxyEntry], int]:
    proxies, invalid_lines = _read_proxy_file_details()
    return proxies, len(invalid_lines)


def refresh_proxy_pool() -> tuple[int, int, str]:
    """
    Reload proxies.txt and reset rotation.
    Returns (valid_count, invalid_count, proxy_file_path).
    """
    global _PROXIES, _INVALID_LINES, _NEXT_INDEX

    with _LOCK:
        _PROXIES, _INVALID_LINES = _read_proxy_file_details()
        _NEXT_INDEX = 0
        return len(_PROXIES), len(_INVALID_LINES), PROXY_FILE


def get_proxy_stats() -> tuple[int, int, str]:
    """Return current proxy pool stats, loading the file lazily if needed."""
    global _PROXIES, _INVALID_LINES

    with _LOCK:
        if _PROXIES is None:
            _PROXIES, _INVALID_LINES = _read_proxy_file_details()
        return len(_PROXIES), len(_INVALID_LINES), PROXY_FILE


def get_next_proxy() -> ProxyEntry | None:
    """Return the next proxy in round-robin order, or None for direct mode."""
    global _PROXIES, _INVALID_LINES, _NEXT_INDEX

    with _LOCK:
        if _PROXIES is None:
            _PROXIES, _INVALID_LINES = _read_proxy_file_details()

        if not _PROXIES:
            return None

        proxy = _PROXIES[_NEXT_INDEX % len(_PROXIES)]
        _NEXT_INDEX += 1
        return proxy


def _short_proxy_error(exc: Exception) -> str:
    if isinstance(exc, requests.exceptions.ProxyError):
        text = str(exc)
        if "407" in text:
            return "proxy auth failed (407)"
        return "proxy connection failed"
    if isinstance(exc, requests.exceptions.Timeout):
        return "proxy timed out"
    if isinstance(exc, requests.exceptions.SSLError):
        return "proxy SSL tunnel failed"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "proxy connection error"
    return exc.__class__.__name__


def check_proxy_alive(proxy: ProxyEntry, timeout: int = PROXY_TEST_TIMEOUT) -> ProxyCheckResult:
    started = time.perf_counter()
    try:
        response = requests.get(
            PROXY_TEST_URL,
            headers={"User-Agent": "TSun-Proxy-Checker/1.0"},
            proxies=proxy.as_requests_proxies(),
            timeout=timeout,
            verify=False,
        )
        elapsed = time.perf_counter() - started
        if response.status_code == 407:
            return ProxyCheckResult(proxy, False, "proxy auth failed (407)", elapsed)
        return ProxyCheckResult(proxy, True, f"HTTP {response.status_code}", elapsed)
    except requests.exceptions.RequestException as exc:
        elapsed = time.perf_counter() - started
        return ProxyCheckResult(proxy, False, _short_proxy_error(exc), elapsed)


def _write_proxy_sources(path: str, sources: list[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as file:
        if sources:
            file.write("\n".join(sources) + "\n")


def _read_existing_sources(path: str) -> list[str]:
    if not os.path.exists(path):
        return []

    sources: list[str] = []
    with open(path, "r", encoding="utf-8") as file:
        for raw_line in file:
            value = _clean_proxy_value(raw_line)
            if value and not value.startswith("#"):
                sources.append(value)
    return sources


def _merge_unique_sources(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for source in group:
            if source not in seen:
                merged.append(source)
                seen.add(source)
    return merged


def save_proxy_check_results(
    alive: list[ProxyCheckResult],
    dead: list[ProxyCheckResult],
    invalid_lines: list[str],
) -> tuple[str, str]:
    """Keep alive proxies in proxies.txt and move dead/invalid lines offline."""
    alive_sources = [result.proxy.source for result in alive]
    dead_sources = [result.proxy.source for result in dead] + invalid_lines
    offline_sources = _merge_unique_sources(_read_existing_sources(OFFLINE_PROXY_FILE), dead_sources)

    _write_proxy_sources(PROXY_FILE, alive_sources)
    _write_proxy_sources(OFFLINE_PROXY_FILE, offline_sources)
    refresh_proxy_pool()
    return PROXY_FILE, OFFLINE_PROXY_FILE


def check_proxy_pool(
    timeout: int = PROXY_TEST_TIMEOUT,
    max_workers: int | None = None,
    on_result: Callable[[ProxyCheckResult, int, int], None] | None = None,
) -> ProxyCheckSummary:
    proxies, invalid_lines = _read_proxy_file_details()
    if not proxies:
        save_proxy_check_results([], [], invalid_lines)
        return ProxyCheckSummary([], [], invalid_lines, PROXY_FILE, OFFLINE_PROXY_FILE)

    workers = max_workers or min(50, len(proxies))
    workers = max(1, min(workers, len(proxies)))

    alive: list[ProxyCheckResult] = []
    dead: list[ProxyCheckResult] = []
    completed = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(check_proxy_alive, proxy, timeout) for proxy in proxies]
        for future in as_completed(futures):
            result = future.result()
            completed += 1
            if result.is_alive:
                alive.append(result)
            else:
                dead.append(result)
            if on_result:
                on_result(result, completed, len(proxies))

    save_proxy_check_results(alive, dead, invalid_lines)
    return ProxyCheckSummary(alive, dead, invalid_lines, PROXY_FILE, OFFLINE_PROXY_FILE)
