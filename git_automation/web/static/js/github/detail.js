// github/detail.js — the .gh-detail pane: renders one issue (header, state badge,
// labels, body, the .gh-comment thread) plus a .gh-reply box that POSTs a comment
// and refreshes the thread. Markup/classes mirror styleguide.html exactly; all
// user content goes in via textContent (no HTML injection).

import { el, setLoading } from '../ui.js';
import * as api from './api.js';
import { relTimeAgo, initial } from './util.js';

const SVG_NS = 'http://www.w3.org/2000/svg';

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

/**
 * @param {HTMLElement} root  the detail-pane mount
 * @param {{ toast:(v,t,m?)=>void }} deps
 */
export function createDetail(root, { toast }) {
  let current = null; // { repo, number }

  function placeholder(msg = 'Select a notification or issue to view it here.') {
    root.replaceChildren(
      el('div', { class: 'gh-detail' }, [
        el('div', { class: 'gh-detail__body', text: msg }),
      ]),
    );
  }

  function render(issue) {
    const titleEl = el('div', { class: 'gh-detail__title', text: issue.title || '(untitled)' });
    titleEl.appendChild(document.createTextNode(' '));
    titleEl.appendChild(el('span', { class: 'gh-detail__num', text: `#${issue.number}` }));

    const labels = (issue.labels || []).map((name) =>
      el('span', { class: 'badge', text: name }),
    );

    const header = el('div', { class: 'gh-detail__header' }, [
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

    const body = el('div', {
      class: 'gh-detail__body',
      text: issue.body && issue.body.trim() ? issue.body : 'No description provided.',
    });

    const comments = (issue.comments || []).map(commentNode);

    // Reply box
    const textarea = el('textarea', {
      class: 'input',
      attrs: { id: 'gh-reply', rows: '3', placeholder: 'Leave a comment…', 'aria-label': 'Add a comment' },
    });
    const commentBtn = el('button', { class: 'btn btn--primary btn--sm', text: 'Comment' });
    const openIssueUrl = issue.url;
    const viewBtn = openIssueUrl
      ? el('a', { class: 'btn btn--ghost btn--sm', text: 'Open on GitHub', attrs: { href: openIssueUrl, target: '_blank', rel: 'noopener' } })
      : null;

    const reply = el('div', { class: 'gh-reply' }, [
      el('div', { class: 'field' }, [
        el('label', { class: 'field__label', text: 'Add a comment', attrs: { for: 'gh-reply' } }),
        textarea,
      ]),
      el('div', { class: 'gh-reply__actions' }, [viewBtn, commentBtn]),
    ]);

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
        await show({ repo: issue.repo, number: issue.number }); // refresh thread
      } catch (err) {
        toast('error', 'Could not post comment', err.message);
      } finally {
        setLoading(commentBtn, false);
      }
    }
    commentBtn.addEventListener('click', submit);
    textarea.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        submit();
      }
    });

    root.replaceChildren(
      el('div', { class: 'gh-detail' }, [header, body, ...comments, reply]),
    );
  }

  async function show({ repo, number }) {
    current = { repo, number };
    placeholder('Loading…');
    try {
      const issue = await api.getIssue(repo, number);
      // Guard against a slower request finishing after the user moved on.
      if (!current || current.repo !== repo || current.number !== number) return;
      render(issue);
    } catch (err) {
      toast('error', 'Could not load issue', err.message);
      placeholder(`Could not load ${repo} #${number}.`);
    }
  }

  placeholder();

  return { show, clear: () => { current = null; placeholder(); } };
}
