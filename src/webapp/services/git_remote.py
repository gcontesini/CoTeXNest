"""Remote, credentials and branch management for a project's repository.

Backs Settings > Git. Complements ``git_history`` (commit/diff/revert and
the one-click "Sync Changes"): this module exposes the individual steps
-- set the remote URL, store credentials, fetch, pull, push, list and
switch branches.

Credential storage: an HTTPS remote's token is kept as the userinfo part
of the ``origin`` URL (``https://user:token@host/path``) in the project's
``.git/config``. That is plain text inside the workspace volume; it is
the only mechanism git offers without a credential helper, and this
headless process has none. The token is never returned by any function
here -- ``GitInfo`` carries only a flag and the username -- and
``git_history.redact_url_credentials`` scrubs it from git error text.
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import git

from ..models import GitActionResult, GitBranches, GitInfo
from .git_history import _git_error_detail, _open_repo, redact_url_credentials

_HTTPS_URL_PATTERN = re.compile(r"https://(?:[^\s/@]+@)?[^\s/@]+(?:/\S*)?")
_SSH_URL_PATTERN = re.compile(r"ssh://[^\s/@]+@[^\s/@]+(?:/\S*)?")
_SCP_URL_PATTERN = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s:]\S*")
_BRANCH_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_NO_REMOTE_MESSAGE = "this project has no remote configured -- set a Remote URL first"


def _validate_remote_url(url_: str) -> str:
    """Return ``url_`` stripped, or raise ``ValueError`` if unsafe.

    Only ``https://``, ``ssh://`` and ``user@host:path`` forms are
    accepted. A leading ``-`` (option injection), ``ext::`` transport
    helpers, ``file://`` and plain ``http://`` (credentials in clear
    text) are all rejected by construction, since none match.
    """
    url: str = url_.strip()

    if not url:
        raise ValueError("remote URL must not be empty")

    if any(pattern.fullmatch(url) for pattern in (
        _HTTPS_URL_PATTERN,
        _SSH_URL_PATTERN,
        _SCP_URL_PATTERN,
    )):
        return url

    raise ValueError(
        "unsupported remote URL -- use https://host/owner/repo.git, "
        "ssh://git@host/owner/repo.git or git@host:owner/repo.git"
    )


def _origin_url(repo_: git.Repo) -> str | None:
    if "origin" not in (remote.name for remote in repo_.remotes):
        return None

    return repo_.remotes.origin.url


def _require_origin_url(repo_: git.Repo) -> str:
    url: str | None = _origin_url(repo_)

    if url is None:
        raise ValueError(_NO_REMOTE_MESSAGE)

    return url


def _current_branch(repo_: git.Repo) -> str | None:
    try:
        return repo_.active_branch.name
    except TypeError:
        return None


def _require_branch(repo_: git.Repo) -> str:
    branch: str | None = _current_branch(repo_)

    if branch is None:
        raise ValueError(
            "this project's repository has no current branch (detached "
            "HEAD) -- switch to a branch first"
        )

    return branch


def _split_userinfo(url_: str) -> tuple[str | None, str | None]:
    """Return ``(username, password)`` embedded in an https/ssh URL."""
    if "://" not in url_:
        return None, None

    parts = urlsplit(url_)
    username = unquote(parts.username) if parts.username else None
    password = unquote(parts.password) if parts.password else None

    return username, password


def _display_url(url_: str) -> str:
    """Return ``url_`` without any userinfo (never shows a token)."""
    if "://" not in url_:
        return url_

    parts = urlsplit(url_)
    if parts.scheme != "https" or parts.username is None:
        return url_

    host: str = parts.hostname or ""
    netloc: str = f"{host}:{parts.port}" if parts.port else host

    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _with_userinfo(url_: str, username_: str, token_: str) -> str:
    parts = urlsplit(url_)
    host: str = parts.hostname or ""
    host_port: str = f"{host}:{parts.port}" if parts.port else host
    userinfo: str = f"{quote(username_, safe='')}:{quote(token_, safe='')}"

    return urlunsplit(
        (parts.scheme, f"{userinfo}@{host_port}", parts.path, parts.query, parts.fragment)
    )


def _ahead_behind(repo_: git.Repo, branch_: str | None) -> tuple[bool, int, int]:
    """Return ``(remote_branch_exists, ahead, behind)`` vs ``origin/<branch>``."""
    if branch_ is None or _origin_url(repo_) is None:
        return False, 0, 0

    remote_ref: str = f"origin/{branch_}"

    if remote_ref not in (ref.name for ref in repo_.refs):
        return False, 0, 0

    ahead: int = sum(1 for _ in repo_.iter_commits(f"{remote_ref}..{branch_}"))
    behind: int = sum(1 for _ in repo_.iter_commits(f"{branch_}..{remote_ref}"))

    return True, ahead, behind


def _build_info(repo_: git.Repo) -> GitInfo:
    url: str | None = _origin_url(repo_)
    branch: str | None = _current_branch(repo_)
    exists, ahead, behind = _ahead_behind(repo_, branch)
    is_https: bool = url is not None and url.startswith("https://")
    username, password = _split_userinfo(url) if is_https else (None, None)

    return GitInfo(
        remote_url=_display_url(url) if url else None,
        has_credentials=username is not None,
        credential_username=username if password is not None else None,
        branch=branch,
        remote_branch_exists=exists,
        ahead=ahead,
        behind=behind,
    )


def get_git_info(project_id: str) -> GitInfo:
    """Return the remote, credential flag, branch and ahead/behind counts.

    Raises:
        FileNotFoundError: If the project does not exist.
    """
    assert project_id, "project_id must be a non-empty string"

    return _build_info(_open_repo(project_id))


def set_remote_url(project_id: str, url_: str) -> GitInfo:
    """Create or update ``origin``.

    Stored credentials are kept only when the new URL is HTTPS on the
    same host as the old one, so a token is never sent to a different
    server than the one it was issued for.

    Raises:
        FileNotFoundError: If the project does not exist.
        ValueError: If the URL is empty or not an accepted form.
    """
    assert project_id, "project_id must be a non-empty string"

    new_url: str = _validate_remote_url(url_)
    repo: git.Repo = _open_repo(project_id)
    old_url: str | None = _origin_url(repo)

    if old_url is not None and "://" in new_url and "://" in old_url:
        old_parts = urlsplit(old_url)
        new_parts = urlsplit(new_url)
        old_user, old_password = _split_userinfo(old_url)
        new_user, _ = _split_userinfo(new_url)

        if (
            old_password is not None
            and new_user is None
            and old_parts.scheme == "https"
            and new_parts.scheme == "https"
            and old_parts.hostname == new_parts.hostname
        ):
            new_url = _with_userinfo(new_url, old_user or "", old_password)

    if old_url is None:
        repo.create_remote("origin", new_url)
    else:
        repo.remotes.origin.set_url(new_url)

    return _build_info(repo)


def set_credentials(project_id: str, username_: str, token_: str) -> GitInfo:
    """Store ``username_`` and ``token_`` in the HTTPS ``origin`` URL.

    Raises:
        FileNotFoundError: If the project does not exist.
        ValueError: If there is no remote, it is not HTTPS, or either
            value is empty or contains whitespace.
    """
    assert project_id, "project_id must be a non-empty string"

    if not username_ or not token_ or re.search(r"\s", username_ + token_):
        raise ValueError("username and token must be non-empty and contain no whitespace")

    repo: git.Repo = _open_repo(project_id)
    current_url: str = _require_origin_url(repo)

    if not current_url.startswith("https://"):
        raise ValueError(
            "credentials can only be stored for an https:// remote; "
            "ssh remotes authenticate with keys"
        )

    repo.remotes.origin.set_url(_with_userinfo(current_url, username_, token_))

    return _build_info(repo)


def clear_credentials(project_id: str) -> GitInfo:
    """Remove any stored username and token from ``origin``.

    Raises:
        FileNotFoundError: If the project does not exist.
        ValueError: If the project has no remote.
    """
    assert project_id, "project_id must be a non-empty string"

    repo: git.Repo = _open_repo(project_id)
    current_url: str = _require_origin_url(repo)

    repo.remotes.origin.set_url(_display_url(current_url))

    return _build_info(repo)


def _fetch(repo_: git.Repo) -> None:
    try:
        repo_.remotes.origin.fetch()
    except git.GitCommandError as error:
        raise ConnectionError(
            f"could not fetch from the remote: {_git_error_detail(error)}"
        ) from error


def fetch_remote(project_id: str) -> GitActionResult:
    """``git fetch origin`` and report the resulting ahead/behind counts.

    Raises:
        FileNotFoundError: If the project does not exist.
        ValueError: If the project has no remote.
        ConnectionError: If the fetch fails (network, authentication).
    """
    assert project_id, "project_id must be a non-empty string"

    repo: git.Repo = _open_repo(project_id)
    _require_origin_url(repo)
    _fetch(repo)

    info: GitInfo = _build_info(repo)

    return GitActionResult(
        message=f"Fetched. {info.behind} commit(s) to pull, {info.ahead} to push.",
        info=info,
    )


def pull_remote(project_id: str) -> GitActionResult:
    """Fetch, then rebase the current branch onto ``origin/<branch>``.

    Refuses when tracked files have uncommitted changes. A rebase
    conflict is aborted so the repository is left exactly as it was.

    Raises:
        FileNotFoundError: If the project does not exist.
        ValueError: If there is no remote or current branch, there are
            uncommitted changes, the remote lacks this branch, or the
            rebase conflicts.
        ConnectionError: If the fetch fails.
    """
    assert project_id, "project_id must be a non-empty string"

    repo: git.Repo = _open_repo(project_id)
    _require_origin_url(repo)
    branch: str = _require_branch(repo)

    if repo.is_dirty(untracked_files=False):
        raise ValueError(
            "uncommitted changes in tracked files -- save a version or "
            "use Sync Changes before pulling"
        )

    _fetch(repo)

    remote_ref: str = f"origin/{branch}"
    if remote_ref not in (ref.name for ref in repo.refs):
        raise ValueError(f"the remote has no branch {remote_ref!r} to pull")

    before: str = repo.head.commit.hexsha

    try:
        repo.git.rebase(remote_ref)
    except git.GitCommandError as error:
        repo.git.rebase("--abort")
        raise ValueError(
            "pull stopped: rebasing onto the remote hit a conflict this app "
            "will not resolve automatically (nothing was changed -- resolve "
            f"it with git in the project's folder): {_git_error_detail(error)}"
        ) from error

    pulled: int = sum(1 for _ in repo.iter_commits(f"{before}..{remote_ref}"))

    return GitActionResult(
        message=f"Pulled {pulled} commit(s) from {remote_ref}.",
        info=_build_info(repo),
    )


def push_remote(project_id: str) -> GitActionResult:
    """Push the current branch to ``origin``, setting upstream.

    Raises:
        FileNotFoundError: If the project does not exist.
        ValueError: If there is no remote or current branch.
        ConnectionError: If the push fails or the remote rejects it
            (for example because the remote branch has newer commits).
    """
    assert project_id, "project_id must be a non-empty string"

    repo: git.Repo = _open_repo(project_id)
    _require_origin_url(repo)
    branch: str = _require_branch(repo)

    try:
        push_info = repo.remotes.origin.push(
            f"{branch}:{branch}", set_upstream=True
        )[0]
    except git.GitCommandError as error:
        raise ConnectionError(
            f"could not push to the remote: {_git_error_detail(error)}"
        ) from error

    failure_flags: int = (
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

    return GitActionResult(
        message=f"Pushed {branch} to origin.",
        info=_build_info(repo),
    )


def list_branches(project_id: str) -> GitBranches:
    """List local branches and remote branches with no local counterpart.

    Remote branches are those last fetched; run Fetch to refresh them.

    Raises:
        FileNotFoundError: If the project does not exist.
    """
    assert project_id, "project_id must be a non-empty string"

    repo: git.Repo = _open_repo(project_id)
    local: list[str] = sorted(head.name for head in repo.heads)
    remote_names: set[str] = set()

    if _origin_url(repo) is not None:
        for ref in repo.remotes.origin.refs:
            if ref.remote_head != "HEAD":
                remote_names.add(ref.remote_head)

    return GitBranches(
        current=_current_branch(repo),
        local=local,
        remote_only=sorted(remote_names - set(local)),
    )


def _validate_branch_name(repo_: git.Repo, name_: str) -> str:
    name: str = name_.strip()

    if not _BRANCH_NAME_PATTERN.match(name):
        raise ValueError(f"invalid branch name: {name_!r}")

    try:
        repo_.git.check_ref_format("--branch", name)
    except git.GitCommandError as error:
        raise ValueError(f"invalid branch name: {name_!r}") from error

    return name


def switch_branch(project_id: str, name_: str, create: bool = False) -> GitBranches:
    """Switch to a branch, create one from ``HEAD``, or track a remote one.

    Refuses when tracked files have uncommitted changes, so no work can
    be overwritten by the checkout.

    Raises:
        FileNotFoundError: If the project does not exist.
        ValueError: If the name is invalid, the branch does not exist
            (or already exists when creating), or there are uncommitted
            changes.
    """
    assert project_id, "project_id must be a non-empty string"

    repo: git.Repo = _open_repo(project_id)
    name: str = _validate_branch_name(repo, name_)
    branches: GitBranches = list_branches(project_id)

    if create and (name in branches.local or name in branches.remote_only):
        raise ValueError(f"branch {name!r} already exists")

    if not create and name not in branches.local and name not in branches.remote_only:
        raise ValueError(f"branch {name!r} does not exist")

    if repo.is_dirty(untracked_files=False):
        raise ValueError(
            "uncommitted changes in tracked files -- save a version or "
            "use Sync Changes before switching branches"
        )

    try:
        if create:
            repo.git.switch("-c", name)
        elif name in branches.local:
            repo.git.switch(name)
        else:
            repo.git.switch("-c", name, "--track", f"origin/{name}")
    except git.GitCommandError as error:
        raise ValueError(
            f"could not switch to {name!r}: {_git_error_detail(error)}"
        ) from error

    return list_branches(project_id)
