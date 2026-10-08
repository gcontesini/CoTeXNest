"""File-level operations within a single project's working directory.

Split out of ``routers/projects.py`` so the AI assistant subsystem
(``services/assistant.py``) can read/write/create/delete/rename files
through the exact same path-safety and tree-building logic the HTTP
API uses, instead of a second, divergent implementation.

Raises plain Python exceptions, not ``HTTPException``: ``routers/
projects.py`` translates ``FileNotFoundError`` to 404 and ``ValueError``
to 400, the same convention ``git_history.py`` and ``latex_render.py``
already use.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .. import config
from ..models import FileNode

_HIDDEN_TREE_ENTRIES: set[str] = {".git", ".project.json"}


def get_project_dir(project_id: str) -> Path:
    """Resolve a project id to its directory, validating it exists.

    Args:
        project_id (str): Project identifier.

    Returns:
        Path: The resolved, existing project directory.

    Raises:
        ValueError: On path traversal attempts.
        FileNotFoundError: If the project does not exist.
    """
    project_root = config.project_dir(project_id)

    if not project_root.is_dir():
        raise FileNotFoundError(f"project not found: {project_id!r}")

    return project_root


def resolve_safe_path(project_root: Path, relative_path: str) -> Path:
    """Resolve a file path within a project, rejecting path traversal.

    Args:
        project_root (Path): Resolved root directory of the project.
        relative_path (str): Path relative to ``project_root``, as
            received from a caller (may contain ``..``).

    Returns:
        Path: The resolved, absolute path, guaranteed to lie inside
        ``project_root``.

    Raises:
        ValueError: If the resolved path escapes the project directory.
    """
    project_root_resolved = project_root.resolve()
    candidate = (project_root_resolved / relative_path).resolve()

    is_inside = candidate == project_root_resolved or str(
        candidate
    ).startswith(str(project_root_resolved) + os.sep)

    if not is_inside:
        raise ValueError(f"invalid path: {relative_path!r}")

    return candidate


def build_file_tree(directory: Path, project_root: Path) -> FileNode:
    """Recursively build a ``FileNode`` tree for a directory.

    Args:
        directory (Path): Directory to describe (``project_root`` on
            the initial call, a subdirectory on recursive calls).
        project_root (Path): Resolved project root, used to compute
            relative paths for every node.

    Returns:
        FileNode: A node of type "dir" with one child per visible
        entry, directories before files, skipping ``.git`` and the
        ``.project.json`` metadata sidecar.
    """
    relative = directory.relative_to(project_root)
    node_path = "" if relative == Path(".") else relative.as_posix()

    entries = sorted(
        directory.iterdir(), key=lambda entry: (entry.is_file(), entry.name)
    )

    children: list[FileNode] = []
    for entry in entries:
        if entry.name in _HIDDEN_TREE_ENTRIES:
            continue

        if entry.is_dir():
            children.append(build_file_tree(entry, project_root))
        else:
            entry_path = entry.relative_to(project_root).as_posix()
            children.append(FileNode(path=entry_path, type="file"))

    return FileNode(path=node_path, type="dir", children=children)


def get_file_tree(project_id: str) -> FileNode:
    """Return the full file tree for a project.

    Args:
        project_id (str): Project identifier.

    Returns:
        FileNode: The root node (type "dir", path "").

    Raises:
        ValueError: On path traversal attempts.
        FileNotFoundError: If the project does not exist.
    """
    project_root = get_project_dir(project_id)
    return build_file_tree(project_root, project_root)


def read_file(project_id: str, file_path: str) -> str:
    """Read a single file's UTF-8 text content from a project.

    Args:
        project_id (str): Project identifier.
        file_path (str): Path to the file, relative to the project
            root.

    Returns:
        str: The file's text content.

    Raises:
        ValueError: On path traversal, or non-UTF-8 content.
        FileNotFoundError: If the project or file does not exist.
    """
    project_root = get_project_dir(project_id)
    target = resolve_safe_path(project_root, file_path)

    if not target.is_file():
        raise FileNotFoundError(f"file not found: {file_path!r}")

    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("file is not valid UTF-8 text") from error


def write_file(project_id: str, file_path: str, content: str) -> None:
    """Write (creating or overwriting) a file's text content.

    Args:
        project_id (str): Project identifier.
        file_path (str): Path to the file, relative to the project
            root. Parent directories are created as needed.
        content (str): New file content.

    Raises:
        ValueError: On path traversal attempts.
        FileNotFoundError: If the project does not exist.
    """
    project_root = get_project_dir(project_id)
    target = resolve_safe_path(project_root, file_path)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def create_entry(project_id: str, path: str, entry_type: str) -> FileNode:
    """Create a new, empty file or directory within a project.

    Refuses to clobber something that already exists at ``path``.

    Args:
        project_id (str): Project identifier.
        path (str): Target path, relative to the project root.
        entry_type (str): Either ``"file"`` or ``"dir"``.

    Returns:
        FileNode: Describing the newly created entry.

    Raises:
        ValueError: On path traversal, an unknown ``entry_type``, or
            if something already exists at ``path``.
        FileNotFoundError: If the project does not exist.
    """
    if entry_type not in ("file", "dir"):
        raise ValueError('entry_type must be "file" or "dir"')

    project_root = get_project_dir(project_id)
    target = resolve_safe_path(project_root, path)

    if target.exists():
        raise ValueError(f"a file or directory already exists at {path!r}")

    if entry_type == "dir":
        target.mkdir(parents=True, exist_ok=False)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch(exist_ok=False)

    return FileNode(path=path, type=entry_type)


def delete_entry(project_id: str, path: str) -> None:
    """Delete a file (or directory, recursively) from a project.

    Args:
        project_id (str): Project identifier.
        path (str): Path to the file or directory, relative to the
            project root.

    Raises:
        ValueError: On path traversal attempts.
        FileNotFoundError: If the project or the entry does not exist.
    """
    project_root = get_project_dir(project_id)
    target = resolve_safe_path(project_root, path)

    if not target.exists():
        raise FileNotFoundError(f"file not found: {path!r}")

    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


def rename_entry(project_id: str, path: str, new_path: str) -> FileNode:
    """Rename or move a file or directory within a project.

    ``new_path`` is a full project-relative destination, so this
    doubles as a "move".

    Args:
        project_id (str): Project identifier.
        path (str): Current path, relative to the project root.
        new_path (str): Destination path, relative to the project
            root.

    Returns:
        FileNode: Describing the entry at its new path.

    Raises:
        ValueError: On path traversal, or if something already exists
            at ``new_path``.
        FileNotFoundError: If the project or the source does not exist.
    """
    project_root = get_project_dir(project_id)
    source = resolve_safe_path(project_root, path)

    if not source.exists():
        raise FileNotFoundError(f"file not found: {path!r}")

    destination = resolve_safe_path(project_root, new_path)

    if destination.exists():
        raise ValueError(f"something already exists at {new_path!r}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)

    return FileNode(
        path=new_path, type="dir" if destination.is_dir() else "file"
    )
