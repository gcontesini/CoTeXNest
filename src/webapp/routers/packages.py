"""HTTP endpoints for the LaTeX package/template catalog and manager."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..models import (
    CatalogEntry,
    InstalledPackage,
    PackageAddLocalRequest,
    PackageInstallRequest,
)
from ..services import package_manager

router: APIRouter = APIRouter(prefix="/api/packages", tags=["packages"])


@router.get("/catalog", response_model=list[CatalogEntry])
def get_catalog(
    query: str | None = None,
    kind: str | None = None,
) -> list[CatalogEntry]:
    """Search the bundled package/template catalog.

    Args:
        query (str | None): Optional case-insensitive substring match
            against name, description, and tags.
        kind (str | None): Optional filter, ``"package"`` or
            ``"template"``.

    Returns:
        list[CatalogEntry]: Matching catalog entries.
    """
    return package_manager.search_catalog(query=query, kind=kind)


@router.get("/installed", response_model=list[InstalledPackage])
def get_installed() -> list[InstalledPackage]:
    """List every package currently installed under ``PLUGINS_ROOT``.

    Returns:
        list[InstalledPackage]: Installed package records.
    """
    return package_manager.list_installed()


@router.post("/install", response_model=InstalledPackage)
def install_package(body: PackageInstallRequest) -> InstalledPackage:
    """Install a catalog package into the shared plugins directory.

    Args:
        body (PackageInstallRequest): Catalog id to install.

    Returns:
        InstalledPackage: The resulting installed-package record.

    Raises:
        HTTPException: 404 if the catalog id is unknown; 400 if the
            entry is a template or fails file-name validation.
    """
    try:
        return package_manager.install_from_catalog(body.catalog_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/add-local", response_model=InstalledPackage)
def add_local_package(body: PackageAddLocalRequest) -> InstalledPackage:
    """Register a user-uploaded file as an installed package.

    Args:
        body (PackageAddLocalRequest): Display name, filename, and raw
            text content of the uploaded file.

    Returns:
        InstalledPackage: The resulting installed-package record.

    Raises:
        HTTPException: 400 if the filename fails path/extension
            validation.
    """
    try:
        return package_manager.add_local_package(
            name=body.name,
            filename=body.filename,
            content=body.content,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.delete("/{package_id}")
def remove_package(package_id: str) -> dict[str, bool]:
    """Remove an installed package's files and manifest entry.

    Args:
        package_id (str): Id of the installed package to remove.

    Returns:
        dict[str, bool]: ``{"success": True}`` on success.

    Raises:
        HTTPException: 404 if the package id is unknown.
    """
    try:
        package_manager.remove_package(package_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

    return {"success": True}
