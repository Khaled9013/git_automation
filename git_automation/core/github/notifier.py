"""Background poller that fires native OS notifications for new mentions/assigns.

An async loop polls :func:`git_automation.core.github.client.list_notifications`
on GitHub's advertised cadence (``X-Poll-Interval``, ~60s; see :func:`_poll_delay`),
diffs the threads against a persisted "already seen" id set, and for each *new*
thread whose ``reason`` is notify-worthy fires a native Linux notification via
``notify-send``. The notifications API is server-cached to roughly that cadence,
so polling faster returns identical data while risking secondary rate limiting.

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
import os
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from git_automation.core.github import client
from git_automation.core.github.hub import hub
from git_automation.core.github.models import Notification

logger = logging.getLogger(__name__)

# Notification reasons worth an OS-level interrupt: anything personally directed
# at the user or a thread they're actively participating in. This is deliberately
# broad because GitHub reuses ONE thread per issue and, after your first mention,
# auto-subscribes you -- so a *repeat* mention usually arrives with reason
# ``comment`` (or ``author``), not ``mention``. Keeping those here is what makes
# the 2nd/3rd/4th mention notify you, not just the first. Pure repo-watching
# (``subscribed``) and automation (``ci_activity``/``push``) are intentionally
# excluded so a heavy watcher isn't flooded.
_NOTIFY_REASONS = {
    "mention",
    "team_mention",
    "assign",
    "review_requested",
    "comment",
    "author",
    "manual",
    "invitation",
}

# Poll cadence. We HONOR GitHub's advertised ``X-Poll-Interval`` (~60s): the
# notifications API is server-cached to roughly that cadence, so polling faster
# returns identical data while risking secondary rate limiting -- which would
# itself stall the feed. ``GITAUTO_POLL_INTERVAL`` forces a faster cadence (never
# below the hard floor) for anyone who accepts that trade-off.
_MIN_POLL_INTERVAL = 10  # hard floor for a manual override

# Repo-local state dir (gitignored); keeps the poller self-contained.
_DEFAULT_STATE_DIR = Path(".gitauto")
_STATE_FILENAME = "notifier-state.json"

_TITLES = {
    "mention": "GitHub: mentioned you",
    "team_mention": "GitHub: your team was mentioned",
    "assign": "GitHub: assigned to you",
    "review_requested": "GitHub: review requested",
    "comment": "GitHub: new comment",
    "author": "GitHub: new activity on your thread",
    "manual": "GitHub: new activity",
    "invitation": "GitHub: invitation",
}

# An async callback that delivers one notification. Injectable for tests.
NotifyFn = Callable[[Notification], Awaitable[None]]

# An async callback that broadcasts an event to live subscribers (default
# ``hub.publish``). Injectable for tests so no real fan-out is required.
PublishFn = Callable[[dict], Awaitable[None]]


def _poll_delay() -> float:
    """Return how many seconds to wait before the next poll.

    Honors GitHub's advertised ``X-Poll-Interval`` (~60s, exposed via
    :func:`client.notifications_poll_interval`). ``GITAUTO_POLL_INTERVAL`` forces
    a faster cadence for anyone who accepts the trade-off; either way the result
    is never below :data:`_MIN_POLL_INTERVAL`.
    """
    override = os.environ.get("GITAUTO_POLL_INTERVAL")
    if override:
        try:
            return float(max(_MIN_POLL_INTERVAL, int(override)))
        except ValueError:
            logger.warning("notifier: invalid GITAUTO_POLL_INTERVAL=%r; ignoring", override)
    return float(max(_MIN_POLL_INTERVAL, client.notifications_poll_interval()))


def _state_path(state_dir: Path | str | None = None) -> Path:
    """Return the JSON state-file path under ``state_dir`` (default ``.gitauto/``)."""
    base = Path(state_dir) if state_dir is not None else _DEFAULT_STATE_DIR
    return base / _STATE_FILENAME


def _load_seen(path: Path) -> dict[str, str]:
    """Load the persisted map of notification id -> last-seen ``updated_at``.

    Keying on ``updated_at`` (not just the id) is what lets a *re*-mention in an
    already-seen thread re-notify: GitHub reuses one thread id per issue and only
    bumps ``updated_at`` on new activity. A legacy list-format file (ids only) is
    migrated to those ids mapped to an empty marker.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    seen = data.get("seen") if isinstance(data, dict) else None
    if isinstance(seen, dict):
        return {str(k): str(v) for k, v in seen.items()}
    if isinstance(seen, list):  # legacy format: ids only
        return {str(x): "" for x in seen}
    return {}


def _save_seen(path: Path, seen: dict[str, str]) -> None:
    """Persist the seen map (id -> updated_at), swallowing/logging fs errors."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"seen": seen}))
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


async def _run_notify_send(title: str, body: str) -> bool:
    """Invoke ``notify-send title body``; return whether it ran and exited 0.

    Best-effort and never raises: an ``OSError`` (binary vanished mid-run) or a
    non-zero exit yields ``False``.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "notify-send",
            title,
            body,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        returncode = await proc.wait()
    except OSError:
        logger.warning("notifier: notify-send invocation failed", exc_info=True)
        return False
    return returncode == 0


async def spawn_notify(notification: Notification) -> None:
    """Fire a native notification via ``notify-send`` (best-effort, never raises)."""
    title, body = _format(notification)
    await _run_notify_send(title, body)


async def _noop_notify(_notification: Notification) -> None:
    """A notifier that does nothing (used when ``notify-send`` is unavailable)."""


async def poll_once(
    seen: dict[str, str],
    notify: NotifyFn,
    *,
    prime: bool = False,
    publish: PublishFn = hub.publish,
) -> dict[str, str]:
    """Run one poll: fetch notifications, notify on new mention/assign threads.

    After diffing, the full round is broadcast to live browser subscribers via
    ``publish`` as a single ``{"type":"notifications",...}`` event (always, even
    on the priming round, so a freshly connected UI gets current state).

    Args:
        seen: Map of already-seen notification id -> last-seen ``updated_at``;
            mutated in place. A thread counts as new when its id is unseen OR its
            ``updated_at`` changed since last seen (i.e. fresh activity such as a
            new mention in the same issue).
        notify: Async callback invoked once per *new* notify-worthy thread.
        prime: When ``True``, only record state (no notifications, no ``new``) --
            used for the first round so existing mentions are not replayed as
            fresh alerts.
        publish: Async broadcast sink for the live event (default ``hub.publish``).

    Returns:
        The (mutated) ``seen`` map, for convenience.
    """
    # Only unread threads (all=false): a read thread drops out of the response on
    # its own, and a re-mention returns it as a fresh unread thread with a bumped
    # updated_at -- which the (id, updated_at) diff below treats as new activity.
    notifications = await client.list_notifications()
    new_notes: list[Notification] = []
    for note in notifications:
        # GitHub reuses one notification thread (stable id) per issue/PR and
        # bumps updated_at on each new activity. Keying on (id, updated_at) means
        # a *re*-mention in an already-seen thread re-notifies, not just the
        # first mention ever.
        if seen.get(note.id) == note.updated_at:
            continue
        if not prime:
            new_notes.append(note)
            # Only *unread* mention/assign threads warrant an OS-level interrupt;
            # a thread already read (here or on github.com) must not re-alert.
            if note.unread and note.reason in _NOTIFY_REASONS:
                await notify(note)
        seen[note.id] = note.updated_at

    unread = sum(1 for note in notifications if note.unread)
    logger.info(
        "notifier: poll round — %d notifications (%d unread), %d new",
        len(notifications),
        unread,
        len(new_notes),
    )
    await publish(
        {
            "type": "notifications",
            "count": unread,
            "items": [note.model_dump() for note in notifications],
            "new": [note.model_dump() for note in new_notes],
        }
    )
    return seen


async def run_poller(
    *,
    notify: NotifyFn | None = None,
    publish: PublishFn = hub.publish,
    state_dir: Path | str | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_rounds: int | None = None,
) -> None:
    """Run the polling loop until cancelled (or ``max_rounds`` is reached).

    Args:
        notify: Notification sink; defaults to :func:`spawn_notify` when
            ``notify-send`` is available, otherwise a logged no-op.
        publish: Live-event broadcast sink passed to each poll round (default
            ``hub.publish``); the app injects ``hub.publish`` explicitly.
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
            await poll_once(seen, notify, prime=prime, publish=publish)
            _save_seen(path, seen)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("notifier: poll round failed", exc_info=True)
        prime = False
        rounds += 1
        if max_rounds is not None and rounds >= max_rounds:
            break
        await sleep(_poll_delay())


async def fire_test_notification(publish: PublishFn = hub.publish) -> bool:
    """Fire a one-off OS + in-app test alert so the user can verify delivery.

    Sends a ``notify-send`` ("git-automation", "Test notification ...") when the
    binary is available, and always broadcasts ``{"type":"test"}`` to live
    subscribers. Returns whether the OS notification was actually delivered
    (``notify-send`` present *and* exited 0); ``False`` lets the UI explain that
    OS alerts are unavailable while the in-app push still arrives.
    """
    delivered = False
    if notify_send_available():
        delivered = await _run_notify_send(
            "git-automation", "Test notification — OS alerts are working"
        )
    await publish({"type": "test"})
    return delivered
