/**
 * spellcheck.js
 *
 * Browser/CodeMirror wiring on top of spellcheck-core.js's pure logic:
 * fetches the English dictionary once, manages a personal dictionary
 * in localStorage, and provides a CodeMirror 6 extension that
 * underlines unrecognized words and offers a right-click menu of
 * corrections plus "Learn spelling".
 *
 * Dictionary data: static/data/dictionary-en.txt, the real
 * "american-english" word list from Debian's `wamerican` package
 * (itself SCOWL -- Spell Checker Oriented Word Lists). See
 * static/data/dictionary-en.LICENSE.txt for the license this file is
 * shipped under.
 *
 * Scope, stated plainly: this checks English only. The personal
 * dictionary mechanism is generic (just a set of accepted words), so
 * a second language would only need its own word-list file and a way
 * to pick it in Settings -- but no second language's word list is
 * bundled here, since fabricating one without a real, verifiable
 * source would be worse than not offering it.
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

// ─── Dictionary loading (English word list + personal learned words) ───

// Original casing, used for suggestions (so a typo'd proper noun like
// "Amercia" can suggest "America", not a lowercased "america").
let dictionaryWords = new Set();
// A lowercase-only view of the same words, used for the actual
// known-word check (see isKnownWordCore's case-insensitive contract)
// -- built once instead of lowercasing dictionaryWords on every call.
let dictionaryWordsLower = new Set();
let dictionaryLoaded = false;
const dictionaryReadyListeners = new Set();

/**
 * Register a callback to run once the dictionary has finished
 * loading (immediately, if it already has). Used so an editor
 * instance mounted before the fetch resolves can refresh its
 * decorations once real data is available, instead of staying
 * "nothing is misspelled" forever.
 *
 * @param {() => void} callback
 * @returns {() => void} Unsubscribe function.
 */
export function onDictionaryReady(callback) {
  if (dictionaryLoaded) {
    callback();
    return () => {};
  }

  dictionaryReadyListeners.add(callback);
  return () => dictionaryReadyListeners.delete(callback);
}

// Kick off the fetch immediately on module load (not lazily on first
// use) so it's as likely as possible to already be done by the time a
// user opens their first file.
fetch(DICTIONARY_URL)
  .then((response) => {
    if (!response.ok) {
      throw new Error(`${response.status} ${response.statusText}`);
    }
    return response.text();
  })
  .then((text) => {
    dictionaryWords = new Set(text.split("\n").map((word) => word.trim()).filter(Boolean));
    dictionaryWordsLower = new Set(
      Array.from(dictionaryWords, (word) => word.toLowerCase()),
    );
  })
  .catch((error) => {
    console.warn("spellcheck: could not load dictionary, spell-check disabled:", error.message);
  })
  .finally(() => {
    dictionaryLoaded = true;
    for (const listener of dictionaryReadyListeners) {
      listener();
    }
    dictionaryReadyListeners.clear();
  });

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
      this.unsubscribe = onDictionaryReady(() => {
        // Deferred: onDictionaryReady's callback can fire synchronously
        // (if the dictionary is already loaded) from within this very
        // constructor call, which itself runs as part of the
        // EditorView's own construction -- dispatching a transaction
        // on a view that hasn't finished constructing throws ("Calls
        // to EditorView.update are not allowed while an update is in
        // progress"), caught live via a real headless-browser check.
        // Deferring to a microtask guarantees this always runs after
        // the view exists, whether the callback fired sync or later.
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
