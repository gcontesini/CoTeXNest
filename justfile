# justfile for the webapp sub-project (self-hosted LaTeX editor).
# This is separate from the justfile at the monorepo root, which
# belongs to the older, unrelated Tauri desktop app.

# Show all available recipes.
default:
  @just --list

# Show all available recipes.
help:
  @just --list

# Install uv itself if it isn't already on PATH.
ensure-uv:
  @if ! command -v uv >/dev/null 2>&1; then \
    echo "uv not found. Installing uv..."; \
    curl -LsSf https://astral.sh/uv/install.sh | sh; \
  else \
    echo "uv is already installed."; \
  fi

# Install/sync all Python dependencies (app + dev tools) into .venv.
install: ensure-uv
  uv sync

# Run the API locally with auto-reload, without Docker.
dev:
  uv run uvicorn webapp.main:app --reload --host 0.0.0.0 --port 8000

# Alias for `dev` — run the app locally without Docker.
run: dev

# Run the test suite.
test:
  uv run pytest -q

# Run the frontend's pure-logic JS tests (spell-check tokenizer/edit-
# distance) via Node's own built-in test runner -- no npm install, no
# bundler, consistent with this project having no Node toolchain.
test-js:
  node --test tests/js/*.test.js

# Run the test suite with a coverage report.
test-cov:
  uv run pytest --cov=src/webapp -q

# Check the code for lint issues with ruff.
lint:
  uv run ruff check src tests

# Build the Sphinx documentation site from docs/ into docs/_build/html.
docs-build:
  uv run sphinx-build -b html docs docs/_build/html

# Remove the built documentation site.
docs-clean:
  rm -rf docs/_build

# Auto-format the code with ruff.
format:
  uv run ruff format src tests

# Build the backend image (LaTeX rendering, git history, package
# manager, REST API) locally, without starting it.
docker-build-backend:
  docker build -t latex-webapp-backend -f Dockerfile .

# Build the webfront image (static editor UI + reverse proxy to the
# backend) locally, without starting it.
docker-build-webfront:
  docker build -t latex-webapp-webfront -f webfront.Dockerfile .

# Build both images via docker compose (uses its own build cache/tags).
docker-build:
  docker compose build

# Build (if needed) and start both containers via docker compose, in
# the background: "backend" (rendering/history/packages API) and
# "webfront" (editor UI, proxying /api to "backend"). Open the app at
# http://localhost:8000 once both report healthy.
up:
  docker compose up --build -d

# Alias for `up` — build and deploy both containers locally via Docker.
deploy: up

# Stop and remove the docker compose containers.
down:
  docker compose down

# Follow both containers' logs.
logs:
  docker compose logs -f

# Follow only the backend's logs (rendering/history/package errors show up here).
logs-backend:
  docker compose logs -f backend

# Follow only the webfront's logs (nginx access/error log).
logs-webfront:
  docker compose logs -f webfront

# Remove local virtual environment and cache directories.
clean:
  rm -rf .venv
  find . -type d -name "__pycache__" -exec rm -rf {} +
  rm -rf .pytest_cache
