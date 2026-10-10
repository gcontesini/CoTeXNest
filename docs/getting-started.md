# Getting Started

This guide covers two ways to run the application: Docker (recommended
for normal use) and a local, non-Docker development setup.

Don't already have Docker, git, or `just` installed, or would rather
not think about it? Run `./bootstrap.sh` from the project root instead
-- it installs whatever's missing and runs the Docker path below for
you. See the main [README](../README.md#platform-support) for what's
verified on your platform versus best-effort.

## Option A: Docker

### Prerequisites

- Docker and Docker Compose

### Run it

From the `webapp/` directory:

```bash
docker compose up --build
```

This builds two images — `backend` (TeX Live, Python dependencies, and
the application code) and `webfront` (nginx, serving the UI and
proxying API calls to `backend`) — and starts both containers. The
first build downloads a full TeX Live distribution for `backend` and
will take noticeably longer than subsequent builds. See
[architecture.md](architecture.md#deployment-model) for why it's split
this way.

Open a browser at:

```
http://localhost:8000
```

`./workspace` and `./plugins` are created in the current directory on
first run and persist across restarts and rebuilds. See
[architecture.md](architecture.md) for what lives in each.

## Option B: Local development (no Docker)

### Prerequisites

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- [`just`](https://github.com/casey/just)
- A local TeX Live install providing `latexmk` on `PATH`

Verify `latexmk` is available before starting the server:

```bash
latexmk --version
```

If this command is not found, compilation requests will fail with a log
stating `latexmk` is missing. Installing TeX Live is outside the scope
of this document; consult the project README for platform-specific
pointers.

### Install dependencies

```bash
just install
```

This runs `uv sync` against `pyproject.toml`, creating a local virtual
environment with FastAPI, Uvicorn, GitPython, and the other backend
dependencies.

### Run the dev server

```bash
just dev
```

This starts the Uvicorn server locally, without Docker, using the
current working directory's `workspace/` and `plugins/` directories (see
`config.py` for the exact paths and any environment variable overrides).
Open a browser at:

```
http://localhost:8000
```

Use `just dev` while iterating on the backend or frontend: it points at
your local TeX Live install directly, and edits to `src/webapp/static/`
take effect on browser refresh with no build step. Use `just up`
(Docker) when you want the exact environment the application will run
in for end users, including the bundled TeX Live install.

## Creating your first project

1. Open the app in a browser, at the address above.
2. Create a new project. You may optionally seed it from a template, or
   start blank, in which case a minimal `main.tex` is provided.
3. Edit the project's source in the editor pane; the PDF preview pane
   compiles the current state of the project on request.
4. Save a version of your work through the history panel once you are
   satisfied with a change. See [history.md](history.md) for details.

For installing LaTeX packages beyond what ships in the default
environment, see [package-manager.md](package-manager.md).
