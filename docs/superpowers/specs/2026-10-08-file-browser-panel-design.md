# File browser panel — design

Date: 2026-10-08

## Context

`.pending_tasks` asks for a left-panel file browser with full CRUD for
non-technical users: delete, add, rename, move files and folders, plus
search. The backend (`src/webapp/routers/projects.py`) already
implements the entire required surface — create (`POST
.../files`), read (`GET .../files/{path}`), write (`PUT
.../files/{path}`), delete (`DELETE .../files/{path}`), and
rename-which-doubles-as-move (`PATCH .../files/{path}`) — all with
path-traversal protection and covered by `tests/test_projects.py`.

The frontend (`static/js/app.js`) only exposes a sliver of this: a
flat, read-only tree, delete-file-only (no folder delete, no rename,
no move), and a single text box for `new/path/file.tex`. This is a
**frontend-only** feature: no backend changes are needed.

## Goals

- Create, rename, delete, and move both files and folders from the
  sidebar.
- Search/filter the tree by name.
- Interaction model usable by non-technical users: every action is
  reachable without needing prior knowledge of right-click or
  drag-and-drop conventions.

## Non-goals

- No backend API changes.
- No automated JS test suite (none exists in this project; see
  "Testing" below).
- No multi-select / bulk actions.

## Architecture

New module `static/js/file-tree.js`, following the existing factory
convention used by `history-panel.js` and `packages-panel.js`:

```js
export function createFileTree({ container, getProjectId, notify, onOpenFile, getCurrentFilePath }) {
  // returns { load(projectId), getExpandedState(), ... } as needed
}
```

`app.js` is simplified to delegate to this module instead of owning
tree-rendering logic directly (removing `renderFileTree`,
`renderTreeNode`, `flattenTreeRoots`, `refreshFileTree`, and the
`new-file-input`/`btn-new-file` wiring), the same way it already
delegates to `createHistoryPanel`/`createPackagesPanel`.

Two additions to `api.js` (mirroring the existing one-function-per-
endpoint style):

```js
export async function createEntry(projectId, path, type) {
  return request("POST", `/api/projects/${encodeURIComponent(projectId)}/files`, { path, type });
}

export async function renameEntry(projectId, path, newPath) {
  return request("PATCH", `/api/projects/${encodeURIComponent(projectId)}/files/${encodeFilePath(path)}`, { new_path: newPath });
}
```

(`deleteFile` and `getFileTree` already exist and are reused as-is.)

## Components

- **Toolbar**: a search `<input>` (live filter, client-side) plus
  "+ File" / "+ Folder" buttons that create a new entry at the
  project root.
- **Tree rows**: an expand/collapse chevron for directories. Every row
  carries a small "⋮" button, visible on hover/focus and always
  visible (so it works on touch devices too), that opens the same
  menu as right-clicking the row.
- **Context menu** (opened by right-click or the "⋮" button):
  - File row: Rename, Delete, Move to...
  - Folder row: New File Inside, New Folder Inside, Rename, Delete,
    Move to...
  - Implemented as a positioned `<ul>` dropdown, closed on
    outside-click or Escape, navigable with arrow keys + Enter.
- **Inline create/rename**: choosing "Rename" or "New File/Folder
  Inside" swaps the row's label for an editable `<input>` in place.
  Enter commits, Escape cancels, blur commits. A newly created node
  starts with an empty value and placeholder text (e.g. "Untitled");
  committing an empty name cancels the creation instead of calling
  the API.
- **"Move to..." dialog**: a `<dialog>` (same styling as the existing
  "New Project" dialog) listing every folder in the project
  (including the root) as a pick-list. Confirming calls
  `api.renameEntry(projectId, sourcePath, chosenFolder + "/" +
  basename(sourcePath))`.
- **Search**: pure client-side substring filter (case-insensitive) over
  the already-fetched tree — no new endpoint. A match keeps its
  ancestor directories visible and auto-expanded; clearing the search
  box restores the prior expand/collapse state.

## Data flow

1. `app.js`'s `openProject()` calls `fileTree.load(projectId)` in
   place of today's `refreshFileTree()`.
2. `file-tree.js` fetches `api.getFileTree(projectId)`, stores the
   result, and renders it.
3. Every mutating action (create, rename, delete, move) calls the
   matching `api.js` function, then re-fetches the whole tree from the
   backend as the source of truth — no optimistic local patching. This
   keeps the module simple and avoids state drift; the extra
   round-trip is irrelevant for a local-first, single-user tree.
4. Opening a file (clicking a file row) calls the `onOpenFile(path)`
   callback, which bubbles up to `app.js`'s existing `openFile()`.

## Error handling

- Every API call is wrapped in try/catch → `notify(message, "error")`,
  matching the existing toast pattern used everywhere else in the app.
- Deletes go through `window.confirm`, consistent with the existing
  project-delete flow. Folder deletes use explicit wording ("and
  everything inside it") since `DELETE .../files/{path}` on a
  directory is a recursive `shutil.rmtree`.
- Inline create/rename validates a non-empty name client-side before
  calling the API. If the backend still rejects it (400, e.g. "already
  exists at path"), the row stays in edit mode with the error surfaced
  via toast, rather than silently discarding the user's input.
- "Move to..." is blocked client-side (with a toast) when the chosen
  destination is the entry's own current parent (no-op) or, for a
  folder being moved, when the destination is that folder itself or
  one of its own descendants — a move the backend has no graceful
  recovery path for.

## Testing

This project has no Node/npm build step and no JS test harness by
design (confirmed in `docs/architecture.md`). The backend endpoints
this feature reuses are already covered by `tests/test_projects.py`
and are unchanged by this work. Verification here is manual: run
`just dev` and exercise create/rename/delete/move/search for both
files and folders in a real browser before considering this done.
