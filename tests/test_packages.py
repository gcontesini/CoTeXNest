"""Tests for the package/template catalog service and its router."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from webapp import config
from webapp.models import CatalogEntry, InstalledPackage
from webapp.routers import packages as packages_router
from webapp.services import package_manager

_FIXTURE_CATALOG: list[dict[str, object]] = [
    {
        "id": "fixture-pkg",
        "name": "Fixture Package",
        "description": "A tiny fixture style file for tests.",
        "kind": "package",
        "tags": ["fixture", "testing"],
        "files": [
            {"name": "fixture-pkg.sty", "content": "% fixture sty\n"},
        ],
    },
    {
        "id": "fixture-template",
        "name": "Fixture Template",
        "description": "A tiny fixture document template for tests.",
        "kind": "template",
        "tags": ["fixture", "template"],
        "files": [
            {"name": "main.tex", "content": "\\documentclass{article}\n"},
        ],
    },
]


@pytest.fixture
def plugins_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Point plugins paths and the catalog at isolated temp fixtures."""
    plugins_root: Path = tmp_path / "plugins"
    catalog_path: Path = tmp_path / "catalog.json"

    catalog_path.write_text(
        json.dumps(_FIXTURE_CATALOG, indent=2),
        encoding="utf-8",
    )

    monkeypatch.setattr(config, "PLUGINS_ROOT", plugins_root)
    monkeypatch.setattr(
        config, "PLUGINS_MANIFEST_PATH", plugins_root / "manifest.json"
    )
    monkeypatch.setattr(config, "CATALOG_PATH", catalog_path)

    package_manager.load_catalog.cache_clear()
    yield plugins_root
    package_manager.load_catalog.cache_clear()


@pytest.fixture
def api_client() -> TestClient:
    """Build a minimal FastAPI app wrapping only the packages router."""
    app: FastAPI = FastAPI()
    app.include_router(packages_router.router)
    return TestClient(app)


# ─── search_catalog ───


def test_search_catalog_matches_name_substring(plugins_env: Path) -> None:
    results: list[CatalogEntry] = package_manager.search_catalog(
        query="Fixture Pack"
    )

    assert [entry.id for entry in results] == ["fixture-pkg"]


def test_search_catalog_matches_tag_substring(plugins_env: Path) -> None:
    results: list[CatalogEntry] = package_manager.search_catalog(
        query="testing"
    )

    assert [entry.id for entry in results] == ["fixture-pkg"]


def test_search_catalog_filters_by_kind(plugins_env: Path) -> None:
    results: list[CatalogEntry] = package_manager.search_catalog(
        kind="template"
    )

    assert [entry.id for entry in results] == ["fixture-template"]


def test_search_catalog_empty_query_returns_all(plugins_env: Path) -> None:
    results: list[CatalogEntry] = package_manager.search_catalog(query=None)

    assert len(results) == 2


# ─── install_from_catalog ───


def test_install_from_catalog_writes_files_and_lists(
    plugins_env: Path,
) -> None:
    installed: InstalledPackage = package_manager.install_from_catalog(
        "fixture-pkg"
    )

    assert installed.id == "fixture-pkg"
    assert installed.files == ["fixture-pkg.sty"]
    written_file: Path = plugins_env / "fixture-pkg" / "fixture-pkg.sty"
    assert written_file.is_file()

    listed: list[InstalledPackage] = package_manager.list_installed()
    assert [entry.id for entry in listed] == ["fixture-pkg"]


def test_install_from_catalog_is_idempotent(plugins_env: Path) -> None:
    package_manager.install_from_catalog("fixture-pkg")
    package_manager.install_from_catalog("fixture-pkg")

    listed: list[InstalledPackage] = package_manager.list_installed()
    assert len(listed) == 1


def test_install_from_catalog_rejects_template(plugins_env: Path) -> None:
    with pytest.raises(ValueError):
        package_manager.install_from_catalog("fixture-template")


def test_install_from_catalog_unknown_id_raises(plugins_env: Path) -> None:
    with pytest.raises(LookupError):
        package_manager.install_from_catalog("does-not-exist")


# ─── add_local_package ───


def test_add_local_package_disallowed_extension_raises(
    plugins_env: Path,
) -> None:
    with pytest.raises(ValueError):
        package_manager.add_local_package(
            name="My Package",
            filename="evil.exe",
            content="whatever",
        )


def test_add_local_package_allowed_extension_succeeds(
    plugins_env: Path,
) -> None:
    installed: InstalledPackage = package_manager.add_local_package(
        name="My Package",
        filename="my-package.sty",
        content="% hello\n",
    )

    assert installed.kind == "package"
    written_file: Path = (
        plugins_env / installed.id / "my-package.sty"
    )
    assert written_file.is_file()
    assert written_file.read_text(encoding="utf-8") == "% hello\n"


def test_add_local_package_same_name_twice_is_unique(
    plugins_env: Path,
) -> None:
    first: InstalledPackage = package_manager.add_local_package(
        name="My Package",
        filename="my-package.sty",
        content="% first\n",
    )
    second: InstalledPackage = package_manager.add_local_package(
        name="My Package",
        filename="my-package.sty",
        content="% second\n",
    )

    assert first.id != second.id

    listed: list[InstalledPackage] = package_manager.list_installed()
    assert len(listed) == 2


# ─── remove_package ───


def test_remove_package_deletes_files_and_manifest_entry(
    plugins_env: Path,
) -> None:
    package_manager.install_from_catalog("fixture-pkg")
    package_dir: Path = plugins_env / "fixture-pkg"
    assert package_dir.is_dir()

    package_manager.remove_package("fixture-pkg")

    assert not package_dir.exists()
    assert package_manager.list_installed() == []


def test_remove_package_unknown_id_raises(plugins_env: Path) -> None:
    with pytest.raises(LookupError):
        package_manager.remove_package("does-not-exist")


# ─── router end-to-end ───


def test_router_install_list_remove_flow(
    plugins_env: Path,
    api_client: TestClient,
) -> None:
    install_response = api_client.post(
        "/api/packages/install",
        json={"catalog_id": "fixture-pkg"},
    )
    assert install_response.status_code == 200
    assert install_response.json()["id"] == "fixture-pkg"

    list_response = api_client.get("/api/packages/installed")
    assert list_response.status_code == 200
    assert [entry["id"] for entry in list_response.json()] == [
        "fixture-pkg"
    ]

    delete_response = api_client.delete("/api/packages/fixture-pkg")
    assert delete_response.status_code == 200
    assert delete_response.json() == {"success": True}

    list_after_delete = api_client.get("/api/packages/installed")
    assert list_after_delete.json() == []


def test_router_install_unknown_id_returns_404(
    plugins_env: Path,
    api_client: TestClient,
) -> None:
    response = api_client.post(
        "/api/packages/install",
        json={"catalog_id": "does-not-exist"},
    )

    assert response.status_code == 404


def test_router_install_template_returns_400(
    plugins_env: Path,
    api_client: TestClient,
) -> None:
    response = api_client.post(
        "/api/packages/install",
        json={"catalog_id": "fixture-template"},
    )

    assert response.status_code == 400


def test_router_remove_unknown_id_returns_404(
    plugins_env: Path,
    api_client: TestClient,
) -> None:
    response = api_client.delete("/api/packages/does-not-exist")

    assert response.status_code == 404


def test_router_add_local_disallowed_extension_returns_400(
    plugins_env: Path,
    api_client: TestClient,
) -> None:
    response = api_client.post(
        "/api/packages/add-local",
        json={
            "name": "Evil",
            "filename": "evil.exe",
            "content": "whatever",
        },
    )

    assert response.status_code == 400


def test_router_catalog_search_and_kind_filter(
    plugins_env: Path,
    api_client: TestClient,
) -> None:
    response = api_client.get("/api/packages/catalog", params={"kind": "package"})

    assert response.status_code == 200
    body: list[dict[str, object]] = response.json()
    assert [entry["id"] for entry in body] == ["fixture-pkg"]
