"""Tests for the projects/files router.

Every test patches config.WORKSPACE_ROOT / config.PLUGINS_ROOT /
config.CATALOG_PATH onto a pytest tmp_path so the real workspace on
disk is never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from webapp import config
from webapp.routers import projects
from webapp.services import project_files

_FAKE_CATALOG = [
    {
        "id": "article-template",
        "name": "Simple Article",
        "description": "A minimal article template",
        "kind": "template",
        "tags": ["article"],
        "files": [
            {"name": "main.tex", "content": "% TEMPLATE MAIN\n"},
            {"name": "chapters/intro.tex", "content": "Intro chapter\n"},
        ],
    },
    {
        "id": "some-package",
        "name": "Some Package",
        "description": "not a template, must be ignored by project creation",
        "kind": "package",
        "tags": [],
        "files": [{"name": "some.sty", "content": "% package\n"}],
    },
]


@pytest.fixture
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    workspace_root = tmp_path / "workspace"
    plugins_root = tmp_path / "plugins"
    catalog_path = tmp_path / "catalog.json"

    workspace_root.mkdir()
    plugins_root.mkdir()
    catalog_path.write_text(json.dumps(_FAKE_CATALOG), encoding="utf-8")

    monkeypatch.setattr(config, "WORKSPACE_ROOT", workspace_root)
    monkeypatch.setattr(config, "PLUGINS_ROOT", plugins_root)
    monkeypatch.setattr(config, "CATALOG_PATH", catalog_path)

    app = FastAPI()
    app.include_router(projects.router)
    return TestClient(app)


def test_create_project_default(client: TestClient) -> None:
    response = client.post("/api/projects", json={"name": "My Paper"})
    assert response.status_code == 200

    body = response.json()
    assert body["name"] == "My Paper"
    assert body["main_file"] == "main.tex"
    assert body["id"].startswith("my-paper-")

    project_dir = config.WORKSPACE_ROOT / body["id"]
    main_tex = project_dir / "main.tex"
    assert main_tex.is_file()
    assert "Hello, world!" in main_tex.read_text(encoding="utf-8")
    assert (project_dir / ".project.json").is_file()
    assert (project_dir / ".git").is_dir()


def test_create_project_with_template(client: TestClient) -> None:
    response = client.post(
        "/api/projects",
        json={"name": "Thesis", "template_id": "article-template"},
    )
    assert response.status_code == 200

    project_dir = config.WORKSPACE_ROOT / response.json()["id"]
    assert (project_dir / "main.tex").read_text(
        encoding="utf-8"
    ) == "% TEMPLATE MAIN\n"
    assert (project_dir / "chapters" / "intro.tex").read_text(
        encoding="utf-8"
    ) == "Intro chapter\n"


def test_create_project_with_unknown_template_falls_back_to_default(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/projects",
        json={"name": "No Such Template", "template_id": "nope"},
    )
    assert response.status_code == 200

    project_dir = config.WORKSPACE_ROOT / response.json()["id"]
    assert "Hello, world!" in (project_dir / "main.tex").read_text(
        encoding="utf-8"
    )


def test_list_projects(client: TestClient) -> None:
    client.post("/api/projects", json={"name": "Alpha"})
    client.post("/api/projects", json={"name": "Beta"})

    response = client.get("/api/projects")
    assert response.status_code == 200

    names = {entry["name"] for entry in response.json()}
    assert names == {"Alpha", "Beta"}


def test_read_write_delete_file(client: TestClient) -> None:
    project_id = client.post(
        "/api/projects", json={"name": "Files Project"}
    ).json()["id"]

    write_response = client.put(
        f"/api/projects/{project_id}/files/notes.tex",
        json={"content": "\\section{Notes}"},
    )
    assert write_response.status_code == 200
    assert write_response.json()["content"] == "\\section{Notes}"

    read_response = client.get(f"/api/projects/{project_id}/files/notes.tex")
    assert read_response.status_code == 200
    assert read_response.json()["content"] == "\\section{Notes}"

    tree_response = client.get(f"/api/projects/{project_id}/files")
    assert tree_response.status_code == 200
    file_paths = {
        child["path"] for child in tree_response.json()["children"]
    }
    assert "notes.tex" in file_paths
    assert ".project.json" not in file_paths

    delete_response = client.delete(
        f"/api/projects/{project_id}/files/notes.tex"
    )
    assert delete_response.status_code == 200
    assert delete_response.json() == {"success": True}

    missing_response = client.get(
        f"/api/projects/{project_id}/files/notes.tex"
    )
    assert missing_response.status_code == 404


def test_write_file_creates_nested_directories(client: TestClient) -> None:
    project_id = client.post(
        "/api/projects", json={"name": "Nested"}
    ).json()["id"]

    response = client.put(
        f"/api/projects/{project_id}/files/sections/one.tex",
        json={"content": "Section one"},
    )
    assert response.status_code == 200

    project_dir = config.WORKSPACE_ROOT / project_id
    assert (project_dir / "sections" / "one.tex").read_text(
        encoding="utf-8"
    ) == "Section one"


def test_path_traversal_rejected_on_read(client: TestClient) -> None:
    project_id = client.post(
        "/api/projects", json={"name": "Guarded"}
    ).json()["id"]

    response = client.get(
        f"/api/projects/{project_id}/files/"
        "%2e%2e/%2e%2e/%2e%2e/etc/passwd"
    )
    assert response.status_code == 400


def test_path_traversal_rejected_on_write(client: TestClient) -> None:
    project_id = client.post(
        "/api/projects", json={"name": "Guarded Write"}
    ).json()["id"]

    response = client.put(
        f"/api/projects/{project_id}/files/%2e%2e/%2e%2e/escape.tex",
        json={"content": "malicious"},
    )
    assert response.status_code == 400


def test_resolve_safe_path_rejects_traversal_directly(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "proj"
    project_root.mkdir()

    with pytest.raises(ValueError):
        project_files.resolve_safe_path(project_root, "../../etc/passwd")


def test_resolve_safe_path_allows_root_itself(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    project_root.mkdir()

    resolved = project_files.resolve_safe_path(project_root, ".")
    assert resolved == project_root.resolve()


def test_delete_project(client: TestClient) -> None:
    project_id = client.post(
        "/api/projects", json={"name": "Temp Project"}
    ).json()["id"]
    project_dir = config.WORKSPACE_ROOT / project_id
    assert project_dir.is_dir()

    response = client.delete(f"/api/projects/{project_id}")
    assert response.status_code == 200
    assert response.json() == {"success": True}
    assert not project_dir.exists()


def test_delete_unknown_project_returns_404(client: TestClient) -> None:
    response = client.delete("/api/projects/does-not-exist")
    assert response.status_code == 404


def test_read_unknown_project_returns_404(client: TestClient) -> None:
    response = client.get("/api/projects/does-not-exist/files/main.tex")
    assert response.status_code == 404
