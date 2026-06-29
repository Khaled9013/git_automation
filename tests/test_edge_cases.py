"""Edge-case and regression tests for the git client and filesystem browser.

Covers unborn repos, detached HEAD, no-remote repos, diff of deleted/binary
files, branch-op failure paths, graph parsing quirks, and the filesystem
browser's error paths. Several of these are regressions for specific fixes:

* graph subjects containing the field delimiter (``_SEP``) must not shift refs;
* ``get_diff`` of a *tracked, unmodified* file must be empty (not a full dump).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from git_automation.core import fs
from git_automation.core.errors import GitAutomationError
from git_automation.core.git import client

_SEP = "\x1f"


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


def _commit(path: Path, name: str = "file.txt", content: str = "hello", msg: str | None = None):
    (path / name).write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-m", msg or f"add {name}")


# --- Unborn repo (git init, no commits) -----------------------------------


async def test_unborn_repo_status_does_not_crash(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    status = await client.get_status(str(repo))
    assert status.current_branch == "main"
    assert status.upstream is None
    assert status.ahead == 0 and status.behind == 0
    assert status.dirty is False


async def test_unborn_repo_graph_is_empty(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    assert await client.get_graph(str(repo)) == []


async def test_unborn_repo_branches_empty_with_current(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    branches = await client.list_branches(str(repo))
    assert branches.current == "main"
    assert branches.local == []
    assert branches.remote == []


async def test_unborn_repo_changes_reports_untracked(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "new.txt").write_text("x")
    changes = await client.get_changes(str(repo))
    assert changes.untracked == ["new.txt"]
    assert changes.staged == [] and changes.unstaged == []


# --- Detached HEAD and no-remote repos ------------------------------------


async def test_detached_head_status_and_graph(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "a")
    _commit(repo, "b.txt", "b")
    _git(repo, "checkout", "HEAD~1")  # detach

    status = await client.get_status(str(repo))
    assert status.current_branch is None  # detached -> no branch name

    branches = await client.list_branches(str(repo))
    assert branches.current is None
    assert not any(b.is_current for b in branches.local)

    graph = await client.get_graph(str(repo))
    head = next(c for c in graph if c.is_head)
    assert head.subject == "add a.txt"  # HEAD is on the older commit


async def test_repo_with_no_remotes(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    assert await client.list_remotes(str(repo)) == []
    status = await client.get_status(str(repo))
    assert status.remotes == []
    assert status.upstream is None


# --- Diff edge cases ------------------------------------------------------


async def test_diff_deleted_file(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "gone.txt", "line1\nline2\n")
    (repo / "gone.txt").unlink()

    diff = await client.get_diff(str(repo), "gone.txt")
    assert diff.binary is False
    assert "-line1" in diff.diff
    assert "deleted file" in diff.diff or "/dev/null" in diff.diff


async def test_diff_binary_untracked_sets_binary_flag(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    (repo / "blob.bin").write_bytes(bytes(range(256)))

    diff = await client.get_diff(str(repo), "blob.bin")
    assert diff.binary is True
    # Raw bytes are not dumped; git's "Binary files ... differ" marker is used.
    assert "\x00" not in diff.diff


async def test_diff_binary_tracked_modified_sets_binary_flag(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "bin")
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02\x03\xff")

    diff = await client.get_diff(str(repo), "blob.bin")
    assert diff.binary is True


async def test_diff_of_tracked_unmodified_file_is_empty(tmp_path: Path) -> None:
    """Regression: a clean tracked file must not be dumped as an added file."""
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "t.txt", "content\n")

    diff = await client.get_diff(str(repo), "t.txt")
    assert diff.diff == ""
    assert diff.binary is False


# --- Branch op failure paths ----------------------------------------------


async def test_delete_current_branch_fails_gracefully(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    result = await client.delete_branch(str(repo), "main")
    assert result.ok is False
    assert result.output  # git explains why, no crash


async def test_delete_unmerged_branch_without_force_fails(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    await client.create_branch(str(repo), "feature", checkout=True)
    _commit(repo, "feature.txt", "f")
    await client.checkout_branch(str(repo), "main")

    result = await client.delete_branch(str(repo), "feature", force=False)
    assert result.ok is False
    assert "feature" in {b.name for b in (await client.list_branches(str(repo))).local}

    forced = await client.delete_branch(str(repo), "feature", force=True)
    assert forced.ok is True


async def test_merge_conflict_surfaces_output_without_crash(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "base\n")
    await client.create_branch(str(repo), "feature", checkout=True)
    _commit(repo, "f.txt", "feature\n", msg="feature change")
    await client.checkout_branch(str(repo), "main")
    _commit(repo, "f.txt", "mainline\n", msg="main change")

    result = await client.merge_branch(str(repo), "feature")
    assert result.ok is False
    assert "CONFLICT" in result.output
    # The conflicted file is reported with the unmerged 'U' status.
    changes = await client.get_changes(str(repo))
    assert any(c.status == "U" for c in changes.unstaged + changes.staged)


# --- Graph parsing quirks -------------------------------------------------


async def test_graph_subject_with_field_delimiter_preserved(tmp_path: Path) -> None:
    """Regression: a delimiter inside the subject must not steal the refs field."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a")
    _git(repo, "add", "-A")
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", f"weird{_SEP}subject"],
        capture_output=True,
        check=True,
    )
    graph = await client.get_graph(str(repo))
    assert graph[0].subject == f"weird{_SEP}subject"
    assert graph[0].is_head is True
    assert any("main" in ref for ref in graph[0].refs)


async def test_graph_subject_with_commas_and_arrows(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "a", msg="fix: a, b -> c, done")
    graph = await client.get_graph(str(repo))
    assert graph[0].subject == "fix: a, b -> c, done"


async def test_graph_merge_commit_has_two_parents(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "a")
    await client.create_branch(str(repo), "feat", checkout=True)
    _commit(repo, "b.txt", "b")
    await client.checkout_branch(str(repo), "main")
    _commit(repo, "c.txt", "c")
    _git(repo, "merge", "--no-ff", "-m", "merge feat", "feat")

    graph = await client.get_graph(str(repo))
    assert len(graph[0].parents) == 2


async def test_graph_root_commit_has_no_parents_and_empty_refs(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "a")  # root commit, carries 'main' ref
    _commit(repo, "b.txt", "b")  # HEAD; the root keeps no decorations
    graph = await client.get_graph(str(repo))
    root = graph[-1]
    assert root.parents == []
    assert root.refs == []  # unreferenced root commit -> empty, parsed cleanly
    assert root.subject == "add a.txt"


# --- Filesystem browser error paths ---------------------------------------


async def test_list_dirs_on_file_path_raises(tmp_path: Path) -> None:
    target = tmp_path / "afile"
    target.write_text("not a dir")
    with pytest.raises(GitAutomationError) as exc:
        await fs.list_dirs(str(target))
    assert exc.value.code == "invalid_path"


async def test_list_dirs_nonexistent_raises(tmp_path: Path) -> None:
    with pytest.raises(GitAutomationError) as exc:
        await fs.list_dirs(str(tmp_path / "does-not-exist"))
    assert exc.value.code == "invalid_path"


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
async def test_list_dirs_permission_denied_raises(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "child").mkdir()
    os.chmod(locked, 0o000)
    try:
        with pytest.raises(GitAutomationError) as exc:
            await fs.list_dirs(str(locked))
        assert exc.value.code == "permission_denied"
    finally:
        os.chmod(locked, 0o755)


async def test_list_dirs_flags_git_repos(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    repo = _init_repo(tmp_path / "repo")
    entries = {e.name: e for e in await fs.list_dirs(str(tmp_path))}
    assert entries["plain"].is_git_repo is False
    assert entries[repo.name].is_git_repo is True
