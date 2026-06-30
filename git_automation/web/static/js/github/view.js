// github/view.js — the GitHub cockpit view: a two-pane .gh-layout with a list
// pane (Notifications vs Issues, driven by a .gh-filter tab strip + issue
// sub-filters) and a .gh-detail pane. Markup/classes mirror styleguide.html.
//
// Lazily constructed by shell.js on first open. The alerts controller (polling +
// badge, created in shell.js so the badge stays live on any tab) is injected via
// init(); this view owns the OS-alert toggle and reuses alerts for list refresh.

import { el } from '../ui.js';
import * as api from './api.js';
import { createDetail } from './detail.js';
import { relTime } from './util.js';

const ICONS = {
  mention: '<circle cx="12" cy="12" r="4"/><path d="M16 12v1.5a2.5 2.5 0 0 0 5 0V12a9 9 0 1 0-3.5 7.1"/>',
  assign: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M19 8v6M22 11h-6"/>',
  review: '<path d="m9 11 3 3 8-8"/><path d="M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h9"/>',
  comment: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
  open: '<circle cx="12" cy="12" r="9"/><path d="M9 12h6"/>',
  closed: '<path d="m9 11 3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>',
};

const SVG_HEAD = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">';

/** Map a notification `reason` to a chip kind + label. */
function reasonChip(reason) {
  const r = String(reason || '').toLowerCase();
  if (r === 'mention') return { kind: 'mention', label: 'Mention' };
  if (r === 'assign' || r === 'assigned') return { kind: 'assign', label: 'Assigned' };
  if (r === 'review_requested' || r === 'review') return { kind: 'review', label: 'Review' };
  return { kind: 'comment', label: 'Comment' };
}

/** Build a `.gh-list__reason--{kind}` chip with a static (trusted) icon + label. */
function chip(kind, iconKey, label) {
  const span = el('span', { class: `gh-list__reason gh-list__reason--${kind}` });
  span.innerHTML = `${SVG_HEAD}${ICONS[iconKey]}</svg>`;
  span.appendChild(document.createTextNode(label));
  return span;
}

export function createView(root, { toast }) {
  let alerts = null;
  let built = false;

  let mode = 'notifications'; // 'notifications' | 'issues'
  let issueFilter = 'assigned'; // assigned | mentioned | created | all
  let issueState = 'open'; // open | closed
  let selectedKey = null; // `${repo}#${number}` of the open row

  // DOM refs (filled by build())
  let listEl, detail, panelTitle, modeTabs, issueControls, notifControls;
  let stateSelect, markReadBtn, osToggleBtn;
  let lastNotifications = [];
  let selectedNotificationId = null;

  // ---- Row builders --------------------------------------------------------
  function notificationRow(note) {
    const c = reasonChip(note.reason);
    const key = note.number != null ? `${note.repo}#${note.number}` : note.id;
    const row = el('button', {
      class: `gh-list__row${note.unread ? ' is-unread' : ''}${selectedKey === key ? ' is-selected' : ''}`,
      attrs: { role: 'listitem', type: 'button' },
    }, [
      chip(c.kind, c.kind, c.label),
      el('span', { class: 'gh-list__body' }, [
        el('span', { class: 'gh-list__title', text: note.title || '(no title)' }),
        el('span', {
          class: 'gh-list__repo',
          text: note.number != null ? `${note.repo} #${note.number}` : note.repo,
        }),
      ]),
      el('span', { class: 'gh-list__time', text: relTime(note.updated_at) }),
    ]);
    if (selectedKey === key) row.setAttribute('aria-current', 'true');
    row.addEventListener('click', () => openNotification(note));
    return row;
  }

  function issueRow(issue) {
    const open = String(issue.state).toLowerCase() === 'open';
    const key = `${issue.repo}#${issue.number}`;
    const meta = `${issue.repo} #${issue.number}` +
      (issue.comments ? ` · ${issue.comments} comment${issue.comments === 1 ? '' : 's'}` : '');
    const row = el('button', {
      class: `gh-list__row${selectedKey === key ? ' is-selected' : ''}`,
      attrs: { role: 'listitem', type: 'button' },
    }, [
      chip(open ? 'assign' : 'comment', open ? 'open' : 'closed', open ? 'Open' : 'Closed'),
      el('span', { class: 'gh-list__body' }, [
        el('span', { class: 'gh-list__title', text: issue.title || '(untitled)' }),
        el('span', { class: 'gh-list__repo', text: meta }),
      ]),
      el('span', { class: 'gh-list__time', text: relTime(issue.updated_at) }),
    ]);
    if (selectedKey === key) row.setAttribute('aria-current', 'true');
    row.addEventListener('click', () => {
      selectedKey = key;
      selectedNotificationId = null;
      renderRows(lastRows, true);
      detail.show({ repo: issue.repo, number: issue.number });
    });
    return row;
  }

  let lastRows = [];
  function emptyState(msg) {
    return el('div', { class: 'gh-list__empty', attrs: { role: 'listitem' } },
      [el('span', { class: 'gh-list__title', text: msg })]);
  }

  function renderRows(rows, isIssues) {
    lastRows = rows;
    listEl.replaceChildren();
    if (!rows.length) {
      listEl.appendChild(emptyState(isIssues ? 'No issues match this filter.' : 'No notifications.'));
      return;
    }
    for (const item of rows) {
      listEl.appendChild(isIssues ? issueRow(item) : notificationRow(item));
    }
  }

  // ---- Data loads ----------------------------------------------------------
  async function loadNotifications() {
    panelTitle.textContent = 'Notifications';
    listEl.setAttribute('aria-busy', 'true');
    try {
      const items = await api.listNotifications();
      lastNotifications = Array.isArray(items) ? items : [];
      if (mode === 'notifications') renderRows(lastNotifications, false);
    } catch (err) {
      toast('error', 'Could not load notifications', err.message);
      if (mode === 'notifications') renderRows([], false);
    } finally {
      listEl.setAttribute('aria-busy', 'false');
    }
  }

  async function loadIssues() {
    panelTitle.textContent = 'Issues';
    listEl.setAttribute('aria-busy', 'true');
    try {
      const items = await api.listIssues({ filter: issueFilter, state: issueState });
      if (mode === 'issues') renderRows(Array.isArray(items) ? items : [], true);
    } catch (err) {
      toast('error', 'Could not load issues', err.message);
      if (mode === 'issues') renderRows([], true);
    } finally {
      listEl.setAttribute('aria-busy', 'false');
    }
  }

  function reload() {
    return mode === 'notifications' ? loadNotifications() : loadIssues();
  }

  // ---- Interactions --------------------------------------------------------
  function openNotification(note) {
    selectedNotificationId = note.id;
    selectedKey = note.number != null ? `${note.repo}#${note.number}` : note.id;
    if (mode === 'notifications') renderRows(lastNotifications, false);
    if (note.number != null) {
      detail.show({ repo: note.repo, number: note.number });
    } else if (note.url) {
      window.open(note.url, '_blank', 'noopener');
    } else {
      toast('info', 'Nothing to open', 'This notification has no linked issue or PR.');
    }
  }

  async function markSelectedRead() {
    const note = lastNotifications.find((n) => n.id === selectedNotificationId);
    if (!note) {
      toast('info', 'Select a notification', 'Pick a notification to mark it read.');
      return;
    }
    try {
      const res = await api.markNotificationRead(note.id);
      if (res && res.ok === false) throw new Error('Server rejected the request.');
      note.unread = false;
      renderRows(lastNotifications, false);
      if (alerts) alerts.setBadge(lastNotifications.filter((n) => n.unread).length);
      toast('success', 'Marked read', `${note.repo} #${note.number ?? ''}`.trim());
    } catch (err) {
      toast('error', 'Could not mark read', err.message);
    }
  }

  // `.cluster { display:flex }` defeats the [hidden] attribute, so swap the
  // active control row by detaching/attaching it (no CSS / inline styles).
  function mountControls() {
    const body = listEl.parentNode;
    if (!body) return;
    if (issueControls.parentNode) issueControls.remove();
    if (notifControls.parentNode) notifControls.remove();
    body.insertBefore(mode === 'issues' ? issueControls : notifControls, listEl);
  }

  function setMode(next) {
    if (next === mode) return;
    mode = next;
    for (const t of modeTabs) {
      const active = t.dataset.mode === mode;
      t.classList.toggle('is-active', active);
      t.setAttribute('aria-selected', String(active));
    }
    mountControls();
    reload();
  }

  function refreshOsToggle() {
    if (!alerts || !osToggleBtn) return;
    const on = alerts.browserEnabled() && alerts.permission() === 'granted';
    osToggleBtn.setAttribute('aria-pressed', String(on));
    osToggleBtn.classList.toggle('is-active', on);
    osToggleBtn.textContent = on ? 'OS alerts: On' : 'OS alerts: Off';
  }

  async function toggleOsAlerts() {
    if (!alerts) return;
    const on = alerts.browserEnabled() && alerts.permission() === 'granted';
    if (on) {
      alerts.disableBrowser();
    } else {
      await alerts.enableBrowser(); // user-gesture-initiated permission request
    }
    refreshOsToggle();
  }

  // ---- Build the DOM -------------------------------------------------------
  function build() {
    // Mode strip: Notifications | Issues
    const tabNotif = el('button', {
      class: 'gh-filter__tab is-active', text: 'Notifications',
      attrs: { role: 'tab', 'aria-selected': 'true' },
    });
    tabNotif.dataset.mode = 'notifications';
    const tabIssues = el('button', {
      class: 'gh-filter__tab', text: 'Issues',
      attrs: { role: 'tab', 'aria-selected': 'false' },
    });
    tabIssues.dataset.mode = 'issues';
    modeTabs = [tabNotif, tabIssues];
    tabNotif.addEventListener('click', () => setMode('notifications'));
    tabIssues.addEventListener('click', () => setMode('issues'));

    osToggleBtn = el('button', {
      class: 'btn btn--ghost btn--sm', text: 'OS alerts: Off',
      attrs: { type: 'button', 'aria-pressed': 'false', title: 'Toggle desktop notifications for mentions & assignments' },
    });
    osToggleBtn.addEventListener('click', toggleOsAlerts);

    const headerRow = el('div', { class: 'cluster' }, [
      el('div', { class: 'gh-filter', attrs: { role: 'tablist', 'aria-label': 'GitHub section' } }, modeTabs),
      el('span', { class: 'toolbar2__spacer' }),
      osToggleBtn,
    ]);

    // Issue sub-filters (assigned / mentioned / created / all) + state
    const filterDefs = [
      ['assigned', 'Assigned'], ['mentioned', 'Mentions'], ['created', 'Created'], ['all', 'All'],
    ];
    const filterTabs = filterDefs.map(([val, label]) => {
      const t = el('button', {
        class: `gh-filter__tab${val === issueFilter ? ' is-active' : ''}`, text: label,
        attrs: { role: 'tab', 'aria-selected': String(val === issueFilter) },
      });
      t.dataset.filter = val;
      t.addEventListener('click', () => {
        if (issueFilter === val) return;
        issueFilter = val;
        for (const ft of filterTabs) {
          const a = ft.dataset.filter === issueFilter;
          ft.classList.toggle('is-active', a);
          ft.setAttribute('aria-selected', String(a));
        }
        loadIssues();
      });
      return t;
    });
    stateSelect = el('select', { class: 'select', attrs: { 'aria-label': 'Issue state' } }, [
      el('option', { text: 'Open', attrs: { value: 'open' } }),
      el('option', { text: 'Closed', attrs: { value: 'closed' } }),
    ]);
    stateSelect.value = issueState;
    stateSelect.addEventListener('change', () => { issueState = stateSelect.value; loadIssues(); });

    issueControls = el('div', { class: 'cluster' }, [
      el('div', { class: 'gh-filter', attrs: { role: 'tablist', 'aria-label': 'Issue filter' } }, filterTabs),
      el('span', { class: 'toolbar2__spacer' }),
      stateSelect,
    ]);

    // Notification controls (mark read)
    markReadBtn = el('button', {
      class: 'btn btn--ghost btn--sm', text: 'Mark read', attrs: { type: 'button' },
    });
    markReadBtn.addEventListener('click', markSelectedRead);
    notifControls = el('div', { class: 'cluster' }, [
      el('span', { class: 'toolbar2__spacer' }),
      markReadBtn,
    ]);

    panelTitle = el('span', { class: 'panel__title', text: 'Notifications' });
    listEl = el('div', { class: 'gh-list', attrs: { role: 'list' } });

    const listPane = el('div', { class: 'panel' }, [
      el('div', { class: 'panel__body stack stack--tight' }, [
        el('div', { class: 'cluster' }, [panelTitle]),
        headerRow,
        listEl,
      ]),
    ]);

    const detailMount = el('div');
    detail = createDetail(detailMount, { toast });
    const detailPane = el('div', { class: 'panel' }, [detailMount]);

    root.replaceChildren(el('div', { class: 'gh-layout' }, [listPane, detailPane]));
    mountControls(); // insert the active control row (notif/issue) before the list
    built = true;
  }

  // ---- Public API ----------------------------------------------------------
  function init(alertsController) {
    alerts = alertsController || null;
    if (!built) build();
    refreshOsToggle();
    // Keep the list fresh when the poller pulls new notifications.
    if (alerts) {
      alerts.setOnChange((items) => {
        lastNotifications = Array.isArray(items) ? items : lastNotifications;
        if (mode === 'notifications' && root.offsetParent !== null) {
          renderRows(lastNotifications, false);
        }
      });
    }
    reload();
  }

  // Open a notification's thread (used by shell when an OS alert is clicked).
  function openThread(note) {
    if (!built) build();
    setMode('notifications');
    openNotification(note);
  }

  return { init, openThread, reload };
}
