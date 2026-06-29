# Git Automation — Design & Plan

**Date:** 2026-06-29
**Status:** Design approved, pre-implementation
**Owner:** Khaled9013

---

## 1. Vision

A GUI-based tool that lets me control, send, and read everything I need from
GitHub through `git` and `gh`, **and** orchestrate AI agents to do real work
based on what lives on GitHub (issues, tasks, PRs).

Two big halves, built in order:

1. **GitHub layer** (first, fully shippable on its own) — a desktop app *and* a
   web UI that together act as a full git client (think GitKraken) plus a GitHub
   notification/issue/repo cockpit, so I never have to manually open github.com
   to find out an issue changed, a task was assigned, or I was mentioned.
2. **Agentic workflow** (second, web-only) — a manager agent reads a GitHub
   issue, decomposes it into tasks, and dispatches specialized sub-agents
   (frontend, API, database, encryption/security, reviewer) that do the work and
   report back as commits/PRs/comments. Task→agent routing is driven by a
   **skill tree** — a visual, GitKraken-style graph of agent capabilities.

### Motivating example

I file an issue for **"WebGate"** (a service that stores websites and reroutes
users to them). The manager agent reads the issue automatically via `gh`,
decomposes it, and dispatches ~5 sub-agents:

- one builds the **frontend**,
- one builds the **APIs**,
- one builds the **database**,
- one handles **encryption / safe data handling**,
- one **tests** it and verifies it follows regulations, meets coding standards,
  and has no data-handling vulnerabilities.

All of this runs automatically, and a live dashboard shows the run.

---

## 2. Design Principles

- **Modular throughout.** `git` operations, `gh` operations, the
  polling/notification engine, agent orchestration, the skill tree, and the UI
  are each separate, independently-testable modules with clean interfaces.
- **One shared core, multiple frontends.** Domain logic lives in `core/` and
  knows nothing about UI. The web UI and the desktop app are two delivery
  methods over the *same* frontend, not two codebases.
- **Ship Phase 1 standalone.** The GitHub layer must be genuinely useful with
  zero agentic features present.
- **Lean on existing auth.** Reuse my existing `gh auth` / local git config — no
  bespoke token storage in Phase 1.
- **YAGNI.** Build only what each phase needs.

---

## 3. Tooling & Stack

| Concern            | Choice                                                        |
| ------------------ | ------------------------------------------------------------- |
| Language           | Python                                                        |
| Env / packaging    | **uv**                                                        |
| Task runner        | **make** (`make setup`, `make web`, `make desktop`, `make test`, `make lint`) |
| Git operations     | local `git` (via `git` CLI / a thin wrapper)                  |
| GitHub operations  | `gh` CLI, incl. `gh api` for REST/GraphQL where the CLI is thin |
| Web backend        | FastAPI (HTTP + WebSocket for live updates)                   |
| Web frontend       | Browser UI; graph rendering via **Cytoscape.js**             |
| Desktop shell      | **pywebview** wrapping the web frontend + system tray + OS notifications |
| Agent backend      | **Claude Agent SDK** (Claude Opus/Sonnet)                     |
| Lint / format / test | ruff + pytest                                               |

---

## 4. Architecture

A single uv-managed monorepo, layered so each module is isolated and testable.

```
git_automation/
├── core/          # domain logic, no UI knowledge
│   ├── git/       # local git client: commit, push, pull, fetch, branch,
│   │              #   merge, stage/unstage, diff, log/history graph, clone
│   ├── gh/        # GitHub via gh: issues, repos, PRs, comments, labels,
│   │              #   notifications, create repo
│   ├── sync/      # polling engine: diff against last-seen state, emit events
│   └── models/    # typed data models (Repo, Issue, PR, Task, Notification...)
├── web/           # FastAPI backend + browser UI (also hosts agentic dashboard)
├── desktop/       # pywebview shell + system tray + OS notifications + poller
├── skilltree/     # capability graph: model, storage, API, visualization data
├── agents/        # Claude Agent SDK orchestration (manager + sub-agents)
├── docs/
├── Makefile
└── pyproject.toml
```

**Module boundaries (each answers: what it does / how to use it / what it depends on):**

- `core/git` — wraps local git. Used by the UI and (later) agents to manage
  working trees. Depends on the `git` CLI only.
- `core/gh` — wraps `gh`. Reads/writes issues, repos, PRs, notifications.
  Depends on `gh` (and its auth).
- `core/sync` — periodically queries `core/gh`, diffs against stored last-seen
  state, and emits typed events (e.g. `IssueAssigned`, `Mentioned`,
  `ReviewRequested`). Depends on `core/gh` + a small local state store.
- `web` — exposes `core` over HTTP/WebSocket and serves the UI. Depends on `core`.
- `desktop` — reuses the web frontend, adds tray + OS notifications + a
  background poller. Depends on `web` (embedded) + `core/sync`.
- `skilltree` — owns the capability graph; serves graph data to the UI and
  routing decisions to `agents`. Depends on `core/models`.
- `agents` — manager + sub-agents. Depends on `core`, `skilltree`, Claude Agent SDK.

---

## 5. Phase 1 — GitHub layer (the shippable core)

**Goal:** a full git client + GitHub cockpit, as a desktop app *and* a web UI,
that removes the need to manually check github.com.

### 5.0 Cross-cutting requirements (apply to all of Phase 1)

- **Shippable to other people.** No identity is hard-coded. On startup the app
  checks for a configured git user (`user.name` / `user.email`) and an
  authenticated `gh` session. If either is missing, it shows a **first-run
  onboarding prompt** to set the git identity and to authenticate `gh` (kicking
  off / guiding `gh auth login`), then proceeds.
- **Modular & readable.** Clear module boundaries, small focused files, typed
  interfaces — so future edits are easy.
- **Performance-first.** Non-blocking subprocess calls, cached/conditional
  GitHub requests, no UI jank.
- **Looks good — dark theme.** A cohesive dark color palette and a polished,
  modern visual design across the whole UI.

### 5.1 Git client features (GitKraken-style)

- Clone / open local repositories.
- Stage / unstage, view diffs.
- Commit.
- **Push / Pull / Fetch with remote selection.** Pressing Push (or Pull /
  Fetch) discovers the repo's configured remotes and lets the user **pick which
  remote** to act on (defaulting sensibly to the branch's upstream / `origin`).
- Create / switch / merge branches.
- Commit history view (graph).

### 5.2 GitHub features (`gh`)

- List & read issues across my repos; comment, label, assign, open, close.
- Create issues and **create repos**.
- View PRs and their status; comment / review.
- An **activity feed** of recent changes.

### 5.3 Notifications (the core pain-killer)

A poller diffs current GitHub state against last-seen state and raises
**desktop + in-app notifications** when:

- a **task is assigned to me**,
- I am **@mentioned anywhere** (issue, PR, comment),
- an **issue is assigned to me**,
- a **review is requested** from me,
- (extensible to: issue updated, new comment on something I follow, etc.).

Desktop notifications are OS-native (via the pywebview shell + system tray);
the web UI shows the same events in an in-app feed/badge.

**Polling cadence — as real-time as GitHub allows:** use GitHub's notifications
API with **conditional requests** (ETag / `Last-Modified`) and honor the
`X-Poll-Interval` header GitHub returns (typically ~60s). This gives
near-real-time updates while staying cheap and within rate limits. If that path
is unavailable (e.g. a query that doesn't expose `X-Poll-Interval`), **fall back
to a fixed 5-minute poll**. The interval remains configurable, but never polls
faster than GitHub's advertised minimum.

### 5.4 Delivery

- **Web:** `make web` → FastAPI serves the browser UI.
- **Desktop:** `make desktop` → pywebview window wrapping the same UI, with tray
  + background poller running even when the window is closed.

### 5.5 Phase 1 success criteria

I can do my daily git + GitHub work (commit/push/pull, read/answer issues,
create repos) from this tool, and I get notified of assignments/mentions without
opening github.com.

---

## 6. Phase 2 — Skill tree

A directed graph of **agent capabilities**, rendered as an interactive
skill-tree visualization in the web UI.

- **Nodes:** skills. **Edges:** specialization / linkage.
- Example hierarchy: `Main → {managerial, accounting, marketing, coding,
  engineering, law, …} → coding → {python, c, java, ruby} → python →
  data-analysis → graphs`.
- **Storage:** JSON or SQLite, served via the web API.
- **Rendering:** Cytoscape.js skill-tree view in the browser.
- Each **leaf skill** maps to a capability a sub-agent can claim — this is what
  the agentic layer uses for routing.

**Success criteria:** I can browse the tree, see how skills link, and each leaf
is bound to an agent capability.

---

## 7. Phase 3 — Agentic workflow (web-only)

Built on the **Claude Agent SDK**.

- **Manager agent:** reads a GitHub issue (via `core/gh`), decomposes it into a
  task plan, and assigns each task to the best-matched capability using the
  **skill tree**.
- **Sub-agents:** specialized workers (e.g. frontend, API, database,
  encryption/security, reviewer-for-standards-and-vulnerabilities). Each picks
  up its task, works in a git working tree (via `core/git`), and reports back.
- **Reporting:** results land as commits / PRs / issue comments on the
  originating issue.
- **Live dashboard:** the web UI visualizes the run — which agent has which
  task, status, and outputs.

**Success criteria:** filing an issue like the WebGate example triggers an
automatic decomposition and produces sub-agent contributions back on the issue.

---

## 8. Build Order

| Phase | Deliverable                                                        | Shippable? |
| ----- | ------------------------------------------------------------------ | ---------- |
| **0** | Scaffolding: uv project, Makefile, repo layout, ruff + pytest, CI  | n/a        |
| **1** | GitHub layer: `core/git` + `core/gh` + `core/sync` + web UI + desktop app + notifications | **Yes**    |
| **2** | Skill tree: model + storage + API + Cytoscape visualization        | Yes        |
| **3** | Agentic workflow: manager + sub-agents (Claude Agent SDK), wired to skill tree, live dashboard | Yes |

Each phase gets its own spec → implementation plan → build cycle. This document
is the umbrella design; Phase 1 will be the first to get a detailed
implementation plan.

---

## 9. Open Questions (to resolve before/within each phase)

- Phase 1: how much of the git history *graph* to render in v1 vs. a simpler
  linear log first.
- Phase 2: JSON vs. SQLite for the skill tree, and whether the tree is editable
  in-UI or file-defined.
- Phase 3: how sub-agents share/isolate working trees (e.g. git worktrees), and
  the review/merge gate before anything is pushed.
