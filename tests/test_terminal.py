"""Unit tests for the PTY session manager.

POSIX-only: the manager relies on ``os.openpty``/``termios``. The whole module
is skipped where a PTY is unavailable (e.g. native Windows).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from git_automation.core.errors import GitAutomationError

pytestmark = pytest.mark.skipif(
    not hasattr(os, "openpty") or sys.platform == "win32",
    reason="PTY support requires a POSIX platform",
)

from git_automation.core.terminal import PtySession  # noqa: E402 - after skipif guard


async def _read_until(session: PtySession, needle: bytes, timeout: float = 5.0) -> bytes:
    """Accumulate output until ``needle`` appears or EOF/timeout is reached."""
    collected = bytearray()

    async def _drain() -> None:
        while needle not in collected:
            chunk = await session.read()
            if chunk is None:
                break
            collected.extend(chunk)

    await asyncio.wait_for(_drain(), timeout=timeout)
    return bytes(collected)


async def test_validates_cwd(tmp_path: Path) -> None:
    with pytest.raises(GitAutomationError) as excinfo:
        PtySession(str(tmp_path / "does-not-exist"))
    assert excinfo.value.code == "invalid_path"


async def test_spawn_and_read_output(tmp_path: Path) -> None:
    session = PtySession(str(tmp_path), command=["echo", "hello-pty"])
    await session.start()
    try:
        output = await _read_until(session, b"hello-pty")
        assert b"hello-pty" in output
    finally:
        await session.terminate()


async def test_read_returns_none_at_eof(tmp_path: Path) -> None:
    session = PtySession(str(tmp_path), command=["echo", "bye"])
    await session.start()
    try:
        await _read_until(session, b"bye")
        # Once the child exits the reader reports EOF and keeps doing so.
        assert await asyncio.wait_for(session.read(), timeout=5.0) is None
        assert await asyncio.wait_for(session.read(), timeout=1.0) is None
    finally:
        await session.terminate()


async def test_write_is_delivered_to_child(tmp_path: Path) -> None:
    # A python child echoes back exactly one line it reads from stdin.
    script = "import sys; line = sys.stdin.readline(); sys.stdout.write('GOT:' + line)"
    session = PtySession(str(tmp_path), command=[sys.executable, "-c", script])
    await session.start()
    try:
        await session.write("ping\n")
        output = await _read_until(session, b"GOT:ping")
        assert b"GOT:ping" in output
    finally:
        await session.terminate()


async def test_resize_propagates_to_child(tmp_path: Path) -> None:
    # Child blocks on a line, then reports the size of its controlling tty.
    script = (
        "import os, sys; sys.stdin.readline();"
        " s = os.get_terminal_size(); print(f'SIZE={s.columns}x{s.lines}')"
    )
    session = PtySession(str(tmp_path), command=[sys.executable, "-c", script])
    await session.start()
    try:
        session.resize(cols=123, rows=45)
        await session.write("go\n")
        output = await _read_until(session, b"SIZE=")
        assert b"SIZE=123x45" in output
    finally:
        await session.terminate()


async def test_terminate_kills_child_and_is_idempotent(tmp_path: Path) -> None:
    script = "import time; time.sleep(60)"
    session = PtySession(str(tmp_path), command=[sys.executable, "-c", script])
    await session.start()
    pid = session.pid
    assert pid is not None
    assert session.running

    await session.terminate()
    assert not session.running
    assert session.returncode is not None  # child was reaped (no zombie)

    # The process is really gone.
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

    # Calling terminate again is a no-op.
    await session.terminate()


async def test_cannot_start_twice(tmp_path: Path) -> None:
    session = PtySession(str(tmp_path), command=["echo", "x"])
    await session.start()
    try:
        with pytest.raises(GitAutomationError) as excinfo:
            await session.start()
        assert excinfo.value.code == "terminal_already_started"
    finally:
        await session.terminate()
