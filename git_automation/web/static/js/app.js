// app.js — entry point. Boots the repo view, the full git-client panels
// (folder browser, branches, changes/commit, diff), the commit graph, and
// runs the first-run onboarding check.

import * as api from './api.js';
import { toast } from './ui.js';
import { initRepo } from './repo.js';
import { initOnboarding } from './onboarding.js';
import { initFsBrowser } from './fsbrowser.js';
import { initChanges } from './changes.js';
import { initBranches } from './branches.js';
import { initDiff } from './diff.js';
import { createGraph } from './graph.js';

function boot() {
  // --- Git-client panels -----------------------------------------------------
  const gitClient = document.getElementById('git-client');
  const graphPanel = document.getElementById('graph-panel');

  const diff = initDiff(document.getElementById('diff-slot'));
  const graph = createGraph(document.getElementById('commit-graph'));

  // `repo` is created below; panels reach it through these thin wrappers so we
  // can define their refresh callbacks before `repo` exists.
  let repoRef = null;
  const refreshAll = () => (repoRef ? repoRef.refreshAll() : Promise.resolve());
  const refreshStatus = () => (repoRef ? repoRef.refreshStatus() : Promise.resolve());

  const changes = initChanges(document.getElementById('changes-slot'), {
    onSelectFile: (file, staged) => {
      const path = repoRef && repoRef.currentPath();
      if (path) diff.show(path, file, staged);
    },
    refreshAll,
    refreshStatus,
  });

  const branches = initBranches(
    {
      dropdown: document.getElementById('branch-dropdown'),
      toggle: document.getElementById('branch-toggle'),
      label: document.getElementById('branch-current'),
      menu: document.getElementById('branch-menu-slot'),
    },
    { refreshAll },
  );

  // --- Repo view (slice 1) + panel orchestration -----------------------------
  const repo = initRepo({
    onRepoLoaded(status) {
      const path = status.path;
      gitClient.hidden = false;
      graphPanel.hidden = false;
      diff.clear();
      changes.load(path, status.current_branch);
      branches.load(path);
      graph.render(path);
    },
  });
  repoRef = repo;

  const fsBrowser = initFsBrowser({ onOpen: (path) => repo.open(path) });

  document.getElementById('btn-browse').addEventListener('click', () => fsBrowser.open());
  document.getElementById('btn-browse-inline').addEventListener('click', () => fsBrowser.open());

  // --- Onboarding (slice 1) --------------------------------------------------
  const onboarding = initOnboarding(() => {
    // Onboarding finished (or skipped) — nothing else required here.
  });

  const btnIdentity = document.getElementById('btn-identity');

  async function checkIdentity({ openIfNeeded }) {
    try {
      const identity = await api.getIdentity();
      if (openIfNeeded && identity.needs_onboarding) {
        onboarding.open(identity);
      }
      return identity;
    } catch (err) {
      toast('error', 'Could not load identity', err.message);
      return null;
    }
  }

  btnIdentity.addEventListener('click', async () => {
    const identity = await checkIdentity({ openIfNeeded: false });
    if (identity) onboarding.open(identity);
  });

  // First-run check, then restore the last-opened repository.
  checkIdentity({ openIfNeeded: true }).finally(() => {
    repo.loadRemembered();
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
