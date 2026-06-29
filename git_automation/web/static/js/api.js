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
