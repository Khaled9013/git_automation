"""PTY-backed terminal session manager.

Trust model
-----------
This module spawns the user's interactive shell (``$SHELL``, falling back to
``/bin/bash``) connected to a pseudo-terminal. The shell runs with the full
privileges of the user running the server -- it is, by design, arbitrary code
execution. That is only acceptable because the web app binds ``127.0.0.1`` and
this capability is *never* exposed beyond localhost. Do not register the
terminal route on any non-loopback interface.

Conservative guarantees enforced here:

* **One PTY per session instance.** :meth:`PtySession.start` refuses to start a
  second child.
* **No shell string building.** The shell program is spawned directly via an
  argument list (``create_subprocess_exec``); no value is ever passed through a
  shell interpreter by this module.
* **Validated working directory.** The cwd is checked with
  :func:`git_automation.core.process.validate_repo_path` before spawning.
* **Deterministic teardown.** :meth:`PtySession.terminate` always kills the
  whole child process group (SIGHUP then SIGKILL), reaps the child to avoid
  zombies, removes the event-loop reader, and closes the master fd -- so a
  dropped WebSocket can never leak a process or file descriptor.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import signal
import struct
import termios
from collections.abc import Iterable

from git_automation.core.errors import GitAutomationError
from git_automation.core.process import validate_repo_path

_DEFAULT_SHELL = "/bin/bash"
_READ_CHUNK = 65536
_MIN_DIM = 1
_MAX_DIM = 9999

# Internal sentinel pushed onto the output queue when the PTY reaches EOF.
_EOF = object()


def _default_shell_command() -> list[str]:
    """Return the argument list for the user's preferred shell."""
    shell = os.environ.get("SHELL") or _DEFAULT_SHELL
    return [shell]


def _clamp_dimension(value: int) -> int:
    """Clamp a terminal column/row count into a sane positive range."""
    return max(_MIN_DIM, min(_MAX_DIM, int(value)))


class PtySession:
    """A single interactive shell attached to a pseudo-terminal.

    The session owns exactly one child process and one PTY master fd. Output is
    drained off the event loop with a non-blocking reader and buffered into an
    :class:`asyncio.Queue`; callers consume it with :meth:`read`.
    """

    def __init__(
        self,
        cwd: str,
        *,
        command: Iterable[str] | None = None,
        env: dict[str, str] | None = None,
        cols: int = 80,
        rows: int = 24,
    ) -> None:
        """Create (but do not start) a session.

        Args:
            cwd: Working directory for the shell. Validated immediately.
            command: Override the program to spawn (used by tests). Defaults to
                the user's ``$SHELL``.
            env: Override the child environment. Defaults to the server's
                environment with ``TERM=xterm-256color``.
            cols: Initial terminal width.
            rows: Initial terminal height.

        Raises:
            GitAutomationError: If ``cwd`` is not an existing directory.
        """
        self._cwd = validate_repo_path(cwd)
        self._command = list(command) if command is not None else _default_shell_command()
        if not self._command:
            raise GitAutomationError("invalid_command", "Terminal command is empty.", 400)
        if env is not None:
            self._env = dict(env)
        else:
            self._env = os.environ.copy()
            self._env.setdefault("TERM", "xterm-256color")
        self._cols = _clamp_dimension(cols)
        self._rows = _clamp_dimension(rows)

        self._master_fd: int | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[bytes | object] = asyncio.Queue()
        self._reader_registered = False
        self._eof_signalled = False
        self._terminated = False

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Spawn the shell on a fresh PTY and begin draining its output.

        Raises:
            GitAutomationError: If the session has already been started.
        """
        if self._proc is not None:
            raise GitAutomationError(
                "terminal_already_started",
                "This terminal session has already been started.",
                409,
            )

        master_fd, slave_fd = os.openpty()
        self._set_winsize(master_fd, self._cols, self._rows)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=self._cwd,
                env=self._env,
                # _acquire_controlling_tty calls setsid() itself, so we do NOT
                # also pass start_new_session (that would setsid twice -> EPERM).
                preexec_fn=_acquire_controlling_tty,
            )
        except OSError as exc:
            os.close(master_fd)
            os.close(slave_fd)
            raise GitAutomationError(
                "terminal_spawn_failed",
                f"Failed to start terminal: {exc}",
                500,
            ) from exc
        finally:
            # The parent never uses the slave end; the child holds its own dup.
            os.close(slave_fd)

        self._master_fd = master_fd
        os.set_blocking(master_fd, False)
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(master_fd, self._on_readable)
        self._reader_registered = True

    @property
    def running(self) -> bool:
        """True while the child process is alive."""
        return self._proc is not None and self._proc.returncode is None and not self._terminated

    @property
    def pid(self) -> int | None:
        """The child process id, or ``None`` before start."""
        return self._proc.pid if self._proc is not None else None

    @property
    def returncode(self) -> int | None:
        """The child's exit code, or ``None`` while it is still running."""
        return self._proc.returncode if self._proc is not None else None

    # -- io -----------------------------------------------------------------

    async def write(self, data: str | bytes) -> None:
        """Write ``data`` to the terminal's input (the shell's stdin)."""
        if self._master_fd is None:
            return
        buffer = data.encode() if isinstance(data, str) else bytes(data)
        while buffer:
            try:
                written = os.write(self._master_fd, buffer)
                buffer = buffer[written:]
            except BlockingIOError:
                await asyncio.sleep(0)
            except OSError:
                # Master closed underneath us (child gone) -- drop the input.
                return

    def resize(self, cols: int, rows: int) -> None:
        """Resize the terminal via ``TIOCSWINSZ``."""
        self._cols = _clamp_dimension(cols)
        self._rows = _clamp_dimension(rows)
        if self._master_fd is None:
            return
        try:
            self._set_winsize(self._master_fd, self._cols, self._rows)
        except OSError:
            # The terminal may already be tearing down; resizing is best-effort.
            pass

    async def read(self) -> bytes | None:
        """Return the next chunk of terminal output, or ``None`` at EOF.

        Once ``None`` is returned the session is finished; subsequent calls keep
        returning ``None``.
        """
        item = await self._queue.get()
        if item is _EOF:
            # Re-arm the sentinel so repeated reads continue to report EOF.
            self._queue.put_nowait(_EOF)
            return None
        assert isinstance(item, bytes)
        return item

    async def terminate(self) -> None:
        """Kill the child, reap it, and release the PTY. Idempotent."""
        if self._terminated:
            return
        self._terminated = True

        if self._reader_registered and self._loop is not None and self._master_fd is not None:
            try:
                self._loop.remove_reader(self._master_fd)
            except (OSError, ValueError):
                pass
            self._reader_registered = False

        if self._proc is not None and self._proc.returncode is None:
            await self._kill_child(self._proc)

        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

        self._signal_eof()

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _set_winsize(fd: int, cols: int, rows: int) -> None:
        """Apply a window size to ``fd`` using ``TIOCSWINSZ``."""
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)

    def _on_readable(self) -> None:
        """Event-loop callback: drain available PTY output into the queue."""
        if self._master_fd is None:
            return
        try:
            data = os.read(self._master_fd, _READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:
            # On Linux a closed slave surfaces as EIO -> treat it as EOF.
            data = b""
        if not data:
            self._stop_reader()
            self._signal_eof()
            return
        self._queue.put_nowait(data)

    def _stop_reader(self) -> None:
        """Remove the event-loop reader (e.g. on EOF)."""
        if self._reader_registered and self._loop is not None and self._master_fd is not None:
            try:
                self._loop.remove_reader(self._master_fd)
            except (OSError, ValueError):
                pass
            self._reader_registered = False

    def _signal_eof(self) -> None:
        """Push the EOF sentinel exactly once."""
        if not self._eof_signalled:
            self._eof_signalled = True
            self._queue.put_nowait(_EOF)

    @staticmethod
    async def _kill_child(proc: asyncio.subprocess.Process) -> None:
        """Terminate ``proc``'s process group and reap it to avoid zombies."""
        for sig in (signal.SIGHUP, signal.SIGKILL):
            if proc.returncode is not None:
                break
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except ProcessLookupError:
                break
            except OSError:
                # Fall back to signalling the bare process.
                try:
                    proc.send_signal(sig)
                except ProcessLookupError:
                    break
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
                return
            except TimeoutError:
                continue
        # Ensure the transport reaps the child even if it died on its own.
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                pass


def _acquire_controlling_tty() -> None:
    """Make the child a session leader and adopt its PTY as controlling tty.

    Runs in the child between fork and exec. By this point subprocess has already
    duplicated the slave PTY onto fds 0/1/2, so ``TIOCSCTTY`` on fd 0 attaches
    the controlling terminal -- enabling job control in the spawned shell.
    """
    os.setsid()
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except OSError:
        pass
