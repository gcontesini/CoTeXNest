# webapp

A local-first, self-hosted LaTeX editor: web editor with PDF preview,
git-backed version history, and a LaTeX package/template manager. Think
Overleaf, but it runs on your own machine or server.

This directory is a self-contained sub-project of the monorepo. It does
not depend on, and is not referenced by, `apps/`.

## Quickstart

Without Docker (local Python development):

```bash
just install
just dev
```

With Docker (includes a full LaTeX toolchain):

```bash
just install
just up
just logs
```

The app is served at `http://localhost:8000` either way. Under Docker
this is two containers — `webfront` (nginx, the UI) proxying to
`backend` (FastAPI: rendering, history, packages) — started together by
`just up`; the backend alone is also reachable at `http://localhost:8001`
for direct API debugging. See `docs/architecture.md`.

See `docs/` for architecture notes, the API contract
(`src/webapp/models.py`, `src/webapp/config.py`), and the package
catalog format. These same Markdown files also build as a Sphinx site
(`just docs-build`, output in `docs/_build/html/`), published to
GitHub Pages on every push to `main` -- see `docs/index.md`.
