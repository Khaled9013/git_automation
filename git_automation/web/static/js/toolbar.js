// toolbar.js — wires the top `.toolbar2` action bar. It owns the repo/branch
// selector labels, the Pull / Push / Fetch / Branch / Stash / Pop actions, and
// the Terminal toggle. Push / Pull / Fetch target a chosen remote: a
// GitKraken-style split button where the action buttons act on the selected
// remote and the trailing caret (`#tb-remote`) opens a `.ctxmenu` to pick it.
// The choice defaults to the branch upstream → origin → first remote, is shown
// on the caret button, and is remembered per-repo. Undo/Redo are wired by
// undo.js; the branch selector dropdown by branches.js.

import { createContextMenu } from './contextmenu.js';

const REMOTE_KEY = 'git-automation:remote';
const REMOTE_ICON = '<circle cx="12" cy="12" r="3"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3"/>';

/**
 * @param {HTMLElement} root the `.toolbar2` element
 * @param {{
 *   onOpenRepo?:()=>void,
 *   onPull?:(remote:string)=>void, onPush?:(remote:string)=>void, onFetch?:(remote:string)=>void,
 *   onBranch?:()=>void, onStash?:()=>void, onPop?:()=>void,
 *   onToggleTerminal?:(active:boolean)=>void,
 * }} handlers
 */
export function initToolbar(root, handlers = {}) {
  const $ = (id) => root.querySelector(`#${id}`);

  const repoSelect = $('tb-repo');
  const repoLabel = $('tb-repo-label');
  const branchLabel = $('tb-branch-label');
  const btnPull = $('tb-pull');
  const btnPush = $('tb-push');
  const btnFetch = $('tb-fetch');
  const btnRemote = $('tb-remote');
  const remoteLabel = $('tb-remote-label');
  const btnBranch = $('tb-branch-create');
  const btnStash = $('tb-stash');
  const btnPop = $('tb-pop');
  const btnTerminal = $('tb-terminal');

  const remoteMenu = createContextMenu();

  let terminalActive = false;
  let path = null;
  let remotes = []; // [{ name, url }]
  let chosen = null; // chosen remote name

  // ---- Remote selection -----------------------------------------------------

  function storageKey() {
    return path ? `${REMOTE_KEY}:${path}` : null;
  }

  function rememberChoice(name) {
    const key = storageKey();
    if (key) {
      try { localStorage.setItem(key, name); } catch { /* ignore */ }
    }
  }

  function defaultRemote(status) {
    const names = remotes.map((r) => r.name);
    const key = storageKey();
    let remembered = null;
    if (key) {
      try { remembered = localStorage.getItem(key); } catch { /* ignore */ }
    }
    if (remembered && names.includes(remembered)) return remembered;
    const upstream = status && status.upstream && status.upstream.remote;
    if (upstream && names.includes(upstream)) return upstream;
    if (names.includes('origin')) return 'origin';
    return names[0] || null;
  }

  function renderRemoteLabel() {
    if (remoteLabel) remoteLabel.textContent = chosen || 'remote';
    if (btnRemote) {
      btnRemote.title = chosen ? `Remote: ${chosen} (click to change)` : 'No remote configured';
      // The caret is only useful when there is more than one remote to pick.
      btnRemote.hidden = remotes.length < 2;
    }
  }

  function chooseRemote(name) {
    chosen = name;
    rememberChoice(name);
    renderRemoteLabel();
  }

  function openRemoteMenu() {
    if (!btnRemote || remotes.length === 0) return;
    const rect = btnRemote.getBoundingClientRect();
    const items = remotes.map((r) => ({
      label: r.name,
      icon: REMOTE_ICON,
      shortcut: r.url || '',
      onSelect: () => chooseRemote(r.name),
    }));
    btnRemote.setAttribute('aria-expanded', 'true');
    remoteMenu.open(rect.left, rect.bottom + 4, items, {
      onClose: () => btnRemote.setAttribute('aria-expanded', 'false'),
    });
  }

  // Run an action against the chosen remote. With multiple remotes and no
  // resolvable choice yet, fall back to the picker so the user can decide.
  function runRemoteAction(fn) {
    if (!fn) return;
    if (!chosen && remotes.length > 1) {
      openRemoteMenu();
      return;
    }
    if (!chosen) return; // no remotes configured
    fn(chosen);
  }

  function on(elm, fn) {
    if (elm && fn) elm.addEventListener('click', (e) => { e.preventDefault(); fn(); });
  }

  on(repoSelect, handlers.onOpenRepo);
  on(btnBranch, handlers.onBranch);
  on(btnStash, handlers.onStash);
  on(btnPop, handlers.onPop);

  if (btnPull) btnPull.addEventListener('click', (e) => { e.preventDefault(); runRemoteAction(handlers.onPull); });
  if (btnPush) btnPush.addEventListener('click', (e) => { e.preventDefault(); runRemoteAction(handlers.onPush); });
  if (btnFetch) btnFetch.addEventListener('click', (e) => { e.preventDefault(); runRemoteAction(handlers.onFetch); });
  if (btnRemote) btnRemote.addEventListener('click', (e) => { e.preventDefault(); openRemoteMenu(); });

  if (btnTerminal) {
    btnTerminal.addEventListener('click', (e) => {
      e.preventDefault();
      terminalActive = !terminalActive;
      btnTerminal.classList.toggle('is-active', terminalActive);
      if (handlers.onToggleTerminal) handlers.onToggleTerminal(terminalActive);
    });
  }

  // Buttons that need an open repository (everything except Open repo).
  const repoButtons = [btnBranch, btnStash, btnPop, btnTerminal];
  // Remote-targeting actions are only usable when a remote is configured.
  const remoteButtons = [btnPull, btnPush, btnFetch, btnRemote];

  function syncRemoteButtons(repoOpen) {
    const usable = repoOpen && remotes.length > 0;
    for (const b of remoteButtons) if (b) b.disabled = !usable;
    if (btnRemote) btnRemote.hidden = remotes.length < 2;
  }

  return {
    setRepo(name) {
      if (repoLabel) repoLabel.textContent = name || 'Open repo…';
    },
    setBranch(name) {
      if (branchLabel) branchLabel.textContent = name || '—';
    },
    /** Update the remote list + default choice from the loaded repo status. */
    setRemoteContext(status) {
      path = (status && status.path) || null;
      remotes = (status && status.remotes) || [];
      chosen = defaultRemote(status);
      renderRemoteLabel();
      syncRemoteButtons(true);
    },
    setEnabled(enabled) {
      for (const b of repoButtons) if (b) b.disabled = !enabled;
      syncRemoteButtons(enabled);
    },
    setTerminalActive(active) {
      terminalActive = !!active;
      if (btnTerminal) btnTerminal.classList.toggle('is-active', terminalActive);
    },
  };
}
