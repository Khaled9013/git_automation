# Host-Guard Hardening & Review Follow-ups

**Date:** 2026-07-02
**Branch:** `improvements/host-guard-hardening` (from `improvements/review-p1-p3`)
**Status:** Implemented & verified — **457 tests passing, ruff clean.**

A second review pass (design / security / modularity / robustness) surfaced one
finding a *remote* attacker could reach and several smaller hardening / quality
items. This is the work log for fixing them. Nine tasks were run as two parallel
waves of file-disjoint specialist agents (plus coordinator-owned wiring/docs),
verified and committed at each boundary — no functionality regressed.

---

## 1. Findings addressed

| # | Finding | Severity | Fix |
|---|---|---|---|
| H1 | **DNS rebinding** bypasses the CORS-preflight defense on the whole JSON API (incl. `POST /api/gh/login/token` → session takeover) | **Medium** (remote-reachable) | Loopback `Host`-header guard middleware |
| H2 | WS `Origin` check accepted any loopback **port** (another local dev server could open the PTY/watch/events sockets) | Low | Pin `Origin` port to the server's port |
| B1 | `make web` bypassed the fail-closed bind guard (direct `uvicorn`, relied on its default) | Low (consistency) | Unify all boots through the guarded entry |
| R1 | PTY `write` busy-spun (`sleep(0)`) on a full input buffer → CPU peg | Low | Event-driven writability wait (`add_writer`) |
| R2 | Notifier seen-state written CWD-relative (`./.gitauto`) → dedup discontinuity | Low | XDG state dir + one-time legacy migration |
| E1 | `get_refs` fanned out 4 sequential git spawns; `delete_files` 1 spawn/file | Efficiency | `asyncio.gather`; single `git ls-files -z` |
| D1 | `reject_option` duplicated (git & gh); loopback-host set duplicated | DRY | One `core/validation.reject_option`; shared set |
| D2 | Notify-reason allow-list duplicated in Python & JS (drift) | DRY | Backend broadcasts it; frontend prefers it |
| X1 | Planned Markdown rendering is the next XSS surface (issue bodies are attacker-controlled) | Forward-looking | Vendored-sanitizer requirement written into the spec |

---

## 2. What shipped (commit by commit)

- **`Security: loopback Host-guard middleware`** — `web/security.py` +
  `HostGuardMiddleware` in `create_app`. Rejects any non-loopback `Host`
  (HTTP `400 invalid_host`; WS `1008`) *before* the route runs; a missing `Host`
  is allowed (non-browser clients). The `GITAUTO_ALLOW_NONLOOPBACK` proxy opt-out
  disables it with a loud warning. `web/security.py` is now the single source of
  truth for "what is loopback". +37 tests (`tests/test_host_guard.py`); existing
  `TestClient`s present a loopback `Host`.
- **`Unify boot paths through the guarded entry point`** — `make web` now runs
  `python -m git_automation.web` (like `web-git`/`web-github`), so the
  fail-closed policy and `GITAUTO_HOST/PORT/TOOL` apply on every boot. Env
  parsing centralized in `web/config.py`; the module-level `app` honors
  `GITAUTO_TOOL` (so raw `uvicorn` and reload mode agree); `main()` pre-validates
  tool names into a clean exit. Verified live (reload boot, `/health`, Host
  rejection). +9 tests.
- **`Security: pin WebSocket Origin to the server port`** —
  `origin_allowed(origin, *, expected_port=…)`; the three WS routers pass
  `websocket.url.port`. Backward-compatible (no port ⇒ old behavior).
- **`Fix PTY write busy-spin`** — `PtySession.write` waits on `add_writer`
  instead of `sleep(0)`; contract preserved (writes all bytes, drops on dead fd,
  never raises); coarse timeout is a dead-fd safety net, not a poll. Tests: 1 MiB
  round-trip through a full buffer; `terminate()` unblocks a parked write.
- **`Notifier: persist seen-state in the XDG state dir`** —
  `$XDG_STATE_HOME/git-automation` (fallback `~/.local/state/git-automation`);
  one-time migration adopts a legacy `./.gitauto` map (priming stays correct);
  explicit `state_dir` still wins for tests.
- **`Efficiency: parallelize get_refs, batch delete_files`** — `get_refs`
  gathers its 4 read-only queries; `delete_files` classifies tracked/untracked
  with one `git ls-files -z`. Identical outputs; guards/`rm`/unlink unchanged.
- **`Refactor: single reject_option + shared loopback-host set`** — the
  option-injection guard lives once in `core/validation.py` (19 call sites
  migrated, error byte-identical); `web/ws.py` imports the loopback set from
  `web/security.py`.
- **`Notifier: broadcast alert-reason allow-list to the frontend`** — each
  `notifications` event carries `alert_reasons=sorted(_NOTIFY_REASONS)`;
  `alerts.js` prefers the server list (fallback to its local copy), killing the
  Python/JS drift. `'assigned'` alias preserved.

---

## 3. Deliberately NOT changed

- **`api_router` route-list duplication** (`web/api/__init__.py` vs.
  `integration/tools.py`) is left as-is: the file documents that it avoids a
  module-load circular import with `integration.tools`. Low value, real risk —
  skipped.
- **`GITAUTO_POLL_INTERVAL` floor (10s)** kept; it exists as a debug/override
  knob. Polling faster than GitHub's advertised `X-Poll-Interval` returns
  identical server-cached data and risks secondary rate limiting, so it is not
  recommended outside debugging (documented in the notifier).

---

## 4. Verification

- `uv run ruff check .` → clean.
- `uv run pytest -q` → **457 passed** (was 384 at the start of this cycle; +73,
  no test weakened or removed).
- Live boot: `make web`-equivalent reload boot serves `/health`, and the Host
  guard returns `invalid_host` for a foreign `Host` header.
- Security subset (`test_host_guard`, `test_security_injection`,
  `test_slice3_security`, `test_validation`) green.

---

## 5. Process note

Nine tasks, two parallel waves. Each wave's agents were given strictly
disjoint file sets (never editing the same file concurrently), self-contained
context, TDD instructions, and a no-commit constraint; the coordinator ran the
full suite + lint and committed per concern at each boundary. Wave A: PTY,
notifier-XDG, git-efficiency, WS-origin-port. Wave B (after A, since it edits
files A touched): guard consolidation, alert-reason broadcast.
