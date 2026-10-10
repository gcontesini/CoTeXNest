"""Tests for the git-backed project history service and router.

Covers ``webapp.services.git_history`` directly, plus HTTP-level status
code mapping through ``webapp.routers.history`` using an ad hoc FastAPI
app scoped to just this router, rather than the full ``webapp.main``
app.
"""

from __future__ import annotations

from pathlib import Path

import git
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from webapp import config
from webapp.routers import history as history_router
from webapp.services import assistant, git_history

_PROJECT_ID: str = "test-project"
_ORIGINAL_CONTENT: str = "original content\n"
_TEST_AUTHOR: git.Actor = git.Actor("tester", "tester@localhost")


@pytest.fixture(autouse=True)
def workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point config.WORKSPACE_ROOT at a throwaway directory per test."""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def git_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point GIT_CONFIG_GLOBAL at a throwaway, per-test app git config.

    Outside tests, main.py's startup event calls
    config.ensure_git_safe_directory() once for the life of the
    process. Tests never go through that startup path, so without this
    fixture, GIT_CONFIG_GLOBAL (set unconditionally at config.py import
    time) would point at a file that was never written -- git would
    see no committer identity at all for the sync tests' `git rebase`
    calls (which need one independent of the author identity
    git_history.py sets explicitly per commit), and no safe.directory
    exception either.
    """
    config_path = tmp_path / ".gitconfig-webapp-test"
    monkeypatch.setattr(config, "GIT_CONFIG_PATH", config_path)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config_path))
    config.ensure_git_safe_directory()
    return config_path


@pytest.fixture(autouse=True)
def assistant_config_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the assistant config at a throwaway directory per test.

    Without this, the sync tests that exercise the "no assistant
    available" fallback path would read/write this developer machine's
    real assistant_config/settings.json instead of a guaranteed-blank
    one, making the test's outcome depend on whatever provider happens
    to be configured locally.
    """
    config_root = tmp_path / "assistant_config"
    monkeypatch.setattr(config, "ASSISTANT_CONFIG_ROOT", config_root)
    monkeypatch.setattr(config, "ASSISTANT_CONFIG_PATH", config_root / "settings.json")
    return config_root


@pytest.fixture
def project_repo_dir(workspace_root: Path) -> Path:
    """Create a project dir that is already a git repo with one commit.

    Mirrors the project-scaffolding flow: ``git init`` + an initial
    commit adding ``main.tex``.
    """
    project_path: Path = workspace_root / _PROJECT_ID
    project_path.mkdir(parents=True)

    repo: git.Repo = git.Repo.init(project_path)
    (project_path / "main.tex").write_text(
        _ORIGINAL_CONTENT,
        encoding="utf-8",
    )
    repo.index.add(["main.tex"])
    repo.index.commit(
        "Initial commit",
        author=_TEST_AUTHOR,
        committer=_TEST_AUTHOR,
    )

    return project_path


def _make_client() -> TestClient:
    """Build a minimal FastAPI app exposing only the history router."""
    app: FastAPI = FastAPI()
    app.include_router(history_router.router)
    return TestClient(app)


# ─── service: list_commits ───


def test_list_commits_returns_initial_commit(project_repo_dir: Path) -> None:
    commits = git_history.list_commits(_PROJECT_ID)

    assert len(commits) == 1
    assert commits[0].message == "Initial commit"
    assert commits[0].author == "tester"
    assert commits[0].changed_files == ["main.tex"]


def test_list_commits_unknown_project_raises_file_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        git_history.list_commits("does-not-exist")


# ─── service: create_commit ───


def test_create_commit_after_edit_adds_new_commit(
    project_repo_dir: Path,
) -> None:
    (project_repo_dir / "main.tex").write_text(
        "edited content\n",
        encoding="utf-8",
    )

    commit_info = git_history.create_commit(
        _PROJECT_ID,
        "Edit main.tex",
        author="alice",
    )

    assert commit_info.message == "Edit main.tex"
    assert commit_info.author == "alice"
    assert "main.tex" in commit_info.changed_files

    commits = git_history.list_commits(_PROJECT_ID)
    assert len(commits) == 2


def test_create_commit_with_no_changes_raises_value_error(
    project_repo_dir: Path,
) -> None:
    with pytest.raises(ValueError):
        git_history.create_commit(_PROJECT_ID, "no-op", author="alice")


# ─── service: get_diff ───


def test_get_diff_on_root_commit_shows_added_file(
    project_repo_dir: Path,
) -> None:
    root_commit_id: str = git_history.list_commits(_PROJECT_ID)[0].id

    diff_entries = git_history.get_diff(_PROJECT_ID, root_commit_id)

    assert len(diff_entries) == 1
    assert diff_entries[0].file_path == "main.tex"
    assert diff_entries[0].status == "added"
    assert _ORIGINAL_CONTENT.strip() in diff_entries[0].patch


def test_get_diff_on_later_commit_shows_modified_and_added(
    project_repo_dir: Path,
) -> None:
    (project_repo_dir / "main.tex").write_text(
        "edited content\n",
        encoding="utf-8",
    )
    (project_repo_dir / "new_file.tex").write_text(
        "new file\n",
        encoding="utf-8",
    )

    commit_info = git_history.create_commit(
        _PROJECT_ID,
        "Edit and add",
        author="bob",
    )

    diff_entries = git_history.get_diff(_PROJECT_ID, commit_info.id)
    status_by_path = {entry.file_path: entry.status for entry in diff_entries}

    assert status_by_path["main.tex"] == "modified"
    assert status_by_path["new_file.tex"] == "added"


def test_get_diff_unknown_commit_raises_lookup_error(
    project_repo_dir: Path,
) -> None:
    with pytest.raises(LookupError):
        git_history.get_diff(_PROJECT_ID, "not-a-real-commit-id")


# ─── service: revert_to_commit ───


def test_revert_to_commit_restores_content_and_appends_history(
    project_repo_dir: Path,
) -> None:
    initial_commit_id: str = git_history.list_commits(_PROJECT_ID)[0].id

    (project_repo_dir / "main.tex").write_text(
        "edited content\n",
        encoding="utf-8",
    )
    git_history.create_commit(_PROJECT_ID, "Edit", author="carol")

    history_length_before_revert: int = len(
        git_history.list_commits(_PROJECT_ID)
    )

    revert_info = git_history.revert_to_commit(
        _PROJECT_ID,
        initial_commit_id,
        author="dave",
    )

    history_after_revert = git_history.list_commits(_PROJECT_ID)

    assert len(history_after_revert) == history_length_before_revert + 1
    assert revert_info.author == "dave"
    assert revert_info.message == f"Revert to {initial_commit_id[:8]}"
    assert (
        project_repo_dir / "main.tex"
    ).read_text(encoding="utf-8") == _ORIGINAL_CONTENT

    # The reverted-from commit must still be reachable: history grows.
    commit_ids = {commit.id for commit in history_after_revert}
    assert initial_commit_id in commit_ids


def test_revert_to_commit_unknown_commit_raises_lookup_error(
    project_repo_dir: Path,
) -> None:
    with pytest.raises(LookupError):
        git_history.revert_to_commit(
            _PROJECT_ID,
            "not-a-real-commit-id",
            author="dave",
        )


# ─── router: HTTP status mapping ───


def test_router_list_history_unknown_project_returns_404() -> None:
    client: TestClient = _make_client()

    response = client.get("/api/projects/does-not-exist/history")

    assert response.status_code == 404


def test_router_diff_unknown_commit_returns_404(
    project_repo_dir: Path,
) -> None:
    client: TestClient = _make_client()

    response = client.get(
        f"/api/projects/{_PROJECT_ID}/history/not-a-real-commit-id/diff"
    )

    assert response.status_code == 404


def test_router_commit_with_no_changes_returns_400(
    project_repo_dir: Path,
) -> None:
    client: TestClient = _make_client()

    response = client.post(
        f"/api/projects/{_PROJECT_ID}/history/commit",
        json={"message": "no-op", "author": "alice"},
    )

    assert response.status_code == 400


def test_router_commit_and_list_round_trip(project_repo_dir: Path) -> None:
    client: TestClient = _make_client()

    (project_repo_dir / "main.tex").write_text(
        "edited via router\n",
        encoding="utf-8",
    )

    commit_response = client.post(
        f"/api/projects/{_PROJECT_ID}/history/commit",
        json={"message": "Router edit", "author": "erin"},
    )
    assert commit_response.status_code == 200
    assert commit_response.json()["message"] == "Router edit"

    history_response = client.get(f"/api/projects/{_PROJECT_ID}/history")
    assert history_response.status_code == 200
    assert len(history_response.json()) == 2


def test_router_revert_returns_404_for_unknown_commit(
    project_repo_dir: Path,
) -> None:
    client: TestClient = _make_client()

    response = client.post(
        f"/api/projects/{_PROJECT_ID}/history/not-a-real-commit-id/revert",
        json={"message": "ignored", "author": "dave"},
    )

    assert response.status_code == 404


# ─── redact_url_credentials ───


def test_redact_url_credentials_strips_embedded_token() -> None:
    text = "fatal: unable to access 'https://ghp_secrettoken@github.com/x/y.git/'"
    redacted = git_history.redact_url_credentials(text)

    assert "ghp_secrettoken" not in redacted
    assert "https://***@github.com/x/y.git/" in redacted


def test_redact_url_credentials_leaves_plain_urls_alone() -> None:
    text = "fatal: repository 'https://github.com/x/y.git' not found"
    assert git_history.redact_url_credentials(text) == text


# ─── sync_with_remote ───


@pytest.fixture
def remote_and_project(
    workspace_root: Path, tmp_path: Path
) -> tuple[Path, Path, str]:
    """A bare 'remote' repo seeded with one commit, and a project cloned
    from it under ``workspace/`` -- mirroring the clone-on-create flow.

    Returns:
        (remote_path, project_path, branch_name)
    """
    remote_path = tmp_path / "remote.git"
    git.Repo.init(remote_path, bare=True, initial_branch="main")

    seed_path = tmp_path / "seed"
    seed_repo = git.Repo.clone_from(remote_path, seed_path)
    (seed_path / "main.tex").write_text(_ORIGINAL_CONTENT, encoding="utf-8")
    seed_repo.index.add(["main.tex"])
    seed_repo.index.commit(
        "Initial commit", author=_TEST_AUTHOR, committer=_TEST_AUTHOR
    )
    seed_repo.remotes.origin.push("main:main")

    project_path = workspace_root / _PROJECT_ID
    git.Repo.clone_from(remote_path, project_path)

    return remote_path, project_path, "main"


def test_sync_with_remote_no_origin_raises_value_error(
    project_repo_dir: Path,
) -> None:
    # project_repo_dir is a plain git-init'd project with no remote.
    with pytest.raises(ValueError, match="no remote configured"):
        git_history.sync_with_remote(_PROJECT_ID, author="alice")


def test_sync_with_remote_dirty_without_message_raises_needs_commit_message(
    remote_and_project: tuple[Path, Path, str],
) -> None:
    _remote_path, project_path, _branch = remote_and_project
    (project_path / "main.tex").write_text("local edit\n", encoding="utf-8")

    with pytest.raises(git_history.NeedsCommitMessage) as excinfo:
        git_history.sync_with_remote(_PROJECT_ID, author="alice")

    assert "main.tex" in excinfo.value.changed_files
    assert "local edit" in excinfo.value.diff_text


def test_sync_with_remote_commits_pulls_and_pushes(
    remote_and_project: tuple[Path, Path, str],
) -> None:
    remote_path, project_path, branch = remote_and_project

    # Someone else pushed a commit to the remote first.
    other_clone_path = project_path.parent / "other-clone"
    other_repo = git.Repo.clone_from(remote_path, other_clone_path)
    (other_clone_path / "other.tex").write_text("from someone else\n", encoding="utf-8")
    other_repo.index.add(["other.tex"])
    other_repo.index.commit(
        "Someone else's commit", author=_TEST_AUTHOR, committer=_TEST_AUTHOR
    )
    other_repo.remotes.origin.push(f"{branch}:{branch}")

    # Meanwhile, our local project has its own uncommitted edit.
    (project_path / "main.tex").write_text("my local edit\n", encoding="utf-8")

    result = git_history.sync_with_remote(
        _PROJECT_ID, commit_message="My edit", author="alice"
    )

    assert result.status == "synced"
    assert result.committed is True
    assert result.commit_message == "My edit"
    assert result.pulled_commits == 1
    assert result.pushed is True

    # Both the local edit and the other clone's file are present.
    assert (project_path / "main.tex").read_text(encoding="utf-8") == "my local edit\n"
    assert (project_path / "other.tex").is_file()

    # And the remote itself now has both commits.
    verify_path = project_path.parent / "verify-clone"
    git.Repo.clone_from(remote_path, verify_path)
    assert (verify_path / "main.tex").read_text(encoding="utf-8") == "my local edit\n"
    assert (verify_path / "other.tex").is_file()


def test_sync_with_remote_clean_tree_still_pulls_and_pushes(
    remote_and_project: tuple[Path, Path, str],
) -> None:
    remote_path, project_path, branch = remote_and_project

    other_clone_path = project_path.parent / "other-clone"
    other_repo = git.Repo.clone_from(remote_path, other_clone_path)
    (other_clone_path / "other.tex").write_text("from someone else\n", encoding="utf-8")
    other_repo.index.add(["other.tex"])
    other_repo.index.commit(
        "Someone else's commit", author=_TEST_AUTHOR, committer=_TEST_AUTHOR
    )
    other_repo.remotes.origin.push(f"{branch}:{branch}")

    result = git_history.sync_with_remote(_PROJECT_ID, author="alice")

    assert result.committed is False
    assert result.pulled_commits == 1
    assert (project_path / "other.tex").is_file()


def test_sync_with_remote_conflict_aborts_rebase_cleanly(
    remote_and_project: tuple[Path, Path, str],
) -> None:
    remote_path, project_path, branch = remote_and_project

    other_clone_path = project_path.parent / "other-clone"
    other_repo = git.Repo.clone_from(remote_path, other_clone_path)
    (other_clone_path / "main.tex").write_text(
        "someone else's conflicting edit\n", encoding="utf-8"
    )
    other_repo.index.add(["main.tex"])
    other_repo.index.commit(
        "Conflicting remote edit", author=_TEST_AUTHOR, committer=_TEST_AUTHOR
    )
    other_repo.remotes.origin.push(f"{branch}:{branch}")

    (project_path / "main.tex").write_text(
        "my own conflicting edit\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="conflict"):
        git_history.sync_with_remote(
            _PROJECT_ID, commit_message="My conflicting edit", author="alice"
        )

    # The rebase must have been cleanly aborted: no stray rebase state,
    # and the local commit we made before attempting it is still there.
    repo = git.Repo(project_path)
    assert not (project_path / ".git" / "rebase-apply").exists()
    assert not (project_path / ".git" / "rebase-merge").exists()
    assert repo.head.commit.message == "My conflicting edit"


def test_sync_with_remote_first_push_establishes_tracking(
    workspace_root: Path, tmp_path: Path
) -> None:
    remote_path = tmp_path / "empty-remote.git"
    git.Repo.init(remote_path, bare=True, initial_branch="main")

    project_path = workspace_root / _PROJECT_ID
    project_path.mkdir(parents=True)
    repo = git.Repo.init(project_path, initial_branch="main")
    repo.create_remote("origin", str(remote_path))
    (project_path / "main.tex").write_text("brand new project\n", encoding="utf-8")
    repo.index.add(["main.tex"])
    repo.index.commit(
        "Initial commit", author=_TEST_AUTHOR, committer=_TEST_AUTHOR
    )

    result = git_history.sync_with_remote(_PROJECT_ID, author="alice")

    assert result.status == "synced"
    assert result.pulled_commits == 0
    assert result.pushed is True

    verify_path = tmp_path / "verify-clone"
    git.Repo.clone_from(remote_path, verify_path)
    assert (verify_path / "main.tex").read_text(encoding="utf-8") == "brand new project\n"


# ─── router: /sync ───


def test_router_sync_unknown_project_returns_404() -> None:
    client: TestClient = _make_client()

    response = client.post(
        "/api/projects/does-not-exist/sync", json={"author": "alice"}
    )

    assert response.status_code == 404


def test_router_sync_no_remote_returns_400(project_repo_dir: Path) -> None:
    client: TestClient = _make_client()

    response = client.post(
        f"/api/projects/{_PROJECT_ID}/sync", json={"author": "alice"}
    )

    assert response.status_code == 400


def test_router_sync_dirty_without_message_and_no_assistant_returns_needs_message(
    remote_and_project: tuple[Path, Path, str],
) -> None:
    _remote_path, project_path, _branch = remote_and_project
    (project_path / "main.tex").write_text("local edit\n", encoding="utf-8")

    client: TestClient = _make_client()
    response = client.post(
        f"/api/projects/{_PROJECT_ID}/sync", json={"author": "alice"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "needs_commit_message"
    assert "main.tex" in body["changed_files"]


def test_router_sync_dirty_uses_assistant_generated_message(
    remote_and_project: tuple[Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _remote_path, project_path, _branch = remote_and_project
    (project_path / "main.tex").write_text("local edit\n", encoding="utf-8")

    monkeypatch.setattr(
        assistant, "generate_commit_message", lambda diff_text: "AI-written message"
    )

    client: TestClient = _make_client()
    response = client.post(
        f"/api/projects/{_PROJECT_ID}/sync", json={"author": "alice"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "synced"
    assert body["committed"] is True
    assert body["commit_message"] == "AI-written message"


def test_router_sync_with_explicit_message_round_trips(
    remote_and_project: tuple[Path, Path, str],
) -> None:
    _remote_path, project_path, _branch = remote_and_project
    (project_path / "main.tex").write_text("local edit\n", encoding="utf-8")

    client: TestClient = _make_client()
    response = client.post(
        f"/api/projects/{_PROJECT_ID}/sync",
        json={"commit_message": "My own message", "author": "alice"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "synced"
    assert body["commit_message"] == "My own message"
