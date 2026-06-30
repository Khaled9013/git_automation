// app.js — entry point for the GitKraken-style 3-pane workspace. It boots every
// module (sidebar, graph, commit detail, changes, toolbar, undo/redo, stash,
// context menu), wires the click / double-click / right-click interaction
// contract, and lazily imports the externally-owned merge editor and terminal so
// a missing file never breaks initial load. First-run onboarding is preserved.

import * as api from './api.js';
import { toast, confirmDialog, promptDialog } from './ui.js';
import { initRepo } from './repo.js';
import { initOnboarding } from './onboarding.js';
import { initFsBrowser } from './fsbrowser.js';
import { initChanges } from './changes.js';
import { initBranches } from './branches.js';
import { initDiff } from './diff.js';
import { createGraph } from './graph.js';
import { initSidebar } from './sidebar.js';
import { initCommitDetail } from './commitdetail.js';
import { initToolbar } from './toolbar.js';
import { initUndo } from './undo.js';
import { initStash } from './stash.js';
import { createContextMenu } from './contextmenu.js';

function basename(p) {
  const trimmed = String(p || '').replace(/\/+$/, '');
  const idx = trimmed.lastIndexOf('/');
  return idx >= 0 ? trimmed.slice(idx + 1) || trimmed : trimmed;
}

// Inner-SVG markup for context-menu icons (wrapped by contextmenu.js).
const ICON = {
  checkout: '<path d="m9 18 6-6-6-6"/>',
  merge: '<path d="M6 3v12"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>',
  reset: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
  cherry: '<circle cx="12" cy="12" r="3"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3"/>',
  pr: '<path d="M7 17 17 7M7 7h10v10"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
  branch: '<circle cx="4" cy="4" r="1.6"/><circle cx="4" cy="12" r="1.6"/><circle cx="12" cy="6" r="1.6"/><path d="M4 5.6v4.8M5.6 4H9a3 3 0 0 1 3 3v.4"/>',
  rename: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
  trash: '<path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/>',
};

function boot() {
  const $ = (id) => document.getElementById(id);

  // --- Panels & modules ------------------------------------------------------
  const diff = initDiff($('detail-diff'));
  const contextMenu = createContextMenu();

  const commitDetailRoot = $('commit-detail-root');
  const changesRoot = $('changes-root');

  let repoRef = null;
  const getPath = () => (repoRef ? repoRef.currentPath() : null);
  const refresh = () => (repoRef ? repoRef.refreshAll() : Promise.resolve());
  const refreshStatus = () => (repoRef ? repoRef.refreshStatus() : Promise.resolve());

  const commitDetail = initCommitDetail(commitDetailRoot, {
    onSelectFile: (file, sha) => {
      const p = getPath();
      if (p) diff.show(p, file, { commit: sha });
    },
  });

  const changes = initChanges(changesRoot, {
    onSelectFile: (file, staged) => {
      const p = getPath();
      if (p) diff.show(p, file, { staged, onChanged: refresh });
    },
    refreshAll: refresh,
    refreshStatus,
  });

  // Switch the detail pane between a selected commit and the working tree (WIP).
  let detailMode = 'commit';
  function setDetailMode(mode) {
    const wip = mode === 'wip';
    detailMode = wip ? 'wip' : 'commit';
    commitDetailRoot.hidden = wip;
    changesRoot.hidden = !wip;
    diff.clear();
  }

  // --- Conflict + lazy modules ----------------------------------------------

  async function openMergeEditor() {
    const p = getPath();
    if (!p) return;
    try {
      const { createMergeEditor } = await import('./mergeeditor.js');
      createMergeEditor($('merge-editor-root')).open(p);
    } catch (err) {
      toast('info', 'Merge conflicts', 'Resolve the conflicts to continue (merge editor unavailable).');
      // eslint-disable-next-line no-console
      console.error('merge editor unavailable', err);
    }
  }

  async function checkConflicts() {
    const p = getPath();
    if (!p) return false;
    try {
      const st = await api.getMergeStatus(p);
      if (st && Array.isArray(st.conflicts) && st.conflicts.length) {
        await openMergeEditor();
        return true;
      }
    } catch {
      /* non-fatal */
    }
    return false;
  }

  // --- Mutating-op helper (honours {ok:false}) -------------------------------

  async function run(label, callFn, { successTitle, successMsg, checkMerge = false } = {}) {
    const p = getPath();
    if (!p) return false;
    try {
      const result = await callFn(p);
      if (result && result.ok === false) {
        toast('error', `${label} failed`, result.output || 'git reported a failure.');
        // eslint-disable-next-line no-console
        console.error(`${label} failed`, result.output);
        await refresh();
        if (checkMerge) await checkConflicts();
        return false;
      }
      if (successTitle) toast('success', successTitle, successMsg || '');
      await refresh();
      if (checkMerge) await checkConflicts();
      return result || true;
    } catch (err) {
      toast('error', `${label} failed`, err.message);
      // eslint-disable-next-line no-console
      console.error(`${label} failed`, err);
      return false;
    }
  }

  // --- Operations ------------------------------------------------------------

  async function doCheckout(ref, { detach = false } = {}) {
    const status = repoRef && repoRef.status();
    const dirty = status && status.dirty;
    if (detach || dirty) {
      const ok = await confirmDialog({
        title: detach ? 'Checkout this commit?' : `Switch to ${ref}?`,
        message: [
          detach ? 'This detaches HEAD (you will not be on a branch).' : '',
          dirty ? 'You have uncommitted changes that may be affected.' : '',
        ].filter(Boolean).join(' '),
        confirmLabel: 'Checkout',
        danger: !!dirty,
      });
      if (!ok) return;
    }
    await run('Checkout', (p) => api.checkout(p, ref), {
      successTitle: 'Checked out', successMsg: ref,
    });
  }

  async function doMerge(ref) {
    const p = getPath();
    if (!p) return;
    try {
      const result = await api.mergeBranch(p, ref);
      if (result && result.conflicted) {
        toast('info', 'Merge has conflicts', `${(result.conflicts || []).length} file(s) to resolve.`);
        await refresh();
        await openMergeEditor();
        return;
      }
      if (result && result.ok === false) {
        toast('error', 'Merge failed', result.output || 'git reported a failure.');
        // eslint-disable-next-line no-console
        console.error('Merge failed', result.output);
        await refresh();
        return;
      }
      toast('success', 'Merged', ref);
      await refresh();
    } catch (err) {
      toast('error', 'Merge failed', err.message);
      // eslint-disable-next-line no-console
      console.error('Merge failed', err);
    }
  }

  async function doReset(sha, mode) {
    const ok = await confirmDialog({
      title: `Reset (${mode}) to ${sha.slice(0, 7)}?`,
      message: mode === 'hard'
        ? 'Hard reset DISCARDS all uncommitted changes and moves the branch. This cannot be undone.'
        : `Moves HEAD to this commit (${mode === 'soft' ? 'keeps index + working tree' : 'keeps working tree, unstages'}).`,
      confirmLabel: `Reset ${mode}`,
      danger: mode === 'hard',
    });
    if (!ok) return;
    await run('Reset', (p) => api.reset(p, sha, mode), {
      successTitle: 'Reset complete', successMsg: `${mode} → ${sha.slice(0, 7)}`,
    });
  }

  function doCherryPick(sha) {
    return run('Cherry-pick', (p) => api.cherryPick(p, sha), {
      successTitle: 'Cherry-picked', successMsg: sha.slice(0, 7), checkMerge: true,
    });
  }

  async function doCreateBranch() {
    const name = await promptDialog({
      title: 'Create branch',
      label: 'New branch name',
      placeholder: 'feature/my-change',
      confirmLabel: 'Create & switch',
    });
    if (!name) return;
    await run('Create branch', (p) => api.branchCreate(p, name, true), {
      successTitle: 'Branch created', successMsg: name,
    });
  }

  async function doDeleteBranch(name) {
    const ok = await confirmDialog({
      title: `Delete branch ${name}?`,
      message: 'This permanently deletes the local branch.',
      confirmLabel: 'Delete', danger: true,
    });
    if (!ok) return;
    try {
      const result = await api.branchDelete(getPath(), name, false);
      if (result && result.ok === false) throw new Error(result.output || 'not fully merged');
      toast('success', 'Branch deleted', name);
      await refresh();
    } catch (err) {
      const force = await confirmDialog({
        title: `Force delete ${name}?`,
        message: `${err.message}\n\nForce-delete anyway? Unmerged work will be lost.`,
        confirmLabel: 'Force delete', danger: true,
      });
      if (!force) return;
      await run('Delete branch', (p) => api.branchDelete(p, name, true), {
        successTitle: 'Branch force-deleted', successMsg: name,
      });
    }
  }

  async function doRenameBranch(name) {
    const newName = await promptDialog({
      title: `Rename ${name}`,
      label: 'New branch name',
      placeholder: name,
      confirmLabel: 'Rename',
    });
    if (!newName || newName === name) return;
    await run('Rename branch', (p) => api.branchRename(p, name, newName), {
      successTitle: 'Branch renamed', successMsg: `${name} → ${newName}`,
    });
  }

  // Double-clicking a remote branch creates + checks out a local tracking branch.
  function doTrackRemote(remoteRef) {
    return run('Track remote', (p) => api.branchTrack(p, remoteRef), {
      successTitle: 'Tracking branch created', successMsg: remoteRef,
    });
  }

  async function openPrFlow({ head = null } = {}) {
    const p = getPath();
    if (!p) return;
    const title = await promptDialog({
      title: 'Create pull request',
      label: head ? `Title (head: ${head})` : 'Title',
      placeholder: 'Add the graph label column',
      confirmLabel: 'Create PR',
    });
    if (!title) return;
    try {
      const pr = await api.prCreate(p, title, null, null, head);
      toast('success', `PR #${pr.number} created`, pr.url || '');
    } catch (err) {
      toast('error', 'Could not create PR', err.message);
      // eslint-disable-next-line no-console
      console.error('PR create failed', err);
    }
  }

  // --- Remote operations (push / pull / fetch against a chosen remote) -------

  function doFetch(remote) {
    return run('Fetch', (p) => api.gitFetch(p, remote), {
      successTitle: 'Fetch complete', successMsg: remote,
    });
  }

  async function doPull(remote) {
    const status = repoRef && repoRef.status();
    const branch = status && status.current_branch ? status.current_branch : null;
    await run('Pull', (p) => api.gitPull(p, remote, branch), {
      successTitle: 'Pull complete', successMsg: remote,
    });
    await checkConflicts();
  }

  function doPush(remote) {
    const status = repoRef && repoRef.status();
    const branch = status && status.current_branch ? status.current_branch : null;
    const setUpstream = !(status && status.upstream);
    return run('Push', (p) => api.gitPush(p, remote, branch, setUpstream), {
      successTitle: 'Push complete', successMsg: remote,
    });
  }

  async function copySha(sha) {
    try {
      await navigator.clipboard.writeText(sha);
      toast('success', 'Copied SHA', sha.slice(0, 12));
    } catch {
      toast('info', 'SHA', sha);
    }
  }

  // --- Context menus ---------------------------------------------------------

  function commitMenuItems(commit, branchNames) {
    const sha = commit.sha;
    return [
      { label: 'Checkout this commit', icon: ICON.checkout, onSelect: () => doCheckout(sha, { detach: true }) },
      { label: 'Merge into current', icon: ICON.merge, onSelect: () => doMerge(sha) },
      { sep: true },
      {
        label: 'Reset to here', icon: ICON.reset,
        submenu: [
          { label: 'Soft', shortcut: 'keep all', onSelect: () => doReset(sha, 'soft') },
          { label: 'Mixed', shortcut: 'unstage', onSelect: () => doReset(sha, 'mixed') },
          { label: 'Hard', shortcut: 'discard', danger: true, onSelect: () => doReset(sha, 'hard') },
        ],
      },
      { label: 'Cherry-pick', icon: ICON.cherry, onSelect: () => doCherryPick(sha) },
      { label: 'Create pull request…', icon: ICON.pr, onSelect: () => openPrFlow({ head: branchNames && branchNames[0] }) },
      { sep: true },
      { label: 'Copy SHA', icon: ICON.copy, shortcut: '⌘C', onSelect: () => copySha(sha) },
    ];
  }

  function branchMenuItems(info) {
    const checkoutName = info.kind === 'remote' ? info.local : info.name;
    const items = [
      { label: 'Checkout', icon: ICON.checkout, disabled: !!info.is_current, onSelect: () => doCheckout(checkoutName) },
      { label: 'Merge into current', icon: ICON.merge, disabled: !!info.is_current, onSelect: () => doMerge(info.name) },
      { label: 'Rename', icon: ICON.rename, disabled: info.kind === 'remote', onSelect: () => doRenameBranch(info.name) },
      { label: 'Delete', icon: ICON.trash, danger: true, disabled: !!info.is_current || info.kind === 'remote', onSelect: () => doDeleteBranch(info.name) },
      { sep: true },
      { label: 'Create pull request…', icon: ICON.pr, onSelect: () => openPrFlow({ head: checkoutName }) },
    ];
    return items;
  }

  // --- Graph -----------------------------------------------------------------
  const graph = createGraph($('commit-graph'), {
    onSelect: (commit) => {
      setDetailMode('commit');
      const p = getPath();
      if (p) commitDetail.show(p, commit.sha);
    },
    onWip: () => setDetailMode('wip'),
    onActivate: (commit, { branches: branchNames }) => {
      if (branchNames && branchNames.length) doCheckout(branchNames[0]);
      else doCheckout(commit.sha, { detach: true });
    },
    onContext: (commit, x, y, { branches: branchNames }) => {
      contextMenu.open(x, y, commitMenuItems(commit, branchNames));
    },
  });

  // --- Sidebar ---------------------------------------------------------------
  const sidebar = initSidebar($('sidebar'), {
    onCheckout: (ref) => doCheckout(ref),
    onTrackRemote: (remoteRef) => doTrackRemote(remoteRef),
    onBranchMenu: (info, x, y) => contextMenu.open(x, y, branchMenuItems(info)),
    onStashMenu: (index, x, y) => contextMenu.open(x, y, stash.menuItems(index)),
  });

  // --- Stash -----------------------------------------------------------------
  const stash = initStash({ getPath, refresh });

  // --- Branches dropdown (toolbar selector) ----------------------------------
  const branches = initBranches(
    {
      dropdown: $('branch-dropdown'),
      toggle: $('branch-toggle'),
      label: $('branch-current'),
      menu: $('branch-menu-slot'),
    },
    { refreshAll: refresh },
  );

  // --- Terminal (lazy) -------------------------------------------------------
  let terminal = null;
  async function toggleTerminal(active) {
    const pane = $('terminal-pane');
    pane.classList.toggle('is-collapsed', !active);
    $('terminal-head').setAttribute('aria-expanded', String(active));
    toolbar.setTerminalActive(active);
    if (active) {
      const p = getPath();
      if (!p) return;
      try {
        const { createTerminal } = await import('./terminal.js');
        if (!terminal) terminal = createTerminal($('terminal-root'));
        terminal.open(p);
      } catch (err) {
        toast('info', 'Terminal unavailable', 'The terminal module is not available yet.');
        // eslint-disable-next-line no-console
        console.error('terminal unavailable', err);
      }
    } else if (terminal) {
      terminal.close();
    }
  }

  $('terminal-head').addEventListener('click', () => {
    const collapsed = $('terminal-pane').classList.contains('is-collapsed');
    toggleTerminal(collapsed); // expand if currently collapsed
  });
  $('terminal-head').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      const collapsed = $('terminal-pane').classList.contains('is-collapsed');
      toggleTerminal(collapsed);
    }
  });

  // --- Toolbar ---------------------------------------------------------------
  const toolbar = initToolbar($('toolbar'), {
    onOpenRepo: () => fsBrowser.open(),
    onPull: (remote) => doPull(remote),
    onPush: (remote) => doPush(remote),
    onFetch: (remote) => doFetch(remote),
    onBranch: () => doCreateBranch(),
    onStash: () => stash.create(),
    onPop: () => stash.pop(),
    onToggleTerminal: (active) => toggleTerminal(active),
  });

  // --- Undo / Redo (reflog) --------------------------------------------------
  const undo = initUndo({ getPath, refresh, undoBtn: $('tb-undo'), redoBtn: $('tb-redo') });

  // --- Auto-refresh: file watcher + debounced panel refresh ------------------
  let watchSocket = null;
  let watchPath = null;
  let watchTimer = null; // debounce timer
  let reconnectTimer = null;
  let reconnectDelay = 1000; // backoff seed (ms), capped in the close handler

  // True while a dialog / menu / merge editor is up — auto-refresh stands down
  // so it never wipes a half-typed message or steals focus mid-interaction.
  function overlayOpen() {
    if (document.querySelector('.modal-overlay.is-open')) return true;
    const mer = $('merge-editor-root');
    if (mer && mer.classList.contains('is-open')) return true;
    if (contextMenu.isOpen()) return true;
    return false;
  }

  // Re-fetch status, refresh badges + drift; cheap (no heavy panel reloads).
  async function refreshStatusAndDrift() {
    await refreshStatus();
    const st = repoRef && repoRef.status();
    if (st) toolbar.setDrift(st);
  }

  // The actual debounced refresh: working tree + status + graph + sidebar, and
  // surface the merge editor if a merge just appeared.
  async function doAutoRefresh() {
    const p = getPath();
    if (!p || overlayOpen()) return;
    const savedMode = detailMode;
    // If the user is typing in the changes panel (e.g. the commit message),
    // skip rebuilding it so we don't steal focus — status/graph still refresh.
    const typingInChanges =
      isTextInput(document.activeElement) && changesRoot.contains(document.activeElement);
    try {
      await Promise.all([
        typingInChanges ? Promise.resolve() : changes.refresh(),
        refreshStatusAndDrift(),
      ]);
      const st = repoRef && repoRef.status();
      await graph.render(p, { dirty: !!(st && st.dirty) });
      // graph.render auto-selects HEAD (→ commit mode); restore WIP if we were there.
      if (savedMode === 'wip') setDetailMode('wip');
      sidebar.load(p);
      undo.refreshState();
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error('auto-refresh failed', err);
    }
    await checkConflicts();
  }

  function scheduleAutoRefresh() {
    clearTimeout(watchTimer);
    watchTimer = setTimeout(doAutoRefresh, 250);
  }

  function teardownWatch() {
    clearTimeout(watchTimer); watchTimer = null;
    clearTimeout(reconnectTimer); reconnectTimer = null;
    if (watchSocket) {
      watchSocket.__intentionalClose = true; // suppress this socket's reconnect
      try { watchSocket.close(); } catch { /* ignore */ }
      watchSocket = null;
    }
  }

  function connectWatch() {
    const p = watchPath;
    if (!p) return;
    let socket;
    try {
      socket = new WebSocket(api.watchUrl(p));
    } catch {
      return; // cannot construct (e.g. bad URL) — give up quietly
    }
    watchSocket = socket;

    socket.addEventListener('message', (ev) => {
      let frame = null;
      try { frame = JSON.parse(ev.data); } catch { return; }
      if (frame && frame.type === 'change') scheduleAutoRefresh();
    });
    socket.addEventListener('open', () => { reconnectDelay = 1000; });
    socket.addEventListener('close', () => {
      if (socket.__intentionalClose) return; // we closed it on purpose
      if (watchPath !== p) return; // repo switched away — let the new socket run
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(() => {
        reconnectDelay = Math.min(reconnectDelay * 2, 15000);
        connectWatch();
      }, reconnectDelay);
    });
    socket.addEventListener('error', () => { try { socket.close(); } catch { /* ignore */ } });
  }

  // Open (or switch) the watcher to `path`, tearing down any previous socket.
  function startWatching(path) {
    if (watchPath === path && watchSocket && watchSocket.readyState <= 1) return;
    teardownWatch();
    watchPath = path;
    reconnectDelay = 1000;
    connectWatch();
  }

  // --- Periodic light status poll (keeps the drift badge fresh) --------------
  let statusPollTimer = null;
  function startStatusPoll() {
    if (statusPollTimer) return; // a single shared interval is enough
    statusPollTimer = setInterval(() => {
      if (!getPath() || overlayOpen()) return;
      refreshStatusAndDrift().catch(() => { /* non-fatal */ });
    }, 60000);
  }

  // --- Commit filter ---------------------------------------------------------
  const filterInput = $('graph-filter-input');
  if (filterInput) {
    filterInput.addEventListener('input', () => graph.setFilter(filterInput.value));
  }

  // --- Keyboard shortcuts + help overlay -------------------------------------
  const shortcutsOverlay = $('shortcuts-overlay');
  const shortcutsClose = $('shortcuts-close');
  const isShortcutsOpen = () => !!(shortcutsOverlay && shortcutsOverlay.classList.contains('is-open'));
  function toggleShortcuts(force) {
    if (!shortcutsOverlay) return;
    const show = force == null ? !isShortcutsOpen() : !!force;
    shortcutsOverlay.classList.toggle('is-open', show);
  }
  if (shortcutsClose) shortcutsClose.addEventListener('click', () => toggleShortcuts(false));
  if (shortcutsOverlay) {
    shortcutsOverlay.addEventListener('mousedown', (e) => {
      if (e.target === shortcutsOverlay) toggleShortcuts(false);
    });
  }

  function isTextInput(target) {
    if (!target) return false;
    const tag = target.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable;
  }
  function clickToolbar(id) {
    const b = $(id);
    if (b && !b.disabled) b.click();
  }

  document.addEventListener('keydown', (e) => {
    const key = e.key;

    // Ctrl/Cmd+Enter → commit, even from inside the message textarea.
    if ((e.ctrlKey || e.metaKey) && key === 'Enter') {
      if (detailMode === 'wip') { e.preventDefault(); changes.triggerCommit(); }
      return;
    }

    // Escape → close the help overlay (modals + context menus handle their own).
    if (key === 'Escape') {
      if (isShortcutsOpen()) { e.preventDefault(); toggleShortcuts(false); }
      return;
    }

    if (e.ctrlKey || e.metaKey || e.altKey) return; // leave browser combos alone
    if (isTextInput(e.target)) return; // typing — ignore single-key shortcuts
    if (overlayOpen() && !isShortcutsOpen()) return; // a dialog/menu owns the keys

    switch (key) {
      case '/':
        e.preventDefault();
        if (filterInput) filterInput.focus();
        break;
      case '?':
        e.preventDefault();
        toggleShortcuts();
        break;
      case 's':
        if (detailMode === 'wip') { e.preventDefault(); changes.stageSelected(); }
        break;
      case 'u':
        if (detailMode === 'wip') { e.preventDefault(); changes.unstageSelected(); }
        break;
      case 'c':
        if (detailMode === 'wip') { e.preventDefault(); changes.focusMessage(); }
        break;
      case 'p':
        e.preventDefault();
        clickToolbar('tb-push');
        break;
      case 'f':
        e.preventDefault();
        clickToolbar('tb-fetch');
        break;
      default:
        break;
    }
  });

  // --- Repo orchestration ----------------------------------------------------
  const repo = initRepo({
    onRepoLoaded(status) {
      const path = status.path;
      toolbar.setRepo(basename(path));
      toolbar.setRemoteContext(status);
      toolbar.setDrift(status);
      toolbar.setEnabled(true);
      setDetailMode('commit');
      branches.load(path);
      sidebar.load(path);
      changes.load(path, status.current_branch);
      graph.render(path, { dirty: !!status.dirty });
      undo.refreshState();
      // (Re)connect the file watcher + periodic status poll for this repo.
      startWatching(path);
      startStatusPoll();
    },
  });
  repoRef = repo;

  const fsBrowser = initFsBrowser({ onOpen: (path) => repo.open(path) });

  // --- Onboarding ------------------------------------------------------------
  const onboarding = initOnboarding(() => {});

  async function checkIdentity({ openIfNeeded }) {
    try {
      const identity = await api.getIdentity();
      if (openIfNeeded && identity.needs_onboarding) onboarding.open(identity);
      return identity;
    } catch (err) {
      toast('error', 'Could not load identity', err.message);
      return null;
    }
  }

  $('tb-identity').addEventListener('click', async () => {
    const identity = await checkIdentity({ openIfNeeded: false });
    if (identity) onboarding.open(identity);
  });

  // First-run check, then restore the last-opened repository.
  checkIdentity({ openIfNeeded: true }).finally(() => repo.loadRemembered());
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
