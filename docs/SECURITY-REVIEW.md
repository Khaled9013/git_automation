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
