# Editor UI Tour

This page covers the editor window itself: the menu bar, the sidebar
file browser, the editor/preview split, and the status bar. For the
History and Packages side panels, see [history.md](history.md) and
[package-manager.md](package-manager.md) -- this page only covers how
to open them.

## Layout overview

From top to bottom, the window is:

1. **Menu bar** -- File and Help dropdown menus.
2. **Top bar** -- project selector, New/Delete Project, Refresh, Save
   Version, History, Packages, and Assistant.
3. **Main area** -- the file browser sidebar on the left, and the
   editor/preview split on the right.
4. **Status bar** -- character, word, and line counts for the file
   currently open in the editor.

## Menu bar

### File menu

| Item | What it does |
|---|---|
| New Project | Opens the "New Project" dialog (same as the top bar's "+ New Project" button). |
| New Project from Sample | Opens the same dialog with the template dropdown focused, for picking a starter template. |
| Open Project | Focuses the project selector in the top bar. |
| Save | Immediately saves the currently open file (autosave already does this ~1 second after you stop typing; this forces it right away). |
| Save As... | Prompts for a new project-relative path, saves the editor's current content there, and opens it. The original file is left untouched. |
| Export PDF | Downloads the project's most recently compiled PDF. Requires at least one successful compile first. |
| Close Project | Clears the editor, preview, and file browser without deleting anything -- just closes the working session. |
| Quit | This is a browser tab, not a desktop app: there's no in-browser action that can stop the server. This shows a short reminder to run `just down` (or `docker compose -f docker/docker-compose.yml down`) from a terminal, and that it's safe to just close the tab -- the server keeps running until you do. |

### Help menu

| Item | What it does |
|---|---|
| Documentation | Opens this documentation site, once it is published (see "Building these docs" in [index.md](index.md)). |
| Template Examples | Opens the Packages panel, which lists the available starter templates alongside installable packages. |

## File browser (sidebar)

The sidebar lists every file and folder in the open project. It is
built entirely on the project/file REST API described in
[api-reference.md](api-reference.md#projects-and-files); everything
below is client-side behavior on top of that API.

- **Search box** -- filters the tree by name as you type. A match keeps
  its parent folders visible (and expanded) so you can still see where
  it lives; clearing the box restores the tree to how it was before you
  searched.
- **"+ File" / "+ Folder" buttons** -- create a new file or folder at
  the project root. The new entry appears in the tree with its name
  already editable -- type a name and press Enter (or Escape to cancel).
- **Expand/collapse** -- click a folder row to toggle its children.
- **Per-row actions** -- every row has a "⋮" button (and also responds
  to right-click) that opens a menu:
  - **Rename** -- edits the name in place, the same way creating a new
    entry does.
  - **Delete** -- asks for confirmation first; deleting a folder warns
    that everything inside it goes too.
  - **Move to...** -- opens a dialog listing the project's folders;
    picking one moves the file or folder there. The list leaves out
    destinations that don't make sense (the item's current folder, or
    -- for a folder -- itself or any of its own subfolders).
  - **New File Inside** / **New Folder Inside** (folders only) --
    same inline-naming behavior as the root-level "+" buttons, but
    nested under that folder.

## Editor and preview

- Typing autosaves to the server about 800ms after you stop, shown
  briefly as "Saving…" / "Saved" next to the file name.
- The PDF preview does **not** recompile on every keystroke or every
  autosave -- that would make a large document feel sluggish to edit.
  Instead, a recompile is scheduled 15 seconds after your last edit,
  and reset every time you make a new edit, so it only actually runs
  once typing settles. The **Refresh** button in the top bar always
  compiles immediately, bypassing this delay.
- The divider between the editor and preview panes is draggable, and so
  is the one above the assistant dock (drag it to resize the dock).

## Status bar

Shows the character, word, and line count of whatever file is
currently open in the editor, updated live as you type (word count
splits on whitespace; an empty file counts as zero characters, words,
and lines).

## Settings > Git

Acts on the open project's repository (backend: `services/git_remote.py`,
routes under `/api/projects/{id}/git`).

- **Repository**: the `origin` remote URL (`https://`, `ssh://` or
  `user@host:path` forms only) with the branch and ahead/behind counts as of
  the last fetch.
- **Identity**: the author name used for saved versions (per browser).
- **Credentials**: username and personal access token for an `https://`
  remote. The token is stored as plain text in the project's `.git/config`
  inside the workspace volume and is never returned by the API. Changing the
  URL to another host removes the stored credentials. Use a token limited to
  one repository.
- **Branch**: switch to a local branch, track a fetched remote-only branch, or
  create a branch from the current one. Requires no uncommitted changes to
  tracked files.
- **Remote actions**: Fetch, Pull (rebase onto `origin/<branch>`, aborted
  cleanly on conflict) and Push (sets upstream). Pull and Push do not commit;
  use Sync Changes for the commit, rebase, push sequence.
