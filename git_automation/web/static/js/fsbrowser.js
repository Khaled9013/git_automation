// fsbrowser.js — in-app folder browser (repo picker) in a modal. Starts at
// `/api/fs/home`, lists directories via `/api/fs/list`, supports breadcrumb +
// into/up navigation, flags git repos, and opens the selected repo.

import * as api from './api.js';
import { el, toast, openModal } from './ui.js';

const FOLDER_ICON =
  '<svg class="fs-browser__icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 4.5A1.5 1.5 0 0 1 3.5 3h3l1.5 1.5h4.5A1.5 1.5 0 0 1 14 6v5.5A1.5 1.5 0 0 1 12.5 13h-9A1.5 1.5 0 0 1 2 11.5z"/></svg>';
const UP_ICON =
  '<svg class="fs-browser__icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M7 4 5.5 2.5h-3A1.5 1.5 0 0 0 1 4v8M1 12V6.5A1.5 1.5 0 0 1 2.5 5h11A1.5 1.5 0 0 1 15 6.5v5A1.5 1.5 0 0 1 13.5 13h-11A1.5 1.5 0 0 1 1 12z"/></svg>';
const REPO_BADGE =
  '<span class="fs-browser__badge"><svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="4" cy="4" r="1.5"/><circle cx="4" cy="12" r="1.5"/><circle cx="12" cy="6" r="1.5"/><path d="M4 5.5v5M5.5 4H9a3 3 0 0 1 3 3v.5"/></svg>repo</span>';

/**
 * Initialise the folder browser modal.
 * @param {{ onOpen:(path:string)=>void }} cbs
 * @returns {{ open():void }}
 */
export function initFsBrowser({ onOpen } = {}) {
  const overlay = document.getElementById('fs-overlay');
  const breadcrumb = document.getElementById('fs-breadcrumb');
  const listEl = document.getElementById('fs-list');
  const btnOpen = document.getElementById('fs-open');
  const btnCancel = document.getElementById('fs-cancel');

  let closeModal = null;
  let currentDir = null;
  let selectedRepo = null; // absolute path of a selected git repo

  function setSelected(path) {
    selectedRepo = path;
    btnOpen.disabled = !path;
    for (const row of listEl.querySelectorAll('.fs-browser__row')) {
      row.classList.toggle('is-selected', !!path && row.dataset.path === path);
    }
  }

  function renderBreadcrumb(path) {
    breadcrumb.replaceChildren();
    const parts = path.split('/').filter(Boolean);

    const rootBtn = el('button', { class: 'breadcrumb__item', text: '/', attrs: { type: 'button' } });
    rootBtn.addEventListener('click', () => navigate('/'));
    breadcrumb.appendChild(rootBtn);

    let acc = '';
    for (const part of parts) {
      acc += `/${part}`;
      breadcrumb.appendChild(el('span', { class: 'breadcrumb__sep', text: '/' }));
      const target = acc;
      const item = el('button', { class: 'breadcrumb__item', text: part, attrs: { type: 'button' } });
      item.addEventListener('click', () => navigate(target));
      breadcrumb.appendChild(item);
    }
  }

  function dirRow({ name, path, isRepo }) {
    const row = el('button', {
      class: `fs-browser__row${isRepo ? ' is-repo' : ''}`,
      attrs: { type: 'button', 'data-path': path, title: path },
    });
    row.innerHTML = FOLDER_ICON;
    row.appendChild(el('span', { class: 'fs-browser__name', text: name }));
    if (isRepo) {
      const holder = document.createElement('template');
      holder.innerHTML = REPO_BADGE;
      row.appendChild(holder.content.firstChild);
    }
    row.addEventListener('click', () => {
      if (isRepo) setSelected(path);
      else navigate(path);
    });
    row.addEventListener('dblclick', () => navigate(path));
    return row;
  }

  function upRow(parent) {
    const row = el('button', { class: 'fs-browser__row', attrs: { type: 'button', title: parent } });
    row.innerHTML = UP_ICON;
    row.appendChild(el('span', { class: 'fs-browser__name', text: '..' }));
    row.addEventListener('click', () => navigate(parent));
    return row;
  }

  async function navigate(path) {
    setSelected(null);
    listEl.replaceChildren(el('div', { class: 'fs-browser__row', text: 'Loading…' }));
    try {
      const res = await api.fsList(path);
      currentDir = res.path;
      renderBreadcrumb(res.path);
      listEl.replaceChildren();
      if (res.parent) listEl.appendChild(upRow(res.parent));
      const entries = (res.entries || []).filter((e) => e.is_dir);
      if (entries.length === 0 && !res.parent) {
        listEl.appendChild(el('div', { class: 'fs-browser__row', text: 'No subfolders here.' }));
      }
      for (const e of entries) {
        listEl.appendChild(dirRow({ name: e.name, path: e.path, isRepo: e.is_git_repo }));
      }
    } catch (err) {
      listEl.replaceChildren(el('div', { class: 'fs-browser__row', text: `Could not list: ${err.message}` }));
      toast('error', 'Could not list folder', err.message);
    }
  }

  btnOpen.addEventListener('click', () => {
    if (!selectedRepo) return;
    const target = selectedRepo;
    if (closeModal) closeModal();
    if (onOpen) onOpen(target);
  });
  btnCancel.addEventListener('click', () => {
    if (closeModal) closeModal();
  });

  async function open() {
    selectedRepo = null;
    btnOpen.disabled = true;
    listEl.replaceChildren(el('div', { class: 'fs-browser__row', text: 'Loading…' }));
    breadcrumb.replaceChildren();
    closeModal = openModal(overlay, { onClose: () => { closeModal = null; } });
    try {
      const home = await api.fsHome();
      await navigate(home.path);
    } catch (err) {
      toast('error', 'Could not open folder browser', err.message);
    }
  }

  return { open };
}
