// undo.js — a small controller around the reflog-based undo/redo endpoints.
//
// These are BEST-EFFORT: they move HEAD along the reflog (`git reset --keep`),
// so they cannot reverse pushes or destructive operations. The UI labels the
// buttons accordingly. Undo availability is derived by peeking the reflog; redo
// is only offered after an undo within the session (the reflog can't reliably
// tell us a "forward" step exists otherwise).

import * as api from './api.js';
import { toast } from './ui.js';

const BEST_EFFORT_TITLE =
  'Best-effort (reflog) — cannot reverse pushes or destructive ops.';

/**
 * @param {{ getPath:()=>(string|null), refresh:()=>Promise<void>|void,
 *           undoBtn:HTMLElement, redoBtn:HTMLElement }} ctx
 */
export function initUndo({ getPath, refresh, undoBtn, redoBtn } = {}) {
  let busy = false;
  let redoDepth = 0; // how many undos are currently redoable

  if (undoBtn) undoBtn.title = BEST_EFFORT_TITLE;
  if (redoBtn) redoBtn.title = BEST_EFFORT_TITLE;

  function applyState({ canUndo }) {
    if (undoBtn) undoBtn.disabled = !canUndo;
    if (redoBtn) redoBtn.disabled = redoDepth <= 0;
  }

  /** Re-read the reflog to enable/disable Undo. */
  async function peek() {
    const path = getPath && getPath();
    if (!path) {
      applyState({ canUndo: false });
      return;
    }
    try {
      const entries = await api.getReflog(path, 5);
      applyState({ canUndo: Array.isArray(entries) && entries.length >= 2 });
    } catch {
      applyState({ canUndo: false });
    }
  }

  async function step(kind) {
    const path = getPath && getPath();
    if (!path || busy) return;
    busy = true;
    if (undoBtn) undoBtn.disabled = true;
    if (redoBtn) redoBtn.disabled = true;
    try {
      const call = kind === 'undo' ? api.undo : api.redo;
      const result = await call(path);
      if (result && result.ok === false) {
        toast('error', `${kind === 'undo' ? 'Undo' : 'Redo'} unavailable`, result.output || 'Nothing to do.');
        return;
      }
      if (kind === 'undo') redoDepth += 1;
      else redoDepth = Math.max(0, redoDepth - 1);
      const subject = (result && result.undone) || '';
      toast('success', kind === 'undo' ? 'Undone' : 'Redone', subject || BEST_EFFORT_TITLE);
      if (refresh) await refresh();
    } catch (err) {
      toast('error', `${kind === 'undo' ? 'Undo' : 'Redo'} failed`, err.message);
      // eslint-disable-next-line no-console
      console.error(`${kind} failed`, err);
    } finally {
      busy = false;
      await peek();
    }
  }

  if (undoBtn) undoBtn.addEventListener('click', () => step('undo'));
  if (redoBtn) redoBtn.addEventListener('click', () => step('redo'));

  return {
    undo: () => step('undo'),
    redo: () => step('redo'),
    refreshState: peek,
  };
}
