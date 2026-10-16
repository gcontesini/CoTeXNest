# webapp

Self-hosted LaTeX editor with PDF preview and git-backed version history.
It includes a package and template manager and an AI assistant that uses
a local Ollama model or an external API key.

This directory is a sub-project of the monorepo. It does not depend on `apps/`.

## Quickstart

Run `./bootstrap.sh` to install git, Docker and `just` when missing and to build and start the app.

Without Docker (local Python development):

```bash
just install
just dev
```

With Docker (includes the LaTeX toolchain):

```bash
just install
just up
just logs
```

The app is served at `http://localhost:8000`. Under Docker `just up` starts three containers:

- `webfront`: nginx serving the UI and proxying to `backend`
- `backend`: FastAPI for rendering, history, packages and the assistant. Also reachable at `http://localhost:8001`
- `ai-assistant`: Ollama for the local AI provider

See `docs/architecture.md`.

The AI assistant has no default model or API key. Open Assistant Settings → AI Assistant and either pull a local model by pasting a tag from [ollama.com/library](https://ollama.com/library) (for example `qwen2.5-coder:3b`) or add an external provider API key. See `docs/ai-assistant.md`.

Further documentation is in `docs/`. The same files build as a Sphinx site with `just docs-build` (output in `docs/_build/html/`) and are published to GitHub Pages on every push to `main`. See `docs/index.md`.

## Two git histories

- **This repository** holds the application: backend, frontend, Docker setup and docs.
- **Each LaTeX project** lives in `workspace/<project-id>/` as a separate git repository. The Save Version and History panels read and write it. `workspace/` is gitignored in this repository.

The app runs `git init` and creates an initial commit when a project is created. The project has no remote until one is added.

### Sharing a project

```bash
cd workspace/<project-id>
git remote add origin <url-to-an-empty-repo>
git push -u origin HEAD
```

`HEAD` pushes the branch git chose when the project was initialized. The full history travels with it.

### Importing a shared project

Click **+ New Project** and paste the repository URL (HTTPS or SSH) into the Remote repository URL field. The app clones it and sets `origin`.

Manual equivalent:

```bash
git clone <url> workspace/<new-project-id>
```

`<new-project-id>` can be any valid directory name. The project appears after reloading the project list. If the clone has no `.project.json` in its root create one:

```json
{
  "name": "Project name",
  "created_at": 0,
  "main_file": "main.tex"
}
```

This works under Docker and `just dev` because `workspace/` is a host directory in both cases (see `docs/architecture.md`).

### Sync Changes

For a project with a remote the **Sync Changes** button in the top banner runs these steps:

1. Commit unsaved local changes. The AI assistant writes the message from the diff when none is given. The app prompts for one when no provider is available.
2. Rebase onto the remote.
3. Push.

- A rebase conflict stops the sync and aborts the rebase. The project returns to its state before the click. Resolve the conflict with git in `workspace/<project-id>` and click the button again. The app has no conflict-resolution UI.
- A project without a remote returns an error.
- The implementation is `git fetch`, `git rebase` and `git push`. It works with any git host.

### Switching projects

Select a project in the "Select project" dropdown at the top of the page. The dropdown is rebuilt from `GET /api/projects` on every page load. Reload the page after cloning into `workspace/`.

The editor autosaves 800 ms after typing stops (`AUTOSAVE_DEBOUNCE_MS` in `static/js/editor.js`). Switching projects or files flushes a pending save.

## Platform support

| Platform | Status |
|---|---|
| Linux | Verified on Debian/Ubuntu. The `dnf` and `pacman` paths in `bootstrap.sh` are untested. |
| macOS | Not verified. `bootstrap.sh` installs dependencies through Homebrew. Docker Desktop must be launched once manually after installation. |
| Windows | Not supported natively. Use WSL2 with Docker Desktop (WSL2 backend) and run `./bootstrap.sh` in the WSL2 terminal. Not verified. |
