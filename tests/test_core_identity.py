"""Tests for the combined identity state. No real git/gh state is touched."""

from __future__ import annotations

import pytest

from git_automation.core import identity, process
from git_automation.core.process import ProcessResult

LOGGED_IN = "github.com\n  ✓ Logged in to github.com account octocat (keyring)\n"


def _fake_run_factory(name: str | None, email: str | None, gh_ok: bool):
    async def fake_run(
        args: list[str], cwd: str | None = None, stdin: str | None = None
    ) -> ProcessResult:
        if args[:2] == ["gh", "auth"]:
            return ProcessResult(0 if gh_ok else 1, "", LOGGED_IN if gh_ok else "no")
        if args[:4] == ["git", "config", "--global", "--get"]:
            key = args[4]
            value = {"user.name": name, "user.email": email}[key]
            return ProcessResult(0 if value else 1, (value or "") + "\n", "")
        return ProcessResult(0, "", "")

    return fake_run


async def test_fully_configured_no_onboarding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process, "run_process", _fake_run_factory("Ada", "ada@x.com", True))
    state = await identity.get_identity_state()
    assert state.git_name == "Ada"
    assert state.git_email == "ada@x.com"
    assert state.gh_authenticated is True
    assert state.gh_user == "octocat"
    assert state.needs_onboarding is False


async def test_missing_git_identity_needs_onboarding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process, "run_process", _fake_run_factory(None, None, True))
    state = await identity.get_identity_state()
    assert state.git_name is None
    assert state.needs_onboarding is True


async def test_missing_gh_auth_needs_onboarding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process, "run_process", _fake_run_factory("Ada", "ada@x.com", False))
    state = await identity.get_identity_state()
    assert state.gh_authenticated is False
    assert state.gh_user is None
    assert state.needs_onboarding is True
