# Notifications Overhaul, Full Review & P1–P3 Improvements

**Date:** 2026-06-30 → 2026-07-02
**Branch:** `improvements/review-p1-p3`
**Status:** Implemented, verified (384 tests passing, ruff clean). Not yet pushed.

This document is a work log of everything done in this cycle: fixing the GitHub
notifications system, a full four-department review of the project, and the
three waves of prioritized improvements that followed.

---

## 0. Starting point & a framework decision

- **"Move the backend from Flask to Django?"** — Clarified that the backend is
  **FastAPI**, not Flask, and that the app is WebSocket/async-heavy (live push,
  PTY terminal, background poller). Django would require Channels/ASGI to regain
  what FastAPI already does natively, plus a rewrite of the test suite. The three
  stated motivations (realtime updates, easy expansion, easy credentials) were
  either already solved or addable without a rewrite. **Decision: stay on FastAPI.**

---

## 1. Notification system — debugging & fixes

Committed as `5e4062b` — *"notifications issue fixed where the notifications
would get stuck and not update unless the user restart the app."*

### Symptoms reported
1. Had to click "clear" for a notification to count as read.
2. "Mark read" only affected the highlighted item.
3. Only ~2 notifications ever showed; sometimes it "got stuck."
4. The UI reloaded the whole page (loading flash) instead of updating seamlessly.
5. New comments inside an issue didn't appear without a manual refresh.
6. Repeat mentions in the same issue didn't re-notify — only the first did.
7. Notifications froze and stopped updating until the app was restarted.

### Root causes found
- **One-thread-per-issue model:** GitHub's notifications API returns a single
  thread per issue (not one per mention), and after the first mention it
  auto-subscribes you — so repeat mentions arrive with `reason: comment`/`author`,
  not `mention`. The alert filter only accepted `mention`/`assign`, silently
  dropping every repeat.
- **The "stuck" bug (the big one):** GitHub returns an **empty ETag (`W/""`)** for
  `/notifications`. The client cached it and sent it back as `If-None-Match`;
  GitHub then answered **`304 Not Modified` to every request**, so the client kept
  serving a **stale cached list forever**. The issues pane never froze because it
  has no ETag caching — which is exactly why issues updated while notifications
  didn't.
- **Latency reality:** GitHub server-caches `/notifications` (~60s), so polling
  faster than the advertised `X-Poll-Interval` returns identical data and only
  risks secondary rate limiting.

### Fixes shipped
- **Mark-read-on-open** (optimistic, reverts on server error); no separate
  "clear" click.
- **"Mark all read"** endpoint (`POST /api/github/notifications/read-all` →
  GitHub `PUT /notifications`) + button.
- **Unread inbox** as the default view (`all=false`): reading removes a thread; a
  re-mention brings it back as unread. (An "Unread/All" toggle was trialed and
  then removed in favor of the simpler unread-only inbox.)
- **Broadened alert reasons** to `mention, team_mention, assign, review_requested,
  comment, author, manual, invitation` (backend `_NOTIFY_REASONS` and frontend
  `ALERT_REASONS`) so 2nd/3rd/4th mentions notify, not just the first.
- **Empty-ETag guard** (`_usable_etag`): a blank validator (`W/""`/`""`) is never
  cached or echoed, so the client always refetches fresh — the fix for the freeze.
- **Honor `X-Poll-Interval` (~60s)** instead of the briefly-tried 15s fast poll.
- **Seamless UI updates:** keyed DOM reconciliation for the notification list (no
  full wipe/flicker); the detail pane became a **live thread** that appends new
  comments in place (textarea preserved, auto-scroll only when already at bottom)
  instead of reloading.

> **Honest note:** an earlier "poll every 15s for near-realtime" change was wrong
> on two counts — it doesn't help (GitHub's server cache) and it makes the
> rate-limit freeze more likely. It was reverted.

---

## 2. Full-project review (4 specialist agents, read-only)

Four parallel specialists audited the project. Summary verdicts:

| Department | Verdict | Rating |
|---|---|---|
| 🔒 Security | Mature threat model (single-user, 127.0.0.1). Shell-free subprocess layer, uniform option/path guards, WS Origin-gating, no XSS. **Zero new vulnerabilities.** | Strong |
| 🐛 Bugs/Correctness | No crashers. Concentrated in shared-state races + unbounded growth. | Solid |
| 🏛️ Architecture | Excellent backend; the "composable tools + integration layer" principle was documented but **not built**. | 7.5/10 |
| ⚡ Efficiency | "A polish pass, not a rescue." Hot paths already careful. | Efficient |

### Highest-signal findings (converged across agents)
1. **Unbounded growth** of both the backend `seen` map and the frontend `seen`
   set (the latter could hit the localStorage quota and silently disable dedup).
2. **Notifications caching:** the empty-ETag workaround meant every poll was a
   full fetch (efficiency), and there was a **latent race** on the shared ETag
   cache between the poller and request handlers (bugs).
3. **Opened-notification flicker:** with GitHub's ~60s eventual consistency, a
   just-read notification can momentarily reappear as unread.

### Prioritized action list produced
- **P1 (quick wins):** bound both `seen` stores; collapse the git auto-refresh
  subprocess fan-out; pause/slow the detail-pane poll.
- **P2 (before Phase 2/3):** `Last-Modified` caching + fix the shared-cache race;
  consolidate `core/gh`+`core/github`, extract the shared WS guard, delete dead
  `core/sync`; security defense-in-depth (explicit path guards, loopback assert).
- **P3 (strategic):** build the integration-layer / `ToolModule` host.

---

## 3. Improvements implemented (6 agents, 3 sequential waves)

Run as three sequential waves (dependencies + shared-file safety), each with two
disjoint-file specialists, verified and committed at every boundary.

### Wave P1 — resource fixes · commit `1d24b0c`
- **notifier.py:** bound the persisted `seen` map with a 500-entry recency cap
  (`_prune_seen`); active threads always retained so re-mention dedup stays correct.
- **git/client.py:** collapse `get_status` from 4 git spawns to one
  `git status --porcelain=v2 --branch` (identical `RepoStatus` output).
- **alerts.js:** cap the localStorage `seen` set at 1000 keys (oldest-evicted) and
  recover from `QuotaExceededError` by trimming + retrying.
- **detail.js:** pause the live-thread poll on hidden tab (`visibilitychange`),
  raise the interval to 45s; no listener leaks.
- Tests: +12. **333 passing.**

### Wave P2 — consolidation, caching, security · commit `4b80ca0`
- **client.py:** per-cache-key `asyncio.Lock` closes the poller/request race (a
  304 can no longer return a stale snapshot); restored cheap conditional polling
  via **`Last-Modified`/`If-Modified-Since`** (GitHub's empty ETag made
  `If-None-Match` useless).
- Consolidated `core/gh` → `core/github/gh_cli.py` (one GitHub package); deleted
  dead `core/sync`; moved the `PullRequest` model into `core/github/models.py`
  (re-exported from `core/models` for back-compat).
- **web/ws.py (new):** shared WebSocket Origin-guard + `safe_close`; all three WS
  routers (terminal, watch, github_events) use it instead of importing
  `terminal.py` privates.
- Defense-in-depth: `stage`/`unstage`/`discard` now route paths through
  `_resolve_worktree_file` (explicit worktree confinement).
- **web/__main__.py:** fails closed on a non-loopback host
  (`GITAUTO_ALLOW_NONLOOPBACK=1` opt-out) so the PTY/WS surface can't be silently
  exposed.
- Tests: +27. **360 passing.** Routes and public signatures unchanged.

### Wave P3 — composable-tools integration layer · commit `1ad7ff7`
- **git_automation/integration/ (new):** `ToolModule` (name, router, optional
  lifespan) + `available_tools() → {"git", "github"}`.
  - **git** tool: `repo, gitops, fs, merge, stash, terminal, watch`.
  - **github** tool: `pr, github, github_events` (owns the notifier lifespan).
  - `identity` is shared and always mounted.
- **create_app(tools=None):** mounts all tools (byte-identical to before) or a
  subset; composes per-tool lifespans via `AsyncExitStack`; unknown tool →
  `ValueError`. Git-only boot doesn't start the notifier.
- **Standalone boot:** `GITAUTO_TOOL` env selection in `web/__main__.py` (pure,
  tested parser) + `make web-git` / `make web-github`.
- Design doc updated: the composable-tools principle now reads as **built**.
- Tests: +24. **384 passing.**

---

## 4. Verification

- `make lint` (ruff): **clean.**
- `uv run pytest -q`: **384 passed.**
- Tool isolation verified via OpenAPI: git-only excludes github routes and vice
  versa; `identity` shared in both; unknown tool → `ValueError`; CLI parser and
  `make web-git`/`web-github` confirmed.
- Notifications empty-ETag behavior verified against the live GitHub API (cache
  stays empty; every poll refetches fresh).

---

## 5. Running the tools

```bash
make web         # all tools (git + github cockpit)
make web-git     # git cockpit only  (GITAUTO_TOOL=git)
make web-github  # github cockpit only (GITAUTO_TOOL=github)
```

- `GITAUTO_TOOL` — comma-separated tool names; unset = all.
- `GITAUTO_HOST` / `GITAUTO_PORT` — bind host/port (defaults `127.0.0.1`).
  Non-loopback hosts are refused unless `GITAUTO_ALLOW_NONLOOPBACK=1`.
- `GITAUTO_POLL_INTERVAL` — override the notification poll cadence (floored; the
  default honors GitHub's `X-Poll-Interval`).

---

## 6. Deferred / still open

- **Just-read flicker guard** — a minimal client-side "recently read" set that
  hides a just-opened thread until GitHub's ~60s eventual consistency catches up,
  while never suppressing a genuine re-mention. Deliberately left out (a prior
  request removed the earlier `locallyRead` map); re-add on request.
- **Minor review nits not bucketed into P1–P3:** the notify-reason allow-list is
  duplicated in Python and JS (drift risk); `_poll_interval` is a shared global
  written by every caller; `notify-send` spawns are serialized per round. All
  low-impact.
- **Not pushed** — the three commits live on `improvements/review-p1-p3` locally.
- **Phase 2/3** (skill tree, agentic workflow) — the integration host is now in
  place for them to register into.

---

## 7. Commit map

| Commit | Wave |
|---|---|
| `5e4062b` | Notification system fixes (§1) |
| `1d24b0c` | P1 — bound seen-state, collapse git status fan-out |
| `4b80ca0` | P2 — GitHub consolidation, caching/race fix, security hardening |
| `1ad7ff7` | P3 — composable-tools integration layer |
