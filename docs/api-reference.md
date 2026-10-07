# API Reference

All endpoints are served under the same FastAPI application as the
static frontend, on the port the application is deployed on (8000 by
default; see [getting-started.md](getting-started.md)). Request and
response bodies are JSON. Field names below match the Pydantic models
in `src/webapp/models.py`; this is a field-name reference, not a full
JSON Schema dump.

## Projects and files

| Method | Path | Request body | Response |
|---|---|---|---|
| GET | `/api/projects` | none | list of `ProjectSummary`: `id`, `name`, `created_at`, `main_file` |
| POST | `/api/projects` | `ProjectCreate`: `name`, `template_id` (optional) | `ProjectSummary` |
| DELETE | `/api/projects/{id}` | none | `{"success": true}` |
| GET | `/api/projects/{id}/files` | none | `FileNode` tree: `path`, `type` (`"file"`/`"dir"`), `children` |
| GET | `/api/projects/{id}/files/{path}` | none | `FileContent`: `path`, `content` |
| PUT | `/api/projects/{id}/files/{path}` | `FileWrite`: `content` | `FileContent`: `path`, `content` |
| DELETE | `/api/projects/{id}/files/{path}` | none | `{"success": true}` |

`{path}` may contain slashes (nested file paths within the project).
Creating a project with `template_id` set seeds the new project's files
from that catalog template; omitting it creates a project with a
default `main.tex`. Deleting a file that is a directory removes it and
everything under it.

## Compilation

| Method | Path | Request body | Response |
|---|---|---|---|
| POST | `/api/projects/{id}/compile` | `CompileRequest`: `main_file` (optional override) | `CompileResult`: `success`, `log`, `pdf_base64` (null on failure), `duration_ms` |

A LaTeX compile error, a missing `latexmk` binary, or a compile timeout
all produce `success: false` with a populated `log`, as an ordinary
`200 OK` response, not an HTTP error. The frontend is expected to show
`log` to the user either way. This endpoint raises an HTTP error only
for a `404` (unknown project id) or a `400` (an explicit `main_file`
override that is not a plain, in-project `.tex` filename — rejected
before it ever reaches `latexmk`'s command line, since an unvalidated
value there would be an argument-injection risk).

## History

| Method | Path | Request body | Response |
|---|---|---|---|
| GET | `/api/projects/{id}/history` | none | list of `CommitInfo`: `id`, `message`, `author`, `timestamp`, `changed_files` |
| POST | `/api/projects/{id}/history/commit` | `CommitCreate`: `message`, `author` (defaults to `"anonymous"`) | `CommitInfo` |
| GET | `/api/projects/{id}/history/{commit_id}/diff` | none | list of `DiffEntry`: `file_path`, `status` (`"added"`/`"modified"`/`"deleted"`), `patch` |
| POST | `/api/projects/{id}/history/{commit_id}/revert` | none | `CommitInfo` (the new commit created by the revert) |

Committing with no working-tree changes, and reverting to a commit that
would produce no change, are both rejected rather than producing an
empty history entry. See [history.md](history.md) for the user-facing
behavior and the "revert creates a new commit" guarantee.

## Packages

| Method | Path | Request body | Response |
|---|---|---|---|
| GET | `/api/packages/catalog` | none | list of `CatalogEntry`: `id`, `name`, `description`, `kind` (`"package"`/`"template"`), `tags`, `files` |
| GET | `/api/packages/installed` | none | list of `InstalledPackage`: `id`, `name`, `kind`, `installed_at`, `files` |
| POST | `/api/packages/install` | `PackageInstallRequest`: `catalog_id` | `InstalledPackage` |
| POST | `/api/packages/add-local` | `PackageAddLocalRequest`: `name`, `filename`, `content` | `InstalledPackage` |
| DELETE | `/api/packages/{package_id}` | none | `{"success": true}` |

`/api/packages/catalog` only ever lists entries from the bundled,
read-only catalog; `/api/packages/installed` lists what is actually
present under `plugins/` on this deployment. See
[package-manager.md](package-manager.md) for the package/template
distinction and the `plugins/<id>/...` layout these endpoints manage.

## Error-handling conventions

These conventions apply consistently across all of the above:

- A project id, file path, commit id, or package id that does not
  exist on the server produces `404 Not Found`.
- A request that fails the application's own validation, such as a file
  path attempting to escape its project directory, an empty commit
  message, or a commit/revert with no resulting changes, produces
  `400 Bad Request` with a `detail` string describing the problem.
- A request body that fails basic JSON schema validation (wrong field
  type, missing required field) is rejected by FastAPI itself before
  reaching application code, as `422 Unprocessable Entity`, per
  FastAPI's standard behavior.
- A failed LaTeX **compile** is deliberately not an HTTP error: it is a
  normal `200 OK` with `CompileResult.success` set to `false` and a
  populated `log`. Only a missing project id makes `/compile` return an
  HTTP error.
