# Package Manager

The package manager installs LaTeX packages and project templates so
they are available to every project without copying files into each
project individually.

## Packages vs. templates

The catalog mixes two kinds of entries, distinguished by a `kind`
field:

- **`package`**: an installable LaTeX dependency, typically one or more
  `.sty`/`.cls` files (or supporting assets such as images or `.bib`
  files). Installing a package adds its files to the shared `plugins/`
  directory, where every project can use it during compilation.
- **`template`**: a starter set of project files (for example a
  preconfigured `main.tex`, a class file, and a bibliography skeleton).
  A template is used only once, at project-creation time, to seed a new
  project's starting files. It is never "installed" as an ongoing
  dependency, and it does not appear in the list of installed packages.

Both kinds live in the same catalog and are searched together; the
`kind` field is what the UI and the install/seed logic use to tell them
apart.

## Where entries come from

The bundled catalog is a curated, read-only JSON file shipped with the
application (`src/webapp/data/catalog.json`). It is searchable from the
UI's package manager panel. Browsing and searching it does not touch
disk beyond reading that file.

Users can also add their own files directly, without going through the
catalog, using "add local file." This covers a `.sty`, `.cls`, `.tex`,
or any other file a user already has that is not in the bundled
catalog.

## What happens on disk

Installing a catalog package, or adding a local file, writes its files
under:

```
plugins/<package_id>/<file_name>
```

`plugins/` is the single global, shared directory described in
[architecture.md](architecture.md): there is one copy of each installed
package, not one copy per project. Removing a package deletes its
subdirectory under `plugins/` and makes it unavailable to every project
that depended on it (existing project files referencing it are left
untouched; only the next compile is affected).

Seeding a project from a template is different: template files are
copied into the new project's own directory under `workspace/<project_id>/`
at creation time. After that point the template's files are
ordinary project files like any other, tracked in that project's own
git history, with no ongoing link back to the catalog entry they came
from.

## Searching, installing, and removing

From the package manager panel in the UI:

- **Search** filters the bundled catalog by name, description, or tag.
  This covers both packages and templates.
- **Install** (packages only) copies the catalog entry's files into
  `plugins/<package_id>/` and marks it as installed. It becomes
  available to every project's next compile, with no per-project
  action required.
- **Add local file** uploads a file directly into `plugins/`, without a
  corresponding catalog entry. This is how a user adds a package that
  is not in the bundled catalog.
- **Remove** deletes an installed package's files from `plugins/`. Any
  project whose `.tex` source still references that package will fail
  to compile afterward, with a log explaining the missing file, until
  the package is reinstalled or the reference is removed.

Templates do not have an install/remove action in this panel: they are
selected when creating a project, not managed as standing dependencies.

## TEXINPUTS, in plain terms

`TEXINPUTS` is an environment variable that tells the LaTeX compiler
where to look for files it cannot find in the current directory, such
as a `.cls` or `.sty` file referenced by `\documentclass` or
`\usepackage`. By default, the compiler only looks in the project's own
directory and in TeX Live's built-in package set.

Every compile request sets `TEXINPUTS` to search, in order:

1. the shared `plugins/` directory (so any installed package is found)
2. the project's own directory (so the user's own files take priority)
3. TeX Live's normal built-in search path (the fallback for anything
   not covered by the above)

This is what makes "install once, usable in every project" work: no
file ever needs to be copied into a project for the compiler to find
it. See `services/latex_render.py` for the exact mechanism.
