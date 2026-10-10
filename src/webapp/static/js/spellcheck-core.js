/**
 * spellcheck-core.js
 *
 * Pure spell-checking logic with zero browser-API dependencies (no
 * `fetch`, `localStorage`, or CodeMirror) -- split out from
 * spellcheck.js specifically so it can be unit-tested directly under
 * plain Node (`node --test`), which the CodeMirror-wired module
 * cannot be, since this project has no bundler to resolve its
 * `@codemirror/*` bare imports outside the browser's import map.
 *
 * See spellcheck.js for what this is used for and the LaTeX-awareness
 * caveats (comments/math/commands excluded, no full parser).
 */

const COMMENT_PATTERN = /(^|[^\\])%.*$/gm;
const MATH_PATTERN = /\$\$[\s\S]*?\$\$|\$[^$\n]*\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)/g;
const COMMAND_PATTERN = /\\[a-zA-Z]+\*?(\[[^\]\n]*\])?/g;
const WORD_PATTERN = /[A-Za-z][A-Za-z']*/g;

function blank(match) {
  return " ".repeat(match.length);
}

/**
 * Replace LaTeX comments, math mode, and command names with spaces,
 * preserving every other character's position exactly (so offsets
 * into the original text stay valid for CodeMirror decorations).
 *
 * @param {string} text
 * @returns {string}
 */
export function maskNonProseRegions(text) {
  return text
    .replace(COMMENT_PATTERN, (match, prefix) => prefix + blank(match.slice(prefix.length)))
    .replace(MATH_PATTERN, blank)
    .replace(COMMAND_PATTERN, blank);
}

/**
 * Find every spell-checkable word span in `text`.
 *
 * @param {string} text - Already masked via `maskNonProseRegions`.
 * @returns {Array<{from: number, to: number, word: string}>}
 */
export function findWordSpans(text) {
  const spans = [];

  for (const match of text.matchAll(WORD_PATTERN)) {
    let word = match[0];
    let from = match.index;

    while (word.startsWith("'")) {
      word = word.slice(1);
      from += 1;
    }
    while (word.endsWith("'")) {
      word = word.slice(0, -1);
    }

    if (word.length > 1) {
      spans.push({ from, to: from + word.length, word });
    }
  }

  return spans;
}

/**
 * Case-insensitive dictionary lookup: both `dictionary` and
 * `personalDictionary` are expected to already be lowercase (the
 * loading code in spellcheck.js guarantees this) -- a typed word is
 * lowercased here before comparing, so a correctly-spelled word typed
 * in the wrong case (e.g. "america" instead of "America") is not
 * flagged as misspelled. Catching capitalization specifically is a
 * different, narrower problem most spell-checkers don't attempt
 * either, and this one doesn't.
 *
 * @param {string} word
 * @param {Set<string>} dictionary
 * @param {Set<string>} [personalDictionary]
 * @returns {boolean}
 */
export function isKnownWord(word, dictionary, personalDictionary = new Set()) {
  const lower = word.toLowerCase();

  if (dictionary.has(lower) || personalDictionary.has(lower)) {
    return true;
  }

  if (
    lower.endsWith("'s") &&
    (dictionary.has(lower.slice(0, -2)) || personalDictionary.has(lower.slice(0, -2)))
  ) {
    return true;
  }

  return false;
}

/**
 * Classic Wagner-Fischer edit distance, O(len(a) * len(b)) time and
 * O(min(len(a), len(b))) space.
 *
 * @param {string} a
 * @param {string} b
 * @returns {number}
 */
export function levenshteinDistance(a, b) {
  if (a === b) return 0;
  if (a.length === 0) return b.length;
  if (b.length === 0) return a.length;

  let previousRow = Array.from({ length: b.length + 1 }, (_, i) => i);
  let currentRow = new Array(b.length + 1);

  for (let i = 1; i <= a.length; i += 1) {
    currentRow[0] = i;

    for (let j = 1; j <= b.length; j += 1) {
      const substitutionCost = a[i - 1] === b[j - 1] ? 0 : 1;
      currentRow[j] = Math.min(
        previousRow[j] + 1,
        currentRow[j - 1] + 1,
        previousRow[j - 1] + substitutionCost,
      );
    }

    [previousRow, currentRow] = [currentRow, previousRow];
  }

  return previousRow[b.length];
}

/**
 * Suggest corrections for a misspelled word by edit distance against
 * the dictionary, filtered first by length and first letter for
 * performance (a standard, if simple, spellchecker heuristic -- there
 * is no phonetic or frequency-based ranking here, only edit distance).
 *
 * @param {string} word
 * @param {Set<string>} dictionary
 * @param {number} [maxSuggestions]
 * @returns {string[]}
 */
export function suggestCorrections(word, dictionary, maxSuggestions = 5) {
  const lower = word.toLowerCase();
  const maxDistance = lower.length <= 4 ? 1 : 2;
  const candidates = [];

  for (const candidate of dictionary) {
    if (Math.abs(candidate.length - lower.length) > maxDistance) {
      continue;
    }

    if (candidate[0]?.toLowerCase() !== lower[0]) {
      continue;
    }

    const distance = levenshteinDistance(lower, candidate.toLowerCase());

    if (distance > 0 && distance <= maxDistance) {
      candidates.push({ candidate, distance });
    }
  }

  candidates.sort(
    (a, b) => a.distance - b.distance || a.candidate.length - b.candidate.length,
  );

  const seen = new Set();
  const results = [];

  for (const { candidate } of candidates) {
    const key = candidate.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    results.push(candidate);
    if (results.length >= maxSuggestions) break;
  }

  return results;
}
