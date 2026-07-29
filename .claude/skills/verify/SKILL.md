---
name: verify
description: Build/launch/drive recipe for verifying git-automation web changes end-to-end
---

# Verifying git-automation

## Launch

```bash
# Isolated port; reload mode is the primary path (make web uses it).
GITAUTO_RELOAD=1 GITAUTO_PORT=8017 uv run python -m git_automation.web
```

Boot is guarded: non-loopback/non-Tailscale `GITAUTO_HOST` exits with a clean
message. With `GITAUTO_HOST` unset the server binds 127.0.0.1 plus the
machine's Tailscale IPs when a tailnet is detected (check the boot log for
one "Uvicorn running on ..." line per bind).

## Drive

- Health: `curl http://127.0.0.1:8017/health` → `{"status":"ok",...}`.
- UI: `GET /` returns the SPA HTML.
- WebSocket handshake (watch/terminal/github-events) via curl:
  `curl -i -H "Connection: Upgrade" -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: x3JJHMbDL1EzLkh9GBhXDw==" "http://127.0.0.1:8017/api/watch?path=$PWD"` → expect `HTTP/1.1 101`.
- Security probes: a foreign `Host:` header must return 400 `invalid_host`;
  a foreign WS `Origin:` must return 403.
- Reload seam: `touch git_automation/web/app.py`, wait ~3s, confirm restart in
  the log and re-curl every bind.

## Gotchas

- Tailscale detection (`web/tailscale.py::self_identity`) shells out to the
  `tailscale` CLI and is lru-cached per process; tests must pin it via
  monkeypatch (see tests/test_tailscale_access.py fixtures).
- `ruff format .` has pre-existing drift in untouched files — format only the
  files you changed.
