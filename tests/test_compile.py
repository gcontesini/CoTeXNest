"""Tests for the LaTeX compilation service and its router."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from webapp import config
from webapp.models import CompileResult
from webapp.routers import compile as compile_router
from webapp.services import latex_render

_LATEXMK_MISSING: bool = shutil.which("latexmk") is None

_VALID_TEX: str = r"""
\documentclass{article}
\begin{document}
Hello, world.
\end{document}
"""

_BROKEN_TEX: str = r"""
\documentclass{article}
\begin{document}
\undefinedcommandthatdoesnotexist
Hello, world.
"""


def _write_project(
    workspace_root_: Path,
    project_id_: str,
    tex_body_: str,
    main_file_: str = "main.tex",
) -> Path:
    """Create a minimal on-disk project with one ``.tex`` file.

    Args:
        workspace_root_ (Path): Root directory standing in for
            ``config.WORKSPACE_ROOT``.
        project_id_ (str): Identifier/subdirectory name for the
            project.
        tex_body_ (str): Contents written to the main ``.tex`` file.
        main_file_ (str): Filename of the main ``.tex`` file.

    Returns:
        Path: Path to the created project directory.
    """
    project_dir: Path = workspace_root_ / project_id_
    project_dir.mkdir(parents=True, exist_ok=True)

    ( project_dir / main_file_ ).write_text(tex_body_, encoding="utf-8")
    ( project_dir / ".project.json" ).write_text(
        json.dumps({"main_file": main_file_}),
        encoding="utf-8",
    )

    return project_dir


@pytest.fixture
def workspace_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Point ``config.WORKSPACE_ROOT`` at an isolated temp directory."""
    root: Path = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", root)
    yield root


@pytest.fixture
def api_client() -> TestClient:
    """Build a minimal FastAPI app wrapping only the compile router."""
    app: FastAPI = FastAPI()
    app.include_router(compile_router.router)
    return TestClient(app)


@pytest.mark.skipif(_LATEXMK_MISSING, reason="latexmk not installed")
def test_compile_project_success(workspace_root: Path) -> None:
    """A valid minimal project compiles and returns a PDF payload."""
    _write_project(workspace_root, "proj-ok", _VALID_TEX)

    result: CompileResult = latex_render.compile_project("proj-ok")

    assert result.success is True
    assert result.pdf_base64 is not None
    assert result.duration_ms >= 0
    assert (workspace_root / "proj-ok" / "main.pdf").is_file()


@pytest.mark.skipif(_LATEXMK_MISSING, reason="latexmk not installed")
def test_compile_project_broken_tex_fails_cleanly(
    workspace_root: Path,
) -> None:
    """A broken ``.tex`` file yields success=False with a log, no raise."""
    _write_project(workspace_root, "proj-broken", _BROKEN_TEX)

    result: CompileResult = latex_render.compile_project("proj-broken")

    assert result.success is False
    assert result.pdf_base64 is None
    assert len(result.log) > 0


def test_compile_project_missing_project_raises(
    workspace_root: Path,
) -> None:
    """An unknown project id raises FileNotFoundError at the service layer."""
    with pytest.raises(FileNotFoundError):
        latex_render.compile_project("does-not-exist")


def test_compile_router_missing_project_returns_404(
    workspace_root: Path,
    api_client: TestClient,
) -> None:
    """The router translates a missing project into an HTTP 404."""
    response = api_client.post(
        "/api/projects/does-not-exist/compile",
        json={"main_file": None},
    )

    assert response.status_code == 404


def test_compile_router_success_is_200(
    workspace_root: Path,
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed *compile* still comes back as HTTP 200, not an error."""
    _write_project(workspace_root, "proj-fake", _VALID_TEX)

    def _fake_compile_project(
        project_id: str,
        main_file: str | None = None,
    ) -> CompileResult:
        return CompileResult(
            success=False,
            log="simulated failure",
            pdf_base64=None,
            duration_ms=1,
        )

    monkeypatch.setattr(
        compile_router.latex_render,
        "compile_project",
        _fake_compile_project,
    )

    response = api_client.post(
        "/api/projects/proj-fake/compile",
        json={"main_file": None},
    )

    assert response.status_code == 200
    body: dict[str, object] = response.json()
    assert body["success"] is False
    assert body["log"] == "simulated failure"


def test_compile_project_missing_latexmk_binary(
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing ``latexmk`` binary yields success=False, not a raise."""
    _write_project(workspace_root, "proj-no-binary", _VALID_TEX)

    def _raise_file_not_found(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("latexmk")

    monkeypatch.setattr(subprocess, "run", _raise_file_not_found)

    result: CompileResult = latex_render.compile_project("proj-no-binary")

    assert result.success is False
    assert result.pdf_base64 is None
    assert "latexmk" in result.log.lower()


def test_compile_project_timeout_fails_cleanly(
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``latexmk`` run exceeding the timeout yields success=False."""
    _write_project(workspace_root, "proj-timeout", _VALID_TEX)

    def _raise_timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(
            cmd="latexmk",
            timeout=config.COMPILE_TIMEOUT_SECONDS,
            output="partial stdout",
            stderr="partial stderr",
        )

    monkeypatch.setattr(subprocess, "run", _raise_timeout)

    result: CompileResult = latex_render.compile_project("proj-timeout")

    assert result.success is False
    assert result.pdf_base64 is None
    assert "timed out" in result.log.lower()
    assert "partial stdout" in result.log


def test_resolve_main_file_defaults_when_manifest_malformed(
    workspace_root: Path,
) -> None:
    """A malformed ``.project.json`` falls back to the default main file."""
    project_dir: Path = workspace_root / "proj-malformed"
    project_dir.mkdir()
    ( project_dir / ".project.json" ).write_text(
        "not valid json {{{",
        encoding="utf-8",
    )

    resolved: str = latex_render._resolve_main_file(project_dir, None)

    assert resolved == config.DEFAULT_MAIN_FILE


def test_resolve_main_file_defaults_when_manifest_missing(
    workspace_root: Path,
) -> None:
    """No ``.project.json`` at all also falls back to the default."""
    project_dir: Path = workspace_root / "proj-no-manifest"
    project_dir.mkdir()

    resolved: str = latex_render._resolve_main_file(project_dir, None)

    assert resolved == config.DEFAULT_MAIN_FILE


def test_resolve_main_file_prefers_explicit_override(
    workspace_root: Path,
) -> None:
    """An explicit ``main_file`` argument wins over the manifest."""
    project_dir: Path = workspace_root / "proj-override"
    project_dir.mkdir()
    ( project_dir / ".project.json" ).write_text(
        json.dumps({"main_file": "manifest.tex"}),
        encoding="utf-8",
    )

    resolved: str = latex_render._resolve_main_file(project_dir, "explicit.tex")

    assert resolved == "explicit.tex"


@pytest.mark.parametrize(
    "malicious_main_file",
    [
        "-e;system('id')",
        "-interaction=batchmode",
        "../../etc/passwd.tex",
        "/etc/passwd.tex",
        "no-extension",
    ],
)
def test_compile_project_rejects_flag_like_or_escaping_main_file(
    workspace_root: Path,
    malicious_main_file: str,
) -> None:
    """A ``main_file`` that could be smuggled in as an argv flag is rejected.

    Covers the argument-injection path: ``latexmk`` accepts an
    ``-e <perl code>`` flag, so a ``main_file`` value starting with
    ``-`` must never reach its argv unvalidated.
    """
    _write_project(workspace_root, "proj-injection", _VALID_TEX)

    with pytest.raises(ValueError):
        latex_render.compile_project("proj-injection", malicious_main_file)


def test_compile_router_rejects_flag_like_main_file_with_400(
    workspace_root: Path,
    api_client: TestClient,
) -> None:
    """The router translates an invalid ``main_file`` into an HTTP 400."""
    _write_project(workspace_root, "proj-injection-http", _VALID_TEX)

    response = api_client.post(
        "/api/projects/proj-injection-http/compile",
        json={"main_file": "-e;system('id')"},
    )

    assert response.status_code == 400
