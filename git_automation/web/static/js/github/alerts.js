// github/alerts.js — live notification stream, unread badge, and browser OS alerts.
//
// Subscribes to `WS /api/github/events`, which the backend pushes on every poll
// round: `{type:'notifications', count:<unread>, items:[...], new:[...]}` (plus a
// `{type:'test'}` ping). On each push we sync the app-nav unread badge from
// `count`, fire an in-app toast + optional browser `Notification` for NEW items
// whose reason is `mention`/`assign(ee)`, and call the `onChange(items)` hook so
// the open view live-refreshes. The socket reconnects with capped backoff; while
// it is down a slow (90s) fallback poll of GET /api/github/notifications keeps the
// badge and view alive. Browser-notification permission is requested only via a
// user toggle (a user gesture), never on load. Clicking an OS notification
// focuses the window and opens that thread in the detail pane.

import * as api from './api.js';

const FALLBACK_INTERVAL_MS = 90000; // slow poll, used ONLY while the socket is down
const RECONNECT_BASE_MS = 1000; // first reconnect delay
const RECONNECT_MAX_MS = 30000; // capped backoff ceiling
// Keys are `id@updated_at`. Bumping the version resets a possibly-stuck local
// dedup set so a fresh session re-seeds cleanly (the first diff is silent).
const SEEN_KEY = 'gh.alerts.seen.v3';
const PREF_KEY = 'gh.alerts.browserEnabled';
// Cap the dedup set so a chatty thread (a new `id@updated_at` key on every
// activity bump) can't grow it without bound and blow the ~5MB localStorage
// quota. A `Set` preserves insertion order, so "oldest" is simply the first
// entries; when adding would exceed the cap we evict from the front.
const SEEN_MAX = 1000;

// Reasons that warrant an active alert (toast / OS notification). Deliberately
// broad: after your first mention GitHub auto-subscribes you, so a REPEAT mention
// on the same thread usually arrives as `comment`/`author`, not `mention`.
// Including those is what makes the 2nd/3rd/4th mention alert you, not just the
// first. This is now only a FALLBACK default — the live allow-list is broadcast
// by the backend on every `notifications` event (its `_NOTIFY_REASONS`, sent as
// `alert_reasons`) so the two can no longer silently drift; see below.
const ALERT_REASONS = new Set([
  'mention', 'team_mention', 'assign', 'assigned',
  'review_requested', 'comment', 'author', 'manual', 'invitation',
]);
// The effective set consulted by isAlertReason(). Starts as the fallback and is
// replaced when the server sends a non-empty `alert_reasons` array (see
// applyAlertReasons), so we track the authoritative backend list at runtime
// while still working against an older backend or before the first event lands.
let effectiveAlertReasons = ALERT_REASONS;

// Adopt the server's authoritative allow-list. The backend sends `'assign'`,
// but the UI has always defensively matched the `'assigned'` alias too; we keep
// that guarantee by unioning in `'assigned'` whenever `'assign'` is present, so
// isAlertReason('assign') and isAlertReason('assigned') both stay true.
function applyAlertReasons(reasons) {
  if (!Array.isArray(reasons) || reasons.length === 0) return;
  const set = new Set(reasons.map((r) => String(r || '').toLowerCase()));
  if (set.has('assign')) set.add('assigned');
  effectiveAlertReasons = set;
}
function isAlertReason(reason) {
  return effectiveAlertReasons.has(String(reason || '').toLowerCase());
}

/** Human phrasing for a reason: toast title + the OS-notification verb. */
function reasonPhrasing(reason) {
  switch (String(reason || '').toLowerCase()) {
    case 'mention':
    case 'team_mention': return { label: 'New mention', verb: 'mentioned you in' };
    case 'assign':
    case 'assigned': return { label: 'New assignment', verb: 'assigned you to' };
    case 'review_requested': return { label: 'Review requested', verb: 'requested your review on' };
    default: return { label: 'New activity', verb: 'new activity in' };
  }
}

// GitHub reuses one notification thread (stable id) per issue and only bumps
// updated_at on new activity. Key the seen-set on (id, updated_at) so a
// re-mention in an already-seen thread re-alerts instead of being swallowed.
function keyOf(note) {
  return `${note.id}@${note.updated_at || ''}`;
}

function loadSeen() {
  try {
    const raw = localStorage.getItem(SEEN_KEY);
    const arr = raw ? JSON.parse(raw) : null;
    const set = new Set(Array.isArray(arr) ? arr : []);
    // A set persisted before the cap existed may be oversized — bound it now
    // (keeping the most-recent keys, which sit at the tail).
    if (set.size > SEEN_MAX) trimSeen(set, SEEN_MAX);
    return set;
  } catch {
    return new Set();
  }
}

// Evict the oldest keys until the set is at most `max` entries. A `Set`
// iterates in insertion order, so the first keys are the oldest.
function trimSeen(set, max) {
  if (set.size <= max) return;
  const overflow = set.size - max;
  const it = set.values();
  for (let i = 0; i < overflow; i++) set.delete(it.next().value);
}

// Add a key while holding the set to its size cap (evicting oldest first).
function addSeen(set, key) {
  if (set.has(key)) return;
  if (set.size >= SEEN_MAX) trimSeen(set, SEEN_MAX - 1);
  set.add(key);
}

function saveSeen(set) {
  // Bound before persisting: the cap is the primary defence against runaway
  // localStorage growth, independent of quota errors.
  trimSeen(set, SEEN_MAX);
  try {
    localStorage.setItem(SEEN_KEY, JSON.stringify([...set]));
  } catch {
    // Likely a QuotaExceededError. Drop the oldest half and retry once so
    // dedup keeps persisting instead of silently dying (after which old
    // alerts would re-fire every session).
    trimSeen(set, Math.floor(set.size / 2));
    try {
      localStorage.setItem(SEEN_KEY, JSON.stringify([...set]));
    } catch {
      /* storage still unavailable — non-fatal, alerts just re-fire next session */
    }
  }
}

/**
 * @param {{
 *   badgeEl: HTMLElement,                // .appnav__badge on the GitHub tab
 *   toast: (variant,title,msg?)=>void,   // ui.js toast
 *   onOpenThread: (note)=>void,          // open a notification's thread in detail
 * }} opts
 */
export function createAlerts({ badgeEl, toast, onOpenThread }) {
  let running = false;
  let seeded = false; // first client-side diff only seeds the seen-set (no spam)
  const seen = loadSeen();
  // localStorage persistence means a returning user is already "seeded".
  if (seen.size > 0) seeded = true;

  let onChange = null; // optional hook for the view to refresh its list

  // Connection state
  let socket = null;
  let reconnectTimer = null;
  let fallbackTimer = null;
  let backoff = RECONNECT_BASE_MS;

  // ---- Unread badge --------------------------------------------------------
  // `.appnav__badge` sets `display:inline-grid`, which defeats the [hidden]
  // attribute, so we hide a zero count by detaching the node from the tab
  // (no CSS / inline styles) and re-appending it when there's something to show.
  const badgeParent = badgeEl ? badgeEl.parentNode : null;
  function setBadge(count) {
    if (!badgeEl) return;
    const n = Math.max(0, count | 0);
    badgeEl.textContent = String(n);
    badgeEl.setAttribute('aria-label', `${n} unread`);
    if (n === 0) {
      if (badgeEl.parentNode) badgeEl.parentNode.removeChild(badgeEl);
    } else if (!badgeEl.parentNode && badgeParent) {
      badgeParent.appendChild(badgeEl);
    }
  }

  // ---- Browser-notification preference (user-toggled) ----------------------
  function browserEnabled() {
    return localStorage.getItem(PREF_KEY) === '1';
  }
  function supported() {
    return typeof window !== 'undefined' && 'Notification' in window;
  }
  function permission() {
    return supported() ? Notification.permission : 'denied';
  }

  /**
   * Enable browser notifications. MUST be called from a user gesture (a toggle).
   * Requests permission once if needed; returns the final boolean enabled state.
   */
  async function enableBrowser() {
    if (!supported()) {
      if (toast) toast('error', 'Not supported', 'This browser cannot show desktop notifications.');
      return false;
    }
    let perm = Notification.permission;
    if (perm === 'default') {
      try {
        perm = await Notification.requestPermission();
      } catch {
        perm = 'denied';
      }
    }
    if (perm !== 'granted') {
      localStorage.setItem(PREF_KEY, '0');
      if (toast) toast('info', 'Notifications blocked', 'Allow notifications in your browser to enable OS alerts.');
      return false;
    }
    localStorage.setItem(PREF_KEY, '1');
    return true;
  }

  function disableBrowser() {
    localStorage.setItem(PREF_KEY, '0');
  }

  function fireOsNotification(note) {
    if (!supported() || permission() !== 'granted' || !browserEnabled()) return;
    const { verb } = reasonPhrasing(note.reason);
    try {
      const n = new Notification(`GitHub: ${note.repo}`, {
        body: `${verb} ${note.title}`,
        tag: `gh-${note.id}`,
      });
      n.addEventListener('click', () => {
        try { window.focus(); } catch { /* ignore */ }
        if (onOpenThread) onOpenThread(note);
        n.close();
      });
    } catch {
      /* constructing a Notification can throw on some platforms — non-fatal */
    }
  }

  // ---- Shared item processing ----------------------------------------------
  // Fire a toast + OS notification for genuinely-new mention/assign items,
  // de-duplicated against the persisted seen-set so a socket replay (reconnect)
  // never re-alerts.
  function processNew(list) {
    if (!Array.isArray(list)) return;
    for (const note of list) {
      if (!note || note.id == null || seen.has(keyOf(note))) continue;
      addSeen(seen, keyOf(note));
      // Read threads (now included in the stream for history) must never alert.
      if (!note.unread || !isAlertReason(note.reason)) continue;
      const { label } = reasonPhrasing(note.reason);
      if (toast) toast('info', label, `${note.repo} — ${note.title}`);
      fireOsNotification(note);
    }
    saveSeen(seen);
  }

  /** Remember every currently-visible item so a later diff won't re-fire it. */
  function markSeen(items) {
    if (!Array.isArray(items)) return;
    let changed = false;
    for (const it of items) {
      if (it && it.id != null && !seen.has(keyOf(it))) { addSeen(seen, keyOf(it)); changed = true; }
    }
    if (changed) saveSeen(seen);
  }

  // ---- WebSocket stream ----------------------------------------------------
  function handleMessage(data) {
    let msg;
    try {
      msg = JSON.parse(data);
    } catch {
      return; // ignore non-JSON frames
    }
    if (!msg || typeof msg !== 'object') return;

    if (msg.type === 'test') {
      if (toast) toast('info', 'Test event received', 'The live update channel is working.');
      return;
    }

    if (msg.type === 'notifications') {
      const items = Array.isArray(msg.items) ? msg.items : [];
      const count = Number.isFinite(msg.count)
        ? msg.count
        : items.filter((it) => it && it.unread).length;
      setBadge(count);
      // Prefer the backend's authoritative allow-list (falls back to the local
      // default when the field is absent) BEFORE deciding what alerts to fire.
      applyAlertReasons(msg.alert_reasons);
      // The server already diffs `new` per poll round; we still de-dup locally.
      processNew(msg.new);
      markSeen(items);
      seeded = true;
      if (onChange) onChange(items);
    }
  }

  function connect() {
    if (!running) return;
    let url;
    try {
      url = api.eventsUrl();
    } catch {
      scheduleReconnect();
      return;
    }
    let s;
    try {
      s = new WebSocket(url);
    } catch {
      scheduleReconnect();
      return;
    }
    socket = s;
    s.addEventListener('open', () => {
      backoff = RECONNECT_BASE_MS; // healthy socket — reset backoff
      stopFallback(); // live stream takes over from the slow poll
    });
    s.addEventListener('message', (e) => handleMessage(e.data));
    s.addEventListener('close', () => {
      if (socket === s) socket = null;
      scheduleReconnect();
    });
    s.addEventListener('error', () => {
      try { s.close(); } catch { /* triggers 'close' → reconnect */ }
    });
  }

  function scheduleReconnect() {
    if (!running) return;
    startFallback(); // keep the badge/view alive while the socket is down
    if (reconnectTimer) return;
    const delay = backoff;
    backoff = Math.min(RECONNECT_MAX_MS, backoff * 2);
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  // ---- Fallback poll (only while the socket is down) -----------------------
  async function fallbackPoll() {
    let items;
    try {
      items = await api.listNotifications(); // all=false: unread inbox only
    } catch {
      return; // transient/auth failure — keep badge as-is, retry next tick
    }
    if (!Array.isArray(items)) return;

    setBadge(items.filter((it) => it && it.unread).length);

    if (!seeded) {
      // First-ever diff only seeds — never replay the whole inbox as "new".
      markSeen(items);
      seeded = true;
    } else {
      processNew(items.filter((it) => it && it.id != null && !seen.has(keyOf(it))));
      markSeen(items);
    }
    if (onChange) onChange(items);
  }

  function startFallback() {
    if (fallbackTimer) return;
    fallbackTimer = setInterval(fallbackPoll, FALLBACK_INTERVAL_MS);
    fallbackPoll(); // immediate catch-up while waiting to reconnect
  }
  function stopFallback() {
    if (fallbackTimer) { clearInterval(fallbackTimer); fallbackTimer = null; }
  }

  // ---- Lifecycle -----------------------------------------------------------
  function start() {
    if (running) return;
    running = true;
    fallbackPoll(); // seed the badge immediately, before the first push lands
    connect();
  }
  function stop() {
    running = false;
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    stopFallback();
    if (socket) {
      try { socket.close(); } catch { /* ignore */ }
      socket = null;
    }
  }

  // Hide the markup's initial "0" immediately (before the first push resolves).
  setBadge(0);

  return {
    start,
    stop,
    setBadge,
    enableBrowser,
    disableBrowser,
    browserEnabled,
    supported,
    permission,
    setOnChange(fn) { onChange = fn; },
  };
}
