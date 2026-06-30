"""Background poller that fires native OS notifications for new mentions/assigns.

An async loop polls :func:`git_automation.core.github.client.list_notifications`
every ``max(60, X-Poll-Interval)`` seconds, diffs the threads against a persisted
"already seen" id set, and for each *new* thread whose ``reason`` is ``mention``
or ``assign`` fires a native Linux notification via ``notify-send``.

The first round after a fresh start only *primes* the seen-set (so launching the
app does not replay every existing mention as a new alert). ``notify-send`` is
detected once; when absent the loop still runs (and keeps state current) but
emits no notifications. The notify action is injectable so tests never spawn a
real subprocess or touch the network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from git_automation.core.github import client
from git_automation.core.github.models import Notification

logger = logging.getLogger(__name__)

# Only these notification reasons warrant an OS-level interrupt.
_NOTIFY_REASONS = {"mention", "assign"}

# Repo-local state dir (gitignored); keeps the poller self-contained.
_DEFAULT_STATE_DIR = Path(".gitauto")
_STATE_FILENAME = "notifier-state.json"

_TITLES = {
    "mention": "GitHub: mentioned you",
    "assign": "GitHub: assigned to you",
}

# An async callback that delivers one notification. Injectable for tests.
NotifyFn = Callable[[Notification], Awaitable[None]]


def _state_path(state_dir: Path | str | None = None) -> Path:
    """Return the JSON state-file path under ``state_dir`` (default ``.gitauto/``)."""
    base = Path(state_dir) if state_dir is not None else _DEFAULT_STATE_DIR
    return base / _STATE_FILENAME


def _load_seen(path: Path) -> set[str]:
    """Load the persisted set of already-seen notification ids (empty on miss)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return set()
    seen = data.get("seen") if isinstance(data, dict) else None
    return {str(x) for x in seen} if isinstance(seen, list) else set()


def _save_seen(path: Path, seen: set[str]) -> None:
    """Persist the seen-id set, swallowing (and logging) filesystem errors."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"seen": sorted(seen)}))
    except OSError:
        logger.warning("notifier: could not persist state to %s", path, exc_info=True)


def notify_send_available() -> bool:
    """Return whether the ``notify-send`` binary is on ``PATH``."""
    return shutil.which("notify-send") is not None


def _format(notification: Notification) -> tuple[str, str]:
    """Build the ``(title, body)`` pair for an OS notification."""
    title = _TITLES.get(notification.reason, "GitHub")
    parts = [p for p in (notification.repo, notification.title) if p]
    return title, ": ".join(parts) if parts else "New activity"


async def spawn_notify(notification: Notification) -> None:
    """Fire a native notification via ``notify-send`` (best-effort, never raises)."""
    title, body = _format(notification)
    try:
        proc = await asyncio.create_subprocess_exec(
            "notify-send",
            title,
            body,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except OSError:
        logger.warning("notifier: notify-send invocation failed", exc_info=True)


async def _noop_notify(_notification: Notification) -> None:
    """A notifier that does nothing (used when ``notify-send`` is unavailable)."""


async def poll_once(
    seen: set[str],
    notify: NotifyFn,
    *,
    prime: bool = False,
) -> set[str]:
    """Run one poll: fetch notifications, notify on new mention/assign threads.

    Args:
        seen: The set of already-seen notification ids; mutated in place to add
            every id observed this round.
        notify: Async callback invoked once per *new* notify-worthy thread.
        prime: When ``True``, only record ids (no notifications) -- used for the
            first round so existing mentions are not replayed as fresh alerts.

    Returns:
        The (mutated) ``seen`` set, for convenience.
    """
    notifications = await client.list_notifications()
    for note in notifications:
        if note.id in seen:
            continue
        if not prime and note.reason in _NOTIFY_REASONS:
            await notify(note)
        seen.add(note.id)
    return seen


async def run_poller(
    *,
    notify: NotifyFn | None = None,
    state_dir: Path | str | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_rounds: int | None = None,
) -> None:
    """Run the polling loop until cancelled (or ``max_rounds`` is reached).

    Args:
        notify: Notification sink; defaults to :func:`spawn_notify` when
            ``notify-send`` is available, otherwise a logged no-op.
        state_dir: Directory for the JSON seen-state file (default ``.gitauto/``).
        sleep: Injectable sleep (tests pass a fast/cancelling stub).
        max_rounds: Stop after this many rounds; ``None`` loops forever.
    """
    if notify is None:
        if notify_send_available():
            notify = spawn_notify
        else:
            logger.info("notifier: 'notify-send' not found; OS notifications disabled")
            notify = _noop_notify

    path = _state_path(state_dir)
    seen = _load_seen(path)
    prime = not path.exists()
    rounds = 0

    while max_rounds is None or rounds < max_rounds:
        try:
            await poll_once(seen, notify, prime=prime)
            _save_seen(path, seen)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("notifier: poll round failed", exc_info=True)
        prime = False
        rounds += 1
        if max_rounds is not None and rounds >= max_rounds:
            break
        interval = max(60, client.notifications_poll_interval())
        await sleep(interval)
