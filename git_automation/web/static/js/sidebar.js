// sidebar.js — the ref tree (LOCAL / REMOTE / TAGS / WORKTREES / STASHES) built
// from GET /api/repo/refs. Renders the design-system `.sidebar` component:
// collapsible sections with a caret + count, the current branch marked with a
// check, and ahead/behind chips. Interactions: double-click a local branch to
// check it out; right-click any ref to raise a context menu (the app supplies
// the menu items so reset/merge/PR wiring lives in one place).

import * as api from './api.js';
import { el, toast } from './ui.js';

const ICONS = {
  branch:
    '<svg class="sidebar__icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="4" cy="4" r="1.6"/><circle cx="4" cy="12" r="1.6"/><circle cx="12" cy="6" r="1.6"/><path d="M4 5.6v4.8M5.6 4H9a3 3 0 0 1 3 3v.4"/></svg>',
  remote:
    '<svg class="sidebar__icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="2"/><path d="M8 1v3M8 12v3M1 8h3M12 8h3"/></svg>',
  tag:
    '<svg class="sidebar__icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 7V3.5A1.5 1.5 0 0 1 3.5 2H7l6.5 6.5a1.5 1.5 0 0 1 0 2.1L10.6 13.5a1.5 1.5 0 0 1-2.1 0L2 7Z"/><circle cx="5" cy="5" r=".8"/></svg>',
  worktree:
    '<svg class="sidebar__icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 4.5A1.5 1.5 0 0 1 3.5 3h3l1.5 1.5h4.5A1.5 1.5 0 0 1 14 6v5.5A1.5 1.5 0 0 1 12.5 13h-9A1.5 1.5 0 0 1 2 11.5z"/></svg>',
  stash:
    '<svg class="sidebar__icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="3" width="12" height="10" rx="1.5"/><path d="M2 6h12"/></svg>',
  caret:
    '<svg class="sidebar__caret" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>',
  check:
    '<svg class="sidebar__check" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m3 8 3.5 3.5L13 5"/></svg>',
  checkEmpty: '<svg class="sidebar__check" viewBox="0 0 16 16"></svg>',
};

/**
 * Initialise the ref sidebar.
 * @param {HTMLElement} root the `.sidebar` element
 * @param {{
 *   onCheckout:(ref:string)=>void,
 *   onBranchMenu:(info:{name:string, kind:string}, x:number, y:number)=>void,
 *   onStashMenu:(index:number, x:number, y:number)=>void,
 *   onTagMenu?:(name:string, x:number, y:number)=>void,
 * }} cbs
 */
export function initSidebar(root, cbs = {}) {
  let path = null;
  // Remember which sections the user collapsed across reloads.
  const collapsed = new Set(['Stashes']);

  function chip(modifier, glyph, title) {
    return el('span', {
      class: `sidebar__chip sidebar__chip--${modifier}`,
      text: glyph,
      attrs: { title },
    });
  }

  function item({ icon, name, current, onActivate, onContext, title }) {
    const btn = el('button', {
      class: `sidebar__item${current ? ' is-current' : ''}`,
      attrs: { type: 'button', title: title || name },
    });
    btn.innerHTML = (current ? ICONS.check : ICONS.checkEmpty) + icon;
    btn.appendChild(el('span', { class: 'sidebar__name', text: name }));
    return { btn, onActivate, onContext };
  }

  function attach(entry, meta) {
    const { btn, onActivate, onContext } = entry;
    if (meta) btn.appendChild(meta);
    if (onActivate) {
      btn.addEventListener('dblclick', (e) => {
        e.preventDefault();
        onActivate();
      });
    }
    if (onContext) {
      btn.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        onContext(e.clientX, e.clientY);
      });
    }
    return btn;
  }

  function section(title, count, rows) {
    const sec = el('div', {
      class: `sidebar__section${collapsed.has(title) ? ' is-collapsed' : ''}`,
    });
    const header = el('button', { class: 'sidebar__header', attrs: { type: 'button' } });
    header.innerHTML = ICONS.caret;
    header.appendChild(el('span', { class: 'sidebar__title', text: title }));
    header.appendChild(el('span', { class: 'sidebar__count', text: String(count) }));
    header.addEventListener('click', () => {
      const nowCollapsed = sec.classList.toggle('is-collapsed');
      if (nowCollapsed) collapsed.add(title);
      else collapsed.delete(title);
    });
    sec.appendChild(header);

    const group = el('div', { class: 'sidebar__group' });
    for (const r of rows) group.appendChild(r);
    sec.appendChild(group);
    return sec;
  }

  function render(refs) {
    root.replaceChildren();

    // LOCAL
    const locals = (refs.local || []).map((b) => {
      const entry = item({
        icon: ICONS.branch,
        name: b.name,
        current: b.is_current,
        onActivate: b.is_current ? null : () => cbs.onCheckout && cbs.onCheckout(b.name),
        onContext: (x, y) => cbs.onBranchMenu && cbs.onBranchMenu({ name: b.name, kind: 'local', is_current: b.is_current }, x, y),
      });
      let meta = null;
      if (b.ahead > 0 || b.behind > 0) {
        meta = el('span', { class: 'sidebar__meta' });
        if (b.ahead > 0) meta.appendChild(chip('ahead', `↑${b.ahead}`, `${b.ahead} ahead`));
        if (b.behind > 0) meta.appendChild(chip('behind', `↓${b.behind}`, `${b.behind} behind`));
      }
      return attach(entry, meta);
    });
    root.appendChild(section('Local', locals.length, locals));

    // REMOTE
    const remotes = (refs.remote || []).map((r) => {
      const local = r.name.includes('/') ? r.name.slice(r.name.indexOf('/') + 1) : r.name;
      const entry = item({
        icon: ICONS.remote,
        name: r.name,
        onActivate: () => cbs.onCheckout && cbs.onCheckout(local),
        onContext: (x, y) => cbs.onBranchMenu && cbs.onBranchMenu({ name: r.name, local, kind: 'remote' }, x, y),
      });
      return attach(entry, null);
    });
    root.appendChild(section('Remote', remotes.length, remotes));

    // TAGS
    const tags = (refs.tags || []).map((t) => {
      const entry = item({
        icon: ICONS.tag,
        name: t.name,
        onActivate: () => cbs.onCheckout && cbs.onCheckout(t.name),
        onContext: (x, y) => cbs.onTagMenu && cbs.onTagMenu(t.name, x, y),
      });
      return attach(entry, null);
    });
    root.appendChild(section('Tags', tags.length, tags));

    // WORKTREES
    const worktrees = (refs.worktrees || []).map((w) => {
      const entry = item({
        icon: ICONS.worktree,
        name: w.path,
        current: w.is_current,
        title: `${w.path}${w.branch ? ` · ${w.branch}` : ''}`,
      });
      return attach(entry, w.branch ? el('span', { class: 'sidebar__meta' }, [chip('ahead', w.branch, w.branch)]) : null);
    });
    root.appendChild(section('Worktrees', worktrees.length, worktrees));

    // STASHES
    const stashes = (refs.stashes || []).map((s) => {
      const entry = item({
        icon: ICONS.stash,
        name: s.message || `stash@{${s.index}}`,
        title: `stash@{${s.index}}: ${s.message || ''}`,
        onContext: (x, y) => cbs.onStashMenu && cbs.onStashMenu(s.index, x, y),
      });
      return attach(entry, null);
    });
    root.appendChild(section('Stashes', stashes.length, stashes));
  }

  async function load(repoPath) {
    path = repoPath;
    if (!path) return;
    try {
      const refs = await api.getRefs(path);
      render(refs);
    } catch (err) {
      toast('error', 'Could not load refs', err.message);
      root.replaceChildren(el('div', { class: 'text-muted', text: 'Could not load refs.' }));
    }
  }

  function clear() {
    path = null;
    root.replaceChildren();
  }

  return { load, clear };
}
