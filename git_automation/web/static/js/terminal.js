// terminal.js — embedded PTY terminal: xterm.js front-end over the backend
// WebSocket at `WS /api/terminal?path=<abs>`.
//
// Protocol (matches the slice-3 contract):
//   client -> server : JSON text frames
//                        { "type":"input",  "data":"<text>" }
//                        { "type":"resize", "cols":N, "rows":N }
//   server -> client : raw BINARY frames  -> terminal output (write verbatim)
//                      JSON text frames    -> { "type":"error", ... } / { "type":"exit" }
//
// Public API:
//   import { createTerminal } from './terminal.js';
//   const term = createTerminal(hostEl);   // hostEl = .terminal-pane__body
//   term.open('/abs/repo/path');           // connect + render
//   term.close();                          // dispose + disconnect
//
// xterm.js is vendored locally for offline use (see ../vendor/xterm/VENDOR.md).
// Its UMD build is injected as a classic <script> and exposes `window.Terminal`;
// its stylesheet is injected once as a <link>. Nothing is fetched from a CDN.

const XTERM_JS_URL = new URL('../vendor/xterm/xterm.js', import.meta.url).href;
const XTERM_CSS_URL = new URL('../vendor/xterm/xterm.css', import.meta.url).href;
const CSS_LINK_ID = 'vendor-xterm-css';

// ---------------------------------------------------------------------------
// One-time, memoised loader for the vendored xterm assets.
// ---------------------------------------------------------------------------

let _xtermPromise = null;

/** Inject the xterm stylesheet once (idempotent). */
function ensureXtermCss() {
  if (document.getElementById(CSS_LINK_ID)) return;
  const link = document.createElement('link');
  link.id = CSS_LINK_ID;
  link.rel = 'stylesheet';
  link.href = XTERM_CSS_URL;
  document.head.appendChild(link);
}

/**
 * Load the vendored xterm UMD build once and resolve with the `Terminal`
 * constructor. The UMD build attaches `Terminal` onto `window`.
 */
function loadXterm() {
  ensureXtermCss();
  if (window.Terminal) return Promise.resolve(window.Terminal);
  if (_xtermPromise) return _xtermPromise;

  _xtermPromise = new Promise((resolve, reject) => {
    // Reuse an existing tag if one is already loading.
    const existing = document.querySelector('script[data-vendor="xterm"]');
    if (existing) {
      existing.addEventListener('load', () => resolve(window.Terminal));
      existing.addEventListener('error', () =>
        reject(new Error('Failed to load vendored xterm.js')),
      );
      return;
    }
    const script = document.createElement('script');
    script.src = XTERM_JS_URL;
    script.async = false;
    script.dataset.vendor = 'xterm';
    script.addEventListener('load', () => {
      if (window.Terminal) resolve(window.Terminal);
      else reject(new Error('xterm.js loaded but window.Terminal is missing'));
    });
    script.addEventListener('error', () =>
      reject(new Error('Failed to load vendored xterm.js')),
    );
    document.head.appendChild(script);
  });
  return _xtermPromise;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Read a CSS custom property off an element (trimmed), with a fallback. */
function cssVar(el, name, fallback) {
  const v = getComputedStyle(el).getPropertyValue(name).trim();
  return v || fallback;
}

/** Build the WebSocket URL for the PTY endpoint from `location`. */
function terminalWsUrl(path) {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  // Same host + port as the page; the backend binds 127.0.0.1.
  return `${proto}//${window.location.host}/api/terminal?path=${encodeURIComponent(path)}`;
}

// ---------------------------------------------------------------------------
// createTerminal
// ---------------------------------------------------------------------------

/**
 * Create an embedded terminal bound to `rootEl` (typically a
 * `.terminal-pane__body`). Returns `{ open(path), close() }`.
 */
export function createTerminal(rootEl) {
  let term = null;
  let socket = null;
  let resizeObserver = null;
  let fitRaf = 0;
  let disposed = false; // set by close(); guards async callbacks

  /**
   * Measure a single character cell (CSS px) for the live terminal so we can
   * compute cols/rows ourselves (no fit addon). Prefers xterm's own measured
   * dimensions and falls back to measuring the configured font.
   */
  function measureCell() {
    const dims = term && term._core && term._core._renderService
      ? term._core._renderService.dimensions
      : null;
    const cell = dims && dims.css && dims.css.cell;
    if (cell && cell.width > 0 && cell.height > 0) {
      return { w: cell.width, h: cell.height };
    }
    // Fallback: measure the configured font directly.
    const span = document.createElement('span');
    span.textContent = '0'.repeat(10);
    span.style.cssText =
      'position:absolute;visibility:hidden;white-space:pre;' +
      `font-family:${term.options.fontFamily};` +
      `font-size:${term.options.fontSize}px;` +
      `line-height:${term.options.lineHeight};`;
    rootEl.appendChild(span);
    const rect = span.getBoundingClientRect();
    rootEl.removeChild(span);
    const w = rect.width / 10 || 9;
    const h = Math.ceil((rect.height || 17) * (term.options.lineHeight || 1));
    return { w, h };
  }

  /** Fit the terminal to `rootEl` and notify the backend of the new size. */
  function fit() {
    if (!term || disposed) return;
    const { w, h } = measureCell();
    const availW = rootEl.clientWidth;
    const availH = rootEl.clientHeight;
    if (availW <= 0 || availH <= 0 || w <= 0 || h <= 0) return;
    const cols = Math.max(2, Math.floor(availW / w));
    const rows = Math.max(1, Math.floor(availH / h));
    if (cols === term.cols && rows === term.rows) return;
    term.resize(cols, rows);
    sendResize(cols, rows);
  }

  /** Coalesce fit() calls into one per animation frame. */
  function scheduleFit() {
    if (fitRaf) cancelAnimationFrame(fitRaf);
    fitRaf = requestAnimationFrame(() => {
      fitRaf = 0;
      fit();
    });
  }

  function sendInput(data) {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: 'input', data }));
    }
  }

  function sendResize(cols, rows) {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: 'resize', cols, rows }));
    }
  }

  /** Write a dimmed status line into the terminal (e.g. on exit/error). */
  function notice(text) {
    if (term && !disposed) term.write(`\r\n\x1b[2m${text}\x1b[0m\r\n`);
  }

  async function open(path) {
    if (term) close(); // re-open cleanly if already running
    disposed = false;

    const Terminal = await loadXterm();
    if (disposed) return; // closed while xterm was loading

    term = new Terminal({
      cursorBlink: true,
      convertEol: false,
      scrollback: 5000,
      fontFamily: cssVar(rootEl, '--font-mono', 'ui-monospace, monospace'),
      fontSize: 13,
      lineHeight: 1.2,
      theme: {
        background: cssVar(rootEl, '--bg-inset', '#0C0F16'),
        foreground: cssVar(rootEl, '--text-secondary', '#A6B0C3'),
        cursor: cssVar(rootEl, '--accent', '#6E8AFF'),
        cursorAccent: cssVar(rootEl, '--bg-inset', '#0C0F16'),
        selectionBackground: cssVar(rootEl, '--accent-subtle', 'rgba(110,138,255,0.3)'),
      },
    });

    term.open(rootEl);
    fit();

    // Local keystrokes -> backend.
    term.onData(sendInput);

    // Refit when the pane resizes.
    if (typeof ResizeObserver !== 'undefined') {
      resizeObserver = new ResizeObserver(scheduleFit);
      resizeObserver.observe(rootEl);
    }
    window.addEventListener('resize', scheduleFit);

    // --- WebSocket -------------------------------------------------------
    socket = new WebSocket(terminalWsUrl(path));
    socket.binaryType = 'arraybuffer';

    socket.addEventListener('open', () => {
      if (disposed) return;
      // Announce the current size so the PTY matches the rendered grid.
      sendResize(term.cols, term.rows);
      term.focus();
    });

    socket.addEventListener('message', (ev) => {
      if (disposed || !term) return;
      if (typeof ev.data === 'string') {
        // JSON control frame.
        let msg;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (msg && msg.type === 'error') {
          notice(`[terminal error] ${msg.message || msg.code || 'unknown error'}`);
        } else if (msg && msg.type === 'exit') {
          notice('[process exited]');
          teardownSocket();
        }
        return;
      }
      // Binary frame: raw terminal output. xterm accepts Uint8Array directly.
      term.write(new Uint8Array(ev.data));
    });

    socket.addEventListener('error', () => {
      if (!disposed) notice('[connection error]');
    });

    socket.addEventListener('close', () => {
      if (!disposed) notice('[disconnected]');
    });
  }

  /** Tear down just the socket (PTY ended) but keep the rendered buffer. */
  function teardownSocket() {
    if (!socket) return;
    try {
      if (
        socket.readyState === WebSocket.OPEN ||
        socket.readyState === WebSocket.CONNECTING
      ) {
        socket.close();
      }
    } catch {
      /* ignore */
    }
    socket = null;
  }

  function close() {
    disposed = true;

    if (fitRaf) {
      cancelAnimationFrame(fitRaf);
      fitRaf = 0;
    }
    window.removeEventListener('resize', scheduleFit);
    if (resizeObserver) {
      resizeObserver.disconnect();
      resizeObserver = null;
    }

    teardownSocket();

    if (term) {
      try {
        term.dispose();
      } catch {
        /* ignore */
      }
      term = null;
    }
  }

  return { open, close };
}
