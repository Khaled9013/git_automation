// diff.js — diff viewer with three modes, one public entry point.
//
//   • Working-tree mode (default): fetches structured hunks via api.getHunks and
//     renders each hunk with a `.diff__hunk-head` carrying a `.diff__stage`
//     (unstaged view) or `.diff__unstage` (staged view) button. Clicking
//     stages/unstages that hunk's patch, then fires the `onChanged` refresh.
//   • Commit mode: read-only historical diff of a file in a commit via
//     api.getCommitDiff — no hunk buttons.
//   • Either renders inline (default) or side-by-side (`.diff--split`); the
//     preference is remembered in localStorage and applies to whatever is shown.
//
// Public entry point (kept backward-compatible with existing callers):
//   show(path, file, options)
//     options — boolean (legacy `staged`) OR
//       { staged?:boolean, onChanged?:()=>void, commit?:string }
//   clear()

import * as api from './api.js';
import { el, toast } from './ui.js';

const SPLIT_PREF_KEY = 'gitcockpit.diff.split';

const ICON_PLUS = '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>';
const ICON_MINUS = '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/></svg>';

/**
 * Parse a unified diff into rows: { kind, oldLn, newLn, code }.
 * kind ∈ 'meta' | 'add' | 'del' | 'ctx'.
 */
function parseDiff(text) {
  const rows = [];
  let oldLn = 0;
  let newLn = 0;

  // Drop a single trailing newline so it doesn't yield a phantom blank row.
  const lines = String(text).replace(/\n$/, '').split('\n');
  for (const raw of lines) {
    if (raw.startsWith('@@')) {
      // @@ -oldStart,oldCount +newStart,newCount @@ context
      const m = /@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(raw);
      if (m) {
        oldLn = parseInt(m[1], 10);
        newLn = parseInt(m[2], 10);
      }
      rows.push({ kind: 'meta', code: raw });
      continue;
    }
    if (
      raw.startsWith('diff ') ||
      raw.startsWith('index ') ||
      raw.startsWith('--- ') ||
      raw.startsWith('+++ ') ||
      raw.startsWith('old mode') ||
      raw.startsWith('new mode') ||
      raw.startsWith('similarity ') ||
      raw.startsWith('rename ') ||
      raw.startsWith('new file') ||
      raw.startsWith('deleted file') ||
      raw.startsWith('\\ No newline')
    ) {
      rows.push({ kind: 'meta', code: raw });
      continue;
    }
    if (raw.startsWith('+')) {
      rows.push({ kind: 'add', newLn, code: raw.slice(1) });
      newLn += 1;
      continue;
    }
    if (raw.startsWith('-')) {
      rows.push({ kind: 'del', oldLn, code: raw.slice(1) });
      oldLn += 1;
      continue;
    }
    // Context line (leading space) — or a blank in-hunk line.
    rows.push({ kind: 'ctx', oldLn, newLn, code: raw.startsWith(' ') ? raw.slice(1) : raw });
    oldLn += 1;
    newLn += 1;
  }
  return rows;
}

/** Turn one structured hunk ({header, lines:[{type,text}]}) into render rows. */
function hunkRows(hunk) {
  const m = /@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(hunk.header || '');
  let oldLn = m ? parseInt(m[1], 10) : 0;
  let newLn = m ? parseInt(m[2], 10) : 0;
  const rows = [];
  for (const ln of hunk.lines || []) {
    const t = ln.type;
    const code = ln.text != null ? ln.text : '';
    if (t === '+') {
      rows.push({ kind: 'add', newLn, code });
      newLn += 1;
    } else if (t === '-') {
      rows.push({ kind: 'del', oldLn, code });
      oldLn += 1;
    } else {
      rows.push({ kind: 'ctx', oldLn, newLn, code });
      oldLn += 1;
      newLn += 1;
    }
  }
  return rows;
}

function rowClass(kind) {
  if (kind === 'meta') return 'diff__line diff__line--meta';
  if (kind === 'add') return 'diff__line diff__line--add';
  if (kind === 'del') return 'diff__line diff__line--del';
  return 'diff__line';
}

/** Inline row: [oldLn] [newLn] [code]. */
function inlineLine({ kind, oldLn, newLn, code }) {
  return el('div', { class: rowClass(kind), attrs: { role: 'row' } }, [
    el('span', { class: 'diff__ln', text: oldLn != null ? String(oldLn) : '' }),
    el('span', { class: 'diff__ln', text: newLn != null ? String(newLn) : '' }),
    el('span', { class: 'diff__code', text: code }),
  ]);
}

/** Split row: [oldLn] [oldCode] [newLn] [newCode]. */
function splitLine({ kind, oldLn, newLn, code }) {
  const isAdd = kind === 'add';
  const isDel = kind === 'del';
  return el('div', { class: rowClass(kind), attrs: { role: 'row' } }, [
    el('span', { class: 'diff__ln', text: !isAdd && oldLn != null ? String(oldLn) : '' }),
    el('span', { class: 'diff__code', text: isAdd ? '' : code }),
    el('span', { class: 'diff__ln', text: !isDel && newLn != null ? String(newLn) : '' }),
    el('span', { class: 'diff__code', text: isDel ? '' : code }),
  ]);
}

function metaRow(text) {
  return inlineLine({ kind: 'meta', code: text });
}

/**
 * Initialise the diff viewer bound to a container element.
 * @returns {{ show(path:string, file:string, options?:boolean|object):Promise<void>, clear():void }}
 */
export function initDiff(container) {
  let split = readSplitPref();
  let current = null; // active request descriptor (identity used as a stale-guard)
  let view = null;    // last fetched + normalised data ({ type:'hunks'|'text', ... })

  function readSplitPref() {
    try {
      return localStorage.getItem(SPLIT_PREF_KEY) === '1';
    } catch {
      return false;
    }
  }
  function writeSplitPref(v) {
    try {
      localStorage.setItem(SPLIT_PREF_KEY, v ? '1' : '0');
    } catch {
      /* storage unavailable — non-fatal */
    }
  }

  function layoutToggle() {
    const btnCls = (active) => `btn btn--sm ${active ? 'btn--secondary' : 'btn--ghost'}`;
    const inlineBtn = el('button', {
      class: btnCls(!split),
      text: 'Inline',
      attrs: { type: 'button', title: 'Inline diff', 'aria-pressed': String(!split) },
    });
    const splitBtn = el('button', {
      class: btnCls(split),
      text: 'Split',
      attrs: { type: 'button', title: 'Side-by-side diff', 'aria-pressed': String(split) },
    });
    inlineBtn.addEventListener('click', () => setSplit(false));
    splitBtn.addEventListener('click', () => setSplit(true));
    return el('div', { class: 'cluster', attrs: { role: 'group', 'aria-label': 'Diff layout' } }, [
      inlineBtn,
      splitBtn,
    ]);
  }

  function setSplit(v) {
    if (v === split) return;
    split = v;
    writeSplitPref(v);
    if (view) renderView();
  }

  function lineRenderer() {
    return split ? splitLine : inlineLine;
  }

  function appendRows(wrap, rows) {
    const renderLine = lineRenderer();
    for (const row of rows) {
      if (row.kind === 'meta' && split) {
        // In split mode, hunk/file headers span the full width as a head row.
        wrap.appendChild(el('div', { class: 'diff__hunk-head' }, [
          el('span', { class: 'diff__code', text: row.code }),
        ]));
      } else {
        wrap.appendChild(renderLine(row));
      }
    }
  }

  function hunkHead(hunk) {
    const head = el('div', { class: 'diff__hunk-head' }, [
      el('span', { class: 'diff__code', text: hunk.header || '' }),
    ]);
    const staged = current && current.staged;
    const btn = el('button', {
      class: staged ? 'diff__unstage' : 'diff__stage',
      html: staged ? ICON_MINUS : ICON_PLUS,
      attrs: { type: 'button', title: staged ? 'Unstage this hunk' : 'Stage this hunk' },
    }, [document.createTextNode(staged ? ' Unstage' : ' Stage hunk')]);
    btn.addEventListener('click', () => applyHunk(hunk, btn));
    head.appendChild(btn);
    return head;
  }

  async function applyHunk(hunk, btn) {
    if (!current) return;
    const { path, file, staged, onChanged } = current;
    btn.disabled = true;
    try {
      const fn = staged ? api.unstageHunk : api.stageHunk;
      const res = await fn(path, file, hunk.patch);
      if (res && res.ok === false) {
        toast('error', staged ? 'Unstage hunk failed' : 'Stage hunk failed', res.output || 'git reported a failure.');
        btn.disabled = false;
        return;
      }
      if (onChanged) onChanged();
      // Refresh our own view too, so it stays correct regardless of onChanged.
      load();
    } catch (err) {
      toast('error', staged ? 'Unstage hunk failed' : 'Stage hunk failed', err.message);
      btn.disabled = false;
    }
  }

  function renderView() {
    if (!view) return;
    const fileName = view.file || (current && current.file) || '';
    const wrap = el('div', {
      class: split ? 'diff diff--split' : 'diff',
      attrs: { role: 'table', 'aria-label': `Diff of ${fileName}` },
    });

    if (view.binary) {
      wrap.appendChild(metaRow(`Binary file ${fileName} — no textual diff.`));
    } else if (view.type === 'hunks') {
      if (!view.hunks || view.hunks.length === 0) {
        wrap.appendChild(metaRow(`No changes to show for ${fileName}.`));
      } else {
        for (const hunk of view.hunks) {
          wrap.appendChild(hunkHead(hunk));
          appendRows(wrap, hunkRows(hunk));
        }
      }
    } else {
      // text (commit-file diff or any raw unified-diff string)
      if (!view.diff || !view.diff.trim()) {
        wrap.appendChild(metaRow(`No changes to show for ${fileName}.`));
      } else {
        appendRows(wrap, parseDiff(view.diff));
      }
    }

    container.replaceChildren(layoutToggle(), wrap);
  }

  async function load() {
    const req = current;
    if (!req) return;
    try {
      if (req.commit) {
        const res = await api.getCommitDiff(req.path, req.commit, req.file);
        if (req !== current) return; // a newer show() superseded this one
        view = { type: 'text', file: res.file || req.file, binary: !!res.binary, diff: res.diff || '' };
      } else {
        const res = await api.getHunks(req.path, req.file, req.staged);
        if (req !== current) return;
        view = { type: 'hunks', file: res.file || req.file, binary: !!res.binary, hunks: res.hunks || [] };
      }
      renderView();
    } catch (err) {
      if (req !== current) return;
      view = null;
      container.replaceChildren();
      toast('error', 'Could not load diff', err.message);
    }
  }

  function show(path, file, options) {
    if (!path || !file) {
      clear();
      return Promise.resolve();
    }
    const opts = typeof options === 'boolean' ? { staged: options } : (options || {});
    current = {
      path,
      file,
      staged: !!opts.staged,
      onChanged: typeof opts.onChanged === 'function' ? opts.onChanged : null,
      commit: opts.commit || null,
    };
    view = null;
    container.replaceChildren(
      layoutToggle(),
      el('div', { class: split ? 'diff diff--split' : 'diff', attrs: { 'aria-busy': 'true' } }, [
        metaRow(`Loading diff for ${file}…`),
      ]),
    );
    return load();
  }

  function clear() {
    current = null;
    view = null;
    container.replaceChildren();
  }

  return { show, clear };
}
