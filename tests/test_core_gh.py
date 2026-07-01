"""Tests for the ``gh`` CLI wrapper. No real gh auth call is ever made."""

from __future__ import annotations

import pytest

from git_automation.core import process
from git_automation.core.errors import GitAutomationError
from git_automation.core.github import gh_cli as client
from git_automation.core.process import ProcessResult

LOGGED_IN_MODERN = "github.com\n  ✓ Logged in to github.com account octocat (keyring)\n"
LOGGED_IN_LEGACY = "github.com\n  ✓ Logged in to github.com as octocat (oauth_token)\n"


def _patch_run(monkeypatch: pytest.MonkeyPatch, handler) -> list[dict]:
    """Patch process.run_process; record each call's args/stdin; delegate to handler."""
    recorded: list[dict] = []

    async def fake_run(
        args: list[str], cwd: str | None = None, stdin: str | None = None
    ) -> ProcessResult:
        recorded.append({"args": args, "stdin": stdin})
        return handler(args, stdin)

    monkeypatch.setattr(process, "run_process", fake_run)
    return recorded


async def test_auth_status_authenticated_modern(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, lambda args, stdin: ProcessResult(0, "", LOGGED_IN_MODERN))
    authed, user = await client.auth_status()
    assert authed is True
    assert user == "octocat"


async def test_auth_status_authenticated_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, lambda args, stdin: ProcessResult(0, "", LOGGED_IN_LEGACY))
    authed, user = await client.auth_status()
    assert authed is True
    assert user == "octocat"


async def test_auth_status_not_authenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, lambda args, stdin: ProcessResult(1, "", "not logged in"))
    authed, user = await client.auth_status()
    assert authed is False
    assert user is None


async def test_login_with_token_passes_token_on_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _patch_run(monkeypatch, lambda args, stdin: ProcessResult(0, "", "Logged in"))
    await client.login_with_token("super-secret-token")

    call = recorded[0]
    assert call["args"] == ["gh", "auth", "login", "--with-token"]
    assert call["stdin"] == "super-secret-token"
    # The token must NEVER appear in the command arguments.
    assert "super-secret-token" not in " ".join(call["args"])


async def test_login_with_token_failure_excludes_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, lambda args, stdin: ProcessResult(1, "", "error: bad credentials"))
    with pytest.raises(GitAutomationError) as exc:
        await client.login_with_token("super-secret-token")
    assert exc.value.code == "gh_login_failed"
    assert "super-secret-token" not in exc.value.message
    assert "bad credentials" in exc.value.message


def test_login_instructions_shape() -> None:
    instructions = client.login_instructions()
    assert instructions["supports_token"] is True
    assert isinstance(instructions["steps"], list)
    assert all(isinstance(step, str) for step in instructions["steps"])
    assert instructions["steps"]
