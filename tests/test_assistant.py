"""Tests for the AI assistant service: config, provider dispatch, the
JSON proposal protocol, and the propose/apply flow.

Provider HTTP calls are monkeypatched (``httpx.get``/``httpx.post``)
rather than hitting a real Ollama server or external API -- this suite
never makes a network call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Self

import git
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from webapp import config
from webapp.models import (
    AssistantChatMessage,
    AssistantConfigUpdate,
    AssistantProposal,
    ExternalProviderProfileCreate,
)
from webapp.routers import assistant as assistant_router
from webapp.services import assistant

_PROJECT_ID = "test-project"
_TEST_AUTHOR = git.Actor("tester", "tester@localhost")


@pytest.fixture(autouse=True)
def assistant_config_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the assistant config at a throwaway directory per test."""
    config_root = tmp_path / "assistant_config"
    monkeypatch.setattr(config, "ASSISTANT_CONFIG_ROOT", config_root)
    monkeypatch.setattr(config, "ASSISTANT_CONFIG_PATH", config_root / "settings.json")
    return config_root


@pytest.fixture(autouse=True)
def workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point config.WORKSPACE_ROOT at a throwaway directory per test."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", workspace)
    return workspace


@pytest.fixture
def project_repo_dir(workspace_root: Path) -> Path:
    """Create a project dir that is already a git repo with one commit."""
    project_path = workspace_root / _PROJECT_ID
    project_path.mkdir(parents=True)

    repo = git.Repo.init(project_path)
    (project_path / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nHello\n\\end{document}\n",
        encoding="utf-8",
    )
    repo.index.add(["main.tex"])
    repo.index.commit("Initial commit", author=_TEST_AUTHOR, committer=_TEST_AUTHOR)

    return project_path


class _FakeResponse:
    def __init__(
        self,
        json_data: dict[str, Any],
        status_code: int = 200,
        reason_phrase: str = "Error",
    ) -> None:
        self._json_data = json_data
        self.status_code = status_code
        self.reason_phrase = reason_phrase

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error", request=None, response=self  # type: ignore[arg-type]
            )

    def json(self) -> dict[str, Any]:
        return self._json_data


class _FakeStreamResponse:
    """Fake for the ``httpx.stream(...)`` context manager used by the
    Ollama model-pull path (``/api/pull`` with ``stream: true``)."""

    def __init__(
        self,
        lines: list[str] | None = None,
        status_code: int = 200,
        reason_phrase: str = "Error",
    ) -> None:
        self._lines = lines or []
        self.status_code = status_code
        self.reason_phrase = reason_phrase

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error", request=None, response=self  # type: ignore[arg-type]
            )

    def iter_lines(self):
        return iter(self._lines)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def _fake_pull_stream(lines: list[str] | None = None):
    def fake_stream(method: str, url: str, json: dict[str, Any], timeout: float) -> _FakeStreamResponse:
        return _FakeStreamResponse(lines)

    return fake_stream


# ─── Config ───


def test_get_config_defaults() -> None:
    cfg = assistant.get_config()
    assert cfg.provider == "local"
    assert cfg.local_model == assistant.DEFAULT_LOCAL_MODEL == "gemma4:31b-cloud"
    assert cfg.has_external_api_key is False
    assert cfg.writing_style_profile is None
    assert cfg.fallback_profiles == []


def test_update_config_roundtrips_and_hides_api_key() -> None:
    updated = assistant.update_config(
        AssistantConfigUpdate(
            provider="external",
            external_provider_kind="openai",
            external_model="gpt-4o",
            external_api_key="sk-secret",
        )
    )

    assert updated.provider == "external"
    assert updated.external_provider_kind == "openai"
    assert updated.external_model == "gpt-4o"
    assert updated.has_external_api_key is True

    raw = assistant._load_raw_config()
    assert raw["external_api_key"] == "sk-secret"


def test_update_config_clear_api_key() -> None:
    assistant.update_config(AssistantConfigUpdate(external_api_key="sk-secret"))
    cleared = assistant.update_config(AssistantConfigUpdate(clear_external_api_key=True))

    assert cleared.has_external_api_key is False
    assert assistant._load_raw_config()["external_api_key"] is None


def test_update_config_rejects_invalid_provider() -> None:
    with pytest.raises(ValueError):
        assistant.update_config(AssistantConfigUpdate(provider="bogus"))


def test_update_config_rejects_invalid_provider_kind() -> None:
    with pytest.raises(ValueError):
        assistant.update_config(
            AssistantConfigUpdate(external_provider_kind="bogus")
        )


# ─── Provider dispatch ───


def test_generate_completion_local_requires_a_configured_model() -> None:
    # A user who clears the model field must get a clear, actionable
    # error rather than a silent fallback to the default.
    assistant.update_config(AssistantConfigUpdate(local_model=""))

    with pytest.raises(ValueError, match="no local model is configured"):
        assistant.generate_completion([{"role": "user", "content": "hi"}])


def test_generate_completion_local_pulls_missing_model_then_chats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assistant.update_config(AssistantConfigUpdate(local_model="qwen2.5-coder:3b"))
    calls: list[str] = []

    def fake_get(url: str, timeout: float) -> _FakeResponse:
        calls.append(f"GET {url}")
        return _FakeResponse({"models": []})

    def fake_stream(method: str, url: str, json: dict[str, Any], timeout: float) -> _FakeStreamResponse:
        calls.append(f"{method} {url}")
        return _FakeStreamResponse(['{"status": "success"}'])

    def fake_post(url: str, json: dict[str, Any], timeout: float, **_: Any) -> _FakeResponse:
        calls.append(f"POST {url}")
        return _FakeResponse({"message": {"content": "hello from ollama"}})

    monkeypatch.setattr("httpx.get", fake_get)
    monkeypatch.setattr("httpx.stream", fake_stream)
    monkeypatch.setattr("httpx.post", fake_post)

    reply = assistant.generate_completion([{"role": "user", "content": "hi"}])

    assert reply == "hello from ollama"
    assert any("/api/pull" in call for call in calls)
    assert any("/api/chat" in call for call in calls)


def test_generate_completion_local_unreachable_raises_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ollama not running/reachable at all (not just "model missing") --
    # this must surface as a clean ConnectionError, not an unhandled
    # httpx exception bubbling past the API layer as a bare 500.
    assistant.update_config(AssistantConfigUpdate(local_model="qwen2.5-coder:3b"))

    def fake_get(url: str, timeout: float) -> _FakeResponse:
        raise httpx.ConnectError("connection refused")

    def fake_stream(method: str, url: str, json: dict[str, Any], timeout: float) -> _FakeStreamResponse:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("httpx.get", fake_get)
    monkeypatch.setattr("httpx.stream", fake_stream)

    with pytest.raises(ConnectionError):
        assistant.generate_completion([{"role": "user", "content": "hi"}])


def test_generate_completion_local_skips_pull_when_model_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assistant.update_config(AssistantConfigUpdate(local_model="qwen2.5-coder:3b"))

    def fake_get(url: str, timeout: float) -> _FakeResponse:
        return _FakeResponse({"models": [{"name": "qwen2.5-coder:3b"}]})

    pull_called = False

    def fake_stream(method: str, url: str, json: dict[str, Any], timeout: float) -> _FakeStreamResponse:
        nonlocal pull_called
        pull_called = True
        return _FakeStreamResponse(['{"status": "success"}'])

    def fake_post(url: str, json: dict[str, Any], timeout: float, **_: Any) -> _FakeResponse:
        return _FakeResponse({"message": {"content": "ok"}})

    monkeypatch.setattr("httpx.get", fake_get)
    monkeypatch.setattr("httpx.stream", fake_stream)
    monkeypatch.setattr("httpx.post", fake_post)

    assistant.generate_completion([{"role": "user", "content": "hi"}])
    assert pull_called is False


def test_generate_completion_local_sets_generous_num_predict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ollama's own default generation length is short enough to
    # routinely cut a reply off right as it reaches the file content
    # (observed live) -- this must always be overridden explicitly.
    assistant.update_config(AssistantConfigUpdate(local_model="llama3.2:1b"))

    def fake_get(url: str, timeout: float) -> _FakeResponse:
        return _FakeResponse({"models": [{"name": "llama3.2:1b"}]})

    captured: dict[str, Any] = {}

    def fake_post(url: str, json: dict[str, Any], timeout: float, **_: Any) -> _FakeResponse:
        captured["json"] = json
        return _FakeResponse({"message": {"content": "ok"}})

    monkeypatch.setattr("httpx.get", fake_get)
    monkeypatch.setattr("httpx.post", fake_post)

    assistant.generate_completion([{"role": "user", "content": "hi"}])

    assert captured["json"]["options"]["num_predict"] >= 2048


def test_generate_completion_external_requires_configuration() -> None:
    assistant.update_config(AssistantConfigUpdate(provider="external"))

    with pytest.raises(ValueError):
        assistant.generate_completion([{"role": "user", "content": "hi"}])


def test_generate_completion_external_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    assistant.update_config(
        AssistantConfigUpdate(
            provider="external",
            external_provider_kind="openai",
            external_model="gpt-4o",
            external_api_key="sk-test",
        )
    )

    captured: dict[str, Any] = {}

    def fake_post(url: str, headers: dict[str, str], json: dict[str, Any], timeout: float) -> _FakeResponse:
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _FakeResponse(
            {"choices": [{"message": {"content": "openai reply"}}]}
        )

    monkeypatch.setattr("httpx.post", fake_post)

    reply = assistant.generate_completion([{"role": "user", "content": "hi"}])

    assert reply == "openai reply"
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer sk-test"


def test_generate_completion_external_anthropic_separates_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assistant.update_config(
        AssistantConfigUpdate(
            provider="external",
            external_provider_kind="anthropic",
            external_model="claude-opus-4-8",
            external_api_key="sk-ant-test",
        )
    )

    captured: dict[str, Any] = {}

    def fake_post(url: str, headers: dict[str, str], json: dict[str, Any], timeout: float) -> _FakeResponse:
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _FakeResponse({"content": [{"type": "text", "text": "claude reply"}]})

    monkeypatch.setattr("httpx.post", fake_post)

    reply = assistant.generate_completion(
        [
            {"role": "system", "content": "be helpful"},
            {"role": "user", "content": "hi"},
        ]
    )

    assert reply == "claude reply"
    assert captured["url"].endswith("/v1/messages")
    assert captured["headers"]["x-api-key"] == "sk-ant-test"
    assert captured["json"]["system"] == "be helpful"
    assert captured["json"]["messages"] == [{"role": "user", "content": "hi"}]


def test_generate_completion_external_429_gives_actionable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assistant.update_config(
        AssistantConfigUpdate(
            provider="external",
            external_provider_kind="openai",
            external_model="gpt-4o-mini",
            external_api_key="sk-test",
        )
    )

    def fake_post(url: str, headers: dict[str, str], json: dict[str, Any], timeout: float) -> _FakeResponse:
        return _FakeResponse({}, status_code=429, reason_phrase="Too Many Requests")

    monkeypatch.setattr("httpx.post", fake_post)

    with pytest.raises(ConnectionError, match="429.*quota|rate-limit"):
        assistant.generate_completion([{"role": "user", "content": "hi"}])


def test_generate_completion_external_429_surfaces_providers_own_error_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # OpenAI's 429 covers two different real conditions distinguished
    # only by error.type in the body -- surfacing it directly resolves
    # the "429 but my dashboard shows no usage" confusion a generic
    # "rate-limited or quota exhausted" hint can't.
    assistant.update_config(
        AssistantConfigUpdate(
            provider="external",
            external_provider_kind="openai",
            external_model="gpt-4o-mini",
            external_api_key="sk-test",
        )
    )

    def fake_post(url: str, headers: dict[str, str], json: dict[str, Any], timeout: float) -> _FakeResponse:
        return _FakeResponse(
            {"error": {"type": "insufficient_quota", "message": "You exceeded your quota"}},
            status_code=429,
            reason_phrase="Too Many Requests",
        )

    monkeypatch.setattr("httpx.post", fake_post)

    with pytest.raises(ConnectionError, match="insufficient_quota"):
        assistant.generate_completion([{"role": "user", "content": "hi"}])


def test_generate_completion_external_failover_promotes_working_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assistant.update_config(
        AssistantConfigUpdate(
            provider="external",
            external_provider_kind="openai",
            external_model="gpt-4o-mini",
            external_api_key="sk-primary",
        )
    )
    assistant.add_fallback_profile(
        ExternalProviderProfileCreate(
            label="backup",
            provider_kind="openai",
            model="deepseek-chat",
            api_key="sk-backup",
            base_url="https://api.deepseek.com",
        )
    )

    def fake_post(url: str, headers: dict[str, str], json: dict[str, Any], timeout: float) -> _FakeResponse:
        if headers["Authorization"] == "Bearer sk-primary":
            return _FakeResponse({}, status_code=429, reason_phrase="Too Many Requests")
        return _FakeResponse({"choices": [{"message": {"content": "from backup"}}]})

    monkeypatch.setattr("httpx.post", fake_post)

    reply, notice = assistant.generate_completion_with_notice(
        [{"role": "user", "content": "hi"}]
    )

    assert reply == "from backup"
    assert notice is not None and "switched" in notice

    # The switch is persisted: the backup is now primary, and the
    # rate-limited provider was demoted to a fallback instead of lost.
    cfg = assistant.get_config()
    assert cfg.external_model == "deepseek-chat"
    assert len(cfg.fallback_profiles) == 1
    assert cfg.fallback_profiles[0].model == "gpt-4o-mini"


def test_generate_completion_external_non_rate_limit_error_does_not_fail_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A bad API key is a configuration problem the user needs to see
    # and fix -- it must not be silently masked by trying a fallback.
    assistant.update_config(
        AssistantConfigUpdate(
            provider="external",
            external_provider_kind="openai",
            external_model="gpt-4o-mini",
            external_api_key="sk-bad",
        )
    )
    assistant.add_fallback_profile(
        ExternalProviderProfileCreate(
            label="backup", provider_kind="openai", model="deepseek-chat", api_key="sk-backup"
        )
    )

    fallback_called = False

    def fake_post(url: str, headers: dict[str, str], json: dict[str, Any], timeout: float) -> _FakeResponse:
        nonlocal fallback_called
        if headers["Authorization"] != "Bearer sk-bad":
            fallback_called = True
        return _FakeResponse({}, status_code=401, reason_phrase="Unauthorized")

    monkeypatch.setattr("httpx.post", fake_post)

    with pytest.raises(ConnectionError, match="API key"):
        assistant.generate_completion([{"role": "user", "content": "hi"}])

    assert fallback_called is False


def test_generate_completion_external_401_gives_actionable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assistant.update_config(
        AssistantConfigUpdate(
            provider="external",
            external_provider_kind="openai",
            external_model="gpt-4o-mini",
            external_api_key="sk-test",
        )
    )

    def fake_post(url: str, headers: dict[str, str], json: dict[str, Any], timeout: float) -> _FakeResponse:
        return _FakeResponse({}, status_code=401, reason_phrase="Unauthorized")

    monkeypatch.setattr("httpx.post", fake_post)

    with pytest.raises(ConnectionError, match="API key"):
        assistant.generate_completion([{"role": "user", "content": "hi"}])


# ─── JSON proposal protocol ───


def test_parse_json_response_plain_json() -> None:
    parsed = assistant._parse_json_response('{"reply": "hi", "proposals": []}')
    assert parsed == {"reply": "hi", "proposals": []}


def test_parse_json_response_strips_code_fence() -> None:
    parsed = assistant._parse_json_response(
        '```json\n{"reply": "hi", "proposals": []}\n```'
    )
    assert parsed == {"reply": "hi", "proposals": []}


def test_parse_json_response_falls_back_to_plain_text() -> None:
    parsed = assistant._parse_json_response("just a plain sentence, no JSON here")
    assert parsed["reply"] == "just a plain sentence, no JSON here"
    assert parsed["proposals"] == []


def test_parse_json_response_tolerates_unescaped_newlines() -> None:
    # A very common small-model mistake: a literal newline inside a
    # JSON string value instead of an escaped "\n". Strict JSON
    # rejects this outright; this must not leak the raw JSON text to
    # the user as a result.
    raw = '{"reply": "line one\nline two", "proposals": []}'
    parsed = assistant._parse_json_response(raw)
    assert parsed["reply"] == "line one\nline two"
    assert parsed["proposals"] == []


def test_parse_json_response_rejects_non_object_top_level() -> None:
    # Valid JSON, but not an object -- must not crash downstream code
    # that expects dict-like access (.get()), and must not surface the
    # raw JSON syntax as the reply either.
    parsed = assistant._parse_json_response('"just a JSON string, not an object"')
    assert parsed["reply"] == '"just a JSON string, not an object"'
    assert parsed["proposals"] == []


def test_coerce_proposals_filters_invalid_entries() -> None:
    proposals = assistant._coerce_proposals(
        [
            {"action": "write_file", "path": "main.tex", "content": "x"},
            {"action": "not_a_real_action", "path": "main.tex"},
            {"action": "write_file"},  # missing path
            "not even a dict",
        ]
    )
    assert len(proposals) == 1
    assert proposals[0].action == "write_file"


# ─── Out-of-band content blocks (LaTeX-safe transport) ───


def test_extract_content_blocks_single() -> None:
    text = "\n---CONTENT---\n\\documentclass{article}\n---CONTENT---\n"
    assert assistant._extract_content_blocks(text) == ["\\documentclass{article}"]


def test_extract_content_blocks_multiple_in_order() -> None:
    text = (
        "---CONTENT---\nfirst\n---CONTENT---\n"
        "some text in between\n"
        "---CONTENT---\nsecond\n---CONTENT---\n"
    )
    assert assistant._extract_content_blocks(text) == ["first", "second"]


def test_extract_content_blocks_strips_markdown_fence() -> None:
    # Models sometimes wrap the block's content in a code fence anyway
    # despite being told not to -- observed live with gemma3:1b.
    text = "---CONTENT---\n```latex\n\\documentclass{article}\n```\n---CONTENT---\n"
    assert assistant._extract_content_blocks(text) == ["\\documentclass{article}"]


def test_extract_content_blocks_falls_back_to_plain_fence() -> None:
    # Observed live with qwen2.5-coder:3b: it ignored the ---CONTENT---
    # marker entirely and used a markdown horizontal rule plus an
    # ordinary fenced code block instead.
    text = (
        "\n\n---\n\n```latex\n"
        "\\documentclass{article}\n\\begin{document}\n\\end{document}\n"
        "```"
    )
    assert assistant._extract_content_blocks(text) == [
        "\\documentclass{article}\n\\begin{document}\n\\end{document}"
    ]


def test_parse_model_response_handles_plain_fence_fallback_end_to_end() -> None:
    raw = (
        "```json\n"
        '{"reply": "Added the missing preamble.", "proposals": '
        '[{"action": "write_file", "path": "main.tex", '
        '"description": "add preamble"}]}\n'
        "```\n\n---\n\n```latex\n"
        "\\documentclass{article}\n\\begin{document}\nHello\n\\end{document}\n"
        "```"
    )

    result = assistant._parse_model_response(raw)

    assert result.reply == "Added the missing preamble."
    assert len(result.proposals) == 1
    assert result.proposals[0].content == (
        "\\documentclass{article}\n\\begin{document}\nHello\n\\end{document}"
    )


def test_coerce_proposals_attaches_content_block_when_json_omits_it() -> None:
    proposals = assistant._coerce_proposals(
        [{"action": "write_file", "path": "main.tex", "description": "fix"}],
        content_blocks=["\\documentclass{article}\n\\begin{document}\n\\end{document}"],
    )
    assert len(proposals) == 1
    assert proposals[0].content == "\\documentclass{article}\n\\begin{document}\n\\end{document}"


def test_coerce_proposals_prefers_inline_content_over_block() -> None:
    proposals = assistant._coerce_proposals(
        [{"action": "write_file", "path": "main.tex", "content": "inline"}],
        content_blocks=["from a block"],
    )
    assert proposals[0].content == "inline"


def test_coerce_proposals_drops_write_file_with_no_content_anywhere() -> None:
    # Regression test: a write_file proposal with no inline content and
    # no out-of-band block used to still reach the frontend, where
    # clicking Accept failed with a 400 ("missing content"). It must be
    # dropped before the caller ever sees it.
    proposals = assistant._coerce_proposals(
        [{"action": "write_file", "path": "main.tex", "description": "a change"}],
        content_blocks=None,
    )
    assert proposals == []


def test_coerce_proposals_skips_content_for_dir_create() -> None:
    proposals = assistant._coerce_proposals(
        [
            {
                "action": "create_entry",
                "path": "images",
                "entry_type": "dir",
                "description": "new folder",
            }
        ],
        content_blocks=["should not be used"],
    )
    assert proposals[0].content is None


def test_parse_model_response_handles_latex_backslashes_via_content_block() -> None:
    # The exact failure mode observed live: a model's JSON is backslash
    # -free (content lives in the block instead), so it parses cleanly
    # even though the actual file content is full of LaTeX commands
    # that would never survive being embedded as a JSON string.
    raw = (
        '{"reply": "Fixed the missing \\\\begin{document}.", "proposals": '
        '[{"action": "write_file", "path": "main.tex", "description": "add preamble"}]}\n'
        "---CONTENT---\n"
        "\\documentclass{article}\n\\begin{document}\nHello\n\\end{document}\n"
        "---CONTENT---\n"
    )

    result = assistant._parse_model_response(raw)

    assert result.reply == "Fixed the missing \\begin{document}."
    assert len(result.proposals) == 1
    # One trailing newline immediately before the closing marker is
    # trimmed, the same way a markdown code fence doesn't count the
    # newline right before the closing ``` as part of its content.
    assert result.proposals[0].content == (
        "\\documentclass{article}\n\\begin{document}\nHello\n\\end{document}"
    )


def test_parse_model_response_ignores_invalid_json_escapes_gracefully() -> None:
    # A model that ignores instructions and inlines raw LaTeX inside
    # the JSON string anyway produces invalid JSON (\\e, \\b etc. are
    # not valid JSON escapes) -- this must degrade to a plain-text
    # reply with no proposals, never raise.
    raw = '{"reply": "Use \\end{document}", "proposals": []}'

    result = assistant._parse_model_response(raw)

    assert result.proposals == []
    assert "end{document}" in result.reply


# ─── Chat / suggest / fix-compile-error ───


def test_chat_includes_main_file_content(
    project_repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_messages: list[dict[str, str]] = []

    def fake_generate_completion(messages: list[dict[str, str]]) -> tuple[str, None]:
        captured_messages.extend(messages)
        return '{"reply": "ok", "proposals": []}', None

    monkeypatch.setattr(assistant, "generate_completion_with_notice", fake_generate_completion)

    result = assistant.chat(
        _PROJECT_ID,
        [AssistantChatMessage(role="user", content="improve my intro")],
        main_file="main.tex",
    )

    assert result.reply == "ok"
    assert any("Hello" in message["content"] for message in captured_messages)


def test_suggest_improvements_returns_proposal(
    project_repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_generate_completion(messages: list[dict[str, str]]) -> tuple[str, None]:
        return (
            '{"reply": "found one thing", "proposals": '
            '[{"action": "write_file", "path": "main.tex", '
            '"content": "better", "description": "fix typo"}]}'
        ), None

    monkeypatch.setattr(assistant, "generate_completion_with_notice", fake_generate_completion)

    result = assistant.suggest_improvements(_PROJECT_ID, "main.tex")

    assert result.reply == "found one thing"
    assert len(result.proposals) == 1
    assert result.proposals[0].path == "main.tex"


def test_fix_compile_error_returns_single_proposal(
    project_repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_content = (
        "\\documentclass{article}\n\\begin{document}\nHello\n\\end{document}\n"
    )

    def fake_generate_completion(messages: list[dict[str, str]]) -> tuple[str, None]:
        return json.dumps(
            {
                "reply": "missing a brace",
                "proposals": [
                    {
                        "action": "write_file",
                        "path": "main.tex",
                        "content": fixed_content,
                        "description": "add missing brace",
                    }
                ],
            }
        ), None

    monkeypatch.setattr(assistant, "generate_completion_with_notice", fake_generate_completion)

    result = assistant.fix_compile_error(_PROJECT_ID, "! Undefined control sequence", "main.tex")

    assert result.suggestion == "missing a brace"
    assert result.proposal is not None
    assert result.proposal.description == "add missing brace"


def test_fix_compile_error_retries_once_when_content_missing(
    project_repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Observed live: a model will sometimes describe the fix in prose
    # without including usable content on the first try. One firm
    # retry should recover a usable proposal.
    fixed_content = (
        "\\documentclass{article}\n\\begin{document}\nFixed\n\\end{document}\n"
    )
    call_count = 0

    def fake_generate_completion(messages: list[dict[str, str]]) -> tuple[str, None]:
        nonlocal call_count
        call_count += 1

        if call_count == 1:
            return json.dumps(
                {
                    "reply": "Remove the broken line.",
                    "proposals": [
                        {
                            "action": "write_file",
                            "path": "main.tex",
                            "description": "remove broken line",
                        }
                    ],
                }
            ), None

        return json.dumps(
            {
                "reply": "Fixed it.",
                "proposals": [
                    {
                        "action": "write_file",
                        "path": "main.tex",
                        "content": fixed_content,
                        "description": "remove broken line",
                    }
                ],
            }
        ), None

    monkeypatch.setattr(assistant, "generate_completion_with_notice", fake_generate_completion)

    result = assistant.fix_compile_error(_PROJECT_ID, "! Undefined control sequence", "main.tex")

    assert call_count == 2
    assert result.suggestion == "Fixed it."
    assert result.proposal is not None
    assert result.proposal.content == fixed_content


def test_fix_compile_error_gives_up_after_one_retry(
    project_repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    call_count = 0

    def fake_generate_completion(messages: list[dict[str, str]]) -> tuple[str, None]:
        nonlocal call_count
        call_count += 1
        return json.dumps({"reply": "Still no content.", "proposals": []}), None

    monkeypatch.setattr(assistant, "generate_completion_with_notice", fake_generate_completion)

    result = assistant.fix_compile_error(_PROJECT_ID, "! Undefined control sequence", "main.tex")

    assert call_count == 2
    assert result.proposal is None
    # Falls back to the *first* attempt's reply, not the retry's --
    # there's no reason to prefer the retry's wording once it also
    # failed to produce a usable proposal.
    assert result.suggestion == "Still no content."


def test_select_fix_proposal_corrects_hallucinated_path() -> None:
    original = "\\documentclass{article}\n\\begin{document}\nHello\n\\end{document}\n"
    proposals = [
        AssistantProposal(
            id="p1",
            action="write_file",
            path="/tmp/some_other_name.tex",
            content="\\documentclass{article}\n\\begin{document}\nFixed\n\\end{document}\n",
            description="fix",
        )
    ]

    selected = assistant._select_fix_proposal(proposals, "main.tex", original)

    assert selected is not None
    assert selected.path == "main.tex"


def test_select_fix_proposal_drops_non_write_file_action() -> None:
    original = "\\documentclass{article}\n\\begin{document}\nHello\n\\end{document}\n"
    proposals = [
        AssistantProposal(
            id="p1", action="delete_entry", path="main.tex", description="delete it"
        )
    ]

    assert assistant._select_fix_proposal(proposals, "main.tex", original) is None


def test_select_fix_proposal_drops_drastically_shorter_content() -> None:
    original = "\\documentclass{article}\n\\begin{document}\nHello\n\\end{document}\n"
    proposals = [
        AssistantProposal(
            id="p1", action="write_file", path="main.tex", content="\\x", description="fix"
        )
    ]

    assert assistant._select_fix_proposal(proposals, "main.tex", original) is None


def test_select_fix_proposal_drops_non_latex_content() -> None:
    original = "\\documentclass{article}\n\\begin{document}\nHello\n\\end{document}\n"
    proposals = [
        AssistantProposal(
            id="p1",
            action="write_file",
            path="main.tex",
            content="Explain what is wrong and propose a fix for the document.",
            description="fix",
        )
    ]

    assert assistant._select_fix_proposal(proposals, "main.tex", original) is None


def test_learn_writing_style_persists_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assistant, "generate_completion", lambda messages: "  terse, formal  ")

    profile = assistant.learn_writing_style(["sample one", "sample two"])

    assert profile == "terse, formal"
    assert assistant.get_config().writing_style_profile == "terse, formal"


def test_clear_writing_style_profile_removes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assistant, "generate_completion", lambda messages: "terse, formal")
    assistant.learn_writing_style(["sample one"])
    assert assistant.get_config().writing_style_profile == "terse, formal"

    assistant.clear_writing_style_profile()

    assert assistant.get_config().writing_style_profile is None


def test_router_delete_writing_style(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assistant, "generate_completion", lambda messages: "terse, formal")
    assistant.learn_writing_style(["sample one"])

    client = _make_client()
    response = client.delete("/api/assistant/writing-style")

    assert response.status_code == 204
    assert assistant.get_config().writing_style_profile is None


# ─── Apply proposal ───


def test_apply_proposal_write_file_commits_to_history(project_repo_dir: Path) -> None:
    proposal = AssistantProposal(
        id="p1",
        action="write_file",
        path="main.tex",
        content="\\documentclass{article}\n\\begin{document}\nUpdated\n\\end{document}\n",
        description="update greeting",
    )

    commit_id = assistant.apply_proposal(_PROJECT_ID, proposal, author="AI assistant")

    assert commit_id
    assert "Updated" in (project_repo_dir / "main.tex").read_text(encoding="utf-8")

    repo = git.Repo(project_repo_dir)
    assert repo.head.commit.message.startswith("AI assistant: update greeting")


def test_apply_proposal_create_entry(project_repo_dir: Path) -> None:
    proposal = AssistantProposal(
        id="p2",
        action="create_entry",
        path="notes.tex",
        entry_type="file",
        content="\\section{Notes}",
        description="add notes file",
    )

    assistant.apply_proposal(_PROJECT_ID, proposal, author="AI assistant")

    assert (project_repo_dir / "notes.tex").read_text(encoding="utf-8") == "\\section{Notes}"


def test_apply_proposal_unknown_action_rejected(project_repo_dir: Path) -> None:
    proposal = AssistantProposal(
        id="p3", action="nonsense", path="main.tex", description="bad"
    )

    with pytest.raises(ValueError):
        assistant.apply_proposal(_PROJECT_ID, proposal, author="AI assistant")


# ─── Background job polling (chat / fix-compile-error) ───


def _wait_for_job_done(job_id: str, timeout_iterations: int = 50) -> dict:
    import time

    for _ in range(timeout_iterations):
        job = assistant.get_assistant_job(job_id)
        if job is not None and job["status"] != "running":
            return job
        time.sleep(0.05)

    raise AssertionError(f"job {job_id} did not finish in time")


def test_get_assistant_job_unknown_id_returns_none() -> None:
    assert assistant.get_assistant_job("does-not-exist") is None


def test_start_chat_job_reports_done_with_result(
    project_repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        assistant, "generate_completion_with_notice", lambda messages: ('{"reply": "ok", "proposals": []}', None)
    )

    job_id = assistant.start_chat_job(
        _PROJECT_ID,
        [AssistantChatMessage(role="user", content="hi")],
        main_file="main.tex",
    )
    job = _wait_for_job_done(job_id)

    assert job["status"] == "done"
    assert job["result"].reply == "ok"
    assert job["error"] is None


def test_start_chat_job_reports_error_with_type(
    project_repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_generate_completion_with_notice(messages):
        raise ConnectionError("provider unreachable")

    monkeypatch.setattr(
        assistant, "generate_completion_with_notice", fake_generate_completion_with_notice
    )

    job_id = assistant.start_chat_job(
        _PROJECT_ID,
        [AssistantChatMessage(role="user", content="hi")],
        main_file="main.tex",
    )
    job = _wait_for_job_done(job_id)

    assert job["status"] == "error"
    assert job["error_type"] == "ConnectionError"
    assert "provider unreachable" in job["error"]


def test_start_fix_compile_error_job_reports_done(
    project_repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        assistant, "generate_completion_with_notice", lambda messages: ('{"reply": "fixed it", "proposals": []}', None)
    )

    job_id = assistant.start_fix_compile_error_job(
        _PROJECT_ID, "! Undefined control sequence", "main.tex"
    )
    job = _wait_for_job_done(job_id)

    assert job["status"] == "done"
    assert job["result"].suggestion == "fixed it"


def test_router_chat_job_roundtrip_success(
    project_repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        assistant, "generate_completion_with_notice", lambda messages: ('{"reply": "ok", "proposals": []}', None)
    )

    client = _make_client()
    start_response = client.post(
        f"/api/projects/{_PROJECT_ID}/assistant/chat/start",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert start_response.status_code == 200
    job_id = start_response.json()["job_id"]

    import time

    poll_response = None
    for _ in range(50):
        poll_response = client.get(f"/api/assistant/jobs/chat/{job_id}")
        if poll_response.json()["status"] == "done":
            break
        time.sleep(0.05)

    assert poll_response is not None
    assert poll_response.status_code == 200
    assert poll_response.json()["result"]["reply"] == "ok"


def test_router_chat_job_poll_maps_connection_error_to_503(
    project_repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_generate_completion_with_notice(messages):
        raise ConnectionError("provider unreachable")

    monkeypatch.setattr(
        assistant, "generate_completion_with_notice", fake_generate_completion_with_notice
    )

    client = _make_client()
    start_response = client.post(
        f"/api/projects/{_PROJECT_ID}/assistant/chat/start",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    job_id = start_response.json()["job_id"]

    import time

    poll_response = None
    for _ in range(50):
        poll_response = client.get(f"/api/assistant/jobs/chat/{job_id}")
        if poll_response.status_code != 200 or poll_response.json().get("status") == "done":
            break
        time.sleep(0.05)

    assert poll_response is not None
    assert poll_response.status_code == 503


def test_router_poll_unknown_job_id_returns_404() -> None:
    client = _make_client()
    response = client.get("/api/assistant/jobs/chat/does-not-exist")
    assert response.status_code == 404

    response = client.get("/api/assistant/jobs/fix-compile-error/does-not-exist")
    assert response.status_code == 404


def test_router_fix_compile_error_job_roundtrip_success(
    project_repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        assistant, "generate_completion_with_notice", lambda messages: ('{"reply": "fixed it", "proposals": []}', None)
    )

    client = _make_client()
    start_response = client.post(
        f"/api/projects/{_PROJECT_ID}/assistant/fix-compile-error/start",
        json={"log": "! error", "main_file": "main.tex"},
    )
    assert start_response.status_code == 200
    job_id = start_response.json()["job_id"]

    import time

    poll_response = None
    for _ in range(50):
        poll_response = client.get(f"/api/assistant/jobs/fix-compile-error/{job_id}")
        if poll_response.json()["status"] == "done":
            break
        time.sleep(0.05)

    assert poll_response is not None
    assert poll_response.status_code == 200
    assert poll_response.json()["result"]["suggestion"] == "fixed it"


# ─── Router (HTTP-level status code mapping) ───


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(assistant_router.router)
    return TestClient(app)


def test_router_get_and_put_config_roundtrip() -> None:
    client = _make_client()

    get_response = client.get("/api/assistant/config")
    assert get_response.status_code == 200
    assert get_response.json()["provider"] == "local"

    put_response = client.put(
        "/api/assistant/config",
        json={"provider": "external", "external_model": "gpt-4o"},
    )
    assert put_response.status_code == 200
    assert put_response.json()["external_model"] == "gpt-4o"
    assert "external_api_key" not in put_response.json()


def test_router_put_config_invalid_provider_returns_400() -> None:
    client = _make_client()

    response = client.put("/api/assistant/config", json={"provider": "bogus"})
    assert response.status_code == 400


def test_router_chat_maps_connection_error_to_503(
    project_repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_chat(project_id: str, messages: object, main_file: object) -> None:
        raise ConnectionError("provider unreachable")

    monkeypatch.setattr(assistant, "chat", fake_chat)

    client = _make_client()
    response = client.post(
        f"/api/projects/{_PROJECT_ID}/assistant/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 503


def test_router_chat_maps_unknown_project_to_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_chat(project_id: str, messages: object, main_file: object) -> None:
        raise FileNotFoundError(f"project not found: {project_id!r}")

    monkeypatch.setattr(assistant, "chat", fake_chat)

    client = _make_client()
    response = client.post(
        "/api/projects/does-not-exist/assistant/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 404


def test_router_apply_success(
    project_repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        assistant, "apply_proposal", lambda project_id, proposal, author: "abc123"
    )

    client = _make_client()
    response = client.post(
        f"/api/projects/{_PROJECT_ID}/assistant/apply",
        json={
            "proposal": {
                "id": "p1",
                "action": "write_file",
                "path": "main.tex",
                "content": "x",
                "description": "test",
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["commit_id"] == "abc123"


def test_router_apply_rejects_bad_proposal(
    project_repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_apply(project_id: str, proposal: object, author: str) -> str:
        raise ValueError("unknown proposal action")

    monkeypatch.setattr(assistant, "apply_proposal", fake_apply)

    client = _make_client()
    response = client.post(
        f"/api/projects/{_PROJECT_ID}/assistant/apply",
        json={
            "proposal": {
                "id": "p1",
                "action": "write_file",
                "path": "main.tex",
                "description": "test",
            }
        },
    )

    assert response.status_code == 400


# ─── Model pull progress ───


def test_get_pull_status_idle_when_never_started() -> None:
    status = assistant.get_pull_status("never-requested:1b")
    assert status.status == "idle"
    assert status.done is False


def test_ensure_ollama_model_reports_streaming_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "httpx.stream",
        _fake_pull_stream(
            [
                '{"status": "pulling manifest"}',
                '{"status": "downloading", "completed": 50, "total": 100}',
                '{"status": "success"}',
            ]
        ),
    )

    assistant._ensure_ollama_model("qwen2.5-coder:3b", "http://ollama:11434")

    status = assistant.get_pull_status("qwen2.5-coder:3b")
    assert status.done is True
    assert status.error is None
    assert status.status == "success"


def test_ensure_ollama_model_records_error_status_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_stream(method: str, url: str, json: dict[str, Any], timeout: float) -> _FakeStreamResponse:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("httpx.stream", fake_stream)

    with pytest.raises(ConnectionError):
        assistant._ensure_ollama_model("qwen2.5-coder:3b", "http://ollama:11434")

    status = assistant.get_pull_status("qwen2.5-coder:3b")
    assert status.done is True
    assert status.error is not None


def test_start_model_pull_is_idempotent_while_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    started = threading.Event()
    release = threading.Event()
    call_count = 0

    def fake_stream(method: str, url: str, json: dict[str, Any], timeout: float) -> _FakeStreamResponse:
        nonlocal call_count
        call_count += 1
        started.set()
        release.wait(timeout=5)
        return _FakeStreamResponse(['{"status": "success"}'])

    monkeypatch.setattr("httpx.stream", fake_stream)

    assistant.start_model_pull("slow-model:1b")
    started.wait(timeout=5)
    assistant.start_model_pull("slow-model:1b")  # should be a no-op, still in flight

    release.set()
    import time

    for _ in range(50):
        if assistant.get_pull_status("slow-model:1b").done:
            break
        time.sleep(0.05)

    assert call_count == 1


def test_router_pull_model_and_status_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "httpx.stream", _fake_pull_stream(['{"status": "success"}'])
    )

    client = _make_client()
    start_response = client.post(
        "/api/assistant/models/pull", json={"model": "qwen2.5-coder:3b"}
    )
    assert start_response.status_code == 200

    import time

    for _ in range(50):
        status_response = client.get(
            "/api/assistant/models/pull-status", params={"model": "qwen2.5-coder:3b"}
        )
        if status_response.json()["done"]:
            break
        time.sleep(0.05)

    assert status_response.json()["done"] is True
    assert status_response.json()["error"] is None


# ─── Fallback providers ───


def test_add_and_remove_fallback_profile_roundtrip() -> None:
    cfg = assistant.add_fallback_profile(
        ExternalProviderProfileCreate(
            label="DeepSeek backup",
            provider_kind="openai",
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
            api_key="sk-backup",
        )
    )
    assert len(cfg.fallback_profiles) == 1
    profile = cfg.fallback_profiles[0]
    assert profile.label == "DeepSeek backup"
    assert profile.has_api_key is True

    cfg = assistant.remove_fallback_profile(profile.id)
    assert cfg.fallback_profiles == []


def test_remove_fallback_profile_unknown_id_raises() -> None:
    with pytest.raises(FileNotFoundError):
        assistant.remove_fallback_profile("does-not-exist")


def test_add_fallback_profile_rejects_invalid_provider_kind() -> None:
    with pytest.raises(ValueError):
        assistant.add_fallback_profile(
            ExternalProviderProfileCreate(
                label="bad", provider_kind="bogus", model="x", api_key="sk-x"
            )
        )


def test_router_fallback_profile_crud() -> None:
    client = _make_client()

    create_response = client.post(
        "/api/assistant/fallback-profiles",
        json={
            "label": "Kimi backup",
            "provider_kind": "openai",
            "base_url": "https://api.moonshot.ai/v1",
            "model": "moonshot-v1-8k",
            "api_key": "sk-kimi",
        },
    )
    assert create_response.status_code == 200
    profiles = create_response.json()["fallback_profiles"]
    assert len(profiles) == 1
    assert "api_key" not in profiles[0]

    delete_response = client.delete(
        f"/api/assistant/fallback-profiles/{profiles[0]['id']}"
    )
    assert delete_response.status_code == 200
    assert delete_response.json()["fallback_profiles"] == []

    missing_response = client.delete("/api/assistant/fallback-profiles/does-not-exist")
    assert missing_response.status_code == 404


def test_router_list_models_local_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    assistant.update_config(AssistantConfigUpdate(local_model=""))

    def fake_get(url: str, timeout: float) -> _FakeResponse:
        return _FakeResponse({"models": [{"name": "llama3.2:1b"}, {"name": "mistral"}]})

    monkeypatch.setattr("httpx.get", fake_get)

    client = _make_client()
    response = client.get("/api/assistant/models")

    assert response.status_code == 200
    ids = {entry["id"] for entry in response.json()}
    assert ids == {"llama3.2:1b", "mistral"}


def test_router_list_models_includes_configured_model_not_yet_pulled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assistant.update_config(AssistantConfigUpdate(local_model="qwen2.5-coder:3b"))

    def fake_get(url: str, timeout: float) -> _FakeResponse:
        return _FakeResponse({"models": [{"name": "llama3.2:1b"}]})

    monkeypatch.setattr("httpx.get", fake_get)

    client = _make_client()
    response = client.get("/api/assistant/models")

    ids = {entry["id"] for entry in response.json()}
    # The configured model is always listed, even if Ollama hasn't
    # reported it as already pulled (so it's still selectable/pullable).
    assert ids == {"llama3.2:1b", "qwen2.5-coder:3b"}


def _configure_primary_and_backup() -> None:
    assistant.update_config(
        AssistantConfigUpdate(
            provider="external",
            external_provider_kind="openai",
            external_model="gpt-4o-mini",
            external_api_key="sk-primary",
        )
    )
    assistant.add_fallback_profile(
        ExternalProviderProfileCreate(
            label="backup",
            provider_kind="openai",
            model="deepseek-chat",
            api_key="sk-backup",
            base_url="https://api.deepseek.com",
        )
    )


def test_external_503_is_retried_then_succeeds_without_failover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_primary_and_backup()
    monkeypatch.setattr(assistant.time, "sleep", lambda _s: None)
    calls: list[str] = []

    def fake_post(url: str, headers: dict[str, str], json: dict[str, Any], timeout: float) -> _FakeResponse:
        calls.append(headers["Authorization"])
        if len(calls) < 3:
            return _FakeResponse({}, status_code=503, reason_phrase="Service Unavailable")
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("httpx.post", fake_post)

    reply, notice = assistant.generate_completion_with_notice(
        [{"role": "user", "content": "hi"}]
    )

    assert reply == "ok" and notice is None
    assert calls == ["Bearer sk-primary"] * 3


def test_external_persistent_503_fails_over_to_backup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_primary_and_backup()
    monkeypatch.setattr(assistant.time, "sleep", lambda _s: None)

    def fake_post(url: str, headers: dict[str, str], json: dict[str, Any], timeout: float) -> _FakeResponse:
        if headers["Authorization"] == "Bearer sk-primary":
            return _FakeResponse({}, status_code=503, reason_phrase="Service Unavailable")
        return _FakeResponse({"choices": [{"message": {"content": "from backup"}}]})

    monkeypatch.setattr("httpx.post", fake_post)

    reply, notice = assistant.generate_completion_with_notice(
        [{"role": "user", "content": "hi"}]
    )

    assert reply == "from backup"
    assert notice is not None


def test_placeholder_fallback_model_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assistant.update_config(
        AssistantConfigUpdate(
            provider="external",
            external_provider_kind="openai",
            external_model="gpt-4o-mini",
            external_api_key="sk-primary",
        )
    )
    assistant.add_fallback_profile(
        ExternalProviderProfileCreate(
            label="nim", provider_kind="openai", model="REPLACE_ME", api_key="k"
        )
    )
    monkeypatch.setattr(assistant.time, "sleep", lambda _s: None)
    urls: list[str] = []

    def fake_post(url: str, headers: dict[str, str], json: dict[str, Any], timeout: float) -> _FakeResponse:
        urls.append(json["model"])
        return _FakeResponse({}, status_code=503, reason_phrase="Service Unavailable")

    monkeypatch.setattr("httpx.post", fake_post)

    with pytest.raises(ConnectionError, match="503"):
        assistant.generate_completion([{"role": "user", "content": "hi"}])

    assert "REPLACE_ME" not in urls
