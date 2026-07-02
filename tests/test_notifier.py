"""Tests for the background OS-notification poller (``core/github/notifier``).

These exercise the diff/notify logic and the loop without spawning a real
``notify-send`` subprocess or touching the network: ``client.list_notifications``
is stubbed and the notify sink is injected. Two poll rounds (round 2 introduces a
new mention) prove exactly one notification fires; non-mention/assign reasons are
ignored; and a missing ``notify-send`` degrades to a graceful no-op.
"""

from __future__ import annotations

import json

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


async def test_published_event_includes_alert_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    """The broadcast ``notifications`` event carries the authoritative allow-list.

    The frontend uses this to stay in sync with the backend ``_NOTIFY_REASONS``
    instead of hardcoding its own copy (which could silently drift).
    """
    _stub_rounds(monkeypatch, [[_note("1", "mention")]])

    events: list[dict] = []

    async def fake_publish(event: dict) -> None:
        events.append(event)

    async def notify(_n: Notification) -> None:
        return None

    seen: dict[str, str] = {}
    await notifier.poll_once(seen, notify, prime=False, publish=fake_publish)

    assert len(events) == 1
    event = events[0]
    assert event["type"] == "notifications"
    assert event["alert_reasons"] == sorted(notifier._NOTIFY_REASONS)


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


# --- seen-map bounding ----------------------------------------------------


async def test_seen_map_is_bounded_across_many_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Over far more distinct threads than the cap, ``seen`` never exceeds it.

    Each round the response contains one brand-new mention id. Without bounding
    the map would grow by one every round forever; with it the map is capped.
    """
    total = notifier._SEEN_CAP * 3
    rounds = [[_note(str(i), "mention")] for i in range(total)]
    _stub_rounds(monkeypatch, rounds)

    async def notify(_n: Notification) -> None:
        return None

    seen: dict[str, str] = {}
    for i in range(total):
        await notifier.poll_once(seen, notify, prime=(i == 0))
        assert len(seen) <= notifier._SEEN_CAP

    # The most-recent id is always retained; a long-gone one is evicted.
    assert str(total - 1) in seen
    assert "0" not in seen


async def test_prune_keeps_all_currently_active_threads() -> None:
    """A response larger than one round's churn keeps every present id."""
    seen = {str(i): "t" for i in range(notifier._SEEN_CAP + 50)}
    present = [str(i) for i in range(10)]  # currently-active subset
    notifier._prune_seen(seen, present)
    assert len(seen) == notifier._SEEN_CAP
    # Every active id survives; some stale absent ids were dropped from the tail.
    for id_ in present:
        assert id_ in seen


async def test_active_thread_re_mention_dedup_survives_pruning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active thread is not re-alerted round-over-round despite pruning.

    Thread ``hot`` stays present every round (same updated_at) while many other
    distinct threads churn through, exceeding the cap. Because ``hot`` is always
    in the current response it is always retained, so its unchanged updated_at is
    recognized and it never spuriously re-alerts. When it finally bumps its
    updated_at, it correctly re-alerts exactly once.
    """
    hot_old = "2026-06-30T10:00:00Z"
    hot_new = "2026-06-30T12:00:00Z"
    churn = notifier._SEEN_CAP * 2

    def hot(updated: str) -> Notification:
        n = _note("hot", "mention", title="Hot thread")
        n.updated_at = updated
        return n

    # Rounds 0..churn-1: hot unchanged + one fresh distinct id each round.
    rounds: list[list[Notification]] = [
        [hot(hot_old), _note(f"x{i}", "mention")] for i in range(churn)
    ]
    # Final round: hot bumps updated_at (genuine new activity).
    rounds.append([hot(hot_new)])
    _stub_rounds(monkeypatch, rounds)

    fired: list[Notification] = []

    async def notify(n: Notification) -> None:
        fired.append(n)

    seen: dict[str, str] = {}
    for i in range(len(rounds)):
        await notifier.poll_once(seen, notify, prime=(i == 0))
        assert len(seen) <= notifier._SEEN_CAP

    hot_alerts = [n for n in fired if n.id == "hot"]
    # Exactly one hot alert: the final bumped-updated_at activity. The unchanged
    # rounds in between never re-alerted (dedup held despite heavy pruning).
    assert [n.updated_at for n in hot_alerts] == [hot_new]


async def test_seen_map_bounded_and_persisted_via_run_poller(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """End-to-end: the persisted state file stays capped and format-compatible."""
    monkeypatch.setattr(notifier, "notify_send_available", lambda: False)

    rounds = notifier._SEEN_CAP + 20
    _stub_rounds(monkeypatch, [[_note(str(i), "mention")] for i in range(rounds)])

    async def fast_sleep(_seconds: float) -> None:
        return None

    await notifier.run_poller(state_dir=tmp_path, sleep=fast_sleep, max_rounds=rounds)

    reloaded = notifier._load_seen(tmp_path / notifier._STATE_FILENAME)
    assert 0 < len(reloaded) <= notifier._SEEN_CAP
    # Format is still {"seen": {id: updated_at}} -> _load_seen returns str->str.
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in reloaded.items())


# --- per-user (XDG) default location + legacy migration -------------------


def test_default_state_dir_honors_xdg_state_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The default base is ``$XDG_STATE_HOME/git-automation`` when set."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert notifier._default_state_dir() == tmp_path / "xdg" / "git-automation"


def test_default_state_dir_falls_back_to_home_local_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Unset (or empty) XDG_STATE_HOME -> ``~/.local/state/git-automation``."""
    expected = tmp_path / "home" / ".local" / "state" / "git-automation"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert notifier._default_state_dir() == expected

    # An empty string counts as unset.
    monkeypatch.setenv("XDG_STATE_HOME", "")
    assert notifier._default_state_dir() == expected


def test_state_path_default_resolves_under_xdg(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``_state_path(None)`` lands the state file under the XDG default."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    expected = tmp_path / "git-automation" / notifier._STATE_FILENAME
    assert notifier._state_path() == expected
    assert notifier._state_path(None) == expected


def test_state_path_explicit_dir_still_wins(tmp_path) -> None:
    """An explicit ``state_dir`` overrides the default location, unchanged."""
    assert notifier._state_path(tmp_path) == tmp_path / notifier._STATE_FILENAME


async def test_run_poller_migrates_legacy_repo_local_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """First run at the new default adopts the old ``./.gitauto`` seen-map.

    Continuity: a re-seen legacy id is NOT re-alerted, and because the round is
    treated as *not* a fresh install (prime=False), a brand-new mention DOES
    alert -- proving the migrated state was loaded rather than everything primed.
    """
    monkeypatch.chdir(tmp_path)
    legacy_dir = tmp_path / ".gitauto"
    legacy_dir.mkdir()
    legacy_file = legacy_dir / notifier._STATE_FILENAME
    legacy_file.write_text(json.dumps({"seen": {"1": "2026-06-30T10:00:00Z"}}))

    # Point the XDG default at a fresh, empty location (no new-format state yet).
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))

    # "1" reappears unchanged (migrated -> no re-alert); "2" is brand new (alerts).
    _stub_rounds(monkeypatch, [[_note("1", "mention"), _note("2", "mention")]])

    async def fast_sleep(_seconds: float) -> None:
        return None

    fired: list[Notification] = []

    async def notify(n: Notification) -> None:
        fired.append(n)

    await notifier.run_poller(
        notify=notify, state_dir=None, sleep=fast_sleep, max_rounds=1
    )

    assert [n.id for n in fired] == ["2"]
    # New state now lives under the XDG default; legacy file left untouched.
    assert (xdg / "git-automation" / notifier._STATE_FILENAME).exists()
    assert legacy_file.exists()


async def test_run_poller_default_location_primes_when_truly_fresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A genuine first-ever run (no new file, no legacy) primes -> no alerts."""
    monkeypatch.chdir(tmp_path)  # no ./.gitauto present
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    _stub_rounds(monkeypatch, [[_note("1", "mention")]])

    async def fast_sleep(_seconds: float) -> None:
        return None

    fired: list[Notification] = []

    async def notify(n: Notification) -> None:
        fired.append(n)

    await notifier.run_poller(
        notify=notify, state_dir=None, sleep=fast_sleep, max_rounds=1
    )
    assert fired == []  # priming round: existing mention not replayed
    assert (tmp_path / "xdg" / "git-automation" / notifier._STATE_FILENAME).exists()


async def test_run_poller_explicit_state_dir_ignores_legacy(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """An explicit ``state_dir`` never triggers legacy migration.

    A legacy file exists, but the explicit (empty) dir means the run primes as a
    fresh install: nothing alerts -- not even the new id "2" -- which proves the
    legacy seen-map was NOT adopted.
    """
    monkeypatch.chdir(tmp_path)
    legacy_dir = tmp_path / ".gitauto"
    legacy_dir.mkdir()
    (legacy_dir / notifier._STATE_FILENAME).write_text(
        json.dumps({"seen": {"1": "2026-06-30T10:00:00Z"}})
    )
    explicit = tmp_path / "explicit"
    _stub_rounds(monkeypatch, [[_note("1", "mention"), _note("2", "mention")]])

    async def fast_sleep(_seconds: float) -> None:
        return None

    fired: list[Notification] = []

    async def notify(n: Notification) -> None:
        fired.append(n)

    await notifier.run_poller(
        notify=notify, state_dir=explicit, sleep=fast_sleep, max_rounds=1
    )
    assert fired == []
