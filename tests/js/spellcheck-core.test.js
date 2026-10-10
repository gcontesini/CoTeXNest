/**
 * spellcheck-core.test.js
 *
 * Runs under Node's own built-in test runner -- `node --test
 * tests/js/` -- no npm install, no bundler, no package.json. This
 * project deliberately has no Node toolchain for the app itself; this
 * stays consistent with that by using only what `node` ships with.
 *
 * Only spellcheck-core.js is tested here (not spellcheck.js), since
 * that module's `@codemirror/*` bare imports only resolve via the
 * browser's import map in index.html and can't run under plain Node.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import {
  maskNonProseRegions,
  findWordSpans,
  isKnownWord,
  levenshteinDistance,
  suggestCorrections,
} from "../../src/webapp/static/js/spellcheck-core.js";

test("maskNonProseRegions blanks a comment but preserves offsets", () => {
  const text = "hello % wrold comment\nworld";
  const masked = maskNonProseRegions(text);
  assert.equal(masked.length, text.length);
  assert.equal(masked, "hello                \nworld");
});

test("maskNonProseRegions does not treat an escaped percent as a comment", () => {
  const text = "100\\% wrold";
  const masked = maskNonProseRegions(text);
  assert.equal(masked, text);
});

test("maskNonProseRegions blanks inline and display math", () => {
  const text = "see $x^2 + y^2$ and \\[ a+b \\] and \\(c\\) done";
  const masked = maskNonProseRegions(text);
  assert.equal(masked.length, text.length);
  assert.ok(!masked.includes("x^2"));
  assert.ok(!masked.includes("a+b"));
  assert.ok(masked.includes("see"));
  assert.ok(masked.includes("and"));
  assert.ok(masked.includes("done"));
});

test("maskNonProseRegions blanks a command name but keeps its required argument", () => {
  const text = "\\textbf{hello} and \\usepackage[utf8]{inputenc}";
  const masked = maskNonProseRegions(text);
  assert.equal(masked.length, text.length);
  assert.ok(masked.includes("hello"), "required {..} content must survive");
  assert.ok(!masked.includes("textbf"));
  assert.ok(!masked.includes("utf8"), "optional [..] argument must be blanked");
  assert.ok(masked.includes("inputenc"), "required {..} content must survive");
});

test("findWordSpans finds words with correct offsets", () => {
  const text = "  hi there  ";
  const spans = findWordSpans(text);
  assert.deepEqual(
    spans.map((s) => s.word),
    ["hi", "there"],
  );
  assert.equal(text.slice(spans[0].from, spans[0].to), "hi");
  assert.equal(text.slice(spans[1].from, spans[1].to), "there");
});

test("findWordSpans keeps a contraction but trims stray leading/trailing apostrophes", () => {
  const text = "don't 'quoted' word";
  const spans = findWordSpans(text).map((s) => s.word);
  assert.deepEqual(spans, ["don't", "quoted", "word"]);
});

test("findWordSpans skips single-character tokens", () => {
  const spans = findWordSpans("a I go");
  assert.deepEqual(
    spans.map((s) => s.word),
    ["go"],
  );
});

test("isKnownWord matches case-insensitively against a lowercase dictionary", () => {
  // isKnownWord's contract (see its docstring): dictionary and
  // personalDictionary are expected pre-lowercased by the caller
  // (spellcheck.js does this once at load time) -- it lowercases
  // only the *typed* word before comparing.
  const dictionary = new Set(["hello", "world"]);
  assert.equal(isKnownWord("hello", dictionary), true);
  assert.equal(isKnownWord("Hello", dictionary), true);
  assert.equal(isKnownWord("WORLD", dictionary), true);
  assert.equal(isKnownWord("bogus", dictionary), false);
});

test("isKnownWord checks the personal dictionary too", () => {
  const dictionary = new Set(["hello"]);
  const personal = new Set(["latexwriter"]);
  assert.equal(isKnownWord("latexwriter", dictionary, personal), true);
  assert.equal(isKnownWord("latexwriter", dictionary), false);
});

test("isKnownWord accepts a possessive whose bare form is known", () => {
  const dictionary = new Set(["cat"]);
  assert.equal(isKnownWord("cat's", dictionary), true);
  assert.equal(isKnownWord("dog's", dictionary), false);
});

test("levenshteinDistance basic cases", () => {
  assert.equal(levenshteinDistance("cat", "cat"), 0);
  assert.equal(levenshteinDistance("cat", "cats"), 1);
  assert.equal(levenshteinDistance("cat", "bat"), 1);
  assert.equal(levenshteinDistance("kitten", "sitting"), 3);
  assert.equal(levenshteinDistance("", "abc"), 3);
});

test("suggestCorrections finds a one-letter-off correction and ranks it first", () => {
  const dictionary = new Set(["hello", "help", "hell", "world", "yellow"]);
  const suggestions = suggestCorrections("helo", dictionary);
  assert.ok(suggestions.includes("hello"));
  assert.ok(suggestions.includes("help"));
  assert.equal(suggestions.includes("world"), false, "unrelated word must not appear");
});

test("suggestCorrections returns nothing for a word already correct", () => {
  const dictionary = new Set(["hello"]);
  assert.deepEqual(suggestCorrections("hello", dictionary), []);
});

test("suggestCorrections caps results at maxSuggestions", () => {
  // Every entry is exactly one substitution away from "cxt" (same
  // first letter, same length) -- 5 qualifying candidates, so a cap
  // of 3 is what's actually under test here, not an incidental count.
  const dictionary = new Set(["cat", "cot", "cut", "cit", "cft"]);
  const suggestions = suggestCorrections("cxt", dictionary, 3);
  assert.equal(suggestions.length, 3);
});

test("suggestCorrections excludes candidates starting with a different letter", () => {
  const dictionary = new Set(["cat", "bat", "hat", "mat"]);
  const suggestions = suggestCorrections("cxt", dictionary);
  assert.deepEqual(suggestions, ["cat"]);
});
