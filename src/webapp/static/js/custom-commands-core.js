/**
 * custom-commands-core.js
 *
 * Pure parsing of the user's custom-commands text into a list the
 * Shortcuts dialog can show. No DOM and no `@codemirror/*` imports, so
 * it runs under plain Node (see tests/js/custom-commands-core.test.js).
 *
 * This is a best-effort reader, not a TeX parser: it only looks at the
 * first line of each definition, and anything it does not recognise is
 * skipped rather than reported. Validity stays the author's job.
 */

const DEFINITION_PATTERN =
  /^\s*\\(?:re)?newcommand\*?\s*\{?\\([A-Za-z@]+)\}?\s*(?:\[(\d)\])?(\[[^\]]*\])?/;
const PROVIDE_PATTERN =
  /^\s*\\providecommand\*?\s*\{?\\([A-Za-z@]+)\}?\s*(?:\[(\d)\])?(\[[^\]]*\])?/;
const MATH_OPERATOR_PATTERN = /^\s*\\DeclareMathOperator\*?\s*\{\\([A-Za-z@]+)\}/;
const TRAILING_COMMENT_PATTERN = /(?<!\\)%\s*(.*)$/;

/**
 * @param {string} line - One source line.
 * @returns {string} The text of a trailing `%` comment, or "".
 */
function trailingComment(line) {
  const match = TRAILING_COMMENT_PATTERN.exec(line);
  return match ? match[1].trim() : "";
}

/**
 * @param {string} name - Command name without the backslash.
 * @param {number} argCount - Number of arguments declared with `[n]`.
 * @param {boolean} hasOptional - Whether a default for the first
 *   argument was given, making it an optional `[...]` argument.
 * @returns {string} A usage string such as `\panelimage[#1]{#2}{#3}`.
 */
function buildUsage(name, argCount, hasOptional) {
  let usage = `\\${name}`;

  for (let index = 1; index <= argCount; index += 1) {
    usage += hasOptional && index === 1 ? `[#${index}]` : `{#${index}}`;
  }

  return usage;
}

/**
 * @param {string} text - The full custom-commands source.
 * @returns {Array<{name: string, usage: string, description: string}>}
 *   One entry per recognised definition, in source order.
 */
export function parseCustomCommands(text) {
  if (typeof text !== "string") {
    return [];
  }

  const commands = [];

  for (const line of text.split(/\r?\n/)) {
    if (line.trim().startsWith("%")) {
      continue;
    }

    const operator = MATH_OPERATOR_PATTERN.exec(line);

    if (operator) {
      commands.push({
        name: operator[1],
        usage: `\\${operator[1]}`,
        description: trailingComment(line),
      });
      continue;
    }

    const definition = DEFINITION_PATTERN.exec(line) || PROVIDE_PATTERN.exec(line);

    if (!definition) {
      continue;
    }

    const argCount = definition[2] ? Number(definition[2]) : 0;
    commands.push({
      name: definition[1],
      usage: buildUsage(definition[1], argCount, Boolean(definition[3])),
      description: trailingComment(line),
    });
  }

  return commands;
}
