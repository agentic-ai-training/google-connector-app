"""Stable, persisted control/candidate assignment for governed canaries."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutorAssignment:
    executor_version: str
    canary_id: str | None
    cohort: str
    reason: str
    okf_bundle_version: str | None = None


@dataclass(frozen=True)
class CandidateAPITarget:
    url: str
    candidate_version: str
    canary_id: str
    reason: str


@dataclass(frozen=True)
class CandidateFrontendTarget:
    url: str
    candidate_version: str
    canary_id: str
    reason: str


def stable_bucket(canary_id: str, user_id: str) -> int:
    digest = hashlib.sha256(f"{canary_id}:{user_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 100


def candidate_applies(manifest: dict, plan: dict | None) -> bool:
    """Require an explicit service/operation boundary before routing a candidate."""
    applicability = manifest.get("applicability") or {}
    if not applicability:
        return False
    plan_services = set((plan or {}).get("services") or [])
    plan_operations = {
        step.get("operation") for step in ((plan or {}).get("steps") or [])
        if step.get("operation")
    }
    required_services = set(applicability.get("services") or [])
    required_operations = set(applicability.get("operations") or [])
    required_rag_modes = set(applicability.get("rag_modes") or [])
    plan_rag_mode = (plan or {}).get("rag_mode", "none")
    return not (
        (required_services and not (required_services & plan_services))
        or (required_operations and not (required_operations & plan_operations))
        or (required_rag_modes and plan_rag_mode not in required_rag_modes)
    )


def _cohort_selected(row, user_id: str) -> tuple[bool, str]:
    allowed = set(row["allowed_users"] or [])
    denied = set(row["denied_users"] or [])
    if user_id in denied:
        return False, "explicit denylist"
    bucket = stable_bucket(str(row["id"]), user_id)
    selected = user_id in allowed or (
        not allowed and bucket < row["traffic_percent"]
    )
    return selected, (
        "explicit allowlist" if user_id in allowed else f"stable bucket {bucket}"
    )


async def resolve_candidate_api_target(
    conn, user_id: str, request_plan: dict,
) -> CandidateAPITarget | None:
    """Return a deployed API candidate only for its stable eligible cohort."""
    rows = await conn.fetch(
        """SELECT c.*,p.candidate_manifest,p.candidate_version,p.deployment_evidence
           FROM improvement_canaries c
           JOIN improvement_proposals p ON p.id=c.proposal_id
           WHERE c.status='active' AND c.routing_enabled=TRUE
             AND p.candidate_kind='code'
           ORDER BY c.started_at,c.id FOR UPDATE"""
    )
    for row in rows:
        manifest = row["candidate_manifest"] or {}
        if "api" not in set(manifest.get("runtime_surfaces") or []):
            continue
        if not candidate_applies(manifest, request_plan):
            continue
        selected, reason = _cohort_selected(row, user_id)
        if not selected:
            continue
        evidence = row["deployment_evidence"] or {}
        url = str(evidence.get("deployment_url") or "").rstrip("/")
        if (
            evidence.get("verified") is not True
            or evidence.get("candidate_version") != row["candidate_version"]
            or not url.startswith("https://")
        ):
            continue
        return CandidateAPITarget(
            url=url, candidate_version=row["candidate_version"],
            canary_id=str(row["id"]), reason=reason,
        )
    return None


async def resolve_candidate_frontend_target(
    conn, user_id: str,
) -> CandidateFrontendTarget | None:
    """Return the attested preview only for a stable active frontend cohort."""
    rows = await conn.fetch(
        """SELECT c.*,p.candidate_manifest,p.candidate_version,p.deployment_evidence
           FROM improvement_canaries c
           JOIN improvement_proposals p ON p.id=c.proposal_id
           WHERE c.status='active' AND c.routing_enabled=TRUE
             AND p.candidate_kind='code'
           ORDER BY c.started_at,c.id FOR UPDATE"""
    )
    for row in rows:
        manifest = row["candidate_manifest"] or {}
        if "frontend" not in set(manifest.get("runtime_surfaces") or []):
            continue
        selected, reason = _cohort_selected(row, user_id)
        if not selected:
            continue
        evidence = row["deployment_evidence"] or {}
        url = str(evidence.get("frontend_url") or "").rstrip("/")
        if (
            evidence.get("verified") is not True
            or evidence.get("candidate_version") != row["candidate_version"]
            or evidence.get("frontend_source_commit") != row["candidate_version"]
            or not url.startswith("https://")
        ):
            continue
        return CandidateFrontendTarget(
            url=url, candidate_version=row["candidate_version"],
            canary_id=str(row["id"]), reason=reason,
        )
    return None


async def resolve_run_candidate_api_target(
    conn, run_id: str, user_id: str,
) -> CandidateAPITarget | None:
    """Resolve the API that owns an already pinned candidate run.

    This deliberately does not require routing to remain enabled: an in-flight
    run stays pinned while new traffic can be rolled back to control.
    """
    row = await conn.fetchrow(
        """SELECT r.executor_version,r.canary_id,c.id,p.deployment_evidence,
                  p.candidate_manifest,p.candidate_version
           FROM agent_runs r
           JOIN improvement_canaries c ON c.id=r.canary_id
           JOIN improvement_proposals p ON p.id=c.proposal_id
           WHERE r.id=$1 AND r.user_id=$2 AND r.cohort_assignment='candidate'
             AND p.candidate_kind='code'""",
        run_id, user_id,
    )
    if not row:
        return None
    manifest = row["candidate_manifest"] or {}
    evidence = row["deployment_evidence"] or {}
    url = str(evidence.get("deployment_url") or "").rstrip("/")
    if (
        "api" not in set(manifest.get("runtime_surfaces") or [])
        or evidence.get("verified") is not True
        or row["executor_version"] != row["candidate_version"]
        or evidence.get("candidate_version") != row["candidate_version"]
        or not url.startswith("https://")
    ):
        return None
    return CandidateAPITarget(
        url=url, candidate_version=row["candidate_version"],
        canary_id=str(row["id"]), reason="run is pinned to candidate API",
    )


async def resolve_executor_assignment(
    conn, user_id: str, control_version: str, plan: dict | None = None,
) -> ExecutorAssignment:
    canaries = await conn.fetch(
        """SELECT c.*,p.candidate_kind,p.candidate_manifest
           FROM improvement_canaries c
           JOIN improvement_proposals p ON p.id=c.proposal_id
           WHERE c.status='active' AND c.routing_enabled=TRUE
           ORDER BY c.started_at,c.id FOR UPDATE"""
    )
    for row in canaries:
        manifest = row["candidate_manifest"] or {}
        if not candidate_applies(manifest, plan):
            continue
        selected, reason = _cohort_selected(row, user_id)
        if reason == "explicit denylist":
            continue
        cohort = "candidate" if selected else "control"
        version = (
            row["control_version"]
            if selected and row["candidate_kind"] in {"okf", "config", "prompt"}
            else (row["candidate_version"] if selected else row["control_version"])
        )
        return ExecutorAssignment(
            executor_version=version or control_version,
            canary_id=str(row["id"]),
            cohort=cohort,
            reason=reason,
            okf_bundle_version=(manifest.get("okf_bundle_hash") if selected else None),
        )
    current_okf = await conn.fetchval(
        """SELECT bundle_hash FROM okf_bundle_versions
           WHERE publication_status='trusted' ORDER BY approved_at DESC NULLS LAST LIMIT 1"""
    )
    return ExecutorAssignment(
        executor_version=control_version, canary_id=None, cohort="control",
        reason="no compatible active canary",
        okf_bundle_version=current_okf,
    )
