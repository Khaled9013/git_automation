"""Slice-3 security regressions: conflict path traversal + terminal CSWSH.

Covers the hardening applied in the Slice-3 security pass:

* ``resolve_conflict`` / ``get_conflict`` confine working-tree file access to the
  validated repository (no ``../`` escape, no absolute path, no ``.git`` write).
* The PTY WebSocket rejects foreign-origin handshakes (Cross-Site WebSocket
  Hijacking) while still accepting loopback / non-browser clients.
* The web entry point fails closed on a non-loopback bind host (the whole trust
  model rests on ``127.0.0.1``), with an explicit env opt-out.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from git_automation.core.errors import GitAutomationError
from git_automation.core.git import client
from git_automation.web import __main__ as web_main
from git_automation.web.api import merge as merge_api
from git_automation.web.api import register_error_handlers
from git_automation.web.ws import origin_allowed


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "seed"], check=True)
    return path


@pytest.fixture
def merge_client() -> TestClient:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(merge_api.router, prefix="/api")
    return TestClient(app, raise_server_exceptions=False)


# --- resolve_conflict: arbitrary-write traversal --------------------------


async def test_resolve_conflict_rejects_relative_traversal(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    outside = tmp_path / "OUTSIDE.txt"
    outside.write_text("ORIGINAL\n")

    with pytest.raises(GitAutomationError) as exc:
        await client.resolve_conflict(str(repo), "../OUTSIDE.txt", "PWNED\n")
    assert exc.value.code == "invalid_argument"
    # The write must NOT have happened.
    assert outside.read_text() == "ORIGINAL\n"


async def test_resolve_conflict_rejects_absolute_path(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    target = tmp_path / "ABS.txt"
    target.write_text("ORIGINAL\n")

    with pytest.raises(GitAutomationError) as exc:
        await client.resolve_conflict(str(repo), str(target), "PWNED\n")
    assert exc.value.code == "invalid_argument"
    assert target.read_text() == "ORIGINAL\n"


async def test_resolve_conflict_rejects_symlink_escape(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    outside = tmp_path / "secret.txt"
    outside.write_text("ORIGINAL\n")
    # A symlink that lives inside the repo but points outside it.
    (repo / "link").symlink_to(outside)

    with pytest.raises(GitAutomationError) as exc:
        await client.resolve_conflict(str(repo), "link", "PWNED\n")
    assert exc.value.code == "invalid_argument"
    assert outside.read_text() == "ORIGINAL\n"


async def test_resolve_conflict_rejects_dot_git_write(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    config_before = (repo / ".git" / "config").read_text()

    with pytest.raises(GitAutomationError) as exc:
        await client.resolve_conflict(str(repo), ".git/config", "[core]\n\tpager = evil\n")
    assert exc.value.code == "invalid_argument"
    assert (repo / ".git" / "config").read_text() == config_before


async def test_resolve_conflict_allows_normal_nested_file(tmp_path: Path) -> None:
    """The legitimate case (a real conflicted path) still works after hardening."""
    repo = _init_repo(tmp_path / "repo")
    result = await client.resolve_conflict(str(repo), "sub/dir/f.txt", "resolved\n")
    assert result.ok is True
    assert (repo / "sub" / "dir" / "f.txt").read_text() == "resolved\n"


def test_resolve_endpoint_rejects_traversal(merge_client: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    outside = tmp_path / "OUTSIDE.txt"
    outside.write_text("ORIGINAL\n")

    resp = merge_client.post(
        "/api/git/resolve",
        json={"path": str(repo), "file": "../OUTSIDE.txt", "content": "PWNED\n"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_argument"
    assert outside.read_text() == "ORIGINAL\n"


# --- get_conflict: arbitrary-read traversal -------------------------------


async def test_get_conflict_rejects_read_traversal(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    secret = tmp_path / "SECRET.txt"
    secret.write_text("SECRET-CONTENT\n")

    with pytest.raises(GitAutomationError) as exc:
        await client.get_conflict(str(repo), "../SECRET.txt")
    assert exc.value.code == "invalid_argument"


def test_conflict_endpoint_rejects_traversal(merge_client: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    secret = tmp_path / "SECRET.txt"
    secret.write_text("SECRET-CONTENT\n")

    resp = merge_client.get(
        "/api/repo/conflict",
        params={"path": str(repo), "file": "../SECRET.txt"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_argument"


# --- Terminal WebSocket: origin / CSWSH defense ---------------------------


def test_origin_allowed_loopback_and_missing() -> None:
    assert origin_allowed(None) is True  # non-browser client (CLI/tests)
    assert origin_allowed("http://localhost:8000") is True
    assert origin_allowed("http://127.0.0.1:8000") is True
    assert origin_allowed("http://[::1]:8000") is True


def test_origin_allowed_rejects_foreign_sites() -> None:
    assert origin_allowed("https://evil.example.com") is False
    assert origin_allowed("http://attacker.test:8000") is False
    # Look-alike host names must not slip through.
    assert origin_allowed("http://127.0.0.1.evil.com") is False
    assert origin_allowed("http://notlocalhost") is False


def test_terminal_ws_rejects_foreign_origin(tmp_path: Path) -> None:
    """A cross-site Origin is rejected at the handshake — no shell is spawned."""
    from starlette.websockets import WebSocketDisconnect as StarletteWSDisconnect

    from git_automation.web.api import terminal as terminal_api

    app = FastAPI()
    app.include_router(terminal_api.router, prefix="/api")
    test_client = TestClient(app)

    with pytest.raises(StarletteWSDisconnect) as exc:
        with test_client.websocket_connect(
            f"/api/terminal?path={tmp_path}",
            headers={"origin": "https://evil.example.com"},
        ):
            pass
    assert exc.value.code == 1008


# --- Entry point: fail-closed on a non-loopback bind host -----------------


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "127.0.0.5"])
def test_resolve_host_allows_loopback(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    monkeypatch.setenv("GITAUTO_HOST", host)
    monkeypatch.delenv("GITAUTO_ALLOW_NONLOOPBACK", raising=False)
    assert web_main._resolve_host() == host


def test_resolve_host_defaults_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITAUTO_HOST", raising=False)
    monkeypatch.delenv("GITAUTO_ALLOW_NONLOOPBACK", raising=False)
    assert web_main._resolve_host() == "127.0.0.1"


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.com"])
def test_resolve_host_rejects_non_loopback(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    monkeypatch.setenv("GITAUTO_HOST", host)
    monkeypatch.delenv("GITAUTO_ALLOW_NONLOOPBACK", raising=False)
    with pytest.raises(SystemExit):
        web_main._resolve_host()


def test_resolve_host_non_loopback_opt_out_warns_and_proceeds(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("GITAUTO_HOST", "0.0.0.0")
    monkeypatch.setenv("GITAUTO_ALLOW_NONLOOPBACK", "1")
    import logging

    with caplog.at_level(logging.WARNING, logger=web_main.logger.name):
        assert web_main._resolve_host() == "0.0.0.0"
    assert any("SECURITY" in rec.message for rec in caplog.records)
