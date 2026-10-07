"""Shared Pydantic request/response models used across all routers.

Keeping these in one module is what lets the editor/preview, history, and
package-manager modules be built independently without their contracts
drifting apart.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# ─── Projects & files ───


class ProjectSummary(BaseModel):
    id: str
    name: str
    created_at: float
    main_file: str = "main.tex"


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    template_id: str | None = None


class FileNode(BaseModel):
    path: str
    type: str  # "file" | "dir"
    children: list[FileNode] = Field(default_factory=list)


class FileContent(BaseModel):
    path: str
    content: str


class FileCreateRequest(BaseModel):
    path: str
    type: str = "file"  # "file" | "dir"


class FileRenameRequest(BaseModel):
    new_path: str


class FileUploadResult(BaseModel):
    path: str
    size_bytes: int
    content_type: str | None = None


class FileWrite(BaseModel):
    content: str


# ─── Compilation ───


class CompileRequest(BaseModel):
    main_file: str | None = None


class CompileResult(BaseModel):
    success: bool
    log: str
    pdf_base64: str | None = None
    duration_ms: int


# ─── History (git-backed) ───


class CommitInfo(BaseModel):
    id: str
    message: str
    author: str
    timestamp: float
    changed_files: list[str] = Field(default_factory=list)


class CommitCreate(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    author: str = Field(default="anonymous", max_length=120)


class DiffEntry(BaseModel):
    file_path: str
    status: str  # "added" | "modified" | "deleted"
    patch: str


# ─── LaTeX package manager ───


class PackageFile(BaseModel):
    name: str
    content: str


class CatalogEntry(BaseModel):
    id: str
    name: str
    description: str
    kind: str  # "package" | "template"
    tags: list[str] = Field(default_factory=list)
    files: list[PackageFile] = Field(default_factory=list)


class InstalledPackage(BaseModel):
    id: str
    name: str
    kind: str
    installed_at: float
    files: list[str] = Field(default_factory=list)


class PackageInstallRequest(BaseModel):
    catalog_id: str


class PackageAddLocalRequest(BaseModel):
    name: str
    filename: str
    content: str


# ─── AI assist ───


class AiModelOption(BaseModel):
    id: str
    label: str


class AiAssistRequest(BaseModel):
    model: str
    log: str
    main_file: str | None = None


class AiAssistResult(BaseModel):
    model: str
    suggestion: str
