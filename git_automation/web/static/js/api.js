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
