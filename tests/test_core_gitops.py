"""Tests for the working-tree / branch / graph git client functions."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from git_automation.core.errors import GitAutomationError
from git_automation.core.git import client


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


def _commit(path: Path, name: str = "file.txt", content: str = "hello") -> None:
    (path / name).write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-m", f"add {name}")


# --- changes / stage / unstage / discard ----------------------------------


async def test_get_changes_classifies_staged_unstaged_untracked(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "tracked.txt", "v1")

    # Modify a tracked file and stage the change.
    (repo / "tracked.txt").write_text("v2")
    _git(repo, "add", "tracked.txt")
    # Modify it again in the worktree -> both staged and unstaged 'M'.
    (repo / "tracked.txt").write_text("v3")
    # A brand-new untracked file.
    (repo / "new.txt").write_text("new")
    # A newly added (staged) file.
    (repo / "added.txt").write_text("added")
    _git(repo, "add", "added.txt")

    changes = await client.get_changes(str(repo))

    staged = {c.path: c.status for c in changes.staged}
    unstaged = {c.path: c.status for c in changes.unstaged}
    assert staged["tracked.txt"] == "M"
    assert staged["added.txt"] == "A"
    assert unstaged["tracked.txt"] == "M"
    assert changes.untracked == ["new.txt"]


async def test_get_changes_clean_repo_is_empty(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    changes = await client.get_changes(str(repo))
    assert changes.staged == []
    assert changes.unstaged == []
    assert changes.untracked == []


async def test_get_changes_unstaged_only_is_not_phantom_staged(tmp_path: Path) -> None:
    """Regression: a single modified-unstaged file must not appear as staged.

    Porcelain emits `` M f.txt`` (leading space = empty index column). Parsing
    the stripped output dropped that space, misreading it as a staged ``M`` with
    a mangled ``.txt`` path. The raw stdout must be parsed instead.
    """
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "v1")
    (repo / "f.txt").write_text("v2")  # modified, unstaged

    changes = await client.get_changes(str(repo))

    assert changes.staged == []
    assert [(c.path, c.status) for c in changes.unstaged] == [("f.txt", "M")]
    assert changes.untracked == []


async def test_get_changes_staged_only(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "v1")
    (repo / "f.txt").write_text("v2")
    _git(repo, "add", "f.txt")

    changes = await client.get_changes(str(repo))

    assert [(c.path, c.status) for c in changes.staged] == [("f.txt", "M")]
    assert changes.unstaged == []
    assert changes.untracked == []


async def test_get_changes_partial_staging_same_file(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "v1")
    (repo / "f.txt").write_text("v2")
    _git(repo, "add", "f.txt")
    (repo / "f.txt").write_text("v3")  # further worktree edit

    changes = await client.get_changes(str(repo))

    assert [(c.path, c.status) for c in changes.staged] == [("f.txt", "M")]
    assert [(c.path, c.status) for c in changes.unstaged] == [("f.txt", "M")]
    assert changes.untracked == []


async def test_get_changes_untracked(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    (repo / "new.txt").write_text("x")

    changes = await client.get_changes(str(repo))

    assert changes.staged == []
    assert changes.unstaged == []
    assert changes.untracked == ["new.txt"]


async def test_get_changes_preserves_leading_space_sensitive_name(tmp_path: Path) -> None:
    """A filename whose first char is a digit/space-adjacent must survive parsing."""
    repo = _init_repo(tmp_path / "repo")
    name = "0leading.txt"
    _commit(repo, name, "v1")
    (repo / name).write_text("v2")

    changes = await client.get_changes(str(repo))

    assert changes.staged == []
    assert [(c.path, c.status) for c in changes.unstaged] == [(name, "M")]


async def test_get_changes_rename(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "old.txt", "content")
    _git(repo, "mv", "old.txt", "new.txt")  # staged rename

    changes = await client.get_changes(str(repo))

    assert [(c.path, c.status) for c in changes.staged] == [("new.txt", "R")]
    assert changes.unstaged == []
    assert changes.untracked == []


# --- delete_files ---------------------------------------------------------


async def test_delete_files_tracked(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "tracked.txt", "v1")

    result = await client.delete_files(str(repo), ["tracked.txt"])

    assert result.ok is True
    assert not (repo / "tracked.txt").exists()
    # ``git rm`` stages the deletion.
    assert [(c.path, c.status) for c in (await client.get_changes(str(repo))).staged] == [
        ("tracked.txt", "D")
    ]


async def test_delete_files_untracked(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    (repo / "scratch.txt").write_text("temp")

    result = await client.delete_files(str(repo), ["scratch.txt"])

    assert result.ok is True
    assert not (repo / "scratch.txt").exists()
    assert (await client.get_changes(str(repo))).untracked == []


async def test_delete_files_mixed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "tracked.txt", "v1")
    (repo / "scratch.txt").write_text("temp")

    result = await client.delete_files(str(repo), ["tracked.txt", "scratch.txt"])

    assert result.ok is True
    assert not (repo / "tracked.txt").exists()
    assert not (repo / "scratch.txt").exists()


async def test_delete_files_rejects_traversal(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    secret = tmp_path / "OUTSIDE.txt"
    secret.write_text("secret")

    with pytest.raises(GitAutomationError) as exc:
        await client.delete_files(str(repo), ["../OUTSIDE.txt"])

    assert exc.value.code == "invalid_argument"
    assert exc.value.status_code == 400
    assert secret.exists()  # untouched


async def test_stage_then_unstage(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    (repo / "a.txt").write_text("a")

    staged = await client.stage(str(repo), ["a.txt"])
    assert staged.ok is True
    assert [c.path for c in (await client.get_changes(str(repo))).staged] == ["a.txt"]

    unstaged = await client.unstage(str(repo), ["a.txt"])
    assert unstaged.ok is True
    changes = await client.get_changes(str(repo))
    assert changes.staged == []
    assert changes.untracked == ["a.txt"]


async def test_discard_restores_working_tree(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "original")
    (repo / "f.txt").write_text("modified")

    result = await client.discard(str(repo), ["f.txt"])
    assert result.ok is True
    assert (repo / "f.txt").read_text() == "original"


# --- commit ---------------------------------------------------------------


async def test_commit_staged_changes(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    (repo / "b.txt").write_text("b")
    await client.stage(str(repo), ["b.txt"])

    result = await client.commit(str(repo), "add b")
    assert result.ok is True
    assert (await client.get_changes(str(repo))).staged == []


async def test_commit_empty_message_raises(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    with pytest.raises(GitAutomationError) as exc:
        await client.commit(str(repo), "   ")
    assert exc.value.code == "empty_commit_message"


async def test_commit_nothing_staged_fails(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    result = await client.commit(str(repo), "nothing here")
    assert result.ok is False
    assert "nothing" in result.output.lower()


# --- diff -----------------------------------------------------------------


async def test_get_diff_unstaged(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "line1\n")
    (repo / "f.txt").write_text("line1\nline2\n")

    diff = await client.get_diff(str(repo), "f.txt")
    assert diff.file == "f.txt"
    assert diff.binary is False
    assert "+line2" in diff.diff


async def test_get_diff_staged(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "line1\n")
    (repo / "f.txt").write_text("line1\nstaged\n")
    _git(repo, "add", "f.txt")

    diff = await client.get_diff(str(repo), "f.txt", staged=True)
    assert "+staged" in diff.diff


async def test_get_diff_untracked_shows_content(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    (repo / "brand.txt").write_text("fresh content\n")

    diff = await client.get_diff(str(repo), "brand.txt")
    assert "fresh content" in diff.diff


# --- branches -------------------------------------------------------------


async def test_list_branches_local_and_current(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    _git(repo, "branch", "feature")

    branches = await client.list_branches(str(repo))
    assert branches.current == "main"
    names = {b.name: b for b in branches.local}
    assert set(names) == {"main", "feature"}
    assert names["main"].is_current is True
    assert names["feature"].is_current is False
    assert names["main"].upstream is None


async def test_list_branches_upstream_ahead_behind(tmp_path: Path) -> None:
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)], capture_output=True, check=True
    )
    work = _init_repo(tmp_path / "work")
    _git(work, "remote", "add", "origin", str(remote))
    _commit(work)
    _git(work, "push", "-u", "origin", "main")
    # One local commit ahead of upstream.
    _commit(work, "ahead.txt", "x")

    branches = await client.list_branches(str(work))
    main = {b.name: b for b in branches.local}["main"]
    assert main.upstream == "origin/main"
    assert main.ahead == 1
    assert main.behind == 0

    remotes = {r.name for r in branches.remote}
    assert "origin/main" in remotes
    assert not any(name.endswith("/HEAD") for name in remotes)


async def test_create_checkout_delete_branch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)

    created = await client.create_branch(str(repo), "topic", checkout=True)
    assert created.ok is True
    assert (await client.list_branches(str(repo))).current == "topic"

    await client.checkout_branch(str(repo), "main")
    assert (await client.list_branches(str(repo))).current == "main"

    deleted = await client.delete_branch(str(repo), "topic", force=True)
    assert deleted.ok is True
    assert "topic" not in {b.name for b in (await client.list_branches(str(repo))).local}


async def test_merge_branch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    await client.create_branch(str(repo), "feature", checkout=True)
    _commit(repo, "feature.txt", "feature")
    await client.checkout_branch(str(repo), "main")

    result = await client.merge_branch(str(repo), "feature")
    assert result.ok is True
    assert (repo / "feature.txt").exists()


# --- graph ----------------------------------------------------------------


async def test_get_graph_parses_commits_parents_refs(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "a")
    _git(repo, "tag", "v1")
    _commit(repo, "b.txt", "b")

    commits = await client.get_graph(str(repo))
    assert len(commits) == 2

    head = commits[0]
    assert head.is_head is True
    assert head.subject == "add b.txt"
    assert len(head.sha) == 40
    assert head.short and len(head.short) >= 7
    # The newest commit has exactly one parent (the first commit).
    assert head.parents == [commits[1].sha]
    assert commits[1].parents == []
    # Ref decorations are captured.
    assert any("v1" in ref for ref in commits[1].refs)
    assert any("main" in ref for ref in head.refs)


async def test_get_graph_includes_all_branches(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "a")
    await client.create_branch(str(repo), "feature", checkout=True)
    _commit(repo, "f.txt", "f")
    await client.checkout_branch(str(repo), "main")

    commits = await client.get_graph(str(repo))
    subjects = {c.subject for c in commits}
    assert "add f.txt" in subjects  # present via --all even though not on HEAD


async def test_get_graph_limit(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    for i in range(5):
        _commit(repo, f"f{i}.txt", str(i))
    commits = await client.get_graph(str(repo), limit=3)
    assert len(commits) == 3


async def test_get_graph_empty_repo(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    assert await client.get_graph(str(repo)) == []


async def test_changes_invalid_path_raises() -> None:
    with pytest.raises(GitAutomationError) as exc:
        await client.get_changes("/nope/not/here-xyz")
    assert exc.value.code == "invalid_path"
