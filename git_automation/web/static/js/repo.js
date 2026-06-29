// repo.js — repository view: open-by-path, status rendering, remote selector,
// and push / pull / fetch actions wired to the backend.

import * as api from './api.js';
import { renderConsole, setLoading, toast } from './ui.js';

const STORAGE_KEY = 'git-automation:last-path';

export function initRepo() {
  const form = document.getElementById('open-form');
  const pathInput = document.getElementById('repo-path');
  const btnOpen = document.getElementById('btn-open');
  const btnRefresh = document.getElementById('btn-refresh');

  const repoPanel = document.getElementById('repo-panel');
  const remoteSelect = document.getElementById('remote-select');
  const statusBadges = document.getElementById('status-badges');
  const consoleSlot = document.getElementById('console-slot');

  const topbarMeta = document.getElementById('topbar-meta');
  const metaRepo = document.getElementById('meta-repo');
  const metaBranch = document.getElementById('meta-branch');
  const metaStatus = document.getElementById('meta-status');

  const btnPush = document.getElementById('btn-push');
  const btnPull = document.getElementById('btn-pull');
  const btnFetch = document.getElementById('btn-fetch');

  /** @type {object|null} */
  let status = null;
  let currentPath = null;

  function badge(cls, text) {
    const span = document.createElement('span');
    span.className = `badge ${cls}`.trim();
    span.textContent = text;
    return span;
  }

  function basename(p) {
    const trimmed = String(p).replace(/\/+$/, '');
    const idx = trimmed.lastIndexOf('/');
    return idx >= 0 ? trimmed.slice(idx + 1) || trimmed : trimmed;
  }

  function renderStatus() {
    statusBadges.replaceChildren();
    if (!status) return;

    const branch = status.current_branch || 'detached';
    const upstream = status.upstream
      ? `${status.upstream.remote}/${status.upstream.branch}`
      : 'no upstream';

    statusBadges.appendChild(badge('badge--accent', branch));
    statusBadges.appendChild(badge('', `upstream: ${upstream}`));

    if (status.ahead > 0) statusBadges.appendChild(badge('', `${status.ahead} ahead`));
    if (status.behind > 0) statusBadges.appendChild(badge('badge--warning', `${status.behind} behind`));
    statusBadges.appendChild(
      status.dirty
        ? badge('badge--danger', 'dirty')
        : badge('badge--success', 'clean'),
    );

    // Topbar meta
    metaRepo.textContent = basename(status.path);
    metaRepo.title = status.path;
    metaBranch.textContent = branch;
    metaStatus.replaceChildren();
    if (status.behind > 0) metaStatus.appendChild(badge('badge--warning', `${status.behind} behind`));
    else if (status.dirty) metaStatus.appendChild(badge('badge--danger', 'dirty'));
    else metaStatus.appendChild(badge('badge--success', 'clean'));
    topbarMeta.hidden = false;
  }

  function populateRemotes() {
    remoteSelect.replaceChildren();
    const remotes = status && status.remotes ? status.remotes : [];

    if (remotes.length === 0) {
      const opt = document.createElement('option');
      opt.textContent = 'No remotes configured';
      opt.value = '';
      remoteSelect.appendChild(opt);
      remoteSelect.disabled = true;
      setActionsEnabled(false);
      return;
    }

    remoteSelect.disabled = false;
    for (const r of remotes) {
      const opt = document.createElement('option');
      opt.value = r.name;
      opt.textContent = r.url ? `${r.name} — ${r.url}` : r.name;
      remoteSelect.appendChild(opt);
    }

    // Default: branch upstream remote, else origin, else first.
    let preferred = status.upstream && status.upstream.remote;
    if (!preferred && remotes.some((r) => r.name === 'origin')) preferred = 'origin';
    if (!preferred) preferred = remotes[0].name;
    remoteSelect.value = preferred;

    setActionsEnabled(true);
  }

  function setActionsEnabled(enabled) {
    for (const b of [btnPush, btnPull, btnFetch]) b.disabled = !enabled;
  }

  // ---- Load / refresh -------------------------------------------------------

  async function load(path) {
    const target = (path || '').trim();
    if (!target) {
      toast('error', 'Path required', 'Enter an absolute path to a local git repository.');
      pathInput.focus();
      return;
    }

    setLoading(btnOpen, true);
    btnRefresh.disabled = true;
    try {
      status = await api.getRepo(target);
      currentPath = status.path || target;
      localStorage.setItem(STORAGE_KEY, currentPath);
      pathInput.value = currentPath;

      renderStatus();
      populateRemotes();
      consoleSlot.replaceChildren();
      repoPanel.hidden = false;
      btnRefresh.disabled = false;
      toast('success', 'Repository opened', basename(currentPath));
    } catch (err) {
      status = null;
      currentPath = null;
      topbarMeta.hidden = true;
      repoPanel.hidden = true;
      btnRefresh.disabled = true;
      toast('error', 'Could not open repository', err.message);
    } finally {
      setLoading(btnOpen, false);
    }
  }

  // ---- Git operations -------------------------------------------------------

  async function runOp(kind) {
    if (!currentPath) return;
    const remote = remoteSelect.value;
    if (!remote) {
      toast('error', 'No remote selected', 'Configure a remote for this repository first.');
      return;
    }

    const branch = status && status.current_branch ? status.current_branch : null;
    const buttons = { push: btnPush, pull: btnPull, fetch: btnFetch };
    const button = buttons[kind];

    let label;
    let call;
    if (kind === 'push') {
      const setUpstream = !(status && status.upstream);
      label = `git push${setUpstream ? ' --set-upstream' : ''} ${remote}${branch ? ` ${branch}` : ''}`;
      call = () => api.gitPush(currentPath, remote, branch, setUpstream);
    } else if (kind === 'pull') {
      label = `git pull ${remote}${branch ? ` ${branch}` : ''}`;
      call = () => api.gitPull(currentPath, remote, branch);
    } else {
      label = `git fetch ${remote}`;
      call = () => api.gitFetch(currentPath, remote);
    }

    setLoading(button, true);
    try {
      const result = await call();
      renderConsole(consoleSlot, { command: label, output: result.output, ok: result.ok !== false });
      toast('success', `${cap(kind)} complete`, `${remote}`);
      // Refresh ahead/behind/upstream after a successful op.
      await refreshStatusQuietly();
    } catch (err) {
      renderConsole(consoleSlot, { command: label, output: err.message, ok: false });
      toast('error', `${cap(kind)} failed`, err.message);
    } finally {
      setLoading(button, false);
    }
  }

  async function refreshStatusQuietly() {
    if (!currentPath) return;
    try {
      status = await api.getRepo(currentPath);
      renderStatus();
      // Keep the user's remote choice if still valid.
      const chosen = remoteSelect.value;
      populateRemotes();
      if (chosen && status.remotes.some((r) => r.name === chosen)) remoteSelect.value = chosen;
    } catch {
      // Non-fatal: leave the last-known status in place.
    }
  }

  function cap(s) {
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  // ---- Wiring ---------------------------------------------------------------

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    load(pathInput.value);
  });
  btnRefresh.addEventListener('click', () => load(currentPath || pathInput.value));
  btnPush.addEventListener('click', () => runOp('push'));
  btnPull.addEventListener('click', () => runOp('pull'));
  btnFetch.addEventListener('click', () => runOp('fetch'));

  // Restore last path (do not auto-load to avoid surprising network calls).
  const remembered = localStorage.getItem(STORAGE_KEY);
  if (remembered) pathInput.value = remembered;

  return {
    /** Programmatically load the remembered path, if any. */
    loadRemembered() {
      const remembered2 = localStorage.getItem(STORAGE_KEY);
      if (remembered2) load(remembered2);
    },
  };
}
