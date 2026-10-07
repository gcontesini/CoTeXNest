"""LaTeX compilation service.

Compiles a project's main ``.tex`` file to PDF using ``latexmk`` rather
than invoking ``pdflatex``/``bibtex`` directly. ``latexmk`` inspects the
``.log``/``.fls`` output after each pass and automatically reruns the
engine (and ``bibtex``/``biber`` when a bibliography changed) exactly as
many times as needed to resolve cross-references, citations, and the
table of contents. Driving ``pdflatex`` by hand would require this repo
to reimplement that fixed-point convergence loop itself.

The other key mechanism here is ``TEXINPUTS``. Packages and templates
installed through the package manager live under the shared,
Docker-mounted ``config.PLUGINS_ROOT`` directory, not inside any single
project. Setting ``TEXINPUTS`` to search ``PLUGINS_ROOT`` (recursively,
via a trailing ``//``), then the project directory itself, then falling
back to the engine's compiled-in default path (the trailing empty
segment), lets every project resolve a shared ``.cls``/``.sty``/image
asset without that asset ever being copied into the project's git
history.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from pathlib import Path

from .. import config
from ..models import CompileResult

_LOG_TAIL_CHARS: int = 20_000


def _validate_main_file(main_file_: str) -> str:
    """Reject anything that isn't a plain, in-project ``.tex`` filename.

    ``main_file_`` ultimately becomes an argv element passed to
    ``latexmk``. Without this check a value like ``"-e;system('...')"``
    (``latexmk`` supports an ``-e <perl code>`` flag) or a path escaping
    the project directory (``"../../etc/passwd"``) would be accepted as
    a compile target instead of being rejected as invalid input.

    Args:
        main_file_ (str): Candidate filename, from a request body or
            ``.project.json``.

    Returns:
        str: The same filename, once confirmed safe.

    Raises:
        ValueError: If the filename is absolute, escapes the project
            directory, looks like a flag, or isn't a ``.tex`` file.
    """
    if (
        not main_file_
        or main_file_.startswith("-")
        or Path(main_file_).is_absolute()
        or ".." in Path(main_file_).parts
        or not main_file_.endswith(".tex")
    ):
        raise ValueError(f"invalid main_file: {main_file_!r}")

    return main_file_


def _resolve_main_file(project_dir_: Path, main_file_: str | None) -> str:
    """Determine which ``.tex`` file to compile for a project.

    Uses ``main_file_`` when given. Otherwise reads the ``main_file``
    key out of the project's ``.project.json`` manifest, defaulting to
    ``"main.tex"`` whenever that file or key is missing or malformed.
    Either way, the result passes through ``_validate_main_file`` before
    ever reaching the ``latexmk`` argv.

    Args:
        project_dir_ (Path): Resolved project working directory.
        main_file_ (str | None): Explicit override, if provided.

    Returns:
        str: Relative filename of the ``.tex`` file to compile.

    Raises:
        ValueError: If the resolved filename fails validation.
    """
    if main_file_:
        return _validate_main_file(main_file_)

    manifest_path: Path = project_dir_ / ".project.json"
    resolved: str = config.DEFAULT_MAIN_FILE

    try:
        manifest_text: str = manifest_path.read_text(encoding="utf-8")
        manifest: dict[str, object] = json.loads(manifest_text)
        candidate: object = manifest.get("main_file", config.DEFAULT_MAIN_FILE)
        if isinstance(candidate, str) and candidate:
            resolved = candidate
    except (OSError, ValueError, AttributeError):
        pass

    return _validate_main_file(resolved)


def _build_texinputs(project_dir_: Path) -> str:
    """Build the ``TEXINPUTS`` value exposing shared plugins to a project.

    Args:
        project_dir_ (Path): Resolved project working directory.

    Returns:
        str: ``TEXINPUTS`` string combining the plugins root, the
        project directory, and the engine's own default path.
    """
    return f"{config.PLUGINS_ROOT}//:{project_dir_}//:"


def compile_project(
    project_id: str,
    main_file: str | None = None,
) -> CompileResult:
    """Compile a project's LaTeX sources to PDF via ``latexmk``.

    Args:
        project_id (str): Identifier of the project under
            ``config.WORKSPACE_ROOT``.
        main_file (str | None): Entry ``.tex`` file to compile. When
            ``None``, resolved from the project's ``.project.json``.

    Returns:
        CompileResult: Outcome of the compile attempt. ``success`` is
        ``False`` (never an exception) for ordinary LaTeX compile
        errors, timeouts, or a missing ``latexmk`` binary.

    Raises:
        FileNotFoundError: If the project directory does not exist.
        ValueError: If the resolved ``main_file`` is not a plain,
            in-project ``.tex`` filename (see ``_validate_main_file``).
    """
    assert project_id, "project_id must be a non-empty string"

    project_dir: Path = config.project_dir(project_id)

    if not project_dir.is_dir():
        raise FileNotFoundError(f"project not found: {project_id!r}")

    resolved_main_file: str = _resolve_main_file(project_dir, main_file)

    pdf_name: str = Path(resolved_main_file).stem + ".pdf"
    expected_pdf_path: Path = project_dir / pdf_name

    compile_env: dict[str, str] = os.environ.copy()
    compile_env["TEXINPUTS"] = _build_texinputs(project_dir)

    command: list[str] = [
        "latexmk",
        "-pdf",
        "-interaction=nonstopmode",
        "-halt-on-error",
        f"-output-directory={project_dir}",
        resolved_main_file,
    ]
    # NOTE: do not insert a "--" end-of-options marker here. latexmk has
    # no such concept — per `latexmk -help`, "All options can be
    # introduced by '-' or '--'" — so a literal "--" argv element is
    # parsed as an unknown option and the run fails immediately
    # ("Latexmk: Bad options specified ... Latexmk: - unknown option").
    # Argument-injection protection against a flag-like main_file is
    # handled above, in `_validate_main_file` (rejects values starting
    # with "-"), not via an argv separator.

    start_time: float = time.monotonic()

    try:
        completed: subprocess.CompletedProcess[str] = subprocess.run(
            command,
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=config.COMPILE_TIMEOUT_SECONDS,
            env=compile_env,
            check=False,  # non-zero exit is a normal, inspected outcome below
        )
    except subprocess.TimeoutExpired as timeout_error:
        duration_ms: int = int((time.monotonic() - start_time) * 1000.0)
        partial_log: str = (
            (timeout_error.stdout or "") + (timeout_error.stderr or "")
        )
        timeout_message: str = (
            f"compilation timed out after "
            f"{config.COMPILE_TIMEOUT_SECONDS}s"
        )
        combined_log: str = (partial_log + "\n" + timeout_message).strip()
        return CompileResult(
            success=False,
            log=combined_log[-_LOG_TAIL_CHARS:],
            pdf_base64=None,
            duration_ms=duration_ms,
        )
    except FileNotFoundError:
        duration_ms = int((time.monotonic() - start_time) * 1000.0)
        return CompileResult(
            success=False,
            log="latexmk not found — is TeX Live installed?",
            pdf_base64=None,
            duration_ms=duration_ms,
        )

    duration_ms = int((time.monotonic() - start_time) * 1000.0)
    combined_log = (completed.stdout + completed.stderr)[-_LOG_TAIL_CHARS:]

    if completed.returncode == 0 and expected_pdf_path.is_file():
        pdf_bytes: bytes = expected_pdf_path.read_bytes()
        pdf_base64: str = base64.b64encode(pdf_bytes).decode("ascii")
        result: CompileResult = CompileResult(
            success=True,
            log=combined_log,
            pdf_base64=pdf_base64,
            duration_ms=duration_ms,
        )
        assert result.pdf_base64 is not None, "success result must carry a pdf"
        return result

    return CompileResult(
        success=False,
        log=combined_log,
        pdf_base64=None,
        duration_ms=duration_ms,
    )
