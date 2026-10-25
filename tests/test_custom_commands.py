"""Tests for the global custom commands and their per-project toggle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from webapp import config
from webapp.routers import custom_commands as custom_commands_router
from webapp.services import custom_commands

_MAIN = (
    "\\documentclass{article}\n"
    "\\usepackage{amsmath}\n\n"
    "\\begin{document}\n"
    "Hello\n"
    "\\end{document}\n"
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path / "workspace")
    monkeypatch.setattr(
        config, "CUSTOM_COMMANDS_PATH", tmp_path / "cfg" / "custom_commands.tex"
    )
    (tmp_path / "workspace").mkdir()
    app = FastAPI()
    app.include_router(custom_commands_router.router)
    return TestClient(app)


def _make_project(name: str, main: str = _MAIN) -> str:
    root = config.WORKSPACE_ROOT / name
    root.mkdir()
    (root / "main.tex").write_text(main, encoding="utf-8")
    (root / ".project.json").write_text(
        json.dumps({"name": name, "main_file": "main.tex"}), encoding="utf-8"
    )
    return name


def test_global_commands_default_empty(client: TestClient) -> None:
    assert client.get("/api/custom-commands").json() == {"content": ""}


def test_global_commands_round_trip_verbatim(client: TestClient) -> None:
    text = "\\newcommand{\\R}{\\mathbb{R}} % reals\n"
    client.put("/api/custom-commands", json={"content": text})

    assert client.get("/api/custom-commands").json() == {"content": text}
    assert config.CUSTOM_COMMANDS_PATH.read_text(encoding="utf-8") == text


def test_enable_inserts_loader_before_begin_document(client: TestClient) -> None:
    project = _make_project("p1")
    client.put("/api/custom-commands", json={"content": "\\newcommand{\\R}{R}\n"})

    response = client.put(
        f"/api/projects/{project}/custom-commands", json={"enabled": True}
    )

    assert response.json() == {"enabled": True, "main_file": "main.tex"}
    main = (config.WORKSPACE_ROOT / project / "main.tex").read_text()
    lines = main.splitlines()
    assert lines.index(custom_commands.LOADER_LINE) == lines.index(
        "\\begin{document}"
    ) - 1
    assert (config.WORKSPACE_ROOT / project / "custom-commands.tex").read_text() == (
        "\\newcommand{\\R}{R}\n"
    )


def test_enable_is_idempotent(client: TestClient) -> None:
    project = _make_project("p1")
    url = f"/api/projects/{project}/custom-commands"

    client.put(url, json={"enabled": True})
    client.put(url, json={"enabled": True})

    main = (config.WORKSPACE_ROOT / project / "main.tex").read_text()
    assert main.count(custom_commands.LOADER_MARKER) == 1


def test_disable_restores_main_and_removes_copy(client: TestClient) -> None:
    project = _make_project("p1")
    url = f"/api/projects/{project}/custom-commands"

    client.put(url, json={"enabled": True})
    response = client.put(url, json={"enabled": False})

    assert response.json()["enabled"] is False
    assert (config.WORKSPACE_ROOT / project / "main.tex").read_text() == _MAIN
    assert not (config.WORKSPACE_ROOT / project / "custom-commands.tex").exists()


def test_status_reflects_hand_edited_loader_line(client: TestClient) -> None:
    project = _make_project(
        "p1", _MAIN.replace("\\begin{document}", f"{custom_commands.LOADER_LINE}\n\\begin{{document}}")
    )

    response = client.get(f"/api/projects/{project}/custom-commands")

    assert response.json() == {"enabled": True, "main_file": "main.tex"}


def test_enable_without_begin_document_is_400(client: TestClient) -> None:
    project = _make_project("p1", "\\documentclass{article}\n")

    response = client.put(
        f"/api/projects/{project}/custom-commands", json={"enabled": True}
    )

    assert response.status_code == 400


def test_unknown_project_is_404(client: TestClient) -> None:
    assert client.get("/api/projects/nope/custom-commands").status_code == 404


def test_saving_refreshes_only_enabled_projects(client: TestClient) -> None:
    on = _make_project("on")
    off = _make_project("off")
    client.put("/api/custom-commands", json={"content": "% v1\n"})
    client.put(f"/api/projects/{on}/custom-commands", json={"enabled": True})

    client.put("/api/custom-commands", json={"content": "% v2\n"})

    assert (config.WORKSPACE_ROOT / on / "custom-commands.tex").read_text() == "% v2\n"
    assert not (config.WORKSPACE_ROOT / off / "custom-commands.tex").exists()
