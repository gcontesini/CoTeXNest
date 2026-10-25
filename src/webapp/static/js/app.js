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
import { createFileTree } from "./file-tree.js";
import { createAssistantPanel } from "./assistant-panel.js";
import {
  applyAppearanceSettings,
  getEditorSettings,
  createSettingsModal,
  AUTHOR_NAME_STORAGE_KEY,
} from "./settings-modal.js";
import { createShortcutsDialog } from "./shortcuts-dialog.js";

// Applied as early as possible (before the project list even loads)
// to avoid a flash of the wrong theme/font on page load.
applyAppearanceSettings();

// ─── DOM references ───

const projectSelect = document.getElementById("project-select");
const btnNewProject = document.getElementById("btn-new-project");
const btnCompile = document.getElementById("btn-compile");
const btnSaveVersion = document.getElementById("btn-save-version");
const btnHistory = document.getElementById("btn-history");
const menuPackagesTrigger = document.getElementById("menu-packages-trigger");
const menuSettingsTrigger = document.getElementById("menu-settings-trigger");
const settingsDialog = document.getElementById("settings-dialog");
const settingsPanelBody = document.getElementById("settings-panel-body");
const settingsDialogTitle = document.getElementById("settings-dialog-title");
const btnCloseSettings = document.getElementById("btn-close-settings");
const btnSyncChanges = document.getElementById("btn-sync-changes");
const btnLogFiles = document.getElementById("btn-log-files");

const syncCommitMessageDialog = document.getElementById("sync-commit-message-dialog");
const syncCommitMessageForm = document.getElementById("sync-commit-message-form");
const syncCommitMessageInput = document.getElementById("sync-commit-message-input");
const syncChangedFilesList = document.getElementById("sync-changed-files-list");
const btnCancelSyncCommitMessage = document.getElementById(
  "btn-cancel-sync-commit-message",
);

const sidebar = document.getElementById("sidebar");
const btnCollapseSidebar = document.getElementById("btn-collapse-sidebar");
const fileTreeContainer = document.getElementById("file-tree");
const fileSearchRow = document.getElementById("file-search-row");
const fileSearchInput = document.getElementById("file-search");
const btnToggleFileSearch = document.getElementById("btn-toggle-file-search");
const btnNewFileRoot = document.getElementById("btn-new-file-root");
const btnNewFolderRoot = document.getElementById("btn-new-folder-root");

const editorContainer = document.getElementById("editor-container");
const currentFileLabel = document.getElementById("current-file-label");
const saveStatus = document.getElementById("save-status");
const btnCollapseEditorPane = document.getElementById("btn-collapse-editor-pane");

const previewContainer = document.getElementById("preview-container");
const compileStatus = document.getElementById("compile-status");
const btnCollapsePreviewPane = document.getElementById("btn-collapse-preview-pane");

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
const logFilesPanelEl = document.getElementById("log-files-panel");
const logFilesPanelBody = document.getElementById("log-files-panel-body");
const btnCloseLogFiles = document.getElementById("btn-close-log-files");
const btnClearLogFiles = document.getElementById("btn-clear-log-files");

const btnAssistant = document.getElementById("btn-assistant");
const assistantDock = document.getElementById("assistant-dock");
const btnCloseAssistant = document.getElementById("btn-close-assistant");
const btnMinimizeAssistant = document.getElementById("btn-minimize-assistant");
const btnMaximizeAssistant = document.getElementById("btn-maximize-assistant");
// Owned by the Settings dialog (Settings → AI Assistant); detached
// until that category is opened.
const assistantSettingsView = document.createElement("div");
assistantSettingsView.className = "assistant-settings-host";
const assistantChatLog = document.getElementById("assistant-chat-log");
const assistantChatForm = document.getElementById("assistant-chat-form");
const assistantChatInput = document.getElementById("assistant-chat-input");

const newProjectDialog = document.getElementById("new-project-dialog");
const newProjectForm = document.getElementById("new-project-form");
const newProjectNameInput = document.getElementById("new-project-name");
const newProjectTemplateSelect = document.getElementById("new-project-template");
const newProjectRemoteUrlInput = document.getElementById("new-project-remote-url");
const btnCancelNewProject = document.getElementById("btn-cancel-new-project");

const deleteProjectDialog = document.getElementById("delete-project-dialog");
const deleteProjectForm = document.getElementById("delete-project-form");
const deleteProjectNameLabel = document.getElementById("delete-project-name");
const deleteProjectConfirmInput = document.getElementById("delete-project-confirm-input");
const btnCancelDeleteProject = document.getElementById("btn-cancel-delete-project");
const btnConfirmDeleteProject = document.getElementById("btn-confirm-delete-project");

const toastContainer = document.getElementById("toast-container");

const statusCharCount = document.getElementById("status-char-count");
const statusWordCount = document.getElementById("status-word-count");
const statusLineCount = document.getElementById("status-line-count");

const quitDialog = document.getElementById("quit-dialog");
const btnCloseQuitDialog = document.getElementById("btn-close-quit-dialog");

// Not yet published -- see docs/superpowers/specs for the Sphinx + GitHub
// Pages documentation task. Fill this in with the real GitHub Pages URL
// once that site is live and this repo has a GitHub remote.
const DOCUMENTATION_URL = "";

// ─── App state ───

/** @type {Map<string, object>} project id -> ProjectSummary */
let projectsById = new Map();
let currentProjectId = null;
let currentFilePath = null;
let editor = null;

// ─── Toasts ───

const TOAST_DISMISS_MS = 20000;

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

  setTimeout(() => toast.remove(), TOAST_DISMISS_MS);

  if (kind === "error") {
    console.error(`[latex-writer] ${message}`);
  }
}

// ─── Menu bar ───

const MENU_SPECS = [
  { itemId: "menu-file-trigger", dropdownId: "menu-file-dropdown" },
  { itemId: "menu-view-trigger", dropdownId: "menu-view-dropdown" },
  { itemId: "menu-help-trigger", dropdownId: "menu-help-dropdown" },
];

const menus = MENU_SPECS.map(({ itemId, dropdownId }) => {
  const trigger = document.getElementById(itemId);
  const dropdown = document.getElementById(dropdownId);
  return { trigger, dropdown, container: trigger.closest(".menubar-item") };
});

function closeAllMenus() {
  for (const menu of menus) {
    menu.dropdown.classList.add("hidden");
    menu.container.classList.remove("open");
    menu.trigger.setAttribute("aria-expanded", "false");
  }
}

function openMenu(menu) {
  closeAllMenus();
  updateViewMenuChecks();
  menu.dropdown.classList.remove("hidden");
  menu.container.classList.add("open");
  menu.trigger.setAttribute("aria-expanded", "true");
}

for (const menu of menus) {
  menu.trigger.addEventListener("click", (event) => {
    event.stopPropagation();
    const isOpen = !menu.dropdown.classList.contains("hidden");
    isOpen ? closeAllMenus() : openMenu(menu);
  });

  menu.dropdown.addEventListener("click", (event) => {
    if (event.target.closest("button, a")) {
      closeAllMenus();
    }
  });
}

document.addEventListener("click", closeAllMenus);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeAllMenus();
  }
});

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

const assistantPanel = createAssistantPanel({
  chatLogContainer: assistantChatLog,
  chatForm: assistantChatForm,
  chatInput: assistantChatInput,
  settingsContainer: assistantSettingsView,
  notify,
  getProjectId: () => currentProjectId,
  getCurrentFilePath: () => currentFilePath,
  getCurrentEditorContent: () => (editor ? editor.getValue() : null),
  onProposalApplied: (proposal) => handleAssistantProposalApplied(proposal),
  onError: (message) => logEntry("assistant", message),
});

/**
 * After the assistant applies a change, the file tree and (if it
 * touched the currently-open file) the editor are now stale -- the
 * change happened on disk, not through the editor's own autosave.
 *
 * @param {object} proposal - The applied AssistantProposal.
 */
async function handleAssistantProposalApplied(proposal) {
  await fileTree.load(currentProjectId);

  const affectedPaths = [proposal.path, proposal.new_path].filter(Boolean);

  if (!currentFilePath || !affectedPaths.includes(currentFilePath)) {
    return;
  }

  if (proposal.action === "delete_entry") {
    handleFileTreePathDeleted(currentFilePath);
  } else {
    await openFile(proposal.new_path || currentFilePath);
  }
}

btnAssistant.addEventListener("click", () => {
  assistantDock.classList.toggle("hidden");
});
btnCloseAssistant.addEventListener("click", () => {
  assistantDock.classList.add("hidden");
});

// ─── Settings dialog (General/Editor/AI Assistant/Dictionary/Git) ───

const settingsModal = createSettingsModal({
  dialogEl: settingsDialog,
  bodyEl: settingsPanelBody,
  titleEl: settingsDialogTitle,
  getEditor: () => editor,
  getProjectId: () => currentProjectId,
  notify,
  assistantSettingsEl: assistantSettingsView,
  renderAssistantSettings: () => assistantPanel.refreshSettings(),
  onProjectFilesChanged: (paths) => handleProjectFilesChanged(paths),
});

/**
 * Settings changed files on disk (the custom-commands toggle and
 * save), not through the editor: refresh the tree and reload the open
 * file if it was one of them.
 *
 * @param {string[]} paths - Project-relative paths that changed.
 */
async function handleProjectFilesChanged(paths) {
  if (!currentProjectId) {
    return;
  }

  await fileTree.load(currentProjectId);

  if (currentFilePath && paths.includes(currentFilePath)) {
    await openFile(currentFilePath);
  }
}

menuSettingsTrigger.addEventListener("click", () => settingsModal.open());
btnCloseSettings.addEventListener("click", () => settingsDialog.close());

// ─── Assistant dock minimize/maximize ───

const ASSISTANT_DOCK_DEFAULT_HEIGHT = "320px";
const ASSISTANT_DOCK_MINIMIZED_HEIGHT = "2.75rem";

// The height to go back to when un-minimizing/un-maximizing -- whatever
// it was before (including a size the user set by dragging the resize
// handle), not always the default.
let assistantDockRestoreHeight = null;

function setAssistantDockMode(mode) {
  const isMinimized = assistantDock.classList.contains("minimized");
  const isMaximized = assistantDock.classList.contains("maximized");
  const alreadyInMode = (mode === "minimized" && isMinimized) || (mode === "maximized" && isMaximized);

  assistantDock.classList.remove("minimized", "maximized");

  if (alreadyInMode) {
    assistantDock.style.height = assistantDockRestoreHeight || ASSISTANT_DOCK_DEFAULT_HEIGHT;
    assistantDockRestoreHeight = null;
    return;
  }

  if (!isMinimized && !isMaximized) {
    assistantDockRestoreHeight =
      assistantDock.style.height || `${assistantDock.getBoundingClientRect().height}px`;
  }

  if (mode === "minimized") {
    assistantDock.classList.add("minimized");
    assistantDock.style.height = ASSISTANT_DOCK_MINIMIZED_HEIGHT;
  } else {
    assistantDock.classList.add("maximized");
    assistantDock.style.height = `${window.innerHeight * 0.85}px`;
  }
}

btnMinimizeAssistant.addEventListener("click", () => setAssistantDockMode("minimized"));
btnMaximizeAssistant.addEventListener("click", () => setAssistantDockMode("maximized"));

// ─── File tree ───

/**
 * Keep the currently-open file's tracked path in sync with a rename
 * or move that happened to it (or to an ancestor folder of it) in the
 * sidebar, so the next autosave doesn't write to a now-stale path.
 *
 * @param {string} oldPath
 * @param {string} newPath
 */
function handleFileTreePathRenamed(oldPath, newPath) {
  if (!currentFilePath) {
    return;
  }

  if (currentFilePath === oldPath) {
    currentFilePath = newPath;
    currentFileLabel.textContent = newPath;
  } else if (currentFilePath.startsWith(`${oldPath}/`)) {
    currentFilePath = newPath + currentFilePath.slice(oldPath.length);
    currentFileLabel.textContent = currentFilePath;
  }
}

/**
 * Close the editor if the currently-open file (or an ancestor folder
 * of it) was just deleted in the sidebar.
 *
 * @param {string} path
 */
function handleFileTreePathDeleted(path) {
  if (!currentFilePath) {
    return;
  }

  if (currentFilePath === path || currentFilePath.startsWith(`${path}/`)) {
    currentFilePath = null;
    currentFileLabel.textContent = "No file open";
    saveStatus.textContent = "";

    if (editor) {
      editor.destroy();
      editor = null;
    }

    editorContainer.replaceChildren();
  }
}

const fileTree = createFileTree({
  container: fileTreeContainer,
  notify,
  onOpenFile: (path) => openFile(path),
  onPathRenamed: handleFileTreePathRenamed,
  onPathDeleted: handleFileTreePathDeleted,
});

fileSearchInput.addEventListener("input", () => {
  fileTree.setSearchQuery(fileSearchInput.value);
});

btnToggleFileSearch.addEventListener("click", () => {
  const isHidden = fileSearchRow.classList.toggle("hidden");

  if (isHidden) {
    fileSearchInput.value = "";
    fileTree.setSearchQuery("");
  } else {
    fileSearchInput.focus();
  }
});

btnNewFileRoot.addEventListener("click", () => {
  if (!currentProjectId) {
    notify("Open a project before creating a file.", "error");
    return;
  }

  fileTree.startCreateAtRoot("file");
});

btnNewFolderRoot.addEventListener("click", () => {
  if (!currentProjectId) {
    notify("Open a project before creating a folder.", "error");
    return;
  }

  fileTree.startCreateAtRoot("dir");
});

// ─── Log Files (session-only record of compile + assistant errors) ───

const LOG_FILES_MAX_ENTRIES = 200;

/** @type {Array<{timestamp: number, source: "compile"|"assistant", message: string}>} */
let errorLogEntries = [];

/**
 * Record a compile or assistant error for the Log Files panel.
 *
 * This is intentionally session-only (an in-memory array, not
 * persisted) -- it's a diagnostic aid for "what just went wrong while
 * I was working", not project state, so it doesn't need a backend
 * endpoint or to survive a page reload.
 *
 * @param {"compile"|"assistant"} source
 * @param {string} message
 */
function logEntry(source, message) {
  errorLogEntries.unshift({ timestamp: Date.now(), source, message });

  if (errorLogEntries.length > LOG_FILES_MAX_ENTRIES) {
    errorLogEntries.length = LOG_FILES_MAX_ENTRIES;
  }

  if (logFilesPanelEl.classList.contains("open")) {
    renderLogFiles();
  }
}

function renderLogFiles() {
  logFilesPanelBody.replaceChildren();

  if (errorLogEntries.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No compilation or assistant errors yet this session.";
    logFilesPanelBody.appendChild(empty);
    return;
  }

  for (const entry of errorLogEntries) {
    const row = document.createElement("div");
    row.className = `log-entry log-entry-${entry.source}`;

    const meta = document.createElement("div");
    meta.className = "log-entry-meta";
    const badge = document.createElement("span");
    badge.className = "log-entry-badge";
    badge.textContent = entry.source === "compile" ? "Compile" : "Assistant";
    const time = document.createElement("span");
    time.textContent = new Date(entry.timestamp).toLocaleTimeString();
    meta.append(badge, time);

    const message = document.createElement("pre");
    message.className = "log-entry-message";
    message.textContent = entry.message;

    row.append(meta, message);
    logFilesPanelBody.appendChild(row);
  }
}

btnClearLogFiles.addEventListener("click", () => {
  errorLogEntries = [];
  renderLogFiles();
});

function closeAllPanels() {
  historyPanelEl.classList.remove("open");
  packagesPanelEl.classList.remove("open");
  logFilesPanelEl.classList.remove("open");
  panelOverlay.classList.remove("open");
}

async function openHistoryPanel({ focusComposer = false } = {}) {
  closeAllPanels();
  historyPanelEl.classList.add("open");
  panelOverlay.classList.add("open");
  await historyPanel.refresh();

  if (focusComposer) {
    const textarea = historyPanelBody.querySelector("textarea");
    textarea?.focus();
  }
}

async function openPackagesPanel() {
  closeAllPanels();
  packagesPanelEl.classList.add("open");
  panelOverlay.classList.add("open");
  await packagesPanel.refresh();
}

function openLogFilesPanel() {
  closeAllPanels();
  logFilesPanelEl.classList.add("open");
  panelOverlay.classList.add("open");
  renderLogFiles();
}

btnHistory.addEventListener("click", () => openHistoryPanel());
btnSaveVersion.addEventListener("click", () => openHistoryPanel({ focusComposer: true }));
menuPackagesTrigger.addEventListener("click", () => openPackagesPanel());
btnLogFiles.addEventListener("click", () => openLogFilesPanel());
btnCloseHistory.addEventListener("click", closeAllPanels);
btnClosePackages.addEventListener("click", closeAllPanels);
btnCloseLogFiles.addEventListener("click", closeAllPanels);
panelOverlay.addEventListener("click", closeAllPanels);

// ─── Sync Changes: commit (AI or user message), pull --rebase, push ───

/**
 * Run (or continue) a sync attempt.
 *
 * @param {string | null} commitMessage - Pass null for the first
 *   attempt, so the backend tries the AI assistant itself; pass a
 *   user-typed message when continuing after
 *   `sync-commit-message-dialog` was shown.
 */
async function performSync(commitMessage) {
  if (!currentProjectId) {
    notify("Open a project before syncing.", "error");
    return;
  }

  btnSyncChanges.disabled = true;
  btnSyncChanges.textContent = "Syncing…";

  try {
    const result = await api.syncProject(currentProjectId, commitMessage, getAuthorName());

    if (result.status === "needs_commit_message") {
      syncChangedFilesList.replaceChildren();
      for (const path of result.changed_files) {
        const item = document.createElement("li");
        item.textContent = path;
        syncChangedFilesList.appendChild(item);
      }
      syncCommitMessageInput.value = "";
      syncCommitMessageDialog.showModal();
      syncCommitMessageInput.focus();
      return;
    }

    const parts = [];
    if (result.committed) {
      parts.push(`committed "${result.commit_message}"`);
    }
    if (result.pulled_commits > 0) {
      parts.push(
        `pulled ${result.pulled_commits} change${result.pulled_commits === 1 ? "" : "s"}`,
      );
    }
    if (result.pushed) {
      parts.push("pushed");
    }
    notify(
      parts.length > 0 ? `Synced: ${parts.join(", ")}.` : "Already up to date.",
      "success",
    );

    if (result.pulled_commits > 0) {
      // The remote's changes just landed in the working tree -- reload
      // so the file tree/editor reflect them.
      await openProject(currentProjectId);
    }
  } catch (error) {
    notify(`Sync failed: ${error.message}`, "error");
  } finally {
    btnSyncChanges.disabled = false;
    btnSyncChanges.textContent = "Sync Changes";
  }
}

btnSyncChanges.addEventListener("click", () => performSync(null));

btnCancelSyncCommitMessage.addEventListener("click", () => syncCommitMessageDialog.close());

syncCommitMessageForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  const message = syncCommitMessageInput.value.trim();
  if (!message) {
    notify("Enter a commit message.", "error");
    return;
  }

  syncCommitMessageDialog.close();
  await performSync(message);
});

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
  await openProject(targetId);
}

projectSelect.addEventListener("change", () => {
  openProject(projectSelect.value || null);
});

async function openNewProjectDialog({ focusTemplate = false } = {}) {
  newProjectNameInput.value = "";
  newProjectRemoteUrlInput.value = "";
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

  if (focusTemplate) {
    newProjectTemplateSelect.focus();
  } else {
    newProjectNameInput.focus();
  }
}

btnNewProject.addEventListener("click", () => openNewProjectDialog());

btnCancelNewProject.addEventListener("click", () => newProjectDialog.close());

newProjectForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  const name = newProjectNameInput.value.trim();
  const remoteUrl = newProjectRemoteUrlInput.value.trim() || null;
  // A remote clone supplies its own content -- ignore the template
  // choice rather than sending both and relying on the backend's 400
  // to catch it (mutually exclusive, enforced there too).
  const templateId = remoteUrl ? null : newProjectTemplateSelect.value || null;

  if (!name) {
    notify("Enter a project name.", "error");
    return;
  }

  try {
    const created = await api.createProject(name, templateId, remoteUrl);
    newProjectDialog.close();
    notify(
      remoteUrl ? `Project "${name}" cloned.` : `Project "${name}" created.`,
      "success",
    );
    await loadProjects({ selectId: created.id });
  } catch (error) {
    notify(`Could not create project: ${error.message}`, "error");
  }
});

document.getElementById("menu-delete-project").addEventListener("click", () => {
  if (!currentProjectId) {
    notify("Open a project before deleting it.", "error");
    return;
  }

  const project = projectsById.get(currentProjectId);
  deleteProjectNameLabel.textContent = project?.name ?? currentProjectId;
  deleteProjectConfirmInput.value = "";
  btnConfirmDeleteProject.disabled = true;
  deleteProjectDialog.showModal();
  deleteProjectConfirmInput.focus();
});

deleteProjectConfirmInput.addEventListener("input", () => {
  const project = projectsById.get(currentProjectId);
  const expectedName = project?.name ?? currentProjectId ?? "";
  btnConfirmDeleteProject.disabled =
    deleteProjectConfirmInput.value !== expectedName;
});

btnCancelDeleteProject.addEventListener("click", () => deleteProjectDialog.close());

deleteProjectForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  if (!currentProjectId || btnConfirmDeleteProject.disabled) {
    return;
  }

  const projectIdToDelete = currentProjectId;

  try {
    await api.deleteProject(projectIdToDelete);
    deleteProjectDialog.close();
    notify("Project deleted.", "success");
    currentProjectId = null;
    await loadProjects();
  } catch (error) {
    notify(`Could not delete project: ${error.message}`, "error");
  }
});

const sidebarReopenBar = document.getElementById("sidebar-reopen-bar");
const sidebarResizer = document.getElementById("sidebar-resizer");
const SIDEBAR_MIN_WIDTH_PX = 160;
const SIDEBAR_MAX_WIDTH_PX = 600;

function setSidebarCollapsed(collapsed) {
  sidebar.classList.toggle("collapsed", collapsed);
  sidebarReopenBar.classList.toggle("hidden", !collapsed);
  sidebarResizer.classList.toggle("hidden", collapsed);
}

btnCollapseSidebar.addEventListener("click", () => setSidebarCollapsed(true));
sidebarReopenBar.addEventListener("click", () => setSidebarCollapsed(false));

(function setUpSidebarResize() {
  let isDragging = false;

  sidebarResizer.addEventListener("mousedown", (event) => {
    isDragging = true;
    sidebarResizer.classList.add("dragging");
    event.preventDefault();
  });

  window.addEventListener("mousemove", (event) => {
    if (!isDragging) {
      return;
    }

    const left = sidebar.getBoundingClientRect().left;
    const width = Math.min(
      SIDEBAR_MAX_WIDTH_PX,
      Math.max(SIDEBAR_MIN_WIDTH_PX, event.clientX - left),
    );
    sidebar.style.transition = "none";
    sidebar.style.width = `${width}px`;
  });

  window.addEventListener("mouseup", () => {
    if (isDragging) {
      isDragging = false;
      sidebarResizer.classList.remove("dragging");
      sidebar.style.transition = "";
    }
  });
})();

// ─── Status bar (char/word/line count) ───

function updateDocumentStats(text) {
  const safeText = text ?? "";
  const charCount = safeText.length;
  const lineCount = safeText.length === 0 ? 0 : safeText.split("\n").length;
  const trimmed = safeText.trim();
  const wordCount = trimmed.length === 0 ? 0 : trimmed.split(/\s+/).length;

  statusCharCount.textContent = `${charCount} character${charCount === 1 ? "" : "s"}`;
  statusWordCount.textContent = `${wordCount} word${wordCount === 1 ? "" : "s"}`;
  statusLineCount.textContent = `${lineCount} line${lineCount === 1 ? "" : "s"}`;
}

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

  // Flush before reassigning currentFilePath: a pending debounced
  // autosave from edits made in the file we're leaving captures its
  // text at keystroke time, but reads currentFilePath (and saves to
  // it) only when its timer actually fires. Reassigning first would
  // let that stale text silently land in the newly-opened file
  // instead, once the old timer eventually ran.
  if (editor) {
    editor.flush();
  }

  currentFilePath = path;
  currentFileLabel.textContent = path;
  fileTree.setActiveFile(path);
  updateDocumentStats(fileContent.content ?? "");

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
    ...getEditorSettings(),
  });
}

/**
 * Save the given (or current editor) text to the currently open file.
 *
 * @param {object} [options]
 * @param {string} [options.textOverride] - Text to save; defaults to
 *   the editor's current value.
 * @param {boolean} [options.silent] - Suppress the success toast (the
 *   "Saved" status-bar flash still shows either way).
 */
async function saveCurrentFile({ textOverride, silent = false } = {}) {
  if (!currentProjectId || !currentFilePath || !editor) {
    if (!silent) {
      notify("Open a file before saving.", "error");
    }
    return;
  }

  const text = textOverride ?? editor.getValue();
  saveStatus.textContent = "Saving…";

  try {
    await api.saveFile(currentProjectId, currentFilePath, text);
    saveStatus.textContent = "Saved";
    updateDocumentStats(text);
    setTimeout(() => {
      if (saveStatus.textContent === "Saved") {
        saveStatus.textContent = "";
      }
    }, 2000);

    if (!silent) {
      notify("Saved.", "success");
    }
  } catch (error) {
    saveStatus.textContent = "";
    notify(`Could not save file: ${error.message}`, "error");
  }
}

// Recompiling LaTeX on every autosave would make typing feel sluggish,
// so the automatic preview refresh is debounced separately (and much
// longer) than the autosave itself -- the explicit "Compile" button
// still compiles immediately, bypassing this delay.
const PREVIEW_REFRESH_DEBOUNCE_MS = 15000;
let previewRefreshTimer = null;

function cancelScheduledPreviewRefresh() {
  if (previewRefreshTimer) {
    clearTimeout(previewRefreshTimer);
    previewRefreshTimer = null;
  }
}

function scheduleDebouncedPreviewRefresh() {
  cancelScheduledPreviewRefresh();
  previewRefreshTimer = setTimeout(() => {
    previewRefreshTimer = null;
    compileCurrentProject({ silent: true });
  }, PREVIEW_REFRESH_DEBOUNCE_MS);
}

async function handleEditorChange(text) {
  if (!currentProjectId || !currentFilePath) {
    return;
  }

  await saveCurrentFile({ textOverride: text, silent: true });
  scheduleDebouncedPreviewRefresh();
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
      showCompileError(previewContainer, result.log, {
        onAskAssistant: (logText) => {
          assistantDock.classList.remove("hidden");
          assistantPanel.askAboutCompileError(logText);
        },
      });
      logEntry("compile", result.log);

      if (!silent) {
        notify("Compile failed — see log in the preview pane.", "error");
      }
    }
  } catch (error) {
    compileStatus.textContent = "";
    clearPreview(previewContainer);
    showCompileError(previewContainer, error.message);
    logEntry("compile", error.message);
    notify(`Compile request failed: ${error.message}`, "error");
  }
}

btnCompile.addEventListener("click", () => compileCurrentProject());

// ─── Opening a project ───

async function openProject(projectId) {
  closeAllPanels();
  cancelScheduledPreviewRefresh();

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
  updateDocumentStats("");

  await fileTree.load(currentProjectId);

  if (!currentProjectId) {
    const placeholder = document.createElement("p");
    placeholder.className = "preview-empty";
    placeholder.textContent = "Create a project to get started.";
    previewContainer.appendChild(placeholder);
    return;
  }

  const project = projectsById.get(currentProjectId);
  const mainFile = project?.main_file || "main.tex";
  const preferredFile = currentFilePath || mainFile;

  const opened = await openFile(preferredFile);

  if (!opened && preferredFile !== mainFile) {
    await openFile(mainFile);
  }

  await compileCurrentProject({ silent: true });
}

// ─── Collapse editor/preview panes (focus on a single pane) ───

function setPaneCollapsed(pane, otherPane, toggleButton, collapsedIcon, expandedIcon) {
  const collapsing = !pane.classList.contains("collapsed");

  pane.classList.toggle("collapsed", collapsing);
  otherPane.classList.toggle("expanded", collapsing);
  toggleButton.textContent = collapsing ? expandedIcon : collapsedIcon;
  toggleButton.title = collapsing ? "Expand" : "Collapse";
}

btnCollapseEditorPane.addEventListener("click", () => {
  setPaneCollapsed(editorPane, previewPane, btnCollapseEditorPane, "◀", "▶");
});

btnCollapsePreviewPane.addEventListener("click", () => {
  setPaneCollapsed(previewPane, editorPane, btnCollapsePreviewPane, "▶", "◀");
});

// ─── View menu (toggle panels) ───

function setEditorPreviewVisible(which, visible) {
  const [pane, other, button, collapsedIcon, expandedIcon] =
    which === "editor"
      ? [editorPane, previewPane, btnCollapseEditorPane, "◀", "▶"]
      : [previewPane, editorPane, btnCollapsePreviewPane, "▶", "◀"];
  const isVisible = !pane.classList.contains("collapsed");

  if (visible === isVisible) {
    return;
  }

  if (!visible && other.classList.contains("collapsed")) {
    // Never leave both panes collapsed: reopen the other one first.
    const otherButton = which === "editor" ? btnCollapsePreviewPane : btnCollapseEditorPane;
    otherButton.click();
  }

  setPaneCollapsed(pane, other, button, collapsedIcon, expandedIcon);
}

const VIEW_TOGGLE_STATE = {
  sidebar: () => !sidebar.classList.contains("collapsed"),
  editor: () => !editorPane.classList.contains("collapsed"),
  preview: () => !previewPane.classList.contains("collapsed"),
  assistant: () => !assistantDock.classList.contains("hidden"),
};

function updateViewMenuChecks() {
  for (const item of document.querySelectorAll("[data-view-toggle]")) {
    item.setAttribute("aria-checked", String(VIEW_TOGGLE_STATE[item.dataset.viewToggle]()));
  }
}

for (const item of document.querySelectorAll("[data-view-toggle]")) {
  item.addEventListener("click", () => {
    const target = item.dataset.viewToggle;
    const shouldShow = !VIEW_TOGGLE_STATE[target]();

    if (target === "sidebar") {
      setSidebarCollapsed(!shouldShow);
    } else if (target === "assistant") {
      assistantDock.classList.toggle("hidden", !shouldShow);
    } else {
      setEditorPreviewVisible(target, shouldShow);
    }
  });
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

// ─── Resizable assistant dock ───

(function setUpAssistantResize() {
  const handle = document.getElementById("assistant-resize-handle");
  let isDragging = false;

  handle.addEventListener("mousedown", (event) => {
    isDragging = true;
    handle.classList.add("dragging");
    // Manually resizing implies leaving minimize/maximize mode --
    // otherwise the next un-minimize/un-maximize would restore a now
    // stale height instead of whatever this drag set.
    assistantDock.classList.remove("minimized", "maximized");
    assistantDockRestoreHeight = null;
    event.preventDefault();
  });

  window.addEventListener("mousemove", (event) => {
    if (!isDragging) {
      return;
    }

    const bounds = assistantDock.getBoundingClientRect();
    const minHeight = 120;
    const maxHeight = window.innerHeight * 0.8;
    const newHeight = Math.min(
      maxHeight,
      Math.max(minHeight, bounds.bottom - event.clientY),
    );

    assistantDock.style.height = `${newHeight}px`;
  });

  window.addEventListener("mouseup", () => {
    if (isDragging) {
      isDragging = false;
      handle.classList.remove("dragging");
    }
  });
})();

// ─── File menu actions ───

document.getElementById("menu-new-project").addEventListener("click", () => {
  openNewProjectDialog();
});

document
  .getElementById("menu-new-project-from-sample")
  .addEventListener("click", () => {
    openNewProjectDialog({ focusTemplate: true });
  });

document.getElementById("menu-open-project").addEventListener("click", () => {
  if (typeof projectSelect.showPicker === "function") {
    projectSelect.showPicker();
  } else {
    projectSelect.focus();
  }
});

document.getElementById("menu-save").addEventListener("click", () => {
  saveCurrentFile();
});

document.getElementById("menu-save-as").addEventListener("click", async () => {
  if (!currentProjectId || !currentFilePath || !editor) {
    notify("Open a file before using Save As.", "error");
    return;
  }

  const newPath = window.prompt("Save as (project-relative path):", currentFilePath);

  if (!newPath || newPath === currentFilePath) {
    return;
  }

  try {
    await api.saveFile(currentProjectId, newPath, editor.getValue());
    notify(`Saved as "${newPath}".`, "success");
    await fileTree.load(currentProjectId);
    await openFile(newPath);
  } catch (error) {
    notify(`Could not save as "${newPath}": ${error.message}`, "error");
  }
});

document.getElementById("menu-export-pdf").addEventListener("click", async () => {
  if (!currentProjectId) {
    notify("Open a project before exporting a PDF.", "error");
    return;
  }

  const project = projectsById.get(currentProjectId);
  const mainFile = project?.main_file || "main.tex";
  const pdfName = `${mainFile.replace(/\.tex$/, "")}.pdf`;
  const url = `/api/projects/${encodeURIComponent(currentProjectId)}/files/${encodeURIComponent(pdfName)}/raw`;

  try {
    const checkResponse = await fetch(url);

    if (!checkResponse.ok) {
      notify("Compile the project at least once before exporting.", "error");
      return;
    }
  } catch (error) {
    notify(`Could not reach the server: ${error.message}`, "error");
    return;
  }

  const link = document.createElement("a");
  link.href = url;
  link.download = pdfName;
  document.body.appendChild(link);
  link.click();
  link.remove();
});

document.getElementById("menu-close-project").addEventListener("click", async () => {
  await openProject(null);
  projectSelect.selectedIndex = -1;
});

document.getElementById("menu-quit").addEventListener("click", () => {
  quitDialog.showModal();
});

btnCloseQuitDialog.addEventListener("click", () => quitDialog.close());

// ─── Help menu actions ───

document.getElementById("menu-help-docs").addEventListener("click", () => {
  if (DOCUMENTATION_URL) {
    window.open(DOCUMENTATION_URL, "_blank", "noopener");
  } else {
    notify(
      "Documentation isn't published yet — see docs/ in the project for now.",
      "info",
    );
  }
});

document.getElementById("menu-help-templates").addEventListener("click", () => {
  openPackagesPanel();
});

const shortcutsDialog = createShortcutsDialog({
  dialogEl: document.getElementById("shortcuts-dialog"),
  bodyEl: document.getElementById("shortcuts-body"),
  closeButtonEl: document.getElementById("btn-close-shortcuts"),
});

document.getElementById("menu-help-shortcuts").addEventListener("click", () => {
  shortcutsDialog.open();
});

// ─── Boot ───

await loadProjects();
