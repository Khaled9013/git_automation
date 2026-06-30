"""Tests for Slice 4 hunk staging on real temp repositories."""

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


# A 10-line file so two edits (line 1 and line 10) sit far enough apart that
# git emits two distinct @@ hunks rather than merging them.
_BASE = "".join(f"line {i}\n" for i in range(1, 11))
_EDITED = "".join(
    ("LINE 1 CHANGED\n" if i == 1 else "LINE 10 CHANGED\n" if i == 10 else f"line {i}\n")
    for i in range(1, 11)
)


async def test_get_hunks_returns_two_separate_hunks(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", _BASE)
    (repo / "f.txt").write_text(_EDITED)

    file_hunks = await client.get_hunks(str(repo), "f.txt")

    assert file_hunks.binary is False
    assert len(file_hunks.hunks) == 2
    for hunk in file_hunks.hunks:
        assert hunk.header.startswith("@@") and hunk.header.endswith("@@")
        assert hunk.patch.startswith("diff --git")
        assert hunk.patch.endswith("\n")
        assert any(line.type == "+" for line in hunk.lines)
        assert any(line.type == "-" for line in hunk.lines)


async def test_stage_hunk_stages_only_that_hunk(tmp_path: Path) -> None:
    """The precise round-trip: stage one of two hunks, the other stays unstaged."""
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", _BASE)
    (repo / "f.txt").write_text(_EDITED)

    hunks = (await client.get_hunks(str(repo), "f.txt")).hunks
    assert len(hunks) == 2
    first = hunks[0]

    result = await client.stage_hunk(str(repo), "f.txt", first.patch)
    assert result.ok is True

    # The staged diff now contains only the first hunk's change (line 1)...
    staged_diff = _git(repo, "diff", "--cached", "--", "f.txt")
    assert "LINE 1 CHANGED" in staged_diff
    assert "LINE 10 CHANGED" not in staged_diff

    # ...and the worktree diff retains only the second, still-unstaged hunk.
    unstaged_diff = _git(repo, "diff", "--", "f.txt")
    assert "LINE 10 CHANGED" in unstaged_diff
    assert "LINE 1 CHANGED" not in unstaged_diff

    # get_hunks(staged=True) now reports exactly one staged hunk.
    staged_hunks = (await client.get_hunks(str(repo), "f.txt", staged=True)).hunks
    assert len(staged_hunks) == 1


async def test_unstage_hunk_reverses_the_stage(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", _BASE)
    (repo / "f.txt").write_text(_EDITED)

    hunks = (await client.get_hunks(str(repo), "f.txt")).hunks
    await client.stage_hunk(str(repo), "f.txt", hunks[0].patch)

    # Re-read the staged hunk and reverse it.
    staged = (await client.get_hunks(str(repo), "f.txt", staged=True)).hunks
    assert len(staged) == 1
    result = await client.unstage_hunk(str(repo), "f.txt", staged[0].patch)
    assert result.ok is True

    # Nothing staged anymore; both changes are back in the worktree.
    assert _git(repo, "diff", "--cached", "--", "f.txt") == ""
    worktree_diff = _git(repo, "diff", "--", "f.txt")
    assert "LINE 1 CHANGED" in worktree_diff
    assert "LINE 10 CHANGED" in worktree_diff


async def test_get_hunks_binary_reports_no_hunks(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02zero")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add blob")
    (repo / "blob.bin").write_bytes(b"\x00\x09\x09changed\x00")

    file_hunks = await client.get_hunks(str(repo), "blob.bin")
    assert file_hunks.binary is True
    assert file_hunks.hunks == []


async def test_get_hunks_clean_file_is_empty(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", _BASE)
    file_hunks = await client.get_hunks(str(repo), "f.txt")
    assert file_hunks.binary is False
    assert file_hunks.hunks == []


async def test_hunk_no_trailing_newline_round_trips(tmp_path: Path) -> None:
    """A file with no final newline must still produce an applyable patch."""
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "alpha\n")
    (repo / "f.txt").write_text("alpha-edited")  # no trailing newline

    hunks = (await client.get_hunks(str(repo), "f.txt")).hunks
    assert len(hunks) == 1
    result = await client.stage_hunk(str(repo), "f.txt", hunks[0].patch)
    assert result.ok is True
    assert "alpha-edited" in _git(repo, "diff", "--cached", "--", "f.txt")


async def test_get_hunks_rejects_path_traversal(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    with pytest.raises(GitAutomationError) as excinfo:
        await client.get_hunks(str(repo), "../escape.txt")
    assert excinfo.value.status_code == 400


async def test_stage_hunk_rejects_dotgit_path(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    with pytest.raises(GitAutomationError):
        await client.stage_hunk(str(repo), ".git/config", "irrelevant")
