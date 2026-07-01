// github/detail.js — the .gh-detail pane: renders one issue (header, state badge,
// labels, body, the .gh-comment thread) plus a .gh-reply box that POSTs a comment.
// Markup/classes mirror styleguide.html exactly; all user content goes in via
// textContent (no HTML injection).
//
// The pane is a *live thread*: while an issue is open it polls on an interval and
// reconciles — updating the header/body in place and APPENDING new comments —
// instead of reloading. Existing DOM (and the reply textarea + its contents) is
// preserved, and we only auto-scroll to a new comment when the reader is already
// near the bottom (standard chat behaviour). This is what makes new comments
// appear without a manual refresh.

import { el, setLoading } from '../ui.js';
import * as api from './api.js';
import { relTimeAgo, initial } from './util.js';

const SVG_NS = 'http://www.w3.org/2000/svg';
// Live-refresh cadence for the open thread. Human comment cadence is slow, so a
// tight interval just wastes network/CPU; 45s is plenty for a live thread.
const POLL_MS = 45000;

function stateBadge(state) {
  const open = String(state).toLowerCase() === 'open';
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', '11');
  svg.setAttribute('height', '11');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2.5');
  svg.setAttribute('stroke-linecap', 'round');
  const c = document.createElementNS(SVG_NS, 'circle');
  c.setAttribute('cx', '12'); c.setAttribute('cy', '12'); c.setAttribute('r', '9');
  const p = document.createElementNS(SVG_NS, 'path');
  p.setAttribute('d', 'M9 12h6');
  svg.append(c, p);
  const badge = el('span', { class: `badge ${open ? 'badge--success' : ''}`.trim() });
  badge.appendChild(svg);
  badge.appendChild(document.createTextNode(open ? 'Open' : 'Closed'));
  return badge;
}

function commentNode(c) {
  return el('div', { class: 'gh-comment' }, [
    el('span', { class: 'gh-comment__avatar', text: initial(c.author) }),
    el('div', { class: 'gh-comment__main' }, [
      el('div', { class: 'gh-comment__head' }, [
        el('span', { class: 'gh-comment__author', text: c.author || 'unknown' }),
        el('span', { class: 'gh-comment__time', text: relTimeAgo(c.created_at) }),
      ]),
      el('div', { class: 'gh-comment__body', text: c.body || '' }),
    ]),
  ]);
}

/** Stable identity for a comment (GitHub id when present; else time+author). */
function commentKey(c) {
  return c && c.id != null ? `c${c.id}` : `t${c && c.created_at}|${c && c.author}`;
}

/** Nearest scrollable ancestor (for the "am I at the bottom?" check). */
function scrollParent(node) {
  let e = node ? node.parentElement : null;
  while (e && e !== document.body) {
    const oy = getComputedStyle(e).overflowY;
    if ((oy === 'auto' || oy === 'scroll') && e.scrollHeight > e.clientHeight) return e;
    e = e.parentElement;
  }
  return document.scrollingElement || document.documentElement;
}

/**
 * @param {HTMLElement} root  the detail-pane mount
 * @param {{ toast:(v,t,m?)=>void }} deps
 */
export function createDetail(root, { toast }) {
  let current = null;  // { repo, number } the user intends to view
  let view = null;     // live render state: { repo, number, sig, els, commentEls: Map }
  let pollTimer = null;

  // While the tab is backgrounded there is no point running the poll at all —
  // the fetch would be wasted work and browsers throttle timers anyway. We
  // register a single `visibilitychange` listener (guarded so `show()`/`clear()`
  // churn can't stack duplicates) that resumes the poll — with an immediate
  // catch-up refresh — the moment the tab is shown again.
  let visListenerAdded = false;

  function onVisibilityChange() {
    if (document.visibilityState === 'hidden') {
      // Pause: kill the timer so nothing fires while backgrounded. `current`
      // is left intact so we know what to resume.
      stopPoll();
    } else if (current) {
      // Became visible again with a thread open — resume polling and do an
      // immediate refresh so the reader isn't left staring at a stale thread.
      startPoll();
      refresh();
    }
  }
  function addVisListener() {
    if (visListenerAdded) return;
    document.addEventListener('visibilitychange', onVisibilityChange);
    visListenerAdded = true;
  }
  function removeVisListener() {
    if (!visListenerAdded) return;
    document.removeEventListener('visibilitychange', onVisibilityChange);
    visListenerAdded = false;
  }

  function stopPoll() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }
  function startPoll() {
    stopPoll();
    // Don't spin up the timer while the tab is hidden — `onVisibilityChange`
    // will (re)start it when the tab is shown again.
    if (document.hidden) return;
    pollTimer = setInterval(() => {
      // Secondary guards: the tab may have been hidden between ticks, and the
      // pane itself may be hidden (another in-app view is showing). Either way,
      // skip the fetch; the timer resumes real work on the next visible tick.
      if (document.hidden || root.offsetParent === null) return;
      refresh();
    }, POLL_MS);
  }

  function placeholder(msg = 'Select a notification or issue to view it here.') {
    view = null;
    root.replaceChildren(
      el('div', { class: 'gh-detail' }, [
        el('div', { class: 'gh-detail__body', text: msg }),
      ]),
    );
  }

  // A fingerprint of the parts of the header/body that can change over time.
  function issueSig(issue) {
    return [
      issue.title || '',
      issue.state || '',
      (issue.labels || []).join(''),
      issue.body || '',
    ].join('');
  }

  function buildHeader(issue) {
    const titleEl = el('div', { class: 'gh-detail__title', text: issue.title || '(untitled)' });
    titleEl.appendChild(document.createTextNode(' '));
    titleEl.appendChild(el('span', { class: 'gh-detail__num', text: `#${issue.number}` }));
    const labels = (issue.labels || []).map((name) => el('span', { class: 'badge', text: name }));
    return el('div', { class: 'gh-detail__header' }, [
      titleEl,
      el('div', { class: 'gh-detail__meta' }, [
        stateBadge(issue.state),
        el('span', { class: 'gh-detail__author' }, [
          document.createTextNode('opened by '),
          el('span', { class: 'mono', text: `@${issue.author || 'unknown'}` }),
        ]),
      ]),
      labels.length ? el('div', { class: 'gh-detail__labels' }, labels) : null,
    ]);
  }

  function buildBody(issue) {
    return el('div', {
      class: 'gh-detail__body',
      text: issue.body && issue.body.trim() ? issue.body : 'No description provided.',
    });
  }

  function buildReply(issue) {
    const textarea = el('textarea', {
      class: 'input',
      attrs: { id: 'gh-reply', rows: '3', placeholder: 'Leave a comment…', 'aria-label': 'Add a comment' },
    });
    const commentBtn = el('button', { class: 'btn btn--primary btn--sm', text: 'Comment' });
    const viewBtn = issue.url
      ? el('a', { class: 'btn btn--ghost btn--sm', text: 'Open on GitHub', attrs: { href: issue.url, target: '_blank', rel: 'noopener' } })
      : null;

    async function submit() {
      const text = textarea.value.trim();
      if (!text) {
        toast('info', 'Nothing to send', 'Write a comment first.');
        textarea.focus();
        return;
      }
      setLoading(commentBtn, true);
      try {
        await api.addComment(issue.repo, issue.number, text);
        textarea.value = '';
        toast('success', 'Comment posted', `${issue.repo} #${issue.number}`);
        await refresh(); // reconcile — the new comment appears without a reload
      } catch (err) {
        toast('error', 'Could not post comment', err.message);
      } finally {
        setLoading(commentBtn, false);
      }
    }
    commentBtn.addEventListener('click', submit);
    textarea.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); submit(); }
    });

    return el('div', { class: 'gh-reply' }, [
      el('div', { class: 'field' }, [
        el('label', { class: 'field__label', text: 'Add a comment', attrs: { for: 'gh-reply' } }),
        textarea,
      ]),
      el('div', { class: 'gh-reply__actions' }, [viewBtn, commentBtn]),
    ]);
  }

  // First paint for an issue: build the whole structure and remember the pieces
  // so later refreshes can mutate them in place.
  function fullRender(issue) {
    const header = buildHeader(issue);
    const body = buildBody(issue);
    const comments = el('div', { class: 'gh-detail__comments' });
    const reply = buildReply(issue);

    const commentEls = new Map();
    for (const c of issue.comments || []) {
      const node = commentNode(c);
      commentEls.set(commentKey(c), { node, body: c.body || '' });
      comments.appendChild(node);
    }

    root.replaceChildren(el('div', { class: 'gh-detail' }, [header, body, comments, reply]));
    view = {
      repo: issue.repo,
      number: issue.number,
      sig: issueSig(issue),
      els: { header, body, comments, reply },
      commentEls,
    };
  }

  // In-place update: refresh header/body if they changed and append (or update)
  // comments, keeping existing nodes, the reply textarea, and scroll position.
  function reconcile(issue) {
    if (!view) { fullRender(issue); return; }

    const sig = issueSig(issue);
    if (sig !== view.sig) {
      const header = buildHeader(issue);
      const body = buildBody(issue);
      view.els.header.replaceWith(header);
      view.els.body.replaceWith(body);
      view.els.header = header;
      view.els.body = body;
      view.sig = sig;
    }

    const sc = scrollParent(view.els.comments);
    const nearBottom = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 120;

    let appended = false;
    for (const c of issue.comments || []) {
      const key = commentKey(c);
      const existing = view.commentEls.get(key);
      if (!existing) {
        const node = commentNode(c);
        node.classList.add('gh-comment--enter'); // subtle fade-in
        view.commentEls.set(key, { node, body: c.body || '' });
        view.els.comments.appendChild(node);
        appended = true;
      } else if (existing.body !== (c.body || '')) {
        // An edited comment: swap just its body text.
        const bodyEl = existing.node.querySelector('.gh-comment__body');
        if (bodyEl) bodyEl.textContent = c.body || '';
        existing.body = c.body || '';
      }
    }

    if (appended && nearBottom) sc.scrollTop = sc.scrollHeight;
  }

  /**
   * Show an issue in the detail pane.
   * @param {{repo:string, number:number}} target
   * @param {{silent?:boolean}} [opts]  When `silent`, refresh without flashing
   *   the "Loading…" state or surfacing errors (used by the live poll).
   */
  async function show({ repo, number }, { silent = false } = {}) {
    const same = view && view.repo === repo && view.number === number;
    current = { repo, number };
    if (!same && !silent) placeholder('Loading…');
    try {
      const issue = await api.getIssue(repo, number);
      // Drop a response the user has already navigated away from.
      if (!current || current.repo !== repo || current.number !== number) return;
      if (view && view.repo === repo && view.number === number) reconcile(issue);
      else fullRender(issue);
      startPoll();
      addVisListener(); // resume/pause the poll as the tab is shown/hidden
    } catch (err) {
      if (silent) return; // a background refresh failing stays invisible
      toast('error', 'Could not load issue', err.message);
      placeholder(`Could not load ${repo} #${number}.`);
    }
  }

  // Re-fetch the currently-open issue and reconcile (used by the poll + on post).
  async function refresh() {
    if (!current) return;
    await show(current, { silent: true });
  }

  placeholder();

  return {
    show,
    refresh,
    clear: () => { current = null; stopPoll(); removeVisListener(); placeholder(); },
  };
}
