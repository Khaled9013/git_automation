// github/alerts.js — notification polling, unread badge, and browser OS alerts.
//
// Polls GET /api/github/notifications on an interval (default 60s; never faster
// than GitHub's advertised minimum), diffs new thread ids against a last-seen set
// in localStorage, keeps the app-nav unread badge in sync, and — for NEW items
// whose reason is `mention` or `assign(ee)` — fires an in-app toast plus an
// optional browser `Notification` (permission is requested only via a user
// toggle, never on load). Clicking the OS notification focuses the window and
// opens that thread in the detail pane.

import * as api from './api.js';

const MIN_INTERVAL_MS = 60000; // hard floor — never poll faster than 60s
const SEEN_KEY = 'gh.alerts.seenIds';
const PREF_KEY = 'gh.alerts.browserEnabled';

/** reasons that warrant an active alert (toast / OS notification). */
function isAlertReason(reason) {
  const r = String(reason || '').toLowerCase();
  return r === 'mention' || r === 'assign' || r === 'assigned';
}

function loadSeen() {
  try {
    const raw = localStorage.getItem(SEEN_KEY);
    const arr = raw ? JSON.parse(raw) : null;
    return new Set(Array.isArray(arr) ? arr : []);
  } catch {
    return new Set();
  }
}

function saveSeen(set) {
  try {
    localStorage.setItem(SEEN_KEY, JSON.stringify([...set]));
  } catch {
    /* storage unavailable — non-fatal, alerts just re-fire next session */
  }
}

/**
 * @param {{
 *   badgeEl: HTMLElement,                // .appnav__badge on the GitHub tab
 *   toast: (variant,title,msg?)=>void,   // ui.js toast
 *   onOpenThread: (note)=>void,          // open a notification's thread in detail
 *   intervalMs?: number,                 // poll cadence (clamped to >= 60s)
 * }} opts
 */
export function createAlerts({ badgeEl, toast, onOpenThread, intervalMs = MIN_INTERVAL_MS }) {
  const period = Math.max(MIN_INTERVAL_MS, Number(intervalMs) || MIN_INTERVAL_MS);
  let timer = null;
  let running = false;
  let seeded = false; // first poll only seeds the seen-set (no alert spam)
  const seen = loadSeen();
  // localStorage persistence means a returning user is already "seeded".
  if (seen.size > 0) seeded = true;

  let onChange = null; // optional hook for the view to refresh its list

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
    const verb = isAlertReason(note.reason) && note.reason.toLowerCase() === 'mention'
      ? 'mentioned you in'
      : 'assigned you to';
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

  // ---- Poll ----------------------------------------------------------------
  async function poll() {
    let items;
    try {
      items = await api.listNotifications();
    } catch {
      // Transient/auth failure — keep the badge as-is, retry next tick.
      return;
    }
    if (!Array.isArray(items)) return;

    const unreadCount = items.filter((it) => it.unread).length;
    setBadge(unreadCount);

    // New = not previously seen. On the very first poll we only seed.
    const fresh = [];
    for (const it of items) {
      if (!seen.has(it.id)) {
        if (seeded) fresh.push(it);
        seen.add(it.id);
      }
    }
    saveSeen(seen);
    seeded = true;

    for (const note of fresh) {
      if (!isAlertReason(note.reason)) continue;
      const reasonLabel = note.reason.toLowerCase() === 'mention' ? 'New mention' : 'New assignment';
      if (toast) toast('info', reasonLabel, `${note.repo} — ${note.title}`);
      fireOsNotification(note);
    }

    if (onChange) onChange(items);
  }

  // ---- Lifecycle -----------------------------------------------------------
  function start() {
    if (running) return;
    running = true;
    poll();
    timer = setInterval(poll, period);
  }
  function stop() {
    running = false;
    if (timer) { clearInterval(timer); timer = null; }
  }

  // Hide the markup's initial "0" immediately (before the first poll resolves).
  setBadge(0);

  return {
    start,
    stop,
    poll,
    setBadge,
    enableBrowser,
    disableBrowser,
    browserEnabled,
    supported,
    permission,
    setOnChange(fn) { onChange = fn; },
  };
}
