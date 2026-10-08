/**
 * file-tree.js
 *
 * Renders the sidebar file/folder browser and owns every interaction
 * on it: expand/collapse, open-file, create, rename, delete, move
 * (via a destination-picker dialog), and client-side name search.
 *
 * This module is self-contained: besides the tree `container` it is
 * handed, it creates its own context menu and "Move to..." dialog
 * elements on demand (appended to `document.body`), so no supporting
 * markup needs to exist in index.html beyond the container itself.
 *
 * The backend (routers/projects.py) already implements the full
 * create/read/write/delete/rename-as-move surface with path-traversal
 * protection; this module only has to drive it.
 */

import { getFileTree, createEntry, renameEntry, deleteFile } from "./api.js";

function createElement(tagName, className, textContent) {
  const element = document.createElement(tagName);

  if (className) {
    element.className = className;
  }

  if (textContent !== undefined) {
    element.textContent = textContent;
  }

  return element;
}

function basename(path) {
  const idx = path.lastIndexOf("/");
  return idx === -1 ? path : path.slice(idx + 1);
}

function dirname(path) {
  const idx = path.lastIndexOf("/");
  return idx === -1 ? "" : path.slice(0, idx);
}

function joinPath(parentPath, name) {
  return parentPath ? `${parentPath}/${name}` : name;
}

function nodeMatchesQuery(node, query) {
  if (basename(node.path).toLowerCase().includes(query)) {
    return true;
  }

  if (node.type !== "dir") {
    return false;
  }

  return (node.children || []).some((child) => nodeMatchesQuery(child, query));
}

function collectDirectoryPaths(node, accumulator) {
  for (const child of node.children || []) {
    if (child.type === "dir") {
      accumulator.push(child.path);
      collectDirectoryPaths(child, accumulator);
    }
  }
}

/**
 * Build an inline text input used for both "create" and "rename"
 * rows: Enter or blur commits (unless the value is empty, which
 * cancels instead), Escape always cancels.
 *
 * @param {object} options
 * @param {string} options.initialValue - Pre-filled text.
 * @param {string} [options.placeholder] - Shown when empty.
 * @param {(value: string) => void} options.onCommit
 * @param {() => void} options.onCancel
 * @returns {HTMLInputElement}
 */
function buildInlineInput({ initialValue, placeholder, onCommit, onCancel }) {
  const input = document.createElement("input");
  input.type = "text";
  input.className = "tree-edit-input";
  input.value = initialValue;

  if (placeholder) {
    input.placeholder = placeholder;
  }

  // Enter and blur both try to commit; guard against running the
  // commit/cancel callback twice (Enter fires first, then the
  // re-render it triggers removes this input, which fires blur).
  let settled = false;

  function resolve() {
    if (settled) {
      return;
    }

    settled = true;
    const value = input.value.trim();

    if (value) {
      onCommit(value);
    } else {
      onCancel();
    }
  }

  input.addEventListener("keydown", (event) => {
    event.stopPropagation();

    if (event.key === "Enter") {
      event.preventDefault();
      resolve();
    } else if (event.key === "Escape") {
      event.preventDefault();
      settled = true;
      onCancel();
    }
  });

  input.addEventListener("blur", resolve);
  input.addEventListener("click", (event) => event.stopPropagation());

  queueMicrotask(() => {
    input.focus();
    input.select();
  });

  return input;
}

/**
 * Create and wire up the file browser sidebar panel.
 *
 * @param {object} options
 * @param {Element} options.container - Element the tree renders into.
 * @param {(message: string, kind?: "info"|"success"|"error") => void} options.notify
 * @param {(path: string) => void} options.onOpenFile - Called when the
 *   user clicks a file row.
 * @param {(oldPath: string, newPath: string) => void} [options.onPathRenamed] -
 *   Called after a successful rename/move, so the caller can update
 *   any path it's tracking for the currently open file.
 * @param {(path: string) => void} [options.onPathDeleted] - Called
 *   after a successful delete, so the caller can clear state if the
 *   deleted path was (or contained) the currently open file.
 * @returns {{
 *   load: (projectId: string | null) => Promise<void>,
 *   setActiveFile: (path: string | null) => void,
 *   setSearchQuery: (query: string) => void,
 *   startCreateAtRoot: (type: "file"|"dir") => void,
 * }}
 */
export function createFileTree({
  container,
  notify,
  onOpenFile,
  onPathRenamed,
  onPathDeleted,
}) {
  if (!(container instanceof Element)) {
    throw new Error("createFileTree: container must be a DOM Element");
  }

  let projectId = null;
  let rootNode = null;
  let activePath = null;
  let searchQuery = "";
  const collapsedPaths = new Set();

  let editingPath = null;
  let draft = null; // { parentPath, type } | null

  let menuEl = null;
  let menuCloseHandler = null;

  let moveDialogEl = null;
  let moveDialogForm = null;
  let moveDialogSelect = null;
  let moveDialogLabel = null;

  // ─── Fetching ───

  async function fetchAndRender() {
    if (!projectId) {
      rootNode = null;
      render();
      return;
    }

    try {
      rootNode = await getFileTree(projectId);
    } catch (error) {
      rootNode = null;
      notify(`Could not load file tree: ${error.message}`, "error");
    }

    render();
  }

  async function load(newProjectId) {
    const changed = newProjectId !== projectId;
    projectId = newProjectId || null;
    draft = null;
    editingPath = null;
    closeContextMenu();

    if (changed) {
      activePath = null;
      collapsedPaths.clear();
      searchQuery = "";
    }

    await fetchAndRender();
  }

  function setActiveFile(path) {
    activePath = path || null;
    render();
  }

  function setSearchQuery(query) {
    searchQuery = (query || "").trim().toLowerCase();
    render();
  }

  // ─── Mutations ───

  function startCreate(parentPath, type) {
    collapsedPaths.delete(parentPath);
    editingPath = null;
    draft = { parentPath, type };
    render();
  }

  function startCreateAtRoot(type) {
    startCreate("", type);
  }

  function startRename(node) {
    draft = null;
    editingPath = node.path;
    render();
  }

  async function commitCreate(name) {
    const { parentPath, type } = draft;
    const path = joinPath(parentPath, name);

    try {
      await createEntry(projectId, path, type);
      draft = null;
      notify(`Created "${path}".`, "success");

      if (type === "file") {
        onOpenFile(path);
      }

      await fetchAndRender();
    } catch (error) {
      notify(`Could not create "${name}": ${error.message}`, "error");
      render();
    }
  }

  async function commitRename(node, name) {
    const newPath = joinPath(dirname(node.path), name);

    if (newPath === node.path) {
      editingPath = null;
      render();
      return;
    }

    try {
      await renameEntry(projectId, node.path, newPath);
      editingPath = null;
      notify(`Renamed to "${name}".`, "success");

      if (activePath === node.path) {
        activePath = newPath;
      }

      onPathRenamed?.(node.path, newPath);
      await fetchAndRender();
    } catch (error) {
      notify(`Could not rename: ${error.message}`, "error");
      render();
    }
  }

  async function handleDelete(node) {
    const isDir = node.type === "dir";
    const message = isDir
      ? `Delete folder "${node.path}" and everything inside it?`
      : `Delete "${node.path}"?`;

    if (!window.confirm(message)) {
      return;
    }

    try {
      await deleteFile(projectId, node.path);
      notify(`Deleted "${node.path}".`, "success");

      if (
        activePath === node.path ||
        (isDir && activePath && activePath.startsWith(`${node.path}/`))
      ) {
        activePath = null;
      }

      onPathDeleted?.(node.path);
      await fetchAndRender();
    } catch (error) {
      notify(`Could not delete: ${error.message}`, "error");
    }
  }

  // ─── Move-to dialog ───

  function ensureMoveDialog() {
    if (moveDialogEl) {
      return;
    }

    moveDialogEl = document.createElement("dialog");
    moveDialogForm = document.createElement("form");

    const heading = document.createElement("h2");
    heading.style.marginTop = "0";
    moveDialogLabel = document.createElement("span");
    heading.append('Move "', moveDialogLabel, '"');
    moveDialogForm.appendChild(heading);

    const formRow = createElement("div", "form-row");
    const label = createElement("label", null, "Destination folder");
    moveDialogSelect = document.createElement("select");
    formRow.appendChild(label);
    formRow.appendChild(moveDialogSelect);
    moveDialogForm.appendChild(formRow);

    const actions = createElement("div", "dialog-actions");
    const cancelButton = createElement("button", "btn", "Cancel");
    cancelButton.type = "button";
    cancelButton.addEventListener("click", () => moveDialogEl.close());
    const confirmButton = createElement("button", "btn btn-primary", "Move");
    confirmButton.type = "submit";
    actions.appendChild(cancelButton);
    actions.appendChild(confirmButton);
    moveDialogForm.appendChild(actions);

    moveDialogEl.appendChild(moveDialogForm);
    document.body.appendChild(moveDialogEl);
  }

  function openMoveDialog(node) {
    ensureMoveDialog();
    moveDialogLabel.textContent = node.path;

    const excludedPrefix = node.type === "dir" ? `${node.path}/` : null;
    const currentParent = dirname(node.path);

    const folders = [""];
    collectDirectoryPaths(rootNode, folders);

    moveDialogSelect.replaceChildren();

    for (const folderPath of folders) {
      if (folderPath === node.path) continue;
      if (excludedPrefix && folderPath.startsWith(excludedPrefix)) continue;
      if (folderPath === currentParent) continue;

      const option = document.createElement("option");
      option.value = folderPath;
      option.textContent = folderPath === "" ? "(project root)" : folderPath;
      moveDialogSelect.appendChild(option);
    }

    if (moveDialogSelect.options.length === 0) {
      notify("There is no other folder to move this into.", "info");
      return;
    }

    moveDialogForm.onsubmit = async (event) => {
      event.preventDefault();

      const destination = moveDialogSelect.value;
      const newPath = joinPath(destination, basename(node.path));
      moveDialogEl.close();

      try {
        await renameEntry(projectId, node.path, newPath);
        notify(`Moved to "${newPath}".`, "success");

        if (activePath === node.path) {
          activePath = newPath;
        }

        onPathRenamed?.(node.path, newPath);
        await fetchAndRender();
      } catch (error) {
        notify(`Could not move: ${error.message}`, "error");
      }
    };

    moveDialogEl.showModal();
  }

  // ─── Context menu ───

  function closeContextMenu() {
    if (menuEl) {
      menuEl.remove();
      menuEl = null;
    }

    if (menuCloseHandler) {
      document.removeEventListener("click", menuCloseHandler, true);
      document.removeEventListener("keydown", menuCloseHandler, true);
      menuCloseHandler = null;
    }
  }

  function buildMenuItems(node) {
    const items = [];

    if (node.type === "dir") {
      items.push({
        label: "New File Inside",
        onSelect: () => startCreate(node.path, "file"),
      });
      items.push({
        label: "New Folder Inside",
        onSelect: () => startCreate(node.path, "dir"),
      });
    }

    items.push({ label: "Rename", onSelect: () => startRename(node) });
    items.push({ label: "Move to...", onSelect: () => openMoveDialog(node) });
    items.push({
      label: "Delete",
      danger: true,
      onSelect: () => handleDelete(node),
    });

    return items;
  }

  function openContextMenu(event, node) {
    closeContextMenu();

    menuEl = document.createElement("ul");
    menuEl.className = "context-menu";
    menuEl.setAttribute("role", "menu");

    for (const item of buildMenuItems(node)) {
      const li = document.createElement("li");
      const button = createElement(
        "button",
        item.danger ? "context-menu-item danger" : "context-menu-item",
        item.label,
      );
      button.type = "button";
      button.setAttribute("role", "menuitem");
      button.addEventListener("click", () => {
        closeContextMenu();
        item.onSelect();
      });
      li.appendChild(button);
      menuEl.appendChild(li);
    }

    document.body.appendChild(menuEl);

    const menuRect = menuEl.getBoundingClientRect();
    const maxX = window.innerWidth - menuRect.width - 4;
    const maxY = window.innerHeight - menuRect.height - 4;
    menuEl.style.left = `${Math.max(4, Math.min(event.clientX, maxX))}px`;
    menuEl.style.top = `${Math.max(4, Math.min(event.clientY, maxY))}px`;

    menuCloseHandler = (closeEvent) => {
      if (closeEvent.type === "keydown" && closeEvent.key !== "Escape") {
        return;
      }

      if (closeEvent.type === "click" && menuEl?.contains(closeEvent.target)) {
        return;
      }

      closeContextMenu();
    };

    // Deferred so the click/contextmenu event that opened the menu
    // doesn't immediately bubble into this same listener and close it.
    setTimeout(() => {
      document.addEventListener("click", menuCloseHandler, true);
      document.addEventListener("keydown", menuCloseHandler, true);
    }, 0);
  }

  // ─── Rendering ───

  function renderDraftRow() {
    const li = document.createElement("li");
    const row = createElement("div", "tree-row");
    row.appendChild(createElement("span", "tree-spacer"));
    row.appendChild(
      createElement("span", "tree-icon", draft.type === "dir" ? "📁" : "📄"),
    );

    const input = buildInlineInput({
      initialValue: "",
      placeholder: draft.type === "dir" ? "New folder name" : "New file name",
      onCommit: commitCreate,
      onCancel: () => {
        draft = null;
        render();
      },
    });
    row.appendChild(input);
    li.appendChild(row);
    return li;
  }

  function renderNode(node) {
    const searchActive = searchQuery.length > 0;

    if (searchActive && !nodeMatchesQuery(node, searchQuery)) {
      return null;
    }

    const li = document.createElement("li");
    const row = createElement("div", "tree-row");
    row.dataset.path = node.path;

    if (node.path === activePath) {
      row.classList.add("active");
    }

    const isDir = node.type === "dir";
    const expanded = searchActive || !collapsedPaths.has(node.path);

    row.appendChild(
      createElement("span", "tree-chevron", isDir ? (expanded ? "▾" : "▸") : ""),
    );
    row.appendChild(createElement("span", "tree-icon", isDir ? "📁" : "📄"));

    if (editingPath === node.path) {
      const input = buildInlineInput({
        initialValue: basename(node.path),
        onCommit: (name) => commitRename(node, name),
        onCancel: () => {
          editingPath = null;
          render();
        },
      });
      row.appendChild(input);
    } else {
      row.appendChild(createElement("span", "tree-label", basename(node.path)));
    }

    const menuButton = createElement("button", "tree-menu-btn", "⋮");
    menuButton.type = "button";
    menuButton.title = "Actions";
    menuButton.addEventListener("click", (event) => {
      event.stopPropagation();
      openContextMenu(event, node);
    });
    row.appendChild(menuButton);

    row.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      event.stopPropagation();
      openContextMenu(event, node);
    });

    row.addEventListener("click", () => {
      if (editingPath === node.path) {
        return;
      }

      if (isDir) {
        collapsedPaths.has(node.path)
          ? collapsedPaths.delete(node.path)
          : collapsedPaths.add(node.path);
        render();
      } else {
        onOpenFile(node.path);
      }
    });

    li.appendChild(row);

    if (isDir) {
      const draftHere = draft && draft.parentPath === node.path;

      if (expanded || draftHere) {
        const childList = document.createElement("ul");
        childList.className = "tree-node";

        if (draftHere) {
          childList.appendChild(renderDraftRow());
        }

        for (const child of node.children || []) {
          const childLi = renderNode(child);
          if (childLi) {
            childList.appendChild(childLi);
          }
        }

        if (childList.children.length > 0) {
          li.appendChild(childList);
        }
      }
    }

    return li;
  }

  function render() {
    container.replaceChildren();

    if (!rootNode) {
      container.appendChild(
        createElement("p", "empty-state", "No project loaded yet."),
      );
      return;
    }

    const rootDraft = draft && draft.parentPath === "";
    const rootList = document.createElement("ul");
    rootList.className = "tree-node root";

    if (rootDraft) {
      rootList.appendChild(renderDraftRow());
    }

    let hasAnyRow = Boolean(rootDraft);

    for (const child of rootNode.children || []) {
      const li = renderNode(child);

      if (li) {
        rootList.appendChild(li);
        hasAnyRow = true;
      }
    }

    if (!hasAnyRow) {
      const message =
        searchQuery.length > 0
          ? "No files match your search."
          : "This project has no files yet.";
      container.appendChild(createElement("p", "empty-state", message));
      return;
    }

    container.appendChild(rootList);
  }

  render();

  return { load, setActiveFile, setSearchQuery, startCreateAtRoot };
}
