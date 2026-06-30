// github/api.js — fetch client for the GitHub cockpit endpoints (/api/github/*).
//
// Self-contained per the composable-tools principle: the GitHub view is just one
// consumer of this seam. Reuses the shared `ApiError` shape from ../api.js so
// callers can `try/catch` and surface `err.message` exactly like the git client.

import { ApiError } from '../api.js';

/**
 * Perform a fetch and normalise the response, mirroring ../api.js#request:
 * throws ApiError for non-2xx / `{error}` / FastAPI `{detail}`; returns JSON.
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
      body = { error: { code: 'bad_response', message: text } };
    }
  }

  if (!res.ok || (body && body.error)) {
    if (body && body.error) {
      throw new ApiError(body.error.message || 'Request failed', body.error.code, res.status);
    }
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

// ---- Endpoints --------------------------------------------------------------

/** GET /api/github/me → { login } */
export function me() {
  return request('/api/github/me');
}

/**
 * GET /api/github/notifications?all=&participating= → Notification[]
 * @param {{all?:boolean, participating?:boolean}} opts
 */
export function listNotifications({ all = false, participating = false } = {}) {
  const qs = new URLSearchParams();
  if (all) qs.set('all', 'true');
  if (participating) qs.set('participating', 'true');
  const suffix = qs.toString() ? `?${qs}` : '';
  return request(`/api/github/notifications${suffix}`);
}

/** POST /api/github/notifications/{thread_id}/read → { ok } */
export function markNotificationRead(threadId) {
  return request(`/api/github/notifications/${encodeURIComponent(threadId)}/read`, {
    method: 'POST',
  });
}

/**
 * GET /api/github/issues?repo=&filter=&state= → IssueSummary[]
 * @param {{repo?:string, filter?:'assigned'|'mentioned'|'created'|'all', state?:'open'|'closed'}} opts
 */
export function listIssues({ repo = null, filter = 'assigned', state = 'open' } = {}) {
  const qs = new URLSearchParams();
  if (repo) qs.set('repo', repo);
  if (filter) qs.set('filter', filter);
  if (state) qs.set('state', state);
  return request(`/api/github/issues?${qs}`);
}

/** GET /api/github/issue?repo=&number= → IssueDetail */
export function getIssue(repo, number) {
  const qs = new URLSearchParams({ repo, number: String(number) });
  return request(`/api/github/issue?${qs}`);
}

/** POST /api/github/issue/comment {repo,number,body} → Comment */
export function addComment(repo, number, body) {
  return postJson('/api/github/issue/comment', { repo, number, body });
}

// ---- Live events ------------------------------------------------------------

/**
 * Build the `WS /api/github/events` URL, picking `wss://` for an https page and
 * `ws://` otherwise, on the same host/port as the app (mirrors `watchUrl` in
 * ../api.js). The socket pushes `{type:'notifications', count, items, new}` on
 * every poll round and `{type:'test'}` for a manual ping.
 */
export function eventsUrl() {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${window.location.host}/api/github/events`;
}

/**
 * POST /api/github/test-notification → { ok, delivered }
 * Fires an OS notification on the server; `delivered:false` means the server has
 * no `notify-send` binary available.
 */
export function testNotification() {
  return request('/api/github/test-notification', { method: 'POST' });
}
