// changes.js — staged / unstaged / untracked file lists with per-file and bulk
// stage / unstage / discard actions, plus a commit message box. Renders the
// `.changes` component; discard is confirmed before it runs.

import * as api from './api.js';
import { el, toast, confirmDialog } from './ui.js';

const STATUS_CLASS = {
  A: 'changes__status--add',
  D: 'changes__status--del',
  M: 'changes__status--mod',
  R: 'changes__status--ren',
  C: 'changes__status--ren',
  U: 'changes__status--conflict',
};
const STATUS_TITLE = {
  A: 'Added', D: 'Deleted', M: 'Modified', R: 'Renamed', C: 'Copied', U: 'Conflict', '?': 'Untracked',
};

function pathParts(p) {
  const idx = p.lastIndexOf('/');
  return idx >= 0 ? { dir: p.slice(0, idx + 1), name: p.slice(idx + 1) } : { dir: '', name: p };
}

/**
 * Initialise the changes/commit panel.
 * @param {HTMLElement} container
 * @param {{ onSelectFile:(file:string,staged:boolean)=>void,
 *           refreshAll:()=>Promise<void>, refreshStatus:()=>Promise<void> }} cbs
 */
export function initChanges(container, { onSelectFile, refreshAll, refreshStatus } = {}) {
  let path = null;
  let branch = null;
  let data = { staged: [], unstaged: [], untracked: [] };
  let selected = null; // { file, staged }
  let message = '';
  let busy = false;

  function statusGlyph(code) {
    const c = (code || '').charAt(0).toUpperCase();
    const span = el('span', {
      class: `changes__status ${STATUS_CLASS[c] || 'changes__status--mod'}`,
      text: c,
      attrs: { title: STATUS_TITLE[c] || 'Changed' },
    });
    return span;
  }

  function untrackedGlyph() {
    return el('span', {
      class: 'changes__status changes__status--untracked',
      text: '?',
      attrs: { title: 'Untracked' },
    });
  }

  function pathLabel(p) {
    const { dir, name } = pathParts(p);
    const span = el('span', { class: 'changes__path' });
    if (dir) span.appendChild(el('span', { class: 'dir', text: dir }));
    span.appendChild(document.createTextNode(name));
    span.title = p;
    return span;
  }

  function actionBtn(label, variant, handler) {
    const b = el('button', { class: `btn ${variant} btn--sm`, text: label, attrs: { type: 'button' } });
    b.addEventListener('click', (e) => {
      e.stopPropagation();
      handler();
    });
    return b;
  }

  function fileRow({ file, code, staged, untracked }) {
    const row = el('div', { class: 'changes__file', attrs: { role: 'button', tabindex: '0' } });
    if (selected && selected.file === file && selected.staged === staged) {
      row.classList.add('is-selected');
    }
    row.appendChild(untracked ? untrackedGlyph() : statusGlyph(code));
    row.appendChild(pathLabel(file));

    const actions = el('div', { class: 'changes__actions' });
    if (staged) {
      actions.appendChild(actionBtn('Unstage', 'btn--ghost', () => doFiles(api.unstage, [file], { reload: true })));
    } else {
      actions.appendChild(actionBtn('Discard', 'btn--ghost', () => doDiscard([file])));
      actions.appendChild(actionBtn('Stage', 'btn--secondary', () => doFiles(api.stage, [file], { reload: true })));
    }
    row.appendChild(actions);

    const select = () => {
      selected = { file, staged };
      render();
      if (onSelectFile) onSelectFile(file, staged);
    };
    row.addEventListener('click', select);
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        select();
      }
    });
    return row;
  }

  function section(title, files, { bulkLabel, bulkAction, render: renderRow }) {
    const head = el('div', { class: 'changes__head' }, [
      el('span', { class: 'changes__title', text: title }),
      el('span', { class: 'changes__count', text: String(files.length) }),
    ]);
    if (files.length > 0 && bulkLabel) {
      const actions = el('div', { class: 'changes__actions' }, [
        actionBtn(bulkLabel, 'btn--ghost', bulkAction),
      ]);
      head.appendChild(actions);
    }
    const sec = el('div', { class: 'changes__section' }, [head]);
    for (const f of files) sec.appendChild(renderRow(f));
    return sec;
  }

  function render() {
    container.replaceChildren();
    const stagedFiles = data.staged || [];
    const unstagedFiles = data.unstaged || [];
    const untrackedFiles = data.untracked || [];
    const nothing =
      stagedFiles.length === 0 && unstagedFiles.length === 0 && untrackedFiles.length === 0;

    const wrap = el('div', { class: 'changes' });

    if (nothing) {
      wrap.appendChild(
        el('div', { class: 'changes__section' }, [
          el('div', { class: 'changes__head' }, [
            el('span', { class: 'changes__title text-muted', text: 'No changes — working tree clean' }),
          ]),
        ]),
      );
    } else {
      wrap.appendChild(
        section('Staged', stagedFiles, {
          bulkLabel: 'Unstage all',
          bulkAction: () => doFiles(api.unstage, stagedFiles.map((f) => f.path), { reload: true }),
          render: (f) => fileRow({ file: f.path, code: f.status, staged: true }),
        }),
      );
      wrap.appendChild(
        section('Unstaged', unstagedFiles, {
          bulkLabel: 'Stage all',
          bulkAction: () => doFiles(api.stage, unstagedFiles.map((f) => f.path), { reload: true }),
          render: (f) => fileRow({ file: f.path, code: f.status, staged: false }),
        }),
      );
      wrap.appendChild(
        section('Untracked', untrackedFiles, {
          bulkLabel: 'Stage all',
          bulkAction: () => doFiles(api.stage, untrackedFiles.slice(), { reload: true }),
          render: (p) => fileRow({ file: p, staged: false, untracked: true }),
        }),
      );
    }

    wrap.appendChild(el('hr', { class: 'divider' }));
    wrap.appendChild(commitBox(stagedFiles.length));
    container.appendChild(wrap);
  }

  function commitBox(stagedCount) {
    const textarea = el('textarea', {
      class: 'input',
      attrs: { rows: '3', placeholder: 'Commit message — summary on the first line', 'aria-label': 'Commit message' },
    });
    textarea.value = message;
    textarea.addEventListener('input', () => {
      message = textarea.value;
      btn.disabled = !canCommit();
    });

    const btn = el('button', { class: 'btn btn--primary', attrs: { type: 'button' } });
    btn.appendChild(document.createTextNode('Commit'));
    if (branch) {
      btn.appendChild(document.createTextNode(' to '));
      btn.appendChild(el('span', { class: 'mono', text: branch }));
    }
    btn.disabled = !canCommit();
    btn.addEventListener('click', doCommit);

    function canCommit() {
      return !busy && message.trim().length > 0 && stagedCount > 0;
    }

    return el('div', { class: 'commit-box' }, [
      textarea,
      el('div', { class: 'commit-box__actions' }, [
        el('span', { class: 'text-muted', text: `${stagedCount} file${stagedCount === 1 ? '' : 's'} staged` }),
        btn,
      ]),
    ]);
  }

  // ---- Operations -----------------------------------------------------------

  async function reloadSelf() {
    if (!path) return;
    try {
      data = await api.getChanges(path);
      // Drop selection if the file no longer exists in either list.
      if (selected) {
        const all = [
          ...(data.staged || []).map((f) => f.path),
          ...(data.unstaged || []).map((f) => f.path),
          ...(data.untracked || []),
        ];
        if (!all.includes(selected.file)) selected = null;
      }
      render();
    } catch (err) {
      toast('error', 'Could not load changes', err.message);
    }
  }

  async function doFiles(fn, files, { reload } = {}) {
    if (!path || busy || files.length === 0) return;
    busy = true;
    try {
      await fn(path, files);
      if (reload) await reloadSelf();
      if (refreshStatus) await refreshStatus();
    } catch (err) {
      toast('error', 'Operation failed', err.message);
    } finally {
      busy = false;
    }
  }

  async function doDiscard(files) {
    if (!path || busy || files.length === 0) return;
    const ok = await confirmDialog({
      title: files.length === 1 ? `Discard changes to ${pathParts(files[0]).name}?` : `Discard changes to ${files.length} files?`,
      message: 'This permanently reverts the working-tree changes. It cannot be undone.',
      confirmLabel: 'Discard',
      danger: true,
    });
    if (!ok) return;
    await doFiles(api.discard, files, { reload: true });
  }

  async function doCommit() {
    const msg = message.trim();
    if (!path || busy || !msg || (data.staged || []).length === 0) return;
    busy = true;
    try {
      await api.commit(path, msg);
      message = '';
      selected = null;
      toast('success', 'Commit created', msg.split('\n')[0]);
      if (refreshAll) await refreshAll();
      else await reloadSelf();
    } catch (err) {
      toast('error', 'Commit failed', err.message);
    } finally {
      busy = false;
    }
  }

  // ---- Public API -----------------------------------------------------------

  async function load(repoPath, branchName) {
    path = repoPath;
    branch = branchName || null;
    selected = null;
    message = '';
    await reloadSelf();
  }

  function clear() {
    container.replaceChildren();
  }

  return { load, clear };
}
