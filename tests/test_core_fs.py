"""Tests for the read-only filesystem browser."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from git_automation.core import fs
from git_automation.core.errors import GitAutomationError


def test_home_dir_is_absolute() -> None:
    home = fs.home_dir()
    assert home == str(Path.home())
    assert os.path.isabs(home)


async def test_list_dirs_returns_sorted_directories_only(tmp_path: Path) -> None:
    (tmp_path / "Beta").mkdir()
    (tmp_path / "alpha").mkdir()
    (tmp_path / "Gamma").mkdir()
    (tmp_path / "a-file.txt").write_text("x")

    entries = await fs.list_dirs(str(tmp_path))

    assert [e.name for e in entries] == ["alpha", "Beta", "Gamma"]
    assert all(e.is_dir for e in entries)
    assert all(os.path.isabs(e.path) for e in entries)
    # The plain file is excluded.
    assert "a-file.txt" not in [e.name for e in entries]


async def test_list_dirs_flags_git_repositories(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()

    entries = {e.name: e for e in await fs.list_dirs(str(tmp_path))}

    assert entries["repo"].is_git_repo is True
    assert entries["plain"].is_git_repo is False


async def test_list_dirs_missing_path_raises_invalid_path() -> None:
    with pytest.raises(GitAutomationError) as exc:
        await fs.list_dirs("/nope/not/here-xyz")
    assert exc.value.code == "invalid_path"
    assert exc.value.status_code == 400


async def test_list_dirs_on_file_raises_invalid_path(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x")
    with pytest.raises(GitAutomationError) as exc:
        await fs.list_dirs(str(target))
    assert exc.value.code == "invalid_path"


async def test_list_dirs_empty_path_raises() -> None:
    with pytest.raises(GitAutomationError) as exc:
        await fs.list_dirs("")
    assert exc.value.code == "invalid_path"


async def test_list_dirs_permission_denied(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "child").mkdir()
    os.chmod(locked, 0o000)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("running as root; permission cannot be enforced")
        with pytest.raises(GitAutomationError) as exc:
            await fs.list_dirs(str(locked))
        assert exc.value.code == "permission_denied"
    finally:
        os.chmod(locked, 0o755)
