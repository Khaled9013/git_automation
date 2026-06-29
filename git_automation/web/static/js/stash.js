// stash.js — stash operations (create / pop / apply / drop). Every mutating call
// honours the backend `{ok:false}` contract: a git-level failure returns HTTP 200
// with `{ok:false, output}`, so we surface that as an error toast rather than
// assuming success. Drop is destructive and confirms first. After any change we
// call `refresh()` so the sidebar/graph re-read state.

import * as api from './api.js';
import { toast, confirmDialog, promptDialog } from './ui.js';

/**
 * @param {{ getPath:()=>(string|null), refresh:()=>Promise<void>|void }} ctx
 */
export function initStash({ getPath, refresh } = {}) {
  let busy = false;

  async function run(label, call, { successTitle, successMsg }) {
    const path = getPath && getPath();
    if (!path || busy) return false;
    busy = true;
    try {
      const result = await call(path);
      if (result && result.ok === false) {
        toast('error', `${label} failed`, result.output || 'git reported a failure.');
        // eslint-disable-next-line no-console
        console.error(`${label} failed`, result.output);
        return false;
      }
      toast('success', successTitle, successMsg);
      if (refresh) await refresh();
      return true;
    } catch (err) {
      toast('error', `${label} failed`, err.message);
      // eslint-disable-next-line no-console
      console.error(`${label} failed`, err);
      return false;
    } finally {
      busy = false;
    }
  }

  async function create() {
    const path = getPath && getPath();
    if (!path) return false;
    const message = await promptDialog({
      title: 'Stash changes',
      label: 'Description (optional)',
      placeholder: 'WIP: …',
      confirmLabel: 'Stash',
    });
    // promptDialog resolves null on cancel; an empty description is allowed.
    if (message === null) return false;
    return run('Stash', (p) => api.stash(p, message || null), {
      successTitle: 'Changes stashed',
      successMsg: message || 'working tree saved',
    });
  }

  function pop(index = null) {
    return run('Stash pop', (p) => api.stashPop(p, index), {
      successTitle: 'Stash popped',
      successMsg: index == null ? 'most recent stash' : `stash@{${index}}`,
    });
  }

  function apply(index) {
    return run('Stash apply', (p) => api.stashApply(p, index), {
      successTitle: 'Stash applied',
      successMsg: `stash@{${index}}`,
    });
  }

  async function drop(index) {
    const ok = await confirmDialog({
      title: `Drop stash@{${index}}?`,
      message: 'This permanently deletes the stash entry. It cannot be undone.',
      confirmLabel: 'Drop',
      danger: true,
    });
    if (!ok) return false;
    return run('Stash drop', (p) => api.stashDrop(p, index), {
      successTitle: 'Stash dropped',
      successMsg: `stash@{${index}}`,
    });
  }

  /** Build `.ctxmenu` items for a stash entry (used by the sidebar right-click). */
  function menuItems(index) {
    return [
      { label: 'Pop', icon: '<path d="M12 19V8M6 13l6-6 6 6"/><path d="M5 5h14"/>', onSelect: () => pop(index) },
      { label: 'Apply', icon: '<path d="M20 6 9 17l-5-5"/>', onSelect: () => apply(index) },
      { sep: true },
      { label: 'Drop', danger: true, icon: '<path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/>', onSelect: () => drop(index) },
    ];
  }

  return { create, pop, apply, drop, menuItems };
}
