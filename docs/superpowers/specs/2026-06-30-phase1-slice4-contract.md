# Phase 1 — Slice 4: Auto-refresh, Hunk Staging, Commit Diff, Polish

Adds quality-of-life features on top of the GitKraken cockpit. Same conventions:
FastAPI async JSON, `{"error":{code,message}}`, `core.process.run_process`
(arg lists, no shell), `validate_repo_path`, `_reject_option` on refs/branches,
`_resolve_worktree_file` for caller file paths. Destructive UI confirms.

## Backend API additions (base `/api`)

### Auto-refresh — file watcher (WebSocket)
- `WS /api/watch?path=` — watch the repo working tree with **`watchfiles`** (add
  it to `pyproject` deps if not already resolvable) and push debounced JSON
  frames `{"type":"change","paths":[...]}` when files change. Ignore noisy
  `.git/` internals but DO emit on `.git/index`, `.git/HEAD`, and `.git/refs/**`
  (so commits/checkouts/branch ops refresh too). **Localhost + Origin-gated**
  exactly like the terminal WS (`_origin_allowed`). One watcher per socket;
  stop + cleanup on disconnect.

### Hunk staging
- `GET /api/repo/hunks?path=&file=&staged=<bool>` →
  ```json
  { "file","binary":false,
    "hunks":[ { "header":"@@ -a,b +c,d @@", "patch":"<minimal applyable patch>",
                "lines":[ {"type":" "|"+"|"-","text":"..."} ] } ] }
  ```
  Parse `git diff [--cached] -- <file>`; each hunk's `patch` includes the file
  header lines + that one hunk, ready for `git apply`.
- `POST /api/git/stage-hunk`   body `{path,file,patch}` → `git apply --cached` (patch on stdin).
- `POST /api/git/unstage-hunk` body `{path,file,patch}` → `git apply --cached -R`.

### Historical commit-file diff
- `GET /api/repo/commit-diff?path=&sha=&file=` → `{file,diff,binary}` via
  `git show <sha> --first-parent -- <file>` (so merge commits work).

### Branch rename / track / amend
- `POST /api/git/branch/rename` body `{path,name,new_name}` → `git branch -m <name> <new_name>`.
- `POST /api/git/branch/track`  body `{path,remote_ref}` → create & checkout a
  local tracking branch: `git switch -c <leaf> --track <remote_ref>` (leaf =
  remote_ref minus the remote prefix). `_reject_option` both.
- `POST /api/git/commit/amend` body `{path,message?}` → `git commit --amend -m <message>`
  (or `--amend --no-edit` when message omitted).

### Models
`Hunk{header,patch,lines:list[HunkLine]}`, `HunkLine{type,text}`,
`FileHunks{file,binary,hunks}`. Reuse `DiffResult` for commit-diff.

## Frontend additions (vanilla ES, design-system classes)
- **Auto-refresh:** on repo open, connect `WS /api/watch?path=`; on a `change`
  frame, debounce (~250ms) then refresh changes + status + graph (+ merge-status
  if merging). Reconnect with backoff; tear down on repo switch.
- **Hunk staging:** in the diff view (`diff.js`), render each hunk with
  Stage-hunk / Unstage-hunk buttons (calls the hunk endpoints with that hunk's
  `patch`), then refresh.
- **Side-by-side diff toggle:** a control to switch the diff between inline and
  split (old | new) views.
- **Commit-file diff:** `commitdetail.js` file click → `getCommitDiff(path,sha,file)`
  (not the working-tree diff).
- **Fetch / drift indicator:** toolbar shows current branch ahead/behind; a
  manual Fetch refreshes it; optional periodic light fetch.
- **Commit search / filter:** a filter box over the graph that filters commits by
  message / author / short-sha (client-side over loaded commits; dims/hides
  non-matches).
- **Keyboard shortcuts:** e.g. `s` stage selected, `c` focus commit message /
  commit, `p` push, `f` fetch, `/` focus filter, `Esc` close menus/modals. Show a
  shortcuts hint (`?`).
- **Menu items:** branch **Rename** (wire the disabled item), double-click a
  **remote** branch → `track`, **Amend** toggle in the commit box.
- New `api.js` names (fixed): `watchUrl(path)`, `getHunks(path,file,staged)`,
  `stageHunk(path,file,patch)`, `unstageHunk(path,file,patch)`,
  `getCommitDiff(path,sha,file)`, `branchRename(path,name,newName)`,
  `branchTrack(path,remoteRef)`, `amendCommit(path,message)`.

## Design-system additions (UI/UX)
Additive in `app.css` + `styleguide.html`: per-hunk action row in `.diff`
(`.diff__hunk-head`, `__stage`/`__unstage`), a side-by-side variant
(`.diff--split` with two columns), a `.filter` search input for the graph, a
`.drift` ahead/behind badge for the toolbar, and a `.shortcuts` help overlay.
