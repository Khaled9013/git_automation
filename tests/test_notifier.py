"""Tests for the background OS-notification poller (``core/github/notifier``).

These exercise the diff/notify logic and the loop without spawning a real
``notify-send`` subprocess or touching the network: ``client.list_notifications``
is stubbed and the notify sink is injected. Two poll rounds (round 2 introduces a
new mention) prove exactly one notification fires; non-mention/assign reasons are
ignored; and a missing ``notify-send`` degrades to a graceful no-op.
"""

from __future__ import annotations

import pytest

from git_automation.core.github import client, notifier
from git_automation.core.github.models import Notification


def _note(id_: str, reason: str, *, repo: str = "octocat/hello", title: str = "Hi") -> Notification:
    return Notification(
        id=id_,
        reason=reason,
        unread=True,
        title=title,
        subject_type="Issue",
        repo=repo,
        number=1,
        url="https://api.github.com/repos/octocat/hello/issues/1",
        updated_at="2026-06-30T10:00:00Z",
    )


def _stub_rounds(monkeypatch: pytest.MonkeyPatch, rounds: list[list[Notification]]) -> None:
    """Make ``client.list_notifications`` return each round's list in turn."""
    seq = iter(rounds)

    async def fake_list(**_kwargs):
        return next(seq)

    monkeypatch.setattr(client, "list_notifications", fake_list)


async def test_new_mention_in_round_two_fires_once(monkeypatch: pytest.MonkeyPatch) -> None:
    round1 = [_note("1", "mention", title="Old mention")]
    round2 = [
        _note("1", "mention", title="Old mention"),
        _note("2", "mention", title="Fresh mention"),
    ]
    _stub_rounds(monkeypatch, [round1, round2])

    fired: list[Notification] = []

    async def notify(n: Notification) -> None:
        fired.append(n)

    seen: dict[str, str] = {}
    # Round 1 primes (records ids, no alerts); round 2 alerts on the new id only.
    await notifier.poll_once(seen, notify, prime=True)
    await notifier.poll_once(seen, notify, prime=False)

    assert len(fired) == 1
    assert fired[0].id == "2"
    assert fired[0].title == "Fresh mention"
    assert set(seen) == {"1", "2"}


async def test_re_mention_in_same_thread_renotifies(monkeypatch: pytest.MonkeyPatch) -> None:
    """A re-mention reuses the thread id but bumps updated_at -> must re-notify."""
    first = _note("1", "mention", title="Mention")
    first.updated_at = "2026-06-30T10:00:00Z"
    bumped = _note("1", "mention", title="Mention again")
    bumped.updated_at = "2026-06-30T11:30:00Z"  # same id, newer activity
    _stub_rounds(monkeypatch, [[first], [bumped]])

    fired: list[Notification] = []

    async def notify(n: Notification) -> None:
        fired.append(n)

    seen: dict[str, str] = {}
    await notifier.poll_once(seen, notify, prime=True)  # primes id->10:00
    await notifier.poll_once(seen, notify, prime=False)  # updated_at changed -> fires

    assert [n.title for n in fired] == ["Mention again"]
    assert seen == {"1": "2026-06-30T11:30:00Z"}


async def test_assign_reason_also_notifies(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_rounds(monkeypatch, [[], [_note("9", "assign")]])
    fired: list[Notification] = []

    async def notify(n: Notification) -> None:
        fired.append(n)

    seen: dict[str, str] = {}
    await notifier.poll_once(seen, notify, prime=True)
    await notifier.poll_once(seen, notify, prime=False)
    assert [n.id for n in fired] == ["9"]


async def test_read_mention_does_not_notify(monkeypatch: pytest.MonkeyPatch) -> None:
    """A thread that is already read (unread=False) must not OS-notify.

    The poller queries unread-only (all=false), but this unread gate is kept as a
    defensive guard so a read thread can never raise an alert.
    """
    read_mention = _note("7", "mention")
    read_mention.unread = False
    _stub_rounds(monkeypatch, [[], [read_mention]])
    fired: list[Notification] = []

    async def notify(n: Notification) -> None:
        fired.append(n)

    seen: dict[str, str] = {}
    await notifier.poll_once(seen, notify, prime=True)
    await notifier.poll_once(seen, notify, prime=False)
    assert fired == []
    assert set(seen) == {"7"}


async def test_repeat_mention_arriving_as_comment_notifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeat mention on a subscribed thread arrives as reason ``comment``.

    After the first mention GitHub auto-subscribes you, so subsequent mentions
    on the same issue commonly report ``reason='comment'``. That MUST still alert
    — otherwise only the very first mention would ever notify.
    """
    _stub_rounds(monkeypatch, [[], [_note("4", "comment"), _note("8", "review_requested")]])
    fired: list[Notification] = []

    async def notify(n: Notification) -> None:
        fired.append(n)

    seen: dict[str, str] = {}
    await notifier.poll_once(seen, notify, prime=True)
    await notifier.poll_once(seen, notify, prime=False)
    assert [n.id for n in fired] == ["4", "8"]


async def test_quiet_reasons_do_not_notify(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pure repo-watching / automation reasons never raise an OS interrupt."""
    _stub_rounds(
        monkeypatch,
        [
            [],
            [
                _note("3", "subscribed"),
                _note("4", "ci_activity"),
                _note("5", "push"),
            ],
        ],
    )
    fired: list[Notification] = []

    async def notify(n: Notification) -> None:
        fired.append(n)

    seen: dict[str, str] = {}
    await notifier.poll_once(seen, notify, prime=True)
    await notifier.poll_once(seen, notify, prime=False)
    assert fired == []
    assert set(seen) == {"3", "4", "5"}


async def test_already_seen_id_not_renotified(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_rounds(monkeypatch, [[_note("1", "mention")]])
    fired: list[Notification] = []

    async def notify(n: Notification) -> None:
        fired.append(n)

    # Same id AND same updated_at as the note -> already seen, no re-alert.
    seen = {"1": "2026-06-30T10:00:00Z"}
    await notifier.poll_once(seen, notify, prime=False)
    assert fired == []


async def test_run_poller_graceful_noop_when_notify_send_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """With ``notify-send`` absent, the loop runs but never spawns a notifier."""
    monkeypatch.setattr(notifier, "notify_send_available", lambda: False)

    spawned: list[Notification] = []

    async def fake_spawn(n: Notification) -> None:  # pragma: no cover - must not run
        spawned.append(n)

    monkeypatch.setattr(notifier, "spawn_notify", fake_spawn)
    _stub_rounds(monkeypatch, [[_note("1", "mention")], [_note("2", "mention")]])

    async def fast_sleep(_seconds: float) -> None:
        return None

    await notifier.run_poller(state_dir=tmp_path, sleep=fast_sleep, max_rounds=2)
    assert spawned == []


async def test_run_poller_fires_default_spawn_for_new_mention(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """When notify-send is available, run_poller routes new mentions to spawn_notify."""
    monkeypatch.setattr(notifier, "notify_send_available", lambda: True)

    spawned: list[Notification] = []

    async def fake_spawn(n: Notification) -> None:
        spawned.append(n)

    monkeypatch.setattr(notifier, "spawn_notify", fake_spawn)
    # Round 1 primes (state file absent), round 2 has a new mention.
    _stub_rounds(
        monkeypatch,
        [[_note("1", "mention")], [_note("1", "mention"), _note("2", "mention")]],
    )

    async def fast_sleep(_seconds: float) -> None:
        return None

    await notifier.run_poller(state_dir=tmp_path, sleep=fast_sleep, max_rounds=2)
    assert [n.id for n in spawned] == ["2"]


async def test_run_poller_persists_state_between_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A fresh run loads the prior seen-set, so a known id does not re-alert."""
    monkeypatch.setattr(notifier, "notify_send_available", lambda: True)

    async def fast_sleep(_seconds: float) -> None:
        return None

    # First run primes id "1" and persists it.
    _stub_rounds(monkeypatch, [[_note("1", "mention")]])
    monkeypatch.setattr(notifier, "spawn_notify", lambda n: _async_none())
    await notifier.run_poller(state_dir=tmp_path, sleep=fast_sleep, max_rounds=1)
    assert (tmp_path / notifier._STATE_FILENAME).exists()

    # Second run: "1" is known (loaded from disk, not primed) -> no alert.
    spawned: list[Notification] = []

    async def fake_spawn(n: Notification) -> None:
        spawned.append(n)

    monkeypatch.setattr(notifier, "spawn_notify", fake_spawn)
    _stub_rounds(monkeypatch, [[_note("1", "mention")]])
    await notifier.run_poller(state_dir=tmp_path, sleep=fast_sleep, max_rounds=1)
    assert spawned == []


async def _async_none() -> None:
    return None


def test_poll_delay_honors_advertised_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no override we poll on GitHub's advertised X-Poll-Interval, not faster."""
    monkeypatch.delenv("GITAUTO_POLL_INTERVAL", raising=False)
    monkeypatch.setattr(client, "notifications_poll_interval", lambda: 60)
    assert notifier._poll_delay() == 60.0
    monkeypatch.setattr(client, "notifications_poll_interval", lambda: 90)
    assert notifier._poll_delay() == 90.0


def test_poll_delay_env_override_forces_faster_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """An override forces a faster cadence, clamped to the hard floor."""
    monkeypatch.setattr(client, "notifications_poll_interval", lambda: 60)
    monkeypatch.setenv("GITAUTO_POLL_INTERVAL", "20")
    assert notifier._poll_delay() == 20.0
    monkeypatch.setenv("GITAUTO_POLL_INTERVAL", "1")  # too aggressive -> floor
    assert notifier._poll_delay() == float(notifier._MIN_POLL_INTERVAL)


def test_poll_delay_invalid_env_falls_back_to_advertised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client, "notifications_poll_interval", lambda: 60)
    monkeypatch.setenv("GITAUTO_POLL_INTERVAL", "not-a-number")
    assert notifier._poll_delay() == 60.0


def test_format_titles() -> None:
    title, body = notifier._format(_note("1", "mention", repo="o/r", title="Hello"))
    assert title == "GitHub: mentioned you"
    assert body == "o/r: Hello"
    title, _ = notifier._format(_note("1", "assign"))
    assert title == "GitHub: assigned to you"
