"""API tests for the filesystem browser endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from git_automation.web.app import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(), base_url="http://127.0.0.1")


def test_fs_home(client: TestClient) -> None:
    resp = client.get("/api/fs/home")
    assert resp.status_code == 200
    assert resp.json()["path"] == str(Path.home())


def test_fs_list(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / ".git").mkdir()
    (tmp_path / "note.txt").write_text("x")

    resp = client.get("/api/fs/list", params={"path": str(tmp_path)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == str(tmp_path)
    assert body["parent"] == str(tmp_path.parent)
    names = [e["name"] for e in body["entries"]]
    assert names == ["alpha", "repo"]
    by_name = {e["name"]: e for e in body["entries"]}
    assert by_name["repo"]["is_git_repo"] is True
    assert by_name["alpha"]["is_git_repo"] is False


def test_fs_list_invalid_path_error_shape(client: TestClient) -> None:
    resp = client.get("/api/fs/list", params={"path": "/nope/not/here-xyz"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_path"
