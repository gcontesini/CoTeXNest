# webapp

A local-first, self-hosted LaTeX editor: web editor with PDF preview,
git-backed version history, a LaTeX package/template manager, and an
AI writing assistant (local via Ollama, or your own external API key).
Think Overleaf, but it runs on your own machine or server.

This directory is a self-contained sub-project of the monorepo. It does
not depend on, and is not referenced by, `apps/`.

## Quickstart

New to the command line, or just want the fastest path to a running
app? Run `./bootstrap.sh` from this directory. It installs git,
Docker, and `just` if any are missing, then builds and starts
everything for you -- see "Platform support" below for what's actually
verified versus best-effort on your OS.

Already comfortable with `just`, or want to install things yourself:

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
this is three containers — `webfront` (nginx, the UI) proxying to
`backend` (FastAPI: rendering, history, packages, assistant), and
`ai-assistant` (Ollama, for the local AI provider) — started together
by `just up`; the backend alone is also reachable at
`http://localhost:8001` for direct API debugging. See
`docs/architecture.md`.

The AI assistant doesn't ship with a default model or API key, by
design, so a fresh install stays lightweight: open the Assistant
panel's Settings tab and either pull a local model (paste any tag from
[ollama.com/library](https://ollama.com/library), e.g.
`qwen2.5-coder:3b`) or add an external provider's API key. See
`docs/ai-assistant.md`.

See `docs/` for architecture notes, the API contract
(`src/webapp/models.py`, `src/webapp/config.py`), and the package
catalog format. These same Markdown files also build as a Sphinx site
(`just docs-build`, output in `docs/_build/html/`), published to
GitHub Pages on every push to `main` -- see `docs/index.md`.

## Two separate git histories: the framework vs. your LaTeX projects

It's easy to assume there's one git repository here, but there are
actually two different kinds, deliberately kept apart:

- **This repository** (the one this README lives in) is the
  application itself: the FastAPI backend, the frontend, the Docker
  setup, these docs. Push *this* to GitHub/GitLab to collaborate on
  the framework with other developers -- normal branches, commits, PRs.
- **Every LaTeX project you create through the app** lives under
  `workspace/<project-id>/` as its own, completely separate git
  repository. This is what the editor's **Save Version** / **History**
  panel reads and writes. `workspace/` is gitignored *in this
  repository* (see `.gitignore`), so none of a project's content or
  history ever becomes part of the framework's own commits.

The app creates that per-project repo for you automatically (`git
init` + an initial commit, the moment you click "New Project"), and it
has no remote configured -- it only exists locally under `workspace/`
until you decide otherwise.

This split is intentional: you install and configure the framework
once (pull a local AI model or set up an API key, build the Docker
images, and so on), and then reuse that same running instance to work
on as many separate LaTeX projects as you like, each with its own
independent save history, without repeating any of that setup per
project.

### Sharing one project with someone else

Each project is a real, ordinary git repository, so you can push it
like any other:

```bash
cd workspace/<project-id>
git remote add origin <url-to-an-empty-repo>
git push -u origin HEAD
```

(`HEAD` rather than a hardcoded branch name, since it pushes whatever
branch git chose by default when the app first initialized that
project -- commonly `main`, but this doesn't assume it.) Everything
the History panel ever recorded travels with it, since it's the same
git history, not an export.

### Bringing in a project someone shared with you

The easiest way: click **+ New Project**, and paste their repository's
URL (HTTPS or SSH) into the "Remote repository URL" field instead of
picking a template. This clones it as the project's starting content
and git history, and wires its `origin` remote automatically -- no
manual git commands needed, and it's the same mechanism **Sync
Changes** (below) then uses to keep that project in sync with the
remote going forward.

The equivalent by hand, if you'd rather:

```bash
git clone <url> workspace/<new-project-id>
```

`<new-project-id>` can be any valid directory name -- it doesn't need
to match the auto-generated `name-a1b2c3` style the UI uses for
projects created through it. The cloned repo already carries its
`.project.json` sidecar file (name, main file, creation time) as part
of its own first commit, since the app commits it alongside the
initial `.tex` content -- so the project should just appear the next
time you reload the project list. If `.project.json` is missing (for
example, a repo that was reorganized before sharing), create one by
hand in the project's root:

```json
{
  "name": "Their project name",
  "created_at": 0,
  "main_file": "main.tex"
}
```

This all works identically whether the app is running under Docker or
`just dev`, since `workspace/` is a plain directory on the host either
way (a bind mount under Docker -- see `docs/architecture.md`).

### Keeping a linked project in sync: the "Sync Changes" button

Once a project has a remote (via the clone-on-create flow above, or
`git remote add origin <url>` by hand), the **Sync Changes** button in
the top banner does what a typical editor's own "sync" action does:
commits any unsaved local changes, rebases them onto whatever's new on
the remote, then pushes. If there's something to commit and you didn't
type a message yourself, it first asks the AI assistant to write one
from the diff; only if that isn't possible (no provider configured, or
it couldn't be reached) does it prompt you for one directly.

A few things worth knowing:

- If rebasing hits a real conflict, syncing stops and tells you so
  instead of guessing -- this app has no conflict-resolution UI, so
  resolve it directly with git in the project's folder
  (`workspace/<project-id>`), then click **Sync Changes** again.
  Nothing is left half-done: a conflicting rebase is cleanly aborted,
  restoring exactly the state before you clicked the button.
- Syncing a project with no remote configured yet returns a clear
  error rather than silently doing nothing.
- This is a plain `git fetch` + `git rebase` + `git push` under the
  hood -- nothing here is specific to any one git host (GitHub,
  GitLab, a bare repo on your own server, all work the same way).

### Switching between projects in the app

The project list is the dropdown at the top of the page, labeled
"Select project." Every project under `workspace/` with a valid
`.project.json` appears there by name.

1. Open `http://localhost:8000` (or reload the page if it's already
   open -- the dropdown is rebuilt from `GET /api/projects` on every
   load, so this is also how a project you just `git clone`d into
   `workspace/` picks up without restarting the server).
2. Click the dropdown and pick a different project by name.
3. The editor, file tree, and PDF preview all switch to that project
   immediately. The editor autosaves ~800ms after you stop typing
   (`static/js/editor.js`'s `AUTOSAVE_DEBOUNCE_MS`); switching projects
   (or files) flushes that pending save immediately rather than
   discarding it, so there's nothing to do beforehand to avoid losing
   an edit -- even one made a moment before you switched.

There's no separate "switch" button: selecting an entry in that one
dropdown *is* the switch.

## Platform support

| Platform | Status |
|---|---|
| Linux | Verified. `./bootstrap.sh` and the Docker deployment are both tested and working in the environment this project is developed in (Debian/Ubuntu-family; the script also has `dnf`/`pacman` paths for Fedora/Arch families, untested). |
| macOS | Best-effort, not independently verified. `./bootstrap.sh` installs git/Docker/`just` via Homebrew; Docker Desktop needs one manual step on first install (launching it once from Applications) that can't be scripted. Everything after that (the Docker Compose deployment itself) should work identically to Linux, since it's the same containers. |
| Windows | Not supported natively -- `bootstrap.sh` is a bash script. Install [WSL2](https://learn.microsoft.com/windows/wsl/install) and Docker Desktop with its WSL2 backend enabled, then open a WSL2 Ubuntu terminal and run `./bootstrap.sh` there; from inside WSL2 this is a normal Linux environment and the Linux path applies unchanged. Not independently verified. |

Why the difference in confidence: this project is built and tested on
Linux, so that's the only platform anything here has actually been run
on. The macOS and Windows/WSL2 paths follow each platform's own
documented, standard tooling (Homebrew, WSL2 + Docker Desktop) rather
than anything project-specific, but "should work the same way" is an
expectation based on how Docker Compose deployments generally behave
across platforms, not a claim that's been checked here. If something
on macOS or Windows doesn't work as described, that's a real gap to
report, not an edge case to apologize for.
