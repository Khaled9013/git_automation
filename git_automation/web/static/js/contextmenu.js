// contextmenu.js — a reusable, keyboard-navigable right-click menu built on the
// design-system `.ctxmenu` component (see styleguide.html). It renders nothing
// until `open(x, y, items)` is called, mounts at the cursor, supports nested
// submenus (e.g. Reset ▸ Soft / Mixed / Hard), and closes on selection, outside
// click, or Escape.
//
// Item shape:
//   { sep:true }                                            — a divider
//   { label, icon?, shortcut?, danger?, disabled?,
//     onSelect?, submenu?:[ …items… ] }                     — an action / submenu
// `icon` is raw inner-SVG markup (paths/circles); it is wrapped in a 24×24 svg.

const SVG_OPEN =
  '<svg class="ctxmenu__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">';
const ARROW =
  '<svg class="ctxmenu__arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>';

/**
 * Create a single reusable context-menu controller.
 * @returns {{ open(x:number, y:number, items:Array):void, close():void, isOpen():boolean }}
 */
export function createContextMenu() {
  let root = null; // the top-level .ctxmenu element
  let onCloseExternal = null;

  function close() {
    if (!root) return;
    root.remove();
    root = null;
    document.removeEventListener('mousedown', onDocDown, true);
    document.removeEventListener('keydown', onKeydown, true);
    window.removeEventListener('resize', close);
    window.removeEventListener('blur', close);
    if (onCloseExternal) {
      const cb = onCloseExternal;
      onCloseExternal = null;
      cb();
    }
  }

  function onDocDown(ev) {
    if (root && !root.contains(ev.target)) close();
  }

  // ---- Item construction ----------------------------------------------------

  function buildItem(item, menuEl) {
    if (item.sep) {
      const sep = document.createElement('div');
      sep.className = 'ctxmenu__sep';
      menuEl.appendChild(sep);
      return;
    }

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'ctxmenu__item' + (item.danger ? ' ctxmenu__item--danger' : '');
    if (item.disabled) btn.disabled = true;

    let html = '';
    if (item.icon) html += SVG_OPEN + item.icon + '</svg>';
    html += `<span class="ctxmenu__label"></span>`;
    btn.innerHTML = html;
    btn.querySelector('.ctxmenu__label').textContent = item.label || '';

    const hasSub = Array.isArray(item.submenu) && item.submenu.length > 0;

    if (item.shortcut) {
      const sc = document.createElement('span');
      sc.className = 'ctxmenu__shortcut';
      sc.textContent = item.shortcut;
      btn.appendChild(sc);
    }

    if (hasSub) {
      btn.setAttribute('aria-haspopup', 'true');
      btn.insertAdjacentHTML('beforeend', ARROW);

      const wrap = document.createElement('div');
      wrap.className = 'ctxmenu__sub';
      wrap.appendChild(btn);

      const submenu = document.createElement('div');
      submenu.className = 'ctxmenu__submenu ctxmenu';
      for (const sub of item.submenu) buildItem(sub, submenu);
      wrap.appendChild(submenu);

      const openSub = () => {
        // Close sibling submenus, then reveal this one.
        for (const open of menuEl.querySelectorAll(':scope > .ctxmenu__sub > .ctxmenu__submenu.is-open')) {
          open.classList.remove('is-open');
        }
        submenu.classList.add('is-open');
      };
      btn.addEventListener('mouseenter', openSub);
      btn.addEventListener('focus', openSub);
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        openSub();
        const first = submenu.querySelector('.ctxmenu__item:not([disabled])');
        if (first) first.focus();
      });
      btn.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowRight' || e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          openSub();
          const first = submenu.querySelector('.ctxmenu__item:not([disabled])');
          if (first) first.focus();
        }
      });
      menuEl.appendChild(wrap);
      return;
    }

    btn.addEventListener('click', (e) => {
      e.preventDefault();
      if (item.disabled) return;
      const fn = item.onSelect;
      close();
      if (typeof fn === 'function') fn();
    });
    btn.addEventListener('mouseenter', () => btn.focus());
    menuEl.appendChild(btn);
  }

  // ---- Keyboard navigation --------------------------------------------------

  function focusables(scope) {
    return Array.from(scope.querySelectorAll('.ctxmenu__item:not([disabled])')).filter(
      (n) => n.offsetParent !== null,
    );
  }

  function onKeydown(e) {
    if (!root) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      // Close the innermost open submenu first, else the whole menu.
      const openSub = root.querySelector('.ctxmenu__submenu.is-open');
      if (openSub) {
        openSub.classList.remove('is-open');
        const parentBtn = openSub.parentElement.querySelector(':scope > .ctxmenu__item');
        if (parentBtn) parentBtn.focus();
      } else {
        close();
      }
      return;
    }

    const active = document.activeElement;
    const scope = active && active.closest('.ctxmenu');
    if (!scope) return;
    const items = focusables(scope);
    const idx = items.indexOf(active);

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      items[(idx + 1 + items.length) % items.length || 0]?.focus();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      items[(idx - 1 + items.length) % items.length]?.focus();
    } else if (e.key === 'ArrowLeft') {
      e.preventDefault();
      const submenu = scope.closest('.ctxmenu__submenu');
      if (submenu) {
        submenu.classList.remove('is-open');
        const parentBtn = submenu.parentElement.querySelector(':scope > .ctxmenu__item');
        if (parentBtn) parentBtn.focus();
      }
    }
  }

  function open(x, y, items, opts = {}) {
    close();
    onCloseExternal = opts.onClose || null;
    root = document.createElement('div');
    root.className = 'ctxmenu';
    root.setAttribute('role', 'menu');
    root.style.position = 'fixed';
    root.style.zIndex = '1000';
    for (const item of items) buildItem(item, root);
    document.body.appendChild(root);

    // Clamp to the viewport.
    const rect = root.getBoundingClientRect();
    const px = Math.min(x, window.innerWidth - rect.width - 8);
    const py = Math.min(y, window.innerHeight - rect.height - 8);
    root.style.left = `${Math.max(8, px)}px`;
    root.style.top = `${Math.max(8, py)}px`;

    document.addEventListener('mousedown', onDocDown, true);
    document.addEventListener('keydown', onKeydown, true);
    window.addEventListener('resize', close);
    window.addEventListener('blur', close);

    const first = focusables(root)[0];
    if (first) first.focus();
  }

  return { open, close, isOpen: () => !!root };
}
