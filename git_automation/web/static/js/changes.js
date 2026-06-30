// changes.js — staged / unstaged / untracked file lists with per-file and bulk
// stage / unstage / discard actions, plus a commit message box. Renders the
// `.changes` component.
//
// Selection: click selects one file; Ctrl/Cmd-click toggles a file in/out of the
// selection; Shift-click selects a contiguous range (over the flat, rendered
// order). Right-clicking a file (or anywhere in the selection) opens a `.ctxmenu`
// (reusing contextmenu.js) with Stage / Unstage / Discard / Delete / Stash
// selected — every action applies to the whole current selection, so the user
// can stage just some of the changed files. Discard and Delete confirm first.

import * as api from './api.js';
import { el, toast, confirmDialog } from './ui.js';
import { createContextMenu } from './contextmenu.js';

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

// Inner-SVG markup for context-menu icons (wrapped by contextmenu.js).
const MENU_ICON = {
  stage: '<path d="M12 5v14M5 12h14"/>',
  unstage: '<path d="M5 12h14"/>',
  discard: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
  delete: '<path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/>',
  stash: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 9h18"/>',
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
  // Multi-selection across the three lists. Keys identify a row uniquely so the
  // same path can be selected in both the staged and unstaged sections.
  let selection = new Set(); // Set<key>
  let anchorKey = null; // range-selection anchor
  let flatRows = []; // rows in rendered order: { key, file, staged, untracked }
  let message = '';
  let busy = false;

  const contextMenu = createContextMenu();

  function rowKey(section, file) {
    return `${section} ${file}`;
  }

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

  function fileRow({ section, file, code, staged, untracked }) {
    const key = rowKey(section, file);
    const row = el('div', { class: 'changes__file', attrs: { role: 'button', tabindex: '0' } });
    if (selection.has(key)) row.classList.add('is-selected');
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

    row.addEventListener('click', (e) => handleRowClick(e, key, file, staged));
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        handleRowClick(e, key, file, staged);
      }
    });
    row.addEventListener('contextmenu', (e) => handleRowContext(e, key, file, staged));
    return row;
  }

  // ---- Selection ------------------------------------------------------------

  function handleRowClick(e, key, file, staged) {
    const index = flatRows.findIndex((r) => r.key === key);
    if (e.shiftKey && anchorKey != null) {
      const from = flatRows.findIndex((r) => r.key === anchorKey);
      if (from >= 0 && index >= 0) {
        const [lo, hi] = from <= index ? [from, index] : [index, from];
        selection = new Set(flatRows.slice(lo, hi + 1).map((r) => r.key));
      }
    } else if (e.ctrlKey || e.metaKey) {
      if (selection.has(key)) selection.delete(key);
      else selection.add(key);
      anchorKey = key;
    } else {
      selection = new Set([key]);
      anchorKey = key;
    }
    render();
    if (onSelectFile) onSelectFile(file, staged);
  }

  function handleRowContext(e, key, file, staged) {
    e.preventDefault();
    // Right-clicking outside the current selection narrows it to that one file.
    if (!selection.has(key)) {
      selection = new Set([key]);
      anchorKey = key;
      render();
      if (onSelectFile) onSelectFile(file, staged);
    }
    openMenu(e.clientX, e.clientY);
  }

  function selectedRows() {
    return flatRows.filter((r) => selection.has(r.key));
  }

  function openMenu(x, y) {
    const rows = selectedRows();
    if (rows.length === 0) return;
    const toStage = rows.filter((r) => !r.staged).map((r) => r.file);
    const toUnstage = rows.filter((r) => r.staged).map((r) => r.file);
    const allPaths = Array.from(new Set(rows.map((r) => r.file)));
    const suffix = allPaths.length > 1 ? ` (${allPaths.length})` : '';

    contextMenu.open(x, y, [
      {
        label: `Stage${suffix}`, icon: MENU_ICON.stage, disabled: toStage.length === 0,
        onSelect: () => doFiles(api.stage, toStage, { reload: true }),
      },
      {
        label: `Unstage${suffix}`, icon: MENU_ICON.unstage, disabled: toUnstage.length === 0,
        onSelect: () => doFiles(api.unstage, toUnstage, { reload: true }),
      },
      { sep: true },
      { label: `Discard${suffix}`, icon: MENU_ICON.discard, danger: true, onSelect: () => doDiscard(allPaths) },
      { label: `Delete${suffix}`, icon: MENU_ICON.delete, danger: true, onSelect: () => doDelete(allPaths) },
      { sep: true },
      { label: `Stash selected${suffix}`, icon: MENU_ICON.stash, onSelect: () => doStash(allPaths) },
    ]);
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

    // Rebuild the flat, rendered order used for range selection.
    flatRows = [
      ...stagedFiles.map((f) => ({ key: rowKey('S', f.path), file: f.path, staged: true, untracked: false })),
      ...unstagedFiles.map((f) => ({ key: rowKey('U', f.path), file: f.path, staged: false, untracked: false })),
      ...untrackedFiles.map((p) => ({ key: rowKey('T', p), file: p, staged: false, untracked: true })),
    ];

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
          render: (f) => fileRow({ section: 'S', file: f.path, code: f.status, staged: true }),
        }),
      );
      wrap.appendChild(
        section('Unstaged', unstagedFiles, {
          bulkLabel: 'Stage all',
          bulkAction: () => doFiles(api.stage, unstagedFiles.map((f) => f.path), { reload: true }),
          render: (f) => fileRow({ section: 'U', file: f.path, code: f.status, staged: false }),
        }),
      );
      wrap.appendChild(
        section('Untracked', untrackedFiles, {
          bulkLabel: 'Stage all',
          bulkAction: () => doFiles(api.stage, untrackedFiles.slice(), { reload: true }),
          render: (p) => fileRow({ section: 'T', file: p, staged: false, untracked: true }),
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

  function pruneSelection() {
    const live = new Set(flatRows.map((r) => r.key));
    for (const k of Array.from(selection)) if (!live.has(k)) selection.delete(k);
    if (anchorKey && !live.has(anchorKey)) anchorKey = null;
  }

  async function reloadSelf() {
    if (!path) return;
    try {
      data = await api.getChanges(path);
      render(); // rebuilds flatRows from the fresh data
      pruneSelection();
      render(); // reflect the pruned selection in the row styling
    } catch (err) {
      toast('error', 'Could not load changes', err.message);
    }
  }

  async function doFiles(fn, files, { reload, full } = {}) {
    if (!path || busy || files.length === 0) return;
    busy = true;
    try {
      const result = await fn(path, files);
      if (result && result.ok === false) {
        toast('error', 'Operation failed', result.output || 'git reported a failure.');
        return;
      }
      if (full && refreshAll) {
        await refreshAll();
      } else {
        if (reload) await reloadSelf();
        if (refreshStatus) await refreshStatus();
      }
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

  async function doDelete(files) {
    if (!path || busy || files.length === 0) return;
    const ok = await confirmDialog({
      title: files.length === 1 ? `Delete ${pathParts(files[0]).name}?` : `Delete ${files.length} files?`,
      message: 'This permanently removes the file(s) from the working tree. It cannot be undone.',
      confirmLabel: 'Delete',
      danger: true,
    });
    if (!ok) return;
    await doFiles(api.deleteFiles, files, { reload: true });
  }

  async function doStash(files) {
    if (!path || busy || files.length === 0) return;
    // Full refresh so the new stash shows up in the sidebar.
    await doFiles((p, f) => api.stashFiles(p, f, null), files, { full: true });
  }

  async function doCommit() {
    const msg = message.trim();
    if (!path || busy || !msg || (data.staged || []).length === 0) return;
    busy = true;
    try {
      await api.commit(path, msg);
      message = '';
      selection = new Set();
      anchorKey = null;
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
    selection = new Set();
    anchorKey = null;
    message = '';
    await reloadSelf();
  }

  function clear() {
    container.replaceChildren();
  }

  return { load, clear };
}
