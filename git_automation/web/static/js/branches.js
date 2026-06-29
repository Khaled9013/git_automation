// branches.js — branch menu dropdown: local/remote branches with the current
// one marked and ahead/behind chips; switch on click; create / merge / delete
// (merge & delete confirm first). Renders the `.branch-menu` into the dropdown.

import * as api from './api.js';
import { el, toast, confirmDialog, promptDialog } from './ui.js';

const CHECK_SVG =
  '<svg class="branch-menu__check" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m3 8 3.5 3.5L13 5"/></svg>';

/**
 * Initialise the branch menu.
 * @param {{ dropdown:HTMLElement, toggle:HTMLElement, label:HTMLElement, menu:HTMLElement }} els
 * @param {{ refreshAll:()=>Promise<void> }} cbs
 */
export function initBranches({ dropdown, toggle, label, menu }, { refreshAll } = {}) {
  let path = null;
  let current = null;
  let busy = false;

  // ---- Open/close -----------------------------------------------------------

  function isOpen() {
    return dropdown.classList.contains('is-open');
  }
  function open() {
    dropdown.classList.add('is-open');
    toggle.setAttribute('aria-expanded', 'true');
    document.addEventListener('click', onDocClick, true);
    document.addEventListener('keydown', onKeydown, true);
    const first = menu.querySelector('.branch-menu__item');
    if (first) first.focus();
  }
  function close() {
    dropdown.classList.remove('is-open');
    toggle.setAttribute('aria-expanded', 'false');
    document.removeEventListener('click', onDocClick, true);
    document.removeEventListener('keydown', onKeydown, true);
  }
  function onDocClick(e) {
    if (!dropdown.contains(e.target)) close();
  }
  function onKeydown(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      close();
      toggle.focus();
    }
  }

  toggle.addEventListener('click', (e) => {
    e.preventDefault();
    if (isOpen()) close();
    else if (path) open();
  });

  // ---- Rendering ------------------------------------------------------------

  function chip(modifier, glyph, title) {
    return el('span', {
      class: `branch-menu__chip branch-menu__chip--${modifier}`,
      text: glyph,
      attrs: { title },
    });
  }

  function localItem(branch) {
    const item = el('div', {
      class: `branch-menu__item${branch.is_current ? ' is-current' : ''}`,
      attrs: { role: 'menuitem', tabindex: '0' },
    });
    item.innerHTML = CHECK_SVG;
    item.appendChild(el('span', { class: 'branch-menu__name', text: branch.name, attrs: { title: branch.name } }));

    const chips = el('span', { class: 'branch-menu__chips' });
    if (branch.ahead > 0) chips.appendChild(chip('ahead', `↑${branch.ahead}`, `${branch.ahead} ahead`));
    if (branch.behind > 0) chips.appendChild(chip('behind', `↓${branch.behind}`, `${branch.behind} behind`));
    item.appendChild(chips);

    if (!branch.is_current) {
      const actions = el('span', { class: 'branch-menu__chips' }, [
        actionBtn('Merge', () => doMerge(branch.name)),
        actionBtn('Delete', () => doDelete(branch.name)),
      ]);
      item.appendChild(actions);

      const switchTo = () => doCheckout(branch.name);
      item.addEventListener('click', switchTo);
      item.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          switchTo();
        }
      });
    }
    return item;
  }

  function remoteItem(ref) {
    const item = el('div', {
      class: 'branch-menu__item',
      attrs: { role: 'menuitem', tabindex: '0' },
    });
    item.innerHTML = CHECK_SVG;
    item.appendChild(el('span', { class: 'branch-menu__name', text: ref.name, attrs: { title: ref.name } }));
    // Checking out a remote ref switches to / creates the local tracking branch.
    const localName = ref.name.includes('/') ? ref.name.slice(ref.name.indexOf('/') + 1) : ref.name;
    const switchTo = () => doCheckout(localName);
    item.addEventListener('click', switchTo);
    item.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        switchTo();
      }
    });
    return item;
  }

  function actionBtn(text, handler) {
    const b = el('button', { class: 'btn btn--ghost btn--sm', text, attrs: { type: 'button' } });
    b.addEventListener('click', (e) => {
      e.stopPropagation();
      handler();
    });
    return b;
  }

  function group(labelText, items) {
    const g = el('div', { class: 'branch-menu__group' }, [
      el('div', { class: 'branch-menu__label', text: labelText }),
    ]);
    for (const it of items) g.appendChild(it);
    return g;
  }

  function render(branchList) {
    const wrap = el('div', { class: 'branch-menu' });
    const locals = branchList.local || [];
    const remotes = branchList.remote || [];

    wrap.appendChild(group('Local', locals.map(localItem)));
    if (remotes.length > 0) {
      wrap.appendChild(el('div', { class: 'dropdown__sep' }));
      wrap.appendChild(group('Remote', remotes.map(remoteItem)));
    }
    wrap.appendChild(el('div', { class: 'dropdown__sep' }));

    const createItem = el('button', {
      class: 'branch-menu__item',
      attrs: { type: 'button', role: 'menuitem' },
    }, [
      el('span', { class: 'branch-menu__check' }),
      el('span', { text: 'Create branch…' }),
    ]);
    createItem.addEventListener('click', (e) => {
      e.stopPropagation();
      doCreate();
    });
    wrap.appendChild(createItem);

    menu.replaceChildren(wrap);
  }

  // ---- Operations -----------------------------------------------------------

  async function doCheckout(name) {
    if (!path || busy) return;
    busy = true;
    close();
    try {
      await api.branchCheckout(path, name);
      toast('success', 'Switched branch', name);
      if (refreshAll) await refreshAll();
    } catch (err) {
      toast('error', 'Could not switch branch', err.message);
    } finally {
      busy = false;
    }
  }

  async function doCreate() {
    if (!path || busy) return;
    close();
    const name = await promptDialog({
      title: 'Create branch',
      label: 'New branch name',
      placeholder: 'feature/my-change',
      confirmLabel: 'Create & switch',
    });
    if (!name) return;
    busy = true;
    try {
      await api.branchCreate(path, name, true);
      toast('success', 'Branch created', name);
      if (refreshAll) await refreshAll();
    } catch (err) {
      toast('error', 'Could not create branch', err.message);
    } finally {
      busy = false;
    }
  }

  async function doMerge(name) {
    if (!path || busy) return;
    close();
    const ok = await confirmDialog({
      title: `Merge ${name} into ${current || 'current branch'}?`,
      message: 'This merges the selected branch into your current branch.',
      confirmLabel: 'Merge',
    });
    if (!ok) return;
    busy = true;
    try {
      await api.branchMerge(path, name);
      toast('success', 'Merged', `${name} → ${current || 'current'}`);
      if (refreshAll) await refreshAll();
    } catch (err) {
      toast('error', 'Merge failed', err.message);
    } finally {
      busy = false;
    }
  }

  async function doDelete(name) {
    if (!path || busy) return;
    close();
    const ok = await confirmDialog({
      title: `Delete branch ${name}?`,
      message: 'This permanently deletes the local branch.',
      confirmLabel: 'Delete',
      danger: true,
    });
    if (!ok) return;
    busy = true;
    try {
      await api.branchDelete(path, name, false);
      toast('success', 'Branch deleted', name);
      if (refreshAll) await refreshAll();
    } catch (err) {
      // Offer a force delete when the branch is not fully merged.
      const force = await confirmDialog({
        title: `Force delete ${name}?`,
        message: `${err.message}\n\nForce-delete it anyway? Unmerged work will be lost.`,
        confirmLabel: 'Force delete',
        danger: true,
      });
      if (force) {
        try {
          await api.branchDelete(path, name, true);
          toast('success', 'Branch force-deleted', name);
          if (refreshAll) await refreshAll();
        } catch (err2) {
          toast('error', 'Could not delete branch', err2.message);
        }
      }
    } finally {
      busy = false;
    }
  }

  // ---- Public API -----------------------------------------------------------

  async function load(repoPath) {
    path = repoPath;
    toggle.disabled = false;
    try {
      const branchList = await api.getBranches(path);
      current = branchList.current || null;
      label.textContent = current || 'detached';
      render(branchList);
    } catch (err) {
      label.textContent = '—';
      menu.replaceChildren();
      toast('error', 'Could not load branches', err.message);
    }
  }

  function clear() {
    path = null;
    current = null;
    label.textContent = '—';
    menu.replaceChildren();
    toggle.disabled = true;
    close();
  }

  return { load, clear };
}
