"""Scaffolding smoke tests: the package imports and the web app is wired up."""

from fastapi.testclient import TestClient

from git_automation import __version__
from git_automation.web.app import create_app


def test_package_has_version():
    assert __version__


def test_health_endpoint():
    client = TestClient(create_app(), base_url="http://127.0.0.1")
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
