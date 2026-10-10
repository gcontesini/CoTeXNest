"""Tests for the projects/files router.

Every test patches config.WORKSPACE_ROOT / config.PLUGINS_ROOT /
config.CATALOG_PATH onto a pytest tmp_path so the real workspace on
disk is never touched.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import git
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from webapp import config
from webapp.routers import projects
from webapp.services import project_files

_GIT_AUTHOR = git.Actor("tester", "tester@localhost")


def _make_source_repo(
    path: Path, files: dict[str, str], with_project_json: dict[str, object] | None = None
) -> Path:
    """Build a real local git repo at ``path``, cloneable via a file path URL."""
    path.mkdir(parents=True)
    repo = git.Repo.init(path)

    for name, content in files.items():
        file_path = path / name
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")

    if with_project_json is not None:
        (path / ".project.json").write_text(
            json.dumps(with_project_json), encoding="utf-8"
        )

    repo.index.add(["."])
    repo.index.commit("Initial commit", author=_GIT_AUTHOR, committer=_GIT_AUTHOR)
    return path

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


# ─── _validate_clone_url: scheme allowlist + SSRF guard ───


@pytest.mark.parametrize(
    "remote_url",
    [
        "https://github.com/octocat/Hello-World.git",
        "git@github.com:octocat/Hello-World.git",
        "ssh://git@github.com/octocat/Hello-World.git",
        "git://github.com/octocat/Hello-World.git",
    ],
)
def test_validate_clone_url_accepts_well_known_host_patterns(
    remote_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This suite never makes a real network call -- a public IP is
    # simulated for the resolution step so this test (scheme/shorthand
    # parsing accepted, then passed the SSRF check) doesn't depend on
    # live DNS.
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, port: [(socket.AF_INET, None, None, "", ("93.184.216.34", 0))]
    )
    projects._validate_clone_url(remote_url)


@pytest.mark.parametrize(
    "remote_url,expected_message",
    [
        ("file:///etc/passwd", "scheme"),
        ("file:///app/workspace/other-project/.git", "scheme"),
        ("/absolute/local/path", "could not parse a host"),
        ("relative/local/path", "could not parse a host"),
        ("http://example.com/repo.git", "scheme"),
        ("ext::sh -c touch /tmp/pwned", "scheme"),
    ],
)
def test_validate_clone_url_rejects_disallowed_schemes(
    remote_url: str, expected_message: str
) -> None:
    with pytest.raises(ValueError, match=expected_message):
        projects._validate_clone_url(remote_url)


@pytest.mark.parametrize(
    "remote_url",
    [
        "https://127.0.0.1/repo.git",
        "https://localhost/repo.git",
        "https://[::1]/repo.git",
        "ssh://10.0.0.5/repo.git",
        "ssh://172.17.0.2/repo.git",  # a typical Docker bridge-network address
        "ssh://192.168.1.1/repo.git",
        "https://169.254.169.254/repo.git",  # cloud metadata endpoint
        "git@127.0.0.1:owner/repo.git",
    ],
)
def test_validate_clone_url_rejects_private_and_loopback_addresses(
    remote_url: str,
) -> None:
    with pytest.raises(ValueError, match="non-public address"):
        projects._validate_clone_url(remote_url)


def test_validate_clone_url_rejects_embedded_control_characters() -> None:
    with pytest.raises(ValueError, match="invalid characters"):
        projects._validate_clone_url("https://github.com/x/y.git\nEvilHeader: x")


def test_validate_clone_url_unresolvable_host_is_not_a_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # DNS failure isn't a security concern -- it's left for the normal
    # clone attempt to fail on its own with a clear error. Mocked
    # (rather than relying on a real, guaranteed-unresolvable hostname)
    # so this doesn't depend on network/DNS access being available at
    # all in the environment running this suite.
    def fake_getaddrinfo(host: str, port: object) -> list[object]:
        raise socket.gaierror("mocked: name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    projects._validate_clone_url("https://this-host-should-not-resolve.invalid/repo.git")


@pytest.fixture
def bypass_clone_url_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the scheme/SSRF checks for tests that clone a local path.

    ``_validate_clone_url`` deliberately rejects bare local paths (the
    same as ``file://``, which it also rejects) -- see
    ``test_validate_clone_url_*`` below for that behavior itself. The
    tests here are about what happens *after* a clone succeeds (main
    file detection, metadata merging, cleanup on failure), for which a
    local temp-dir repo is just a fast, hermetic stand-in for a real
    git host -- not something meant to exercise the security boundary.
    """
    monkeypatch.setattr(projects, "_validate_clone_url", lambda remote_url: None)


def test_create_project_clones_remote(
    client: TestClient, tmp_path: Path, bypass_clone_url_validation: None
) -> None:
    source = _make_source_repo(
        tmp_path / "source-repo",
        {"main.tex": "% cloned content\n", "chapters/intro.tex": "Intro\n"},
    )

    response = client.post(
        "/api/projects",
        json={"name": "Cloned Paper", "remote_url": str(source)},
    )
    assert response.status_code == 200

    body = response.json()
    assert body["name"] == "Cloned Paper"
    assert body["main_file"] == "main.tex"

    project_dir = config.WORKSPACE_ROOT / body["id"]
    assert (project_dir / "main.tex").read_text(encoding="utf-8") == "% cloned content\n"
    assert (project_dir / "chapters" / "intro.tex").is_file()
    assert (project_dir / ".project.json").is_file()

    repo = git.Repo(project_dir)
    assert repo.remotes.origin.url == str(source)
    # The clone's own history plus our metadata commit on top.
    assert repo.head.commit.message == "Link project metadata"
    assert not repo.is_dirty(untracked_files=True)


def test_create_project_clone_preserves_existing_metadata(
    client: TestClient, tmp_path: Path, bypass_clone_url_validation: None
) -> None:
    source = _make_source_repo(
        tmp_path / "source-repo",
        {"thesis.tex": "% has its own main file\n"},
        with_project_json={
            "name": "Original Name",
            "created_at": 12345.0,
            "main_file": "thesis.tex",
        },
    )

    response = client.post(
        "/api/projects",
        json={"name": "My Local Name", "remote_url": str(source)},
    )
    assert response.status_code == 200

    body = response.json()
    # The user's own name wins; the original creation time and the
    # already-correct main_file survive from the shared .project.json.
    assert body["name"] == "My Local Name"
    assert body["created_at"] == 12345.0
    assert body["main_file"] == "thesis.tex"


def test_create_project_clone_detects_main_file_without_main_tex(
    client: TestClient, tmp_path: Path, bypass_clone_url_validation: None
) -> None:
    source = _make_source_repo(tmp_path / "source-repo", {"paper.tex": "content\n"})

    response = client.post(
        "/api/projects", json={"name": "No Main Tex", "remote_url": str(source)}
    )
    assert response.status_code == 200
    assert response.json()["main_file"] == "paper.tex"


def test_create_project_clone_failure_cleans_up_and_returns_400(
    client: TestClient, tmp_path: Path, bypass_clone_url_validation: None
) -> None:
    bogus_path = tmp_path / "does-not-exist"

    response = client.post(
        "/api/projects", json={"name": "Bad Clone", "remote_url": str(bogus_path)}
    )
    assert response.status_code == 400

    # No orphaned project directory left behind under workspace/.
    assert list(config.WORKSPACE_ROOT.iterdir()) == []


def test_create_project_rejects_remote_url_with_template_id(
    client: TestClient, tmp_path: Path
) -> None:
    source = _make_source_repo(tmp_path / "source-repo", {"main.tex": "x\n"})

    response = client.post(
        "/api/projects",
        json={
            "name": "Conflicting",
            "template_id": "article-template",
            "remote_url": str(source),
        },
    )
    assert response.status_code == 400


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


def test_create_entry_permission_denied_returns_actionable_500(
    client: TestClient,
) -> None:
    # Reproduces a real-world failure mode: a project directory owned
    # by a different user (e.g. created via Docker, now accessed via
    # `just dev` as a plain host user) raises PermissionError, which
    # must become a clear, actionable 500 -- not an unhandled crash.
    project_id = client.post(
        "/api/projects", json={"name": "Read Only Project"}
    ).json()["id"]
    project_dir = config.WORKSPACE_ROOT / project_id

    original_mode = project_dir.stat().st_mode
    project_dir.chmod(0o555)

    try:
        response = client.post(
            f"/api/projects/{project_id}/files",
            json={"path": "images", "type": "dir"},
        )
    finally:
        project_dir.chmod(original_mode)

    assert response.status_code == 500
    assert "Filesystem error" in response.json()["detail"]
    assert "chown" in response.json()["detail"]
