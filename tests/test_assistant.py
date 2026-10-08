"""Tests for the AI assistant service: config, provider dispatch, the
JSON proposal protocol, and the propose/apply flow.

Provider HTTP calls are monkeypatched (``httpx.get``/``httpx.post``)
rather than hitting a real Ollama server or external API -- this suite
never makes a network call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import git
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from webapp import config
from webapp.models import (
    AssistantChatMessage,
    AssistantConfigUpdate,
    AssistantProposal,
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
    def __init__(self, json_data: dict[str, Any], status_code: int = 200) -> None:
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                "error", request=None, response=None  # type: ignore[arg-type]
            )

    def json(self) -> dict[str, Any]:
        return self._json_data


# ─── Config ───


def test_get_config_defaults() -> None:
    cfg = assistant.get_config()
    assert cfg.provider == "local"
    assert cfg.local_model == "llama3.2:1b"
    assert cfg.has_external_api_key is False
    assert cfg.writing_style_profile is None


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


def test_generate_completion_local_pulls_missing_model_then_chats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_get(url: str, timeout: float) -> _FakeResponse:
        calls.append(f"GET {url}")
        return _FakeResponse({"models": []})

    def fake_post(url: str, json: dict[str, Any], timeout: float, **_: Any) -> _FakeResponse:
        calls.append(f"POST {url}")
        if url.endswith("/api/pull"):
            return _FakeResponse({})
        return _FakeResponse({"message": {"content": "hello from ollama"}})

    monkeypatch.setattr("httpx.get", fake_get)
    monkeypatch.setattr("httpx.post", fake_post)

    reply = assistant.generate_completion([{"role": "user", "content": "hi"}])

    assert reply == "hello from ollama"
    assert any("/api/pull" in call for call in calls)
    assert any("/api/chat" in call for call in calls)


def test_generate_completion_local_skips_pull_when_model_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, timeout: float) -> _FakeResponse:
        return _FakeResponse({"models": [{"name": "llama3.2:1b"}]})

    pull_called = False

    def fake_post(url: str, json: dict[str, Any], timeout: float, **_: Any) -> _FakeResponse:
        nonlocal pull_called
        if url.endswith("/api/pull"):
            pull_called = True
        return _FakeResponse({"message": {"content": "ok"}})

    monkeypatch.setattr("httpx.get", fake_get)
    monkeypatch.setattr("httpx.post", fake_post)

    assistant.generate_completion([{"role": "user", "content": "hi"}])
    assert pull_called is False


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
    assert proposals[0].path == "main.tex"


# ─── Chat / suggest / fix-compile-error ───


def test_chat_includes_main_file_content(
    project_repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_messages: list[dict[str, str]] = []

    def fake_generate_completion(messages: list[dict[str, str]]) -> str:
        captured_messages.extend(messages)
        return '{"reply": "ok", "proposals": []}'

    monkeypatch.setattr(assistant, "generate_completion", fake_generate_completion)

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
    def fake_generate_completion(messages: list[dict[str, str]]) -> str:
        return (
            '{"reply": "found one thing", "proposals": '
            '[{"action": "write_file", "path": "main.tex", '
            '"content": "better", "description": "fix typo"}]}'
        )

    monkeypatch.setattr(assistant, "generate_completion", fake_generate_completion)

    result = assistant.suggest_improvements(_PROJECT_ID, "main.tex")

    assert result.reply == "found one thing"
    assert len(result.proposals) == 1
    assert result.proposals[0].path == "main.tex"


def test_fix_compile_error_returns_single_proposal(
    project_repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_generate_completion(messages: list[dict[str, str]]) -> str:
        return (
            '{"reply": "missing a brace", "proposals": '
            '[{"action": "write_file", "path": "main.tex", '
            '"content": "fixed", "description": "add missing brace"}]}'
        )

    monkeypatch.setattr(assistant, "generate_completion", fake_generate_completion)

    result = assistant.fix_compile_error(_PROJECT_ID, "! Undefined control sequence", "main.tex")

    assert result.suggestion == "missing a brace"
    assert result.proposal is not None
    assert result.proposal.description == "add missing brace"


def test_learn_writing_style_persists_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assistant, "generate_completion", lambda messages: "  terse, formal  ")

    profile = assistant.learn_writing_style(["sample one", "sample two"])

    assert profile == "terse, formal"
    assert assistant.get_config().writing_style_profile == "terse, formal"


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


def test_router_list_models_local_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, timeout: float) -> _FakeResponse:
        return _FakeResponse({"models": [{"name": "llama3.2:1b"}, {"name": "mistral"}]})

    monkeypatch.setattr("httpx.get", fake_get)

    client = _make_client()
    response = client.get("/api/assistant/models")

    assert response.status_code == 200
    ids = {entry["id"] for entry in response.json()}
    assert ids == {"llama3.2:1b", "mistral"}
