"""Tests for the Settings > Git service and router.

Covers ``webapp.services.git_remote`` (remote URL, credentials, fetch,
pull, push, branch selection) against throwaway local bare repositories,
plus HTTP status mapping through ``webapp.routers.git_remote``.
"""

from __future__ import annotations

from pathlib import Path

import git
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from webapp import config
from webapp.routers import git_remote as git_remote_router
from webapp.services import git_remote

_PROJECT_ID: str = "test-project"
_TEST_AUTHOR: git.Actor = git.Actor("tester", "tester@localhost")
_TOKEN: str = "ghp_SECRETTOKEN123"


@pytest.fixture(autouse=True)
def workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def git_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_path = tmp_path / ".gitconfig-webapp-test"
    monkeypatch.setattr(config, "GIT_CONFIG_PATH", config_path)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config_path))
    config.ensure_git_safe_directory()
    return config_path


@pytest.fixture
def project_repo_dir(workspace_root: Path) -> Path:
    """A project that is a git repo with one commit and no remote."""
    project_path: Path = workspace_root / _PROJECT_ID
    project_path.mkdir(parents=True)
    repo = git.Repo.init(project_path, initial_branch="main")
    (project_path / "main.tex").write_text("original\n", encoding="utf-8")
    repo.index.add(["main.tex"])
    repo.index.commit("Initial commit", author=_TEST_AUTHOR, committer=_TEST_AUTHOR)
    return project_path


@pytest.fixture
def remote_and_project(workspace_root: Path, tmp_path: Path) -> tuple[Path, Path]:
    """A bare remote seeded with one commit and a project cloned from it."""
    remote_path = tmp_path / "remote.git"
    git.Repo.init(remote_path, bare=True, initial_branch="main")

    seed_path = tmp_path / "seed"
    seed_repo = git.Repo.clone_from(remote_path, seed_path)
    (seed_path / "main.tex").write_text("original\n", encoding="utf-8")
    seed_repo.index.add(["main.tex"])
    seed_repo.index.commit("Initial commit", author=_TEST_AUTHOR, committer=_TEST_AUTHOR)
    seed_repo.remotes.origin.push("main:main")

    project_path = workspace_root / _PROJECT_ID
    git.Repo.clone_from(remote_path, project_path)
    return remote_path, project_path


def _commit_in_clone(remote_path: Path, tmp_path: Path, name: str, text: str) -> None:
    """Push a new commit to the remote from a separate clone."""
    clone_path = tmp_path / f"other-{name}"
    clone = git.Repo.clone_from(remote_path, clone_path)
    (clone_path / "main.tex").write_text(text, encoding="utf-8")
    clone.index.add(["main.tex"])
    clone.index.commit(name, author=_TEST_AUTHOR, committer=_TEST_AUTHOR)
    clone.remotes.origin.push("main:main")


def _raw_origin_url(project_path: Path) -> str:
    return git.Repo(project_path).remotes.origin.url


# ─── get_git_info / set_remote_url ───


def test_info_without_remote(project_repo_dir: Path) -> None:
    info = git_remote.get_git_info(_PROJECT_ID)

    assert info.remote_url is None
    assert info.has_credentials is False
    assert info.branch == "main"


def test_info_unknown_project_raises() -> None:
    with pytest.raises(FileNotFoundError):
        git_remote.get_git_info("does-not-exist")


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/repo.git",
        "ssh://git@github.com/owner/repo.git",
        "git@github.com:owner/repo.git",
    ],
)
def test_set_remote_url_accepts_supported_forms(
    project_repo_dir: Path, url: str
) -> None:
    info = git_remote.set_remote_url(_PROJECT_ID, url)

    assert info.remote_url == url
    assert _raw_origin_url(project_repo_dir) == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "-oProxyCommand=id",
        "--upload-pack=id",
        "ext::sh -c id",
        "file:///etc/passwd",
        "http://github.com/owner/repo.git",
        "https://github.com/own er/repo.git",
        "https://github.com/owner/repo.git\nfoo",
        "../relative/path",
    ],
)
def test_set_remote_url_rejects_unsafe_values(
    project_repo_dir: Path, url: str
) -> None:
    with pytest.raises(ValueError):
        git_remote.set_remote_url(_PROJECT_ID, url)

    assert git.Repo(project_repo_dir).remotes == []


def test_set_remote_url_updates_existing_origin(project_repo_dir: Path) -> None:
    git_remote.set_remote_url(_PROJECT_ID, "https://github.com/a/one.git")
    info = git_remote.set_remote_url(_PROJECT_ID, "https://github.com/a/two.git")

    assert info.remote_url == "https://github.com/a/two.git"
    assert len(git.Repo(project_repo_dir).remotes) == 1


# ─── credentials ───


def test_set_credentials_stores_token_but_never_returns_it(
    project_repo_dir: Path,
) -> None:
    git_remote.set_remote_url(_PROJECT_ID, "https://github.com/owner/repo.git")

    info = git_remote.set_credentials(_PROJECT_ID, "alice", _TOKEN)

    assert info.has_credentials is True
    assert info.credential_username == "alice"
    assert info.remote_url == "https://github.com/owner/repo.git"
    assert _TOKEN not in info.model_dump_json()
    assert _raw_origin_url(project_repo_dir) == (
        f"https://alice:{_TOKEN}@github.com/owner/repo.git"
    )


def test_set_credentials_percent_encodes_special_characters(
    project_repo_dir: Path,
) -> None:
    git_remote.set_remote_url(_PROJECT_ID, "https://github.com/owner/repo.git")

    git_remote.set_credentials(_PROJECT_ID, "a@b", "p/w:#?")

    assert _raw_origin_url(project_repo_dir) == (
        "https://a%40b:p%2Fw%3A%23%3F@github.com/owner/repo.git"
    )
    assert git_remote.get_git_info(_PROJECT_ID).credential_username == "a@b"


def test_set_credentials_without_remote_raises(project_repo_dir: Path) -> None:
    with pytest.raises(ValueError, match="remote"):
        git_remote.set_credentials(_PROJECT_ID, "alice", _TOKEN)


def test_set_credentials_rejects_ssh_remote(project_repo_dir: Path) -> None:
    git_remote.set_remote_url(_PROJECT_ID, "git@github.com:owner/repo.git")

    with pytest.raises(ValueError, match="https"):
        git_remote.set_credentials(_PROJECT_ID, "alice", _TOKEN)


@pytest.mark.parametrize("username,token", [("", "t"), ("u", ""), ("u v", "t")])
def test_set_credentials_rejects_empty_or_spaced_values(
    project_repo_dir: Path, username: str, token: str
) -> None:
    git_remote.set_remote_url(_PROJECT_ID, "https://github.com/owner/repo.git")

    with pytest.raises(ValueError):
        git_remote.set_credentials(_PROJECT_ID, username, token)


def test_clear_credentials_removes_userinfo(project_repo_dir: Path) -> None:
    git_remote.set_remote_url(_PROJECT_ID, "https://github.com/owner/repo.git")
    git_remote.set_credentials(_PROJECT_ID, "alice", _TOKEN)

    info = git_remote.clear_credentials(_PROJECT_ID)

    assert info.has_credentials is False
    assert _raw_origin_url(project_repo_dir) == "https://github.com/owner/repo.git"


def test_set_remote_url_keeps_credentials_for_same_host(
    project_repo_dir: Path,
) -> None:
    git_remote.set_remote_url(_PROJECT_ID, "https://github.com/owner/one.git")
    git_remote.set_credentials(_PROJECT_ID, "alice", _TOKEN)

    info = git_remote.set_remote_url(_PROJECT_ID, "https://github.com/owner/two.git")

    assert info.has_credentials is True
    assert _raw_origin_url(project_repo_dir) == (
        f"https://alice:{_TOKEN}@github.com/owner/two.git"
    )


def test_set_remote_url_drops_credentials_for_different_host(
    project_repo_dir: Path,
) -> None:
    git_remote.set_remote_url(_PROJECT_ID, "https://github.com/owner/one.git")
    git_remote.set_credentials(_PROJECT_ID, "alice", _TOKEN)

    info = git_remote.set_remote_url(_PROJECT_ID, "https://gitlab.com/owner/one.git")

    assert info.has_credentials is False
    assert _TOKEN not in _raw_origin_url(project_repo_dir)


def test_info_redacts_credentials_embedded_by_user(project_repo_dir: Path) -> None:
    git_remote.set_remote_url(
        _PROJECT_ID, f"https://bob:{_TOKEN}@github.com/owner/repo.git"
    )

    info = git_remote.get_git_info(_PROJECT_ID)

    assert info.has_credentials is True
    assert info.credential_username == "bob"
    assert _TOKEN not in info.model_dump_json()


# ─── fetch / pull / push ───


def test_fetch_without_remote_raises(project_repo_dir: Path) -> None:
    with pytest.raises(ValueError, match="no remote"):
        git_remote.fetch_remote(_PROJECT_ID)


def test_fetch_reports_behind_count(
    remote_and_project: tuple[Path, Path], tmp_path: Path
) -> None:
    remote_path, _ = remote_and_project
    _commit_in_clone(remote_path, tmp_path, "remote-change", "remote\n")

    result = git_remote.fetch_remote(_PROJECT_ID)

    assert result.info.behind == 1
    assert result.info.ahead == 0


def test_fetch_unreachable_remote_raises_connection_error(
    project_repo_dir: Path, tmp_path: Path
) -> None:
    git.Repo(project_repo_dir).create_remote("origin", str(tmp_path / "missing.git"))

    with pytest.raises(ConnectionError):
        git_remote.fetch_remote(_PROJECT_ID)


def test_pull_applies_remote_commits(
    remote_and_project: tuple[Path, Path], tmp_path: Path
) -> None:
    remote_path, project_path = remote_and_project
    _commit_in_clone(remote_path, tmp_path, "remote-change", "remote\n")

    result = git_remote.pull_remote(_PROJECT_ID)

    assert (project_path / "main.tex").read_text(encoding="utf-8") == "remote\n"
    assert result.info.behind == 0


def test_pull_refuses_with_uncommitted_changes(
    remote_and_project: tuple[Path, Path],
) -> None:
    _, project_path = remote_and_project
    (project_path / "main.tex").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(ValueError, match="uncommitted"):
        git_remote.pull_remote(_PROJECT_ID)

    assert (project_path / "main.tex").read_text(encoding="utf-8") == "dirty\n"


def test_pull_conflict_aborts_rebase(
    remote_and_project: tuple[Path, Path], tmp_path: Path
) -> None:
    remote_path, project_path = remote_and_project
    _commit_in_clone(remote_path, tmp_path, "remote-change", "remote\n")
    repo = git.Repo(project_path)
    (project_path / "main.tex").write_text("local\n", encoding="utf-8")
    repo.index.add(["main.tex"])
    repo.index.commit("local change", author=_TEST_AUTHOR, committer=_TEST_AUTHOR)

    with pytest.raises(ValueError, match="conflict"):
        git_remote.pull_remote(_PROJECT_ID)

    assert not (project_path / ".git" / "rebase-merge").exists()
    assert (project_path / "main.tex").read_text(encoding="utf-8") == "local\n"


def test_pull_without_remote_branch_raises(
    remote_and_project: tuple[Path, Path],
) -> None:
    _, project_path = remote_and_project
    git.Repo(project_path).git.switch("-c", "feature")

    with pytest.raises(ValueError, match="origin/feature"):
        git_remote.pull_remote(_PROJECT_ID)


def test_push_publishes_local_commits(
    remote_and_project: tuple[Path, Path], tmp_path: Path
) -> None:
    remote_path, project_path = remote_and_project
    repo = git.Repo(project_path)
    (project_path / "main.tex").write_text("local\n", encoding="utf-8")
    repo.index.add(["main.tex"])
    repo.index.commit("local change", author=_TEST_AUTHOR, committer=_TEST_AUTHOR)

    result = git_remote.push_remote(_PROJECT_ID)

    verify = git.Repo.clone_from(remote_path, tmp_path / "verify")
    assert (tmp_path / "verify" / "main.tex").read_text(encoding="utf-8") == "local\n"
    assert verify.head.commit.message.strip() == "local change"
    assert result.info.ahead == 0


def test_push_new_branch_sets_upstream(
    remote_and_project: tuple[Path, Path], tmp_path: Path
) -> None:
    remote_path, project_path = remote_and_project
    git.Repo(project_path).git.switch("-c", "feature")

    git_remote.push_remote(_PROJECT_ID)

    assert "feature" in [head.name for head in git.Repo(remote_path).heads]
    assert git_remote.get_git_info(_PROJECT_ID).remote_branch_exists is True


def test_push_rejected_when_remote_is_ahead(
    remote_and_project: tuple[Path, Path], tmp_path: Path
) -> None:
    remote_path, project_path = remote_and_project
    _commit_in_clone(remote_path, tmp_path, "remote-change", "remote\n")
    repo = git.Repo(project_path)
    (project_path / "other.tex").write_text("local\n", encoding="utf-8")
    repo.index.add(["other.tex"])
    repo.index.commit("local change", author=_TEST_AUTHOR, committer=_TEST_AUTHOR)

    with pytest.raises(ConnectionError, match="rejected"):
        git_remote.push_remote(_PROJECT_ID)


def test_push_without_remote_raises(project_repo_dir: Path) -> None:
    with pytest.raises(ValueError, match="no remote"):
        git_remote.push_remote(_PROJECT_ID)


# ─── branches ───


def test_list_branches_local_and_remote_only(
    remote_and_project: tuple[Path, Path],
) -> None:
    remote_path, project_path = remote_and_project
    other = git.Repo.clone_from(remote_path, project_path.parent / "other")
    other.git.switch("-c", "remote-feature")
    other.remotes.origin.push("remote-feature:remote-feature")
    git.Repo(project_path).git.switch("-c", "local-feature")
    git.Repo(project_path).remotes.origin.fetch()

    branches = git_remote.list_branches(_PROJECT_ID)

    assert branches.current == "local-feature"
    assert sorted(branches.local) == ["local-feature", "main"]
    assert branches.remote_only == ["remote-feature"]


def test_list_branches_without_remote(project_repo_dir: Path) -> None:
    branches = git_remote.list_branches(_PROJECT_ID)

    assert branches.current == "main"
    assert branches.local == ["main"]
    assert branches.remote_only == []


def test_switch_branch_creates_new_branch(project_repo_dir: Path) -> None:
    branches = git_remote.switch_branch(_PROJECT_ID, "feature/x", create=True)

    assert branches.current == "feature/x"
    assert git.Repo(project_repo_dir).active_branch.name == "feature/x"


def test_switch_branch_to_existing_local_branch(project_repo_dir: Path) -> None:
    git_remote.switch_branch(_PROJECT_ID, "feature", create=True)

    branches = git_remote.switch_branch(_PROJECT_ID, "main")

    assert branches.current == "main"


def test_switch_branch_tracks_remote_only_branch(
    remote_and_project: tuple[Path, Path],
) -> None:
    remote_path, project_path = remote_and_project
    other = git.Repo.clone_from(remote_path, project_path.parent / "other")
    other.git.switch("-c", "remote-feature")
    other.remotes.origin.push("remote-feature:remote-feature")
    git.Repo(project_path).remotes.origin.fetch()

    branches = git_remote.switch_branch(_PROJECT_ID, "remote-feature")

    assert branches.current == "remote-feature"
    assert "remote-feature" in branches.local
    assert branches.remote_only == []


def test_switch_branch_create_existing_name_raises(project_repo_dir: Path) -> None:
    with pytest.raises(ValueError, match="already exists"):
        git_remote.switch_branch(_PROJECT_ID, "main", create=True)


def test_switch_branch_unknown_name_raises(project_repo_dir: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        git_remote.switch_branch(_PROJECT_ID, "nope")


@pytest.mark.parametrize(
    "name",
    ["-f", "--orphan", "a b", "a..b", "a~1", "a:b", "a\\b", "/lead", "trail/", "x.lock", "@{u}"],
)
def test_switch_branch_rejects_invalid_names(
    project_repo_dir: Path, name: str
) -> None:
    with pytest.raises(ValueError, match="invalid branch name"):
        git_remote.switch_branch(_PROJECT_ID, name, create=True)


def test_switch_branch_refuses_with_uncommitted_changes(
    project_repo_dir: Path,
) -> None:
    git_remote.switch_branch(_PROJECT_ID, "feature", create=True)
    (project_repo_dir / "main.tex").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(ValueError, match="uncommitted"):
        git_remote.switch_branch(_PROJECT_ID, "main")

    assert git.Repo(project_repo_dir).active_branch.name == "feature"


# ─── router ───


def _make_client() -> TestClient:
    app: FastAPI = FastAPI()
    app.include_router(git_remote_router.router)
    return TestClient(app)


def test_router_get_info_unknown_project_404() -> None:
    assert _make_client().get("/api/projects/nope/git").status_code == 404


def test_router_get_info_ok(project_repo_dir: Path) -> None:
    response = _make_client().get(f"/api/projects/{_PROJECT_ID}/git")

    assert response.status_code == 200
    assert response.json()["branch"] == "main"


def test_router_set_remote_invalid_url_400(project_repo_dir: Path) -> None:
    response = _make_client().put(
        f"/api/projects/{_PROJECT_ID}/git/remote", json={"url": "-oProxy=x"}
    )

    assert response.status_code == 400


def test_router_credentials_roundtrip_never_leaks_token(
    project_repo_dir: Path,
) -> None:
    client = _make_client()
    client.put(
        f"/api/projects/{_PROJECT_ID}/git/remote",
        json={"url": "https://github.com/owner/repo.git"},
    )

    put_response = client.put(
        f"/api/projects/{_PROJECT_ID}/git/credentials",
        json={"username": "alice", "token": _TOKEN},
    )
    get_response = client.get(f"/api/projects/{_PROJECT_ID}/git")
    delete_response = client.delete(f"/api/projects/{_PROJECT_ID}/git/credentials")

    assert put_response.status_code == 200
    assert _TOKEN not in put_response.text
    assert _TOKEN not in get_response.text
    assert get_response.json()["has_credentials"] is True
    assert delete_response.json()["has_credentials"] is False


def test_router_credentials_blank_token_422(project_repo_dir: Path) -> None:
    response = _make_client().put(
        f"/api/projects/{_PROJECT_ID}/git/credentials",
        json={"username": "alice", "token": ""},
    )

    assert response.status_code == 422


def test_router_fetch_without_remote_400(project_repo_dir: Path) -> None:
    response = _make_client().post(f"/api/projects/{_PROJECT_ID}/git/fetch")

    assert response.status_code == 400


def test_router_fetch_unreachable_503(project_repo_dir: Path, tmp_path: Path) -> None:
    git.Repo(project_repo_dir).create_remote("origin", str(tmp_path / "missing.git"))

    response = _make_client().post(f"/api/projects/{_PROJECT_ID}/git/fetch")

    assert response.status_code == 503


def test_router_branches_list_and_switch(project_repo_dir: Path) -> None:
    client = _make_client()

    created = client.post(
        f"/api/projects/{_PROJECT_ID}/git/branch",
        json={"name": "feature", "create": True},
    )
    listed = client.get(f"/api/projects/{_PROJECT_ID}/git/branches")

    assert created.status_code == 200
    assert listed.json()["current"] == "feature"


def test_router_switch_invalid_name_400(project_repo_dir: Path) -> None:
    response = _make_client().post(
        f"/api/projects/{_PROJECT_ID}/git/branch",
        json={"name": "-f", "create": True},
    )

    assert response.status_code == 400
