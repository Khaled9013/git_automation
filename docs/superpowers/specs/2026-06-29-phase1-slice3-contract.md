# Phase 1 — Slice 3: GitKraken-style Layout, Interactions, Conflicts, Toolbar

Extends slices 1 & 2 into the full GitKraken experience: a 3-pane layout, a ref
sidebar, a rich graph with branch/tag labels, a commit-detail panel, click /
double-click / right-click interactions, reset/cherry-pick, a **visual 3-way
merge conflict editor**, and the full toolbar (Stash/Pop, Undo/Redo, embedded
Terminal, Pull/Push/Branch, Create PR).

Same conventions: FastAPI async JSON, errors `{"error":{code,message}}`,
subprocess via `core.process.run_process` (arg lists, no shell), repo paths
validated, all caller refs/branches guarded by `_reject_option`. Destructive
actions confirm in the UI.

## Layout (target)

```
┌───────────────────────────────────────────────────────────────────┐
│ Toolbar: repo ▾  branch ▾   Undo Redo  Pull Push Branch Stash Pop  …│
├───────────────┬───────────────────────────────────────────────────┤
│ Ref sidebar   │  BRANCH/TAG │ GRAPH │ COMMIT MESSAGE · author·sha·date│
│  LOCAL        │  (labels)   │  ●─┐  │  WIP / uncommitted (top row)    │
│  REMOTE       │             │  ●─┘  │  …commits…                      │
│  TAGS         │             │       │                                 │
│  WORKTREES    │             │       │                                 │
│  STASHES      ├─────────────┴───────┴─────────────────────────────────┤
│               │  Commit detail: message + changed files → click=diff   │
├───────────────┴───────────────────────────────────────────────────────┤
│ ▸_ Terminal (collapsible)                                              │
└───────────────────────────────────────────────────────────────────────┘
```

## Backend API additions (base `/api`)

### Refs sidebar
- `GET /api/repo/refs?path=` →
  ```json
  { "local":  [ {"name","is_current","upstream","ahead","behind"} ],
    "remote": [ {"name"} ],
    "tags":   [ {"name"} ],
    "worktrees": [ {"path","branch","is_current"} ],
    "stashes":   [ {"index","message"} ] }
  ```
  (Reuse slice-2 branch logic; add tags `git tag`, worktrees `git worktree list --porcelain`, stashes `git stash list`.)

### Commit detail
- `GET /api/repo/commit?path=&sha=` →
  ```json
  { "sha","short","parents":[...],"author","email","date",
    "subject","body","refs":[...],
    "files":[ {"path","status","additions","deletions"} ] }
  ```
  Via `git show --no-patch --pretty` + `git show --numstat --name-status`.

### Navigation / history rewrite
- `POST /api/git/checkout` body `{path, ref}` — branch name OR commit sha (detached). (Extends slice-1 checkout.)
- `POST /api/git/reset` body `{path, sha, mode}` — `mode ∈ {soft,mixed,hard}`.
- `POST /api/git/cherry-pick` body `{path, sha}`.

### Merge lifecycle + conflicts (the 3-way editor backend)
- `POST /api/git/merge` body `{path, name}` →
  `{ok, output, conflicted:bool, conflicts:[<paths>]}` (pull may also conflict — same shape).
- `GET /api/repo/merge-status?path=` → `{merging:bool, conflicts:[<paths>], message:"<MERGE_MSG>"}`.
- `GET /api/repo/conflict?path=&file=` →
  ```json
  { "file","base":"<:1 content>","ours":"<:2>","theirs":"<:3>",
    "merged":"<working file w/ markers>","binary":false }
  ```
  (`git show :1:file` / `:2:file` / `:3:file`; missing stage → null.)
- `POST /api/git/resolve` body `{path, file, content}` — write `content` to the
  working file then `git add -- file`.
- `POST /api/git/merge/continue` body `{path, message?}` — commit the merge
  (errors if conflicts remain).
- `POST /api/git/merge/abort` body `{path}` — `git merge --abort`.

### Stash
- `POST /api/git/stash` body `{path, message?, include_untracked?}`.
- `POST /api/git/stash/pop` body `{path, index?}`.
- `POST /api/git/stash/apply` body `{path, index}`.
- `POST /api/git/stash/drop` body `{path, index}`.

### Undo / Redo (reflog-based, best-effort — document limits)
- `GET /api/repo/reflog?path=&limit=` → `[ {"selector":"HEAD@{1}","subject":"..."} ]`.
- `POST /api/git/undo` body `{path}` — move HEAD back one reflog step with
  `git reset --keep HEAD@{1}` (refuses if it would discard uncommitted work).
- `POST /api/git/redo` body `{path}` — forward step when available.
  Both return `{ok, output, undone:"<subject>"}`. UI must label this "best-effort
  (reflog) — cannot reverse pushes or destructive ops."

### Pull requests (`gh`)
- `POST /api/gh/pr/create` body `{path, title, body?, base?, head?}` →
  `{number, url}` (`gh pr create`).
- `GET /api/gh/pr/list?path=` → `[ {number,title,url,state,head,base} ]`.

### Terminal (PTY over WebSocket) — security-sensitive
- `WS /api/terminal?path=` — spawns the user's `$SHELL` in `path` via a pseudo-
  terminal (`pty`/`os.openpty`, non-blocking). Messages: client→server `{type:
  "input"|"resize", data|cols/rows}`, server→client raw output frames. **Bind
  localhost only**; the server already binds `127.0.0.1`. One PTY per socket;
  killed on disconnect. Frontend uses **xterm.js vendored at
  `static/vendor/xterm/`** (offline, no CDN).

## New Python modules / surfaces
- `core/git/client.py` — add the above git ops (commit detail, checkout-ref,
  reset, cherry-pick, merge/abort/continue, conflict get/resolve, stash*,
  reflog/undo/redo, refs aggregation incl. tags/worktrees/stashes).
- `core/gh/client.py` — `pr_create`, `pr_list`.
- `core/terminal.py` — PTY session manager.
- `web/api/` — new routers: `gitops` extended, `merge.py`, `stash.py`,
  `terminal.py` (WebSocket), `pr.py`. Register in `web/api/__init__.py`.
- Models: `RefsBundle`, `CommitDetail`, `CommitFile`, `Conflict`, `MergeStatus`,
  `Stash`, `ReflogEntry`, `PullRequest`, `Worktree`, `Tag`.

## Frontend modules (vanilla ES, no deps except vendored xterm.js)
- `layout.js` (3-pane grid + collapsible terminal), `sidebar.js` (ref tree),
  `toolbar.js`, `contextmenu.js` (reusable right-click menu), `commitdetail.js`,
  `mergeeditor.js` (3-way editor), `stash.js`, `terminal.js` (xterm + WS),
  `undo.js`, plus `api.js`/`app.js` wiring. The slice-2 graph (`graph.js`) is
  embedded as the center pane and gains: branch/tag label column, a top WIP row,
  and emits single-click (detail), double-click (checkout), right-click (menu).

### Carried-over fix (from the Stage-D debugging pass)
- **All mutating ops must honor `{ok:false}`.** Git-level failures return HTTP
  200 with `{ok:false, output}` (not a thrown error). The frontend currently
  only reacts to thrown `ApiError`s, so a rejected commit/stage/branch op can
  wrongly show a success toast. In this slice, every mutating call site must
  check `result.ok` and surface failure (error toast + console) when false.

### Interaction contract (graph + sidebar)
- **single-click** commit → load `GET /api/repo/commit` into the detail panel.
- **double-click** commit/branch → checkout (`POST /api/git/checkout`), confirm
  if it would detach HEAD or discard changes.
- **right-click** commit → menu: Checkout, Merge into current, Reset ▸
  (Soft/Mixed/Hard), Cherry-pick, Create PR, Copy SHA. Right-click branch (in
  sidebar or label) → Checkout, Merge, Rename, Delete, Create PR.
- Any merge/pull that returns `conflicted:true` opens the **merge editor**.

## Design-system additions (UI/UX)
Extend `app.css` + `styleguide.html` (token-driven, additive): `.layout-3pane`
grid, `.sidebar` ref tree (`__section`,`__item`,`.is-current`,counts),
`.toolbar2` top bar buttons w/ icons, `.ctxmenu` (`__item`,`__sep`,`__submenu`),
`.commit-detail` (header + `.file-list` rows), `.merge-editor`
(`__pane--ours/--theirs/--result`, conflict hunk controls, `--add/--del`),
`.stash-list`, `.terminal-pane`, and graph label chips in the BRANCH/TAG column.
