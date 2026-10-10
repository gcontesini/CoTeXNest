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
    AssistantChatJobStatus,
    AssistantChatRequest,
    AssistantChatResult,
    AssistantConfigUpdate,
    AssistantFixJobStatus,
    AssistantJobStarted,
    AssistantProviderConfig,
    ExternalProviderProfileCreate,
    ModelPullRequest,
    ModelPullStatus,
    WritingStyleResult,
    WritingStyleSampleRequest,
)
from ..services import assistant

router = APIRouter(prefix="/api", tags=["assistant"])

# A job that failed stores the exception type name it was raised as
# (see services.assistant._start_assistant_job); this maps that back
# to the same HTTP status _translate_errors would have produced had
# the original synchronous endpoint raised it directly.
_JOB_ERROR_STATUS: dict[str, int] = {
    "FileNotFoundError": 404,
    "ConnectionError": 503,
}


def _resolve_job(job_id: str) -> dict:
    """Fetch a background job's state, raising if unknown or failed.

    Args:
        job_id (str): Id returned by a ``.../start`` endpoint.

    Returns:
        dict: The job's state when not in an error state (``status``
        is ``"running"`` or ``"done"``).

    Raises:
        HTTPException: 404 if the job id is unknown or has expired;
            otherwise the status ``_JOB_ERROR_STATUS`` maps the job's
            recorded error type to (400 if not listed, matching
            ``_translate_errors``'s fallback for a plain ``ValueError``).
    """
    job = assistant.get_assistant_job(job_id)

    if job is None:
        raise HTTPException(
            status_code=404, detail=f"unknown or expired job id: {job_id!r}"
        )

    if job["status"] == "error":
        status_code = _JOB_ERROR_STATUS.get(job["error_type"], 400)
        raise HTTPException(status_code=status_code, detail=job["error"])

    return job


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
    if isinstance(error, OSError):
        return HTTPException(
            status_code=500,
            detail=(
                f"Filesystem error: {error}. If this project's files were "
                "created via Docker and you're now running without Docker "
                "(or vice versa), they may be owned by a different user "
                "than this process -- fix ownership or recreate the "
                "project."
            ),
        )
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

    if cfg.local_model and cfg.local_model not in names:
        names = [cfg.local_model, *names]

    return [AiModelOption(id=name, label=name) for name in names]


@router.post("/assistant/models/pull", response_model=ModelPullStatus)
def pull_model(payload: ModelPullRequest) -> ModelPullStatus:
    """Start pulling an Ollama model in the background and return at once.

    Paste any model tag from https://ollama.com/library. Poll
    ``GET /assistant/models/pull-status`` for progress; calling this
    again for a model already being pulled is a no-op.
    """
    assistant.start_model_pull(payload.model)
    return assistant.get_pull_status(payload.model)


@router.get("/assistant/models/pull-status", response_model=ModelPullStatus)
def pull_model_status(model: str) -> ModelPullStatus:
    """Report progress of a model pull started via ``POST .../pull``.

    Returns ``status="idle"`` if this model was never pulled (or the
    backend process has restarted since).
    """
    return assistant.get_pull_status(model)


@router.post("/assistant/fallback-profiles", response_model=AssistantProviderConfig)
def add_fallback_profile(payload: ExternalProviderProfileCreate) -> AssistantProviderConfig:
    """Add one external-provider fallback, tried on a rate-limit/quota error.

    Raises:
        HTTPException: 400 if ``provider_kind`` is not recognized.
    """
    try:
        return assistant.add_fallback_profile(payload)
    except ValueError as error:
        raise _translate_errors(error) from error


@router.delete(
    "/assistant/fallback-profiles/{profile_id}", response_model=AssistantProviderConfig
)
def remove_fallback_profile(profile_id: str) -> AssistantProviderConfig:
    """Remove one previously-added fallback provider.

    Raises:
        HTTPException: 404 if no fallback profile has this id.
    """
    try:
        return assistant.remove_fallback_profile(profile_id)
    except FileNotFoundError as error:
        raise _translate_errors(error) from error


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


@router.delete("/assistant/writing-style", status_code=204)
def delete_writing_style() -> None:
    """Delete the saved writing-style profile, if any."""
    assistant.clear_writing_style_profile()


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
    except (ValueError, FileNotFoundError, ConnectionError, OSError) as error:
        raise _translate_errors(error) from error


@router.post(
    "/projects/{project_id}/assistant/chat/start",
    response_model=AssistantJobStarted,
)
def start_chat(project_id: str, payload: AssistantChatRequest) -> AssistantJobStarted:
    """Start one chat turn as a background job; poll for its result.

    Preferred over ``POST .../assistant/chat`` from the UI: a slow
    provider (local or external) can legitimately take longer than any
    timeout reasonable to block an HTTP request on, so this returns
    immediately with a job id instead -- see
    ``GET .../assistant/jobs/chat/{job_id}``.
    """
    job_id = assistant.start_chat_job(project_id, payload.messages, payload.main_file)
    return AssistantJobStarted(job_id=job_id)


@router.get(
    "/assistant/jobs/chat/{job_id}", response_model=AssistantChatJobStatus
)
def poll_chat_job(job_id: str) -> AssistantChatJobStatus:
    """Poll a chat job started via ``POST .../assistant/chat/start``.

    Raises:
        HTTPException: 404 if the job id is unknown/expired or the
            project didn't exist, 503 if the provider could not be
            reached, 400 for any other failure (e.g. misconfiguration).
    """
    job = _resolve_job(job_id)
    return AssistantChatJobStatus(status=job["status"], result=job["result"])


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
    except (ValueError, FileNotFoundError, ConnectionError, OSError) as error:
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
    except (ValueError, FileNotFoundError, ConnectionError, OSError) as error:
        raise _translate_errors(error) from error


@router.post(
    "/projects/{project_id}/assistant/fix-compile-error/start",
    response_model=AssistantJobStarted,
)
def start_fix_compile_error(
    project_id: str, payload: AiAssistRequest
) -> AssistantJobStarted:
    """Start a compile-error fix attempt as a background job.

    Preferred over ``POST .../assistant/fix-compile-error`` from the
    UI -- see ``start_chat`` for why, and
    ``GET .../assistant/jobs/fix-compile-error/{job_id}`` to poll.
    """
    job_id = assistant.start_fix_compile_error_job(
        project_id, payload.log, payload.main_file
    )
    return AssistantJobStarted(job_id=job_id)


@router.get(
    "/assistant/jobs/fix-compile-error/{job_id}",
    response_model=AssistantFixJobStatus,
)
def poll_fix_compile_error_job(job_id: str) -> AssistantFixJobStatus:
    """Poll a job started via ``POST .../assistant/fix-compile-error/start``.

    Raises:
        HTTPException: 404 if the job id is unknown/expired or the
            project didn't exist, 503 if the provider could not be
            reached, 400 for any other failure (e.g. misconfiguration).
    """
    job = _resolve_job(job_id)
    return AssistantFixJobStatus(status=job["status"], result=job["result"])


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
    except (ValueError, FileNotFoundError, OSError) as error:
        raise _translate_errors(error) from error

    return AssistantApplyResult(success=True, commit_id=commit_id)
