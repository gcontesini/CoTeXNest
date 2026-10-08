"""Sphinx configuration for the webapp documentation.

Builds the same Markdown files under ``docs/`` that are also readable
directly in a text editor or on GitHub, via MyST -- so there is one
set of docs, not a Markdown copy and a separate Sphinx copy.
"""

from __future__ import annotations

project = "LaTeX Writer"
copyright = "2026, LaTeX Writer contributors"
author = "LaTeX Writer contributors"

extensions = [
  "myst_parser",
]

# Lets in-page links like "architecture.md#deployment-model" (the
# anchors GitHub auto-generates for Markdown headings) resolve here
# too, instead of only working when these files are read on GitHub.
myst_heading_anchors = 4

source_suffix = {
  ".md": "markdown",
  ".rst": "restructuredtext",
}

# "superpowers" holds internal planning/spec documents (see
# docs/superpowers/specs/), not user-facing documentation -- excluded
# from the built site the same way _build (Sphinx's own output) is.
exclude_patterns = ["_build", "superpowers", "Thumbs.db", ".DS_Store"]

html_theme = "furo"
html_title = "LaTeX Writer Documentation"
