"""Project and file management endpoints.

Covers the CRUD surface for projects (self-contained git repositories
living under ``config.WORKSPACE_ROOT``) and for the files inside each
project. Compilation, history, and package management live in their own
routers and are wired together in ``webapp.main``.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from webapp import config
from webapp.models import (
    FileContent,
    FileCreateRequest,
    FileNode,
    FileRenameRequest,
    FileUploadResult,
    FileWrite,
    ProjectCreate,
    ProjectSummary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["projects"])

# Per-project metadata sidecar file. Not exposed through the file-tree
# endpoints since it is an implementation detail of this router, not a
# user-authored project file.
_PROJECT_METADATA_FILENAME = ".project.json"

_DEFAULT_MAIN_TEX = (
    "\\documentclass{article}\n\n"
    "\\begin{document}\n"
    "Hello, world!\n"
    "\\end{document}\n"
)

_HIDDEN_TREE_ENTRIES = {".git", _PROJECT_METADATA_FILENAME}


# ─── Internal helpers ───


def _slugify(name: str) -> str:
    """Convert an arbitrary project name into a URL-safe slug fragment.

    Args:
        name: Human-provided project name.

    Returns:
        Lowercase, hyphen-separated slug with no leading/trailing
        hyphens, truncated to 60 characters. Falls back to "project"
        if the name has no alphanumeric characters at all.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug[:60] or "project"


def _generate_project_id(name: str) -> str:
    """Generate a unique, collision-checked project id for a new project.

    Args:
        name: Human-provided project name, used as the slug base.

    Returns:
        A string of the form ``"<slug>-<6 hex chars>"`` that does not
        already exist as a directory under ``config.WORKSPACE_ROOT``.

    Raises:
        HTTPException: 500 if no unique id could be found after several
            attempts (practically unreachable given the suffix space).
    """
    base_slug = _slugify(name)

    for _ in range(10):
        candidate = f"{base_slug}-{secrets.token_hex(3)}"
        if not (config.WORKSPACE_ROOT / candidate).exists():
            return candidate

    raise HTTPException(
        status_code=500, detail="failed to generate a unique project id"
    )


def _read_project_metadata(project_root: Path) -> dict[str, object] | None:
    """Read and parse a project's ``.project.json`` sidecar file.

    Args:
        project_root: Resolved directory of the project.

    Returns:
        The parsed metadata dict, or None if the file is missing or
        unparsable (the project is then skipped by the listing
        endpoint rather than raising).
    """
    metadata_path = project_root / _PROJECT_METADATA_FILENAME

    if not metadata_path.is_file():
        return None

    try:
        return json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "failed to read project metadata at %s: %s", metadata_path, exc
        )
        return None


def _write_project_metadata(
    project_root: Path, metadata: dict[str, object]
) -> None:
    """Write a project's ``.project.json`` sidecar file.

    Args:
        project_root: Resolved directory of the project.
        metadata: JSON-serializable metadata dict (name, created_at,
            main_file).
    """
    metadata_path = project_root / _PROJECT_METADATA_FILENAME
    metadata_path.write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )


def _get_existing_project_dir(project_id: str) -> Path:
    """Resolve a project id to its directory, validating it exists.

    Args:
        project_id: Project identifier from the URL path.

    Returns:
        The resolved, existing project directory.

    Raises:
        HTTPException: 400 on path traversal attempts, 404 if the
            project does not exist.
    """
    try:
        project_root = config.project_dir(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not project_root.is_dir():
        raise HTTPException(status_code=404, detail="project not found")

    return project_root


def _resolve_safe_path(project_root: Path, relative_path: str) -> Path:
    """Resolve a file path within a project, rejecting path traversal.

    Args:
        project_root: Resolved root directory of the project.
        relative_path: Path relative to ``project_root``, as received
            from the URL (may contain ``..`` from a malicious client).

    Returns:
        The resolved, absolute path, guaranteed to lie inside
        ``project_root``.

    Raises:
        HTTPException: 400 if the resolved path escapes the project
            directory.
    """
    project_root_resolved = project_root.resolve()
    candidate = (project_root_resolved / relative_path).resolve()

    is_inside = candidate == project_root_resolved or str(
        candidate
    ).startswith(str(project_root_resolved) + os.sep)

    if not is_inside:
        raise HTTPException(status_code=400, detail="invalid path")

    return candidate


def _build_file_tree(directory: Path, project_root: Path) -> FileNode:
    """Recursively build a FileNode tree for a directory.

    Args:
        directory: Directory to describe (project_root on the initial
            call, a subdirectory on recursive calls).
        project_root: Resolved project root, used to compute relative
            paths for every node.

    Returns:
        A FileNode of type "dir" with one child per visible entry,
        directories before files, skipping ``.git`` and the
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
            children.append(_build_file_tree(entry, project_root))
        else:
            entry_path = entry.relative_to(project_root).as_posix()
            children.append(FileNode(path=entry_path, type="file"))

    return FileNode(path=node_path, type="dir", children=children)


def _apply_template(project_root: Path, template_id: str) -> bool:
    """Populate a new project directory from a catalog template.

    Args:
        project_root: Newly created, empty project directory.
        template_id: ``id`` of a ``CatalogEntry`` with ``kind ==
            "template"`` in ``config.CATALOG_PATH``.

    Returns:
        True if a matching template was found and its files were
        written, False otherwise (caller should fall back to the
        default main file).
    """
    if not config.CATALOG_PATH.is_file():
        logger.warning("catalog file not found at %s", config.CATALOG_PATH)
        return False

    try:
        catalog = json.loads(config.CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("failed to read catalog: %s", exc)
        return False

    entry = next(
        (
            item
            for item in catalog
            if item.get("id") == template_id and item.get("kind") == "template"
        ),
        None,
    )

    if entry is None:
        return False

    for file_spec in entry.get("files", []):
        file_name = file_spec.get("name")
        content = file_spec.get("content", "")

        if not file_name:
            continue

        try:
            target = _resolve_safe_path(project_root, file_name)
        except HTTPException:
            logger.warning(
                "template %s file escapes project root: %s",
                template_id,
                file_name,
            )
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    return True


def _init_git_repo(project_root: Path) -> None:
    """Initialize a git repository with an initial commit for a project.

    Best-effort: a missing ``git`` binary or any other git failure is
    logged as a warning and swallowed so project creation still
    succeeds without version history.

    Args:
        project_root: Directory to initialize as a git repository.
    """
    try:
        import git

        repo = git.Repo.init(project_root)
        repo.index.add(["."])
        repo.index.commit(
            "Initial commit",
            author=git.Actor("webapp", "webapp@localhost"),
        )
    except Exception as exc:  # noqa: BLE001 - git must never block creation
        logger.warning(
            "failed to initialize git repo at %s: %s", project_root, exc
        )


# ─── Routes ───


@router.get("", response_model=list[ProjectSummary])
def list_projects() -> list[ProjectSummary]:
    """List every project with valid metadata under the workspace root.

    Returns:
        One ProjectSummary per immediate subdirectory of
        ``config.WORKSPACE_ROOT`` that contains a readable
        ``.project.json``. Directories without it are silently
        skipped.
    """
    config.ensure_dirs()

    summaries: list[ProjectSummary] = []
    for entry in sorted(config.WORKSPACE_ROOT.iterdir()):
        if not entry.is_dir():
            continue

        metadata = _read_project_metadata(entry)
        if metadata is None:
            continue

        summaries.append(
            ProjectSummary(
                id=entry.name,
                name=str(metadata.get("name", entry.name)),
                created_at=float(metadata.get("created_at", 0.0)),
                main_file=str(
                    metadata.get("main_file", config.DEFAULT_MAIN_FILE)
                ),
            )
        )

    return summaries


@router.post("", response_model=ProjectSummary)
def create_project(payload: ProjectCreate) -> ProjectSummary:
    """Create a new project, optionally seeded from a catalog template.

    Args:
        payload: Project name and optional ``template_id``.

    Returns:
        The ProjectSummary for the newly created project.

    Raises:
        HTTPException: 400 if the generated project id is somehow
            invalid, 500 if a unique id cannot be generated.
    """
    config.ensure_dirs()

    project_id = _generate_project_id(payload.name)

    try:
        project_root = config.project_dir(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    project_root.mkdir(parents=True, exist_ok=False)

    template_applied = False
    if payload.template_id is not None:
        template_applied = _apply_template(project_root, payload.template_id)

    if not template_applied:
        main_path = project_root / config.DEFAULT_MAIN_FILE
        main_path.write_text(_DEFAULT_MAIN_TEX, encoding="utf-8")

    created_at = time.time()
    metadata: dict[str, object] = {
        "name": payload.name,
        "created_at": created_at,
        "main_file": config.DEFAULT_MAIN_FILE,
    }
    _write_project_metadata(project_root, metadata)

    _init_git_repo(project_root)

    return ProjectSummary(
        id=project_id,
        name=payload.name,
        created_at=created_at,
        main_file=config.DEFAULT_MAIN_FILE,
    )


@router.delete("/{project_id}")
def delete_project(project_id: str) -> dict[str, bool]:
    """Permanently delete a project and all of its files.

    Args:
        project_id: Project identifier.

    Returns:
        ``{"success": True}`` on success.

    Raises:
        HTTPException: 400 on path traversal attempts, 404 if the
            project does not exist.
    """
    project_root = _get_existing_project_dir(project_id)
    shutil.rmtree(project_root)
    return {"success": True}


@router.post("/{project_id}/files", response_model=FileNode)
def create_entry(project_id: str, payload: FileCreateRequest) -> FileNode:
    """Create a new, empty file or directory within a project.

    This is distinct from ``PUT .../files/{path}`` (which writes text
    content, creating or overwriting): this endpoint only creates an
    empty placeholder and refuses to clobber something that already
    exists at ``payload.path``, so "New File"/"New Folder" actions in
    the UI can never silently discard existing content.

    Args:
        project_id: Project identifier.
        payload: Target path (relative to the project root) and
            ``type``, either ``"file"`` or ``"dir"``.

    Returns:
        A FileNode describing the newly created entry.

    Raises:
        HTTPException: 400 on path traversal, an unknown ``type``, or
            if something already exists at that path; 404 if the
            project does not exist.
    """
    if payload.type not in ("file", "dir"):
        raise HTTPException(
            status_code=400, detail='type must be "file" or "dir"'
        )

    project_root = _get_existing_project_dir(project_id)
    target = _resolve_safe_path(project_root, payload.path)

    if target.exists():
        raise HTTPException(
            status_code=400,
            detail=f"a file or directory already exists at {payload.path!r}",
        )

    if payload.type == "dir":
        target.mkdir(parents=True, exist_ok=False)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch(exist_ok=False)

    return FileNode(path=payload.path, type=payload.type)


@router.get("/{project_id}/files", response_model=FileNode)
def get_file_tree(project_id: str) -> FileNode:
    """Return the full file tree for a project.

    Args:
        project_id: Project identifier.

    Returns:
        The root FileNode (type "dir", path ""), recursing into every
        subdirectory except ``.git``.

    Raises:
        HTTPException: 400 on path traversal attempts, 404 if the
            project does not exist.
    """
    project_root = _get_existing_project_dir(project_id)
    return _build_file_tree(project_root, project_root)


@router.get("/{project_id}/files/{file_path:path}/raw")
def read_file_raw(project_id: str, file_path: str) -> FileResponse:
    """Serve a file's raw bytes with a guessed content type.

    This exists for binary assets (images, in particular) that
    ``read_file`` below cannot return, since that endpoint assumes
    UTF-8 text. Used directly as an ``<img src="...">`` target by the
    frontend.

    IMPORTANT (route ordering): this path pattern must stay registered
    *before* ``read_file``'s ``GET /{project_id}/files/{file_path:path}``.
    Both use the greedy ``:path`` converter, which matches a URL like
    ``.../files/images/figure.png/raw`` against *either* pattern
    (``file_path`` would simply absorb the trailing ``/raw`` under the
    shorter pattern). FastAPI/Starlette dispatches to the first route
    whose method *and* path both match, so if the plain GET route were
    registered first, requests meant for this endpoint would silently
    be swallowed by it instead (and 404, since no file is literally
    named "...raw").

    Args:
        project_id: Project identifier.
        file_path: Path to the file, relative to the project root.

    Returns:
        The raw file bytes, with ``Content-Type`` guessed from the
        file extension (``application/octet-stream`` if unrecognized).

    Raises:
        HTTPException: 400 on path traversal attempts, 404 if the
            project or file does not exist.
    """
    project_root = _get_existing_project_dir(project_id)
    target = _resolve_safe_path(project_root, file_path)

    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")

    guessed_type, _ = mimetypes.guess_type(target.name)
    return FileResponse(target, media_type=guessed_type or "application/octet-stream")


@router.post(
    "/{project_id}/files/{file_path:path}/upload", response_model=FileUploadResult
)
async def upload_file(
    project_id: str, file_path: str, file: UploadFile = File(...)
) -> FileUploadResult:
    """Upload binary (or text) file content to an exact destination path.

    Unlike ``create_entry``, this overwrites an existing file at
    ``file_path`` -- re-uploading to the same path (e.g. replacing an
    image) is expected to just work, the same way ``write_file`` lets
    you re-save a text file.

    Args:
        project_id: Project identifier.
        file_path: Destination path, relative to the project root.
            Parent directories are created as needed.
        file: The uploaded file (multipart/form-data field ``file``).

    Returns:
        The destination path and the number of bytes written.

    Raises:
        HTTPException: 400 on path traversal attempts, 404 if the
            project does not exist.
    """
    project_root = _get_existing_project_dir(project_id)
    target = _resolve_safe_path(project_root, file_path)

    target.parent.mkdir(parents=True, exist_ok=True)
    contents = await file.read()
    target.write_bytes(contents)

    return FileUploadResult(
        path=file_path,
        size_bytes=len(contents),
        content_type=file.content_type,
    )


@router.get("/{project_id}/files/{file_path:path}", response_model=FileContent)
def read_file(project_id: str, file_path: str) -> FileContent:
    """Read a single file's text content from a project.

    Args:
        project_id: Project identifier.
        file_path: Path to the file, relative to the project root.

    Returns:
        The file's path and UTF-8 text content.

    Raises:
        HTTPException: 400 on path traversal or non-UTF-8 content,
            404 if the project or file does not exist.
    """
    project_root = _get_existing_project_dir(project_id)
    target = _resolve_safe_path(project_root, file_path)

    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")

    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400, detail="file is not valid UTF-8 text"
        ) from exc

    return FileContent(path=file_path, content=content)


@router.put("/{project_id}/files/{file_path:path}", response_model=FileContent)
def write_file(
    project_id: str, file_path: str, payload: FileWrite
) -> FileContent:
    """Write (creating or overwriting) a file's text content.

    Args:
        project_id: Project identifier.
        file_path: Path to the file, relative to the project root.
            Parent directories are created as needed.
        payload: New file content.

    Returns:
        The file's path and the content that was written.

    Raises:
        HTTPException: 400 on path traversal attempts, 404 if the
            project does not exist.
    """
    project_root = _get_existing_project_dir(project_id)
    target = _resolve_safe_path(project_root, file_path)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload.content, encoding="utf-8")

    return FileContent(path=file_path, content=payload.content)


@router.delete("/{project_id}/files/{file_path:path}")
def delete_file(project_id: str, file_path: str) -> dict[str, bool]:
    """Delete a file (or directory) from a project.

    Args:
        project_id: Project identifier.
        file_path: Path to the file or directory, relative to the
            project root.

    Returns:
        ``{"success": True}`` on success.

    Raises:
        HTTPException: 400 on path traversal attempts, 404 if the
            project or the project does not exist.
    """
    project_root = _get_existing_project_dir(project_id)
    target = _resolve_safe_path(project_root, file_path)

    if not target.exists():
        raise HTTPException(status_code=404, detail="file not found")

    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()

    return {"success": True}


@router.patch("/{project_id}/files/{file_path:path}", response_model=FileNode)
def rename_file(
    project_id: str, file_path: str, payload: FileRenameRequest
) -> FileNode:
    """Rename or move a file or directory within a project.

    ``payload.new_path`` is a full project-relative destination, so
    this doubles as a "move": renaming "images/fig.png" to
    "assets/figure-1.png" both renames and relocates it in one call.

    Args:
        project_id: Project identifier.
        file_path: Current path, relative to the project root.
        payload: Destination path, relative to the project root.

    Returns:
        A FileNode for the entry at its new path.

    Raises:
        HTTPException: 400 on path traversal attempts or if something
            already exists at the destination, 404 if the source does
            not exist.
    """
    project_root = _get_existing_project_dir(project_id)
    source = _resolve_safe_path(project_root, file_path)

    if not source.exists():
        raise HTTPException(status_code=404, detail="file not found")

    destination = _resolve_safe_path(project_root, payload.new_path)

    if destination.exists():
        raise HTTPException(
            status_code=400,
            detail=f"something already exists at {payload.new_path!r}",
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)

    return FileNode(
        path=payload.new_path, type="dir" if destination.is_dir() else "file"
    )
