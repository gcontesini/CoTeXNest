# Architecture

This document describes the system design of the webapp: a local-first,
Docker-deployed LaTeX editor with side-by-side PDF preview, git-backed
version history, and a LaTeX package/template manager.

## Stack

| Layer | Choice |
|---|---|
| Backend | Python, FastAPI, Uvicorn |
| Backend tooling | `uv` for environment and dependency management |
| LaTeX compilation | TeX Live, driven through `latexmk` |
| Version history | real `git` repositories, accessed via GitPython |
| Frontend | plain HTML/CSS/JS, no Node/npm build step |
| Editor component | CodeMirror 6, loaded as an ES module from `esm.sh` |
| PDF preview component | pdf.js, loaded as an ES module from `esm.sh` |

In the Docker deployment, the application runs as two containers: an
nginx-based `webfront` serving the static files and reverse-proxying
`/api/*`, and a `backend` running the FastAPI app (see "Deployment
model" below). The backend's own `main.py` can still serve the static
files itself and is what `just dev` (no Docker) runs directly, so there
is no bundler step either way.

## Directory layout

```
webapp/
  pyproject.toml, justfile, README.md
  docker/Dockerfile          # "backend" image: FastAPI + TeX Live + git
  docker/webfront.Dockerfile # "webfront" image: nginx, static files + /api proxy
  docker/nginx.conf          # webfront's routing (see "Deployment model")
  docker/docker-compose.yml  # wires backend + webfront together
  src/webapp/
    main.py                   # FastAPI app wiring
    config.py                 # paths: WORKSPACE_ROOT, PLUGINS_ROOT, CATALOG_PATH, etc.
    models.py                 # shared Pydantic request/response models
    routers/{projects,compile,history,packages}.py
    services/{latex_render,git_history,package_manager}.py
    data/catalog.json         # curated seed catalog of packages + templates
    static/                   # the frontend (index.html, css/, js/)
  workspace/<project_id>/     # one real git repo per project (Docker volume)
  plugins/<package_id>/       # shared, global installed-package files (Docker volume)
  docs/                      # Markdown docs, also a Sphinx source dir (conf.py, index.md)
  .github/workflows/docs.yml # builds docs/ and deploys it to GitHub Pages
  tests/
```

`routers/` holds the FastAPI route handlers, one module per feature area.
`services/` holds the logic each router calls into: LaTeX compilation,
git operations, and package/template management. Routers stay thin;
services hold the behavior that is unit-tested directly.

`workspace/` and `plugins/` are the two pieces of state the application
owns. Everything else under `webapp/` is source code and can be rebuilt
from the repository at any time.

## Deployment model

`just up` (`docker compose -f docker/docker-compose.yml up --build`) builds and starts two containers:

- **`webfront`** — nginx, serving `static/` and reverse-proxying
  `/api/*` and `/healthz` to `backend`. This is the container exposed
  to the host, at `http://localhost:8000`. It holds no application
  state and has no TeX Live, Python, or git inside it.
- **`backend`** — the FastAPI app: the REST API, LaTeX compilation,
  git-backed history, and the package manager. Also reachable directly
  at `http://localhost:8001`, for debugging the API without going
  through the proxy.

The browser only ever talks to `webfront`; `docker/nginx.conf` is what makes
the split invisible to the frontend code, which still issues same-origin
`/api/...` requests exactly as it would against a single process.

`backend` bind-mounts two host directories:

- `./workspace` into the container's `workspace/` (one subdirectory per
  project, each a git repository)
- `./plugins` into the container's `plugins/` (installed packages and
  their files, shared across all projects)

Because both directories are bind mounts rather than named volumes
internal to the container, project files and installed packages survive
a container rebuild (`just up` again) without any
export/import step. Rebuilding either image replaces that container's
code; it does not touch `./workspace` or `./plugins` on the host.
`webfront` depends on `backend` passing its `/healthz` check before it
starts, so a slow first-time TeX Live/dependency install in `backend`
does not result in `webfront` serving a UI that immediately fails every
API call.

## Design decisions

### Every piece of state lives in a bind-mounted volume

`workspace/` (project files, one git repo per project) and `plugins/`
(installed packages) are bind mounts, not Docker-internal named
volumes -- see "Deployment model" above. This is a deliberate,
project-wide rule, not just an implementation detail of those two
directories: **any new persistent state this application ever grows
must land under a bind-mounted host directory too**, so a user can
inspect or edit it directly with a text editor/file manager, with no
container running at all. A feature whose state only exists inside a
container's internal filesystem, a database only reachable through the
API, or (for anything server-relevant) in browser-only storage, does
not meet this bar.

The accepted exceptions today are all genuinely per-browser UI
preferences, not project or application configuration -- none of them
carry any meaning outside the browser session that set them, and none
of them are something a second person opening the same project would
expect to inherit:

- The "author name" shown on saved versions, editable in Settings > Git
  (`AUTHOR_NAME_STORAGE_KEY` in `static/js/settings-modal.js`).
- Appearance settings from the Settings dialog -- theme, application
  font/size, editor syntax color scheme, indentation size, and the
  highlight-current-line/highlight-matching-brackets toggles (all in
  `static/js/settings-modal.js`).
- The personal spell-check dictionary -- words learned via the
  editor's right-click "Learn spelling" or added directly in Settings
  (`static/js/spellcheck.js`). The *English dictionary itself* is
  different: that's bundled, read-only application data shipped with
  every deployment (`static/data/dictionary-en.txt`), not per-browser
  state, so it lives as a normal static asset rather than in
  localStorage.
- The session-only Log Files list (compile/assistant error history
  shown in the Log Files panel) -- deliberately *not* persisted at
  all, not even to localStorage: it's a live diagnostic aid for the
  current session, not something meant to survive a reload.

### TeX Live over Tectonic

Compilation runs through TeX Live's `latexmk`, not Tectonic. Tectonic
offers a smaller, more reproducible, self-contained install, but its
package coverage lags behind the full CTAN archive that TeX Live ships.
Because this editor targets non-technical users who may paste in LaTeX
from arbitrary sources (journal templates, class files found online,
packages the package manager has not curated), broad compatibility with
whatever `.sty`/`.cls` a user encounters matters more than install size
or determinism of the compiler itself. The trade-off is a considerably
larger container image.

### A single global `plugins/` directory, not per-project

Installed packages live in one shared directory, visible to every
project through `TEXINPUTS`, rather than being copied into each
project's own directory. This mirrors how a system-wide TeX Live
package manager works: install once, available everywhere. It also
keeps a project's git history limited to the user's own `.tex` content
instead of being inflated by every project separately vendoring copies
of the same `.sty` file. The trade-off is that packages are not
versioned per project: upgrading or removing a package in `plugins/`
changes what every project sees on its next compile.

### Revert creates a new commit; history is never rewritten

Reverting a project to an earlier state does not run a destructive
operation like `git reset --hard` or a force-push. It checks out the
old tree and records that restoration as a new commit on top of the
current history. Every commit that ever existed stays reachable. This
matters because the application runs by and for non-technical users:
there is no concept of "detached HEAD" or "lost commit" presented to
them, and nothing a user does through the UI can make a previous
version of their project irrecoverable.

### No-build-step frontend, loaded from a CDN

The frontend is plain HTML/CSS/JS with no Node toolchain, no package.json,
and no bundling step. CodeMirror 6 and pdf.js are imported as ES modules
directly from `esm.sh` at runtime. This keeps the frontend buildable and
editable without installing any JavaScript tooling, which matches the
project's "Python + `uv` only" developer environment.

This is a genuine, currently unresolved limitation, not a design choice
with no downside: the first time a browser loads the app, it needs
internet access to fetch CodeMirror and pdf.js from `esm.sh`. The browser
then caches those modules, so subsequent loads from the same browser
work offline. A fully air-gapped deployment, one with no internet access
at any point, is not currently supported. Vendoring both libraries into
`static/` so the container is self-contained from the first request is a
documented future improvement, not something this version implements.
