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
    remote_url: str | None = Field(
        default=None,
        description=(
            "Any git-clonable URL (HTTPS or SSH). When given, this "
            "clone becomes the project's initial content and git "
            "history, and its origin remote -- instead of starting "
            "from template_id or a blank document. Mutually exclusive "
            "with template_id."
        ),
    )


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


class ProjectSyncRequest(BaseModel):
    """Request for the "Sync Changes" action: commit, pull --rebase, push.

    ``commit_message`` is optional on the first call of a sync attempt
    -- if there are uncommitted changes and none is given, the response
    comes back with ``status="needs_commit_message"`` instead of
    failing, so the caller can supply one (AI-generated or
    user-typed) and call again.
    """

    commit_message: str | None = None
    author: str = Field(default="anonymous", max_length=120)


class ProjectSyncResult(BaseModel):
    status: str  # "synced" | "needs_commit_message"
    committed: bool = False
    commit_message: str | None = None
    pulled_commits: int = 0
    pushed: bool = False
    changed_files: list[str] = Field(default_factory=list)


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


# ─── AI assistant ───


class AiModelOption(BaseModel):
    id: str
    label: str


class ModelPullRequest(BaseModel):
    model: str = Field(min_length=1)


class ModelPullStatus(BaseModel):
    """Progress of an Ollama model pull, polled by the frontend.

    ``status`` mirrors whatever short phrase Ollama itself reports for
    the current pull stage (e.g. "pulling manifest", "downloading",
    "verifying sha256 digest"); ``completed_bytes``/``total_bytes`` are
    only populated while a layer is actively downloading.
    """

    model: str
    status: str = "idle"
    completed_bytes: int | None = None
    total_bytes: int | None = None
    done: bool = False
    error: str | None = None


class ExternalProviderProfile(BaseModel):
    """One configured external-API fallback, tried in order on a
    rate-limit/quota error from whichever provider answered last.
    """

    id: str
    label: str
    provider_kind: str  # "anthropic" | "openai"
    base_url: str | None = None
    model: str
    has_api_key: bool = False


class ExternalProviderProfileCreate(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    provider_kind: str = "openai"
    base_url: str | None = None
    model: str = Field(min_length=1)
    api_key: str = Field(min_length=1)


class AssistantProposal(BaseModel):
    """One proposed file change the assistant wants to make.

    Never applied automatically -- the frontend shows it to the user
    (as a diff/description) and only ``POST .../assistant/apply``
    actually touches disk, which also commits the result to the
    project's git history so it's always recoverable.
    """

    id: str
    action: str  # "write_file" | "create_entry" | "delete_entry" | "rename_entry"
    path: str
    new_path: str | None = None
    entry_type: str | None = None  # "file" | "dir", for create_entry only
    content: str | None = None  # full new content, for write_file/create_entry
    description: str


class AssistantProviderConfig(BaseModel):
    provider: str = "local"  # "local" | "external"
    local_model: str | None = None
    external_provider_kind: str = "anthropic"  # "anthropic" | "openai"
    external_base_url: str | None = None
    external_model: str | None = None
    has_external_api_key: bool = False
    writing_style_profile: str | None = None
    fallback_profiles: list[ExternalProviderProfile] = Field(default_factory=list)


class AssistantConfigUpdate(BaseModel):
    provider: str | None = None
    local_model: str | None = None
    external_provider_kind: str | None = None
    external_base_url: str | None = None
    external_model: str | None = None
    external_api_key: str | None = None
    clear_external_api_key: bool = False


class AssistantChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class AssistantChatRequest(BaseModel):
    messages: list[AssistantChatMessage] = Field(min_length=1)
    main_file: str | None = None


class AssistantChatResult(BaseModel):
    reply: str
    proposals: list[AssistantProposal] = Field(default_factory=list)
    provider_notice: str | None = None


class AssistantApplyRequest(BaseModel):
    proposal: AssistantProposal
    author: str = "AI assistant"


class AssistantApplyResult(BaseModel):
    success: bool
    commit_id: str


class WritingStyleSampleRequest(BaseModel):
    samples: list[str] = Field(min_length=1)


class WritingStyleResult(BaseModel):
    profile: str


class AiAssistRequest(BaseModel):
    log: str
    main_file: str


class AiAssistResult(BaseModel):
    suggestion: str
    proposal: AssistantProposal | None = None
    provider_notice: str | None = None


# ─── Background job polling (chat / fix-compile-error) ───
#
# A provider call -- local or external -- can legitimately take longer
# than any fixed timeout short enough to still be reasonable to block
# an HTTP request on (observed live: a CPU-only local model under load,
# and separately a slow external provider, each exceeded even a 180s
# synchronous ceiling). These two actions run on a background thread
# instead; the frontend polls for the result rather than waiting on
# the original request. See services/assistant.py's job runner.


class AssistantJobStarted(BaseModel):
    job_id: str


class AssistantChatJobStatus(BaseModel):
    status: str  # "running" | "done"
    result: AssistantChatResult | None = None


class AssistantFixJobStatus(BaseModel):
    status: str  # "running" | "done"
    result: AiAssistResult | None = None
