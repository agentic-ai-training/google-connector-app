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
