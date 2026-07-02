"""Tests for the shared web env config and the unified boot path.

The app must behave identically no matter how it is started (``make web``,
``make web-git``/``web-github``, or the ``git-automation`` script): the same
env variables select tools, host, port, and reload. That means:

* ``git_automation.web.config`` owns the env parsing (one source of truth).
* The *module-level* ``web.app.app`` honors ``GITAUTO_TOOL``, so even a raw
  ``uvicorn git_automation.web.app:app`` boot mounts the selected tools.
* ``__main__.main()`` runs uvicorn with the import string (enabling
  ``GITAUTO_RELOAD``) after fail-closed host and tool validation.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest

from git_automation.web import config

# --- parse_tool_selection (moved from __main__._selected_tools) --------------


@pytest.mark.parametrize("raw", [None, "", "  ", ",", " , "])
def test_parse_tool_selection_empty_means_all(raw: str | None) -> None:
    assert config.parse_tool_selection(raw) is None


def test_parse_tool_selection_splits_and_trims() -> None:
    assert config.parse_tool_selection("  git ,  github ") == ["git", "github"]
    assert config.parse_tool_selection("git,,github") == ["git", "github"]


def test_tools_from_env_reads_gitauto_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITAUTO_TOOL", "git")
    assert config.tools_from_env() == ["git"]
    monkeypatch.delenv("GITAUTO_TOOL")
    assert config.tools_from_env() is None


# --- reload flag --------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_reload_from_env_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("GITAUTO_RELOAD", value)
    assert config.reload_from_env() is True


@pytest.mark.parametrize("value", [None, "", "0", "false"])
def test_reload_from_env_falsy(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("GITAUTO_RELOAD", raising=False)
    else:
        monkeypatch.setenv("GITAUTO_RELOAD", value)
    assert config.reload_from_env() is False


# --- module-level app honors GITAUTO_TOOL -------------------------------------


def test_module_level_app_honors_gitauto_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raw ``uvicorn web.app:app`` boot must mount only the selected tools."""
    import git_automation.web.app as app_module

    monkeypatch.setenv("GITAUTO_TOOL", "git")
    try:
        reloaded = importlib.reload(app_module)
        paths = set(reloaded.app.openapi()["paths"])
        assert "/api/repo" in paths
        assert "/api/github/notifications" not in paths
    finally:
        monkeypatch.delenv("GITAUTO_TOOL", raising=False)
        restored = importlib.reload(app_module)
        paths = set(restored.app.openapi()["paths"])
        assert "/api/github/notifications" in paths  # default: all tools


# --- main(): unified, guarded boot --------------------------------------------


def _stub_uvicorn(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple, dict]]:
    calls: list[tuple[tuple, dict]] = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(run=run))
    return calls


def test_main_runs_import_string_with_env_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from git_automation.web import __main__ as web_main

    calls = _stub_uvicorn(monkeypatch)
    monkeypatch.delenv("GITAUTO_HOST", raising=False)
    monkeypatch.delenv("GITAUTO_TOOL", raising=False)
    monkeypatch.setenv("GITAUTO_PORT", "9001")
    monkeypatch.setenv("GITAUTO_RELOAD", "1")

    web_main.main()

    (args, kwargs) = calls[0]
    assert args == ("git_automation.web.app:app",)
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 9001
    assert kwargs["reload"] is True


def test_main_rejects_unknown_tool_with_clean_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from git_automation.web import __main__ as web_main

    calls = _stub_uvicorn(monkeypatch)
    monkeypatch.delenv("GITAUTO_HOST", raising=False)
    monkeypatch.setenv("GITAUTO_TOOL", "bogus")

    with pytest.raises(SystemExit) as exc:
        web_main.main()
    assert "bogus" in str(exc.value)
    assert calls == []  # never reached uvicorn
