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
from webapp.services import project_files

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
            target = project_files.resolve_safe_path(project_root, file_name)
        except ValueError:
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
    try:
        project_root = project_files.get_project_dir(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

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
    try:
        return project_files.create_entry(project_id, payload.path, payload.type)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    try:
        return project_files.get_file_tree(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    try:
        project_root = project_files.get_project_dir(project_id)
        target = project_files.resolve_safe_path(project_root, file_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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
    try:
        project_root = project_files.get_project_dir(project_id)
        target = project_files.resolve_safe_path(project_root, file_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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
    try:
        content = project_files.read_file(project_id, file_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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
    try:
        project_files.write_file(project_id, file_path, payload.content)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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
    try:
        project_files.delete_entry(project_id, file_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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
    try:
        return project_files.rename_entry(project_id, file_path, payload.new_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
