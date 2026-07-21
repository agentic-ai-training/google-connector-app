from typing import Any, Literal

from pydantic import BaseModel, Field


class PlanStep(BaseModel):
    id: str
    title: str
    service: str | None = None
    operation: str = "execute"
    dependencies: list[str] = Field(default_factory=list)
    arguments: dict[str, Any] = Field(default_factory=dict)
    read_only: bool = True
    risk_level: Literal["low", "medium", "high"] = "low"
    requires_approval: bool = False
    weight: float = 1.0
    preconditions: list[str] = Field(default_factory=list)
    postconditions: list[str] = Field(default_factory=list)


class ExecutionPlan(BaseModel):
    objective: str
    intent_kind: Literal[
        "workspace_action", "workspace_guidance", "product_information",
        "scope_chat", "ambiguous", "out_of_scope",
    ] = "workspace_action"
    assumptions: list[str] = Field(default_factory=list)
    required_clarifications: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    rag_mode: Literal["none", "metadata", "keyword", "semantic", "hybrid"] = "none"
    steps: list[PlanStep]
    success_criteria: list[str] = Field(default_factory=list)
    estimated_max_tokens: int = 0


class RunCreate(BaseModel):
    message: str = Field(min_length=1, max_length=50_000)
    session_id: str = Field(min_length=1, max_length=200)
    idempotency_key: str | None = Field(default=None, max_length=200)


class RunDecision(BaseModel):
    approved: bool
    action_hash: str
    note: str | None = Field(default=None, max_length=2_000)


class RunResume(BaseModel):
    retry_failed_step: bool = True


class RunClarification(BaseModel):
    answers: dict[str, str]


class ArtifactCleanupRequest(BaseModel):
    action: Literal[
        "preserve", "delete", "cancel_event", "retry_population",
        "rollback_sharing",
    ]


class ArtifactCleanupDecision(BaseModel):
    approved: bool
    action_hash: str


class ImprovementDecision(BaseModel):
    decision: Literal["approved", "rejected", "changes_requested"]
    proposal_hash: str
    note: str | None = Field(default=None, max_length=4_000)


class CanaryActivationDecision(ImprovementDecision):
    traffic_percent: int = Field(default=5, ge=1, le=50)
    allowed_users: list[str] = Field(default_factory=list, max_length=200)
    denied_users: list[str] = Field(default_factory=list, max_length=200)


class ImprovementCandidateFile(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    change_type: Literal["create", "replace", "delete"]
    content: str | None = Field(default=None, max_length=500_000)


class ImprovementCandidateRegistration(BaseModel):
    candidate_kind: Literal["code", "okf", "config", "prompt"]
    base_version: str = Field(min_length=7, max_length=100)
    candidate_version: str = Field(min_length=7, max_length=100)
    exact_diff: str = Field(min_length=1, max_length=500_000)
    files: list[ImprovementCandidateFile] = Field(min_length=1, max_length=50)
    validation_report: dict[str, Any]
    rollback_plan: dict[str, Any]
    applicability: dict[str, list[str]]


class ImprovementDeploymentEvidence(BaseModel):
    candidate_version: str = Field(min_length=7, max_length=100)
    deployment_id: str = Field(min_length=1, max_length=300)
    deployment_url: str = Field(min_length=1, max_length=2_000)
    verified: bool
    smoke_tests: dict[str, Any]


class CandidateValidationAttestation(BaseModel):
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    tree_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    repository: str = Field(min_length=3, max_length=300)
    workflow: str = Field(min_length=1, max_length=300)
    run_id: str = Field(min_length=1, max_length=100)
    suite_version: str = Field(min_length=1, max_length=100)
    commands: list[str] = Field(min_length=1, max_length=50)
    results: dict[str, Any]
    file_hashes: dict[str, str]
    log_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    passed: bool


class CandidateDeploymentAttestation(BaseModel):
    candidate_version: str = Field(pattern=r"^[0-9a-f]{40}$")
    deployment_id: str = Field(min_length=1, max_length=300)
    service_name: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    workflow: str = Field(min_length=1, max_length=300)
    run_id: str = Field(min_length=1, max_length=100)
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    runtime_surfaces: list[Literal["api", "worker", "registry"]] = Field(
        min_length=1, max_length=3,
    )
    deployment_url: str | None = Field(default=None, max_length=2_000)
    smoke_tests: dict[str, Any]
    verified: bool


class ProductionDeploymentAttestation(BaseModel):
    production_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    project_id: str = Field(min_length=1, max_length=200)
    api_service: str = Field(min_length=1, max_length=200)
    api_deployment_id: str = Field(min_length=1, max_length=300)
    api_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    worker_service: str = Field(min_length=1, max_length=200)
    worker_deployment_id: str = Field(min_length=1, max_length=300)
    worker_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    workflow: str = Field(min_length=1, max_length=300)
    run_id: str = Field(min_length=1, max_length=100)
    smoke_tests: dict[str, Any]
    verified: bool


class CandidateBuildDraft(BaseModel):
    files: list[ImprovementCandidateFile] = Field(min_length=1, max_length=50)
    exact_diff: str = Field(min_length=1, max_length=500_000)
    rollback_plan: dict[str, Any]
    validation_commands: list[str] = Field(default_factory=list, max_length=50)
    roles_completed: list[str] = Field(min_length=1, max_length=5)
    models_used: list[str] = Field(default_factory=list, max_length=5)
    tokens_used: int = Field(ge=0)


class CandidateBuildFailure(BaseModel):
    stage: Literal["input", "generation", "submission"]
    error_type: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=2_000)
    retryable: bool
    retry_after_seconds: int | None = Field(default=None, ge=1, le=86_400)
