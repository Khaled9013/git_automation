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
