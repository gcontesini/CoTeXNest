/**
 * shortcuts-dialog.js
 *
 * The Help -> Shortcuts dialog: keyboard shortcuts of the editor and
 * the app, plus the user's own custom commands read live from the
 * global custom-commands store.
 *
 * The editor list mirrors the keymaps editor.js installs
 * (`defaultKeymap`, `historyKeymap`, `searchKeymap`, `indentWithTab`
 * from CodeMirror 6); update both together.
 */

import { getCustomCommands } from "./api.js";
import { parseCustomCommands } from "./custom-commands-core.js";

const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || "");
const MOD = IS_MAC ? "Cmd" : "Ctrl";
const ALT = IS_MAC ? "Option" : "Alt";

const EDITOR_SHORTCUTS = [
  [`${MOD}+Z`, "Undo"],
  [IS_MAC ? "Cmd+Shift+Z" : "Ctrl+Y", "Redo"],
  [`${MOD}+A`, "Select all"],
  [`${MOD}+F`, "Find"],
  [`${MOD}+G`, "Find next"],
  [`${MOD}+Shift+G`, "Find previous"],
  [`${MOD}+D`, "Select the next occurrence of the selected text"],
  [`${ALT}+Up / ${ALT}+Down`, "Move the line up or down"],
  [`Shift+${ALT}+Up / Shift+${ALT}+Down`, "Copy the line up or down"],
  [`${MOD}+Shift+K`, "Delete the line"],
  [`${MOD}+Enter`, "Insert a blank line below"],
  ["Tab / Shift+Tab", "Indent / dedent the selection"],
];

const APP_SHORTCUTS = [
  ["Enter", "Send the message in the AI Assistant"],
  ["Shift+Enter", "New line in the AI Assistant message"],
  ["Esc", "Close an open menu"],
  ["Right-click a red-underlined word", "Spelling suggestions and Learn spelling"],
  ["Right-click a file or folder", "Rename, move, delete"],
  ["Drag a file onto the Files panel", "Copy it into the project"],
];

function element(tagName, className, text) {
  const node = document.createElement(tagName);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function shortcutTable(title, rows, keyClass = "shortcut-keys") {
  const section = element("div", "settings-section");
  section.appendChild(element("h3", null, title));

  const table = element("table", "shortcuts-table");

  for (const [keys, description] of rows) {
    const row = element("tr");
    row.append(element("td", keyClass, keys), element("td", null, description));
    table.appendChild(row);
  }

  section.appendChild(table);
  return section;
}

/**
 * @param {object} options
 * @param {HTMLDialogElement} options.dialogEl
 * @param {HTMLElement} options.bodyEl
 * @param {HTMLElement} options.closeButtonEl
 * @returns {{open: () => Promise<void>}}
 */
export function createShortcutsDialog({ dialogEl, bodyEl, closeButtonEl }) {
  closeButtonEl.addEventListener("click", () => dialogEl.close());

  async function renderCustomCommands() {
    let commands = [];
    let failure = "";

    try {
      commands = parseCustomCommands((await getCustomCommands()).content);
    } catch (error) {
      failure = `Could not load custom commands: ${error.message}`;
    }

    if (failure || commands.length === 0) {
      const section = element("div", "settings-section");
      section.appendChild(element("h3", null, "Custom commands"));
      section.appendChild(
        element(
          "p",
          "package-note",
          failure || "None yet. Add some under Settings → Custom Commands.",
        ),
      );
      return section;
    }

    return shortcutTable(
      "Custom commands",
      commands.map((command) => [command.usage, command.description || "No description"]),
      "shortcut-keys shortcut-command",
    );
  }

  return {
    async open() {
      bodyEl.replaceChildren(
        shortcutTable("Editor", EDITOR_SHORTCUTS),
        shortcutTable("Application", APP_SHORTCUTS),
      );
      dialogEl.showModal();
      bodyEl.appendChild(await renderCustomCommands());
    },
  };
}
