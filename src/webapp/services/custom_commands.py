"""Global custom LaTeX commands and their per-project loader line.

The commands are authored once (Settings -> Custom Commands) and stored
in ``config.CUSTOM_COMMANDS_PATH``. A project opts in by having a loader
line in its main file's preamble::

    \\input{custom-commands.tex} % latex-studio:custom-commands

Whether the line is present *is* the on/off state, so toggling it by
hand in the editor and toggling it from Settings are the same thing.
Enabling also writes ``custom-commands.tex`` into the project (a copy of
the global text) so the project still compiles for collaborators who
clone it without this app's volume. The commands are never validated:
an invalid command is the author's responsibility and surfaces as a
normal compile error.
"""

from __future__ import annotations

import json
import logging

from .. import config
from . import project_files

logger = logging.getLogger(__name__)

PROJECT_COMMANDS_FILENAME = "custom-commands.tex"
LOADER_MARKER = "% latex-studio:custom-commands"
LOADER_LINE = f"\\input{{{PROJECT_COMMANDS_FILENAME}}} {LOADER_MARKER}"
_BEGIN_DOCUMENT = "\\begin{document}"
_METADATA_FILENAME = ".project.json"


def read_global_commands() -> str:
    """Return the saved global commands, or "" if none were saved yet."""
    try:
        return config.CUSTOM_COMMANDS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _main_file_of(project_id: str) -> str:
    project_root = project_files.get_project_dir(project_id)
    metadata_path = project_root / _METADATA_FILENAME

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return config.DEFAULT_MAIN_FILE

    return str(metadata.get("main_file", config.DEFAULT_MAIN_FILE))


def _has_loader_line(text: str) -> bool:
    return any(LOADER_MARKER in line for line in text.splitlines())


def _without_loader_line(text: str) -> str:
    kept = [
        line
        for line in text.splitlines(keepends=True)
        if LOADER_MARKER not in line
    ]
    return "".join(kept)


def _with_loader_line(text: str) -> str:
    lines = text.splitlines(keepends=True)

    for index, line in enumerate(lines):
        if line.strip().startswith(_BEGIN_DOCUMENT):
            lines.insert(index, LOADER_LINE + "\n")
            return "".join(lines)

    raise ValueError(
        f"main file has no {_BEGIN_DOCUMENT} line to load the commands before"
    )


def get_project_status(project_id: str) -> tuple[bool, str]:
    """Report whether a project's main file loads the custom commands.

    Returns:
        tuple[bool, str]: ``(enabled, main_file)``.

    Raises:
        FileNotFoundError: If the project or its main file is missing.
    """
    main_file = _main_file_of(project_id)
    text = project_files.read_file(project_id, main_file)
    return _has_loader_line(text), main_file


def set_project_enabled(project_id: str, enabled: bool) -> tuple[bool, str]:
    """Add or remove the loader line (and the project's copy of the file).

    Idempotent: enabling twice leaves a single loader line.

    Raises:
        FileNotFoundError: If the project or its main file is missing.
        ValueError: If enabling and the main file has no
            ``\\begin{document}`` line.
    """
    main_file = _main_file_of(project_id)
    text = project_files.read_file(project_id, main_file)

    if enabled:
        project_files.write_file(
            project_id, PROJECT_COMMANDS_FILENAME, read_global_commands()
        )

        if not _has_loader_line(text):
            project_files.write_file(
                project_id, main_file, _with_loader_line(text)
            )
    else:
        if _has_loader_line(text):
            project_files.write_file(
                project_id, main_file, _without_loader_line(text)
            )

        commands_file = (
            project_files.get_project_dir(project_id)
            / PROJECT_COMMANDS_FILENAME
        )
        commands_file.unlink(missing_ok=True)

    return enabled, main_file


def _enabled_project_ids() -> list[str]:
    if not config.WORKSPACE_ROOT.is_dir():
        return []

    enabled_ids: list[str] = []

    for entry in sorted(config.WORKSPACE_ROOT.iterdir()):
        if not (entry / _METADATA_FILENAME).is_file():
            continue

        try:
            enabled, _ = get_project_status(entry.name)
        except (OSError, ValueError):
            continue

        if enabled:
            enabled_ids.append(entry.name)

    return enabled_ids


def save_global_commands(content: str) -> list[str]:
    """Persist the global commands and refresh every opted-in project.

    Args:
        content (str): The full command text, stored verbatim.

    Returns:
        list[str]: Ids of the projects whose copy was refreshed.
    """
    config.CUSTOM_COMMANDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.CUSTOM_COMMANDS_PATH.write_text(content, encoding="utf-8")

    refreshed: list[str] = []

    for project_id in _enabled_project_ids():
        try:
            project_files.write_file(
                project_id, PROJECT_COMMANDS_FILENAME, content
            )
        except OSError as error:
            logger.warning(
                "could not refresh custom commands in %s: %s", project_id, error
            )
            continue

        refreshed.append(project_id)

    return refreshed
