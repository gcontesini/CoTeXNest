/**
 * spellcheck.js
 *
 * Browser/CodeMirror wiring on top of spellcheck-core.js's pure logic:
 * fetches the English dictionary once, manages a personal dictionary
 * in localStorage, and provides a CodeMirror 6 extension that
 * underlines unrecognized words and offers a right-click menu of
 * corrections plus "Learn spelling".
 *
 * Dictionary data: static/data/dictionary-en.txt and
 * dictionary-en-GB.txt, the real "american-english" and
 * "british-english" word lists from Debian's `wamerican` and
 * `wbritish` packages (both derived from SCOWL -- Spell Checker
 * Oriented Word Lists). See static/data/dictionary-en.LICENSE.txt for
 * the license both files are shipped under.
 *
 * Scope, stated plainly: only those two English variants are bundled.
 * Other languages are supported by importing a plain-text word list
 * (one word per line) in Settings → Dictionary; no further word list
 * is bundled because it would need a verifiable source and license.
 */

import { Decoration, EditorView, ViewPlugin } from "@codemirror/view";
import { RangeSetBuilder } from "@codemirror/state";
import {
  maskNonProseRegions,
  findWordSpans,
  isKnownWord as isKnownWordCore,
  suggestCorrections as suggestCorrectionsCore,
} from "./spellcheck-core.js";

const DICTIONARY_URL = "/static/data/dictionary-en.txt";
const PERSONAL_DICTIONARY_STORAGE_KEY = "latex-writer-personal-dictionary";

// ─── Dictionary loading (selectable word list + personal learned words) ───

const BUILTIN_DICTIONARIES = [
  { id: "en-US", label: "English (US)", url: "/static/data/dictionary-en.txt" },
  { id: "en-GB", label: "English (UK)", url: "/static/data/dictionary-en-GB.txt" },
];
const DEFAULT_DICTIONARY_ID = "en-US";
const ACTIVE_DICTIONARY_STORAGE_KEY = "latex-writer-active-dictionary";
const CUSTOM_DICTIONARIES_STORAGE_KEY = "latex-writer-custom-dictionaries";

// Original casing, used for suggestions (so a typo'd proper noun like
// "Amercia" can suggest "America", not a lowercased "america").
let dictionaryWords = new Set();
// A lowercase-only view of the same words, used for the actual
// known-word check (see isKnownWordCore's case-insensitive contract)
// -- built once instead of lowercasing dictionaryWords on every call.
let dictionaryWordsLower = new Set();
let dictionaryLoaded = false;
let activeDictionaryId = DEFAULT_DICTIONARY_ID;
// Guards against a slow earlier fetch overwriting a newer selection.
let dictionaryLoadToken = 0;
const dictionaryChangeListeners = new Set();

/**
 * Register a callback that runs every time a dictionary finishes
 * loading, including after the user switches language. It is not
 * called for a dictionary that was already loaded when subscribing,
 * so a subscriber must render once itself for the current state.
 *
 * @param {() => void} callback
 * @returns {() => void} Unsubscribe function.
 */
export function onDictionaryChange(callback) {
  dictionaryChangeListeners.add(callback);
  return () => dictionaryChangeListeners.delete(callback);
}

function parseWordList(text) {
  return text
    .split(/\r?\n/)
    .map((word) => word.trim())
    .filter(Boolean);
}

function applyWordList(words) {
  dictionaryWords = new Set(words);
  dictionaryWordsLower = new Set(Array.from(dictionaryWords, (word) => word.toLowerCase()));
}

function loadCustomDictionaries() {
  try {
    const raw = window.localStorage.getItem(CUSTOM_DICTIONARIES_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function saveCustomDictionaries(dictionaries) {
  window.localStorage.setItem(CUSTOM_DICTIONARIES_STORAGE_KEY, JSON.stringify(dictionaries));
}

/**
 * @returns {{id: string, label: string, builtin: boolean}[]} Every
 *   dictionary the user can select: bundled ones first, then any
 *   word lists they imported.
 */
export function listDictionaries() {
  const custom = Object.entries(loadCustomDictionaries()).map(([id, entry]) => ({
    id,
    label: entry.label,
    builtin: false,
  }));
  return [...BUILTIN_DICTIONARIES.map((d) => ({ id: d.id, label: d.label, builtin: true })), ...custom];
}

/**
 * @returns {string} Id of the dictionary currently used for checking.
 */
export function getActiveDictionaryId() {
  return activeDictionaryId;
}

async function fetchBuiltinWordList(dictionary) {
  const response = await fetch(dictionary.url);
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return parseWordList(await response.text());
}

/**
 * Switch the active dictionary, load its words, and notify every
 * open editor so underlines refresh. Persists the choice.
 *
 * @param {string} id - A dictionary id from `listDictionaries()`.
 * @returns {Promise<void>}
 * @throws {Error} If the id is unknown. A failed fetch does not
 *   throw: spell-check is disabled (empty word set) and a warning is
 *   logged, matching the module's original degrade-quietly behavior.
 */
export async function setActiveDictionary(id) {
  const builtin = BUILTIN_DICTIONARIES.find((d) => d.id === id);
  const custom = loadCustomDictionaries()[id];

  if (!builtin && !custom) {
    throw new Error(`unknown dictionary: ${id}`);
  }

  const token = ++dictionaryLoadToken;
  let words = [];

  try {
    words = builtin ? await fetchBuiltinWordList(builtin) : parseWordList(custom.words);
  } catch (error) {
    console.warn("spellcheck: could not load dictionary, spell-check disabled:", error.message);
  }

  if (token !== dictionaryLoadToken) return;

  applyWordList(words);
  activeDictionaryId = id;
  dictionaryLoaded = true;

  try {
    window.localStorage.setItem(ACTIVE_DICTIONARY_STORAGE_KEY, id);
  } catch {
    // Selection still applies for this session without persistence.
  }

  for (const listener of dictionaryChangeListeners) {
    listener();
  }
}

/**
 * Import a plain-text word list (one word per line) as a selectable
 * dictionary. Stored in this browser's localStorage, so it is a
 * per-browser preference like the personal dictionary.
 *
 * @param {string} label - Name shown in Settings.
 * @param {string} text - File contents, one word per line.
 * @returns {string} The new dictionary's id.
 * @throws {Error} If the list is empty or the browser refuses to
 *   store it (localStorage quota).
 */
export function addCustomDictionary(label, text) {
  const words = parseWordList(text);
  if (words.length === 0) {
    throw new Error("the word list is empty");
  }

  const dictionaries = loadCustomDictionaries();
  let id = `custom-${label.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
  while (id in dictionaries || BUILTIN_DICTIONARIES.some((d) => d.id === id)) {
    id += "-2";
  }

  dictionaries[id] = { label, words: words.join("\n") };
  saveCustomDictionaries(dictionaries);
  return id;
}

/**
 * Delete an imported dictionary. Falls back to the default when the
 * removed one was active. Bundled dictionaries cannot be removed.
 *
 * @param {string} id
 * @returns {Promise<void>}
 */
export async function removeCustomDictionary(id) {
  const dictionaries = loadCustomDictionaries();
  if (!(id in dictionaries)) return;

  delete dictionaries[id];
  saveCustomDictionaries(dictionaries);

  if (activeDictionaryId === id) {
    await setActiveDictionary(DEFAULT_DICTIONARY_ID);
  }
}

// Start loading immediately on module load (not lazily on first use)
// so it is as likely as possible to be ready by the time a user opens
// their first file.
(function loadInitialDictionary() {
  let stored = null;
  try {
    stored = window.localStorage.getItem(ACTIVE_DICTIONARY_STORAGE_KEY);
  } catch {
    stored = null;
  }

  const known = listDictionaries().some((d) => d.id === stored);
  setActiveDictionary(known ? stored : DEFAULT_DICTIONARY_ID);
})();

function loadPersonalDictionarySet() {
  try {
    const raw = window.localStorage.getItem(PERSONAL_DICTIONARY_STORAGE_KEY);
    return new Set(raw ? JSON.parse(raw) : []);
  } catch {
    return new Set();
  }
}

function savePersonalDictionarySet(words) {
  window.localStorage.setItem(
    PERSONAL_DICTIONARY_STORAGE_KEY,
    JSON.stringify(Array.from(words)),
  );
}

/**
 * @returns {string[]} The personal dictionary's words, sorted.
 */
export function getPersonalDictionary() {
  return Array.from(loadPersonalDictionarySet()).sort((a, b) => a.localeCompare(b));
}

/**
 * Add a word to the personal dictionary so it's never flagged again.
 * Stored lowercase, consistent with isKnownWord's case-insensitive
 * contract (see spellcheck-core.js).
 *
 * @param {string} word
 */
export function addToPersonalDictionary(word) {
  const words = loadPersonalDictionarySet();
  words.add(word.toLowerCase());
  savePersonalDictionarySet(words);
}

/**
 * @param {string} word
 */
export function removeFromPersonalDictionary(word) {
  const words = loadPersonalDictionarySet();
  words.delete(word.toLowerCase());
  savePersonalDictionarySet(words);
}

// ─── CodeMirror decorations (the red wavy underline) ───

const misspelledMark = Decoration.mark({ class: "cm-misspelled" });

function buildDecorations(view) {
  const builder = new RangeSetBuilder();
  const personalDictionary = loadPersonalDictionarySet();

  for (const { from, to } of view.visibleRanges) {
    const text = view.state.doc.sliceString(from, to);
    const masked = maskNonProseRegions(text);

    for (const span of findWordSpans(masked)) {
      if (!isKnownWordCore(span.word, dictionaryWordsLower, personalDictionary)) {
        builder.add(from + span.from, from + span.to, misspelledMark);
      }
    }
  }

  return builder.finish();
}

const spellCheckPlugin = ViewPlugin.fromClass(
  class {
    constructor(view) {
      this.decorations = dictionaryLoaded ? buildDecorations(view) : Decoration.none;
      this.unsubscribe = onDictionaryChange(() => {
        // Deferred to a microtask so a dispatch never lands while the
        // EditorView is still constructing or mid-update ("Calls to
        // EditorView.update are not allowed while an update is in
        // progress").
        queueMicrotask(() => {
          this.decorations = buildDecorations(view);
          view.dispatch({});
        });
      });
    }

    update(update) {
      if (update.docChanged || update.viewportChanged) {
        this.decorations = buildDecorations(update.view);
      }
    }

    destroy() {
      this.unsubscribe();
    }
  },
  { decorations: (instance) => instance.decorations },
);

// ─── Right-click menu: suggestions + "Learn spelling" ───

function closeMenu(menuEl, handler) {
  menuEl.remove();
  document.removeEventListener("click", handler, true);
  document.removeEventListener("keydown", handler, true);
}

function showSuggestionMenu({ x, y, word, onApply, onLearn }) {
  const menu = document.createElement("ul");
  menu.className = "context-menu";
  menu.setAttribute("role", "menu");

  const suggestions = suggestCorrectionsCore(word, dictionaryWords);

  if (suggestions.length === 0) {
    const li = document.createElement("li");
    const span = document.createElement("span");
    span.className = "context-menu-item empty-state";
    span.textContent = "No suggestions";
    li.appendChild(span);
    menu.appendChild(li);
  }

  let handler;

  for (const suggestion of suggestions) {
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "context-menu-item";
    button.setAttribute("role", "menuitem");
    button.textContent = suggestion;
    button.addEventListener("click", () => {
      closeMenu(menu, handler);
      onApply(suggestion);
    });
    li.appendChild(button);
    menu.appendChild(li);
  }

  const separator = document.createElement("li");
  separator.className = "menubar-separator";
  separator.setAttribute("role", "separator");
  menu.appendChild(separator);

  const learnLi = document.createElement("li");
  const learnButton = document.createElement("button");
  learnButton.type = "button";
  learnButton.className = "context-menu-item";
  learnButton.setAttribute("role", "menuitem");
  learnButton.textContent = `Learn spelling: "${word}"`;
  learnButton.addEventListener("click", () => {
    closeMenu(menu, handler);
    onLearn(word);
  });
  learnLi.appendChild(learnButton);
  menu.appendChild(learnLi);

  document.body.appendChild(menu);

  const rect = menu.getBoundingClientRect();
  const maxX = window.innerWidth - rect.width - 4;
  const maxY = window.innerHeight - rect.height - 4;
  menu.style.left = `${Math.max(4, Math.min(x, maxX))}px`;
  menu.style.top = `${Math.max(4, Math.min(y, maxY))}px`;

  handler = (event) => {
    if (event.type === "keydown" && event.key !== "Escape") return;
    if (event.type === "click" && menu.contains(event.target)) return;
    closeMenu(menu, handler);
  };

  setTimeout(() => {
    document.addEventListener("click", handler, true);
    document.addEventListener("keydown", handler, true);
  }, 0);
}

function findMisspelledWordAt(view, pos) {
  const pluginInstance = view.plugin(spellCheckPlugin);
  if (!pluginInstance) return null;

  let found = null;
  pluginInstance.decorations.between(pos, pos, (from, to) => {
    found = { from, to };
    return false;
  });

  return found;
}

const spellCheckContextMenuHandler = EditorView.domEventHandlers({
  contextmenu(event, view) {
    const pos = view.posAtCoords({ x: event.clientX, y: event.clientY });
    if (pos === null) return false;

    const range = findMisspelledWordAt(view, pos);
    if (!range) return false;

    event.preventDefault();
    const word = view.state.doc.sliceString(range.from, range.to);

    showSuggestionMenu({
      x: event.clientX,
      y: event.clientY,
      word,
      onApply: (suggestion) => {
        view.dispatch({
          changes: { from: range.from, to: range.to, insert: suggestion },
        });
        view.focus();
      },
      onLearn: (learnedWord) => {
        addToPersonalDictionary(learnedWord);
        view.dispatch({}); // force a redraw now that the word is known
      },
    });

    return true;
  },
});

/**
 * CodeMirror 6 extension providing spell-check underlines and a
 * right-click suggestion/learn menu. Include this in `createEditor`'s
 * extensions list.
 */
export const spellCheckExtension = [spellCheckPlugin, spellCheckContextMenuHandler];
