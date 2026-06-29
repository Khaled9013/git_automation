// ui.js — small DOM helpers that reuse the existing design-system components
// (toast, console, modal, button loading). No new markup conventions invented;
// every class here is documented in app.css / styleguide.html.

const SVG_NS = 'http://www.w3.org/2000/svg';

/** Build an element with className, attributes and children. */
export function el(tag, opts = {}, children = []) {
  const node = document.createElement(tag);
  if (opts.class) node.className = opts.class;
  if (opts.text != null) node.textContent = opts.text;
  if (opts.html != null) node.innerHTML = opts.html;
  if (opts.attrs) {
    for (const [k, v] of Object.entries(opts.attrs)) {
      if (v != null) node.setAttribute(k, v);
    }
  }
  for (const child of [].concat(children)) {
    if (child) node.appendChild(child);
  }
  return node;
}

function svgIcon(paths, extra = {}) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  for (const [k, v] of Object.entries(extra)) svg.setAttribute(k, v);
  for (const d of [].concat(paths)) {
    const p = document.createElementNS(SVG_NS, 'path');
    p.setAttribute('d', d);
    svg.appendChild(p);
  }
  return svg;
}

const TOAST_ICONS = {
  success: () => svgIcon('m20 6-11 11-5-5', { class: 'toast__icon' }),
  error: () => {
    const svg = svgIcon(['M12 8v5', 'M12 16h.01'], { class: 'toast__icon' });
    const c = document.createElementNS(SVG_NS, 'circle');
    c.setAttribute('cx', '12'); c.setAttribute('cy', '12'); c.setAttribute('r', '9');
    svg.insertBefore(c, svg.firstChild);
    return svg;
  },
  info: () => {
    const svg = svgIcon(['M12 11v5', 'M12 8h.01'], { class: 'toast__icon' });
    const c = document.createElementNS(SVG_NS, 'circle');
    c.setAttribute('cx', '12'); c.setAttribute('cy', '12'); c.setAttribute('r', '9');
    svg.insertBefore(c, svg.firstChild);
    return svg;
  },
};

/**
 * Show a toast in the bottom-right stack.
 * @param {'success'|'error'|'info'} variant
 */
export function toast(variant, title, message = '', timeout = 4500) {
  const stack = document.getElementById('toast-stack');
  if (!stack) return;

  const node = el('div', { class: `toast toast--${variant}`, attrs: { role: 'status' } });
  node.appendChild((TOAST_ICONS[variant] || TOAST_ICONS.info)());

  const body = el('div', { class: 'toast__body' }, [
    el('div', { class: 'toast__title', text: title }),
  ]);
  if (message) body.appendChild(el('div', { class: 'toast__msg', text: message }));
  node.appendChild(body);

  const close = el('button', {
    class: 'toast__close',
    attrs: { 'aria-label': 'Dismiss' },
    html: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 6 12 12M18 6 6 18"/></svg>',
  });
  const dismiss = () => {
    clearTimeout(timer);
    node.remove();
  };
  close.addEventListener('click', dismiss);
  node.appendChild(close);

  stack.appendChild(node);
  const timer = timeout ? setTimeout(dismiss, timeout) : null;
  return dismiss;
}

/**
 * Render a `.console` result block from a command result.
 * @param {HTMLElement} slot  container to render into (replaces contents)
 * @param {object} opts  { command, output, ok }
 */
export function renderConsole(slot, { command, output, ok }) {
  slot.replaceChildren();

  const state = ok ? 'is-success' : 'is-error';
  const dotState = ok ? 'status-dot--ok' : 'status-dot--err';
  const badge = ok
    ? el('span', { class: 'badge badge--success', text: 'Success' })
    : el('span', { class: 'badge badge--danger', text: 'Failed' });

  const head = el('div', { class: 'console__head' }, [
    el('span', { class: `status-dot ${dotState}` }),
    el('span', { text: command }),
    badge,
  ]);

  const pre = el('pre', { class: 'console__body' });
  // First line dimmed as the invoked command, remaining lines as output.
  pre.appendChild(el('span', { class: 'out-dim', text: `$ ${command}` }));
  pre.appendChild(document.createTextNode('\n'));
  const outClass = ok ? '' : 'out-err';
  pre.appendChild(el('span', outClass ? { class: outClass, text: output || '' } : { text: output || '' }));

  slot.appendChild(el('div', { class: `console ${state}` }, [head, pre]));
}

// ---- Button loading state ---------------------------------------------------

/** Toggle the `.is-loading` (+ disabled) state on a button. */
export function setLoading(button, loading) {
  if (!button) return;
  if (loading) {
    button.classList.add('is-loading');
    button.disabled = true;
  } else {
    button.classList.remove('is-loading');
    button.disabled = false;
  }
}

// ---- Modal (focus management + keyboard dismiss) ----------------------------

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Open a `.modal-overlay`, trapping focus and wiring Escape-to-close.
 * @returns {() => void} a close() function.
 */
export function openModal(overlay, { onClose } = {}) {
  const previouslyFocused = document.activeElement;
  overlay.classList.add('is-open');

  const dialog = overlay.querySelector('.modal') || overlay;

  const close = () => {
    overlay.classList.remove('is-open');
    document.removeEventListener('keydown', onKeydown, true);
    if (previouslyFocused && previouslyFocused.focus) previouslyFocused.focus();
    if (onClose) onClose();
  };

  function onKeydown(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      close();
      return;
    }
    if (e.key !== 'Tab') return;
    const items = Array.from(dialog.querySelectorAll(FOCUSABLE)).filter(
      (n) => n.offsetParent !== null,
    );
    if (items.length === 0) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  document.addEventListener('keydown', onKeydown, true);

  // Focus the first sensible control.
  const firstField = dialog.querySelector(FOCUSABLE);
  if (firstField) firstField.focus();

  return close;
}

/** Toggle the `.has-error` state on a `.field` wrapper. */
export function setFieldError(field, hasError) {
  if (!field) return;
  field.classList.toggle('has-error', !!hasError);
}
