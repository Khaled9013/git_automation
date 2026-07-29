# Security Review — git-automation Phase 1

Scope: backend (`git_automation/core/*`, `git_automation/web/*`) and the
vanilla-JS frontend. The app is a single-user, localhost-only tool
(`uvicorn ... host="127.0.0.1"`) that drives the local `git` and `gh` CLIs.

Audit date: 2026-06-29. Test baseline before/after: 71 → 82 passing
(`uv run pytest -q`), `uv run ruff check .` clean.

---

## Findings

### F1 — Git option injection via remote / branch values  — HIGH (fixed)

`git_automation/core/git/client.py` — `fetch`, `pull`, `push`,
`create_branch`, `checkout_branch`, `delete_branch`, `merge_branch`.

**What:** These functions placed caller-supplied `remote` / `branch` / `name`
values directly as positional arguments to `git` (e.g. `["fetch", remote]`,
`["merge", name]`). A value beginning with `-` is parsed by git as an *option*,
not a ref. The `remote` of `fetch`/`pull` and `push` is the highest-impact:
`git fetch --upload-pack=<cmd>` (or `git push --receive-pack=<cmd>`) executes an
arbitrary command on the local machine. The values are accepted as free-form
strings by the corresponding `/api/git/*` endpoints, so the vector is reachable
by anything able to POST to the API. (Exposure is reduced — but not eliminated
as defense-in-depth — by the localhost bind and the JSON content-type, which
forces a CORS preflight that a cross-origin browser page cannot satisfy.)

**Fix:** Added `_reject_option(value, label)` in `git/client.py`, which raises
`GitAutomationError("invalid_argument", ..., 400)` for any value starting with
`-`. Applied to every remote/branch/ref argument in the functions above. This
breaks no valid behavior: git refs and remote names can never legitimately
begin with `-` (`git check-ref-format`). `--` is *not* a usable separator for
all affected commands (`git checkout -- main` means a pathspec, not a branch),
so a value guard is the uniform defense. `delete_branch` additionally passes the
name after `--` as belt-and-suspenders. Regression tests in
`tests/test_security_injection.py` (core + API layers).

### F2 — File-path arguments — OK (already hardened)

`stage`, `unstage`, `discard`, `get_diff` already pass file paths after a `--`
separator (`["add", "--", *files]`, `["restore", "--staged", "--", *files]`,
`["diff", ..., "--", file]`, `["diff", "--no-index", "--", "/dev/null", file]`).
A file named `-x` therefore cannot be misread as an option. No change needed.

### F3 — Commit message — OK

`commit` runs `["commit", "-m", message]`; the message is the *argument to*
`-m`, so it cannot be reinterpreted as an option regardless of content. Empty
messages are rejected with `empty_commit_message`. No change needed.

### F4 — No shell, always arg lists — OK

Every subprocess flows through `core.process.run_process`, which calls
`asyncio.create_subprocess_exec(*args)` — an argument list, no `shell=True`,
no string interpolation. `git`/`gh` invocations across the codebase all pass
lists. Confirmed; no change needed.

### F5 — `gh` token confidentiality — OK

`login_with_token` writes the token to the process **stdin** (`run_process(...,
stdin=token)`), never into `args`. On failure it raises with only `gh`'s own
combined output (`result.combined`), not the token. There is no logging
anywhere in the codebase, so the token cannot leak to logs. Existing tests
(`test_login_with_token_passes_token_on_stdin`,
`test_login_with_token_failure_excludes_token`) lock this in. No change needed.

### F6 — Filesystem browser — OK (intended), with guards confirmed

`core/fs.py::list_dirs` lists **directories only** and never returns file
contents (the only path to file contents is `git diff`, which is repo-scoped).
Browsing arbitrary absolute paths on the local machine is the intended design of
the in-app repo picker (single-user local tool) and is documented as accepted
risk. Confirmed it does not crash on errors: missing/`not-a-dir` → 400
`invalid_path`; unreadable dir → 403 `permission_denied`; `_is_git_repo` swallows
`OSError`. **Accepted risk:** `child.is_dir()` follows symlinks, so a symlinked
directory is listed — single-level only (no recursion), so no traversal blow-up.

### F7 — Error shape does not leak internals — OK

`GitAutomationError` renders as `{"error":{"code","message"}}` via the
registered handler. Messages are domain strings (and the user-supplied path);
no stack traces or internal paths are exposed. Unhandled exceptions fall through
to FastAPI's default 500 ("Internal Server Error") with no traceback in the body
(server not run in debug). No change needed.

### F8 — Static file serving — OK

Served via Starlette `StaticFiles(directory=..., html=True)`, which normalizes
and confines paths to the mount directory (no traversal). Mounted last so
`/health` and `/api/*` take precedence. Nothing custom overrides it. No change
needed.

### F9 — Destructive operations — OK

`discard` → `git restore` (no `--force` beyond restoring the index version);
`delete_branch` uses `-d` (safe) and only `-D` when the caller explicitly passes
`force=True`; `merge_branch` is a plain `git merge` (no `--force`/strategy
surprises). The backend adds no hidden force flags. The frontend confirms before
calling each: discard (`changes.js`), merge and delete (`branches.js`) via
`confirmDialog`, with force-delete a second explicit confirm. No change needed.

---

## Accepted risks (out of scope to "fix")

- **Local filesystem enumeration** via `/api/fs/list` is by design for a
  single-user local tool (F6). Directory names only; no contents.
- **`/api/repo/graph?limit=` is an unbounded int** — a very large value is a
  minor local resource concern only; no remote exposure. Not changed.
- **No CSRF token / auth.** The app is localhost-only and stateless w.r.t.
  sessions; JSON-body endpoints require a content-type that triggers a CORS
  preflight, blocking cross-origin browser POSTs. Adequate for the stated
  single-user local threat model. The F1 input guard is the meaningful
  defense-in-depth here.

---

## Hardening applied

- Added `_reject_option()` guard in `git_automation/core/git/client.py` and
  applied it to all remote/branch/ref arguments of `fetch`, `pull`, `push`,
  `create_branch`, `checkout_branch`, `delete_branch`, `merge_branch` — closing
  the option-injection / `--upload-pack` RCE vector (F1).
- `delete_branch` now passes the branch name after `--`.
- Added `tests/test_security_injection.py` (11 regression tests: core +
  API layers) asserting option-like values are rejected with a 400
  `invalid_argument` and never reach the subprocess layer.

---

# Security Review — Phase 1 / Slice 3 (PTY, merge/conflict, new git/PR ops)

Scope: the *new* Slice-3 surface only — `core/terminal.py`,
`web/api/terminal.py` (PTY WebSocket); the merge/conflict + reset/cherry-pick/
checkout/stash/reflog/undo additions in `core/git/client.py`; the new routers
`web/api/{merge,stash,pr,gitops}.py`; and PR support in `core/gh/client.py`.
Slice-1/2 findings (F1–F9 above) are not re-litigated.

Audit date: 2026-06-29. Test baseline: 157 → 168 passing
(`uv run pytest -q`), `uv run ruff check .` clean.

## Findings

### S1 — `resolve_conflict` arbitrary file **write** via path traversal — HIGH (fixed)

`core/git/client.py::resolve_conflict`. The endpoint computed the write target
as `Path(cwd) / file` with a client-supplied `file`, then `git add`. Because
`Path(cwd) / "/etc/x"` collapses to `/etc/x` and `Path(cwd) / "../../x"`
traverses upward, a caller could write arbitrary content to **any** path the
server user can write — confirmed empirically with both a relative `../` path
and an absolute path (the subsequent `git add` fails, but the file is already
overwritten). Targeting `.git/config` or a `.git` hook escalates this to code
execution on the next git operation. Reachable via `POST /api/git/resolve`.

**Fix:** Added `_resolve_worktree_file(cwd, file)` which `.resolve()`s the
target (following symlinks) and requires it to stay strictly inside the
worktree and outside `.git`, raising `invalid_argument` (400) otherwise.
Applied to the write in `resolve_conflict`. Legitimate nested conflict paths
still work. Regression tests in `tests/test_slice3_security.py` cover relative
traversal, absolute paths, symlink escape, `.git` writes, the API layer, and a
positive (normal nested file) case.

### S2 — `get_conflict` arbitrary file **read** via path traversal — HIGH (fixed)

`core/git/client.py::get_conflict`. The working-copy read used
`Path(cwd) / file` and returned the contents in the `merged` field — confirmed
leaking an out-of-repo file (`../SECRET.txt`) via `GET /api/repo/conflict`. The
three merge *stages* are fetched with `git show :N:file`, which git already
confines to the repo object store, so only the working-tree read was exposed.

**Fix:** `get_conflict` now resolves the working path through the same
`_resolve_worktree_file` guard (400 on escape). Regression tests at the client
and API layers.

### S3 — PTY WebSocket reachable cross-origin (CSWSH) — HIGH (fixed)

`web/api/terminal.py`. The accepted-risk note for the JSON API ("a cross-origin
page can't POST JSON without a CORS preflight") does **not** extend to
WebSockets: a browser may open a cross-site WebSocket to `127.0.0.1` with no
preflight and no same-origin restriction. Any website the user visits could
therefore connect to `WS /api/terminal` and obtain an interactive shell with
the server user's privileges — Cross-Site WebSocket Hijacking → RCE. The
localhost bind does not help, because the victim's own browser originates the
connection.

**Fix:** Added an `Origin` check (`_origin_allowed`) that rejects the handshake
(close code 1008, before `accept()` — no shell spawned) unless the `Origin`
host is loopback (`localhost`/`127.0.0.1`/`::1`). A missing `Origin` (CLI /
non-browser / tests) is allowed; browsers always send a forge-proof `Origin`.
Look-alike hosts (e.g. `127.0.0.1.evil.com`) are rejected. Regression tests in
`tests/test_slice3_security.py` cover the allow/deny matrix and a live
foreign-origin handshake rejection.

## Verified OK (no change needed)

### S4 — `get_diff` file param (read side) — OK (git-enforced)

`get_diff` passes `file` to `git diff -- <file>` and, only when that diff is
*empty*, to `git diff --no-index -- /dev/null <file>`. For an out-of-repo path
(relative `../` or absolute), `git diff -- <file>` fails with a non-empty
`"... is outside repository"` message, so the `--no-index` content-disclosure
branch is never reached and no file contents leak (confirmed empirically). The
boundary is enforced by git itself; left unchanged.

### S5 — Option injection on the new ops — OK (already guarded)

`reset` sha, `cherry-pick` sha, `checkout` ref, `merge` name, and
`get_commit_detail` sha all pass through `_reject_option`; `reset` additionally
allow-lists `mode ∈ {soft,mixed,hard}`. For PRs, `pr_create` guards `base`/
`head` with `_reject_option`, while `title`/`body` are safe as **values of**
`--title`/`--body` (a leading `-` cannot be re-read as a flag). `stash`
selectors are built from server-side `int` indices (`stash@{N}`). Covered by
existing tests in `tests/test_slice3_*.py` and `tests/test_pr.py`.

### S6 — reflog / undo / redo cannot silently destroy work — OK

`undo`/`redo` use `git reset --keep HEAD@{1}`; `--keep` makes git itself refuse
(non-zero exit, no data loss) when the step would discard uncommitted changes.
Locked in by `test_undo_refuses_to_discard_uncommitted_work` in
`tests/test_slice3_gitops.py`.

### S7 — Error / secret leakage on the new endpoints — OK

`pr_create`/`pr_list` failures surface only `gh`'s own combined output (the
token is never in argv — it goes via stdin on login). Domain errors render as
`{"error":{code,message}}`; the terminal sends only `{type:error, code,
message}` control frames. No stack traces, file contents, or tokens are
exposed. Consistent with F5/F7.

## Accepted risks (Slice 3)

- **The PTY is arbitrary code execution by design.** With the S3 origin check
  in place, it is reachable only by a same-origin (loopback) browser tab or a
  local non-browser client — the intended single-user, localhost-only model.
  Documented in `core/terminal.py` / `web/api/terminal.py`; do not bind the app
  to a non-loopback interface or front it with a proxy that forwards remote
  clients or rewrites `Origin`.
- **`validate_repo_path` accepts any existing directory** (not only git repos)
  as the terminal `cwd`; this only selects the shell's starting directory, which
  a shell user can change anyway. No additional exposure.

## Hardening applied (Slice 3)

- Added `_resolve_worktree_file()` in `core/git/client.py`; applied to the
  read in `get_conflict` and the write in `resolve_conflict` (S1, S2).
- Added `_origin_allowed()` + handshake rejection in `web/api/terminal.py`
  (S3).
- Added `tests/test_slice3_security.py` (11 regression tests) covering conflict
  path-traversal (write/read, relative/absolute/symlink/`.git`, core + API) and
  the terminal origin/CSWSH defense.

---

# Security Review — GitHub cockpit slice (notifications + issues)

Scope: the *new* GitHub surface only — `core/github/{client,models}.py`,
`web/api/github.py`, and the frontend `static/js/github/*` + `js/shell.js`.
Earlier findings (F1–F9, S1–S7) are not re-litigated. Same threat model:
single-user, localhost-only, driving the authenticated `gh` CLI.

Audit date: 2026-06-30. Test baseline: 264 → 285 passing (`uv run pytest -q`),
`uv run ruff check .` clean.

## Findings

### G1 — `gh api` path injection via `thread_id` — MEDIUM (fixed)

`core/github/client.py::mark_notification_read`. The caller-supplied `thread_id`
was interpolated into the `gh api` path `"/notifications/threads/{thread_id}"`
after only rejecting a leading `-`. A value containing `/`, `?`, or `..`
(e.g. `123/../../user`, `1?per_page=1`) could therefore re-target the request at
a *different* GitHub API endpoint — an SSRF-style path-traversal within the
authenticated GitHub API. `mark_notification_read` is the exposable seam future
agents call directly, so HTTP route-matching (which already rejects `/`) is not
the only entry point.

**Fix:** `_validate_thread_id` now requires `^[0-9]+$` (GitHub thread ids are
integers). This simultaneously closes the option-injection (`-`) and
path-injection (`/`, `?`, `..`) vectors. Regression tests parametrize the
rejected forms (`tests/test_github.py::test_mark_notification_read_rejects_non_numeric_id`).

### G2 — `state` not allow-listed — LOW (fixed)

`core/github/client.py::list_issues`. `state` flowed straight into
`["--state", state]` for both `gh issue list` and `gh search issues`. Because it
is the *value of* `--state`, a leading `-` could not be re-read as a separate
flag (so no injection), but the value was otherwise unvalidated — inconsistent
with the `filter` allow-list and the project's defense-in-depth posture.

**Fix:** Added `_validate_state` allow-listing `state ∈ {open, closed}` (the only
two states the UI offers); applied at the top of `list_issues` before any argv
is built. Regression test parametrizes rejected values
(`test_list_issues_rejects_invalid_state`).

## Verified OK (no change needed)

### G3 — No shell, always arg lists — OK

Every `gh` invocation in `core/github/client.py` passes a Python list to
`run_process` (`asyncio.create_subprocess_exec`, no `shell=True`). Tests assert
the exact argv built for every function. Consistent with F4.

### G4 — Comment body confidentiality / integrity — OK

`add_comment` passes the body via `gh issue comment ... --body-file -` with the
text on **stdin** (`run_process(..., stdin=body)`); it never appears in argv or
any log. Markdown, newlines, and shell metacharacters are preserved byte-for-byte
(no shell to interpret them). Locked in by
`test_add_comment_preserves_special_chars_via_stdin`. Consistent with F5/S7.

### G5 — Option injection via `repo` / `number` / `filter` — OK (guarded)

`repo` is validated against a strict `owner/name` regex whose first character
class excludes `-` (so it can never be read as a `gh` option); spaces and
shell metacharacters are rejected. `number` must be a positive `int` (rejecting
`bool`). `filter` is allow-listed to `{assigned, mentioned, created, all}`, with
`all` additionally requiring a `repo` (mirrors the `gh search` constraint).
Existing + new tests assert each rejects bad input *before* any subprocess runs.

### G6 — Error / rate-limit / auth-failure leakage — OK

`gh` failures (401 not-authenticated, 403 rate-limit, private-repo 404, bad
JSON) surface as `GitAutomationError` rendered as `{"error":{code,message}}`
carrying only `gh`'s own combined output — no tracebacks, file contents, or
tokens. The `gh` token lives in `gh`'s own keyring/config and is never read,
passed, or logged by this module. Covered by `test_list_*_gh_failure`,
`test_list_issues_rate_limited_is_clean_error`, and
`test_mark_notification_read_already_read_or_invalid_thread`.

## Documented limitation (not a vulnerability)

- **`/notifications` returns only the first page.** `list_notifications` calls
  `gh api /notifications` without `--paginate`. This is deliberate: the inbox
  shows the most recent threads and the 60s alert poller diffs ids across polls,
  so new items always surface. `--paginate` would fan out an unbounded number of
  requests on every poll and burn the user's GitHub rate limit. Documented in
  the function docstring; raise `?per_page=50` before reaching for `--paginate`
  if a future slice needs the backlog.

## Hardening applied (GitHub slice)

- Tightened `_validate_thread_id` to digits-only and added `_validate_state`
  (allow-list `{open, closed}`) in `core/github/client.py` (G1, G2).
- Documented the first-page `/notifications` limitation in the
  `list_notifications` docstring.
- Added 21 regression/edge tests to `tests/test_github.py` (thread-id injection,
  state allow-list, rate-limit/404 clean errors, empty notification/issue lists,
  closed/empty-body issue detail, and stdin-preservation of special-char bodies).
- Frontend UX: the Issues "All" filter (which the backend requires a repo for)
  is now gated behind a new repo input in the filter row — disabled until a repo
  is set — removing the previous dead/error tab (`static/js/github/view.js`).

---

# Security Review — hardening addendum (host guard, WS origin port)

Scope: findings from the 2026-07 design/robustness re-review, on top of F1–F9,
S1–S7, G1–G2. Same threat model: single-user, localhost-only. Audit date:
2026-07-02. Verified with `uv run pytest -q` (all passing) and `ruff` clean.

## H1 — DNS rebinding bypasses the CORS-preflight defense — MEDIUM (fixed)

The earlier "accepted risk" note argued that JSON `POST`s are safe from a
cross-origin page because the `application/json` content type forces a CORS
preflight the page cannot satisfy. That argument does **not** cover **DNS
rebinding**: an attacker's page at `http://evil.com:8000` whose DNS record is
re-pointed at `127.0.0.1` becomes *same-origin* with this server in the victim's
browser — no CORS, no preflight, and its `Origin` matches itself. The entire
JSON `/api` surface was reachable this way: run git ops on local repos,
enumerate directories via `/api/fs/list`, post GitHub comments, and — worst —
replace the `gh` session via `POST /api/gh/login/token` with an attacker token,
silently redirecting future pushes/PRs. This is the one vector reachable by a
*remote* attacker (via the victim's browser), not just a local process.

The signal that survives rebinding is the `Host` header: the browser still sends
the attacker's hostname (`evil.com:8000`). **Fix:** `HostGuardMiddleware`
(`web/security.py`), added in `create_app`, rejects any request whose `Host` is
not loopback — HTTP `400 invalid_host`, WebSocket close `1008`, both *before* the
route runs. A missing `Host` is allowed (non-browser clients; browsers always
send one), mirroring the WS `Origin` trust call. The
`GITAUTO_ALLOW_NONLOOPBACK` opt-out (for an authenticating reverse proxy, where
`Host` is legitimately arbitrary) disables the guard with a loud warning.
Regression tests in `tests/test_host_guard.py` cover the parse/allow/deny matrix
(incl. bracketed IPv6 and look-alike hosts), the HTTP + WS paths, and the
opt-out.

## H2 — WebSocket Origin accepted any loopback *port* — LOW (fixed)

`origin_allowed` accepted any loopback-host `Origin` regardless of port, so a
page served by *another* local dev server (e.g. `http://localhost:3000`) was
treated as same-origin and could open the privileged PTY / watch / events
sockets. **Fix:** `origin_allowed(origin, *, expected_port=...)` now also pins
the `Origin` port to the server's own port; the three WS routers pass
`websocket.url.port`. Backward-compatible (no `expected_port` ⇒ prior
host-only behavior); a portless `Origin` is treated as a different origin and
rejected. Tests in `tests/test_slice3_security.py`.

## Unified loopback trust + boot policy (defense-in-depth)

The fail-closed bind policy (`web/__main__.py`) and the in-app Host guard now
share one loopback definition (`web/security.py`), and **every** boot path
(`make web`, `web-git`, `web-github`, the console script) goes through the same
guarded entry point — so a non-loopback `GITAUTO_HOST` is refused uniformly, not
just on the standalone-tool targets. `make web`'s previous direct-`uvicorn`
invocation (which bypassed the guard and relied on uvicorn's default bind) is
gone.

## Forward-looking: Markdown rendering will be the next XSS surface

The design doc's next step renders issue/PR bodies and READMEs as HTML. Issue
bodies are **remote attacker-controlled** (anyone can comment on a public
issue). Today's `textContent`/escaped-`innerHTML` discipline protects the app;
the moment Markdown is rendered to HTML, a **vendored HTML sanitizer** must sit
between the renderer and the DOM (no-CDN, consistent with the offline rule).
This is now a written requirement in the design doc's next-step section.

## Addendum (2026-07-13): tailnet (Tailscale) trust extension

The loopback-only trust model gained one deliberate extension: the machine's
own Tailscale tailnet. Rationale: a tailnet is a private WireGuard mesh whose
peers are devices the operator explicitly enrolled, so "reachable over
Tailscale" means "the operator's own authenticated devices" — unlike a LAN,
where any nearby machine can connect.

What changed (`web/tailscale.py` is the single source of truth):

- **Bind policy** (`web/__main__.py`): with `GITAUTO_HOST` unset, the app
  binds loopback **plus** the machine's Tailscale IPs when `tailscale status`
  reports a running backend (multi-bind = one pre-bound socket per host handed
  to a single uvicorn server, reload mode included). Explicit Tailscale-range
  IPs are allowed without the `GITAUTO_ALLOW_NONLOOPBACK` opt-out; everything
  else still fails closed.
- **Host guard / WS Origin guard** (`web/security.py::is_trusted_host`,
  `web/ws.py`): besides loopback, they accept literal IPs inside Tailscale's
  ranges (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) and this machine's *own*
  MagicDNS names as reported by the CLI. Other machines' `.ts.net` names and
  arbitrary hostnames stay rejected, so the DNS-rebinding and CSWSH defenses
  hold: an attacker's page can never present a literal Tailscale IP or a name
  that only this machine's tailnet resolver hands out.
- **Fail-safe detection**: any detection failure (no CLI, daemon stopped,
  unexpected JSON) degrades to `None` and the app behaves exactly as before
  (loopback-only). Parsed IPs are re-validated against the Tailscale ranges so
  CLI output can never smuggle another address into the trusted set.

Residual risk accepted: every device on the operator's tailnet can reach the
unauthenticated terminal/watch WebSocket surface. That is the feature (the
tailnet is the operator's own devices); operators who share their tailnet
should run with `GITAUTO_HOST=127.0.0.1`. Covered by
`tests/test_tailscale_access.py`.
