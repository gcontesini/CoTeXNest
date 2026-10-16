/**
 * settings-modal.js
 *
 * The app-wide Settings dialog: a left-side list of categories
 * (General, Editor, AI Assistant, Dictionary, Git) next to a right-side
 * panel of controls for whichever one is selected.
 *
 * Every setting here is a per-browser UI preference -- app theme/font,
 * editor appearance/behavior, the personal spell-check dictionary --
 * stored in localStorage, the same documented exception to "all state
 * lives in a bind-mounted volume" that the author-name field already
 * uses (see docs/architecture.md). None of it is project data, so
 * none of it needs to be visible outside the browser that set it.
 *
 * The "AI Assistant" category hosts assistant-panel.js's settings UI
 * (provider, keys, fallbacks, writing style) directly: the panel
 * renders into a container this dialog owns and shows on demand, so
 * there is exactly one implementation and the chat dock stays
 * chat-only.
 */

import {
  getPersonalDictionary,
  addToPersonalDictionary,
  removeFromPersonalDictionary,
  listDictionaries,
  getActiveDictionaryId,
  setActiveDictionary,
  addCustomDictionary,
  removeCustomDictionary,
} from "./spellcheck.js";
import {
  getGitInfo,
  setGitRemote,
  setGitCredentials,
  clearGitCredentials,
  gitFetch,
  gitPull,
  gitPush,
  listGitBranches,
  switchGitBranch,
} from "./api.js";

// Shared with app.js, which prompts for the name on first commit; the
// Git settings category lets it be set or changed up front.
export const AUTHOR_NAME_STORAGE_KEY = "latex-writer:author-name";

const STORAGE_KEYS = {
  theme: "latex-writer-theme", // "system" | "dark" | "light"
  appFont: "latex-writer-app-font", // "system" | "sans" | "serif" | "mono"
  appFontSize: "latex-writer-app-font-size", // px, number
  editorColorScheme: "latex-writer-editor-color-scheme", // "default" | "high-contrast"
  editorIndentSize: "latex-writer-editor-indent-size", // number
  editorHighlightActiveLine: "latex-writer-editor-highlight-active-line", // "true"|"false"
  editorHighlightBrackets: "latex-writer-editor-highlight-brackets", // "true"|"false"
};

const APP_FONT_STACKS = {
  system:
    '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
  sans: 'Arial, Helvetica, "Helvetica Neue", sans-serif',
  serif: 'Georgia, Cambria, "Times New Roman", Times, serif',
  mono: 'ui-monospace, SFMono-Regular, "SF Mono", Consolas, "Liberation Mono", Menlo, monospace',
};

const DEFAULT_SETTINGS = {
  theme: "system",
  appFont: "system",
  appFontSize: 15,
  editorColorScheme: "default",
  editorIndentSize: 2,
  editorHighlightActiveLine: true,
  editorHighlightBrackets: true,
};

function readStoredSettings() {
  return {
    theme: localStorage.getItem(STORAGE_KEYS.theme) || DEFAULT_SETTINGS.theme,
    appFont: localStorage.getItem(STORAGE_KEYS.appFont) || DEFAULT_SETTINGS.appFont,
    appFontSize:
      Number(localStorage.getItem(STORAGE_KEYS.appFontSize)) || DEFAULT_SETTINGS.appFontSize,
    editorColorScheme:
      localStorage.getItem(STORAGE_KEYS.editorColorScheme) || DEFAULT_SETTINGS.editorColorScheme,
    editorIndentSize:
      Number(localStorage.getItem(STORAGE_KEYS.editorIndentSize)) ||
      DEFAULT_SETTINGS.editorIndentSize,
    editorHighlightActiveLine:
      localStorage.getItem(STORAGE_KEYS.editorHighlightActiveLine) !== "false",
    editorHighlightBrackets:
      localStorage.getItem(STORAGE_KEYS.editorHighlightBrackets) !== "false",
  };
}

/**
 * Apply the theme and app font/size to the document. Call this once
 * at boot (before the first paint, as early as practical) and again
 * whenever the user changes one of these in Settings.
 */
export function applyAppearanceSettings() {
  const settings = readStoredSettings();
  const root = document.documentElement;

  if (settings.theme === "system") {
    root.removeAttribute("data-theme");
  } else {
    root.setAttribute("data-theme", settings.theme);
  }

  root.style.setProperty("--app-font-family", APP_FONT_STACKS[settings.appFont] || APP_FONT_STACKS.system);
  root.style.setProperty("--app-font-size", `${settings.appFontSize}px`);
}

/**
 * @returns {{indentSize: number, highlightActiveLineEnabled: boolean, highlightMatchingBracketsEnabled: boolean, colorScheme: string}}
 *   The subset of settings `editor.js`'s `createEditor`/`reconfigure`
 *   expect, read fresh from storage -- call this whenever mounting a
 *   new editor instance.
 */
export function getEditorSettings() {
  const settings = readStoredSettings();
  return {
    indentSize: settings.editorIndentSize,
    highlightActiveLineEnabled: settings.editorHighlightActiveLine,
    highlightMatchingBracketsEnabled: settings.editorHighlightBrackets,
    colorScheme: settings.editorColorScheme,
  };
}

function createElement(tagName, className, textContent) {
  const element = document.createElement(tagName);
  if (className) element.className = className;
  if (textContent !== undefined) element.textContent = textContent;
  return element;
}

function formRow(labelText, control) {
  const row = createElement("div", "form-row");
  const label = createElement("label", null, labelText);
  row.append(label, control);
  return row;
}

/**
 * Build and wire the Settings dialog.
 *
 * @param {object} options
 * @param {HTMLDialogElement} options.dialogEl
 * @param {HTMLElement} options.bodyEl - Container the active
 *   category's controls render into.
 * @param {HTMLElement} options.titleEl - Dialog heading, updated to
 *   match the active category.
 * @param {() => object | null} options.getEditor - Returns the
 *   currently mounted editor instance (see editor.js), or null if
 *   none is open.
 * @param {() => string | null} options.getProjectId - Returns the id
 *   of the open project, or null; the Git category acts on it.
 * @param {(message: string, kind?: string) => void} options.notify
 * @param {() => void} options.renderAssistantSettings - Renders the
 *   assistant's settings UI into the container passed as
 *   `assistantSettingsEl`.
 * @param {HTMLElement} options.assistantSettingsEl - Persistent
 *   container the assistant panel renders its settings into; this
 *   dialog attaches it to `bodyEl` for the "AI Assistant" category.
 * @returns {{ open: () => void }}
 */
export function createSettingsModal({
  dialogEl,
  bodyEl,
  titleEl,
  getEditor,
  getProjectId,
  notify,
  renderAssistantSettings,
  assistantSettingsEl,
}) {
  const navButtons = Array.from(dialogEl.querySelectorAll(".settings-nav-item"));
  let activeCategory = "general";

  function updateSetting(key, value) {
    localStorage.setItem(STORAGE_KEYS[key], String(value));
  }

  function applyAndPersistAppearance() {
    applyAppearanceSettings();
  }

  function renderGeneral() {
    const settings = readStoredSettings();
    bodyEl.replaceChildren();

    const themeSection = createElement("div", "settings-section");
    themeSection.appendChild(createElement("h3", null, "Theme"));
    const themeRow = createElement("div", "radio-row");

    for (const [value, label] of [
      ["system", "System"],
      ["dark", "Dark"],
      ["light", "Light"],
    ]) {
      const themeLabel = document.createElement("label");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "settings-theme";
      radio.value = value;
      radio.checked = settings.theme === value;
      radio.addEventListener("change", () => {
        updateSetting("theme", value);
        applyAndPersistAppearance();
      });
      themeLabel.append(radio, ` ${label}`);
      themeRow.appendChild(themeLabel);
    }

    themeSection.appendChild(themeRow);
    bodyEl.appendChild(themeSection);

    const fontSection = createElement("div", "settings-section");
    fontSection.appendChild(createElement("h3", null, "Application font"));

    const fontSelect = document.createElement("select");
    for (const [value, label] of [
      ["system", "System default"],
      ["sans", "Sans-serif"],
      ["serif", "Serif"],
      ["mono", "Monospace"],
    ]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      option.selected = settings.appFont === value;
      fontSelect.appendChild(option);
    }
    fontSelect.addEventListener("change", () => {
      updateSetting("appFont", fontSelect.value);
      applyAndPersistAppearance();
    });
    fontSection.appendChild(formRow("Font", fontSelect));

    const sizeInput = document.createElement("input");
    sizeInput.type = "number";
    sizeInput.min = "11";
    sizeInput.max = "22";
    sizeInput.value = String(settings.appFontSize);
    sizeInput.addEventListener("change", () => {
      const size = Math.min(22, Math.max(11, Number(sizeInput.value) || DEFAULT_SETTINGS.appFontSize));
      sizeInput.value = String(size);
      updateSetting("appFontSize", size);
      applyAndPersistAppearance();
    });
    fontSection.appendChild(formRow("Font size (px)", sizeInput));

    bodyEl.appendChild(fontSection);
  }

  function renderEditor() {
    const settings = readStoredSettings();
    bodyEl.replaceChildren();

    const section = createElement("div", "settings-section");

    const colorSchemeSelect = document.createElement("select");
    for (const [value, label] of [
      ["default", "Default"],
      ["high-contrast", "High contrast"],
    ]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      option.selected = settings.editorColorScheme === value;
      colorSchemeSelect.appendChild(option);
    }
    colorSchemeSelect.addEventListener("change", () => {
      updateSetting("editorColorScheme", colorSchemeSelect.value);
      getEditor()?.reconfigure({ colorScheme: colorSchemeSelect.value });
    });
    section.appendChild(formRow("Syntax color scheme", colorSchemeSelect));

    const indentInput = document.createElement("input");
    indentInput.type = "number";
    indentInput.min = "1";
    indentInput.max = "8";
    indentInput.value = String(settings.editorIndentSize);
    indentInput.addEventListener("change", () => {
      const size = Math.min(8, Math.max(1, Number(indentInput.value) || DEFAULT_SETTINGS.editorIndentSize));
      indentInput.value = String(size);
      updateSetting("editorIndentSize", size);
      getEditor()?.reconfigure({ indentSize: size });
    });
    section.appendChild(formRow("Indentation size (spaces)", indentInput));

    const activeLineRow = createElement("div", "form-row");
    const activeLineLabel = document.createElement("label");
    const activeLineCheckbox = document.createElement("input");
    activeLineCheckbox.type = "checkbox";
    activeLineCheckbox.checked = settings.editorHighlightActiveLine;
    activeLineCheckbox.addEventListener("change", () => {
      updateSetting("editorHighlightActiveLine", activeLineCheckbox.checked);
      getEditor()?.reconfigure({ highlightActiveLineEnabled: activeLineCheckbox.checked });
    });
    activeLineLabel.append(activeLineCheckbox, " Highlight the current line");
    activeLineRow.appendChild(activeLineLabel);
    section.appendChild(activeLineRow);

    const bracketsRow = createElement("div", "form-row");
    const bracketsLabel = document.createElement("label");
    const bracketsCheckbox = document.createElement("input");
    bracketsCheckbox.type = "checkbox";
    bracketsCheckbox.checked = settings.editorHighlightBrackets;
    bracketsCheckbox.addEventListener("change", () => {
      updateSetting("editorHighlightBrackets", bracketsCheckbox.checked);
      getEditor()?.reconfigure({ highlightMatchingBracketsEnabled: bracketsCheckbox.checked });
    });
    bracketsLabel.append(bracketsCheckbox, " Highlight matching brackets");
    bracketsRow.appendChild(bracketsLabel);
    section.appendChild(bracketsRow);

    bodyEl.appendChild(section);
  }

  function renderAssistant() {
    bodyEl.replaceChildren(assistantSettingsEl);
    assistantSettingsEl.classList.remove("hidden");
    renderAssistantSettings();
  }

  function renderDictionary() {
    bodyEl.replaceChildren();

    const languageSection = createElement("div", "settings-section");
    languageSection.appendChild(createElement("h3", null, "Language dictionary"));

    const languageSelect = document.createElement("select");
    for (const dictionary of listDictionaries()) {
      const option = document.createElement("option");
      option.value = dictionary.id;
      option.textContent = dictionary.label;
      option.selected = dictionary.id === getActiveDictionaryId();
      languageSelect.appendChild(option);
    }
    languageSelect.addEventListener("change", async () => {
      await setActiveDictionary(languageSelect.value);
      renderDictionary();
    });
    languageSection.appendChild(formRow("Spell-check language", languageSelect));

    const importedDictionaries = listDictionaries().filter((d) => !d.builtin);
    for (const dictionary of importedDictionaries) {
      const row = createElement("div", "fallback-profile-row");
      row.appendChild(createElement("span", null, dictionary.label));
      const removeButton = createElement("button", "btn btn-icon", "Remove");
      removeButton.type = "button";
      removeButton.addEventListener("click", async () => {
        await removeCustomDictionary(dictionary.id);
        renderDictionary();
      });
      row.appendChild(removeButton);
      languageSection.appendChild(row);
    }

    languageSection.appendChild(
      createElement(
        "p",
        null,
        "English (US) and English (UK) are bundled. Add another " +
          "language by importing a plain-text word list with one word " +
          "per line.",
      ),
    );

    const fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.accept = ".txt,.dic,.dict,text/plain";
    fileInput.addEventListener("change", async () => {
      const file = fileInput.files?.[0];
      if (!file) return;

      try {
        const label = file.name.replace(/\.[^.]+$/, "");
        const id = addCustomDictionary(label, await file.text());
        await setActiveDictionary(id);
        notify?.(`Added dictionary "${label}" and switched to it.`, "success");
      } catch (error) {
        notify?.(`Could not add dictionary: ${error.message}`, "error");
      }
      renderDictionary();
    });
    languageSection.appendChild(formRow("Add dictionary file", fileInput));
    bodyEl.appendChild(languageSection);

    const section = createElement("div", "settings-section");
    section.appendChild(createElement("h3", null, "Personal dictionary"));
    section.appendChild(
      createElement(
        "p",
        null,
        "Words you \"Learn spelling\" on in the editor (or add here) " +
          "are never flagged again, in any language.",
      ),
    );

    const words = getPersonalDictionary();

    if (words.length === 0) {
      section.appendChild(createElement("p", "empty-state", "No learned words yet."));
    } else {
      const list = createElement("div", "fallback-profile-list");
      for (const word of words) {
        const row = createElement("div", "fallback-profile-row");
        row.appendChild(createElement("span", null, word));
        const removeButton = createElement("button", "btn btn-icon", "Remove");
        removeButton.type = "button";
        removeButton.addEventListener("click", () => {
          removeFromPersonalDictionary(word);
          renderDictionary();
        });
        row.appendChild(removeButton);
        list.appendChild(row);
      }
      section.appendChild(list);
    }

    const addRow = createElement("div", "form-row");
    const addInput = document.createElement("input");
    addInput.type = "text";
    addInput.placeholder = "Add a word";
    const addButton = createElement("button", "btn btn-icon", "Add");
    addButton.type = "button";
    const commitAdd = () => {
      const word = addInput.value.trim();
      if (!word) return;
      addToPersonalDictionary(word);
      addInput.value = "";
      renderDictionary();
      notify?.(`Added "${word}" to your personal dictionary.`, "success");
    };
    addButton.addEventListener("click", commitAdd);
    addInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        commitAdd();
      }
    });
    addRow.append(addInput, addButton);
    section.appendChild(addRow);

    bodyEl.appendChild(section);
  }

  // ─── Git (remote, identity, credentials, branches, fetch/pull/push) ───

  function gitSection(title) {
    const section = createElement("div", "settings-section");
    section.appendChild(createElement("h3", null, title));
    return section;
  }

  function gitButton(label, onClick, className = "btn") {
    const button = createElement("button", className, label);
    button.type = "button";
    button.addEventListener("click", onClick);
    return button;
  }

  function describeGitStatus(info) {
    if (!info.remote_url) return "No remote configured.";
    if (!info.branch) return "Detached HEAD: switch to a branch.";
    if (!info.remote_branch_exists) {
      return `Branch ${info.branch} is not on the remote yet. Push to publish it.`;
    }
    return `Branch ${info.branch}: ${info.ahead} to push, ${info.behind} to pull (as of the last fetch).`;
  }

  async function renderGit() {
    bodyEl.replaceChildren();
    const projectId = getProjectId();

    if (!projectId) {
      bodyEl.appendChild(
        createElement("p", "empty-state", "Open a project to configure its Git repository."),
      );
      return;
    }

    let info;
    let branches;
    try {
      [info, branches] = await Promise.all([getGitInfo(projectId), listGitBranches(projectId)]);
    } catch (error) {
      notify(error.message, "error");
      return;
    }

    if (activeCategory !== "git") return;

    bodyEl.replaceChildren();

    async function run(action, successMessage) {
      try {
        const result = await action();
        notify(result?.message ?? successMessage, "success");
      } catch (error) {
        notify(error.message, "error");
      }
      if (activeCategory === "git") await renderGit();
    }

    const repoSection = gitSection("Repository");
    const urlInput = createElement("input");
    urlInput.type = "text";
    urlInput.placeholder = "https://github.com/owner/repo.git";
    urlInput.value = info.remote_url ?? "";
    repoSection.appendChild(formRow("Remote URL (origin)", urlInput));
    repoSection.appendChild(
      gitButton("Save remote URL", () =>
        run(() => setGitRemote(projectId, urlInput.value), "Remote URL saved."),
      ),
    );
    repoSection.appendChild(createElement("p", "package-note", describeGitStatus(info)));
    bodyEl.appendChild(repoSection);

    const identitySection = gitSection("Identity");
    const nameInput = createElement("input");
    nameInput.type = "text";
    nameInput.value = localStorage.getItem(AUTHOR_NAME_STORAGE_KEY) ?? "";
    nameInput.placeholder = "Your name";
    nameInput.addEventListener("change", () => {
      const name = nameInput.value.trim();
      if (name) {
        localStorage.setItem(AUTHOR_NAME_STORAGE_KEY, name);
        notify("Name saved.", "success");
      }
    });
    identitySection.appendChild(formRow("Your name (author of saved versions)", nameInput));
    bodyEl.appendChild(identitySection);

    const credentialsSection = gitSection("Credentials");
    const usernameInput = createElement("input");
    usernameInput.type = "text";
    usernameInput.autocomplete = "off";
    usernameInput.value = info.credential_username ?? "";
    const tokenInput = createElement("input");
    tokenInput.type = "password";
    tokenInput.autocomplete = "off";
    tokenInput.placeholder = info.has_credentials ? "Stored (enter a new token to replace)" : "";
    credentialsSection.append(
      formRow("Username", usernameInput),
      formRow("Personal access token", tokenInput),
    );
    const credentialButtons = createElement("div", "form-row");
    credentialButtons.append(
      gitButton(
        "Save credentials",
        () =>
          run(
            () => setGitCredentials(projectId, usernameInput.value.trim(), tokenInput.value.trim()),
            "Credentials saved.",
          ),
        "btn btn-primary",
      ),
      gitButton(
        "Remove credentials",
        () => run(() => clearGitCredentials(projectId), "Credentials removed."),
        "btn btn-danger",
      ),
    );
    credentialsSection.appendChild(credentialButtons);
    credentialsSection.appendChild(
      createElement(
        "p",
        "package-note",
        (info.has_credentials ? `Stored for ${info.credential_username ?? "a token-only login"}. ` : "") +
          "https:// remotes only. The token is kept as plain text in this project's " +
          ".git/config inside the workspace volume and is never shown again. Use a " +
          "token limited to this repository.",
      ),
    );
    bodyEl.appendChild(credentialsSection);

    const branchSection = gitSection("Branch");
    const branchSelect = createElement("select");
    for (const name of branches.local) {
      const option = createElement("option", null, name);
      option.value = name;
      option.selected = name === branches.current;
      branchSelect.appendChild(option);
    }
    if (branches.remote_only.length > 0) {
      const group = document.createElement("optgroup");
      group.label = "Remote only (fetched)";
      for (const name of branches.remote_only) {
        const option = createElement("option", null, name);
        option.value = name;
        group.appendChild(option);
      }
      branchSelect.appendChild(group);
    }
    branchSection.appendChild(formRow("Current branch", branchSelect));
    branchSection.appendChild(
      gitButton("Switch branch", () =>
        run(async () => {
          await switchGitBranch(projectId, branchSelect.value, false);
          return { message: `Switched to ${branchSelect.value}. Reopen files to see their content.` };
        }),
      ),
    );
    const newBranchInput = createElement("input");
    newBranchInput.type = "text";
    newBranchInput.placeholder = "feature/new-branch";
    branchSection.appendChild(formRow("New branch (from current)", newBranchInput));
    branchSection.appendChild(
      gitButton("Create and switch", () =>
        run(async () => {
          await switchGitBranch(projectId, newBranchInput.value.trim(), true);
          return { message: `Created and switched to ${newBranchInput.value.trim()}.` };
        }),
      ),
    );
    branchSection.appendChild(
      createElement(
        "p",
        "package-note",
        "Switching needs a clean working tree: save a version first. Run Fetch to refresh the remote-only list.",
      ),
    );
    bodyEl.appendChild(branchSection);

    const actionsSection = gitSection("Remote actions");
    const actionRow = createElement("div", "git-action-row");
    actionRow.append(
      gitButton("Fetch", () => run(() => gitFetch(projectId))),
      gitButton("Pull", () => run(() => gitPull(projectId))),
      gitButton("Push", () => run(() => gitPush(projectId))),
    );
    actionsSection.appendChild(actionRow);
    actionsSection.appendChild(
      createElement(
        "p",
        "package-note",
        "Pull rebases onto the remote branch and needs a clean working tree. Push publishes committed work only.",
      ),
    );
    bodyEl.appendChild(actionsSection);
  }

  const RENDERERS = {
    general: { title: "General", render: renderGeneral },
    editor: { title: "Editor", render: renderEditor },
    assistant: { title: "AI Assistant", render: renderAssistant },
    dictionary: { title: "Dictionary", render: renderDictionary },
    git: { title: "Git", render: renderGit },
  };

  function showCategory(category) {
    activeCategory = category;

    for (const button of navButtons) {
      button.classList.toggle("active", button.dataset.category === category);
    }

    titleEl.textContent = RENDERERS[category].title;
    RENDERERS[category].render();
  }

  for (const button of navButtons) {
    button.addEventListener("click", () => showCategory(button.dataset.category));
  }

  return {
    open() {
      showCategory(activeCategory);
      dialogEl.showModal();
    },
  };
}
