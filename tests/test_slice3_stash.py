"""Slice-3 stash tests: save / pop / apply / drop round-trips.

The stash router is registered into the global app by the API manager
separately, so these API tests mount it onto a local app.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from git_automation.core.git import client
from git_automation.web.api import register_error_handlers
from git_automation.web.api import stash as stash_api


@pytest.fixture
def api() -> TestClient:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(stash_api.router, prefix="/api")
    return TestClient(app, raise_server_exceptions=False)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], capture_output=True, check=True)
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _seeded(path: Path) -> Path:
    repo = _init_repo(path)
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo


# --- Client ---------------------------------------------------------------


async def test_stash_and_pop_round_trip(tmp_path: Path) -> None:
    repo = _seeded(tmp_path / "repo")
    (repo / "f.txt").write_text("dirty\n")

    saved = await client.stash(str(repo), message="wip")
    assert saved.ok is True
    assert (repo / "f.txt").read_text() == "base\n"  # working tree reverted
    assert _git(repo, "status", "--porcelain") == ""

    stashes = (await client.get_refs(str(repo))).stashes
    assert [s.index for s in stashes] == [0]
    assert "wip" in stashes[0].message

    popped = await client.stash_pop(str(repo))
    assert popped.ok is True
    assert (repo / "f.txt").read_text() == "dirty\n"
    assert (await client.get_refs(str(repo))).stashes == []


async def test_stash_include_untracked(tmp_path: Path) -> None:
    repo = _seeded(tmp_path / "repo")
    (repo / "new.txt").write_text("untracked\n")

    saved = await client.stash(str(repo), include_untracked=True)
    assert saved.ok is True
    assert not (repo / "new.txt").exists()

    await client.stash_pop(str(repo))
    assert (repo / "new.txt").read_text() == "untracked\n"


async def test_stash_apply_keeps_entry_then_drop(tmp_path: Path) -> None:
    repo = _seeded(tmp_path / "repo")
    (repo / "f.txt").write_text("change-a\n")
    await client.stash(str(repo), message="a")
    (repo / "f.txt").write_text("change-b\n")
    await client.stash(str(repo), message="b")

    # apply (not pop) the older entry; it stays on the stack.
    applied = await client.stash_apply(str(repo), index=1)
    assert applied.ok is True
    assert (repo / "f.txt").read_text() == "change-a\n"
    assert len((await client.get_refs(str(repo))).stashes) == 2

    dropped = await client.stash_drop(str(repo), index=1)
    assert dropped.ok is True
    remaining = (await client.get_refs(str(repo))).stashes
    assert len(remaining) == 1
    assert "b" in remaining[0].message


async def test_stash_specific_file_leaves_other_changes(tmp_path: Path) -> None:
    repo = _seeded(tmp_path / "repo")
    (repo / "g.txt").write_text("second\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add g")

    (repo / "f.txt").write_text("dirty-f\n")
    (repo / "g.txt").write_text("dirty-g\n")

    saved = await client.stash(str(repo), message="just-f", files=["f.txt"])
    assert saved.ok is True
    # f.txt reverted; g.txt change remains in the working tree.
    assert (repo / "f.txt").read_text() == "base\n"
    assert (repo / "g.txt").read_text() == "dirty-g\n"

    await client.stash_pop(str(repo))
    assert (repo / "f.txt").read_text() == "dirty-f\n"


async def test_stash_files_rejects_traversal(tmp_path: Path) -> None:
    repo = _seeded(tmp_path / "repo")
    (repo / "f.txt").write_text("dirty\n")
    from git_automation.core.errors import GitAutomationError

    with pytest.raises(GitAutomationError) as exc:
        await client.stash(str(repo), files=["../OUTSIDE.txt"])
    assert exc.value.code == "invalid_argument"
    # Working tree untouched by a rejected request.
    assert (repo / "f.txt").read_text() == "dirty\n"


# --- API ------------------------------------------------------------------


def test_stash_endpoints_round_trip(api: TestClient, tmp_path: Path) -> None:
    repo = _seeded(tmp_path / "repo")
    (repo / "f.txt").write_text("dirty\n")

    save = api.post("/api/git/stash", json={"path": str(repo), "message": "wip"})
    assert save.status_code == 200 and save.json()["ok"] is True
    assert (repo / "f.txt").read_text() == "base\n"

    pop = api.post("/api/git/stash/pop", json={"path": str(repo)})
    assert pop.status_code == 200 and pop.json()["ok"] is True
    assert (repo / "f.txt").read_text() == "dirty\n"


def test_stash_endpoint_accepts_files(api: TestClient, tmp_path: Path) -> None:
    repo = _seeded(tmp_path / "repo")
    (repo / "g.txt").write_text("second\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add g")
    (repo / "f.txt").write_text("dirty-f\n")
    (repo / "g.txt").write_text("dirty-g\n")

    save = api.post("/api/git/stash", json={"path": str(repo), "files": ["f.txt"]})
    assert save.status_code == 200 and save.json()["ok"] is True
    assert (repo / "f.txt").read_text() == "base\n"
    assert (repo / "g.txt").read_text() == "dirty-g\n"


def test_stash_apply_and_drop_endpoints(api: TestClient, tmp_path: Path) -> None:
    repo = _seeded(tmp_path / "repo")
    (repo / "f.txt").write_text("dirty\n")
    api.post("/api/git/stash", json={"path": str(repo), "message": "wip"})

    apply = api.post("/api/git/stash/apply", json={"path": str(repo), "index": 0})
    assert apply.status_code == 200 and apply.json()["ok"] is True
    assert (repo / "f.txt").read_text() == "dirty\n"

    drop = api.post("/api/git/stash/drop", json={"path": str(repo), "index": 0})
    assert drop.status_code == 200 and drop.json()["ok"] is True
    assert _git(repo, "stash", "list") == ""
