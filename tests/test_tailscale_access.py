"""Tests for the tailnet trust extension (Tailscale bind + Host/Origin guards).

The app's baseline trust model is loopback-only; the operator's own Tailscale
tailnet is the one deliberate extension (every tailnet peer is a device they
enrolled). These tests lock in that extension end to end:

* ``tailscale status --json`` parsing degrades to ``None`` on anything
  unexpected, and only ever yields IPs inside the Tailscale ranges.
* ``is_tailscale_ip`` / ``is_trusted_host`` accept Tailscale-range literals and
  this machine's own MagicDNS names -- and nothing else (other machines'
  ``.ts.net`` names and arbitrary hostnames stay rejected, preserving the
  DNS-rebinding and WebSocket-hijacking defenses).
* The Host guard and the WS Origin guard honor the same trusted set.
* ``GITAUTO_HOST`` parses as a comma-separated list, the ``tailscale`` token
  expands to detected IPs, and the unset default binds loopback + tailnet.
* ``main()`` dual-binds by handing one pre-bound socket per host to a single
  uvicorn server (and to the reload supervisor in reload mode).

Tailscale detection is always pinned (``fake_tailnet`` / ``no_tailnet``) so the
suite behaves identically with or without a real tailscaled running.
"""

from __future__ import annotations

import sys
import types

import pytest
from fastapi.testclient import TestClient

from git_automation.web import __main__ as web_main
from git_automation.web import config, tailscale
from git_automation.web.app import create_app
from git_automation.web.security import is_trusted_host
from git_automation.web.tailscale import TailnetIdentity, _parse_status, is_tailscale_ip
from git_automation.web.ws import origin_allowed

_IDENTITY = TailnetIdentity(
    ips=("100.80.90.10", "fd7a:115c:a1e0::aa11:bb22"),
    dns_names=("gitbox.tail1234.ts.net", "gitbox"),
)

_RUNNING_STATUS = """
{
  "BackendState": "Running",
  "TailscaleIPs": ["100.80.90.10", "fd7a:115c:a1e0::aa11:bb22"],
  "Self": {"HostName": "GitBox", "DNSName": "gitbox.tail1234.ts.net."}
}
"""


@pytest.fixture()
def fake_tailnet(monkeypatch: pytest.MonkeyPatch) -> TailnetIdentity:
    """Pin detection to a running tailnet (both guards and the entry point)."""
    monkeypatch.setattr(tailscale, "self_identity", lambda: _IDENTITY)
    return _IDENTITY


@pytest.fixture()
def no_tailnet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin detection to "no tailnet" (loopback-only baseline)."""
    monkeypatch.setattr(tailscale, "self_identity", lambda: None)


# --- unit: status parsing -----------------------------------------------------


def test_parse_status_running_yields_ips_and_dns_names() -> None:
    identity = _parse_status(_RUNNING_STATUS)
    assert identity == _IDENTITY


def test_parse_status_short_name_is_first_dns_label() -> None:
    identity = _parse_status(_RUNNING_STATUS)
    assert identity is not None
    assert identity.dns_names == ("gitbox.tail1234.ts.net", "gitbox")


@pytest.mark.parametrize(
    "raw",
    [
        "",  # empty output
        "not json",
        "[]",  # JSON but not an object
        '{"TailscaleIPs": ["100.80.90.10"]}',  # no BackendState
        '{"BackendState": "Stopped", "TailscaleIPs": ["100.80.90.10"]}',
        '{"BackendState": "Running", "TailscaleIPs": []}',  # no addresses
        '{"BackendState": "Running", "TailscaleIPs": "100.80.90.10"}',  # not a list
    ],
)
def test_parse_status_degrades_to_none(raw: str) -> None:
    assert _parse_status(raw) is None


def test_parse_status_filters_non_tailscale_ips() -> None:
    """CLI output can never smuggle a non-Tailscale address into the trust set."""
    raw = (
        '{"BackendState": "Running",'
        ' "TailscaleIPs": ["192.168.1.5", "100.80.90.10", 42],'
        ' "Self": {"DNSName": ""}}'
    )
    identity = _parse_status(raw)
    assert identity is not None
    assert identity.ips == ("100.80.90.10",)
    assert identity.dns_names == ()


def test_self_identity_none_when_cli_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("tailscale not on PATH")

    monkeypatch.setattr(tailscale.subprocess, "run", boom)
    tailscale.self_identity.cache_clear()
    try:
        assert tailscale.self_identity() is None
    finally:
        tailscale.self_identity.cache_clear()


# --- unit: the trusted-host predicate ------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "100.64.0.0",  # bottom of the CGNAT block
        "100.80.90.10",
        "100.127.255.255",  # top of the CGNAT block
        "fd7a:115c:a1e0::aa11:bb22",
        "[fd7a:115c:a1e0::aa11:bb22]",  # bracketed, as in a Host header
    ],
)
def test_is_tailscale_ip_accepts_tailscale_ranges(host: str) -> None:
    assert is_tailscale_ip(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "100.63.255.255",  # just below the CGNAT block
        "100.128.0.0",  # just above the CGNAT block
        "fd7a:115c:a1e1::1",  # outside the /48
        "192.168.1.10",
        "127.0.0.1",  # loopback is trusted, but it is not a *Tailscale* IP
        "gitbox.tail1234.ts.net",  # hostnames are never Tailscale IPs
    ],
)
def test_is_tailscale_ip_rejects_everything_else(host: str) -> None:
    assert is_tailscale_ip(host) is False


@pytest.mark.parametrize(
    "host",
    ["localhost", "127.0.0.1", "100.80.90.10", "gitbox.tail1234.ts.net", "GITBOX"],
)
def test_is_trusted_host_accepts_loopback_and_own_tailnet(
    fake_tailnet: TailnetIdentity, host: str
) -> None:
    assert is_trusted_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "evil.com",
        "phone.other-tailnet.ts.net",  # someone else's tailnet name
        "gitbox.tail1234.ts.net.evil.com",  # suffix spoof
        "192.168.1.10",
    ],
)
def test_is_trusted_host_rejects_foreign_names(fake_tailnet: TailnetIdentity, host: str) -> None:
    assert is_trusted_host(host) is False


def test_is_trusted_host_without_tailnet_is_loopback_only(no_tailnet: None) -> None:
    assert is_trusted_host("127.0.0.1") is True
    assert is_trusted_host("gitbox.tail1234.ts.net") is False
    # Range check is syntactic, so a literal Tailscale IP stays trusted even
    # when detection fails -- it still only routes over a tailnet.
    assert is_trusted_host("100.80.90.10") is True


# --- integration: the Host guard ------------------------------------------------


@pytest.mark.parametrize("base", ["http://100.80.90.10:8000", "http://gitbox.tail1234.ts.net:8000"])
def test_http_allows_own_tailnet_host(
    monkeypatch: pytest.MonkeyPatch, fake_tailnet: TailnetIdentity, base: str
) -> None:
    monkeypatch.delenv("GITAUTO_ALLOW_NONLOOPBACK", raising=False)
    client = TestClient(create_app(tools=["git"]), base_url=base)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_http_still_rejects_foreign_host_with_tailnet(
    monkeypatch: pytest.MonkeyPatch, fake_tailnet: TailnetIdentity
) -> None:
    monkeypatch.delenv("GITAUTO_ALLOW_NONLOOPBACK", raising=False)
    client = TestClient(create_app(tools=["git"]), base_url="http://phone.other-tailnet.ts.net")
    resp = client.get("/health")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_host"


def test_websocket_allows_tailscale_host(
    monkeypatch: pytest.MonkeyPatch, fake_tailnet: TailnetIdentity, tmp_path
) -> None:
    # websocket_connect ignores base_url, so the Host is supplied explicitly
    # (same technique as the loopback test in test_host_guard.py).
    monkeypatch.delenv("GITAUTO_ALLOW_NONLOOPBACK", raising=False)
    client = TestClient(create_app(tools=["git"]))
    with client.websocket_connect(f"/api/watch?path={tmp_path}", headers={"host": "100.80.90.10"}):
        pass  # handshake accepted; the route takes over from here


# --- integration: the WS Origin guard -------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [
        "http://100.80.90.10:8000",
        "http://gitbox.tail1234.ts.net:8000",
        "http://gitbox:8000",  # short MagicDNS name
    ],
)
def test_origin_allowed_accepts_own_tailnet(fake_tailnet: TailnetIdentity, origin: str) -> None:
    assert origin_allowed(origin, expected_port=8000) is True


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example.com",
        "http://phone.other-tailnet.ts.net:8000",
        "http://192.168.1.10:8000",
    ],
)
def test_origin_allowed_rejects_foreign_origins(fake_tailnet: TailnetIdentity, origin: str) -> None:
    assert origin_allowed(origin, expected_port=8000) is False


def test_origin_port_pin_still_applies_to_tailnet(
    fake_tailnet: TailnetIdentity,
) -> None:
    assert origin_allowed("http://100.80.90.10:3000", expected_port=8000) is False


# --- unit: GITAUTO_HOST parsing --------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "  ", ",", " , "])
def test_parse_host_selection_empty_means_default_policy(raw: str | None) -> None:
    assert config.parse_host_selection(raw) is None


def test_parse_host_selection_splits_trims_dedupes() -> None:
    assert config.parse_host_selection(" 127.0.0.1 , tailscale ") == [
        "127.0.0.1",
        "tailscale",
    ]
    assert config.parse_host_selection("127.0.0.1,,127.0.0.1") == ["127.0.0.1"]


# --- entry point: bind-host resolution -------------------------------------------


def test_resolve_hosts_default_binds_loopback_plus_tailnet(
    monkeypatch: pytest.MonkeyPatch, fake_tailnet: TailnetIdentity
) -> None:
    monkeypatch.delenv("GITAUTO_HOST", raising=False)
    monkeypatch.delenv("GITAUTO_ALLOW_NONLOOPBACK", raising=False)
    assert web_main._resolve_hosts() == ["127.0.0.1", *_IDENTITY.ips]


def test_resolve_hosts_explicit_loopback_stays_loopback_only(
    monkeypatch: pytest.MonkeyPatch, fake_tailnet: TailnetIdentity
) -> None:
    monkeypatch.setenv("GITAUTO_HOST", "127.0.0.1")
    assert web_main._resolve_hosts() == ["127.0.0.1"]


def test_resolve_hosts_expands_tailscale_token(
    monkeypatch: pytest.MonkeyPatch, fake_tailnet: TailnetIdentity
) -> None:
    monkeypatch.setenv("GITAUTO_HOST", "127.0.0.1,tailscale")
    assert web_main._resolve_hosts() == ["127.0.0.1", *_IDENTITY.ips]


def test_resolve_hosts_tailscale_token_without_tailnet_fails_closed(
    monkeypatch: pytest.MonkeyPatch, no_tailnet: None
) -> None:
    monkeypatch.setenv("GITAUTO_HOST", "tailscale")
    with pytest.raises(SystemExit) as exc:
        web_main._resolve_hosts()
    assert "tailscale" in str(exc.value).lower()


def test_resolve_hosts_allows_explicit_tailscale_ip_without_opt_out(
    monkeypatch: pytest.MonkeyPatch, no_tailnet: None
) -> None:
    monkeypatch.setenv("GITAUTO_HOST", "100.80.90.10")
    monkeypatch.delenv("GITAUTO_ALLOW_NONLOOPBACK", raising=False)
    assert web_main._resolve_hosts() == ["100.80.90.10"]


def test_resolve_hosts_dedupes_token_overlap(
    monkeypatch: pytest.MonkeyPatch, fake_tailnet: TailnetIdentity
) -> None:
    monkeypatch.setenv("GITAUTO_HOST", "tailscale,100.80.90.10")
    assert web_main._resolve_hosts() == list(_IDENTITY.ips)


# --- entry point: multi-host boot -------------------------------------------------


def _stub_uvicorn_api(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub the uvicorn surface main() uses for multi-host boots.

    Records Config construction, socket binding, Server.run(sockets=...) and
    ChangeReload usage so the tests can assert one pre-bound socket per host
    is handed to a single server / reload supervisor.
    """
    record: dict = {"configs": [], "servers": [], "reloaders": [], "run_calls": []}

    class Config:
        def __init__(self, app: str, host: str, port: int, reload: bool = False):
            self.app, self.host, self.port = app, host, port
            self.should_reload = reload
            record["configs"].append(self)

        def bind_socket(self):
            return ("socket", self.host, self.port)

    class Server:
        def __init__(self, config: Config):
            self.config = config
            self.served_sockets = None
            record["servers"].append(self)

        def run(self, sockets=None) -> None:
            self.served_sockets = sockets

    class ChangeReload:
        def __init__(self, config: Config, target, sockets):
            self.config, self.target, self.sockets = config, target, sockets
            self.ran = False
            record["reloaders"].append(self)

        def run(self) -> None:
            self.ran = True

    supervisors = types.SimpleNamespace(ChangeReload=ChangeReload)
    module = types.SimpleNamespace(
        Config=Config,
        Server=Server,
        supervisors=supervisors,
        run=lambda *a, **kw: record["run_calls"].append((a, kw)),
    )
    monkeypatch.setitem(sys.modules, "uvicorn", module)
    monkeypatch.setitem(sys.modules, "uvicorn.supervisors", supervisors)
    return record


def test_main_dual_binds_one_socket_per_host(
    monkeypatch: pytest.MonkeyPatch, fake_tailnet: TailnetIdentity
) -> None:
    record = _stub_uvicorn_api(monkeypatch)
    monkeypatch.delenv("GITAUTO_HOST", raising=False)
    monkeypatch.delenv("GITAUTO_TOOL", raising=False)
    monkeypatch.delenv("GITAUTO_RELOAD", raising=False)
    monkeypatch.setenv("GITAUTO_PORT", "9001")

    web_main.main()

    hosts = [cfg.host for cfg in record["configs"]]
    assert hosts == ["127.0.0.1", *_IDENTITY.ips]
    assert all(cfg.port == 9001 for cfg in record["configs"])
    assert record["run_calls"] == []  # multi-host path, not uvicorn.run()
    (server,) = record["servers"]
    assert server.config is record["configs"][0]
    assert server.served_sockets == [("socket", host, 9001) for host in hosts]
    assert record["reloaders"] == []


def test_main_dual_bind_reload_hands_sockets_to_supervisor(
    monkeypatch: pytest.MonkeyPatch, fake_tailnet: TailnetIdentity
) -> None:
    record = _stub_uvicorn_api(monkeypatch)
    monkeypatch.delenv("GITAUTO_HOST", raising=False)
    monkeypatch.delenv("GITAUTO_TOOL", raising=False)
    monkeypatch.setenv("GITAUTO_RELOAD", "1")
    monkeypatch.setenv("GITAUTO_PORT", "9001")

    web_main.main()

    (reloader,) = record["reloaders"]
    assert reloader.ran is True
    assert reloader.config is record["configs"][0]
    (server,) = record["servers"]
    assert reloader.target == server.run
    assert reloader.sockets == [("socket", host, 9001) for host in ["127.0.0.1", *_IDENTITY.ips]]
