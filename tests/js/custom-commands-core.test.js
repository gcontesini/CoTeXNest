/**
 * custom-commands-core.test.js
 *
 * Run with `node --test tests/js/*.test.js` (see `just test-js`).
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { parseCustomCommands } from "../../src/webapp/static/js/custom-commands-core.js";

test("parses a zero-argument command with its trailing comment", () => {
  const result = parseCustomCommands(
    "\\newcommand{\\R}{\\mathbb{R}}                       % Real numbers symbol",
  );

  assert.deepEqual(result, [
    { name: "R", usage: "\\R", description: "Real numbers symbol" },
  ]);
});

test("builds usage from the argument count", () => {
  const [note] = parseCustomCommands("\\newcommand{\\note}[1]{{\\color{blue} #1}} % Note");
  const [pair] = parseCustomCommands("\\newcommand{\\charlienote}[2]{x}");

  assert.equal(note.usage, "\\note{#1}");
  assert.equal(pair.usage, "\\charlienote{#1}{#2}");
});

test("a default value makes the first argument optional", () => {
  const [image] = parseCustomCommands("\\newcommand{\\panelimage}[3][black]{%");

  assert.equal(image.usage, "\\panelimage[#1]{#2}{#3}");
  assert.equal(image.description, "");
});

test("accepts renewcommand, providecommand and DeclareMathOperator", () => {
  const names = parseCustomCommands(
    [
      "\\renewcommand{\\a}{1}",
      "\\providecommand{\\b}{2}",
      "\\DeclareMathOperator{\\argmax}{arg\\,max} % maximiser",
    ].join("\n"),
  ).map((command) => command.name);

  assert.deepEqual(names, ["a", "b", "argmax"]);
});

test("skips comment-only lines, body lines and blank text", () => {
  assert.deepEqual(parseCustomCommands(""), []);
  assert.deepEqual(parseCustomCommands("% PDF Annotation shortcuts\n\n  \\begin{tikzpicture}"), []);
});

test("ignores an escaped percent sign in the description", () => {
  const [command] = parseCustomCommands("\\newcommand{\\pct}{\\%} % shows a percent");

  assert.equal(command.description, "shows a percent");
});

test("returns an empty list for non-string input", () => {
  assert.deepEqual(parseCustomCommands(undefined), []);
});
