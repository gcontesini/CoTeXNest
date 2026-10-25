"""FastAPI application entrypoint: wires every router together.

This module is intentionally thin: all business logic lives in
``services/*`` and all HTTP surface lives in ``routers/*``. This file
only constructs the app, mounts routers, serves the static frontend,
and runs one-time startup housekeeping.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .routers import assistant, custom_commands, git_remote, history, packages, projects
from .routers import compile as compile_router

_STATIC_DIR: Path = Path(__file__).resolve().parent / "static"

app: FastAPI = FastAPI(
    title="LaTeX Studio",
    description=(
        "Self-hosted, Docker-deployed LaTeX editor: web editor + PDF "
        "preview, git-backed version history, and a LaTeX package/"
        "template manager."
    ),
    version="0.1.0",
)

# This app is designed to run on a trusted local network behind, at
# most, a thin reverse-proxy container (see docker/docker-compose.yml's
# two-container split: a static-file/proxy "webfront" service in front
# of this API). There is no user/session model to protect with a
# stricter policy, so permissive CORS keeps local development (hitting
# the API directly on its own port) and the proxied deployment both
# working without extra configuration.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _on_startup() -> None:
    """Ensure the workspace/plugins volumes exist before serving traffic."""
    config.ensure_dirs()
    config.ensure_git_safe_directory()


app.include_router(projects.router)
app.include_router(compile_router.router)
app.include_router(history.router)
app.include_router(git_remote.router)
app.include_router(packages.router)
app.include_router(assistant.router)
app.include_router(custom_commands.router)


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    """Liveness/readiness probe for container orchestration."""
    return {"ok": True}


if _STATIC_DIR.is_dir():
    app.mount(
        "/static",
        StaticFiles(directory=_STATIC_DIR),
        name="static",
    )

    @app.get("/")
    def index() -> FileResponse:
        """Serve the single-page app shell.

        Only relevant when this process also serves the frontend (the
        "just dev" local workflow, and the backend container's static
        mount is simply unused in the two-container Docker deployment,
        where the webfront/nginx container serves ``index.html`` and
        proxies ``/api`` here instead).
        """
        return FileResponse(_STATIC_DIR / "index.html")
