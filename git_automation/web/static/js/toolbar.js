// toolbar.js — wires the top `.toolbar2` action bar. It owns the repo/branch
// selector labels, the Pull / Push / Branch / Stash / Pop actions, and the
// Terminal toggle. Undo/Redo are wired separately by undo.js (it manages their
// enabled state); the branch selector dropdown is wired by branches.js. This
// module just connects the remaining buttons to the handlers the app supplies.

/**
 * @param {HTMLElement} root the `.toolbar2` element
 * @param {{
 *   onOpenRepo?:()=>void, onPull?:()=>void, onPush?:()=>void,
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
  const btnBranch = $('tb-branch-create');
  const btnStash = $('tb-stash');
  const btnPop = $('tb-pop');
  const btnTerminal = $('tb-terminal');

  let terminalActive = false;

  function on(elm, fn) {
    if (elm && fn) elm.addEventListener('click', (e) => { e.preventDefault(); fn(); });
  }

  on(repoSelect, handlers.onOpenRepo);
  on(btnPull, handlers.onPull);
  on(btnPush, handlers.onPush);
  on(btnBranch, handlers.onBranch);
  on(btnStash, handlers.onStash);
  on(btnPop, handlers.onPop);

  if (btnTerminal) {
    btnTerminal.addEventListener('click', (e) => {
      e.preventDefault();
      terminalActive = !terminalActive;
      btnTerminal.classList.toggle('is-active', terminalActive);
      if (handlers.onToggleTerminal) handlers.onToggleTerminal(terminalActive);
    });
  }

  // Buttons that need an open repository (everything except Open repo).
  const repoButtons = [btnPull, btnPush, btnBranch, btnStash, btnPop, btnTerminal];

  return {
    setRepo(name) {
      if (repoLabel) repoLabel.textContent = name || 'Open repo…';
    },
    setBranch(name) {
      if (branchLabel) branchLabel.textContent = name || '—';
    },
    setEnabled(enabled) {
      for (const b of repoButtons) if (b) b.disabled = !enabled;
    },
    setTerminalActive(active) {
      terminalActive = !!active;
      if (btnTerminal) btnTerminal.classList.toggle('is-active', terminalActive);
    },
  };
}
