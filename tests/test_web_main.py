"""Unit tests for the web entry point's tool-selection parser.

Covers the pure ``_selected_tools`` helper that turns the ``GITAUTO_TOOL`` env
value into a tool-selection list for ``create_app(tools=...)``. Server startup
is intentionally out of scope here (loopback/bind behavior lives in
``tests/test_slice3_security.py``); this file only exercises the parser so it
stays fast and independent of whether ``create_app(tools=...)`` is wired yet.
"""

from __future__ import annotations

import pytest

from git_automation.web import __main__ as web_main


@pytest.mark.parametrize("raw", [None, "", "   ", ",", " , , "])
def test_selected_tools_empty_means_all(raw: str | None) -> None:
    """Unset/empty/all-blank -> None, i.e. mount every tool (default)."""
    assert web_main._selected_tools(raw) is None


def test_selected_tools_single() -> None:
    assert web_main._selected_tools("git") == ["git"]
    assert web_main._selected_tools("github") == ["github"]


def test_selected_tools_multiple_preserves_order() -> None:
    assert web_main._selected_tools("git,github") == ["git", "github"]
    assert web_main._selected_tools("github,git") == ["github", "git"]


def test_selected_tools_trims_whitespace() -> None:
    assert web_main._selected_tools("  git ,  github ") == ["git", "github"]


def test_selected_tools_drops_empty_segments() -> None:
    assert web_main._selected_tools("git,,github") == ["git", "github"]
    assert web_main._selected_tools(",git,") == ["git"]
    assert web_main._selected_tools("git, ,github") == ["git", "github"]


def test_selected_tools_does_not_dedupe_or_validate() -> None:
    """The parser is purely syntactic: validation is create_app's job."""
    assert web_main._selected_tools("git,git") == ["git", "git"]
    assert web_main._selected_tools("bogus") == ["bogus"]
