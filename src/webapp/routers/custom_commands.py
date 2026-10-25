"""HTTP endpoints for the global custom LaTeX commands.

``/api/custom-commands`` reads and saves the global text.
``/api/projects/{id}/custom-commands`` reports and toggles whether one
project loads it (see ``services/custom_commands.py``).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..models import (
    CustomCommands,
    CustomCommandsProjectStatus,
    CustomCommandsProjectUpdate,
)
from ..services import custom_commands

router = APIRouter(prefix="/api", tags=["custom-commands"])


def _translate(error: Exception) -> HTTPException:
    if isinstance(error, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(error))

    if isinstance(error, ValueError):
        return HTTPException(status_code=400, detail=str(error))

    return HTTPException(
        status_code=500,
        detail=f"could not update the custom commands on disk: {error}",
    )


@router.get("/custom-commands", response_model=CustomCommands)
def get_custom_commands() -> CustomCommands:
    return CustomCommands(content=custom_commands.read_global_commands())


@router.put("/custom-commands", response_model=CustomCommands)
def put_custom_commands(payload: CustomCommands) -> CustomCommands:
    try:
        custom_commands.save_global_commands(payload.content)
    except OSError as error:
        raise _translate(error) from error

    return CustomCommands(content=payload.content)


@router.get(
    "/projects/{project_id}/custom-commands",
    response_model=CustomCommandsProjectStatus,
)
def get_project_custom_commands(project_id: str) -> CustomCommandsProjectStatus:
    try:
        enabled, main_file = custom_commands.get_project_status(project_id)
    except (OSError, ValueError) as error:
        raise _translate(error) from error

    return CustomCommandsProjectStatus(enabled=enabled, main_file=main_file)


@router.put(
    "/projects/{project_id}/custom-commands",
    response_model=CustomCommandsProjectStatus,
)
def put_project_custom_commands(
    project_id: str, payload: CustomCommandsProjectUpdate
) -> CustomCommandsProjectStatus:
    try:
        enabled, main_file = custom_commands.set_project_enabled(
            project_id, payload.enabled
        )
    except (OSError, ValueError) as error:
        raise _translate(error) from error

    return CustomCommandsProjectStatus(enabled=enabled, main_file=main_file)
