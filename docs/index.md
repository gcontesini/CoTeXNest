# LaTeX Writer

A local-first, self-hosted LaTeX editor: web editor with PDF preview,
git-backed version history, and a LaTeX package/template manager.
Think Overleaf, but it runs on your own machine or server.

```{toctree}
:maxdepth: 2
:caption: Contents

getting-started
editor-ui
architecture
package-manager
history
api-reference
```

## Building these docs

Every page here is a plain Markdown file under `docs/` in the
repository -- readable directly in a text editor or on GitHub, with no
Sphinx build required. The same files are also this Sphinx site, via
the `myst-parser` extension (see `docs/conf.py`), so there is one copy
of the documentation, not two.

To build this site locally:

```bash
just docs-build
```

The built HTML lands in `docs/_build/html/index.html`. A GitHub
Actions workflow (`.github/workflows/docs.yml`) rebuilds and publishes
this same site to GitHub Pages on every push to `main`.
