"""Tests for the async git client against real, hermetic temp repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from git_automation.core import process
from git_automation.core.errors import GitAutomationError
from git_automation.core.git import client
from git_automation.core.process import ProcessResult


def _git(cwd: Path, *args: str) -> str:
    """Run git synchronously for test setup, returning stdout."""
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


async def test_run_git_reports_success() -> None:
    result = await client.run_git(["--version"])
    assert result.ok is True
    assert "git version" in result.output


async def test_run_git_reports_failure() -> None:
    result = await client.run_git(["not-a-real-subcommand"])
    assert result.ok is False
    assert result.output


async def test_list_remotes_empty(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    assert await client.list_remotes(str(repo)) == []


async def test_list_remotes_returns_configured(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "remote", "add", "origin", "https://example.com/a.git")
    _git(repo, "remote", "add", "fork", "https://example.com/b.git")
    remotes = await client.list_remotes(str(repo))
    by_name = {r.name: r.url for r in remotes}
    assert by_name == {
        "origin": "https://example.com/a.git",
        "fork": "https://example.com/b.git",
    }


async def test_get_status_clean_repo(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    status = await client.get_status(str(repo))
    assert status.current_branch == "main"
    assert status.upstream is None
    assert status.ahead == 0
    assert status.behind == 0
    assert status.dirty is False
    assert status.remotes == []
    assert status.path == str(repo)


async def test_get_status_dirty(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    (repo / "untracked.txt").write_text("x")
    status = await client.get_status(str(repo))
    assert status.dirty is True


async def test_get_status_invalid_path_raises() -> None:
    with pytest.raises(GitAutomationError) as exc:
        await client.get_status("/this/path/does/not/exist-xyz")
    assert exc.value.code == "invalid_path"
    assert exc.value.status_code == 400


async def test_get_status_detached_head(tmp_path: Path) -> None:
    """A detached HEAD reports ``current_branch is None`` (not the SHA)."""
    repo = _init_repo(tmp_path / "repo")
    _commit(repo)
    _commit(repo, name="second.txt", content="two")
    _git(repo, "checkout", "--detach", "HEAD")
    status = await client.get_status(str(repo))
    assert status.current_branch is None
    assert status.upstream is None
    assert status.ahead == 0
    assert status.behind == 0


async def test_get_status_ahead_and_behind(tmp_path: Path) -> None:
    """Ahead+behind counts come from the single porcelain-v2 branch header."""
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)], capture_output=True, check=True
    )
    work = _init_repo(tmp_path / "work")
    _git(work, "remote", "add", "origin", str(remote))
    _commit(work)
    await client.push(str(work), "origin", "main", set_upstream=True)

    # Diverge: local moves ahead by one, and origin/main is rewound so we're also
    # behind by one relative to a freshly fetched upstream.
    _commit(work, name="local.txt", content="local")  # +1 ahead

    # Push a different commit from a second clone so upstream advances.
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(remote), str(clone)], capture_output=True, check=True)
    _git(clone, "config", "user.name", "Clone")
    _git(clone, "config", "user.email", "clone@example.com")
    _git(clone, "config", "commit.gpgsign", "false")
    _commit(clone, name="remote.txt", content="remote")
    _git(clone, "push", "origin", "main")

    await client.fetch(str(work), "origin")
    status = await client.get_status(str(work))
    assert status.upstream is not None
    assert status.upstream.remote == "origin"
    assert status.upstream.branch == "main"
    assert status.ahead == 1
    assert status.behind == 1


async def test_get_status_dirty_staged_unstaged_untracked_renamed(tmp_path: Path) -> None:
    """A mix of staged, unstaged, untracked, and renamed changes -> dirty."""
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, name="orig.txt", content="a\n")
    _commit(repo, name="mod.txt", content="b\n")

    _git(repo, "mv", "orig.txt", "renamed.txt")  # staged rename
    (repo / "mod.txt").write_text("b changed\n")  # unstaged modification
    (repo / "staged_new.txt").write_text("new\n")
    _git(repo, "add", "staged_new.txt")  # staged addition
    (repo / "untracked.txt").write_text("u\n")  # untracked

    status = await client.get_status(str(repo))
    assert status.dirty is True
    assert status.current_branch == "main"


def test_parse_status_v2_no_upstream() -> None:
    out = "# branch.oid abcdef\n# branch.head main\n"
    branch, upstream, ahead, behind, dirty = client._parse_status_v2(out)
    assert branch == "main"
    assert upstream is None
    assert (ahead, behind, dirty) == (0, 0, False)


def test_parse_status_v2_detached() -> None:
    out = "# branch.oid abcdef\n# branch.head (detached)\n"
    branch, upstream, ahead, behind, dirty = client._parse_status_v2(out)
    assert branch is None
    assert upstream is None
    assert dirty is False


def test_parse_status_v2_ahead_behind_upstream() -> None:
    out = (
        "# branch.oid abcdef\n"
        "# branch.head main\n"
        "# branch.upstream origin/main\n"
        "# branch.ab +3 -2\n"
    )
    branch, upstream, ahead, behind, dirty = client._parse_status_v2(out)
    assert branch == "main"
    assert upstream is not None
    assert (upstream.remote, upstream.branch) == ("origin", "main")
    assert (ahead, behind) == (3, 2)
    assert dirty is False


def test_parse_status_v2_upstream_with_slashed_branch() -> None:
    """A remote branch containing a slash keeps its full path after the remote."""
    out = "# branch.head feat\n# branch.upstream origin/feature/x\n# branch.ab +0 -0\n"
    _, upstream, _, _, _ = client._parse_status_v2(out)
    assert upstream is not None
    assert upstream.remote == "origin"
    assert upstream.branch == "feature/x"


def test_parse_status_v2_dirty_change_entries() -> None:
    """Rename (2), tracked change (1), and untracked (?) lines all mark dirty."""
    out = (
        "# branch.head main\n"
        "2 R. N... 100644 100644 100644 aaa aaa R100 new.txt\torig.txt\n"
        "1 .M N... 100644 100644 100644 bbb bbb mod.txt\n"
        "? untracked.txt\n"
    )
    branch, upstream, ahead, behind, dirty = client._parse_status_v2(out)
    assert branch == "main"
    assert dirty is True


async def test_list_remotes_invalid_path_raises(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("not a directory")
    with pytest.raises(GitAutomationError) as exc:
        await client.list_remotes(str(not_a_dir))
    assert exc.value.code == "invalid_path"


async def test_push_fetch_pull_against_local_remote(tmp_path: Path) -> None:
    # Bare repo acts as the remote.
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)], capture_output=True, check=True
    )

    work = _init_repo(tmp_path / "work")
    _git(work, "remote", "add", "origin", str(remote))
    _commit(work)

    pushed = await client.push(str(work), "origin", "main", set_upstream=True)
    assert pushed.ok is True

    # Upstream is now visible in status.
    status = await client.get_status(str(work))
    assert status.upstream is not None
    assert status.upstream.remote == "origin"
    assert status.upstream.branch == "main"

    # Second clone fetches/pulls a new commit pushed from the first.
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(remote), str(clone)], capture_output=True, check=True)
    _git(clone, "config", "user.name", "Clone User")
    _git(clone, "config", "user.email", "clone@example.com")

    _commit(work, name="second.txt", content="more")
    await client.push(str(work), "origin", "main")

    fetched = await client.fetch(str(clone), "origin")
    assert fetched.ok is True

    pulled = await client.pull(str(clone), "origin", "main")
    assert pulled.ok is True
    assert (clone / "second.txt").exists()


async def test_get_and_set_global_identity_monkeypatched(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    async def fake_run(
        args: list[str], cwd: str | None = None, stdin: str | None = None
    ) -> ProcessResult:
        calls.append(args)
        if args[:4] == ["git", "config", "--global", "--get"]:
            key = args[4]
            value = {"user.name": "Ada", "user.email": "ada@example.com"}[key]
            return ProcessResult(0, value + "\n", "")
        return ProcessResult(0, "", "")

    monkeypatch.setattr(process, "run_process", fake_run)

    name, email = await client.get_global_identity()
    assert name == "Ada"
    assert email == "ada@example.com"

    await client.set_global_identity("Grace", "grace@example.com")
    assert ["git", "config", "--global", "user.name", "Grace"] in calls
    assert ["git", "config", "--global", "user.email", "grace@example.com"] in calls


async def test_get_global_identity_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(
        args: list[str], cwd: str | None = None, stdin: str | None = None
    ) -> ProcessResult:
        return ProcessResult(1, "", "")

    monkeypatch.setattr(process, "run_process", fake_run)
    assert await client.get_global_identity() == (None, None)
