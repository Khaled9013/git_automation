"""Tests for the composable integration host (:mod:`git_automation.integration`).

Covers the tool registry, the ``create_app(tools=...)`` composition, per-tool
route selection, the shared ``identity`` router, and the fact that only the
``github`` tool's lifespan starts the notification poller.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from git_automation.integration import ToolModule, available_tools
from git_automation.integration import tools as tools_mod
from git_automation.web.app import create_app

# A git-only route, a github-only route, and a shared-identity route. These are
# stable public URLs -- the host must not change them under composition.
GIT_ROUTE = "/api/repo"
GITHUB_ROUTE = "/api/github/notifications"
IDENTITY_ROUTE = "/api/identity"


def _served_paths(app) -> set[str]:
    """The set of URL paths the app serves, via its OpenAPI schema."""
    return set(app.openapi()["paths"].keys())


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_available_tools_returns_expected_two_tools() -> None:
    tools = available_tools()
    assert set(tools) == {"git", "github"}
    assert tools["git"].name == "git"
    assert tools["github"].name == "github"
    assert all(isinstance(t, ToolModule) for t in tools.values())


def test_git_tool_has_no_lifespan_github_tool_does() -> None:
    tools = available_tools()
    assert tools["git"].lifespan is None
    assert tools["github"].lifespan is not None


def test_available_tools_is_freshly_built_each_call() -> None:
    # Callers must not be able to mutate shared state across calls.
    first = available_tools()
    first["git"] = ToolModule(name="tampered", router=first["github"].router)
    second = available_tools()
    assert second["git"].name == "git"


# --------------------------------------------------------------------------- #
# Composition: which routes each selection serves
# --------------------------------------------------------------------------- #


def test_all_tools_serves_git_and_github_and_identity() -> None:
    paths = _served_paths(create_app())
    assert GIT_ROUTE in paths
    assert GITHUB_ROUTE in paths
    assert IDENTITY_ROUTE in paths
    assert "/health" in paths


def test_git_only_serves_git_not_github() -> None:
    paths = _served_paths(create_app(tools=["git"]))
    assert GIT_ROUTE in paths
    assert IDENTITY_ROUTE in paths  # shared, always mounted
    assert GITHUB_ROUTE not in paths


def test_github_only_serves_github_not_git() -> None:
    paths = _served_paths(create_app(tools=["github"]))
    assert GITHUB_ROUTE in paths
    assert IDENTITY_ROUTE in paths  # shared, always mounted
    assert GIT_ROUTE not in paths


def test_git_only_returns_404_for_github_route() -> None:
    # No `with`: we assert routing, not lifespan, so don't start the poller.
    client = TestClient(create_app(tools=["git"]), base_url="http://127.0.0.1")
    assert client.get(GITHUB_ROUTE).status_code == 404


def test_github_only_returns_404_for_git_route() -> None:
    client = TestClient(create_app(tools=["github"]), base_url="http://127.0.0.1")
    assert client.get(GIT_ROUTE).status_code == 404


def test_empty_selection_still_mounts_shared_identity() -> None:
    paths = _served_paths(create_app(tools=[]))
    assert IDENTITY_ROUTE in paths
    assert GIT_ROUTE not in paths
    assert GITHUB_ROUTE not in paths


def test_unknown_tool_raises_value_error() -> None:
    with pytest.raises(ValueError):
        create_app(tools=["does-not-exist"])


# --------------------------------------------------------------------------- #
# Lifespan: only the github tool starts the notifier poller
# --------------------------------------------------------------------------- #


@pytest.fixture
def poller_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """Patch the notifier so we can observe whether the poller was launched.

    ``auth_status`` is forced True so an authenticated environment isn't
    required; ``run_poller`` records that it started and then blocks until
    cancelled (mimicking the real long-running loop, so clean cancel is
    exercised on shutdown).
    """
    spy = {"auth_checked": False, "poller_started": False}

    async def fake_auth_status() -> tuple[bool, None]:
        spy["auth_checked"] = True
        return True, None

    async def fake_run_poller(**_kwargs: object) -> None:
        spy["poller_started"] = True
        await asyncio.Event().wait()  # block until the lifespan cancels us

    monkeypatch.setattr(tools_mod.gh_cli, "auth_status", fake_auth_status)
    monkeypatch.setattr(tools_mod.notifier, "run_poller", fake_run_poller)
    return spy


def test_all_tools_starts_notifier(poller_spy: dict[str, bool]) -> None:
    with TestClient(create_app(), base_url="http://127.0.0.1"):
        pass
    assert poller_spy["poller_started"] is True


def test_github_only_starts_notifier(poller_spy: dict[str, bool]) -> None:
    with TestClient(create_app(tools=["github"]), base_url="http://127.0.0.1"):
        pass
    assert poller_spy["poller_started"] is True


def test_git_only_does_not_start_notifier(poller_spy: dict[str, bool]) -> None:
    with TestClient(create_app(tools=["git"]), base_url="http://127.0.0.1"):
        pass
    assert poller_spy["auth_checked"] is False
    assert poller_spy["poller_started"] is False


def test_notifier_startup_never_crashes_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure while starting the poller must not crash app startup."""

    async def boom() -> tuple[bool, None]:
        raise RuntimeError("gh exploded")

    monkeypatch.setattr(tools_mod.gh_cli, "auth_status", boom)
    # If the guard is missing, entering the context (startup) would raise.
    with TestClient(create_app(tools=["github"]), base_url="http://127.0.0.1") as client:
        assert client.get("/health").status_code == 200
