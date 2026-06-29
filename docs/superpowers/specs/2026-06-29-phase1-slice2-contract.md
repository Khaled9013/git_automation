# Phase 1 — Slice 2: Full Git Client + Commit Graph + Folder Browser

Extends slice 1 (`2026-06-29-phase1-slice-contract.md`). Adds the full
GitKraken-class git operations, an interactive commit graph, and an in-app
folder browser for picking a repo. Same conventions: FastAPI, async, JSON,
errors as `{"error":{"code","message"}}`, subprocess via the centralized
non-blocking helper (no shell), repo paths validated.

Destructive operations (**discard**, **branch delete**, **merge**) MUST be
confirmed in the UI before the request is sent.

## New HTTP API (base `/api`)

### Filesystem browser (in-app repo picker)
- `GET /api/fs/home` → `{ "path": "<abs home dir>" }`
- `GET /api/fs/list?path=<abs dir>` →
  ```json
  { "path": "string", "parent": "string|null",
    "entries": [ { "name": "...", "path": "<abs>", "is_dir": true,
                   "is_git_repo": false } ] }
  ```
  Directories only, sorted (case-insensitive); `is_git_repo` true when the dir
  contains a `.git`. Permission/`not a dir` errors return the error shape.

### Working tree: changes / stage / commit / diff
- `GET /api/repo/changes?path=<abs>` →
  ```json
  { "staged":   [ { "path": "...", "status": "M" } ],
    "unstaged": [ { "path": "...", "status": "M" } ],
    "untracked":[ "path", ... ] }
  ```
  `status` is the git short code (M/A/D/R/C/U).
- `POST /api/git/stage`    body `{ "path": "...", "files": ["..."] }` → `{ok,output}`
- `POST /api/git/unstage`  body `{ "path": "...", "files": ["..."] }` → `{ok,output}`
- `POST /api/git/discard`  body `{ "path": "...", "files": ["..."] }` → `{ok,output}`
  (restore working-tree file(s); destructive — UI confirms first)
- `POST /api/git/commit`   body `{ "path": "...", "message": "..." }` → `{ok,output}`
  (commits staged changes; error if message empty or nothing staged)
- `GET /api/repo/diff?path=<abs>&file=<rel>&staged=<bool>` →
  `{ "file": "...", "diff": "<unified diff text>", "binary": false }`

### Branches
- `GET /api/repo/branches?path=<abs>` →
  ```json
  { "current": "main",
    "local":  [ { "name": "main", "is_current": true,
                  "upstream": "origin/main", "ahead": 0, "behind": 0 } ],
    "remote": [ { "name": "origin/feature-x" } ] }
  ```
- `POST /api/git/branch/create`   body `{ "path","name","checkout":bool }` → `{ok,output}`
- `POST /api/git/branch/checkout` body `{ "path","name" }` → `{ok,output}`
- `POST /api/git/branch/delete`   body `{ "path","name","force":bool }` → `{ok,output}`
- `POST /api/git/branch/merge`    body `{ "path","name" }` → `{ok,output}`  (merges `name` into current)

### Commit graph
- `GET /api/repo/graph?path=<abs>&limit=<int=200>` →
  ```json
  { "commits": [
      { "sha": "<full>", "short": "abc1234", "parents": ["<sha>", ...],
        "author": "Name", "date": "<ISO8601>", "subject": "...",
        "refs": ["HEAD -> main", "origin/main", "tag: v1"],
        "is_head": true }
    ] }
  ```
  Ordered for graph drawing (`git log --all --date-order`). Includes all refs
  (`--all`). Lane/column assignment is done client-side by the graph renderer.

## New module interfaces (Python)

### `git_automation/core/fs.py`
```python
def home_dir() -> str
async def list_dirs(path: str) -> list[FsEntry]   # dirs only, is_git_repo flag
```

### `git_automation/core/git/client.py` (additions)
```python
async def get_changes(path: str) -> Changes
async def stage(path: str, files: list[str]) -> CommandResult
async def unstage(path: str, files: list[str]) -> CommandResult
async def discard(path: str, files: list[str]) -> CommandResult
async def commit(path: str, message: str) -> CommandResult
async def get_diff(path: str, file: str, staged: bool = False) -> DiffResult
async def list_branches(path: str) -> BranchList
async def create_branch(path: str, name: str, checkout: bool = False) -> CommandResult
async def checkout_branch(path: str, name: str) -> CommandResult
async def delete_branch(path: str, name: str, force: bool = False) -> CommandResult
async def merge_branch(path: str, name: str) -> CommandResult
async def get_graph(path: str, limit: int = 200) -> list[GraphCommit]
```

### `git_automation/core/models/__init__.py` (additions, pydantic)
`FsEntry{name,path,is_dir,is_git_repo}`, `FileChange{path,status}`,
`Changes{staged:list[FileChange],unstaged:list[FileChange],untracked:list[str]}`,
`Branch{name,is_current,upstream,ahead,behind}`,
`BranchList{current,local:list[Branch],remote:list[BranchRef]}`,
`BranchRef{name}`, `DiffResult{file,diff,binary}`,
`GraphCommit{sha,short,parents,author,date,subject,refs,is_head}`.

## Frontend contract (additions)

All new UI reuses the extended design system (below). New JS modules (vanilla ES
modules, no deps): `fsbrowser.js`, `changes.js`, `branches.js`, `diff.js`, and
`graph.js`. `api.js` gains one function per new endpoint. `app.js` wires them.

- **Folder browser modal** ("Add / Open repo"): opens at `GET /api/fs/home`,
  lists dirs via `GET /api/fs/list`, breadcrumb + up/into navigation, a repo
  badge on `is_git_repo` dirs; "Open" loads the selected repo. Replaces the raw
  path text box as the primary entry point (keep the path field as a fallback).
- **Changes panel**: staged / unstaged / untracked lists with per-file and
  bulk Stage / Unstage / Discard (discard confirms); a commit message box +
  Commit button (disabled until a message exists and something is staged).
- **Diff viewer**: clicking a changed file shows its diff (`GET /api/repo/diff`).
- **Branch menu**: dropdown of local/remote branches (current marked,
  ahead/behind shown); switch on click; create / merge / delete actions
  (merge & delete confirm).
- **Commit graph**: rendered in a container `#commit-graph` from
  `GET /api/repo/graph`; nodes = commits with branch lanes and edges to parents,
  ref chips, HEAD highlighted; clicking a node shows its details. Must stay
  smooth up to ~500 commits.

### Graph renderer interface (so it can be built independently)
`graph.js` exports:
```js
export function createGraph(containerEl)  // -> { render(path), clear() }
```
It fetches commits via `api.getGraph(path)` (defined in `api.js`), assigns lanes
client-side, and draws with SVG using the `.graph*` classes from the design
system. The container `#commit-graph` is created by the frontend (panels) agent.

## Design-system additions (UI/UX)

Extend `app.css` (token-driven, no new colors hardcoded) + show every new
component in `styleguide.html`:
- `.fs-browser` (rows w/ folder icon + `.is-repo` badge, `.breadcrumb`).
- `.changes` file rows (`__status` glyph, `__path`, per-row action buttons),
  section headers, `.commit-box`.
- `.diff` viewer (`__line`, `--add` / `--del` / `--meta` using success/danger
  subtle tokens; monospaced).
- `.branch-menu` items (`.is-current` marker, ahead/behind chips).
- `.graph` styles: `__node` dot, a small categorical **lane palette** (derive
  4–6 lane colors as tokens), `__edge` stroke, `__ref` chip, `.is-head` /
  `.is-selected` states, and a `.graph__row` hover.
