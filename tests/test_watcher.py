"""Tests for the repo file-watcher core and the ``/watch`` WebSocket gate.

These exercise a live filesystem watcher, so they are inherently timing-based.
They use small timeouts and are skipped where ``watchfiles`` cannot import (a
platform that cannot watch).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

watchfiles = pytest.importorskip("watchfiles", reason="watchfiles unavailable on this platform")

from git_automation.core.errors import GitAutomationError  # noqa: E402 - after importorskip
from git_automation.core.watcher import watch_repo  # noqa: E402 - after importorskip

# Time to let the OS watcher arm before we mutate the tree.
_ARM_DELAY = 0.4
# Generous detection window; a single change is yielded after awatch's quiet
# step (~50ms), so this rarely waits long.
_DETECT_TIMEOUT = 5.0


async def test_validates_path_on_iteration() -> None:
    gen = watch_repo("/no/such/repo/here")
    with pytest.raises(GitAutomationError) as excinfo:
        await gen.__anext__()
    assert excinfo.value.code == "invalid_path"
    await gen.aclose()


async def test_detects_working_tree_change(tmp_path: Path) -> None:
    gen = watch_repo(str(tmp_path))
    task = asyncio.ensure_future(gen.__anext__())
    try:
        await asyncio.sleep(_ARM_DELAY)
        (tmp_path / "hello.txt").write_text("hi")
        paths = await asyncio.wait_for(task, timeout=_DETECT_TIMEOUT)
        assert "hello.txt" in paths
    finally:
        await gen.aclose()


async def test_detects_git_refs_change(tmp_path: Path) -> None:
    refs_heads = tmp_path / ".git" / "refs" / "heads"
    refs_heads.mkdir(parents=True)
    gen = watch_repo(str(tmp_path))
    task = asyncio.ensure_future(gen.__anext__())
    try:
        await asyncio.sleep(_ARM_DELAY)
        (refs_heads / "master").write_text("0" * 40)
        paths = await asyncio.wait_for(task, timeout=_DETECT_TIMEOUT)
        assert ".git/refs/heads/master" in paths
    finally:
        await gen.aclose()


async def test_ignores_pure_git_noise(tmp_path: Path) -> None:
    # Pre-create the noisy .git internals so only file writes (not dir creation)
    # happen while watching.
    objects = tmp_path / ".git" / "objects" / "ab"
    objects.mkdir(parents=True)
    logs = tmp_path / ".git" / "logs"
    logs.mkdir(parents=True)

    gen = watch_repo(str(tmp_path))
    task = asyncio.ensure_future(gen.__anext__())
    try:
        await asyncio.sleep(_ARM_DELAY)
        (objects / "deadbeef").write_text("loose object")
        (tmp_path / ".git" / "COMMIT_EDITMSG").write_text("wip")
        (logs / "HEAD").write_text("reflog entry")
        # None of those are in the allow-list, so nothing should be yielded.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await gen.aclose()


async def test_cancels_cleanly(tmp_path: Path) -> None:
    gen = watch_repo(str(tmp_path))
    task = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(_ARM_DELAY)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # aclose must run awatch's teardown without raising.
    await gen.aclose()


def test_ws_rejects_foreign_origin(tmp_path: Path) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from git_automation.web.api import watch

    app = FastAPI()
    app.include_router(watch.router)
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            f"/watch?path={tmp_path}", headers={"origin": "http://evil.example"}
        ) as ws:
            ws.receive_json()
    assert excinfo.value.code == 1008
