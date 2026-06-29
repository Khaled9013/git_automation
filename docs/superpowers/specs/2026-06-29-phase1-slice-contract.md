# Phase 1 — First Slice: API & Module Contract

Shared interface for the focused slice: **first-run gh/git onboarding** +
**remote-aware push/pull/fetch**. Every agent builds to this contract so the
pieces compose without collisions.

## Scope of this slice

1. On startup, detect whether a git identity (`user.name`/`user.email`) and an
   authenticated `gh` session exist. If not, the UI shows a **first-run
   onboarding** flow to set them. (Ships with no hard-coded identity.)
2. Open/select a local repository.
3. **Push / Pull / Fetch** buttons that discover the repo's remotes and let the
   user pick which remote to act on (default = current branch's upstream, else
   `origin`).

Out of scope for this slice (later): issues, PRs, notifications, staging/diff,
branches/merge, history graph.

## HTTP API (FastAPI, all JSON)

Base path `/api`. All endpoints async. Errors return
`{"error": {"code": str, "message": str}}` with an appropriate 4xx/5xx status.

### Identity / onboarding

- `GET /api/identity` →
  ```json
  { "git_name": "string|null", "git_email": "string|null",
    "gh_authenticated": true, "gh_user": "string|null",
    "needs_onboarding": false }
  ```
  `needs_onboarding` is true when git name/email is unset **or**
  `gh_authenticated` is false.
- `POST /api/identity/git`  body `{ "name": "...", "email": "..." }` → sets
  **global** git config, returns the updated `GET /api/identity` payload.
- `GET /api/gh/login/instructions` → `{ "steps": ["..."], "supports_token": true }`
  Human-readable guidance for authenticating `gh` on a fresh machine.
- `POST /api/gh/login/token` body `{ "token": "..." }` → authenticates via
  `gh auth login --with-token` (token piped on stdin, never logged). Returns the
  updated identity payload.

### Repository

- `GET /api/repo?path=<abs path>` →
  ```json
  { "path": "string", "current_branch": "string|null",
    "upstream": { "remote": "string", "branch": "string" },
    "remotes": [ { "name": "origin", "url": "..." } ],
    "ahead": 0, "behind": 0, "dirty": false }
  ```
  `upstream` is null if the branch has none.
- `GET /api/repo/remotes?path=<abs path>` → `[ { "name": "...", "url": "..." } ]`

### Git remote operations

Each takes `{ "path": "...", "remote": "...", "branch": "string|null" }` and
returns `{ "ok": true, "output": "combined stdout/stderr" }` (or the error shape).

- `POST /api/git/fetch`
- `POST /api/git/pull`
- `POST /api/git/push`  (also accepts `"set_upstream": bool` for first push)

## Module interfaces (Python)

Subprocess calls MUST be non-blocking (`asyncio.create_subprocess_exec`, arg
lists, **never** `shell=True`) and MUST validate the repo path.

### `git_automation/core/models.py` (pydantic)

`Remote{name,url}`, `Upstream{remote,branch}`, `RepoStatus{path,current_branch,
upstream,remotes,ahead,behind,dirty}`, `IdentityState{git_name,git_email,
gh_authenticated,gh_user,needs_onboarding}`, `CommandResult{ok,output}`.

### `git_automation/core/git/client.py`

```python
async def run_git(args: list[str], cwd: str | None = None) -> CommandResult
async def get_global_identity() -> tuple[str | None, str | None]   # (name, email)
async def set_global_identity(name: str, email: str) -> None
async def list_remotes(path: str) -> list[Remote]
async def get_status(path: str) -> RepoStatus
async def fetch(path: str, remote: str) -> CommandResult
async def pull(path: str, remote: str, branch: str | None = None) -> CommandResult
async def push(path: str, remote: str, branch: str | None = None,
               set_upstream: bool = False) -> CommandResult
```

### `git_automation/core/gh/client.py`

```python
async def auth_status() -> tuple[bool, str | None]      # (authenticated, user)
async def login_with_token(token: str) -> None          # gh auth login --with-token
def login_instructions() -> dict                        # {steps: [...], supports_token: bool}
```

### `git_automation/core/identity.py`

```python
async def get_identity_state() -> IdentityState
```

## Frontend contract

- Single-page app served by FastAPI from `web/static/`.
- On load, calls `GET /api/identity`; if `needs_onboarding`, shows the onboarding
  modal (git name/email form + gh token field with link to instructions).
- Repo view: a "remote" selector populated from `GET /api/repo/remotes`, plus
  Push / Pull / Fetch buttons that POST to the matching endpoint with the chosen
  remote and show the returned `output`.
- Uses the design system from `web/static/css/` (dark theme). No inline styles.

## Design-system contract (UI/UX)

- `web/static/css/tokens.css` — CSS custom properties: dark palette (background
  layers, surface, border, text primary/secondary, accent, success, danger),
  spacing, radius, typography scale.
- `web/static/css/app.css` — component styles using those tokens: buttons
  (primary/secondary/ghost), inputs/selects, modal/dialog, dropdown, toast,
  cards, layout. Class names documented in `styleguide.html`.
- `styleguide.html` — static gallery rendering every component (no JS logic) so
  the frontend agent knows the exact class names to use.
