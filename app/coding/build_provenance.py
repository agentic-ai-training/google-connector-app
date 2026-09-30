"""Verify an operator-pinned builder's assertion about an immutable user release.

Signature verification is necessary but not sufficient build proof: the signer must
later be enrolled from a reviewed, keyless builder that observes the produced image
digests. Never accept a public key or a signature supplied as authority by a model.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
from typing import Literal
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BuildProvenanceError(ValueError):
    pass


class BuildStatement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    request_id: UUID
    user_id: str = Field(min_length=1, max_length=512)
    target_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    target_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    compose_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    service_images: dict[str, str]
    built_services: tuple[str, ...]
    external_services: tuple[str, ...]
    build_job_id: UUID
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: datetime
    expires_at: datetime

    @field_validator("issued_at", "expires_at")
    @classmethod
    def zoned(cls, value):
        if value.utcoffset() is None:
            raise ValueError("build attestation time must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def bounded_statement(self):
        images = self.service_images
        names = set(images)
        if (not 1 <= len(images) <= 12
                or any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", name)
                       or not isinstance(image, str) or len(image) > 320
                       for name, image in images.items())
                or len(self.built_services) != len(set(self.built_services))
                or len(self.external_services) != len(set(self.external_services))
                or set(self.built_services) & set(self.external_services)
                or set(self.built_services) | set(self.external_services) != names
                or not self.issued_at < self.expires_at <= self.issued_at + timedelta(hours=24)):
            raise ValueError("build statement identity or lifetime is invalid")
        return self


class SignedBuildAttestation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    statement: BuildStatement
    signature_hex: str = Field(pattern=r"^[0-9a-f]{128}$")


def canonical_statement(statement: BuildStatement) -> bytes:
    return json.dumps(statement.model_dump(mode="json"), sort_keys=True,
                      separators=(",", ":")).encode()


def verify_build_attestation(target, manifest, *, compose: bytes | None = None):
    """Fail closed before provider access and again inside the dispatch transaction."""
    attestation = manifest.build_attestation
    key_hex = target.release_verification_key_hex
    if attestation is None or key_hex is None:
        raise BuildProvenanceError("trusted build attestation or verifier is unavailable")
    statement = attestation.statement
    now = datetime.now(timezone.utc)
    if (statement.request_id != manifest.request_id
            or statement.user_id != target.owner_user_id
            or statement.target_id != manifest.target_id
            or statement.target_hash != manifest.target_hash
            or statement.source_hash != manifest.validated_source_hash
            or statement.release_hash != manifest.release_hash
            or statement.compose_hash != manifest.compose_hash
            or statement.issued_at > now + timedelta(minutes=5)
            or statement.expires_at <= now
            or statement.expires_at > manifest.planned_end_at):
        raise BuildProvenanceError("build attestation does not bind the approved release")
    services = set(statement.service_images)
    if manifest.release_profile == "react_spring_postgres":
        if (set(statement.built_services) != {"frontend", "backend"}
                or set(statement.external_services) != {"database"}):
            raise BuildProvenanceError("generated stack build provenance is incomplete")
    elif set(statement.built_services) != services or statement.external_services:
        raise BuildProvenanceError("single-service build provenance is incomplete")
    from app.coding.compose_policy import ComposePolicyError, release_identity

    try:
        identity = release_identity(statement.source_hash, statement.compose_hash,
                                    statement.service_images)
    except ComposePolicyError:
        raise BuildProvenanceError("build attestation image set is invalid") from None
    if identity != manifest.release_hash:
        raise BuildProvenanceError("build attestation image set is not bound to release")
    if compose is not None:
        from app.coding.compose_policy import parse_compose_document

        document = parse_compose_document(compose)
        actual = {name: service["image"] for name, service in document["services"].items()}
        if actual != statement.service_images:
            raise BuildProvenanceError("build attestation image digest differs from Compose")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_hex)).verify(
            bytes.fromhex(attestation.signature_hex), canonical_statement(statement),
        )
    except (InvalidSignature, ValueError):
        raise BuildProvenanceError("build attestation signature is invalid") from None
