// app.js — entry point. Boots the repo view and runs the first-run check.

import * as api from './api.js';
import { toast } from './ui.js';
import { initRepo } from './repo.js';
import { initOnboarding } from './onboarding.js';

function boot() {
  const repo = initRepo();
  const onboarding = initOnboarding(() => {
    // Onboarding finished (or skipped) — nothing else required here.
  });

  // Allow re-opening onboarding from the topbar "Identity" button.
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
