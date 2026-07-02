"""Tests for the loopback Host-header guard (DNS-rebinding defense).

A remote page whose DNS is rebound to ``127.0.0.1`` becomes *same-origin* with
this app in the victim's browser, so the CORS-preflight argument that protects
the JSON API against ordinary cross-origin pages does not apply. The browser
still sends the attacker's hostname in the ``Host`` header, which is the one
signal the server can check. These tests lock in that guard:

* HTTP requests with a non-loopback ``Host`` are rejected with a 400
  ``invalid_host`` error before reaching any route.
* WebSocket handshakes with a non-loopback ``Host`` are closed with 1008.
* Loopback hosts (``localhost``, ``127.0.0.1``, ``[::1]``) pass through.
* ``GITAUTO_ALLOW_NONLOOPBACK`` (the documented reverse-proxy opt-out)
  disables the guard.

The default ``TestClient`` base URL (``http://testserver``) conveniently plays
the role of the foreign/rebound hostname.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from git_automation.web.app import create_app
from git_automation.web.security import (
    host_header_hostname,
    is_loopback_host,
    nonloopback_allowed,
)

# --- unit: Host-header parsing ---------------------------------------------


@pytest.mark.parametrize(
    ("header", "hostname"),
    [
        ("127.0.0.1:8000", "127.0.0.1"),
        ("127.0.0.1", "127.0.0.1"),
        ("localhost:8000", "localhost"),
        ("LOCALHOST", "localhost"),
        ("[::1]:8000", "::1"),
        ("[::1]", "::1"),
        ("evil.com:8000", "evil.com"),
        ("127.0.0.1.evil.com:8000", "127.0.0.1.evil.com"),
    ],
)
def test_host_header_hostname_parses(header: str, hostname: str) -> None:
    assert host_header_hostname(header) == hostname


@pytest.mark.parametrize("header", [None, "", "bad[host", "[::1", "host:notaport"])
def test_host_header_hostname_rejects_malformed(header: str | None) -> None:
    assert host_header_hostname(header) is None


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "127.0.0.5"])
def test_is_loopback_host_accepts_loopback(host: str) -> None:
    assert is_loopback_host(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.com", "testserver"])
def test_is_loopback_host_rejects_non_loopback(host: str) -> None:
    assert is_loopback_host(host) is False


# --- unit: opt-out env parsing ----------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_nonloopback_allowed_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("GITAUTO_ALLOW_NONLOOPBACK", value)
    assert nonloopback_allowed() is True


@pytest.mark.parametrize("value", [None, "", "0", "false", "off"])
def test_nonloopback_allowed_falsy(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("GITAUTO_ALLOW_NONLOOPBACK", raising=False)
    else:
        monkeypatch.setenv("GITAUTO_ALLOW_NONLOOPBACK", value)
    assert nonloopback_allowed() is False


# --- integration: the guard on the app ---------------------------------------


def test_http_rejects_foreign_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rebound/foreign Host header is rejected before any route runs."""
    monkeypatch.delenv("GITAUTO_ALLOW_NONLOOPBACK", raising=False)
    client = TestClient(create_app(tools=["git"]))  # Host: testserver
    resp = client.get("/health")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_host"


@pytest.mark.parametrize("base", ["http://127.0.0.1", "http://localhost"])
def test_http_allows_loopback_host(monkeypatch: pytest.MonkeyPatch, base: str) -> None:
    monkeypatch.delenv("GITAUTO_ALLOW_NONLOOPBACK", raising=False)
    client = TestClient(create_app(tools=["git"]), base_url=base)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_websocket_rejects_foreign_host(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The guard also covers WebSocket handshakes (closed 1008, no accept)."""
    monkeypatch.delenv("GITAUTO_ALLOW_NONLOOPBACK", raising=False)
    client = TestClient(create_app(tools=["git"]))  # Host: testserver
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/api/watch?path={tmp_path}"):
            pass
    assert exc.value.code == 1008


def test_websocket_allows_loopback_host(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # websocket_connect ignores base_url (hardcoded ws://testserver), so the
    # loopback Host is supplied explicitly -- httpx honors an explicit header.
    monkeypatch.delenv("GITAUTO_ALLOW_NONLOOPBACK", raising=False)
    client = TestClient(create_app(tools=["git"]))
    with client.websocket_connect(
        f"/api/watch?path={tmp_path}", headers={"host": "127.0.0.1"}
    ):
        pass  # handshake accepted; the route takes over from here


def test_opt_out_disables_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """GITAUTO_ALLOW_NONLOOPBACK=1 (reverse-proxy mode) skips the Host check."""
    monkeypatch.setenv("GITAUTO_ALLOW_NONLOOPBACK", "1")
    client = TestClient(create_app(tools=["git"]))  # Host: testserver
    resp = client.get("/health")
    assert resp.status_code == 200
