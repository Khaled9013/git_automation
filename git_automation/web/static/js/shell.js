// shell.js — app-nav host. Owns the top .appnav: switches between the existing
// Git cockpit (#view-git, booted by app.js and left untouched) and the GitHub
// cockpit (#view-github, lazily built on first open). The notification poller +
// unread badge start at boot so the badge stays live on any tab; the GitHub view
// is only constructed the first time its tab is opened.

import { toast } from './ui.js';
import { createAlerts } from './github/alerts.js';

function shell() {
  const $ = (id) => document.getElementById(id);

  const tabGit = $('nav-git');
  const tabGithub = $('nav-github');
  const viewGit = $('view-git');
  const viewGithub = $('view-github');
  const badge = $('gh-nav-badge');

  if (!tabGit || !tabGithub || !viewGit || !viewGithub) return; // shell markup absent

  let ghView = null; // lazily created GitHub view controller
  let current = 'git';

  // #view-git is `.layout-3pane` (display:grid) so it can fill the viewport via
  // the flex auto-shrink under body.app — but `display:grid` defeats the [hidden]
  // attribute, so we hide the git view by detaching it from the DOM and
  // re-inserting it right after the app-nav. The GitHub view is a `.main` block
  // (no forced display), so a plain [hidden] toggle hides it cleanly.
  const navEl = tabGit.closest('.appnav') || tabGit.parentNode;
  function attachGit() {
    if (!viewGit.parentNode) navEl.after(viewGit);
  }
  function detachGit() {
    if (viewGit.parentNode) viewGit.parentNode.removeChild(viewGit);
  }

  function setActiveTab(tab) {
    for (const t of [tabGit, tabGithub]) {
      const active = t === tab;
      t.classList.toggle('is-active', active);
      t.setAttribute('aria-selected', String(active));
      if (active) t.setAttribute('aria-current', 'page');
      else t.removeAttribute('aria-current');
    }
  }

  // Lazily import + build the GitHub view exactly once.
  async function ensureGithub() {
    if (ghView) return ghView;
    try {
      const { createView } = await import('./github/view.js');
      ghView = createView(viewGithub, { toast });
      ghView.init(alerts);
    } catch (err) {
      toast('error', 'GitHub view unavailable', err.message);
      // eslint-disable-next-line no-console
      console.error('failed to load GitHub view', err);
      throw err;
    }
    return ghView;
  }

  async function switchTo(view) {
    if (view === 'github') {
      detachGit();
      viewGithub.hidden = false;
      setActiveTab(tabGithub);
      current = 'github';
      try { await ensureGithub(); } catch { /* error already surfaced */ }
    } else {
      viewGithub.hidden = true;
      attachGit();
      setActiveTab(tabGit);
      current = 'git';
    }
  }

  // Notification poller + badge — created and started at boot, independent of
  // which tab is showing. Clicking an OS alert focuses the app + opens the thread.
  const alerts = createAlerts({
    badgeEl: badge,
    toast,
    onOpenThread: async (note) => {
      await switchTo('github');
      try {
        const v = await ensureGithub();
        v.openThread(note);
      } catch { /* error already surfaced */ }
    },
  });

  tabGit.addEventListener('click', () => switchTo('git'));
  tabGithub.addEventListener('click', () => switchTo('github'));

  // Default to the Git cockpit (already visible); just sync the badge poller.
  setActiveTab(tabGit);
  alerts.start();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', shell);
} else {
  shell();
}
