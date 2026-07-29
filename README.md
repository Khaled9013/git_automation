# git-automation

A GUI git client + GitHub cockpit (desktop **and** web) that removes the need to
manually check github.com, with an agentic workflow layered on top.

See the full design: [`docs/superpowers/specs/2026-06-29-git-automation-design.md`](docs/superpowers/specs/2026-06-29-git-automation-design.md).

## What it does (by phase)

- **Phase 1 — GitHub layer (shippable):** full git client (commit, push, pull,
  branch, merge, diff, history) plus a `gh` cockpit (issues, repos, PRs) with
  desktop + in-app **notifications** when you're assigned a task, @mentioned
  anywhere, assigned an issue, or asked for a review. Near-real-time polling
  with a 5-minute fallback.
- **Phase 2 — Skill tree:** an interactive graph of agent capabilities.
- **Phase 3 — Agentic workflow (web-only):** a manager agent reads an issue,
  decomposes it, and dispatches sub-agents (frontend, API, database,
  encryption, reviewer) via the skill tree.

## Requirements

- [`uv`](https://docs.astral.sh/uv/), `git`, and the authenticated
  [`gh`](https://cli.github.com/) CLI (`gh auth login`).

## Quick start

```bash
make setup     # create the venv and install dependencies
make web       # run the FastAPI web app  (http://127.0.0.1:8000)
make test      # run the test suite
make check     # lint + test
make help      # list all targets
```

### Access over Tailscale

When a running Tailscale is detected, `make web` also binds the machine's
Tailscale IPs, so the app is reachable from your other tailnet devices —
`http://<tailscale-ip>:8000` or, with MagicDNS, `http://<machine>:8000` /
`http://<machine>.<tailnet>.ts.net:8000`. Only loopback and the machine's own
tailnet identity are served; every other bind host / `Host` / `Origin` is
still refused (see `git_automation/web/security.py`).

`GITAUTO_HOST` controls the binds explicitly (comma-separated; the token
`tailscale` expands to the detected Tailscale IPs):

```bash
GITAUTO_HOST=127.0.0.1 make web              # loopback only (no tailnet)
GITAUTO_HOST=127.0.0.1,tailscale make web    # explicit dual bind
GITAUTO_HOST=tailscale make web              # tailnet only
```
