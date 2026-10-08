"""HTTP endpoints for the AI writing assistant.

Covers global configuration (provider, model, writing-style profile)
and, per project, chat, the 15-second auto-scan's "suggest" call,
compile-error assistance, and applying a previously-returned proposal.
See ``services/assistant.py`` for the propose/apply safety model: every
route here except ``/apply`` only ever *returns* proposed changes, it
never writes to a project's files.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException

from .. import config
from ..models import (
    AiAssistRequest,
    AiAssistResult,
    AiModelOption,
    AssistantApplyRequest,
    AssistantApplyResult,
    AssistantChatRequest,
    AssistantChatResult,
    AssistantConfigUpdate,
    AssistantProviderConfig,
    WritingStyleResult,
    WritingStyleSampleRequest,
)
from ..services import assistant

router = APIRouter(prefix="/api", tags=["assistant"])


def _translate_errors(error: Exception) -> HTTPException:
    """Map a service-layer exception to the matching HTTP status.

    Args:
        error (Exception): One of ``ValueError``, ``FileNotFoundError``,
            or ``ConnectionError`` raised by ``services.assistant``.

    Returns:
        HTTPException: 400 for bad input/config, 404 for an unknown
        project/file, 503 if the configured provider could not be
        reached.
    """
    if isinstance(error, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, ConnectionError):
        return HTTPException(status_code=503, detail=str(error))
    return HTTPException(status_code=400, detail=str(error))


@router.get("/assistant/config", response_model=AssistantProviderConfig)
def get_assistant_config() -> AssistantProviderConfig:
    """Return the current assistant configuration (never the raw API key)."""
    return assistant.get_config()


@router.put("/assistant/config", response_model=AssistantProviderConfig)
def update_assistant_config(
    payload: AssistantConfigUpdate,
) -> AssistantProviderConfig:
    """Apply a partial update to the assistant configuration.

    Raises:
        HTTPException: 400 if ``provider``/``external_provider_kind``
            is not a recognized value.
    """
    try:
        return assistant.update_config(payload)
    except ValueError as error:
        raise _translate_errors(error) from error


@router.get("/assistant/models", response_model=list[AiModelOption])
def list_models() -> list[AiModelOption]:
    """List selectable models for the current provider.

    For the local provider, this is whatever Ollama currently reports
    as pulled (plus the configured model, even if not pulled yet, so
    it's still visible/selectable). For the external provider, this is
    just the single configured model -- there is no generic way to
    enumerate every external vendor's available models.
    """
    cfg = assistant.get_config()

    if cfg.provider != "local":
        if cfg.external_model:
            return [AiModelOption(id=cfg.external_model, label=cfg.external_model)]
        return []

    try:
        response = httpx.get(
            f"{config.OLLAMA_BASE_URL.rstrip('/')}/api/tags", timeout=10
        )
        response.raise_for_status()
        names = [
            entry.get("name")
            for entry in response.json().get("models", [])
            if entry.get("name")
        ]
    except httpx.HTTPError:
        names = []

    if cfg.local_model not in names:
        names = [cfg.local_model, *names]

    return [AiModelOption(id=name, label=name) for name in names]


@router.post("/assistant/writing-style", response_model=WritingStyleResult)
def learn_writing_style(payload: WritingStyleSampleRequest) -> WritingStyleResult:
    """Synthesize and persist a writing-style profile from samples.

    Raises:
        HTTPException: 400 if the provider is misconfigured, 503 if it
            could not be reached.
    """
    try:
        profile = assistant.learn_writing_style(payload.samples)
    except (ValueError, ConnectionError) as error:
        raise _translate_errors(error) from error

    return WritingStyleResult(profile=profile)


@router.post(
    "/projects/{project_id}/assistant/chat", response_model=AssistantChatResult
)
def chat(project_id: str, payload: AssistantChatRequest) -> AssistantChatResult:
    """Run one chat turn against the configured assistant.

    Raises:
        HTTPException: 404 if the project (or ``main_file``) does not
            exist, 400 if the provider is misconfigured, 503 if it
            could not be reached.
    """
    try:
        return assistant.chat(project_id, payload.messages, payload.main_file)
    except (ValueError, FileNotFoundError, ConnectionError) as error:
        raise _translate_errors(error) from error


@router.post(
    "/projects/{project_id}/assistant/suggest", response_model=AssistantChatResult
)
def suggest(project_id: str, main_file: str) -> AssistantChatResult:
    """Read-only "anything worth improving?" pass, for the auto-scan.

    Raises:
        HTTPException: 404 if the project/file does not exist, 400 if
            the file isn't valid UTF-8 or the provider is
            misconfigured, 503 if it could not be reached.
    """
    try:
        return assistant.suggest_improvements(project_id, main_file)
    except (ValueError, FileNotFoundError, ConnectionError) as error:
        raise _translate_errors(error) from error


@router.post(
    "/projects/{project_id}/assistant/fix-compile-error",
    response_model=AiAssistResult,
)
def fix_compile_error(project_id: str, payload: AiAssistRequest) -> AiAssistResult:
    """Ask the assistant to explain and, if possible, fix a compile error.

    Raises:
        HTTPException: 404 if the project/file does not exist, 400 if
            the file isn't valid UTF-8 or the provider is
            misconfigured, 503 if it could not be reached.
    """
    try:
        return assistant.fix_compile_error(
            project_id, payload.log, payload.main_file
        )
    except (ValueError, FileNotFoundError, ConnectionError) as error:
        raise _translate_errors(error) from error


@router.post(
    "/projects/{project_id}/assistant/apply", response_model=AssistantApplyResult
)
def apply(project_id: str, payload: AssistantApplyRequest) -> AssistantApplyResult:
    """Apply a previously-returned proposal and commit it to history.

    This is the only assistant endpoint that writes to a project's
    files -- see ``services.assistant.apply_proposal``.

    Raises:
        HTTPException: 404 if the project (or, for delete/rename, the
            target path) does not exist, 400 if the proposal is
            malformed or there is nothing to commit.
    """
    try:
        commit_id = assistant.apply_proposal(
            project_id, payload.proposal, payload.author
        )
    except (ValueError, FileNotFoundError) as error:
        raise _translate_errors(error) from error

    return AssistantApplyResult(success=True, commit_id=commit_id)
