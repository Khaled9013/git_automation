"""Slice-3 tests: refs sidebar, commit detail, checkout/reset/cherry-pick, reflog.

Exercised against real, hermetic temp repositories at both the client and the
(registered) gitops API layer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from git_automation.core.errors import GitAutomationError
from git_automation.core.git import client
from git_automation.web.app import create_app


@pytest.fixture
def api() -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=False)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], capture_output=True, check=True)
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _commit(
    path: Path, name: str = "file.txt", content: str = "hello", msg: str | None = None
) -> str:
    (path / name).write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-m", msg or f"add {name}")
    return _git(path, "rev-parse", "HEAD")


# --- Refs sidebar ---------------------------------------------------------


async def test_get_refs_local_remote_tags_worktrees_stashes(tmp_path: Path) -> None:
    # Bare remote so we get a remote-tracking ref.
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)], capture_output=True, check=True
    )
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "remote", "add", "origin", str(remote))
    _commit(repo)
    _git(repo, "push", "-u", "origin", "main")
    _git(repo, "branch", "feature")
    _git(repo, "tag", "v1.0")

    # A stash entry.
    (repo / "file.txt").write_text("dirty")
    _git(repo, "stash", "push", "-m", "wip work")

    # A linked worktree.
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", str(wt), "feature")

    refs = await client.get_refs(str(repo))

    assert {b.name for b in refs.local} == {"main", "feature"}
    assert any(b.is_current and b.name == "main" for b in refs.local)
    assert any(r.name == "origin/main" for r in refs.remote)
    assert [t.name for t in refs.tags] == ["v1.0"]
    assert [s.index for s in refs.stashes] == [0]
    assert refs.stashes[0].message and "wip work" in refs.stashes[0].message

    wt_paths = {Path(w.path).name: w for w in refs.worktrees}
    assert "repo" in wt_paths and wt_paths["repo"].is_current is True
    assert "wt" in wt_paths and wt_paths["wt"].branch == "feature"
    assert wt_paths["wt"].is_current is False


def test_refs_endpoint(api: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    _git(repo, "tag", "v0")
    resp = api.get("/api/repo/refs", params={"path": str(repo)})
    assert resp.status_code == 200
    body = resp.json()
    assert [t["name"] for t in body["tags"]] == ["v0"]
    assert any(b["name"] == "main" for b in body["local"])
    assert body["stashes"] == []


# --- Commit detail --------------------------------------------------------


async def test_get_commit_detail(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    first = _commit(repo, "a.txt", "a\n", msg="first")
    _git(repo, "tag", "rel")
    (repo / "a.txt").write_text("a\nb\nc\n")
    (repo / "new.txt").write_text("new\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "second\n\nbody line one\nbody line two")
    sha = _git(repo, "rev-parse", "HEAD")

    detail = await client.get_commit_detail(str(repo), sha)
    assert detail.sha == sha
    assert detail.short == sha[:7] or sha.startswith(detail.short)
    assert detail.parents == [first]
    assert detail.author == "Test User"
    assert detail.email == "test@example.com"
    assert detail.subject == "second"
    assert "body line one" in detail.body
    by_path = {f.path: f for f in detail.files}
    assert set(by_path) == {"a.txt", "new.txt"}
    assert by_path["new.txt"].status == "A"
    assert by_path["a.txt"].status == "M"
    assert by_path["a.txt"].additions == 2 and by_path["a.txt"].deletions == 0

    # The first commit carries the tag decoration.
    first_detail = await client.get_commit_detail(str(repo), first)
    assert first_detail.parents == []
    assert any("rel" in ref for ref in first_detail.refs)


def test_commit_endpoint_and_unknown(api: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    sha = _commit(repo)
    ok = api.get("/api/repo/commit", params={"path": str(repo), "sha": sha})
    assert ok.status_code == 200 and ok.json()["sha"] == sha

    bad = api.get("/api/repo/commit", params={"path": str(repo), "sha": "deadbeef"})
    assert bad.status_code == 404
    assert bad.json()["error"]["code"] == "unknown_commit"


async def test_commit_detail_rejects_option_like_sha(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    with pytest.raises(GitAutomationError) as exc:
        await client.get_commit_detail(str(repo), "--all")
    assert exc.value.code == "invalid_argument"


# --- Checkout (branch + detached) -----------------------------------------


async def test_checkout_ref_branch_and_detached(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    first = _commit(repo, "a.txt", "a")
    _commit(repo, "b.txt", "b")

    await client.create_branch(str(repo), "feature")
    out = await client.checkout_ref(str(repo), "feature")
    assert out.ok is True
    assert _git(repo, "branch", "--show-current") == "feature"

    # Checking out a bare SHA detaches HEAD.
    detached = await client.checkout_ref(str(repo), first)
    assert detached.ok is True
    assert _git(repo, "branch", "--show-current") == ""
    assert _git(repo, "rev-parse", "HEAD") == first


def test_checkout_endpoint(api: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    _git(repo, "branch", "dev")
    resp = api.post("/api/git/checkout", json={"path": str(repo), "ref": "dev"})
    assert resp.status_code == 200 and resp.json()["ok"] is True


async def test_checkout_rejects_option_like_ref(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    with pytest.raises(GitAutomationError) as exc:
        await client.checkout_ref(str(repo), "--orphan")
    assert exc.value.code == "invalid_argument"


# --- Reset ----------------------------------------------------------------


async def test_reset_soft_keeps_changes_staged(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    first = _commit(repo, "a.txt", "a")
    _commit(repo, "a.txt", "ab", msg="second")

    out = await client.reset(str(repo), first, "soft")
    assert out.ok is True
    assert _git(repo, "rev-parse", "HEAD") == first
    # Soft keeps the working tree and index; the change is staged.
    assert "a.txt" in _git(repo, "diff", "--cached", "--name-only")


async def test_reset_hard_discards(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    first = _commit(repo, "a.txt", "a")
    _commit(repo, "a.txt", "changed", msg="second")

    out = await client.reset(str(repo), first, "hard")
    assert out.ok is True
    assert (repo / "a.txt").read_text() == "a"
    assert _git(repo, "status", "--porcelain") == ""


async def test_reset_rejects_bad_mode(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    sha = _commit(repo)
    with pytest.raises(GitAutomationError) as exc:
        await client.reset(str(repo), sha, "nuke")
    assert exc.value.code == "invalid_argument"


async def test_reset_rejects_option_like_sha(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    with pytest.raises(GitAutomationError) as exc:
        await client.reset(str(repo), "--hard", "mixed")
    assert exc.value.code == "invalid_argument"


def test_reset_endpoint(api: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    first = _commit(repo, "a.txt", "a")
    _commit(repo, "a.txt", "b", msg="second")
    resp = api.post("/api/git/reset", json={"path": str(repo), "sha": first, "mode": "mixed"})
    assert resp.status_code == 200 and resp.json()["ok"] is True


# --- Cherry-pick ----------------------------------------------------------


async def test_cherry_pick(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "base.txt", "base")
    await client.create_branch(str(repo), "feature", checkout=True)
    pick = _commit(repo, "feature.txt", "f", msg="add feature file")
    await client.checkout_branch(str(repo), "main")

    out = await client.cherry_pick(str(repo), pick)
    assert out.ok is True
    assert (repo / "feature.txt").exists()
    assert _git(repo, "log", "-1", "--format=%s") == "add feature file"


def test_cherry_pick_endpoint_rejects_option(api: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    resp = api.post("/api/git/cherry-pick", json={"path": str(repo), "sha": "--quit"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_argument"


# --- Reflog / undo / redo -------------------------------------------------


async def test_reflog_lists_entries(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "a", msg="first")
    _commit(repo, "b.txt", "b", msg="second")
    entries = await client.get_reflog(str(repo), limit=10)
    assert entries
    assert entries[0].selector == "HEAD@{0}"
    assert "second" in entries[0].subject


async def test_undo_then_redo_round_trip(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "a", msg="first")
    second = _commit(repo, "f.txt", "b", msg="second")

    undone = await client.undo(str(repo))
    assert undone.ok is True
    assert undone.undone  # subject of the action being reversed
    assert _git(repo, "log", "-1", "--format=%s") == "first"
    assert (repo / "f.txt").read_text() == "a"

    redone = await client.redo(str(repo))
    assert redone.ok is True
    assert _git(repo, "rev-parse", "HEAD") == second
    assert (repo / "f.txt").read_text() == "b"


async def test_undo_refuses_to_discard_uncommitted_work(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "a", msg="first")
    _commit(repo, "f.txt", "b", msg="second")
    # Local uncommitted edit that would be lost by moving HEAD back.
    (repo / "f.txt").write_text("uncommitted local edit")

    result = await client.undo(str(repo))
    assert result.ok is False  # git reset --keep refuses
    assert (repo / "f.txt").read_text() == "uncommitted local edit"


def test_reflog_undo_redo_endpoints(api: TestClient, tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "a", msg="first")
    _commit(repo, "f.txt", "b", msg="second")

    reflog = api.get("/api/repo/reflog", params={"path": str(repo), "limit": 5})
    assert reflog.status_code == 200 and len(reflog.json()) >= 2

    undo = api.post("/api/git/undo", json={"path": str(repo)})
    assert undo.status_code == 200 and undo.json()["ok"] is True
    assert _git(repo, "log", "-1", "--format=%s") == "first"

    redo = api.post("/api/git/redo", json={"path": str(repo)})
    assert redo.status_code == 200 and redo.json()["ok"] is True
    assert _git(repo, "log", "-1", "--format=%s") == "second"
