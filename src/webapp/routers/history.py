"""HTTP routes for git-backed project version history.

Thin wrapper around ``services.git_history``: all git logic lives in
the service module, this router only translates service exceptions
into HTTP status codes.

Exception mapping:

* ``FileNotFoundError`` (project directory missing or not a git repo)
  -> ``404``.
* ``LookupError`` (unknown commit id) -> ``404``.
* ``ValueError`` (e.g. "no changes to commit") -> ``400``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..models import CommitCreate, CommitInfo, DiffEntry
from ..services import git_history

router = APIRouter(prefix="/api/projects", tags=["history"])


@router.get("/{project_id}/history")
def list_history(project_id: str, limit: int = 50) -> list[CommitInfo]:
    """Return a project's commit history, most recent first."""
    try:
        return git_history.list_commits(project_id, limit=limit)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/{project_id}/history/commit")
def commit_history(project_id: str, body: CommitCreate) -> CommitInfo:
    """Stage all working-tree changes and record them as a new commit."""
    try:
        return git_history.create_commit(
            project_id,
            message=body.message,
            author=body.author,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/{project_id}/history/{commit_id}/diff")
def diff_history(project_id: str, commit_id: str) -> list[DiffEntry]:
    """Return the per-file diff introduced by a single commit."""
    try:
        return git_history.get_diff(project_id, commit_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/{project_id}/history/{commit_id}/revert")
def revert_history(
    project_id: str,
    commit_id: str,
    body: CommitCreate,
) -> CommitInfo:
    """Revert a project to a past commit by recording a new commit on top.

    ``body.message`` is accepted but ignored: the revert commit always
    uses the generated message ``"Revert to <short sha>"``. Only
    ``body.author`` is used, as an optional author override.
    """
    try:
        return git_history.revert_to_commit(
            project_id,
            commit_id,
            author=body.author,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
