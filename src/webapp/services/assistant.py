"""AI writing assistant: config, provider abstraction, and the
propose/apply flow that lets it edit project files.

Provider abstraction: "local" calls a self-hosted Ollama server
(``config.OLLAMA_BASE_URL``, a different container in the Docker
deployment -- see docker/docker-compose.yml); "external" calls a
user-configured Anthropic- or OpenAI-compatible HTTP API using a
user-supplied API key. Either way, the rest of this module only ever
calls ``generate_completion()`` and does not know which provider
answered.

Tool-calling protocol: rather than depending on a specific provider's
native structured tool-calling API (support for which varies widely,
especially on the kind of small model this is meant to run locally),
the model is instructed, via the system prompt, to reply in two parts:
a JSON object for structure (``{"reply": "...", "proposals": [...]}``)
and, separately, plain ``---CONTENT---``-delimited blocks for any file
content. The split exists because LaTeX is full of backslash commands,
which are not valid JSON escapes unless doubled -- a mistake small
models make close to always, which used to silently corrupt or drop
proposals outright. See ``_parse_model_response`` for the full parse
(JSON object via ``_extract_json_object``, content via
``_extract_content_blocks``), which falls back to treating the whole
reply as plain text with no proposals if even this tolerant parse
doesn't find a usable JSON object, rather than raising.

Safety model: nothing here writes to a project's files except
``apply_proposal``, which always goes through ``services.project_files``
and then records an automatic git commit (``services.git_history``),
so every assistant-applied change is itself a recoverable
version-history entry -- the same "nothing is ever truly lost"
guarantee the History panel already gives a manually-saved version.
Every other tool (chat, suggest, fix-compile-error) only ever *returns*
proposals; it never executes them.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from .. import config
from ..models import (
    AiAssistResult,
    AssistantChatMessage,
    AssistantChatResult,
    AssistantConfigUpdate,
    AssistantProposal,
    AssistantProviderConfig,
    ExternalProviderProfile,
    ExternalProviderProfileCreate,
    ModelPullStatus,
)
from . import git_history, project_files

logger = logging.getLogger(__name__)

# Deliberately NOT auto-selected anywhere -- shipping a default would
# mean every fresh deployment silently pulls a multi-gigabyte model the
# user never asked for. Only used as a suggestion string in docs/UI
# copy. Live testing showed sub-1B models (llama3.2:1b, qwen2.5:0.5b,
# gemma3:1b) almost never produce a usable fix-compile-error proposal,
# while a code-tuned ~3B model succeeds close to consistently -- see "A
# note on model capability" in docs/ai-assistant.md.
_SUGGESTED_LOCAL_MODEL = "qwen2.5-coder:3b"
_MODEL_PULL_TIMEOUT_SECONDS = 600

# A proposal's content can legitimately be a full LaTeX document, so
# every provider call needs a generation-length cap generous enough to
# not cut a reply off right as it reaches that content -- observed
# live against Ollama's own (historically ~128-token) default.
_MAX_RESPONSE_TOKENS = 4096

_DEFAULT_EXTERNAL_BASE_URLS: dict[str, str] = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
}

_PROPOSAL_ACTIONS: set[str] = {
    "write_file",
    "create_entry",
    "delete_entry",
    "rename_entry",
}

_CONTENT_BLOCK_MARKER = "---CONTENT---"

_JSON_PROPOSAL_INSTRUCTIONS = (
    "Respond in two parts.\n\n"
    "Part 1: a single JSON object and nothing else on those lines -- "
    "no prose before or after it, no markdown code fences. The object "
    'must have this exact shape: {"reply": "<a short message to show '
    'the user>", "proposals": [<zero or more proposal objects>]}. Each '
    "proposal object is one of:\n"
    '  {"action": "write_file", "path": "<project-relative path>", '
    '"description": "<one sentence summary>"}\n'
    '  {"action": "create_entry", "path": "<path>", "entry_type": '
    '"file" or "dir", "description": "..."}\n'
    '  {"action": "delete_entry", "path": "<path>", "description": "..."}\n'
    '  {"action": "rename_entry", "path": "<current path>", '
    '"new_path": "<new path>", "description": "..."}\n'
    "IMPORTANT: never put file content inside this JSON. LaTeX is full "
    "of backslash commands, which break JSON string escaping almost "
    "every time -- leave file content out of the JSON entirely.\n\n"
    "Part 2: only if a write_file or create_entry (of a file, not a "
    "folder) proposal needs content, follow the JSON with one block "
    "per such proposal, in the same order they appear in "
    f'"proposals", each wrapped exactly like this (two lines reading '
    f"{_CONTENT_BLOCK_MARKER}, with the content verbatim between them "
    "-- no escaping, no markdown fence):\n"
    f"{_CONTENT_BLOCK_MARKER}\n"
    "<the exact file content>\n"
    f"{_CONTENT_BLOCK_MARKER}\n\n"
    "Never invent a proposal the user did not ask for -- if you are "
    'only answering a question, return an empty "proposals" list and '
    "no content blocks. When you do propose a write_file change, keep "
    "it additive and targeted: add or refine content rather than "
    "deleting sections the user did not ask you to remove."
)


# ─── Config persistence ───


def _default_config_dict() -> dict[str, Any]:
    return {
        "provider": "local",
        # No default: a fresh deployment must not silently pull a
        # multi-gigabyte model. The user must explicitly name one
        # (Settings tab, or PUT /api/assistant/config) or switch to an
        # external API key before the assistant does anything.
        "local_model": None,
        "external_provider_kind": "anthropic",
        "external_base_url": None,
        "external_model": None,
        "external_api_key": None,
        "writing_style_profile": None,
        # Ordered list of additional external providers to try, in
        # order, when the active one answers with a rate-limit/quota
        # error -- see generate_completion()'s failover. Each entry:
        # {id, label, provider_kind, base_url, model, api_key}.
        "fallback_profiles": [],
    }


def _load_raw_config() -> dict[str, Any]:
    """Read the assistant config file, defaulting missing fields.

    Returns:
        dict[str, Any]: The full raw config, every key always present.
    """
    config.ASSISTANT_CONFIG_ROOT.mkdir(parents=True, exist_ok=True)

    if not config.ASSISTANT_CONFIG_PATH.is_file():
        return _default_config_dict()

    try:
        raw_text = config.ASSISTANT_CONFIG_PATH.read_text(encoding="utf-8")
        data = json.loads(raw_text) if raw_text.strip() else {}
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("failed to read assistant config: %s", error)
        return _default_config_dict()

    merged = _default_config_dict()
    merged.update(data)
    return merged


def _save_raw_config(data: dict[str, Any]) -> None:
    config.ASSISTANT_CONFIG_ROOT.mkdir(parents=True, exist_ok=True)
    config.ASSISTANT_CONFIG_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def get_config() -> AssistantProviderConfig:
    """Return the current assistant configuration.

    Returns:
        AssistantProviderConfig: Never includes a raw API key (primary
        or fallback), only whether one is set.
    """
    raw = _load_raw_config()
    return AssistantProviderConfig(
        provider=raw["provider"],
        local_model=raw.get("local_model"),
        external_provider_kind=raw["external_provider_kind"],
        external_base_url=raw.get("external_base_url"),
        external_model=raw.get("external_model"),
        has_external_api_key=bool(raw.get("external_api_key")),
        writing_style_profile=raw.get("writing_style_profile"),
        fallback_profiles=[
            ExternalProviderProfile(
                id=profile["id"],
                label=profile["label"],
                provider_kind=profile["provider_kind"],
                base_url=profile.get("base_url"),
                model=profile["model"],
                has_api_key=bool(profile.get("api_key")),
            )
            for profile in raw.get("fallback_profiles", [])
        ],
    )


def update_config(update: AssistantConfigUpdate) -> AssistantProviderConfig:
    """Apply a partial update to the assistant configuration.

    Args:
        update (AssistantConfigUpdate): Fields to change; ``None``
            fields are left untouched. ``external_api_key`` is
            write-only -- it is stored but never read back by
            ``get_config()``.

    Returns:
        AssistantProviderConfig: The configuration after the update.

    Raises:
        ValueError: If ``provider`` or ``external_provider_kind`` is
            set to an unrecognized value.
    """
    raw = _load_raw_config()

    if update.provider is not None:
        if update.provider not in ("local", "external"):
            raise ValueError('provider must be "local" or "external"')
        raw["provider"] = update.provider

    if update.external_provider_kind is not None:
        if update.external_provider_kind not in ("anthropic", "openai"):
            raise ValueError(
                'external_provider_kind must be "anthropic" or "openai"'
            )
        raw["external_provider_kind"] = update.external_provider_kind

    if update.local_model is not None:
        raw["local_model"] = update.local_model

    if update.external_base_url is not None:
        raw["external_base_url"] = update.external_base_url

    if update.external_model is not None:
        raw["external_model"] = update.external_model

    if update.clear_external_api_key:
        raw["external_api_key"] = None
    elif update.external_api_key is not None:
        raw["external_api_key"] = update.external_api_key

    _save_raw_config(raw)
    return get_config()


def add_fallback_profile(payload: ExternalProviderProfileCreate) -> AssistantProviderConfig:
    """Append one external-provider fallback, tried on a rate-limit/quota error.

    Args:
        payload (ExternalProviderProfileCreate): The fallback's own
            kind/base_url/model/api_key -- independent of the primary
            external provider's configuration.

    Returns:
        AssistantProviderConfig: The configuration after the addition.

    Raises:
        ValueError: If ``provider_kind`` is not recognized.
    """
    if payload.provider_kind not in ("anthropic", "openai"):
        raise ValueError('provider_kind must be "anthropic" or "openai"')

    raw = _load_raw_config()
    profiles = raw.setdefault("fallback_profiles", [])
    profiles.append(
        {
            "id": str(uuid.uuid4()),
            "label": payload.label,
            "provider_kind": payload.provider_kind,
            "base_url": payload.base_url,
            "model": payload.model,
            "api_key": payload.api_key,
        }
    )
    _save_raw_config(raw)
    return get_config()


def remove_fallback_profile(profile_id: str) -> AssistantProviderConfig:
    """Remove one previously-added fallback provider.

    Args:
        profile_id (str): The fallback's id, from ``get_config()``.

    Returns:
        AssistantProviderConfig: The configuration after the removal.

    Raises:
        FileNotFoundError: If no fallback profile has this id.
    """
    raw = _load_raw_config()
    profiles = raw.get("fallback_profiles", [])
    remaining = [profile for profile in profiles if profile["id"] != profile_id]

    if len(remaining) == len(profiles):
        raise FileNotFoundError(f"no fallback profile with id {profile_id!r}")

    raw["fallback_profiles"] = remaining
    _save_raw_config(raw)
    return get_config()


def set_writing_style_profile(profile: str) -> None:
    """Persist a newly-learned writing-style profile.

    Args:
        profile (str): Free-text style summary, included in every
            future prompt built by this module.
    """
    raw = _load_raw_config()
    raw["writing_style_profile"] = profile
    _save_raw_config(raw)


def clear_writing_style_profile() -> None:
    """Delete the saved writing-style profile, if any.

    Future prompts built by this module simply omit the "established
    writing style" section (see ``_build_system_prompt``) until a new
    one is learned via ``learn_writing_style``.
    """
    raw = _load_raw_config()
    raw["writing_style_profile"] = None
    _save_raw_config(raw)


# ─── Provider calls ───


def _ollama_has_model(model: str, base_url: str) -> bool:
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=10)
        response.raise_for_status()
    except httpx.HTTPError:
        return False

    names = {entry.get("name") for entry in response.json().get("models", [])}
    return model in names or f"{model}:latest" in names


_pull_status_lock = threading.Lock()
_pull_status: dict[str, dict[str, Any]] = {}


def _idle_pull_status(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "status": "idle",
        "completed_bytes": None,
        "total_bytes": None,
        "done": False,
        "error": None,
    }


def get_pull_status(model: str) -> ModelPullStatus:
    """Report the current progress of a model pull, for frontend polling.

    Args:
        model (str): The Ollama model tag, exactly as configured.

    Returns:
        ModelPullStatus: ``status="idle"`` if no pull for this exact
        model tag has ever been started in this process's lifetime.
    """
    with _pull_status_lock:
        state = dict(_pull_status.get(model, _idle_pull_status(model)))

    return ModelPullStatus(**state)


def _set_pull_status(model: str, **fields: Any) -> None:
    with _pull_status_lock:
        state = _pull_status.setdefault(model, _idle_pull_status(model))
        state.update(fields)


def start_model_pull(model: str) -> None:
    """Kick off a model pull on a background thread and return immediately.

    Idempotent while a pull for the same model tag is already in
    flight, so an explicit "Pull model" click racing the implicit
    first-chat auto-pull (or a double click) can't start two
    concurrent pulls of the same model.

    Args:
        model (str): The Ollama model tag to pull.
    """
    with _pull_status_lock:
        existing = _pull_status.get(model)
        if existing is not None and not existing["done"]:
            return
        _pull_status[model] = _idle_pull_status(model)
        _pull_status[model]["status"] = "starting"

    thread = threading.Thread(target=_pull_model_thread, args=(model,), daemon=True)
    thread.start()


def _pull_model_thread(model: str) -> None:
    try:
        _ensure_ollama_model(model, config.OLLAMA_BASE_URL)
    except ConnectionError as error:
        # _ensure_ollama_model already recorded the error in
        # _pull_status for the polling endpoint to surface; logging
        # here is this background thread's only other way to report
        # it, since there is no caller left to raise to.
        logger.warning("background pull of %r failed: %s", model, error)


def _ensure_ollama_model(model: str, base_url: str) -> None:
    """Pull a model into the local Ollama server if not present.

    Blocks until the pull completes (or fails) -- model pulls can take
    minutes, hence the much longer timeout than a normal chat request.
    Streams Ollama's own NDJSON progress events (status string, plus
    completed/total byte counts while a layer is downloading) into
    ``_pull_status`` as they arrive, so ``GET
    /api/assistant/models/pull-status`` can show live progress whether
    the pull was started explicitly or implicitly by a chat call.

    Raises:
        ConnectionError: If Ollama can't be reached at all (not just a
            failed pull) -- without this, a connection-refused error
            here would otherwise propagate as an unhandled httpx
            exception instead of the clean, user-facing error the rest
            of this module produces.
    """
    logger.info("pulling ollama model %r (first use)...", model)
    _set_pull_status(model, status="starting", done=False, error=None)

    try:
        with httpx.stream(
            "POST",
            f"{base_url.rstrip('/')}/api/pull",
            json={"model": model, "stream": True},
            timeout=_MODEL_PULL_TIMEOUT_SECONDS,
        ) as response:
            response.raise_for_status()

            for line in response.iter_lines():
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                _set_pull_status(
                    model,
                    status=str(event.get("status", "")),
                    completed_bytes=event.get("completed"),
                    total_bytes=event.get("total"),
                )
    except httpx.HTTPError as error:
        message = (
            f"could not reach the local Ollama server at {base_url} "
            f"to pull model {model!r}: {error}"
        )
        _set_pull_status(model, status="error", done=True, error=message)
        raise ConnectionError(message) from error

    # Leave `status` as whatever Ollama's own last event reported
    # (normally "success") rather than overwriting it -- `done` is the
    # authoritative "finished" flag the frontend polls on.
    _set_pull_status(model, done=True, error=None)


def _call_ollama(messages: list[dict[str, str]], model: str) -> str:
    base_url = config.OLLAMA_BASE_URL

    if not _ollama_has_model(model, base_url):
        _ensure_ollama_model(model, base_url)

    try:
        response = httpx.post(
            f"{base_url.rstrip('/')}/api/chat",
            json={
                "model": model,
                "messages": messages,
                "stream": False,
                # Ollama's own default generation length is short enough
                # (historically 128 tokens) to routinely cut a reply off
                # right as it reaches the file content -- observed live
                # across multiple models. A proposal's content can
                # legitimately be a full LaTeX document.
                "options": {"num_predict": _MAX_RESPONSE_TOKENS},
            },
            timeout=config.AI_ASSIST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise ConnectionError(
            f"could not reach the local Ollama server at {base_url}: {error}"
        ) from error

    return str(response.json()["message"]["content"])


_STATUS_CODE_HINTS: dict[int, str] = {
    400: (
        "the provider rejected the request as malformed -- a common cause "
        "is a base URL or API key format that doesn't match the API style "
        "you've selected (e.g. an Anthropic key with an OpenAI-compatible "
        "base URL, or vice versa)."
    ),
    401: "the API key was rejected -- check it's correct and still active.",
    403: "the API key doesn't have access to this model/endpoint.",
    404: (
        "the model name wasn't found -- double-check it's a real, current "
        "model id for this provider (model names change over time)."
    ),
    429: (
        "the provider is rate-limiting or your quota/billing is exhausted "
        "-- check your account's usage and billing page."
    ),
    503: (
        "the provider's own servers are temporarily overloaded/unavailable "
        "-- this is usually transient; try again in a moment."
    ),
}


class ProviderRateLimitedError(ConnectionError):
    """An external provider is reachable but returned HTTP 429.

    A distinct type (rather than a plain ``ConnectionError``) because
    this is specifically the condition ``generate_completion``'s
    automatic failover reacts to -- a wrong API key or bad model name
    should surface to the user as-is, not silently try a different
    provider.
    """


class ProviderUnavailableError(ProviderRateLimitedError):
    """An external provider is overloaded or unreachable (HTTP 500/502/503/504,
    timeout or connection failure).

    A subclass of ``ProviderRateLimitedError`` so the existing failover
    path treats it the same way, but retried first with backoff since
    these are usually transient.
    """


_TRANSIENT_STATUS_CODES: frozenset[int] = frozenset({500, 502, 503, 504})
_TRANSIENT_RETRY_DELAYS_SECONDS: tuple[float, ...] = (2.0, 5.0)


def _extract_provider_error_detail(response: httpx.Response) -> str | None:
    """Pull the provider's own error message out of its response body.

    A single HTTP status code can cover genuinely different conditions
    -- OpenAI returns 429 for both "too many requests per minute" and
    "zero quota remaining", distinguished only by ``error.type`` in the
    body (``rate_limit_exceeded`` vs ``insufficient_quota``). Reporting
    the provider's own wording is strictly more informative than
    guessing from the status code alone, and is what actually resolves
    the "429 but my dashboard shows no requests and plenty of credit"
    confusion: those are two different things sharing one status code.

    Args:
        response (httpx.Response): The provider's HTTP response.

    Returns:
        str | None: A short, human-readable detail string if the body
        had a recognizable shape (OpenAI/Anthropic-style
        ``{"error": {...}}``, or a bare ``{"message": ...}``), else
        None.
    """
    try:
        body = response.json()
    except ValueError:
        return None

    if not isinstance(body, dict):
        return None

    error_obj = body.get("error")

    if isinstance(error_obj, dict):
        kind = error_obj.get("type") or error_obj.get("code")
        message = error_obj.get("message")
        if kind and message:
            return f"{kind}: {message}"
        return str(message or kind) if (message or kind) else None

    message = body.get("message")
    return str(message) if message else None


def _external_provider_error(error: httpx.HTTPError) -> ConnectionError:
    """Build a clear, actionable error for a failed external-provider call.

    Distinguishes "reachable but rejected the request" (an HTTP status
    error -- wrong API key, invalid model name, rate limit) from
    "couldn't even connect" (DNS/network failure), since the former
    has specific, fixable causes worth surfacing instead of a generic
    "could not reach" message. Prefers the provider's own reported
    error detail over a generic status-code-based hint whenever the
    response body has one.

    Args:
        error (httpx.HTTPError): The error raised by the request.

    Returns:
        ConnectionError: A ``ProviderRateLimitedError`` for a 429 (so
        ``generate_completion`` can fail over to a fallback provider),
        a plain ``ConnectionError`` otherwise.
    """
    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        provider_detail = _extract_provider_error_detail(error.response)
        hint = _STATUS_CODE_HINTS.get(status_code)
        detail = (
            f" ({provider_detail})"
            if provider_detail
            else (f" ({hint})" if hint else "")
        )
        message = (
            f"external provider rejected the request with "
            f"{status_code} {error.response.reason_phrase}{detail}"
        )

        if status_code == 429:
            return ProviderRateLimitedError(message)
        if status_code in _TRANSIENT_STATUS_CODES:
            return ProviderUnavailableError(message)
        return ConnectionError(message)

    return ProviderUnavailableError(
        f"could not reach the external provider: {error}"
    )


def _call_external_openai(
    messages: list[dict[str, str]], model: str, base_url: str, api_key: str
) -> str:
    try:
        response = httpx.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": messages,
                "max_tokens": _MAX_RESPONSE_TOKENS,
            },
            timeout=config.AI_ASSIST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise _external_provider_error(error) from error

    return str(response.json()["choices"][0]["message"]["content"])


def _call_external_anthropic(
    messages: list[dict[str, str]], model: str, base_url: str, api_key: str
) -> str:
    system_text = "\n\n".join(
        message["content"] for message in messages if message["role"] == "system"
    )
    conversation = [message for message in messages if message["role"] != "system"]

    try:
        response = httpx.post(
            f"{base_url.rstrip('/')}/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": _MAX_RESPONSE_TOKENS,
                "system": system_text or None,
                "messages": conversation,
            },
            timeout=config.AI_ASSIST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise _external_provider_error(error) from error

    data = response.json()
    return "".join(block.get("text", "") for block in data.get("content", []))


def generate_completion(messages: list[dict[str, str]]) -> str:
    """Send a chat-style message list to the configured provider.

    Args:
        messages (list[dict[str, str]]): OpenAI-chat-style messages,
            each ``{"role": ..., "content": ...}``.

    Returns:
        str: The raw text the model replied with (not yet parsed as
        the JSON proposal protocol).

    Raises:
        ValueError: If "local" is selected but no model has been
            configured yet, or "external" is selected but is not fully
            configured (base_url/model/api_key).
        ConnectionError: If the provider (and every configured
            fallback) could not be reached or returned an error.
    """
    reply, _notice = generate_completion_with_notice(messages)
    return reply


def generate_completion_with_notice(
    messages: list[dict[str, str]],
) -> tuple[str, str | None]:
    """Like ``generate_completion``, but also reports an automatic failover.

    Args:
        messages (list[dict[str, str]]): See ``generate_completion``.

    Returns:
        tuple[str, str | None]: The reply text, and a short
        user-facing notice if answering this call required
        automatically switching to a fallback provider (None
        otherwise) -- see ``_generate_external_with_failover``.

    Raises:
        ValueError: See ``generate_completion``.
        ConnectionError: See ``generate_completion``.
    """
    raw = _load_raw_config()

    if raw["provider"] == "external":
        return _generate_external_with_failover(messages, raw)

    local_model = raw.get("local_model")

    if not local_model:
        raise ValueError(
            "no local model is configured yet -- open the Assistant "
            "Settings tab and either pick a model tag from "
            "https://ollama.com/library to pull it (there's a Pull "
            "button right there), or switch to an external API and "
            "add a key."
        )

    return _call_ollama(messages, local_model), None


def _external_candidate_from_primary(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": None,
        "provider_kind": raw["external_provider_kind"],
        "base_url": raw.get("external_base_url"),
        "model": raw.get("external_model"),
        "api_key": raw.get("external_api_key"),
    }


def _call_candidate_with_retry(
    candidate: dict[str, Any],
    model: str,
    base_url: str,
    api_key: str,
    messages: list[dict[str, str]],
) -> str:
    """Call one provider, retrying transient (5xx/network) errors with backoff.

    A 429 is not retried here: it fails over immediately.
    """
    call = (
        _call_external_openai
        if candidate["provider_kind"] == "openai"
        else _call_external_anthropic
    )

    for delay in _TRANSIENT_RETRY_DELAYS_SECONDS:
        try:
            return call(messages, model, base_url, api_key)
        except ProviderUnavailableError as error:
            logger.warning("transient provider error, retrying in %ss: %s", delay, error)
            time.sleep(delay)

    return call(messages, model, base_url, api_key)


def _generate_external_with_failover(
    messages: list[dict[str, str]], raw: dict[str, Any]
) -> tuple[str, str | None]:
    """Call the active external provider, auto-promoting a fallback on 429 or transient 5xx.

    Tries the primary external provider first, then each configured
    fallback in order, stopping at the first one that doesn't come
    back with a rate-limit/quota error (``ProviderRateLimitedError``).
    Any other failure (bad key, bad model, network error) is raised
    immediately without trying fallbacks -- those are configuration
    problems the user needs to see and fix, not conditions another
    provider would resolve.

    The first fallback that succeeds is promoted to be the new primary
    going forward (persisted), and the provider that just got
    rate-limited is appended to the fallback list rather than
    discarded, so it's automatically retried again once other options
    are also exhausted, instead of being abandoned for good over what
    may be a temporary limit.

    Args:
        messages (list[dict[str, str]]): See ``generate_completion``.
        raw (dict[str, Any]): The full raw config, as returned by
            ``_load_raw_config``.

    Returns:
        tuple[str, str | None]: The reply, and a notice describing the
        switch if one happened.

    Raises:
        ValueError: If the primary external provider is not fully
            configured.
        ConnectionError: If every candidate failed; the error from the
            *last* candidate tried.
    """
    candidates = [_external_candidate_from_primary(raw), *raw.get("fallback_profiles", [])]
    last_error: ConnectionError | None = None

    for index, candidate in enumerate(candidates):
        base_url = candidate.get("base_url") or _DEFAULT_EXTERNAL_BASE_URLS.get(
            candidate["provider_kind"]
        )
        model = candidate.get("model")
        api_key = candidate.get("api_key")

        if not model or not api_key:
            if index == 0:
                raise ValueError(
                    "external provider selected but external_model/"
                    "external_api_key are not fully configured"
                )
            continue  # an incompletely-configured fallback is just skipped

        if str(model).startswith("REPLACE_"):
            continue

        try:
            reply = _call_candidate_with_retry(candidate, model, base_url, api_key, messages)
        except ProviderRateLimitedError as error:
            last_error = error
            continue

        if index == 0:
            return reply, None

        previous_kind = raw["external_provider_kind"]
        _promote_fallback_profile(raw, index)
        notice = (
            f"The {previous_kind} provider was rate-limited or out of "
            f"quota, so the assistant automatically switched to "
            f"{candidate.get('label') or candidate['provider_kind']} "
            "for this and future requests."
        )
        return reply, notice

    assert last_error is not None, "candidates always has at least the primary"
    raise last_error


def _promote_fallback_profile(raw: dict[str, Any], index: int) -> None:
    """Make the fallback at ``candidates[index]`` the new primary provider.

    The previous primary is demoted to the end of the fallback list
    (not discarded) so it's automatically retried once other options
    are also exhausted -- a rate limit is often temporary.

    Args:
        raw (dict[str, Any]): The full raw config (mutated in place
            and persisted).
        index (int): Position within ``_generate_external_with_failover``'s
            ``candidates`` list (1-based within fallbacks, since index
            0 is always the primary and never promoted via this path).
    """
    fallback_profiles = raw.get("fallback_profiles", [])
    promoted = fallback_profiles[index - 1]
    remaining = [*fallback_profiles[: index - 1], *fallback_profiles[index:]]

    demoted_primary = {
        "id": str(uuid.uuid4()),
        "label": f"previous provider ({raw['external_provider_kind']})",
        "provider_kind": raw["external_provider_kind"],
        "base_url": raw.get("external_base_url"),
        "model": raw.get("external_model"),
        "api_key": raw.get("external_api_key"),
    }

    raw["external_provider_kind"] = promoted["provider_kind"]
    raw["external_base_url"] = promoted.get("base_url")
    raw["external_model"] = promoted["model"]
    raw["external_api_key"] = promoted.get("api_key")
    raw["fallback_profiles"] = [*remaining, demoted_primary]

    _save_raw_config(raw)
    logger.warning(
        "external provider %r was rate-limited/quota-exhausted -- "
        "automatically switched to fallback %r (%r)",
        demoted_primary["provider_kind"],
        promoted.get("label"),
        promoted["provider_kind"],
    )


# ─── JSON proposal protocol ───


def _build_system_prompt(writing_style_profile: str | None) -> str:
    parts = [
        (
            "You are a LaTeX writing assistant embedded in a web-based "
            "LaTeX editor, helping a user who may not know LaTeX well."
        ),
    ]

    if writing_style_profile:
        parts.append("The user's established writing style:\n" + writing_style_profile)

    parts.append(_JSON_PROPOSAL_INSTRUCTIONS)
    return "\n\n".join(parts)


def _strip_wrapping_fence(text: str) -> str:
    """Strip a single leading/trailing markdown code fence, if present."""
    stripped = text.strip()
    fence_match = re.match(r"^```(?:\w+)?\s*(.*?)\s*```$", stripped, re.DOTALL)
    return fence_match.group(1) if fence_match else stripped


def _extract_json_object(text: str) -> tuple[dict[str, Any] | None, int]:
    """Find and parse the first top-level JSON object anywhere in ``text``.

    Uses ``json.JSONDecoder.raw_decode`` rather than a regex, which
    means it correctly finds where the JSON value *ends* even when
    something else (a stray sentence, a content block) follows it, and
    tolerates a model prefixing the JSON with a sentence despite being
    told not to, by retrying from each ``{`` found in turn. Parses with
    ``strict=False`` so a literal, unescaped newline inside a string
    value (a common small-model mistake) doesn't fail parsing outright.

    Args:
        text (str): Candidate text, possibly containing a JSON object
            plus other content before/after it.

    Returns:
        tuple[dict[str, Any] | None, int]: The parsed object and the
        index in ``text`` immediately after it, or ``(None, -1)`` if no
        JSON object could be found.
    """
    decoder = json.JSONDecoder(strict=False)
    start = text.find("{")

    while start != -1:
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue

        if isinstance(obj, dict):
            return obj, end

        start = text.find("{", start + 1)

    return None, -1


def _parse_json_response(raw_text: str) -> dict[str, Any]:
    """Extract the JSON object a model returned.

    Tolerates a model that didn't follow the formatting instructions
    exactly: a leading/trailing markdown code fence, extra prose around
    the JSON object, or unescaped newlines inside a string value. Falls
    back to treating the entire reply as plain text with no proposals,
    rather than raising, since a small local model occasionally
    ignoring format instructions should degrade to "just a chat reply",
    not a hard failure (and definitely not raw JSON syntax shown
    verbatim to the user).

    Args:
        raw_text (str): The model's raw response text.

    Returns:
        dict[str, Any]: Parsed with ``"reply"`` and ``"proposals"``
        keys, both defaulted if absent/unparsable.
    """
    parsed, _end = _extract_json_object(raw_text)

    if parsed is not None:
        return parsed

    return {"reply": raw_text, "proposals": []}


_CONTENT_BLOCK_PATTERN = re.compile(
    re.escape(_CONTENT_BLOCK_MARKER) + r"\s*\n(.*?)\n?" + re.escape(_CONTENT_BLOCK_MARKER),
    re.DOTALL,
)

# Observed live: a model will often ignore the custom marker and fall
# back to a plain markdown code fence instead (its training clearly
# has a much stronger prior for "fenced block" than for a bespoke
# delimiter) -- used as a fallback when no marker-delimited block is
# found at all, not as the primary format, since real file content
# could in principle itself contain a ``` sequence.
_FENCED_BLOCK_PATTERN = re.compile(r"```(?:\w+)?\n?(.*?)\n?```", re.DOTALL)


def _extract_content_blocks(text: str) -> list[str]:
    """Pull out out-of-band file content blocks, in order.

    This is how file content reaches a proposal instead of living
    inside the JSON object: LaTeX's backslash commands make it
    extremely easy for a model to produce JSON that looks right but
    fails to parse (``\\end`` is not a valid JSON escape, and small
    models essentially never remember to double every backslash), so
    content travels as plain, unescaped, out-of-band text instead.

    Tries the documented ``---CONTENT---`` marker first; if the model
    ignored that instruction and used an ordinary markdown code fence
    instead (a common substitution in practice), falls back to that.

    Args:
        text (str): Text to search (typically everything *after* the
            JSON object in a model's response).

    Returns:
        list[str]: Block contents, in the order they appeared.
    """
    marker_blocks = [
        match.group(1) for match in _CONTENT_BLOCK_PATTERN.finditer(text)
    ]

    if marker_blocks:
        return [_strip_wrapping_fence(block) for block in marker_blocks]

    return [match.group(1) for match in _FENCED_BLOCK_PATTERN.finditer(text)]


def _coerce_proposals(
    raw_proposals: object, content_blocks: list[str] | None = None
) -> list[AssistantProposal]:
    """Validate and build proposals, attaching out-of-band content.

    Args:
        raw_proposals (object): The (untrusted) ``"proposals"`` value
            from the model's parsed JSON.
        content_blocks (list[str] | None): Content blocks extracted
            from the text following the JSON (see
            ``_extract_content_blocks``), assigned in order to
            proposals that need content and didn't already get an
            (unreliable) inline ``"content"`` field from the model.

    Returns:
        list[AssistantProposal]: Only entries with a recognized
        ``action`` and a ``path`` are kept.
    """
    if not isinstance(raw_proposals, list):
        return []

    blocks = list(content_blocks or [])
    next_block_index = 0
    proposals: list[AssistantProposal] = []

    for raw in raw_proposals:
        if not isinstance(raw, dict):
            continue

        action = raw.get("action")
        path = raw.get("path")

        if action not in _PROPOSAL_ACTIONS or not path:
            continue

        content = raw.get("content")
        is_dir = action == "create_entry" and raw.get("entry_type") == "dir"
        needs_content = action in ("write_file", "create_entry") and not is_dir

        if not content and needs_content and next_block_index < len(blocks):
            content = blocks[next_block_index]
            next_block_index += 1

        if needs_content and not content:
            # A write_file/create_entry(file) proposal with no content
            # anywhere -- not an inline field, not an out-of-band block
            # -- can never be applied; services.project_files.write_file
            # requires it, and apply_proposal() raises on a missing
            # one. Dropping it here (rather than surfacing it and
            # letting Accept fail with a 400) is the general case of
            # the same gate _select_fix_proposal already applies for
            # fix_compile_error specifically.
            continue

        proposals.append(
            AssistantProposal(
                id=str(uuid.uuid4()),
                action=action,
                path=path,
                new_path=raw.get("new_path"),
                entry_type=raw.get("entry_type"),
                content=content,
                description=str(raw.get("description") or f"{action} {path}"),
            )
        )

    return proposals


def _parse_model_response(raw_text: str) -> AssistantChatResult:
    """Parse a model's full two-part response into reply + proposals.

    Args:
        raw_text (str): The model's raw response text.

    Returns:
        AssistantChatResult: The reply text and any proposals, with
        content already attached from any out-of-band blocks.
    """
    parsed, end = _extract_json_object(raw_text)

    if parsed is None:
        return AssistantChatResult(reply=raw_text, proposals=[])

    remainder = raw_text[end:]
    stripped_remainder = remainder.lstrip()

    if stripped_remainder.startswith("```"):
        # A lone leftover closing fence from a ```json ... ``` wrapper
        # around the JSON itself -- _extract_json_object only consumed
        # the JSON *value*, not that wrapper's closing marker. Drop it
        # so it isn't mistaken for a content block's opening fence.
        remainder = stripped_remainder[3:]

    content_blocks = _extract_content_blocks(remainder)

    return AssistantChatResult(
        reply=str(parsed.get("reply", raw_text)),
        proposals=_coerce_proposals(parsed.get("proposals"), content_blocks),
    )


def _run_prompt(system_prompt: str, user_content: str) -> AssistantChatResult:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    raw_reply, notice = generate_completion_with_notice(messages)
    result = _parse_model_response(raw_reply)

    if notice:
        result = result.model_copy(update={"provider_notice": notice})

    return result


# ─── Assistant actions ───


def chat(
    project_id: str,
    messages: list[AssistantChatMessage],
    main_file: str | None,
) -> AssistantChatResult:
    """Run one chat turn, optionally grounded in a project file's content.

    Args:
        project_id (str): Project the conversation is about.
        messages (list[AssistantChatMessage]): Full conversation so
            far, oldest first.
        main_file (str | None): If given, this file's current content
            is included as context for the model.

    Returns:
        AssistantChatResult: The assistant's reply and any proposals.
    """
    style = get_config().writing_style_profile
    system_prompt = _build_system_prompt(style)

    context_messages = [{"role": "system", "content": system_prompt}]

    if main_file:
        try:
            content = project_files.read_file(project_id, main_file)
            context_messages.append(
                {
                    "role": "system",
                    "content": f"Current contents of {main_file!r}:\n\n{content}",
                }
            )
        except (FileNotFoundError, ValueError):
            pass

    context_messages.extend({"role": m.role, "content": m.content} for m in messages)

    raw_reply, notice = generate_completion_with_notice(context_messages)
    result = _parse_model_response(raw_reply)

    if notice:
        result = result.model_copy(update={"provider_notice": notice})

    return result


def suggest_improvements(project_id: str, main_file: str) -> AssistantChatResult:
    """One-shot, read-only "anything worth improving?" pass over a file.

    Used by the frontend's periodic auto-scan; never applies anything.

    Args:
        project_id (str): Project to inspect.
        main_file (str): File to review.

    Returns:
        AssistantChatResult: Empty ``proposals`` if nothing stood out.

    Raises:
        FileNotFoundError: If the project or file does not exist.
        ValueError: If the file is not valid UTF-8, or the external
            provider is misconfigured.
        ConnectionError: If the provider could not be reached.
    """
    content = project_files.read_file(project_id, main_file)
    style = get_config().writing_style_profile

    return _run_prompt(
        _build_system_prompt(style),
        f"Here is the current content of {main_file!r}. If you notice "
        "one clear, worthwhile improvement (clarity, grammar, LaTeX "
        "correctness), propose a single targeted write_file change. If "
        "nothing stands out, return an empty proposals list and a "
        "short reply saying so.\n\n" + content,
    )


def fix_compile_error(project_id: str, log: str, main_file: str) -> AiAssistResult:
    """Ask the assistant to explain and, if possible, fix a compile error.

    Args:
        project_id (str): Project that failed to compile.
        log (str): The latexmk/pdflatex build log.
        main_file (str): The file that was compiled.

    Returns:
        AiAssistResult: A plain-language explanation, and a single
        proposed fix if the model offered one.

    Raises:
        FileNotFoundError: If the project or file does not exist.
        ValueError: If the file is not valid UTF-8, or the external
            provider is misconfigured.
        ConnectionError: If the provider could not be reached.
    """
    content = project_files.read_file(project_id, main_file)
    style = get_config().writing_style_profile
    system_prompt = _build_system_prompt(style)

    user_message = (
        f"Compiling {main_file!r} failed. Here is the compile log:\n\n"
        f"{log}\n\nHere is the current content of {main_file!r}:\n\n"
        f"{content}\n\nExplain what's wrong in plain language, and if "
        "you can fix it, propose exactly one write_file change with "
        "the corrected content. Keep the fix as small and targeted as "
        "possible -- don't remove content unrelated to the error."
    )

    result = _run_prompt(system_prompt, user_message)
    proposal = _select_fix_proposal(result.proposals, main_file, content)

    if proposal is None:
        # Observed live: small/medium models frequently describe the
        # fix in the "reply" text ("remove the broken line") without
        # actually including the content block the fix needs -- one
        # firm, specific retry measurably helps, and the cost of a
        # second call only applies in this already-failing case.
        retry_result = _run_prompt(
            system_prompt,
            user_message
            + "\n\nYour previous answer did not include a usable "
            f"{_CONTENT_BLOCK_MARKER} block with the complete, "
            "corrected file content. Try again -- the proposals list "
            "must have exactly one write_file entry, and it must be "
            f"followed by a {_CONTENT_BLOCK_MARKER} block containing "
            "the full corrected content, not just a description of "
            "the change.",
        )
        retry_proposal = _select_fix_proposal(retry_result.proposals, main_file, content)

        if retry_proposal is not None:
            return AiAssistResult(
                suggestion=retry_result.reply,
                proposal=retry_proposal,
                provider_notice=retry_result.provider_notice,
            )

    return AiAssistResult(
        suggestion=result.reply, proposal=proposal, provider_notice=result.provider_notice
    )


def _select_fix_proposal(
    proposals: list[AssistantProposal],
    main_file: str,
    original_content: str,
) -> AssistantProposal | None:
    """Pick (and correct) the proposal to surface for a compile-error fix.

    Small models frequently hallucinate the wrong path for this
    request (even an absolute path well outside the project) even
    though the target file is already known deterministically here --
    so the path is corrected rather than trusted. A proposal that
    isn't a ``write_file``, or whose content looks like it deletes most
    of the file rather than making a targeted fix (the non-destructive
    editing this feature is required to do), is dropped instead of
    surfaced, rather than relying on the model to follow that
    instruction on its own.

    Args:
        proposals (list[AssistantProposal]): Proposals returned
            alongside the model's reply.
        main_file (str): The file that actually failed to compile.
        original_content (str): Its current content, for the
            "not too much got removed" sanity check.

    Returns:
        AssistantProposal | None: A corrected, trustworthy proposal, or
        None if nothing usable was offered.
    """
    for proposal in proposals:
        if proposal.action != "write_file" or not proposal.content:
            continue

        if (
            len(original_content) > 40
            and len(proposal.content) < len(original_content) * 0.3
        ):
            continue

        if "\\" in original_content and "\\" not in proposal.content:
            # The original is clearly LaTeX (uses commands) but the
            # proposed replacement has no LaTeX commands at all -- a
            # reliable sign the model echoed prose/instructions back
            # instead of producing an actual document.
            continue

        if proposal.path != main_file:
            proposal = proposal.model_copy(update={"path": main_file})

        return proposal

    return None


def learn_writing_style(samples: list[str]) -> str:
    """Synthesize a writing-style profile from user-provided samples.

    Args:
        samples (list[str]): Raw text samples of the user's own
            writing.

    Returns:
        str: The synthesized profile, already persisted via
        ``set_writing_style_profile``.

    Raises:
        ValueError: If the external provider is misconfigured.
        ConnectionError: If the provider could not be reached.
    """
    joined_samples = "\n\n---\n\n".join(samples)
    messages = [
        {
            "role": "system",
            "content": (
                "You analyze writing samples and produce a concise "
                "style profile (tone, vocabulary preferences, typical "
                "sentence length and structure, any recurring habits) "
                "that another writing assistant can use to match the "
                "author's voice. Respond with the profile as plain "
                "text only -- no JSON, no preamble."
            ),
        },
        {
            "role": "user",
            "content": "Here are writing samples from one author:\n\n"
            + joined_samples,
        },
    ]

    profile = generate_completion(messages).strip()
    set_writing_style_profile(profile)
    return profile


def generate_commit_message(diff_text: str) -> str:
    """Ask the assistant to summarize a diff as a one-line commit message.

    Used by the "Sync Changes" feature (``services.git_history``) to
    write a commit message for you when there are uncommitted changes
    and you didn't type one yourself; if this fails (no provider
    configured, unreachable), the caller falls back to asking you
    directly instead.

    Args:
        diff_text (str): Unified diff of the changes to summarize.

    Returns:
        str: A single-line, imperative-mood commit message. Falls back
        to a generic message only if the model's own reply is somehow
        empty after cleanup -- a genuine provider failure still raises,
        rather than silently returning this fallback.

    Raises:
        ValueError: If the provider is misconfigured.
        ConnectionError: If the provider could not be reached.
    """
    messages = [
        {
            "role": "system",
            "content": (
                "You write git commit messages. Given a diff, reply "
                "with ONLY a single-line, imperative-mood commit "
                "message summarizing it (e.g. 'Fix typo in abstract', "
                "'Add methodology section') -- no quotes, no "
                "explanation, no markdown, nothing else."
            ),
        },
        {"role": "user", "content": diff_text},
    ]

    raw_reply = generate_completion(messages).strip()
    cleaned = _strip_wrapping_fence(raw_reply).strip().strip("\"'")
    first_line = cleaned.splitlines()[0].strip() if cleaned else ""
    return first_line or "Update project files"


def apply_proposal(
    project_id: str, proposal: AssistantProposal, author: str
) -> str:
    """Apply one previously-returned proposal and commit it to history.

    This is the only function in this module that writes to project
    files. Every applied change is immediately committed to the
    project's git history (the same mechanism the History panel's
    "Save Version" uses), so it stays recoverable through a revert even
    if the change itself removes something.

    Args:
        project_id (str): Project to apply the change to.
        proposal (AssistantProposal): A proposal previously returned by
            ``chat``/``suggest_improvements``/``fix_compile_error``.
        author (str): Display name recorded as the commit author.

    Returns:
        str: The id of the commit recording this change.

    Raises:
        ValueError: If the proposal is malformed, or there is nothing
            to commit (the proposed change is a no-op).
        FileNotFoundError: If the project, or (for delete/rename) the
            target path, does not exist.
    """
    if proposal.action == "write_file":
        if proposal.content is None:
            raise ValueError("write_file proposal is missing content")
        project_files.write_file(project_id, proposal.path, proposal.content)
    elif proposal.action == "create_entry":
        entry_type = proposal.entry_type or "file"
        project_files.create_entry(project_id, proposal.path, entry_type)
        if entry_type == "file" and proposal.content:
            project_files.write_file(project_id, proposal.path, proposal.content)
    elif proposal.action == "delete_entry":
        project_files.delete_entry(project_id, proposal.path)
    elif proposal.action == "rename_entry":
        if not proposal.new_path:
            raise ValueError("rename_entry proposal is missing new_path")
        project_files.rename_entry(project_id, proposal.path, proposal.new_path)
    else:
        raise ValueError(f"unknown proposal action: {proposal.action!r}")

    commit = git_history.create_commit(
        project_id,
        message=f"AI assistant: {proposal.description}",
        author=author,
    )
    return commit.id


# ─── Background job polling (chat / fix-compile-error) ───
#
# Why this exists: a provider call -- local or external -- can
# legitimately take longer than any fixed timeout short enough to
# still be reasonable to block an HTTP request on. Observed live: a
# CPU-only local model under load, and separately an external
# provider, each exceeded even a 180-second synchronous wait for a
# single realistic fix_compile_error call (which itself can make two
# sequential provider calls, via its retry). Rather than raising that
# ceiling further -- which only delays, not prevents, the same failure
# -- chat() and fix_compile_error() run on a background thread here;
# the caller polls get_assistant_job() for the result instead of
# waiting on the original request. This removes the hard wall-clock
# ceiling entirely rather than just making it more generous.

_assistant_job_lock = threading.Lock()
_assistant_jobs: dict[str, dict[str, Any]] = {}

# How long a finished (done/error) job's result stays available to
# poll for, before being pruned. This process can run indefinitely, so
# without this, _assistant_jobs would grow without bound over a
# long-running deployment; an hour is far longer than any client would
# plausibly keep polling for one answer.
_ASSISTANT_JOB_TTL_SECONDS = 3600


def _prune_old_assistant_jobs() -> None:
    cutoff = time.monotonic() - _ASSISTANT_JOB_TTL_SECONDS
    stale_ids = [
        job_id
        for job_id, job in _assistant_jobs.items()
        if job["status"] != "running" and job["started_at"] < cutoff
    ]
    for job_id in stale_ids:
        del _assistant_jobs[job_id]


def _start_assistant_job(fn: Callable[[], Any]) -> str:
    """Run ``fn`` on a background thread; return an id to poll for its result.

    Args:
        fn (Callable[[], Any]): Zero-argument callable to run. Its
            return value (typically an ``AssistantChatResult`` or
            ``AiAssistResult``) becomes the job's result; any exception
            it raises is captured (by type name and message) rather
            than propagated, for ``get_assistant_job`` to report.

    Returns:
        str: Job id to pass to ``get_assistant_job``.
    """
    job_id = str(uuid.uuid4())

    with _assistant_job_lock:
        _assistant_jobs[job_id] = {
            "status": "running",
            "result": None,
            "error": None,
            "error_type": None,
            "started_at": time.monotonic(),
        }
        _prune_old_assistant_jobs()

    def run() -> None:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 -- surfaced via polling, not raised
            logger.warning("assistant job %s failed: %s", job_id, exc)
            with _assistant_job_lock:
                _assistant_jobs[job_id]["status"] = "error"
                _assistant_jobs[job_id]["error"] = str(exc)
                _assistant_jobs[job_id]["error_type"] = type(exc).__name__
            return

        with _assistant_job_lock:
            _assistant_jobs[job_id]["status"] = "done"
            _assistant_jobs[job_id]["result"] = result

    threading.Thread(target=run, daemon=True).start()
    return job_id


def get_assistant_job(job_id: str) -> dict[str, Any] | None:
    """Look up a background job's current state.

    Args:
        job_id (str): Id previously returned by ``_start_assistant_job``.

    Returns:
        dict[str, Any] | None: A copy of the job's state (``status``,
        ``result``, ``error``, ``error_type``), or None if the id is
        unknown or has expired (see ``_ASSISTANT_JOB_TTL_SECONDS``).
    """
    with _assistant_job_lock:
        job = _assistant_jobs.get(job_id)
        return dict(job) if job is not None else None


def start_chat_job(
    project_id: str,
    messages: list[AssistantChatMessage],
    main_file: str | None,
) -> str:
    """Start ``chat`` as a background job. See ``_start_assistant_job``."""
    return _start_assistant_job(lambda: chat(project_id, messages, main_file))


def start_fix_compile_error_job(project_id: str, log: str, main_file: str) -> str:
    """Start ``fix_compile_error`` as a background job.

    See ``_start_assistant_job``.
    """
    return _start_assistant_job(lambda: fix_compile_error(project_id, log, main_file))
