// diff.js — unified diff viewer. Fetches `/api/repo/diff` for a file and
// renders it with the `.diff` component (old/new gutters + line modifiers).

import * as api from './api.js';
import { el, toast } from './ui.js';

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

function diffLine({ kind, oldLn, newLn, code }) {
  const cls = kind === 'meta'
    ? 'diff__line diff__line--meta'
    : kind === 'add'
      ? 'diff__line diff__line--add'
      : kind === 'del'
        ? 'diff__line diff__line--del'
        : 'diff__line';
  return el('div', { class: cls, attrs: { role: 'row' } }, [
    el('span', { class: 'diff__ln', text: oldLn != null ? String(oldLn) : '' }),
    el('span', { class: 'diff__ln', text: newLn != null ? String(newLn) : '' }),
    el('span', { class: 'diff__code', text: code }),
  ]);
}

/**
 * Initialise the diff viewer bound to a container element.
 * @returns {{ show(path,file,staged):Promise<void>, clear():void }}
 */
export function initDiff(container) {
  function clear() {
    container.replaceChildren();
  }

  async function show(path, file, staged = false) {
    container.replaceChildren(
      el('div', { class: 'diff', attrs: { 'aria-busy': 'true' } }, [
        el('div', { class: 'diff__line diff__line--meta' }, [
          el('span', { class: 'diff__ln' }),
          el('span', { class: 'diff__ln' }),
          el('span', { class: 'diff__code', text: `Loading diff for ${file}…` }),
        ]),
      ]),
    );
    try {
      const res = await api.getDiff(path, file, staged);
      const wrap = el('div', { class: 'diff', attrs: { role: 'table', 'aria-label': `Diff of ${file}` } });
      if (res.binary) {
        wrap.appendChild(diffLine({ kind: 'meta', code: `Binary file ${file} — no textual diff.` }));
      } else if (!res.diff || !res.diff.trim()) {
        wrap.appendChild(diffLine({ kind: 'meta', code: `No changes to show for ${file}.` }));
      } else {
        for (const row of parseDiff(res.diff)) wrap.appendChild(diffLine(row));
      }
      container.replaceChildren(wrap);
    } catch (err) {
      clear();
      toast('error', 'Could not load diff', err.message);
    }
  }

  return { show, clear };
}
