"""AI writing assistant: config, provider abstraction, and the
propose/apply flow that lets it edit project files.

Provider abstraction: "local" calls a self-hosted Ollama server
(``config.OLLAMA_BASE_URL``, a different container in the Docker
deployment -- see docker-compose.yml); "external" calls a
user-configured Anthropic- or OpenAI-compatible HTTP API using a
user-supplied API key. Either way, the rest of this module only ever
calls ``generate_completion()`` and does not know which provider
answered.

Tool-calling protocol: rather than depending on a specific provider's
native structured tool-calling API (support for which varies widely,
especially on the kind of small model this is meant to run locally),
the model is instructed, via the system prompt, to reply with a single
JSON object: ``{"reply": "...", "proposals": [...]}``. This is a
deliberate simplification -- it trades the precision of a real
tool-calling API for working the same way against *any* chat-capable
model, local or external, small or large. ``_parse_json_response``
tolerates a model that doesn't follow the format perfectly (wraps its
answer in prose, in a markdown fence, etc.) by falling back to
treating the whole reply as plain text with no proposals, rather than
raising.

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
import uuid
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
)
from . import git_history, project_files

logger = logging.getLogger(__name__)

_DEFAULT_LOCAL_MODEL = "llama3.2:1b"
_MODEL_PULL_TIMEOUT_SECONDS = 600

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

_JSON_PROPOSAL_INSTRUCTIONS = (
    "Respond with a single JSON object and nothing else -- no prose "
    "before or after it, no markdown code fences. The object must have "
    'this exact shape: {"reply": "<a short message to show the '
    'user>", "proposals": [<zero or more proposal objects>]}. Each '
    "proposal object is one of:\n"
    '  {"action": "write_file", "path": "<project-relative path>", '
    '"content": "<the full new file content>", "description": '
    '"<one sentence summary>"}\n'
    '  {"action": "create_entry", "path": "<path>", "entry_type": '
    '"file" or "dir", "content": "<initial content, files only, may '
    'be empty>", "description": "..."}\n'
    '  {"action": "delete_entry", "path": "<path>", "description": "..."}\n'
    '  {"action": "rename_entry", "path": "<current path>", '
    '"new_path": "<new path>", "description": "..."}\n'
    "Never invent a proposal the user did not ask for -- if you are "
    'only answering a question, return an empty "proposals" list. '
    "When you do propose a write_file change, keep it additive and "
    "targeted: add or refine content rather than deleting sections "
    "the user did not ask you to remove."
)


# ─── Config persistence ───


def _default_config_dict() -> dict[str, Any]:
    return {
        "provider": "local",
        "local_model": _DEFAULT_LOCAL_MODEL,
        "external_provider_kind": "anthropic",
        "external_base_url": None,
        "external_model": None,
        "external_api_key": None,
        "writing_style_profile": None,
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
        AssistantProviderConfig: Never includes the raw API key, only
        whether one is set (``has_external_api_key``).
    """
    raw = _load_raw_config()
    return AssistantProviderConfig(
        provider=raw["provider"],
        local_model=raw["local_model"],
        external_provider_kind=raw["external_provider_kind"],
        external_base_url=raw.get("external_base_url"),
        external_model=raw.get("external_model"),
        has_external_api_key=bool(raw.get("external_api_key")),
        writing_style_profile=raw.get("writing_style_profile"),
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


def set_writing_style_profile(profile: str) -> None:
    """Persist a newly-learned writing-style profile.

    Args:
        profile (str): Free-text style summary, included in every
            future prompt built by this module.
    """
    raw = _load_raw_config()
    raw["writing_style_profile"] = profile
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


def _ensure_ollama_model(model: str, base_url: str) -> None:
    """Pull a model into the local Ollama server if not present.

    Blocks until the pull completes (or fails) -- model pulls can take
    minutes, hence the much longer timeout than a normal chat request.
    """
    logger.info("pulling ollama model %r (first use)...", model)
    response = httpx.post(
        f"{base_url.rstrip('/')}/api/pull",
        json={"model": model, "stream": False},
        timeout=_MODEL_PULL_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def _call_ollama(messages: list[dict[str, str]], model: str) -> str:
    base_url = config.OLLAMA_BASE_URL

    if not _ollama_has_model(model, base_url):
        _ensure_ollama_model(model, base_url)

    try:
        response = httpx.post(
            f"{base_url.rstrip('/')}/api/chat",
            json={"model": model, "messages": messages, "stream": False},
            timeout=config.AI_ASSIST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise ConnectionError(
            f"could not reach the local Ollama server at {base_url}: {error}"
        ) from error

    return str(response.json()["message"]["content"])


def _call_external_openai(
    messages: list[dict[str, str]], model: str, base_url: str, api_key: str
) -> str:
    try:
        response = httpx.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": messages},
            timeout=config.AI_ASSIST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise ConnectionError(f"external provider request failed: {error}") from error

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
                "max_tokens": 4096,
                "system": system_text or None,
                "messages": conversation,
            },
            timeout=config.AI_ASSIST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise ConnectionError(f"external provider request failed: {error}") from error

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
        ValueError: If the "external" provider is selected but is not
            fully configured (base_url/model/api_key).
        ConnectionError: If the provider could not be reached or
            returned an error.
    """
    raw = _load_raw_config()

    if raw["provider"] == "external":
        base_url = raw.get("external_base_url") or _DEFAULT_EXTERNAL_BASE_URLS.get(
            raw["external_provider_kind"]
        )
        model = raw.get("external_model")
        api_key = raw.get("external_api_key")

        if not model or not api_key:
            raise ValueError(
                "external provider selected but external_model/"
                "external_api_key are not fully configured"
            )

        if raw["external_provider_kind"] == "openai":
            return _call_external_openai(messages, model, base_url, api_key)

        return _call_external_anthropic(messages, model, base_url, api_key)

    return _call_ollama(messages, raw["local_model"])


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


def _parse_json_response(raw_text: str) -> dict[str, Any]:
    """Extract the JSON object a model returned.

    Tolerates a model that didn't follow the formatting instructions
    exactly: a leading/trailing markdown code fence, or extra prose
    around the JSON object. Falls back to treating the entire reply as
    plain text with no proposals, rather than raising, since a small
    local model occasionally ignoring format instructions should
    degrade to "just a chat reply", not a hard failure.

    Args:
        raw_text (str): The model's raw response text.

    Returns:
        dict[str, Any]: Parsed with ``"reply"`` and ``"proposals"``
        keys, both defaulted if absent/unparsable.
    """
    text = raw_text.strip()

    fence_match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    return {"reply": raw_text, "proposals": []}


def _coerce_proposals(raw_proposals: object) -> list[AssistantProposal]:
    if not isinstance(raw_proposals, list):
        return []

    proposals: list[AssistantProposal] = []
    for raw in raw_proposals:
        if not isinstance(raw, dict):
            continue

        action = raw.get("action")
        path = raw.get("path")

        if action not in _PROPOSAL_ACTIONS or not path:
            continue

        proposals.append(
            AssistantProposal(
                id=str(uuid.uuid4()),
                action=action,
                path=path,
                new_path=raw.get("new_path"),
                entry_type=raw.get("entry_type"),
                content=raw.get("content"),
                description=str(raw.get("description") or f"{action} {path}"),
            )
        )

    return proposals


def _run_prompt(system_prompt: str, user_content: str) -> AssistantChatResult:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    raw_reply = generate_completion(messages)
    parsed = _parse_json_response(raw_reply)

    return AssistantChatResult(
        reply=str(parsed.get("reply", raw_reply)),
        proposals=_coerce_proposals(parsed.get("proposals")),
    )


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

    raw_reply = generate_completion(context_messages)
    parsed = _parse_json_response(raw_reply)

    return AssistantChatResult(
        reply=str(parsed.get("reply", raw_reply)),
        proposals=_coerce_proposals(parsed.get("proposals")),
    )


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

    result = _run_prompt(
        _build_system_prompt(style),
        f"Compiling {main_file!r} failed. Here is the compile log:\n\n"
        f"{log}\n\nHere is the current content of {main_file!r}:\n\n"
        f"{content}\n\nExplain what's wrong in plain language, and if "
        "you can fix it, propose exactly one write_file change with "
        "the corrected content. Keep the fix as small and targeted as "
        "possible -- don't remove content unrelated to the error.",
    )

    return AiAssistResult(
        suggestion=result.reply,
        proposal=result.proposals[0] if result.proposals else None,
    )


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
