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
from webapp.services import git_history

_PROJECT_ID: str = "test-project"
_ORIGINAL_CONTENT: str = "original content\n"
_TEST_AUTHOR: git.Actor = git.Actor("tester", "tester@localhost")


@pytest.fixture(autouse=True)
def workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point config.WORKSPACE_ROOT at a throwaway directory per test."""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    return tmp_path


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
