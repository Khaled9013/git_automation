"""Regression tests for git option-injection hardening.

A caller-supplied remote/branch value that begins with ``-`` must be rejected
before it can reach ``git`` as an argument, where it could be reinterpreted as
an option (e.g. ``--upload-pack=<cmd>`` / ``--receive-pack=<cmd>`` leading to
arbitrary command execution). These tests assert the guard rejects such input
at the core layer and surfaces as a 400 error shape at the API layer, while
never invoking the real ``git`` process.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from git_automation.core import process
from git_automation.core.errors import GitAutomationError
from git_automation.core.git import client
from git_automation.web.app import create_app


@pytest.fixture
def repo(tmp_path: Path) -> str:
    """An existing directory so path validation passes; git is never invoked."""
    d = tmp_path / "repo"
    d.mkdir()
    return str(d)


@pytest.fixture(autouse=True)
def _no_real_git(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Fail loudly if any rejected call still reaches the subprocess layer."""
    calls: list[list[str]] = []

    async def fake_run(args, cwd=None, stdin=None):  # type: ignore[no-untyped-def]
        calls.append(args)
        raise AssertionError(f"git should not have been called with: {args}")

    monkeypatch.setattr(process, "run_process", fake_run)
    return calls


# --- core layer -----------------------------------------------------------


async def test_fetch_rejects_option_like_remote(repo: str) -> None:
    with pytest.raises(GitAutomationError) as exc:
        await client.fetch(repo, "--upload-pack=touch /tmp/pwned")
    assert exc.value.code == "invalid_argument"
    assert exc.value.status_code == 400


async def test_pull_rejects_option_like_remote(repo: str) -> None:
    with pytest.raises(GitAutomationError) as exc:
        await client.pull(repo, "--upload-pack=evil")
    assert exc.value.code == "invalid_argument"


async def test_pull_rejects_option_like_branch(repo: str) -> None:
    with pytest.raises(GitAutomationError) as exc:
        await client.pull(repo, "origin", "--upload-pack=evil")
    assert exc.value.code == "invalid_argument"


async def test_push_rejects_option_like_remote(repo: str) -> None:
    with pytest.raises(GitAutomationError) as exc:
        await client.push(repo, "--receive-pack=evil")
    assert exc.value.code == "invalid_argument"


async def test_push_rejects_option_like_branch(repo: str) -> None:
    with pytest.raises(GitAutomationError) as exc:
        await client.push(repo, "origin", "--force")
    assert exc.value.code == "invalid_argument"


@pytest.mark.parametrize(
    "fn",
    [client.create_branch, client.checkout_branch, client.delete_branch, client.merge_branch],
)
async def test_branch_ops_reject_option_like_name(repo: str, fn) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(GitAutomationError) as exc:
        await fn(repo, "--force")
    assert exc.value.code == "invalid_argument"


# --- API layer ------------------------------------------------------------


def test_api_fetch_rejects_option_like_remote(repo: str) -> None:
    api = TestClient(create_app(), raise_server_exceptions=False)
    resp = api.post("/api/git/fetch", json={"path": repo, "remote": "--upload-pack=evil"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_argument"


def test_api_branch_delete_rejects_option_like_name(repo: str) -> None:
    api = TestClient(create_app(), raise_server_exceptions=False)
    resp = api.post(
        "/api/git/branch/delete", json={"path": repo, "name": "--force", "force": True}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_argument"
