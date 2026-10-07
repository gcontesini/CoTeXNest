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

import { Annotation, EditorState } from "@codemirror/state";
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
  indentOnInput,
  bracketMatching,
} from "@codemirror/language";
import { searchKeymap, highlightSelectionMatches } from "@codemirror/search";
import { stex } from "@codemirror/legacy-modes/mode/stex";

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
 * @returns {((...args: any[]) => void) & { cancel: () => void }} Debounced function with a .cancel() escape hatch.
 */
function debounce(fn, delayMs) {
  let timerId = null;

  function debounced(...args) {
    if (timerId !== null) {
      clearTimeout(timerId);
    }

    timerId = setTimeout(() => {
      timerId = null;
      fn(...args);
    }, delayMs);
  }

  debounced.cancel = () => {
    if (timerId !== null) {
      clearTimeout(timerId);
      timerId = null;
    }
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
 * @returns {{ getValue: () => string, setValue: (text: string) => void, destroy: () => void }}
 */
export function createEditor({ parent, doc = "", onChange = () => {} }) {
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

  const extensions = [
    lineNumbers(),
    highlightActiveLine(),
    highlightActiveLineGutter(),
    history(),
    indentOnInput(),
    bracketMatching(),
    highlightSelectionMatches(),
    syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
    keymap.of([...defaultKeymap, ...historyKeymap, ...searchKeymap, indentWithTab]),
    EditorView.lineWrapping,
    updateListener,
  ];

  if (latexLanguageExtension !== null) {
    extensions.push(latexLanguageExtension);
  }

  const state = EditorState.create({ doc, extensions });
  const view = new EditorView({ state, parent });

  return {
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
     * Tear down the editor and cancel any pending debounced autosave.
     */
    destroy() {
      debouncedOnChange.cancel();
      view.destroy();
    },
  };
}
