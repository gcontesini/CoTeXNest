"""Centralized filesystem paths and settings for the webapp backend.

All modules import paths from here instead of hard-coding them, so the
Docker volume layout (workspace/ and plugins/ mounted separately from
the source tree) stays consistent across the whole app.
"""

from __future__ import annotations

import os
from pathlib import Path

# Every git subprocess this app ever spawns (project creation's clone,
# the Sync Changes feature's fetch/push, both against a remote URL a
# user supplies) must never block waiting for interactive credentials
# -- this process is headless, with no TTY to answer a username/
# password prompt, so without this a request against a private remote
# without cached/configured credentials would hang indefinitely
# instead of failing with a clear, immediate error. Set once, at
# import time, since config.py is the first thing every entry point
# imports -- a module-local `git.Git().custom_environment(...)` context
# manager per call site would be easy to forget on the next one added.
os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")

# Repository root for this sub-project (webapp/), not the monorepo root.
BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent

# Redirects git's "global" config scope to a file this app owns,
# instead of whatever (or no) ~/.gitconfig the operator already has --
# see ensure_git_safe_directory() below for why, and why the actual
# file write happens at real app startup, not here at import time.
# Scoping this via GIT_CONFIG_GLOBAL rather than editing ~/.gitconfig
# directly means `just dev` (no Docker) never touches a real
# developer's own git configuration, only what this process's own git
# subprocesses see.
GIT_CONFIG_PATH: Path = BASE_DIR / ".gitconfig-webapp"
os.environ.setdefault("GIT_CONFIG_GLOBAL", str(GIT_CONFIG_PATH))

# Each project is a real git repo living under WORKSPACE_ROOT/<project_id>/.
WORKSPACE_ROOT: Path = Path(
    os.environ.get("WEBAPP_WORKSPACE_ROOT", str(BASE_DIR / "workspace"))
)

# Shared, global LaTeX package/template store. Installing a package here
# makes it available to every project via TEXINPUTS (see latex_render.py).
PLUGINS_ROOT: Path = Path(
    os.environ.get("WEBAPP_PLUGINS_ROOT", str(BASE_DIR / "plugins"))
)

# Manifest of what's currently installed under PLUGINS_ROOT.
PLUGINS_MANIFEST_PATH: Path = PLUGINS_ROOT / "manifest.json"

# Curated, bundled catalog of searchable packages/templates (read-only,
# ships with the app; distinct from the manifest of what's installed).
CATALOG_PATH: Path = BASE_DIR / "src" / "webapp" / "data" / "catalog.json"

# Default main file name used when creating a new project.
DEFAULT_MAIN_FILE: str = "main.tex"

# Wall-clock timeout (seconds) for a single LaTeX compile invocation.
COMPILE_TIMEOUT_SECONDS: int = int(os.environ.get("WEBAPP_COMPILE_TIMEOUT", "60"))

# Per-user AI assistant configuration (provider choice, model, external
# API key, writing-style profile) -- see services/assistant.py. A bind
# mount in Docker, the same way WORKSPACE_ROOT/PLUGINS_ROOT are, so it
# survives a container rebuild and is inspectable without the app running.
ASSISTANT_CONFIG_ROOT: Path = Path(
    os.environ.get(
        "WEBAPP_ASSISTANT_CONFIG_ROOT", str(BASE_DIR / "assistant_config")
    )
)
ASSISTANT_CONFIG_PATH: Path = ASSISTANT_CONFIG_ROOT / "settings.json"

# Base URL of the local Ollama server the AI assistant talks to when its
# provider is "local". Defaults to Ollama's own default port on
# localhost for "just dev"; docker-compose.yml overrides this to the
# "ai-assistant" service's container-network address.
OLLAMA_BASE_URL: str = os.environ.get(
    "WEBAPP_OLLAMA_BASE_URL", "http://localhost:11434"
)

# Wall-clock timeout (seconds) for a single AI-assist provider call
# (chat, suggestion, compile-error fix, or writing-style synthesis).
# Ollama model pulls use their own, much longer timeout -- see
# assistant.py. 60s used to be tight enough to routinely time out a
# real (non-trivial) prompt -- observed live against both a CPU-only
# local model under load and a slow external provider. chat() and
# fix_compile_error() now run as background jobs the frontend polls
# for (see assistant.py's job runner) rather than blocking an HTTP
# request on this directly, so this can afford to be generous.
AI_ASSIST_TIMEOUT_SECONDS: int = int(os.environ.get("WEBAPP_AI_TIMEOUT", "180"))


def ensure_dirs() -> None:
    """Create the mounted-volume directories if they don't exist yet.

    Safe to call on every startup; idempotent.
    """
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    PLUGINS_ROOT.mkdir(parents=True, exist_ok=True)
    ASSISTANT_CONFIG_ROOT.mkdir(parents=True, exist_ok=True)


def ensure_git_safe_directory() -> None:
    """Write this app's own, self-sufficient global git config.

    Two independent problems, fixed by the same file:

    1. **Dubious ownership.** Cloning a project (or Sync Changes's
       fetch/rebase/push) can fail with git's "detected dubious
       ownership" refusal the moment a repository's on-disk owner
       doesn't exactly match this process's -- confirmed live under
       Docker, where the backend container runs as root while
       ``./workspace`` is bind-mounted in owned by the host user. The
       same class of mismatch this project has already hit before in
       the opposite direction (a root-owned project directory blocking
       a non-Docker ``just dev`` run -- see ``routers/projects.py``'s
       ``_FILESYSTEM_ERROR_HINT``). Blanket-trusting every directory
       (rather than scoping to just ``WORKSPACE_ROOT``) is deliberate,
       not laziness: verified live, git's ``safe.directory`` matching
       does not honor a ``<path>/*`` wildcard prefix the way its own
       help text implies -- only an exact literal path or the single
       blanket ``*`` value actually works, and project directories are
       created dynamically, so listing exact paths isn't practical.
       The "someone else's planted repository" threat this check
       exists for doesn't apply here: this backend process is the sole
       owner/operator of everything under ``WORKSPACE_ROOT`` by
       design, whether that's exactly reflected in filesystem UIDs or
       not.
    2. **Committer identity for rebase.** ``git rebase`` (Sync
       Changes) replays commits under a *committer* identity read from
       git's own config -- independent of, and in addition to, the
       *author* identity ``services.git_history`` already sets
       explicitly per commit via ``git.Actor(...)``. Since
       ``GIT_CONFIG_GLOBAL`` (set above) replaces whatever real
       ``~/.gitconfig`` the operator has rather than layering on top
       of it, this app's git subprocesses would otherwise have no
       committer identity at all unless the operator happened to
       already have one configured -- this app must not depend on
       that. ``user.name``/``user.email`` here are never shown to
       anyone; they only satisfy git's own internal requirement that a
       committer identity exist.

    This blanket trust and fallback identity are scoped to a config
    file this app owns (``GIT_CONFIG_PATH``, via ``GIT_CONFIG_GLOBAL``
    set above), never the operator's real ``~/.gitconfig``.

    Must run at real app startup (``main.py``), not at this module's
    import time: every test file imports ``webapp.config``, and
    importing a module must never have the side effect of writing a
    file to disk.
    """
    GIT_CONFIG_PATH.write_text(
        "[safe]\n"
        "\tdirectory = *\n"
        "[user]\n"
        "\tname = LaTeX Writer\n"
        "\temail = webapp@localhost\n",
        encoding="utf-8",
    )


def project_dir(project_id: str) -> Path:
    """Resolve a project id to its working directory, rejecting path traversal."""
    candidate = (WORKSPACE_ROOT / project_id).resolve()
    if not str(candidate).startswith(str(WORKSPACE_ROOT.resolve())):
        raise ValueError(f"invalid project_id: {project_id!r}")
    return candidate
