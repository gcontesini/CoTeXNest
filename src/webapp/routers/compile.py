"""HTTP endpoints for triggering a project's LaTeX compilation."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..models import CompileRequest, CompileResult
from ..services import latex_render

router: APIRouter = APIRouter(prefix="/api/projects", tags=["compile"])


@router.post("/{project_id}/compile", response_model=CompileResult)
def compile_project(
    project_id: str,
    body: CompileRequest,
) -> CompileResult:
    """Compile a project's LaTeX sources and return the result.

    A failed LaTeX compilation (``success=False``) is a normal 200
    response, not an HTTP error, since the frontend needs the build
    log either way. Only an unknown ``project_id`` is a 404.

    Args:
        project_id (str): Identifier of the project to compile.
        body (CompileRequest): Optional override of the main file.

    Returns:
        CompileResult: Compilation outcome, including the build log
        and, on success, the base64-encoded PDF.

    Raises:
        HTTPException: 404 if the project does not exist, 400 if
            ``main_file`` is not a valid in-project ``.tex`` filename.
    """
    try:
        return latex_render.compile_project(project_id, body.main_file)
    except FileNotFoundError as error:
        raise HTTPException(
            status_code=404,
            detail="project not found",
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error
