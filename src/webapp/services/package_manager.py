"""LaTeX package/template catalog and shared-plugins install manager.

Design: ``config.PLUGINS_ROOT`` is a single directory shared across every
project in the workspace, not a per-project directory. Installing a
``kind="package"`` catalog entry writes its files once, under
``PLUGINS_ROOT/<catalog_id>/``, and ``latex_render.py`` already points
``TEXINPUTS`` at ``PLUGINS_ROOT`` for every compile, so every project
immediately sees the installed ``.sty``/``.cls`` without copying it into
that project's own git history. This mirrors how TeX Live's own package
manager (tlmgr) maintains one shared texmf tree rather than a private
copy per document.

``kind="template"`` catalog entries are a different concept entirely:
they are full starter ``.tex`` documents consumed directly by the
project-scaffolding router at project-creation time (which reads
``config.CATALOG_PATH`` itself), and are never written under
``PLUGINS_ROOT`` or tracked in the installed-packages manifest. The
``install_from_catalog``/``add_local_package``/``remove_package``
functions in this module therefore only ever operate on packages;
attempting to "install" a template through them is rejected with a
``ValueError``.

The manifest at ``config.PLUGINS_MANIFEST_PATH`` is the single source of
truth for what is currently installed under ``PLUGINS_ROOT``: a JSON
object keyed by package id, mapping to the metadata needed to rebuild an
``InstalledPackage``.
"""

from __future__ import annotations

import functools
import json
import re
import shutil
import time
import uuid
from pathlib import Path

from .. import config
from ..models import CatalogEntry, InstalledPackage

_ALLOWED_LOCAL_EXTENSIONS: tuple[str, ...] = (
    ".sty",
    ".cls",
    ".tex",
    ".bib",
    ".bst",
    ".clo",
    ".def",
    ".ldf",
)


@functools.lru_cache(maxsize=1)
def load_catalog() -> list[CatalogEntry]:
    """Load and validate the bundled catalog from ``config.CATALOG_PATH``.

    Cached after the first call since the catalog ships read-only with
    the app and never changes at runtime.

    Returns:
        list[CatalogEntry]: Every catalog entry, package and template.

    Raises:
        FileNotFoundError: If ``config.CATALOG_PATH`` does not exist.
        ValueError: If the file is not valid JSON or an entry does not
            match the ``CatalogEntry`` schema.
    """
    raw_text: str = config.CATALOG_PATH.read_text(encoding="utf-8")

    try:
        raw_entries: object = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise ValueError(f"malformed catalog JSON: {error}") from error

    assert isinstance(raw_entries, list), "catalog.json must be a JSON array"

    entries: list[CatalogEntry] = [
        CatalogEntry.model_validate(raw_entry) for raw_entry in raw_entries
    ]

    return entries


def search_catalog(
    query: str | None = None,
    kind: str | None = None,
) -> list[CatalogEntry]:
    """Search the catalog by case-insensitive substring and optional kind.

    Args:
        query (str | None): Substring matched against each entry's
            ``name``, ``description``, and ``tags``. ``None`` or an
            empty/whitespace-only string matches every entry.
        kind (str | None): When given, restrict results to this
            ``CatalogEntry.kind`` (``"package"`` or ``"template"``).

    Returns:
        list[CatalogEntry]: Matching entries, in catalog order.
    """
    entries: list[CatalogEntry] = load_catalog()

    if kind is not None:
        entries = [entry for entry in entries if entry.kind == kind]

    normalized_query: str = (query or "").strip().lower()
    if not normalized_query:
        return entries

    def _matches(entry: CatalogEntry) -> bool:
        haystack: list[str] = [entry.name, entry.description, *entry.tags]
        return any(normalized_query in field.lower() for field in haystack)

    return [entry for entry in entries if _matches(entry)]


def _load_manifest() -> dict[str, dict[str, object]]:
    """Read the installed-packages manifest, defaulting to empty.

    Returns:
        dict[str, dict[str, object]]: Manifest keyed by package id.
    """
    config.PLUGINS_ROOT.mkdir(parents=True, exist_ok=True)

    if not config.PLUGINS_MANIFEST_PATH.is_file():
        return {}

    raw_text: str = config.PLUGINS_MANIFEST_PATH.read_text(encoding="utf-8")
    if not raw_text.strip():
        return {}

    manifest: object = json.loads(raw_text)
    assert isinstance(manifest, dict), "manifest.json must be a JSON object"

    return manifest


def _save_manifest(data: dict[str, dict[str, object]]) -> None:
    """Persist the installed-packages manifest as JSON.

    Args:
        data (dict[str, dict[str, object]]): Full manifest to write,
            keyed by package id.
    """
    config.PLUGINS_ROOT.mkdir(parents=True, exist_ok=True)
    config.PLUGINS_MANIFEST_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _manifest_entry_to_installed(
    package_id: str,
    entry: dict[str, object],
) -> InstalledPackage:
    """Convert one manifest record into an ``InstalledPackage`` model."""
    return InstalledPackage(
        id=package_id,
        name=str(entry.get("name", package_id)),
        kind=str(entry.get("kind", "package")),
        installed_at=float(entry.get("installed_at", 0.0)),
        files=list(entry.get("files", [])),
    )


def list_installed() -> list[InstalledPackage]:
    """List every package currently installed under ``PLUGINS_ROOT``.

    Returns:
        list[InstalledPackage]: One entry per installed package, in
        manifest insertion order.
    """
    manifest: dict[str, dict[str, object]] = _load_manifest()

    return [
        _manifest_entry_to_installed(package_id, entry)
        for package_id, entry in manifest.items()
    ]


def _validate_relative_file_name(file_name: str) -> None:
    """Reject path-traversal or absolute paths in a package file name.

    Args:
        file_name (str): Candidate file name/relative path.

    Raises:
        ValueError: If ``file_name`` escapes its package directory.
    """
    if not file_name or file_name.startswith("/") or ".." in Path(
        file_name
    ).parts:
        raise ValueError(f"invalid file name: {file_name!r}")


def install_from_catalog(catalog_id: str) -> InstalledPackage:
    """Install a ``kind="package"`` catalog entry into ``PLUGINS_ROOT``.

    Re-running this with the same ``catalog_id`` overwrites the
    package's files and manifest entry in place, so repeated installs
    are idempotent rather than erroring.

    Args:
        catalog_id (str): ``CatalogEntry.id`` to install.

    Returns:
        InstalledPackage: The resulting installed-package record.

    Raises:
        LookupError: If no catalog entry has this id.
        ValueError: If the entry is a template, or any of its file
            names fail path-traversal validation.
    """
    assert catalog_id, "catalog_id must be a non-empty string"

    entries: list[CatalogEntry] = load_catalog()
    matching: list[CatalogEntry] = [
        entry for entry in entries if entry.id == catalog_id
    ]

    if not matching:
        raise LookupError(f"unknown catalog id: {catalog_id}")

    entry: CatalogEntry = matching[0]

    if entry.kind == "template":
        raise ValueError(
            "templates are applied at project-creation time, not "
            "installed as packages"
        )

    for package_file in entry.files:
        _validate_relative_file_name(package_file.name)

    config.PLUGINS_ROOT.mkdir(parents=True, exist_ok=True)
    package_dir: Path = config.PLUGINS_ROOT / entry.id
    package_dir.mkdir(parents=True, exist_ok=True)

    written_files: list[str] = []
    for package_file in entry.files:
        file_path: Path = package_dir / package_file.name
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(package_file.content, encoding="utf-8")
        written_files.append(package_file.name)

    installed_at: float = time.time()

    manifest: dict[str, dict[str, object]] = _load_manifest()
    manifest[entry.id] = {
        "name": entry.name,
        "kind": entry.kind,
        "installed_at": installed_at,
        "files": written_files,
    }
    _save_manifest(manifest)

    result: InstalledPackage = InstalledPackage(
        id=entry.id,
        name=entry.name,
        kind=entry.kind,
        installed_at=installed_at,
        files=written_files,
    )

    assert result.files, "installed package must have at least one file"

    return result


def _slugify(name: str) -> str:
    """Convert a display name into a kebab-case id slug.

    Args:
        name (str): Human-readable package name.

    Returns:
        str: Lowercase, hyphen-separated slug with no leading/trailing
        or repeated hyphens.
    """
    lowered: str = name.strip().lower()
    slug: str = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return slug or "package"


def add_local_package(
    name: str,
    filename: str,
    content: str,
) -> InstalledPackage:
    """Register a single user-uploaded file as an installed package.

    Args:
        name (str): Display name; slugified into the package id. If
            the resulting slug already exists in the manifest, a short
            random suffix is appended to keep ids unique rather than
            overwriting an unrelated package.
        filename (str): Target file name, validated against a path
            and extension allowlist.
        content (str): Raw text content of the file.

    Returns:
        InstalledPackage: The resulting installed-package record.

    Raises:
        ValueError: If ``filename`` contains path traversal or does
            not match the allowed extension list.
    """
    assert name.strip(), "name must be a non-empty string"
    assert filename.strip(), "filename must be a non-empty string"

    if filename.startswith("/") or ".." in Path(filename).parts:
        raise ValueError(f"invalid file name: {filename!r}")

    if not filename.lower().endswith(_ALLOWED_LOCAL_EXTENSIONS):
        allowed: str = ", ".join(_ALLOWED_LOCAL_EXTENSIONS)
        raise ValueError(
            f"filename {filename!r} must end with one of: {allowed}"
        )

    manifest: dict[str, dict[str, object]] = _load_manifest()

    base_slug: str = _slugify(name)
    package_id: str = base_slug
    while package_id in manifest:
        package_id = f"{base_slug}-{uuid.uuid4().hex[:6]}"

    config.PLUGINS_ROOT.mkdir(parents=True, exist_ok=True)
    package_dir: Path = config.PLUGINS_ROOT / package_id
    package_dir.mkdir(parents=True, exist_ok=True)

    file_path: Path = package_dir / filename
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")

    installed_at: float = time.time()
    manifest[package_id] = {
        "name": name,
        "kind": "package",
        "installed_at": installed_at,
        "files": [filename],
    }
    _save_manifest(manifest)

    return InstalledPackage(
        id=package_id,
        name=name,
        kind="package",
        installed_at=installed_at,
        files=[filename],
    )


def remove_package(package_id: str) -> None:
    """Remove an installed package's files and manifest entry.

    Args:
        package_id (str): Id of the installed package to remove.

    Raises:
        LookupError: If no installed package has this id.
    """
    assert package_id, "package_id must be a non-empty string"

    manifest: dict[str, dict[str, object]] = _load_manifest()

    if package_id not in manifest:
        raise LookupError(f"unknown installed package id: {package_id}")

    shutil.rmtree(config.PLUGINS_ROOT / package_id, ignore_errors=True)

    del manifest[package_id]
    _save_manifest(manifest)
