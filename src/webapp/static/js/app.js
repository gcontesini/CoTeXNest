/**
 * app.js
 *
 * Entry point for the LaTeX Writer single-page app. Wires together
 * the project selector, file tree sidebar, CodeMirror editor, PDF
 * preview, and the History/Packages slide-in panels on top of the
 * REST API in api.js.
 *
 * ─── Network / offline notice (read this before deploying air-gapped) ───
 *
 * This project deliberately has no Node/npm build step: CodeMirror 6
 * and pdf.js are loaded straight in the browser as real ES modules
 * from esm.sh (https://esm.sh/<package>@<version>), wired up via the
 * <script type="importmap"> block in index.html. That means:
 *
 *   1. The *first* time this app is opened on a given machine, the
 *      browser needs an internet connection to fetch those modules.
 *   2. After that first load, the browser's normal HTTP cache keeps
 *      them available, so subsequent loads on the same machine/
 *      browser profile work without a network round-trip (subject to
 *      normal cache eviction).
 *   3. A fully air-gapped deployment (no internet access, ever, even
 *      on first run) is NOT supported by this setup as-is. Doing
 *      that would require vendoring these libraries' ESM builds
 *      locally under static/vendor/ and rewriting the import map in
 *      index.html to point at those local files instead of esm.sh.
 *      That vendoring is a reasonable future improvement, but it has
 *      NOT been done here -- do not describe this app as a
 *      "fully offline solution" in user-facing docs.
 *
 * Doc-writing agent: please carry this caveat into the deployment/
 * install docs verbatim rather than summarizing it as "works
 * offline".
 */

import * as api from "./api.js";
import { createEditor } from "./editor.js";
import { renderPdf, showCompileError, clearPreview } from "./preview.js";
import { createHistoryPanel } from "./history-panel.js";
import { createPackagesPanel } from "./packages-panel.js";

// ─── DOM references ───

const projectSelect = document.getElementById("project-select");
const btnNewProject = document.getElementById("btn-new-project");
const btnDeleteProject = document.getElementById("btn-delete-project");
const btnCompile = document.getElementById("btn-compile");
const btnSaveVersion = document.getElementById("btn-save-version");
const btnHistory = document.getElementById("btn-history");
const btnPackages = document.getElementById("btn-packages");

const sidebar = document.getElementById("sidebar");
const btnCollapseSidebar = document.getElementById("btn-collapse-sidebar");
const fileTreeContainer = document.getElementById("file-tree");
const newFileInput = document.getElementById("new-file-input");
const btnNewFile = document.getElementById("btn-new-file");

const editorContainer = document.getElementById("editor-container");
const currentFileLabel = document.getElementById("current-file-label");
const saveStatus = document.getElementById("save-status");

const previewContainer = document.getElementById("preview-container");
const compileStatus = document.getElementById("compile-status");

const paneDivider = document.getElementById("pane-divider");
const editorPane = document.getElementById("editor-pane");
const previewPane = document.getElementById("preview-pane");

const panelOverlay = document.getElementById("panel-overlay");
const historyPanelEl = document.getElementById("history-panel");
const historyPanelBody = document.getElementById("history-panel-body");
const btnCloseHistory = document.getElementById("btn-close-history");
const packagesPanelEl = document.getElementById("packages-panel");
const packagesPanelBody = document.getElementById("packages-panel-body");
const btnClosePackages = document.getElementById("btn-close-packages");

const newProjectDialog = document.getElementById("new-project-dialog");
const newProjectForm = document.getElementById("new-project-form");
const newProjectNameInput = document.getElementById("new-project-name");
const newProjectTemplateSelect = document.getElementById("new-project-template");
const btnCancelNewProject = document.getElementById("btn-cancel-new-project");

const toastContainer = document.getElementById("toast-container");

// ─── App state ───

/** @type {Map<string, object>} project id -> ProjectSummary */
let projectsById = new Map();
let currentProjectId = null;
let currentFilePath = null;
let editor = null;

const AUTHOR_NAME_STORAGE_KEY = "latex-writer:author-name";

// ─── Toasts ───

/**
 * Show a dismissible banner/toast so errors and confirmations are
 * always visible, never silent console-only failures.
 *
 * @param {string} message - Text to display.
 * @param {"info"|"success"|"error"} [kind] - Visual style.
 */
function notify(message, kind = "info") {
  const toast = document.createElement("div");
  toast.className = `toast toast-${kind}`;

  const text = document.createElement("span");
  text.textContent = message;
  toast.appendChild(text);

  const closeButton = document.createElement("button");
  closeButton.className = "toast-close";
  closeButton.type = "button";
  closeButton.textContent = "✕";
  closeButton.addEventListener("click", () => toast.remove());
  toast.appendChild(closeButton);

  toastContainer.appendChild(toast);

  if (kind !== "error") {
    setTimeout(() => toast.remove(), 6000);
  }

  if (kind === "error") {
    console.error(`[latex-writer] ${message}`);
  }
}

function getAuthorName() {
  let author = window.localStorage.getItem(AUTHOR_NAME_STORAGE_KEY);

  if (!author) {
    author = window.prompt("Your name (shown on saved versions):", "anonymous") || "anonymous";
    window.localStorage.setItem(AUTHOR_NAME_STORAGE_KEY, author);
  }

  return author;
}

// ─── Panels ───

const historyPanel = createHistoryPanel({
  container: historyPanelBody,
  getProjectId: () => currentProjectId,
  getAuthorName,
  notify,
  onReverted: async () => {
    await openProject(currentProjectId);
  },
});

const packagesPanel = createPackagesPanel({
  container: packagesPanelBody,
  notify,
});

function closeAllPanels() {
  historyPanelEl.classList.remove("open");
  packagesPanelEl.classList.remove("open");
  panelOverlay.classList.remove("open");
}

async function openHistoryPanel({ focusComposer = false } = {}) {
  packagesPanelEl.classList.remove("open");
  historyPanelEl.classList.add("open");
  panelOverlay.classList.add("open");
  await historyPanel.refresh();

  if (focusComposer) {
    const textarea = historyPanelBody.querySelector("textarea");
    textarea?.focus();
  }
}

async function openPackagesPanel() {
  historyPanelEl.classList.remove("open");
  packagesPanelEl.classList.add("open");
  panelOverlay.classList.add("open");
  await packagesPanel.refresh();
}

btnHistory.addEventListener("click", () => openHistoryPanel());
btnSaveVersion.addEventListener("click", () => openHistoryPanel({ focusComposer: true }));
btnPackages.addEventListener("click", () => openPackagesPanel());
btnCloseHistory.addEventListener("click", closeAllPanels);
btnClosePackages.addEventListener("click", closeAllPanels);
panelOverlay.addEventListener("click", closeAllPanels);

// ─── Project selector / creation / deletion ───

async function loadProjects({ selectId } = {}) {
  let projects;

  try {
    projects = await api.listProjects();
  } catch (error) {
    notify(`Could not load projects: ${error.message}`, "error");
    return;
  }

  projectsById = new Map((projects || []).map((project) => [project.id, project]));
  projectSelect.replaceChildren();

  if (projectsById.size === 0) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No projects yet — create one";
    projectSelect.appendChild(option);
    btnDeleteProject.classList.add("hidden");
    await openProject(null);
    return;
  }

  for (const project of projectsById.values()) {
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = project.name;
    projectSelect.appendChild(option);
  }

  const targetId =
    (selectId && projectsById.has(selectId) && selectId) ||
    (currentProjectId && projectsById.has(currentProjectId) && currentProjectId) ||
    projectsById.keys().next().value;

  projectSelect.value = targetId;
  btnDeleteProject.classList.remove("hidden");
  await openProject(targetId);
}

projectSelect.addEventListener("change", () => {
  openProject(projectSelect.value || null);
});

btnNewProject.addEventListener("click", async () => {
  newProjectNameInput.value = "";
  newProjectTemplateSelect.replaceChildren();

  const blankOption = document.createElement("option");
  blankOption.value = "";
  blankOption.textContent = "Blank project";
  newProjectTemplateSelect.appendChild(blankOption);

  try {
    const templates = await api.searchCatalog(null, "template");

    for (const template of templates || []) {
      const option = document.createElement("option");
      option.value = template.id;
      option.textContent = template.name;
      newProjectTemplateSelect.appendChild(option);
    }
  } catch (error) {
    notify(`Could not load templates: ${error.message}`, "error");
  }

  newProjectDialog.showModal();
});

btnCancelNewProject.addEventListener("click", () => newProjectDialog.close());

newProjectForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  const name = newProjectNameInput.value.trim();
  const templateId = newProjectTemplateSelect.value || null;

  if (!name) {
    notify("Enter a project name.", "error");
    return;
  }

  try {
    const created = await api.createProject(name, templateId);
    newProjectDialog.close();
    notify(`Project "${name}" created.`, "success");
    await loadProjects({ selectId: created.id });
  } catch (error) {
    notify(`Could not create project: ${error.message}`, "error");
  }
});

btnDeleteProject.addEventListener("click", async () => {
  if (!currentProjectId) {
    return;
  }

  const project = projectsById.get(currentProjectId);
  const confirmed = window.confirm(
    `Delete project "${project?.name ?? currentProjectId}"? This cannot be undone.`,
  );

  if (!confirmed) {
    return;
  }

  try {
    await api.deleteProject(currentProjectId);
    notify("Project deleted.", "success");
    currentProjectId = null;
    await loadProjects();
  } catch (error) {
    notify(`Could not delete project: ${error.message}`, "error");
  }
});

// ─── File tree ───

function flattenTreeRoots(rawTree) {
  if (Array.isArray(rawTree)) {
    return rawTree;
  }

  if (rawTree && typeof rawTree === "object") {
    return Array.isArray(rawTree.children) ? rawTree.children : [rawTree];
  }

  return [];
}

function renderFileTree(rawTree) {
  fileTreeContainer.replaceChildren();

  const roots = flattenTreeRoots(rawTree);

  if (roots.length === 0) {
    fileTreeContainer.appendChild(
      Object.assign(document.createElement("p"), {
        className: "empty-state",
        textContent: "This project has no files yet.",
      }),
    );
    return;
  }

  const rootList = document.createElement("ul");
  rootList.className = "tree-node root";

  for (const node of roots) {
    rootList.appendChild(renderTreeNode(node));
  }

  fileTreeContainer.appendChild(rootList);
}

function renderTreeNode(node) {
  const listItem = document.createElement("li");

  const row = document.createElement("div");
  row.className = "tree-row";
  row.dataset.path = node.path;

  const label = document.createElement("span");
  label.className = "tree-label";
  label.textContent = (node.type === "dir" ? "📁 " : "📄 ") + node.path.split("/").pop();
  row.appendChild(label);

  if (node.type !== "dir") {
    const deleteButton = document.createElement("button");
    deleteButton.className = "tree-delete";
    deleteButton.type = "button";
    deleteButton.title = "Delete file";
    deleteButton.textContent = "✕";
    deleteButton.addEventListener("click", async (event) => {
      event.stopPropagation();
      const confirmed = window.confirm(`Delete "${node.path}"?`);

      if (!confirmed) {
        return;
      }

      try {
        await api.deleteFile(currentProjectId, node.path);
        notify(`Deleted "${node.path}".`, "success");
        await refreshFileTree();
      } catch (error) {
        notify(`Could not delete file: ${error.message}`, "error");
      }
    });
    row.appendChild(deleteButton);

    row.addEventListener("click", () => openFile(node.path));
  }

  if (node.path === currentFilePath) {
    row.classList.add("active");
  }

  listItem.appendChild(row);

  if (Array.isArray(node.children) && node.children.length > 0) {
    const childList = document.createElement("ul");
    childList.className = "tree-node";

    for (const child of node.children) {
      childList.appendChild(renderTreeNode(child));
    }

    listItem.appendChild(childList);
  }

  return listItem;
}

async function refreshFileTree() {
  if (!currentProjectId) {
    renderFileTree([]);
    return;
  }

  try {
    const tree = await api.getFileTree(currentProjectId);
    renderFileTree(tree);
  } catch (error) {
    notify(`Could not load file tree: ${error.message}`, "error");
  }
}

btnNewFile.addEventListener("click", async () => {
  const path = newFileInput.value.trim();

  if (!currentProjectId) {
    notify("Open a project before creating a file.", "error");
    return;
  }

  if (!path) {
    notify("Enter a file path, e.g. chapters/intro.tex", "error");
    return;
  }

  try {
    await api.saveFile(currentProjectId, path, "");
    newFileInput.value = "";
    notify(`Created "${path}".`, "success");
    await refreshFileTree();
    await openFile(path);
  } catch (error) {
    notify(`Could not create file: ${error.message}`, "error");
  }
});

btnCollapseSidebar.addEventListener("click", () => {
  sidebar.classList.toggle("collapsed");
});

// ─── Editor + autosave + auto-compile ───

/**
 * Load a file's content into the editor.
 *
 * @param {string} path - Project-relative file path to open.
 * @returns {Promise<boolean>} True if the file was opened successfully.
 */
async function openFile(path) {
  if (!currentProjectId) {
    return false;
  }

  let fileContent;

  try {
    fileContent = await api.getFile(currentProjectId, path);
  } catch (error) {
    notify(`Could not open "${path}": ${error.message}`, "error");
    return false;
  }

  currentFilePath = path;
  currentFileLabel.textContent = path;

  fileTreeContainer.querySelectorAll(".tree-row").forEach((row) => {
    row.classList.toggle("active", row.dataset.path === path);
  });

  if (editor) {
    editor.setValue(fileContent.content ?? "");
  } else {
    mountEditor(fileContent.content ?? "");
  }

  return true;
}

function mountEditor(initialText) {
  editor = createEditor({
    parent: editorContainer,
    doc: initialText,
    onChange: handleEditorChange,
  });
}

async function handleEditorChange(text) {
  if (!currentProjectId || !currentFilePath) {
    return;
  }

  saveStatus.textContent = "Saving…";

  try {
    await api.saveFile(currentProjectId, currentFilePath, text);
    saveStatus.textContent = "Saved";
    setTimeout(() => {
      if (saveStatus.textContent === "Saved") {
        saveStatus.textContent = "";
      }
    }, 2000);
  } catch (error) {
    saveStatus.textContent = "";
    notify(`Could not save file: ${error.message}`, "error");
    return;
  }

  await compileCurrentProject({ silent: true });
}

// ─── Compilation ───

async function compileCurrentProject({ silent = false } = {}) {
  if (!currentProjectId) {
    return;
  }

  const project = projectsById.get(currentProjectId);
  compileStatus.textContent = "Compiling…";

  try {
    const result = await api.compile(currentProjectId, project?.main_file ?? null);
    compileStatus.textContent = `${result.success ? "Success" : "Failed"} (${result.duration_ms} ms)`;

    if (result.success) {
      await renderPdf(previewContainer, result.pdf_base64);

      if (!silent) {
        notify("Compiled successfully.", "success");
      }
    } else {
      showCompileError(previewContainer, result.log);

      if (!silent) {
        notify("Compile failed — see log in the preview pane.", "error");
      }
    }
  } catch (error) {
    compileStatus.textContent = "";
    clearPreview(previewContainer);
    showCompileError(previewContainer, error.message);
    notify(`Compile request failed: ${error.message}`, "error");
  }
}

btnCompile.addEventListener("click", () => compileCurrentProject());

// ─── Opening a project ───

async function openProject(projectId) {
  closeAllPanels();

  if (editor) {
    editor.destroy();
    editor = null;
  }

  // Only keep the currently-open file path when re-opening the SAME
  // project (e.g. right after a revert) -- a file path from a
  // different project is meaningless here and would just 404.
  const isSameProject = Boolean(projectId) && projectId === currentProjectId;

  if (!isSameProject) {
    currentFilePath = null;
  }

  currentProjectId = projectId || null;

  editorContainer.replaceChildren();
  currentFileLabel.textContent = "No file open";
  saveStatus.textContent = "";
  compileStatus.textContent = "";
  clearPreview(previewContainer);

  if (!currentProjectId) {
    renderFileTree([]);

    const placeholder = document.createElement("p");
    placeholder.className = "preview-empty";
    placeholder.textContent = "Create a project to get started.";
    previewContainer.appendChild(placeholder);
    return;
  }

  await refreshFileTree();

  const project = projectsById.get(currentProjectId);
  const mainFile = project?.main_file || "main.tex";
  const preferredFile = currentFilePath || mainFile;

  const opened = await openFile(preferredFile);

  if (!opened && preferredFile !== mainFile) {
    await openFile(mainFile);
  }

  await compileCurrentProject({ silent: true });
}

// ─── Resizable editor/preview divider ───

(function setUpDivider() {
  let isDragging = false;

  paneDivider.addEventListener("mousedown", (event) => {
    isDragging = true;
    paneDivider.classList.add("dragging");
    event.preventDefault();
  });

  window.addEventListener("mousemove", (event) => {
    if (!isDragging) {
      return;
    }

    const workspace = paneDivider.parentElement;
    const bounds = workspace.getBoundingClientRect();
    const ratio = Math.min(
      0.85,
      Math.max(0.15, (event.clientX - bounds.left) / bounds.width),
    );

    editorPane.style.flex = `${ratio} 1 0`;
    previewPane.style.flex = `${1 - ratio} 1 0`;
  });

  window.addEventListener("mouseup", () => {
    if (isDragging) {
      isDragging = false;
      paneDivider.classList.remove("dragging");
    }
  });
})();

// ─── Boot ───

await loadProjects();
