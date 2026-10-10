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

import re
import shutil
from pathlib import Path

import git

from .. import config
from ..models import CommitInfo, DiffEntry, ProjectSyncResult

# Cap on how much diff text ever gets sent to the AI assistant for
# commit-message generation -- a small local model's context window is
# limited, and a huge diff would either get silently truncated by the
# model itself or blow past it entirely. Measured in characters, not
# tokens, since this module has no tokenizer available; a generous
# overestimate (roughly 1 token per 4 characters) keeps this well
# under even a modest 2048-token context.
_MAX_DIFF_CHARS_FOR_COMMIT_MESSAGE = 6000


class NeedsCommitMessage(Exception):
    """Raised by ``sync_with_remote`` when uncommitted changes exist but
    no ``commit_message`` was given.

    The caller (the router) is expected to try generating one (e.g. via
    the AI assistant, using ``diff_text``) and call ``sync_with_remote``
    again with the result -- or, failing that, ask the user directly.
    """

    def __init__(self, changed_files: list[str], diff_text: str) -> None:
        super().__init__("uncommitted changes need a commit message before syncing")
        self.changed_files = changed_files
        self.diff_text = diff_text


_CREDENTIALS_IN_URL_PATTERN = re.compile(r"://[^/@\s]+@")


def redact_url_credentials(text: str) -> str:
    """Strip embedded basic-auth credentials from any URL in ``text``.

    A remote URL can legitimately look like
    ``https://token@github.com/owner/repo.git`` -- convenient for
    scripting, but not something that should ever come back verbatim
    in an API response, a toast, or a log line. Used on every git
    error message this module (and project creation's clone path)
    surfaces, since git itself often echoes the exact URL it was given
    back in a failure message.

    Args:
        text (str): Arbitrary text that may contain one or more URLs.

    Returns:
        str: Same text with any ``scheme://user:pass@`` or
        ``scheme://token@`` prefix replaced with ``scheme://***@``.
    """
    return _CREDENTIALS_IN_URL_PATTERN.sub("://***@", text)


def _git_error_detail(error: git.GitCommandError) -> str:
    """Pull a clean, credential-redacted message out of a GitCommandError.

    Args:
        error (git.GitCommandError): The error raised by a git
            subprocess call.

    Returns:
        str: ``error.stderr`` if captured, else ``str(error)`` -- both
        redacted.
    """
    raw = error.stderr.strip() if error.stderr else str(error)
    return redact_url_credentials(raw)


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


def _staged_diff_text(repo: git.Repo) -> str:
    """Render the staged (index-vs-HEAD) diff as text, capped in size.

    Used only to feed the AI assistant a commit-message prompt -- not
    shown to the user directly, so truncating is an acceptable
    trade-off for staying within a small model's context window.

    Args:
        repo (git.Repo): Repository with changes already staged
            (``repo.git.add(A=True)`` already called).

    Returns:
        str: The diff text, truncated with a note if it was too long.
    """
    try:
        diff_text = repo.git.diff("--cached")
    except git.GitCommandError:
        diff_text = ""

    if len(diff_text) > _MAX_DIFF_CHARS_FOR_COMMIT_MESSAGE:
        diff_text = (
            diff_text[:_MAX_DIFF_CHARS_FOR_COMMIT_MESSAGE]
            + "\n... (diff truncated for length)"
        )

    return diff_text


def sync_with_remote(
    project_id: str,
    commit_message: str | None = None,
    author: str = "anonymous",
) -> ProjectSyncResult:
    """Commit any local changes, rebase onto the remote, then push.

    Mirrors a typical editor's "Sync Changes" action: if the working
    tree is dirty, commit everything first (so local work is never
    lost by a later step); fetch and rebase local commits on top of
    ``origin/<current branch>`` if it already exists (a no-op the
    first time a brand-new remote is synced); then push, establishing
    upstream tracking if this is the first push.

    A rebase conflict aborts the rebase (restoring the pre-rebase
    state exactly) and raises, rather than leaving the repository in a
    conflicted, partially-rebased state for the caller to discover
    later -- this app has no conflict-resolution UI, so asking the
    user to resolve it directly with git in the project's folder is
    the honest option, not a silent ``git checkout --theirs`` or
    similar that could discard one side's changes without asking.

    Args:
        project_id (str): Identifier of the project under
            ``config.WORKSPACE_ROOT``.
        commit_message (str | None): Message for the commit of any
            uncommitted changes. Required only if the working tree is
            actually dirty; ignored otherwise.
        author (str): Display name recorded as the commit author, if a
            commit is made.

    Returns:
        ProjectSyncResult: What happened -- whether a commit was made,
        how many remote commits were pulled in, and whether a push
        happened.

    Raises:
        FileNotFoundError: If the project directory does not exist or
            is not a git repository.
        NeedsCommitMessage: If the working tree is dirty and no
            ``commit_message`` was given -- the caller should try to
            generate one (``exc.diff_text`` is provided for this) and
            call again, or ask the user directly.
        ValueError: If the project has no ``origin`` remote
            configured, its repository is in a detached-HEAD state, or
            rebasing onto the remote hit a conflict.
        ConnectionError: If fetching from or pushing to the remote
            failed (network error, auth failure, rejected push, etc.).
    """
    assert author.strip(), "author must not be empty"

    repo: git.Repo = _open_repo(project_id)

    if "origin" not in (remote.name for remote in repo.remotes):
        raise ValueError(
            "this project has no remote configured -- link one first "
            "(see the README for how to set a project's git remote)"
        )

    committed = False
    used_commit_message: str | None = None

    if repo.is_dirty(untracked_files=True):
        repo.git.add(A=True)

        if not commit_message or not commit_message.strip():
            changed_files = list(repo.index.diff("HEAD"))
            changed_paths = sorted(
                {item.b_path or item.a_path for item in changed_files}
                | set(repo.untracked_files)
            )
            raise NeedsCommitMessage(changed_paths, _staged_diff_text(repo))

        commit_author = git.Actor(author, f"{author}@localhost")
        repo.index.commit(
            commit_message.strip(), author=commit_author, committer=commit_author
        )
        committed = True
        used_commit_message = commit_message.strip()

    try:
        branch_name = repo.active_branch.name
    except TypeError as error:
        raise ValueError(
            "this project's repository has no current branch (detached "
            "HEAD) -- this shouldn't normally happen through the app; "
            "resolve it directly with git in the project's folder"
        ) from error

    try:
        repo.remotes.origin.fetch()
    except git.GitCommandError as error:
        raise ConnectionError(
            f"could not fetch from the remote: {_git_error_detail(error)}"
        ) from error

    remote_ref_name = f"origin/{branch_name}"
    has_remote_branch = remote_ref_name in (ref.name for ref in repo.refs)

    pulled_commits = 0
    if has_remote_branch:
        before_rebase = repo.head.commit.hexsha
        try:
            repo.git.rebase(remote_ref_name)
        except git.GitCommandError as error:
            repo.git.rebase("--abort")
            raise ValueError(
                "syncing stopped: rebasing your changes onto the remote's "
                "hit a conflict this app won't resolve automatically "
                f"(nothing was changed -- resolve it directly with git in "
                f"the project's folder, then try Sync again): "
                f"{_git_error_detail(error)}"
            ) from error

        pulled_commits = sum(
            1 for _ in repo.iter_commits(f"{before_rebase}..{remote_ref_name}")
        )

    try:
        push_info = repo.remotes.origin.push(
            f"{branch_name}:{branch_name}", set_upstream=True
        )[0]
    except git.GitCommandError as error:
        raise ConnectionError(
            f"could not push to the remote: {_git_error_detail(error)}"
        ) from error

    failure_flags = (
        push_info.ERROR
        | push_info.REJECTED
        | push_info.REMOTE_REJECTED
        | push_info.REMOTE_FAILURE
    )
    if push_info.flags & failure_flags:
        raise ConnectionError(
            "push rejected by the remote: "
            f"{redact_url_credentials(push_info.summary.strip())}"
        )

    return ProjectSyncResult(
        status="synced",
        committed=committed,
        commit_message=used_commit_message,
        pulled_commits=pulled_commits,
        pushed=True,
    )
