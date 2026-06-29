// mergeeditor.js — visual 3-way merge conflict editor (GitKraken-style).
//
// Renders an overlay panel into the host element (`#merge-editor-root`) that,
// for every conflicted file in an in-progress merge, shows three monospaced
// panes — OURS | RESULT | THEIRS. Conflict markers in the working copy
// (`<<<<<<<` / `|||||||` / `=======` / `>>>>>>>`) are parsed into hunks; each
// hunk can be resolved with Accept Ours / Accept Theirs / Accept Both, which
// rewrites the editable RESULT pane. Saving a file stages it via the backend;
// once every file is resolved the merge can be continued (or aborted at any
// time). Binary conflicts skip the text panes and offer keep-ours/keep-theirs.
//
// Public API:
//   createMergeEditor(rootEl) -> { open(path), close() }
//
// Uses only the documented `api.js` calls and the shared `ui.js` helpers; it
// invents no markup conventions beyond the `.merge-editor` design classes.

import * as api from './api.js';
import { el, toast, confirmDialog } from './ui.js';

// ---------------------------------------------------------------------------
// Conflict-marker parsing
// ---------------------------------------------------------------------------

const MARK_OURS = '<<<<<<<';
const MARK_BASE = '|||||||';
const MARK_SEP = '=======';
const MARK_THEIRS = '>>>>>>>';

/**
 * Split working-copy text with conflict markers into ordered segments.
 * Each segment is either:
 *   { type:'ctx',      lines:string[] }
 *   { type:'conflict', ours:string[], base:string[], theirs:string[],
 *                      oursLabel, theirsLabel }
 * Handles both 2-way (`<<< === >>>`) and diff3 (`<<< ||| === >>>`) styles.
 */
export function parseConflicts(text) {
  const lines = String(text == null ? '' : text).split('\n');
  const segments = [];
  let ctx = [];

  const flushCtx = () => {
    if (ctx.length) {
      segments.push({ type: 'ctx', lines: ctx });
      ctx = [];
    }
  };

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith(MARK_OURS)) {
      flushCtx();
      const oursLabel = line.slice(MARK_OURS.length).trim();
      i += 1;

      const ours = [];
      while (
        i < lines.length &&
        !lines[i].startsWith(MARK_BASE) &&
        !lines[i].startsWith(MARK_SEP)
      ) {
        ours.push(lines[i]);
        i += 1;
      }

      const base = [];
      if (i < lines.length && lines[i].startsWith(MARK_BASE)) {
        i += 1; // skip the base marker
        while (i < lines.length && !lines[i].startsWith(MARK_SEP)) {
          base.push(lines[i]);
          i += 1;
        }
      }

      // At the `=======` separator (or EOF for a malformed block).
      if (i < lines.length && lines[i].startsWith(MARK_SEP)) i += 1;

      const theirs = [];
      while (i < lines.length && !lines[i].startsWith(MARK_THEIRS)) {
        theirs.push(lines[i]);
        i += 1;
      }

      let theirsLabel = '';
      if (i < lines.length && lines[i].startsWith(MARK_THEIRS)) {
        theirsLabel = lines[i].slice(MARK_THEIRS.length).trim();
        i += 1;
      }

      segments.push({ type: 'conflict', ours, base, theirs, oursLabel, theirsLabel });
    } else {
      ctx.push(line);
      i += 1;
    }
  }
  flushCtx();
  return segments;
}

/** Number of conflict hunks in a parsed segment list. */
function countConflicts(segments) {
  return segments.reduce((n, s) => n + (s.type === 'conflict' ? 1 : 0), 0);
}

/**
 * Rebuild RESULT text from the segments and the per-hunk `choices` array
 * (`'ours' | 'theirs' | 'both' | null`). Unresolved hunks keep their markers
 * so the file round-trips faithfully until the user picks a side.
 */
export function buildResult(segments, choices) {
  const out = [];
  let ci = 0;
  for (const seg of segments) {
    if (seg.type === 'ctx') {
      for (const l of seg.lines) out.push(l);
      continue;
    }
    const choice = choices[ci];
    ci += 1;
    if (choice === 'ours') {
      for (const l of seg.ours) out.push(l);
    } else if (choice === 'theirs') {
      for (const l of seg.theirs) out.push(l);
    } else if (choice === 'both') {
      for (const l of seg.ours) out.push(l);
      for (const l of seg.theirs) out.push(l);
    } else {
      out.push(`${MARK_OURS} ${seg.oursLabel || 'ours'}`.trimEnd());
      for (const l of seg.ours) out.push(l);
      out.push(MARK_SEP);
      for (const l of seg.theirs) out.push(l);
      out.push(`${MARK_THEIRS} ${seg.theirsLabel || 'theirs'}`.trimEnd());
    }
  }
  return out.join('\n');
}

// ---------------------------------------------------------------------------
// Small DOM helpers
// ---------------------------------------------------------------------------

function btn(cls, text, onClick, attrs) {
  const b = el('button', { class: cls, text, attrs: { type: 'button', ...(attrs || {}) } });
  if (onClick) b.addEventListener('click', onClick);
  return b;
}

function codeLine(modifier, lnText, code) {
  return el('div', { class: `merge-editor__line merge-editor__line--${modifier}` }, [
    el('span', { class: 'merge-editor__ln', text: lnText }),
    el('span', { class: 'merge-editor__code', text: code }),
  ]);
}

// ---------------------------------------------------------------------------
// Editor
// ---------------------------------------------------------------------------

/**
 * Create a merge-conflict editor bound to a host element.
 * @param {HTMLElement} rootEl  container the overlay renders into.
 * @returns {{ open(path:string):Promise<void>, close():void }}
 */
export function createMergeEditor(rootEl) {
  if (!rootEl) throw new Error('createMergeEditor: rootEl is required');

  // Per-session state.
  let state = null;
  // state = {
  //   path, message,
  //   files: [{ file, binary, segments, choices, resolved, resultEl }],
  //   active, // index
  //   continueBtn, fileTabsEl, panesHostEl,
  // }

  function reset() {
    rootEl.replaceChildren();
    rootEl.classList.remove('is-open');
    state = null;
  }

  function close() {
    reset();
  }

  // --- resolution bookkeeping ----------------------------------------------

  function allResolved() {
    return state.files.length > 0 && state.files.every((f) => f.resolved);
  }

  function refreshContinueState() {
    if (!state || !state.continueBtn) return;
    state.continueBtn.disabled = !allResolved();
  }

  // --- file tabs ------------------------------------------------------------

  function renderTabs() {
    const host = state.fileTabsEl;
    host.replaceChildren();
    if (state.files.length <= 1) {
      host.style.display = 'none';
      return;
    }
    host.style.display = 'flex';
    state.files.forEach((f, idx) => {
      const active = idx === state.active;
      const label = `${f.resolved ? '✓ ' : ''}${f.file}`;
      const tab = btn(
        `btn btn--sm ${active ? 'btn--secondary' : 'btn--ghost'}`,
        label,
        () => {
          if (state.active === idx) return;
          state.active = idx;
          renderTabs();
          renderActiveFile();
        },
      );
      if (f.resolved) tab.classList.add('is-resolved');
      host.appendChild(tab);
    });
  }

  // --- active-file rendering ------------------------------------------------

  function renderActiveFile() {
    const host = state.panesHostEl;
    host.replaceChildren();
    const f = state.files[state.active];
    if (!f) return;
    if (f.binary) {
      host.appendChild(renderBinaryPane(f));
    } else {
      host.appendChild(renderTextPanes(f));
    }
  }

  function renderBinaryPane(f) {
    const wrap = el('div', { class: 'merge-editor__panes', attrs: { style: 'grid-template-columns:1fr' } });
    const pane = el('div', { class: 'merge-editor__pane merge-editor__pane--result' });
    pane.appendChild(
      el('div', { class: 'merge-editor__pane-head' }, [el('span', { text: 'Binary file' })]),
    );
    pane.appendChild(
      el('div', { class: 'merge-editor__hunk-head', text: `${f.file} is binary — choose a whole side to keep.` }),
    );
    const controls = el('div', { attrs: { style: 'display:flex;gap:var(--space-2);padding:var(--space-3)' } }, [
      btn('merge-editor__accept merge-editor__accept--ours', 'Keep ours', () => saveBinary(f, 'ours')),
      btn('merge-editor__accept merge-editor__accept--theirs', 'Keep theirs', () => saveBinary(f, 'theirs')),
    ]);
    pane.appendChild(controls);
    if (f.resolved) {
      pane.appendChild(
        el('div', { class: 'merge-editor__hunk-head', attrs: { style: 'color:var(--success);background:var(--success-subtle)' }, text: 'Resolved & staged.' }),
      );
    }
    wrap.appendChild(pane);
    return wrap;
  }

  function renderTextPanes(f) {
    const total = countConflicts(f.segments);
    const panes = el('div', { class: 'merge-editor__panes' });

    // ---- OURS pane (left) --------------------------------------------------
    const oursPane = el('div', { class: 'merge-editor__pane merge-editor__pane--ours' });
    oursPane.appendChild(
      el('div', { class: 'merge-editor__pane-head' }, [
        el('span', { text: `Ours${labelSuffix(f, 'ours')}` }),
        btn('merge-editor__accept merge-editor__accept--ours', 'Use ours', () => acceptAll(f, 'ours')),
      ]),
    );

    // ---- THEIRS pane (right) ----------------------------------------------
    const theirsPane = el('div', { class: 'merge-editor__pane merge-editor__pane--theirs' });
    theirsPane.appendChild(
      el('div', { class: 'merge-editor__pane-head' }, [
        el('span', { text: `Theirs${labelSuffix(f, 'theirs')}` }),
        btn('merge-editor__accept merge-editor__accept--theirs', 'Use theirs', () => acceptAll(f, 'theirs')),
      ]),
    );

    // Walk segments, emitting context + per-hunk heads/lines into both panes.
    let oursLn = 0;
    let theirsLn = 0;
    let hunk = 0;
    for (const seg of f.segments) {
      if (seg.type === 'ctx') {
        for (const line of seg.lines) {
          oursLn += 1;
          theirsLn += 1;
          oursPane.appendChild(codeLine('ctx', String(oursLn), line));
          theirsPane.appendChild(codeLine('ctx', String(theirsLn), line));
        }
        continue;
      }

      const idx = hunk;
      hunk += 1;
      const choice = f.choices[idx];
      const chosen = choice ? ` · ${choice}` : '';
      const headText = `Conflict ${idx + 1} of ${total}${chosen}`;

      // OURS hunk head carries the per-hunk Ours + Both controls.
      const oursHead = el('div', { class: 'merge-editor__hunk-head' }, [
        el('span', { text: headText }),
        el('span', { attrs: { style: 'margin-left:auto;display:inline-flex;gap:var(--space-2)' } }, [
          btn('merge-editor__accept merge-editor__accept--ours', 'Ours', () => acceptHunk(f, idx, 'ours')),
          btn('merge-editor__accept merge-editor__accept--both', 'Both', () => acceptHunk(f, idx, 'both')),
        ]),
      ]);
      oursPane.appendChild(oursHead);
      for (const line of seg.ours) {
        oursLn += 1;
        oursPane.appendChild(codeLine('add', String(oursLn), line));
      }

      // THEIRS hunk head carries the per-hunk Theirs control.
      const theirsHead = el('div', { class: 'merge-editor__hunk-head' }, [
        el('span', { text: headText }),
        el('span', { attrs: { style: 'margin-left:auto' } }, [
          btn('merge-editor__accept merge-editor__accept--theirs', 'Theirs', () => acceptHunk(f, idx, 'theirs')),
        ]),
      ]);
      theirsPane.appendChild(theirsHead);
      for (const line of seg.theirs) {
        theirsLn += 1;
        theirsPane.appendChild(codeLine('del', String(theirsLn), line));
      }
    }

    // ---- RESULT pane (centre, editable) -----------------------------------
    const resultPane = el('div', { class: 'merge-editor__pane merge-editor__pane--result' });
    const unresolvedHunks = f.choices.filter((c) => !c).length;
    const resultHeadLabel =
      total === 0
        ? 'No conflicts'
        : unresolvedHunks === 0
          ? 'All hunks resolved'
          : `Pick a side · ${unresolvedHunks} of ${total} unresolved`;
    resultPane.appendChild(
      el('div', { class: 'merge-editor__pane-head' }, [
        el('span', { text: 'Result' }),
        btn('merge-editor__accept merge-editor__accept--both', 'Accept both', () => acceptAll(f, 'both')),
        btn('btn btn--primary btn--sm', 'Save & stage', () => saveText(f), { style: 'margin-left:var(--space-2)' }),
      ]),
    );
    resultPane.appendChild(el('div', { class: 'merge-editor__hunk-head', text: resultHeadLabel }));

    const ta = el('textarea', {
      class: 'merge-editor__result-input',
      attrs: {
        spellcheck: 'false',
        'aria-label': `Merged result for ${f.file}`,
        style:
          'flex:1;min-height:0;width:100%;resize:none;border:0;outline:0;padding:var(--space-2) var(--space-3);' +
          'font-family:var(--font-mono);font-size:var(--text-sm);line-height:var(--leading-relaxed);' +
          'color:var(--text-primary);background:var(--bg-inset);white-space:pre;tab-size:4',
      },
    });
    ta.value = f.resultText;
    // Keep saved content in sync with manual edits.
    ta.addEventListener('input', () => {
      f.resultText = ta.value;
    });
    f.resultEl = ta;
    resultPane.appendChild(ta);

    // Order: OURS | RESULT | THEIRS (matches the design grid).
    panes.appendChild(oursPane);
    panes.appendChild(resultPane);
    panes.appendChild(theirsPane);
    return panes;
  }

  function labelSuffix(f, side) {
    // Derive a branch-ish label from the first conflict hunk if present.
    const seg = f.segments.find((s) => s.type === 'conflict');
    if (!seg) return '';
    const lbl = side === 'ours' ? seg.oursLabel : seg.theirsLabel;
    return lbl ? ` · ${lbl}` : '';
  }

  // --- accept actions -------------------------------------------------------

  function recomputeResult(f) {
    f.resultText = buildResult(f.segments, f.choices);
    if (f.resultEl) f.resultEl.value = f.resultText;
  }

  function acceptHunk(f, idx, side) {
    f.choices[idx] = side;
    recomputeResult(f);
    renderActiveFile();
  }

  function acceptAll(f, side) {
    f.choices = f.choices.map(() => side);
    recomputeResult(f);
    renderActiveFile();
  }

  // --- save / continue / abort ---------------------------------------------

  async function saveText(f) {
    const content = f.resultEl ? f.resultEl.value : f.resultText;
    if (content.includes(MARK_OURS) || content.includes(MARK_THEIRS)) {
      const ok = await confirmDialog({
        title: 'Unresolved conflict markers',
        message: `${f.file} still contains conflict markers. Stage it anyway?`,
        confirmLabel: 'Stage anyway',
        danger: true,
      });
      if (!ok) return;
    }
    await doResolve(f, content);
  }

  async function saveBinary(f, side) {
    const conflict = f.conflict || {};
    const content = side === 'ours' ? conflict.ours : conflict.theirs;
    if (content == null) {
      toast('error', 'Cannot keep side', `No "${side}" version is available for ${f.file}.`);
      return;
    }
    await doResolve(f, content);
  }

  async function doResolve(f, content) {
    try {
      const res = await api.resolveConflict(state.path, f.file, content);
      if (res && res.ok === false) {
        toast('error', 'Could not stage', res.output || `Failed to resolve ${f.file}.`);
        return;
      }
      f.resolved = true;
      f.resultText = content;
      toast('success', 'Resolved & staged', f.file);
      renderTabs();
      renderActiveFile();
      refreshContinueState();
    } catch (err) {
      toast('error', 'Could not stage', err.message);
    }
  }

  async function doContinue() {
    if (!allResolved()) {
      toast('info', 'Resolve all files first', 'Some conflicts are still unstaged.');
      return;
    }
    try {
      const res = await api.mergeContinue(state.path);
      if (res && res.ok === false) {
        toast('error', 'Could not continue merge', res.output || 'Merge still has conflicts.');
        return;
      }
      toast('success', 'Merge completed', res && res.output ? res.output : '');
      close();
    } catch (err) {
      toast('error', 'Could not continue merge', err.message);
    }
  }

  async function doAbort() {
    const ok = await confirmDialog({
      title: 'Abort merge?',
      message: 'This restores your branch to its pre-merge state. Resolved staging will be discarded.',
      confirmLabel: 'Abort merge',
      danger: true,
    });
    if (!ok) return;
    try {
      const res = await api.mergeAbort(state.path);
      if (res && res.ok === false) {
        toast('error', 'Could not abort merge', res.output || 'Abort failed.');
        return;
      }
      toast('success', 'Merge aborted', res && res.output ? res.output : '');
      close();
    } catch (err) {
      toast('error', 'Could not abort merge', err.message);
    }
  }

  // --- shell ----------------------------------------------------------------

  function renderShell() {
    rootEl.replaceChildren();
    rootEl.classList.add('is-open');

    const fileCount = state.files.length;
    const conflictWord = fileCount === 1 ? 'conflict' : 'conflicts';

    const continueBtn = btn('btn btn--primary btn--sm', 'Continue merge', () => doContinue());
    continueBtn.disabled = true;
    state.continueBtn = continueBtn;

    const head = el('div', { class: 'merge-editor__head' }, [
      el('span', { text: 'Resolving merge' }),
      el('span', { class: 'badge badge--danger', text: `${fileCount} ${conflictWord}` }),
      el('span', { class: 'merge-editor__actions' }, [
        btn('btn btn--ghost btn--sm', 'Abort merge', () => doAbort()),
        continueBtn,
        btn('btn btn--icon btn--sm', '✕', () => close(), { 'aria-label': 'Close merge editor' }),
      ]),
    ]);

    const fileTabsEl = el('div', {
      attrs: {
        style:
          'display:flex;gap:var(--space-2);flex-wrap:wrap;padding:var(--space-2) var(--space-3);' +
          'border-bottom:1px solid var(--border);background:var(--surface)',
      },
    });
    state.fileTabsEl = fileTabsEl;

    const panesHostEl = el('div', {
      attrs: { style: 'flex:1;min-height:0;display:flex;flex-direction:column;overflow:hidden' },
    });
    state.panesHostEl = panesHostEl;

    const panel = el('div', {
      class: 'merge-editor',
      attrs: { style: 'width:min(1400px,96vw);height:90vh' },
    }, [head, fileTabsEl, panesHostEl]);

    const overlay = el('div', {
      class: 'modal-overlay is-open',
      attrs: { role: 'presentation', style: 'padding:var(--space-4)' },
    }, [panel]);

    rootEl.appendChild(overlay);

    renderTabs();
    renderActiveFile();
    refreshContinueState();
  }

  function renderEmpty(messageText) {
    rootEl.replaceChildren();
    rootEl.classList.add('is-open');
    const panel = el('div', { class: 'merge-editor', attrs: { style: 'width:min(560px,92vw)' } }, [
      el('div', { class: 'merge-editor__head' }, [
        el('span', { text: 'Merge editor' }),
        el('span', { class: 'merge-editor__actions' }, [
          btn('btn btn--icon btn--sm', '✕', () => close(), { 'aria-label': 'Close merge editor' }),
        ]),
      ]),
      el('div', { class: 'merge-editor__hunk-head', text: messageText }),
    ]);
    const overlay = el('div', {
      class: 'modal-overlay is-open',
      attrs: { role: 'presentation', style: 'padding:var(--space-4)' },
    }, [panel]);
    rootEl.appendChild(overlay);
  }

  // --- open -----------------------------------------------------------------

  async function open(path) {
    if (!path) {
      toast('error', 'Cannot open merge editor', 'No repository path was provided.');
      return;
    }
    // Loading shell.
    renderEmpty('Loading conflicts…');

    let status;
    try {
      status = await api.getMergeStatus(path);
    } catch (err) {
      toast('error', 'Could not load merge status', err.message);
      close();
      return;
    }

    const conflicts = (status && status.conflicts) || [];
    if (!status || (!status.merging && conflicts.length === 0)) {
      renderEmpty('No merge in progress — nothing to resolve.');
      return;
    }
    if (conflicts.length === 0) {
      // Merging but all stages clean — let the user finish.
      state = { path, message: status.message || '', files: [], active: 0 };
      renderEmpty('All conflicts resolved. You can continue the merge.');
      return;
    }

    // Fetch every conflicted file's stages.
    let conflictData;
    try {
      conflictData = await Promise.all(
        conflicts.map((file) =>
          api.getConflict(path, file).then((c) => ({ file, c })),
        ),
      );
    } catch (err) {
      toast('error', 'Could not load conflicts', err.message);
      close();
      return;
    }

    const files = conflictData.map(({ file, c }) => {
      const conflict = c || {};
      const binary = !!conflict.binary;
      const segments = binary ? [] : parseConflicts(conflict.merged);
      const choices = new Array(countConflicts(segments)).fill(null);
      return {
        file,
        conflict,
        binary,
        segments,
        choices,
        resolved: false,
        resultText: binary ? '' : buildResult(segments, choices),
        resultEl: null,
      };
    });

    state = {
      path,
      message: (status && status.message) || '',
      files,
      active: 0,
      continueBtn: null,
      fileTabsEl: null,
      panesHostEl: null,
    };

    renderShell();
  }

  return { open, close };
}
