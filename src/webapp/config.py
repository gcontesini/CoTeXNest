"""Centralized filesystem paths and settings for the webapp backend.

All modules import paths from here instead of hard-coding them, so the
Docker volume layout (workspace/ and plugins/ mounted separately from
the source tree) stays consistent across the whole app.
"""

from __future__ import annotations

import os
from pathlib import Path

# Repository root for this sub-project (webapp/), not the monorepo root.
BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent

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

# Wall-clock timeout (seconds) for a single AI-assist request (chat,
# suggestion, compile-error fix, or writing-style synthesis). Ollama
# model pulls use their own, much longer timeout -- see assistant.py.
AI_ASSIST_TIMEOUT_SECONDS: int = int(os.environ.get("WEBAPP_AI_TIMEOUT", "60"))


def ensure_dirs() -> None:
    """Create the mounted-volume directories if they don't exist yet.

    Safe to call on every startup; idempotent.
    """
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    PLUGINS_ROOT.mkdir(parents=True, exist_ok=True)
    ASSISTANT_CONFIG_ROOT.mkdir(parents=True, exist_ok=True)


def project_dir(project_id: str) -> Path:
    """Resolve a project id to its working directory, rejecting path traversal."""
    candidate = (WORKSPACE_ROOT / project_id).resolve()
    if not str(candidate).startswith(str(WORKSPACE_ROOT.resolve())):
        raise ValueError(f"invalid project_id: {project_id!r}")
    return candidate
