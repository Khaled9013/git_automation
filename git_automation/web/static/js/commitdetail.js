// commitdetail.js — the right-pane commit inspector. Single-clicking a commit in
// the graph calls `show(path, sha)`, which loads GET /api/repo/commit and renders
// the design-system `.commit-detail` block (subject / sha / author / date / body)
// plus a `.file-list` of changed files. Clicking a file emits `onSelectFile` so
// the app can render its diff (reusing diff.js).

import * as api from './api.js';
import { el, toast } from './ui.js';

const STATUS_CLASS = {
  A: 'file-list__status--add',
  D: 'file-list__status--del',
  M: 'file-list__status--mod',
  R: 'file-list__status--ren',
  C: 'file-list__status--ren',
};
const STATUS_TITLE = {
  A: 'Added', D: 'Deleted', M: 'Modified', R: 'Renamed', C: 'Copied',
};

function pathParts(p) {
  const idx = p.lastIndexOf('/');
  return idx >= 0 ? { dir: p.slice(0, idx + 1), name: p.slice(idx + 1) } : { dir: '', name: p };
}

/**
 * Initialise the commit-detail panel.
 * @param {HTMLElement} root
 * @param {{ onSelectFile?:(file:string)=>void }} cbs
 * @returns {{ show(path:string, sha:string):Promise<void>, clear():void }}
 */
export function initCommitDetail(root, { onSelectFile } = {}) {
  let selectedRow = null;

  function metaRow(label, value, mono = false) {
    return el('div', { class: 'commit-detail__row' }, [
      el('span', { class: 'commit-detail__label', text: label }),
      el('span', mono ? { class: 'mono', text: value } : { text: value }),
    ]);
  }

  function fileRow(f) {
    const code = (f.status || 'M').charAt(0).toUpperCase();
    const row = el('div', { class: 'file-list__row', attrs: { role: 'button', tabindex: '0', title: f.path } });
    row.appendChild(el('span', {
      class: `file-list__status ${STATUS_CLASS[code] || 'file-list__status--mod'}`,
      text: code,
      attrs: { title: STATUS_TITLE[code] || 'Changed' },
    }));
    const { dir, name } = pathParts(f.path);
    const pathSpan = el('span', { class: 'file-list__path' });
    if (dir) pathSpan.appendChild(el('span', { class: 'dir', text: dir }));
    pathSpan.appendChild(document.createTextNode(name));
    row.appendChild(pathSpan);
    row.appendChild(el('span', { class: 'file-list__counts' }, [
      el('span', { class: 'file-list__add', text: `+${f.additions ?? 0}` }),
      el('span', { class: 'file-list__del', text: `−${f.deletions ?? 0}` }),
    ]));

    const pick = () => {
      if (selectedRow) selectedRow.classList.remove('is-selected');
      selectedRow = row;
      row.classList.add('is-selected');
      if (onSelectFile) onSelectFile(f.path);
    };
    row.addEventListener('click', pick);
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        pick();
      }
    });
    return row;
  }

  function render(c) {
    selectedRow = null;
    const wrap = el('div', { class: 'commit-detail' });

    const header = el('div', { class: 'commit-detail__header' }, [
      el('div', { class: 'commit-detail__subject', text: c.subject || '(no subject)' }),
      el('span', { class: 'commit-detail__sha', text: c.short || (c.sha || '').slice(0, 7) }),
    ]);
    const meta = el('div', { class: 'commit-detail__meta' });
    const who = c.email ? `${c.author} <${c.email}>` : c.author || '';
    if (who) meta.appendChild(metaRow('Author', who));
    if (c.date) meta.appendChild(metaRow('Date', c.date));
    if (c.parents && c.parents.length) {
      meta.appendChild(metaRow('Parents', c.parents.map((p) => p.slice(0, 7)).join(' '), true));
    }
    header.appendChild(meta);
    wrap.appendChild(header);

    if (c.body && c.body.trim()) {
      wrap.appendChild(el('div', { class: 'commit-detail__body', text: c.body.trim() }));
    }

    const files = c.files || [];
    const list = el('div', { class: 'file-list' });
    if (files.length === 0) {
      list.appendChild(el('div', { class: 'text-muted', text: 'No file changes.' }));
    } else {
      for (const f of files) list.appendChild(fileRow(f));
    }
    wrap.appendChild(list);

    root.replaceChildren(wrap);
  }

  async function show(path, sha) {
    if (!path || !sha) return;
    root.replaceChildren(el('div', { class: 'text-muted', text: 'Loading commit…' }));
    try {
      const detail = await api.getCommit(path, sha);
      render(detail);
    } catch (err) {
      toast('error', 'Could not load commit', err.message);
      root.replaceChildren(el('div', { class: 'text-muted', text: 'Could not load commit.' }));
    }
  }

  function clear() {
    selectedRow = null;
    root.replaceChildren();
  }

  return { show, clear };
}
