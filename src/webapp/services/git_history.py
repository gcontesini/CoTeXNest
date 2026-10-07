"""Git-backed version history for projects.

Each project directory under ``config.WORKSPACE_ROOT`` is already a real
git repository (another module ``git init``s it and makes the initial
commit when the project is created). This module layers a small,
frictionless "commit / diff / revert" API on top of that repository
using GitPython.

Design choice: revert = new commit, never rewrite history.

``revert_to_commit`` never performs a destructive ``git reset --hard``
or history rewrite. Instead it restores the working tree to match the
target commit's content and then records that restoration as a brand
new commit on top of the current ``HEAD``. This means:

* The reverted-from state is never lost. It stays reachable at its
  original commit id.
* Concurrent collaborators (or another browser tab/process reading the
  same repo) never see a commit they were looking at disappear or get
  force-pushed away; ``HEAD`` only ever moves forward.
* The history list in the UI reads like a normal, append-only commit
  log, which matches the "frictionless, like git" mental model this
  feature is meant to provide, without exposing users to the sharp
  edges of ``git reset``/``git checkout <sha>`` (detached HEAD, lost
  commits, etc.).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import git

from .. import config
from ..models import CommitInfo, DiffEntry

_CHANGE_TYPE_TO_STATUS: dict[str, str] = {
    "A": "added",
    "D": "deleted",
}


def _diff_item_status(diff_item: git.diff.Diff) -> str:
    """Classify a GitPython diff item as added/modified/deleted.

    Prefers ``change_type`` (``'A'``/``'D'``/other) when GitPython has
    populated it. In patch mode (``create_patch=True``) the installed
    GitPython version (verified empirically: 3.2.0) leaves
    ``change_type`` as ``None`` even though ``new_file``/
    ``deleted_file`` are still set correctly, so those booleans are
    used as the authoritative fallback.

    Args:
        diff_item (git.diff.Diff): Single entry from a ``DiffIndex``.

    Returns:
        str: One of ``"added"``, ``"modified"``, ``"deleted"``.
    """
    if diff_item.change_type is not None:
        return _CHANGE_TYPE_TO_STATUS.get(diff_item.change_type, "modified")

    if diff_item.new_file:
        return "added"

    if diff_item.deleted_file:
        return "deleted"

    return "modified"


def _open_repo(project_id: str) -> git.Repo:
    """Open the git repository backing a project.

    Args:
        project_id (str): Identifier of the project under
            ``config.WORKSPACE_ROOT``.

    Returns:
        git.Repo: Opened repository handle.

    Raises:
        FileNotFoundError: If the project directory does not exist or
            is not a git repository.
    """
    assert project_id, "project_id must be a non-empty string"

    project_path: Path = config.project_dir(project_id)

    if not project_path.is_dir():
        raise FileNotFoundError(f"project not found: {project_id!r}")

    try:
        repo: git.Repo = git.Repo(project_path)
    except git.InvalidGitRepositoryError as error:
        raise FileNotFoundError(
            f"project not found: {project_id!r}"
        ) from error

    return repo


def _to_commit_info(commit: git.Commit) -> CommitInfo:
    """Map a GitPython ``Commit`` to the shared ``CommitInfo`` model.

    Args:
        commit (git.Commit): Commit to convert.

    Returns:
        CommitInfo: Serializable commit summary.
    """
    commit_info: CommitInfo = CommitInfo(
        id=commit.hexsha,
        message=str(commit.message).strip(),
        author=commit.author.name,
        timestamp=float(commit.committed_date),
        changed_files=list(commit.stats.files.keys()),
    )

    assert commit_info.id, "commit id must not be empty"

    return commit_info


def list_commits(
    project_id: str,
    limit: int = 50,
) -> list[CommitInfo]:
    """List a project's commit history, most recent first.

    Args:
        project_id (str): Identifier of the project under
            ``config.WORKSPACE_ROOT``.
        limit (int): Maximum number of commits to return.

    Returns:
        list[CommitInfo]: Commits in reverse-chronological order.

    Raises:
        FileNotFoundError: If the project directory does not exist or
            is not a git repository.
    """
    assert limit > 0, "limit must be a positive integer"

    repo: git.Repo = _open_repo(project_id)

    commits: list[CommitInfo] = [
        _to_commit_info(commit)
        for commit in repo.iter_commits(max_count=limit)
    ]

    return commits


def create_commit(
    project_id: str,
    message: str,
    author: str = "anonymous",
) -> CommitInfo:
    """Stage all working-tree changes and record them as a new commit.

    Args:
        project_id (str): Identifier of the project under
            ``config.WORKSPACE_ROOT``.
        message (str): Commit message.
        author (str): Display name recorded as the commit author.

    Returns:
        CommitInfo: The newly created commit.

    Raises:
        FileNotFoundError: If the project directory does not exist or
            is not a git repository.
        ValueError: If there are no changes to commit.
    """
    assert message.strip(), "message must not be empty"
    assert author.strip(), "author must not be empty"

    repo: git.Repo = _open_repo(project_id)

    repo.git.add(A=True)

    has_staged_changes: bool = bool(repo.index.diff("HEAD"))
    has_untracked_files: bool = bool(repo.untracked_files)
    is_dirty: bool = repo.is_dirty(untracked_files=True)

    if not (has_staged_changes or has_untracked_files or is_dirty):
        raise ValueError("no changes to commit")

    commit_author: git.Actor = git.Actor(author, f"{author}@localhost")
    new_commit: git.Commit = repo.index.commit(
        message,
        author=commit_author,
        committer=commit_author,
    )

    commit_info: CommitInfo = _to_commit_info(new_commit)

    assert commit_info.id, "created commit must have a valid id"

    return commit_info


def get_diff(
    project_id: str,
    commit_id: str,
) -> list[DiffEntry]:
    """Compute the per-file diff introduced by a single commit.

    Diffs against the commit's first parent, or against git's empty
    tree when the commit is the repository root (has no parent).

    Args:
        project_id (str): Identifier of the project under
            ``config.WORKSPACE_ROOT``.
        commit_id (str): Commit SHA (full or abbreviated).

    Returns:
        list[DiffEntry]: One entry per changed file.

    Raises:
        FileNotFoundError: If the project directory does not exist or
            is not a git repository.
        LookupError: If ``commit_id`` does not resolve to a commit.
    """
    assert commit_id, "commit_id must be a non-empty string"

    repo: git.Repo = _open_repo(project_id)
    commit: git.Commit = _resolve_commit(repo, commit_id)

    if commit.parents:
        diff_index = commit.parents[0].diff(commit, create_patch=True)
    else:
        diff_index = commit.diff(git.NULL_TREE, create_patch=True)

    diff_entries: list[DiffEntry] = []

    for diff_item in diff_index:
        status: str = _diff_item_status(diff_item)
        file_path: str | None = diff_item.b_path or diff_item.a_path

        assert file_path, "diff item must reference at least one path"

        patch_text: str = diff_item.diff.decode(
            "utf-8",
            errors="replace",
        )

        diff_entries.append(
            DiffEntry(
                file_path=file_path,
                status=status,
                patch=patch_text,
            )
        )

    return diff_entries


def revert_to_commit(
    project_id: str,
    commit_id: str,
    author: str = "anonymous",
) -> CommitInfo:
    """Restore the working tree to a past commit as a new commit.

    This never rewrites existing history (see module docstring). It
    removes files that exist at ``HEAD`` but did not exist at the
    target commit, checks the target commit's tree out onto the
    working directory, stages everything, and records the result as a
    new commit on top of ``HEAD``.

    Args:
        project_id (str): Identifier of the project under
            ``config.WORKSPACE_ROOT``.
        commit_id (str): Commit SHA (full or abbreviated) to revert to.
        author (str): Display name recorded as the commit author.

    Returns:
        CommitInfo: The newly created revert commit.

    Raises:
        FileNotFoundError: If the project directory does not exist or
            is not a git repository.
        LookupError: If ``commit_id`` does not resolve to a commit.
        ValueError: If there is nothing to commit after the revert
            (the working tree already matched the target commit).
    """
    assert commit_id, "commit_id must be a non-empty string"
    assert author.strip(), "author must not be empty"

    repo: git.Repo = _open_repo(project_id)
    target_commit: git.Commit = _resolve_commit(repo, commit_id)

    project_path: Path = config.project_dir(project_id)
    head_commit: git.Commit = repo.head.commit

    added_since_target = target_commit.diff(head_commit)

    for diff_item in added_since_target:
        if diff_item.change_type != "A":
            continue

        removed_path: str | None = diff_item.b_path or diff_item.a_path
        assert removed_path, "added diff item must reference a path"

        absolute_path: Path = project_path / removed_path
        if absolute_path.is_dir():
            shutil.rmtree(absolute_path, ignore_errors=True)
        elif absolute_path.exists() or absolute_path.is_symlink():
            absolute_path.unlink()

    repo.git.checkout(target_commit.hexsha, "--", ".")
    repo.git.add(A=True)

    has_staged_changes: bool = bool(repo.index.diff("HEAD"))
    has_untracked_files: bool = bool(repo.untracked_files)

    if not (has_staged_changes or has_untracked_files):
        raise ValueError("no changes to commit")

    commit_author: git.Actor = git.Actor(author, f"{author}@localhost")
    revert_commit: git.Commit = repo.index.commit(
        f"Revert to {commit_id[:8]}",
        author=commit_author,
        committer=commit_author,
    )

    commit_info: CommitInfo = _to_commit_info(revert_commit)

    assert commit_info.id, "revert commit must have a valid id"

    return commit_info


def _resolve_commit(repo: git.Repo, commit_id: str) -> git.Commit:
    """Resolve a commit id string to a ``git.Commit``, normalizing errors.

    Args:
        repo (git.Repo): Repository to resolve the commit against.
        commit_id (str): Commit SHA (full or abbreviated).

    Returns:
        git.Commit: Resolved commit.

    Raises:
        LookupError: If ``commit_id`` does not resolve to a commit.
    """
    try:
        return repo.commit(commit_id)
    except (
        git.BadName,
        git.BadObject,
        git.GitCommandError,
        ValueError,
    ) as error:
        raise LookupError(f"unknown commit: {commit_id}") from error
