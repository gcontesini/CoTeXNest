"""HTTP routes for Settings > Git: remote, credentials, fetch/pull/push, branches.

Thin wrapper around ``services.git_remote``. Exception mapping matches
``routers.history``:

* ``FileNotFoundError`` -> ``404``
* ``ValueError`` (no remote, invalid URL or branch name, uncommitted
  changes, rebase conflict) -> ``400``
* ``ConnectionError`` (fetch/push failed or rejected) -> ``503``
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from fastapi import APIRouter, HTTPException

from ..models import (
    GitActionResult,
    GitBranches,
    GitBranchSwitch,
    GitCredentialsSet,
    GitInfo,
    GitRemoteSet,
)
from ..services import git_remote

router = APIRouter(prefix="/api/projects", tags=["git"])

_T = TypeVar("_T")


def _call(operation_: Callable[[], _T]) -> _T:
    """Run a service call, translating its exceptions to HTTP errors."""
    try:
        return operation_()
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ConnectionError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.get("/{project_id}/git", response_model=GitInfo)
def get_git_info(project_id: str) -> GitInfo:
    """Return the remote URL (no credentials), branch and ahead/behind."""
    return _call(lambda: git_remote.get_git_info(project_id))


@router.put("/{project_id}/git/remote", response_model=GitInfo)
def put_remote(project_id: str, body: GitRemoteSet) -> GitInfo:
    """Create or update the ``origin`` remote URL."""
    return _call(lambda: git_remote.set_remote_url(project_id, body.url))


@router.put("/{project_id}/git/credentials", response_model=GitInfo)
def put_credentials(project_id: str, body: GitCredentialsSet) -> GitInfo:
    """Store a username and token for the HTTPS remote (never echoed)."""
    return _call(
        lambda: git_remote.set_credentials(project_id, body.username, body.token)
    )


@router.delete("/{project_id}/git/credentials", response_model=GitInfo)
def delete_credentials(project_id: str) -> GitInfo:
    """Remove the stored username and token."""
    return _call(lambda: git_remote.clear_credentials(project_id))


@router.post("/{project_id}/git/fetch", response_model=GitActionResult)
def post_fetch(project_id: str) -> GitActionResult:
    return _call(lambda: git_remote.fetch_remote(project_id))


@router.post("/{project_id}/git/pull", response_model=GitActionResult)
def post_pull(project_id: str) -> GitActionResult:
    return _call(lambda: git_remote.pull_remote(project_id))


@router.post("/{project_id}/git/push", response_model=GitActionResult)
def post_push(project_id: str) -> GitActionResult:
    return _call(lambda: git_remote.push_remote(project_id))


@router.get("/{project_id}/git/branches", response_model=GitBranches)
def get_branches(project_id: str) -> GitBranches:
    """List local branches and remote-only branches (as last fetched)."""
    return _call(lambda: git_remote.list_branches(project_id))


@router.post("/{project_id}/git/branch", response_model=GitBranches)
def post_branch(project_id: str, body: GitBranchSwitch) -> GitBranches:
    """Switch to, track, or create a branch."""
    return _call(
        lambda: git_remote.switch_branch(project_id, body.name, create=body.create)
    )
