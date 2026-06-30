"""Slice-3 edge-case hardening tests against real, hermetic temp repos.

These pin behaviour for the fragile corners of the merge/reset/cherry-pick/
stash/refs/reflog surfaces: a merge commit's changed-file list (regression
guard for the ``--name-status`` combined-diff suppression bug), already-up-to-
date and in-progress merges, conflicting/non-existent cherry-picks, reset
``mixed`` semantics and reset on an unborn repo, stash no-op/conflict/bad-index
cases, detached-HEAD and linked-worktree ref aggregation, undo on an unborn
repo, and the reflog limit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from git_automation.core.errors import GitAutomationError
from git_automation.core.git import client


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


def _commit(path: Path, name: str, content: str, msg: str) -> str:
    (path / name).write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-m", msg)
    return _git(path, "rev-parse", "HEAD")


# --- Merge commit detail (regression for empty file list) -----------------


async def test_merge_commit_detail_lists_parents_and_files(tmp_path: Path) -> None:
    """A merge commit must report both parents AND its changed files.

    ``git show --name-status`` emits nothing for a merge by default (combined
    diff suppression); without ``--first-parent`` the file list came back empty.
    """
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "a\n", "base")
    _git(repo, "checkout", "-b", "feature")
    _commit(repo, "b.txt", "b\n", "feat")
    _git(repo, "checkout", "main")
    _commit(repo, "c.txt", "c\n", "main")
    _git(repo, "merge", "--no-ff", "feature", "-m", "merge feature")
    sha = _git(repo, "rev-parse", "HEAD")

    detail = await client.get_commit_detail(str(repo), sha)
    assert len(detail.parents) == 2
    # The merge brought in b.txt relative to the first parent (main).
    paths = {f.path for f in detail.files}
    assert "b.txt" in paths
    bfile = next(f for f in detail.files if f.path == "b.txt")
    assert bfile.status == "A"
    assert bfile.additions == 1


async def test_commit_detail_unknown_sha_raises(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "a\n", "base")
    with pytest.raises(GitAutomationError) as exc:
        await client.get_commit_detail(str(repo), "deadbeefdeadbeef")
    assert exc.value.code == "unknown_commit"
    assert exc.value.status_code == 404


# --- Merge edge cases -----------------------------------------------------


async def test_merge_already_up_to_date(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "a\n", "base")
    _git(repo, "branch", "feature")  # points at the same commit
    result = await client.merge_branch(str(repo), "feature")
    assert result.ok is True
    assert result.conflicted is False
    assert result.conflicts == []
    assert "up to date" in result.output.lower()


async def test_merge_while_merge_in_progress_refuses(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "base\n", "base")
    _git(repo, "checkout", "-b", "feature")
    _commit(repo, "f.txt", "feature\n", "feat")
    _git(repo, "checkout", "main")
    _commit(repo, "f.txt", "main\n", "main")

    first = await client.merge_branch(str(repo), "feature")
    assert first.conflicted is True
    # A second merge while unmerged paths remain must fail, not crash.
    second = await client.merge_branch(str(repo), "feature")
    assert second.ok is False
    assert "unmerged" in second.output.lower() or "not possible" in second.output.lower()


# --- Cherry-pick edge cases -----------------------------------------------


async def test_cherry_pick_conflict_surfaces_without_crashing(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "base\n", "base")
    _git(repo, "checkout", "-b", "feature")
    pick = _commit(repo, "f.txt", "feature\n", "feat change")
    _git(repo, "checkout", "main")
    _commit(repo, "f.txt", "main\n", "main change")

    result = await client.cherry_pick(str(repo), pick)
    assert result.ok is False
    assert "CONFLICT" in result.output
    # The cherry-pick is left in progress (so the UI can resolve or abort).
    assert (repo / ".git" / "CHERRY_PICK_HEAD").exists()


async def test_cherry_pick_nonexistent_sha(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "base\n", "base")
    result = await client.cherry_pick(str(repo), "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
    assert result.ok is False
    assert "bad object" in result.output.lower() or "bad revision" in result.output.lower()


# --- Reset semantics ------------------------------------------------------


async def test_reset_mixed_unstages_but_keeps_worktree(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    first = _commit(repo, "a.txt", "a\n", "first")
    _commit(repo, "a.txt", "ab\n", "second")

    out = await client.reset(str(repo), first, "mixed")
    assert out.ok is True
    assert _git(repo, "rev-parse", "HEAD") == first
    # Working-tree content is preserved...
    assert (repo / "a.txt").read_text() == "ab\n"
    # ...but the change is unstaged (nothing in the index).
    assert _git(repo, "diff", "--cached", "--name-only") == ""
    assert "a.txt" in _git(repo, "diff", "--name-only")


async def test_reset_on_unborn_repo_fails_gracefully(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    out = await client.reset(str(repo), "HEAD", "mixed")
    assert out.ok is False
    assert out.output  # git's error surfaced, no exception


# --- Stash edge cases -----------------------------------------------------


async def test_stash_with_no_changes_surfaces_message(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "a\n", "base")
    # git stash exits 0 even with nothing to save; the message is surfaced.
    result = await client.stash(str(repo))
    assert result.ok is True
    assert "no local changes" in result.output.lower()
    assert (await client.get_refs(str(repo))).stashes == []


async def test_stash_pop_conflict_keeps_entry(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "base\n", "base")
    (repo / "f.txt").write_text("stashed\n")
    await client.stash(str(repo), message="wip")
    # Commit a competing change so the pop has to 3-way merge -> conflict.
    _commit(repo, "f.txt", "committed\n", "competing")

    result = await client.stash_pop(str(repo))
    assert result.ok is False
    assert "<<<<<<<" in (repo / "f.txt").read_text()
    # The entry is kept so the user does not lose the stash on a failed pop.
    assert len((await client.get_refs(str(repo))).stashes) == 1


async def test_stash_apply_bad_index(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "a\n", "base")
    result = await client.stash_apply(str(repo), 9)
    assert result.ok is False
    assert "stash@{9}" in result.output


async def test_stash_drop_bad_index(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "a\n", "base")
    result = await client.stash_drop(str(repo), 9)
    assert result.ok is False
    assert "stash@{9}" in result.output


# --- Refs aggregation edge cases ------------------------------------------


async def test_refs_detached_head_reports_no_current_branch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    first = _commit(repo, "a.txt", "a\n", "first")
    _commit(repo, "b.txt", "b\n", "second")
    _git(repo, "checkout", first)  # detach HEAD

    refs = await client.get_refs(str(repo))
    assert all(b.is_current is False for b in refs.local)


async def test_refs_worktree_is_current_flag(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "a\n", "first")
    wt = tmp_path / "linked"
    _git(repo, "worktree", "add", "-b", "wtbranch", str(wt))

    refs = await client.get_refs(str(repo))
    by_resolved = {Path(w.path).resolve(): w for w in refs.worktrees}
    main_wt = by_resolved[Path(repo).resolve()]
    linked_wt = by_resolved[Path(wt).resolve()]
    # get_refs runs against the main worktree, so only it is current.
    assert main_wt.is_current is True
    assert linked_wt.is_current is False
    assert linked_wt.branch == "wtbranch"


# --- Reflog / undo edge cases ---------------------------------------------


async def test_undo_on_unborn_repo_fails_gracefully(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    result = await client.undo(str(repo))
    assert result.ok is False  # no HEAD@{1} to move to; surfaced, not crashed


async def test_reflog_limit_caps_entries(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    for i in range(5):
        _commit(repo, "f.txt", f"v{i}\n", f"commit {i}")
    entries = await client.get_reflog(str(repo), limit=2)
    assert len(entries) == 2
    assert entries[0].selector == "HEAD@{0}"
