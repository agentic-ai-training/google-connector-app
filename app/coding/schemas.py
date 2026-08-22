"""Public schemas for the durable hosted coding-agent lifecycle."""

from pydantic import BaseModel, Field, field_validator


class CodingRunCreate(BaseModel):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_ref: str = Field(default="main", min_length=1, max_length=200)
    request: str = Field(min_length=1, max_length=12_000)
    idempotency_key: str = Field(min_length=8, max_length=200)
    source_egress_consent: bool

    @field_validator("base_ref")
    @classmethod
    def safe_ref(cls, value: str) -> str:
        if value.startswith("-") or ".." in value or any(
            character.isspace() for character in value
        ):
            raise ValueError("base_ref is unsafe")
        return value


class CodingRunApproval(BaseModel):
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: str = Field(pattern=r"^(approved|rejected)$")
    note: str = Field(default="", max_length=1000)


class CodingRunCancel(BaseModel):
    reason: str = Field(default="user_requested", min_length=1, max_length=500)
