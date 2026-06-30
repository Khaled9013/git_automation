// graph.js — interactive, GitKraken-style commit graph.
//
// Public API:
//   import { createGraph } from './graph.js';
//   const graph = createGraph(containerEl, { onSelect, limit });
//   await graph.render(path);   // fetch commits, assign lanes, draw SVG
//   graph.clear();              // empty the container
//
// Rendering reuses the design-system `.graph*` classes (see app.css / the
// "Commit graph" section of styleguide.html). It adds NO CSS. Lane colours
// cycle the `--lane-1..6` palette via the `.graph__lane-N` helper classes.
//
// The lane/column layout is computed client-side with the standard git-graph
// algorithm: walk commits newest-first (the order the API returns), keep a set
// of "active lanes" (each waiting for the next commit it expects), continue a
// commit's first parent in its own lane, merge converging lines into the
// left-most shared lane, and allocate fresh lanes for diverging branches.

import * as api from './api.js';

// ---- Geometry (matches the styleguide SVG sample exactly) ------------------
const LANE_W = 18; // px between lane centres
const ROW_H = 38; // px row height (mirrors --graph-row-h)
const MID_Y = 19; // node vertical centre
const NODE_X0 = 9; // x of lane 0's centre
const NODE_R = 4.5; // node radius
const LANE_COUNT = 6; // palette size (--lane-1..6)
const CURVE = 0.55; // bezier tension for diagonal connectors

/** Centre-x for a lane column. */
const laneX = (col) => NODE_X0 + LANE_W * col;

/** Escape a string for safe interpolation into innerHTML. */
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/** Cubic-bezier path string for a smooth vertical-ish diagonal connector. */
function curvePath(x1, y1, x2, y2) {
  const dy = y2 - y1;
  const c1y = y1 + dy * CURVE;
  const c2y = y2 - dy * CURVE;
  return `M ${x1} ${y1} C ${x1} ${c1y} ${x2} ${c2y} ${x2} ${y2}`;
}

/** Short relative-time label, e.g. "2h", "3d", "5mo". */
function relativeTime(iso) {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return `${secs}s`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d`;
  const weeks = Math.round(days / 7);
  if (weeks < 5) return `${weeks}w`;
  const months = Math.round(days / 30);
  if (months < 12) return `${months}mo`;
  return `${Math.round(days / 365)}y`;
}

/** First name token of an author string, for the compact meta line. */
function shortAuthor(name) {
  const n = String(name || '').trim();
  const sp = n.indexOf(' ');
  return sp > 0 ? n.slice(0, sp) : n;
}

const TAG_ICON =
  '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 7V3.5A1.5 1.5 0 0 1 3.5 2H7l6.5 6.5a1.5 1.5 0 0 1 0 2.1L10.6 13.5a1.5 1.5 0 0 1-2.1 0L2 7Z"/></svg>';

/**
 * Build the BRANCH/TAG label-column markup for a commit's `refs` array, using
 * the `.graph__labels` container + `.graph__label--{head,branch,remote,tag}`
 * chips from the styleguide. The container is always emitted (even when empty)
 * so the column stays aligned across rows.
 */
function labelsHtml(refs) {
  const chips = [];
  for (const raw of refs || []) {
    const ref = String(raw).trim();
    if (!ref) continue;
    let cls = 'graph__label--branch';
    let inner = esc(ref);
    if (ref === 'HEAD') {
      cls = 'graph__label--head';
    } else if (ref.startsWith('HEAD -> ')) {
      cls = 'graph__label--head';
      inner = `HEAD → ${esc(ref.slice('HEAD -> '.length))}`;
    } else if (ref.startsWith('tag: ')) {
      cls = 'graph__label--tag';
      inner = `${TAG_ICON}${esc(ref.slice('tag: '.length))}`;
    } else if (ref.includes('/')) {
      cls = 'graph__label--remote';
    }
    chips.push(`<span class="graph__label ${cls}">${inner}</span>`);
  }
  return `<span class="graph__labels">${chips.join('')}</span>`;
}

/** Local branch names a commit carries (for double-click → checkout branch). */
function localBranches(refs) {
  const out = [];
  for (const raw of refs || []) {
    const ref = String(raw).trim();
    if (!ref || ref === 'HEAD' || ref.startsWith('tag: ') || ref.includes('/')) continue;
    out.push(ref.startsWith('HEAD -> ') ? ref.slice('HEAD -> '.length) : ref);
  }
  return out;
}

/**
 * Assign each commit a lane (column) and compute the SVG paths for its row.
 *
 * Input commits are newest-first (the API's `--date-order`). Returns an array
 * aligned with `commits`, each entry: { col, color, isHead, width, paths }
 * where `paths` is an array of { d, color } and `color` is a 0-based lane index.
 */
function layout(commits) {
  // Active lanes below the current row. Each: { sha, color } or null (free).
  const lanes = [];
  let colorSeq = 0;

  const allocLane = (sha) => {
    const color = colorSeq++ % LANE_COUNT;
    let idx = lanes.indexOf(null);
    if (idx === -1) {
      idx = lanes.length;
      lanes.push(null);
    }
    lanes[idx] = { sha, color };
    return idx;
  };

  const rows = new Array(commits.length);

  for (let r = 0; r < commits.length; r++) {
    const c = commits[r];
    const parents = c.parents || [];

    // Snapshot of lanes entering this row from the top.
    const incoming = lanes.map((l) => (l ? { sha: l.sha, color: l.color } : null));

    // Lanes waiting for this commit (children converging into its node).
    const matchCols = [];
    for (let i = 0; i < lanes.length; i++) {
      if (lanes[i] && lanes[i].sha === c.sha) matchCols.push(i);
    }

    let myCol;
    let nodeColor;
    if (matchCols.length) {
      myCol = matchCols[0];
      nodeColor = lanes[myCol].color;
    } else {
      // Branch tip with no descendant in view — open a fresh lane.
      myCol = allocLane(c.sha);
      nodeColor = lanes[myCol].color;
    }

    // The extra converging lanes terminate at this node.
    for (let k = 1; k < matchCols.length; k++) lanes[matchCols[k]] = null;

    // Route each parent to a lane. First parent prefers to continue in myCol;
    // any parent already awaited elsewhere merges into that existing lane.
    const parentEdges = []; // { col, color }
    const assigned = new Set();
    parents.forEach((p, pi) => {
      let col = -1;
      for (let i = 0; i < lanes.length; i++) {
        if (lanes[i] && lanes[i].sha === p && !assigned.has(i)) {
          col = i;
          break;
        }
      }
      if (col !== -1) {
        assigned.add(col);
        // First parent keeps the node's colour; merges adopt the target lane.
        parentEdges.push({ col, color: pi === 0 ? nodeColor : lanes[col].color });
      } else if (pi === 0) {
        lanes[myCol] = { sha: p, color: nodeColor };
        assigned.add(myCol);
        parentEdges.push({ col: myCol, color: nodeColor });
      } else {
        col = allocLane(p);
        assigned.add(col);
        parentEdges.push({ col, color: lanes[col].color });
      }
    });

    // If the node's own lane wasn't claimed by a parent, it ends here.
    if (!assigned.has(myCol)) lanes[myCol] = null;

    // Trim trailing free lanes to keep the graph as narrow as possible.
    while (lanes.length && lanes[lanes.length - 1] === null) lanes.pop();

    // ---- Build this row's SVG paths --------------------------------------
    const paths = [];

    // Pass-through lanes: lines crossing the row that neither start nor end
    // here. Existing lanes never change column, so these are straight.
    for (let i = 0; i < incoming.length; i++) {
      const inc = incoming[i];
      if (!inc || inc.sha === c.sha) continue;
      const x = laneX(i);
      paths.push({ d: `M ${x} 0 L ${x} ${ROW_H}`, color: inc.color });
    }

    // Top connectors: lanes from above that terminate at this node.
    if (incoming[myCol] && incoming[myCol].sha === c.sha) {
      const x = laneX(myCol);
      paths.push({ d: `M ${x} 0 L ${x} ${MID_Y}`, color: nodeColor });
    }
    for (let k = 1; k < matchCols.length; k++) {
      const i = matchCols[k];
      paths.push({ d: curvePath(laneX(i), 0, laneX(myCol), MID_Y), color: incoming[i].color });
    }

    // Bottom connectors: node down to each parent's lane.
    for (const e of parentEdges) {
      const x1 = laneX(myCol);
      const x2 = laneX(e.col);
      const d = e.col === myCol
        ? `M ${x1} ${MID_Y} L ${x1} ${ROW_H}`
        : curvePath(x1, MID_Y, x2, ROW_H);
      paths.push({ d, color: e.color });
    }

    const width = Math.max(incoming.length, lanes.length, myCol + 1) * LANE_W;
    rows[r] = { col: myCol, color: nodeColor, isHead: !!c.is_head, width, paths };
  }

  return rows;
}

/** Render one commit row to an HTML string. */
function rowHtml(commit, lay, index) {
  const w = Math.max(lay.width, LANE_W);
  const edges = lay.paths
    .map((p) => `<path class="graph__edge graph__lane-${p.color + 1}" d="${p.d}"/>`)
    .join('');
  const node =
    `<circle class="graph__node graph__lane-${lay.color + 1}${lay.isHead ? ' is-head' : ''}" ` +
    `cx="${laneX(lay.col)}" cy="${MID_Y}" r="${NODE_R}"/>`;

  const meta = [shortAuthor(commit.author), relativeTime(commit.date)]
    .filter(Boolean)
    .join(' · ');

  return (
    `<div class="graph__row" role="treeitem" data-i="${index}" aria-selected="false">` +
    `<svg class="graph__lanes" width="${w}" height="${ROW_H}" viewBox="0 0 ${w} ${ROW_H}" aria-hidden="true">` +
    `${edges}${node}</svg>` +
    labelsHtml(commit.refs) +
    `<div class="graph__content">` +
    `<span class="graph__sha">${esc(commit.short || (commit.sha || '').slice(0, 7))}</span>` +
    `<span class="graph__subject">${esc(commit.subject)}</span>` +
    `<span class="graph__meta">${esc(meta)}</span>` +
    `</div></div>`
  );
}

/** The top "uncommitted changes" (WIP) row, shown when the working tree is dirty. */
function wipRowHtml() {
  const w = LANE_W;
  const x = laneX(0);
  const svg =
    `<svg class="graph__lanes" width="${w}" height="${ROW_H}" viewBox="0 0 ${w} ${ROW_H}" aria-hidden="true">` +
    `<path class="graph__edge graph__lane-1" d="M ${x} ${MID_Y} L ${x} ${ROW_H}"/>` +
    `<circle class="graph__node graph__lane-1 is-head" cx="${x}" cy="${MID_Y}" r="${NODE_R}"/></svg>`;
  return (
    `<div class="graph__row" role="treeitem" data-wip="1" aria-selected="false">` +
    `${svg}` +
    `<span class="graph__labels"><span class="graph__label graph__label--head">WIP</span></span>` +
    `<div class="graph__content">` +
    `<span class="graph__subject">Uncommitted changes</span>` +
    `<span class="graph__meta">working tree</span>` +
    `</div></div>`
  );
}

/**
 * Create a commit-graph renderer bound to `containerEl` (the `.graph` element,
 * e.g. `#commit-graph`).
 *
 * Options:
 *   onSelect(commit, index) — called when a row is clicked/selected.
 *   limit                   — passed through to api.getGraph(path, limit).
 *
 * Returns { render(path), clear() }.
 */
export function createGraph(containerEl, options = {}) {
  if (!containerEl) throw new Error('createGraph: containerEl is required');
  const { onSelect, onActivate, onContext, onWip, limit } = options;

  let commits = [];
  let selectedRow = null;
  let filterQuery = '';

  function clear() {
    commits = [];
    selectedRow = null;
    containerEl.replaceChildren();
  }

  // ---- Client-side filter (message / author / short-sha) -------------------

  function rowMatches(commit, q) {
    if (!q) return true;
    const hay = [
      commit.subject || '',
      commit.author || '',
      commit.short || '',
      commit.sha || '',
    ].join(' ').toLowerCase();
    return hay.includes(q);
  }

  // Hide non-matching rows; the WIP row always stays visible.
  function applyFilter() {
    const q = filterQuery.trim().toLowerCase();
    const rows = containerEl.querySelectorAll('.graph__row');
    rows.forEach((row) => {
      if (row.dataset.wip === '1') return;
      const c = commitFor(row);
      row.hidden = !!c && !rowMatches(c, q);
    });
  }

  /** Set the active filter query and (re)apply it to the rendered rows. */
  function setFilter(q) {
    filterQuery = q || '';
    applyFilter();
  }

  function markSelected(row) {
    if (row === selectedRow) return;
    if (selectedRow) {
      selectedRow.classList.remove('is-selected');
      selectedRow.setAttribute('aria-selected', 'false');
    }
    selectedRow = row;
    if (row) {
      row.classList.add('is-selected');
      row.setAttribute('aria-selected', 'true');
    }
  }

  function commitFor(row) {
    if (!row || row.dataset.i == null) return null;
    return commits[Number(row.dataset.i)] || null;
  }

  function select(row) {
    if (!row) return;
    markSelected(row);
    if (row.dataset.wip === '1') {
      if (typeof onWip === 'function') onWip();
      return;
    }
    const c = commitFor(row);
    if (c && typeof onSelect === 'function') onSelect(c, Number(row.dataset.i));
  }

  // Delegated listeners for the whole graph — cheap for ~500 rows.
  containerEl.addEventListener('click', (ev) => {
    const row = ev.target.closest('.graph__row');
    if (row && containerEl.contains(row)) select(row);
  });

  containerEl.addEventListener('dblclick', (ev) => {
    const row = ev.target.closest('.graph__row');
    if (!row || !containerEl.contains(row) || row.dataset.wip === '1') return;
    const c = commitFor(row);
    if (c && typeof onActivate === 'function') {
      onActivate(c, { branches: localBranches(c.refs) });
    }
  });

  containerEl.addEventListener('contextmenu', (ev) => {
    const row = ev.target.closest('.graph__row');
    if (!row || !containerEl.contains(row) || row.dataset.wip === '1') return;
    const c = commitFor(row);
    if (c && typeof onContext === 'function') {
      ev.preventDefault();
      markSelected(row);
      if (typeof onSelect === 'function') onSelect(c, Number(row.dataset.i));
      onContext(c, ev.clientX, ev.clientY, { branches: localBranches(c.refs) });
    }
  });

  async function render(path, { dirty = false } = {}) {
    const { commits: data } = await api.getGraph(path, limit);
    commits = Array.isArray(data) ? data : [];
    selectedRow = null;

    if (!commits.length && !dirty) {
      containerEl.innerHTML =
        '<div class="text-muted" style="padding:var(--space-4)">No commits to display.</div>';
      return;
    }

    // Compute all geometry up front, then write the DOM in a single pass to
    // avoid per-row layout thrash.
    const rows = layout(commits);
    let html = dirty ? wipRowHtml() : '';
    for (let i = 0; i < commits.length; i++) html += rowHtml(commits[i], rows[i], i);
    containerEl.innerHTML = html;

    // Auto-select HEAD (or the first row) so details show immediately.
    const headIdx = commits.findIndex((c) => c.is_head);
    const startRow = containerEl.querySelector(
      `.graph__row[data-i="${headIdx >= 0 ? headIdx : 0}"]`,
    );
    if (startRow) select(startRow);

    // Re-apply any active filter to the freshly-rendered rows.
    applyFilter();
  }

  return { render, clear, setFilter };
}
