"""End-to-end integration flows against real temp repositories.

These exercise full sequences (init -> stage -> commit -> graph, branch ->
merge -> graph, push/fetch/pull) and assert the state transitions at each step,
rather than testing a single function in isolation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

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


async def test_full_change_stage_commit_graph_flow(tmp_path: Path) -> None:
    """init -> write -> changes(untracked) -> stage -> changes(staged) ->
    commit -> graph & branches reflect the new commit."""
    repo = _init_repo(tmp_path / "repo")

    # Freshly written file shows up as untracked, nothing staged.
    (repo / "hello.txt").write_text("hi\n")
    changes = await client.get_changes(str(repo))
    assert changes.untracked == ["hello.txt"]
    assert changes.staged == []
    assert changes.unstaged == []

    # After staging it moves to staged as an addition.
    assert (await client.stage(str(repo), ["hello.txt"])).ok is True
    changes = await client.get_changes(str(repo))
    assert [(c.path, c.status) for c in changes.staged] == [("hello.txt", "A")]
    assert changes.untracked == []

    # The graph is empty until the first commit lands (unborn repo).
    assert await client.get_graph(str(repo)) == []

    # Commit clears the staged set.
    assert (await client.commit(str(repo), "first commit")).ok is True
    assert (await client.get_changes(str(repo))).staged == []

    # Graph and branches now reflect the new commit.
    graph = await client.get_graph(str(repo))
    assert len(graph) == 1
    assert graph[0].subject == "first commit"
    assert graph[0].is_head is True
    assert graph[0].parents == []
    assert any("main" in ref for ref in graph[0].refs)

    branches = await client.list_branches(str(repo))
    assert branches.current == "main"
    assert {b.name for b in branches.local} == {"main"}


async def test_branch_merge_produces_two_parent_commit(tmp_path: Path) -> None:
    """branch + checkout -> commit -> merge back -> graph shows a 2-parent merge."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "base.txt").write_text("base\n")
    await client.stage(str(repo), ["base.txt"])
    await client.commit(str(repo), "base")

    # Diverge: a commit on a feature branch and another on main.
    await client.create_branch(str(repo), "feature", checkout=True)
    (repo / "feature.txt").write_text("feature\n")
    await client.stage(str(repo), ["feature.txt"])
    await client.commit(str(repo), "feature work")

    await client.checkout_branch(str(repo), "main")
    (repo / "main.txt").write_text("main\n")
    await client.stage(str(repo), ["main.txt"])
    await client.commit(str(repo), "main work")

    # A non-fast-forward merge creates a merge commit with two parents.
    merged = await client.merge_branch(str(repo), "feature")
    assert merged.ok is True
    assert (repo / "feature.txt").exists()

    graph = await client.get_graph(str(repo))
    merge_commit = graph[0]
    assert merge_commit.is_head is True
    assert len(merge_commit.parents) == 2


async def test_push_pull_round_trip_updates_behind_then_pulls(tmp_path: Path) -> None:
    """Two clones of a bare remote: one pushes, the other sees 'behind' and pulls."""
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)], capture_output=True, check=True
    )

    alice = _init_repo(tmp_path / "alice")
    _git(alice, "remote", "add", "origin", str(remote))
    (alice / "f.txt").write_text("v1\n")
    _git(alice, "add", "-A")
    _git(alice, "commit", "-m", "v1")
    assert (await client.push(str(alice), "origin", "main", set_upstream=True)).ok is True

    bob = tmp_path / "bob"
    subprocess.run(["git", "clone", str(remote), str(bob)], capture_output=True, check=True)
    _git(bob, "config", "user.name", "Bob")
    _git(bob, "config", "user.email", "bob@example.com")

    # Alice pushes a second commit.
    (alice / "f.txt").write_text("v2\n")
    _git(alice, "add", "-A")
    _git(alice, "commit", "-m", "v2")
    assert (await client.push(str(alice), "origin", "main")).ok is True

    # Bob fetches: status should report him 1 commit behind, 0 ahead.
    assert (await client.fetch(str(bob), "origin")).ok is True
    status = await client.get_status(str(bob))
    assert status.behind == 1
    assert status.ahead == 0

    # Pull brings the change in and clears 'behind'.
    assert (await client.pull(str(bob), "origin", "main")).ok is True
    assert (bob / "f.txt").read_text() == "v2\n"
    assert (await client.get_status(str(bob))).behind == 0
