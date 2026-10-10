/**
 * editor.js
 *
 * Wraps a CodeMirror 6 EditorView so the rest of the app never has to
 * touch CodeMirror internals directly. See the import map in
 * index.html for which CDN build each "@codemirror/*" specifier
 * resolves to.
 *
 * LaTeX syntax highlighting note: there is no actively-maintained,
 * unambiguous "the" LaTeX language package for CodeMirror 6 on npm
 * that this agent could verify exists without network access in this
 * authoring session. Rather than invent a package name, this module
 * uses the official "@codemirror/legacy-modes" package, which ports
 * CodeMirror 5's stream-parser modes -- including "stex" (the mode
 * historically used for LaTeX/TeX) -- to CodeMirror 6 via
 * StreamLanguage.define(). This gives real LaTeX-aware tokenization
 * (commands, braces, math mode, comments) using a package this agent
 * is confident is real and current. If that import ever fails to
 * resolve, the editor still works as a plain-text editor (see the
 * try/catch below) rather than throwing.
 */

import { Annotation, Compartment, EditorState } from "@codemirror/state";
import {
  EditorView,
  keymap,
  lineNumbers,
  highlightActiveLine,
  highlightActiveLineGutter,
} from "@codemirror/view";
import {
  defaultKeymap,
  history,
  historyKeymap,
  indentWithTab,
} from "@codemirror/commands";
import {
  StreamLanguage,
  syntaxHighlighting,
  defaultHighlightStyle,
  HighlightStyle,
  indentUnit,
  indentOnInput,
  bracketMatching,
} from "@codemirror/language";
import { searchKeymap, highlightSelectionMatches } from "@codemirror/search";
import { stex } from "@codemirror/legacy-modes/mode/stex";
import { tags } from "@lezer/highlight";
import { spellCheckExtension } from "./spellcheck.js";

// A second syntax-color option alongside CodeMirror's own
// defaultHighlightStyle -- deliberately a small, conservative set of
// tags (the same ones defaultHighlightStyle itself covers) rather
// than an exhaustive theme, since this project has no way to visually
// regression-test a syntax theme.
const HIGH_CONTRAST_HIGHLIGHT_STYLE = HighlightStyle.define([
  { tag: tags.keyword, color: "#ff2d95", fontWeight: "bold" },
  { tag: tags.comment, color: "#6a9955", fontStyle: "italic" },
  { tag: tags.string, color: "#ce9178" },
  { tag: tags.number, color: "#b5cea8" },
  { tag: tags.operator, color: "#ff2d95" },
  { tag: tags.bracket, color: "#ffd700", fontWeight: "bold" },
  { tag: tags.variableName, color: "#4fc1ff" },
  { tag: tags.heading, color: "#ffa500", fontWeight: "bold" },
]);

function highlightStyleFor(colorScheme) {
  return colorScheme === "high-contrast" ? HIGH_CONTRAST_HIGHLIGHT_STYLE : defaultHighlightStyle;
}

const AUTOSAVE_DEBOUNCE_MS = 800;

// Marks a dispatched transaction as programmatic (e.g. setValue())
// so the debounced onChange callback -- which exists to drive
// autosave of user edits -- does not fire when the app loads a file
// into the editor.
const programmaticUpdate = Annotation.define();

/**
 * Build a debounced wrapper around a function.
 *
 * @param {(...args: any[]) => void} fn - Function to debounce.
 * @param {number} delayMs - Idle delay, in milliseconds, before fn runs.
 * @returns {((...args: any[]) => void) & { cancel: () => void, flush: () => void }}
 *   Debounced function with `.cancel()` (discard, don't run) and
 *   `.flush()` (run immediately with the latest pending args, if any)
 *   escape hatches.
 */
function debounce(fn, delayMs) {
  let timerId = null;
  let pendingArgs = null;

  function debounced(...args) {
    pendingArgs = args;

    if (timerId !== null) {
      clearTimeout(timerId);
    }

    timerId = setTimeout(() => {
      timerId = null;
      const args = pendingArgs;
      pendingArgs = null;
      fn(...args);
    }, delayMs);
  }

  debounced.cancel = () => {
    if (timerId !== null) {
      clearTimeout(timerId);
      timerId = null;
    }
    pendingArgs = null;
  };

  debounced.flush = () => {
    if (timerId === null) {
      return;
    }

    clearTimeout(timerId);
    timerId = null;
    const args = pendingArgs;
    pendingArgs = null;
    fn(...args);
  };

  return debounced;
}

/**
 * Create a CodeMirror 6 editor bound to a DOM element.
 *
 * @param {object} options
 * @param {Element} options.parent - DOM element the editor mounts into.
 * @param {string} [options.doc] - Initial document text.
 * @param {(text: string) => void} [options.onChange] - Called roughly
 *   800ms after the user stops typing, with the full current document
 *   text. Not called for programmatic setValue() updates.
 * @param {number} [options.indentSize] - Spaces per indent level.
 * @param {boolean} [options.highlightActiveLineEnabled]
 * @param {boolean} [options.highlightMatchingBracketsEnabled]
 * @param {"default"|"high-contrast"} [options.colorScheme]
 * @returns {{
 *   getValue: () => string,
 *   setValue: (text: string) => void,
 *   flush: () => void,
 *   destroy: () => void,
 *   reconfigure: (settings: {indentSize?: number, highlightActiveLineEnabled?: boolean, highlightMatchingBracketsEnabled?: boolean, colorScheme?: string}) => void,
 * }}
 */
export function createEditor({
  parent,
  doc = "",
  onChange = () => {},
  indentSize = 2,
  highlightActiveLineEnabled = true,
  highlightMatchingBracketsEnabled = true,
  colorScheme = "default",
}) {
  if (!(parent instanceof Element)) {
    throw new Error("createEditor: parent must be a DOM Element");
  }

  const debouncedOnChange = debounce((text) => {
    onChange(text);
  }, AUTOSAVE_DEBOUNCE_MS);

  const updateListener = EditorView.updateListener.of((update) => {
    if (!update.docChanged) {
      return;
    }

    const isProgrammatic = update.transactions.some((transaction) =>
      transaction.annotation(programmaticUpdate),
    );

    if (!isProgrammatic) {
      debouncedOnChange(update.state.doc.toString());
    }
  });

  let latexLanguageExtension = null;

  try {
    latexLanguageExtension = StreamLanguage.define(stex);
  } catch (error) {
    console.warn(
      "editor.js: LaTeX syntax mode unavailable, falling back to " +
        "plain-text editing.",
      error,
    );
  }

  // Compartments let Settings changes reconfigure an already-mounted
  // editor live (preserving cursor position/undo history) instead of
  // requiring the file to be reopened to take effect.
  const indentCompartment = new Compartment();
  const activeLineCompartment = new Compartment();
  const bracketMatchCompartment = new Compartment();
  const highlightStyleCompartment = new Compartment();

  const extensions = [
    lineNumbers(),
    indentCompartment.of(indentUnit.of(" ".repeat(indentSize))),
    activeLineCompartment.of(
      highlightActiveLineEnabled ? [highlightActiveLine(), highlightActiveLineGutter()] : [],
    ),
    history(),
    indentOnInput(),
    bracketMatchCompartment.of(highlightMatchingBracketsEnabled ? bracketMatching() : []),
    highlightSelectionMatches(),
    highlightStyleCompartment.of(syntaxHighlighting(highlightStyleFor(colorScheme), { fallback: true })),
    keymap.of([...defaultKeymap, ...historyKeymap, ...searchKeymap, indentWithTab]),
    EditorView.lineWrapping,
    updateListener,
    spellCheckExtension,
  ];

  if (latexLanguageExtension !== null) {
    extensions.push(latexLanguageExtension);
  }

  const state = EditorState.create({ doc, extensions });
  const view = new EditorView({ state, parent });

  return {
    /**
     * Apply new Settings values to the currently mounted editor
     * without remounting it. Any field omitted is left unchanged.
     *
     * @param {object} settings
     */
    reconfigure({
      indentSize: newIndentSize,
      highlightActiveLineEnabled: newHighlightActiveLine,
      highlightMatchingBracketsEnabled: newHighlightBrackets,
      colorScheme: newColorScheme,
    } = {}) {
      const effects = [];

      if (newIndentSize !== undefined) {
        effects.push(indentCompartment.reconfigure(indentUnit.of(" ".repeat(newIndentSize))));
      }

      if (newHighlightActiveLine !== undefined) {
        effects.push(
          activeLineCompartment.reconfigure(
            newHighlightActiveLine ? [highlightActiveLine(), highlightActiveLineGutter()] : [],
          ),
        );
      }

      if (newHighlightBrackets !== undefined) {
        effects.push(
          bracketMatchCompartment.reconfigure(newHighlightBrackets ? bracketMatching() : []),
        );
      }

      if (newColorScheme !== undefined) {
        effects.push(
          highlightStyleCompartment.reconfigure(
            syntaxHighlighting(highlightStyleFor(newColorScheme), { fallback: true }),
          ),
        );
      }

      if (effects.length > 0) {
        view.dispatch({ effects });
      }
    },

    /**
     * @returns {string} The editor's current full document text.
     */
    getValue() {
      return view.state.doc.toString();
    },

    /**
     * Replace the entire document, without triggering onChange.
     *
     * @param {string} text - New document text.
     */
    setValue(text) {
      const nextText = text ?? "";

      view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: nextText },
        annotations: programmaticUpdate.of(true),
        selection: { anchor: 0 },
      });
    },

    /**
     * Run any pending debounced autosave immediately, synchronously
     * (the save itself is still async, but the call to trigger it
     * happens before this returns) -- call this before anything reads
     * or replaces the editor's content out from under a pending save
     * (switching the open file or project), so an edit made in the
     * last `AUTOSAVE_DEBOUNCE_MS` isn't silently discarded (`destroy`)
     * or, worse, saved against the *new* file/project's path once the
     * caller reassigns it (`setValue`).
     */
    flush() {
      debouncedOnChange.flush();
    },

    /**
     * Tear down the editor, flushing any pending debounced autosave
     * first rather than discarding it.
     */
    destroy() {
      debouncedOnChange.flush();
      view.destroy();
    },
  };
}
