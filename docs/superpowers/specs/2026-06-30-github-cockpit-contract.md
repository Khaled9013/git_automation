# GitHub Cockpit — Contract (the second tool)

A standalone **GitHub tool** alongside the git cockpit, reachable via a new top
nav. First slice: **Notifications inbox** (mentions / assignments / reviews /
comments) + **Issues** (list, open, read & post comments), with in-app +
**browser OS** alerts. Built on the authenticated `gh` CLI.

**Composable-architecture rule:** all GitHub logic lives in a self-contained
`core/github/` module with a clean, typed Python interface and a `/api/github/*`
HTTP surface. The UI is just one consumer; the future agentic tool consumes the
SAME module/endpoints. No coupling to the git-cockpit code. (See the project's
composable-tools principle.)

Conventions unchanged: FastAPI async JSON, `{"error":{code,message}}`,
`core.process.run_process` (arg lists, no shell), `gh` token never logged.

## Backend — `core/github/` (the exposable interface)

### `core/github/client.py`
```python
async def list_notifications(*, all: bool = False, participating: bool = False) -> list[Notification]
async def mark_notification_read(thread_id: str) -> None
async def list_issues(*, repo: str | None = None, filter: str = "assigned",
                      state: str = "open") -> list[IssueSummary]
    # filter in {"assigned","mentioned","created","all"} -> gh search issues --assignee/@me etc.
async def get_issue(repo: str, number: int) -> IssueDetail        # body + comments
async def add_comment(repo: str, number: int, body: str) -> Comment
async def current_login() -> str                                   # gh api /user -> login
```
- `list_notifications` → `gh api /notifications` (+ `?all`/`?participating`). Map
  each: `reason` (mention/assign/review_requested/comment/...), `subject.title`,
  `subject.type` (Issue/PullRequest), `subject.url`, `repository.full_name`,
  `unread`, `updated_at`, and a derived `number` parsed from `subject.url`.
- `list_issues` → `gh search issues` (`--assignee @me`, `--mentions @me`,
  `--author @me`) or `gh issue list --repo` for a specific repo; `--json`.
- `get_issue` → `gh issue view <n> --repo <r> --json ...,comments`.
- `add_comment` → `gh issue comment <n> --repo <r> --body <body>` (body via a
  temp file / stdin, never shell-interpolated).
- Validate/guard inputs; surface `gh` failures as `GitAutomationError`.

### Models (`core/github/models.py`, pydantic)
`Notification{id,reason,unread,title,subject_type,repo,number,url,updated_at}`,
`IssueSummary{repo,number,title,state,author,assignees:list[str],labels:list[str],
comments:int,updated_at,url}`,
`Comment{author,body,created_at}`,
`IssueDetail{repo,number,title,state,author,body,assignees,labels,comments:list[Comment],url}`.

### `web/api/github.py`  (router, registered under `/api`)
- `GET  /api/github/notifications?all=&participating=` → `list[Notification]`
- `POST /api/github/notifications/{thread_id}/read` → `{ok}`
- `GET  /api/github/issues?repo=&filter=&state=` → `list[IssueSummary]`
- `GET  /api/github/issue?repo=&number=` → `IssueDetail`
- `POST /api/github/issue/comment` body `{repo,number,body}` → `Comment`
- `GET  /api/github/me` → `{login}`

### Alerts / polling
- No new WS needed: the UI **polls** `GET /api/github/notifications` on an
  interval (default 60s; never faster than GitHub's advertised minimum), diffs
  against last-seen ids, and for NEW items whose `reason` is `mention` or
  `assign` fires an alert. (Endpoint is the exposable seam; agents poll it too.)

## Frontend

### Top nav (host shell)
- Add a slim top **app nav** above everything with tabs: **Git** (the existing
  3-pane cockpit), **GitHub** (new), and a disabled **AI** placeholder (future).
- Switching tabs swaps the main view; each view is self-contained. The git
  cockpit becomes the "Git" view unchanged.

### GitHub view (`static/js/github/*` + a `github.html` partial or section)
- **Notifications inbox:** list with a `reason` badge (mention / assigned /
  review / comment), repo + title, unread emphasis, time; click → open the
  thread in the detail pane; "mark read" action.
- **Issues:** filter tabs (Assigned to me / Mentions me / Created by me / All in
  repo) + state (open/closed); list of `IssueSummary`; click → detail.
- **Detail pane:** issue/PR title, state, labels, author, body (markdown ok as
  text), the comment thread, and a **comment box** to reply (`add_comment`).
- **Alerts:** an unread **badge** on the GitHub nav tab; poll every 60s; on new
  mention/assignment, fire a **browser `Notification`** (request permission once,
  gated behind a user toggle) AND an in-app toast. Clicking the OS notification
  focuses the app and opens that thread.
- New `api.js` (or `github/api.js`) functions, one per endpoint above.

## Design-system additions (UI/UX)
Additive: `.appnav` (top tab bar with `.appnav__tab.is-active`, unread badge),
`.gh-list` (notification/issue rows with `__reason--mention/--assign/--review/--comment`,
`.is-unread`), `.gh-detail` (issue header + labels + body + `.gh-comment` thread +
reply box), an `.gh-filter` tab strip. Reuse existing tokens/components.

## Out of scope (later slices)
Creating issues/PRs from the UI, GitHub Discussions, reactions, marking all
read, and the desktop-tray variant of alerts.
