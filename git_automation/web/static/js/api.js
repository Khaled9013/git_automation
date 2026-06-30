// api.js — thin typed-ish fetch client for the git-automation backend.
//
// Every call returns parsed JSON on success and throws an `ApiError` on the
// backend's `{ error: { code, message } }` shape (or any non-2xx response).
// Callers should `try/catch` and surface `err.message` to the user.

/** Error carrying the backend's stable `code` plus a human message. */
export class ApiError extends Error {
  constructor(message, code = 'error', status = 0) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
  }
}

/**
 * Perform a fetch and normalise the response.
 * Throws ApiError for non-2xx or the `{error}` shape; returns parsed JSON otherwise.
 */
async function request(url, options = {}) {
  let res;
  try {
    res = await fetch(url, options);
  } catch (networkErr) {
    throw new ApiError(
      `Could not reach the server. ${networkErr.message || ''}`.trim(),
      'network_error',
    );
  }

  let body = null;
  const text = await res.text();
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      // Non-JSON body (shouldn't happen for the API) — keep raw text.
      body = { error: { code: 'bad_response', message: text } };
    }
  }

  if (!res.ok || (body && body.error)) {
    // Domain error shape: { error: { code, message } }
    if (body && body.error) {
      throw new ApiError(body.error.message || 'Request failed', body.error.code, res.status);
    }
    // FastAPI validation error shape: { detail: [...] | "..." }
    if (body && body.detail) {
      const detail = Array.isArray(body.detail)
        ? body.detail.map((d) => d.msg || JSON.stringify(d)).join('; ')
        : String(body.detail);
      throw new ApiError(detail, 'validation_error', res.status);
    }
    throw new ApiError(`Request failed (HTTP ${res.status})`, 'http_error', res.status);
  }

  return body;
}

const jsonHeaders = { 'Content-Type': 'application/json' };

function postJson(url, payload) {
  return request(url, { method: 'POST', headers: jsonHeaders, body: JSON.stringify(payload) });
}

// ---- Identity / onboarding --------------------------------------------------

/** GET /api/identity */
export function getIdentity() {
  return request('/api/identity');
}

/** POST /api/identity/git */
export function setGitIdentity(name, email) {
  return postJson('/api/identity/git', { name, email });
}

/** GET /api/gh/login/instructions */
export function getLoginInstructions() {
  return request('/api/gh/login/instructions');
}

/** POST /api/gh/login/token */
export function loginWithToken(token) {
  return postJson('/api/gh/login/token', { token });
}

// ---- Repository -------------------------------------------------------------

/** GET /api/repo?path= */
export function getRepo(path) {
  return request(`/api/repo?path=${encodeURIComponent(path)}`);
}

/** GET /api/repo/remotes?path= */
export function getRemotes(path) {
  return request(`/api/repo/remotes?path=${encodeURIComponent(path)}`);
}

// ---- Git remote operations --------------------------------------------------

/** POST /api/git/fetch */
export function gitFetch(path, remote) {
  return postJson('/api/git/fetch', { path, remote });
}

/** POST /api/git/pull */
export function gitPull(path, remote, branch = null) {
  return postJson('/api/git/pull', { path, remote, branch });
}

/** POST /api/git/push */
export function gitPush(path, remote, branch = null, setUpstream = false) {
  return postJson('/api/git/push', { path, remote, branch, set_upstream: setUpstream });
}

// ---- Filesystem browser -----------------------------------------------------

/** GET /api/fs/home → { path } */
export function fsHome() {
  return request('/api/fs/home');
}

/** GET /api/fs/list?path= → { path, parent, entries:[{name,path,is_dir,is_git_repo}] } */
export function fsList(path) {
  return request(`/api/fs/list?path=${encodeURIComponent(path)}`);
}

// ---- Working tree: changes / stage / commit / diff --------------------------

/** GET /api/repo/changes?path= → { staged, unstaged, untracked } */
export function getChanges(path) {
  return request(`/api/repo/changes?path=${encodeURIComponent(path)}`);
}

/** POST /api/git/stage */
export function stage(path, files) {
  return postJson('/api/git/stage', { path, files });
}

/** POST /api/git/unstage */
export function unstage(path, files) {
  return postJson('/api/git/unstage', { path, files });
}

/** POST /api/git/discard (destructive — confirm in UI first) */
export function discard(path, files) {
  return postJson('/api/git/discard', { path, files });
}

/** POST /api/git/commit */
export function commit(path, message) {
  return postJson('/api/git/commit', { path, message });
}

/** POST /api/git/delete — remove files from the working tree (destructive — confirm in UI first). */
export function deleteFiles(path, files) {
  return postJson('/api/git/delete', { path, files });
}

/** GET /api/repo/diff?path=&file=&staged= → { file, diff, binary } */
export function getDiff(path, file, staged = false) {
  const q = `path=${encodeURIComponent(path)}&file=${encodeURIComponent(file)}&staged=${staged ? 'true' : 'false'}`;
  return request(`/api/repo/diff?${q}`);
}

// ---- Branches ---------------------------------------------------------------

/** GET /api/repo/branches?path= → { current, local, remote } */
export function getBranches(path) {
  return request(`/api/repo/branches?path=${encodeURIComponent(path)}`);
}

/** POST /api/git/branch/create */
export function branchCreate(path, name, checkout = false) {
  return postJson('/api/git/branch/create', { path, name, checkout });
}

/** POST /api/git/branch/checkout */
export function branchCheckout(path, name) {
  return postJson('/api/git/branch/checkout', { path, name });
}

/** POST /api/git/branch/delete (destructive — confirm in UI first) */
export function branchDelete(path, name, force = false) {
  return postJson('/api/git/branch/delete', { path, name, force });
}

/** POST /api/git/branch/merge (destructive — confirm in UI first) */
export function branchMerge(path, name) {
  return postJson('/api/git/branch/merge', { path, name });
}

// ---- Commit graph -----------------------------------------------------------

/** GET /api/repo/graph?path=&limit= → { commits:[...] } */
export function getGraph(path, limit = 200) {
  return request(`/api/repo/graph?path=${encodeURIComponent(path)}&limit=${encodeURIComponent(limit)}`);
}

// ---- Slice 3: refs sidebar --------------------------------------------------

/** GET /api/repo/refs?path= → { local, remote, tags, worktrees, stashes } */
export function getRefs(path) {
  return request(`/api/repo/refs?path=${encodeURIComponent(path)}`);
}

// ---- Slice 3: commit detail -------------------------------------------------

/** GET /api/repo/commit?path=&sha= → { sha, short, parents, author, …, files } */
export function getCommit(path, sha) {
  return request(`/api/repo/commit?path=${encodeURIComponent(path)}&sha=${encodeURIComponent(sha)}`);
}

// ---- Slice 3: navigation / history rewrite ----------------------------------

/** POST /api/git/checkout — branch name OR commit sha (detached). */
export function checkout(path, ref) {
  return postJson('/api/git/checkout', { path, ref });
}

/** POST /api/git/reset — mode ∈ {soft,mixed,hard} (destructive — confirm in UI). */
export function reset(path, sha, mode) {
  return postJson('/api/git/reset', { path, sha, mode });
}

/** POST /api/git/cherry-pick */
export function cherryPick(path, sha) {
  return postJson('/api/git/cherry-pick', { path, sha });
}

// ---- Slice 3: merge lifecycle + conflicts -----------------------------------

/** POST /api/git/merge → { ok, output, conflicted, conflicts } */
export function mergeBranch(path, name) {
  return postJson('/api/git/merge', { path, name });
}

/** GET /api/repo/merge-status?path= → { merging, conflicts, message } */
export function getMergeStatus(path) {
  return request(`/api/repo/merge-status?path=${encodeURIComponent(path)}`);
}

/** GET /api/repo/conflict?path=&file= → { file, base, ours, theirs, merged, binary } */
export function getConflict(path, file) {
  return request(`/api/repo/conflict?path=${encodeURIComponent(path)}&file=${encodeURIComponent(file)}`);
}

/** POST /api/git/resolve — write resolved content + stage. */
export function resolveConflict(path, file, content) {
  return postJson('/api/git/resolve', { path, file, content });
}

/** POST /api/git/merge/continue — commit the in-progress merge. */
export function mergeContinue(path, message = null) {
  return postJson('/api/git/merge/continue', { path, message });
}

/** POST /api/git/merge/abort */
export function mergeAbort(path) {
  return postJson('/api/git/merge/abort', { path });
}

// ---- Slice 3: stash ---------------------------------------------------------

/** POST /api/git/stash */
export function stash(path, message = null) {
  return postJson('/api/git/stash', { path, message });
}

/** POST /api/git/stash — stash only the given files (partial stash). */
export function stashFiles(path, files, message = null) {
  return postJson('/api/git/stash', { path, message, files });
}

/** POST /api/git/stash/pop */
export function stashPop(path, index = null) {
  return postJson('/api/git/stash/pop', { path, index });
}

/** POST /api/git/stash/apply */
export function stashApply(path, index) {
  return postJson('/api/git/stash/apply', { path, index });
}

/** POST /api/git/stash/drop (destructive — confirm in UI first) */
export function stashDrop(path, index) {
  return postJson('/api/git/stash/drop', { path, index });
}

// ---- Slice 3: undo / redo (reflog-based, best-effort) -----------------------

/** GET /api/repo/reflog?path=&limit= → [ {selector, subject} ] */
export function getReflog(path, limit = 50) {
  return request(`/api/repo/reflog?path=${encodeURIComponent(path)}&limit=${encodeURIComponent(limit)}`);
}

/** POST /api/git/undo → { ok, output, undone } */
export function undo(path) {
  return postJson('/api/git/undo', { path });
}

/** POST /api/git/redo → { ok, output, undone } */
export function redo(path) {
  return postJson('/api/git/redo', { path });
}

// ---- Slice 3: pull requests (gh) --------------------------------------------

/** POST /api/gh/pr/create → { number, url } */
export function prCreate(path, title, body = null, base = null, head = null) {
  return postJson('/api/gh/pr/create', { path, title, body, base, head });
}

/** GET /api/gh/pr/list?path= → [ {number, title, url, state, head, base} ] */
export function prList(path) {
  return request(`/api/gh/pr/list?path=${encodeURIComponent(path)}`);
}
