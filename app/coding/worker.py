"""Version-pinned durable worker for hosted coding runs.

The worker may give the Groq planner only typed repository observations. Validation and
mutation run later in a fresh, credential-free sandbox. Publication creates a draft PR;
this worker never merges or deploys code.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import re
import subprocess  # nosec B404
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

import httpx

from app.coding.repository import (
    append_coding_event,
    decrypted_request,
    encrypt_json,
    sanitized_excerpt,
)
from app.config.settings import get_settings
from app.db.oauth_credentials import decrypt_private_payload, encrypt_private_payload
from app.improvements.publisher import github_api_headers


MAX_ARCHIVE_BYTES = 134_217_728
MAX_ARCHIVE_FILES = 20_000
MAX_EXTRACTED_BYTES = 536_870_912
MAX_PLAN_BYTES = 1_048_576
MAX_MANIFEST_BYTES = 8_388_608
_SAFE_BRANCH = re.compile(r"[^A-Za-z0-9._-]+")
logger = logging.getLogger(__name__)


class CodingWorkerError(RuntimeError):
    def __init__(self, category: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.category = category
        self.retryable = retryable


async def _claim(pool):
    settings = get_settings()
    executor = settings.executor_version or settings.deployment_version
    owner = f"coding:{os.getpid()}:{time.time_ns()}"
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """SELECT * FROM coding_runs
               WHERE executor_version=$1 AND deleted_at IS NULL AND available_at<=now()
                 AND (status IN ('queued','approved') OR
                      (status IN ('planning','executing','ci_running')
                       AND lease_expires_at<now()) OR
                      (status='published' AND NOT EXISTS (
                         SELECT 1 FROM candidate_builds b WHERE b.coding_run_id=coding_runs.id
                      )))
               ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1""",
            executor,
        )
        if not row:
            return None
        prior = row["status"]
        phase = (
            "planning" if prior in {"queued", "planning"}
            else "executing" if prior in {"approved", "executing"}
            else "ci_running"
        )
        claimed = await conn.fetchrow(
            """UPDATE coding_runs SET status=$1,current_phase=$1,lease_owner=$2,
               lease_expires_at=now()+($3 * interval '1 second'),heartbeat_at=now(),
               started_at=COALESCE(started_at,now()),attempt_count=attempt_count+1,
               updated_at=now() WHERE id=$4 RETURNING *""",
            phase, owner, settings.coding_worker_lease_seconds, row["id"],
        )
        await append_coding_event(
            conn, run_id=str(row["id"]), user_id=row["user_id"],
            event_type="coding_worker_claimed", phase=phase,
            message="Version-pinned coding worker claimed this phase",
            payload={"executor_version": executor, "recovered_lease": prior == phase},
        )
    return claimed


async def _heartbeat(pool, run_id: str, owner: str, stop: asyncio.Event):
    settings = get_settings()
    interval = max(5, settings.coding_worker_lease_seconds // 3)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE coding_runs SET heartbeat_at=now(),
                       lease_expires_at=now()+($1 * interval '1 second')
                       WHERE id=$2 AND lease_owner=$3
                         AND status IN ('planning','executing')""",
                    settings.coding_worker_lease_seconds, run_id, owner,
                )


async def _repository_snapshot(repository: str, ref: str, destination: Path) -> str:
    base_url = f"https://api.github.com/repos/{repository}"
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        client.headers.update(await github_api_headers(client, repository))
        commit_response = await client.get(f"{base_url}/commits/{ref}")
        if commit_response.status_code in {403, 404}:
            raise CodingWorkerError(
                "repository_access", "GitHub App cannot access the requested repository/ref"
            )
        commit_response.raise_for_status()
        commit = commit_response.json()["sha"]
        archive_response = await client.get(f"{base_url}/zipball/{commit}")
        archive_response.raise_for_status()
    archive = archive_response.content
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise CodingWorkerError("repository_limit", "Repository archive exceeds 128 MiB")
    _safe_extract_zip(archive, destination)
    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise CodingWorkerError("repository_archive", "GitHub archive has an invalid root")
    for child in roots[0].iterdir():
        child.rename(destination / child.name)
    roots[0].rmdir()
    return commit


def _safe_extract_zip(archive: bytes, destination: Path) -> None:
    files = 0
    extracted = 0
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        for info in bundle.infolist():
            path = PurePosixPath(info.filename)
            if path.is_absolute() or ".." in path.parts:
                raise CodingWorkerError("repository_archive", "Archive path traversal denied")
            mode = info.external_attr >> 16
            if mode & 0o170000 == 0o120000:
                raise CodingWorkerError("repository_archive", "Archive symlink denied")
            if info.is_dir():
                continue
            files += 1
            extracted += info.file_size
            if files > MAX_ARCHIVE_FILES or extracted > MAX_EXTRACTED_BYTES:
                raise CodingWorkerError("repository_limit", "Repository exceeds sandbox bounds")
        bundle.extractall(destination)  # nosec B202 - every member was bounded above


def _run_local(arguments: list[str], *, coding_key: str | None = None) -> dict:
    settings = get_settings()
    binary = Path(settings.coding_local_runner_binary)
    if not binary.is_file():
        raise CodingWorkerError("runtime_unavailable", "gca-local is not installed")
    clean_home = tempfile.mkdtemp(prefix="hosted-coding-home-")
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": clean_home,
        "CI": "true",
    }
    if coding_key:
        environment["CODING_GROQ_API_KEY"] = coding_key
    try:
        completed = subprocess.run(  # nosec B603
            [str(binary), *arguments], capture_output=True, text=True,
            timeout=960, check=False, env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise CodingWorkerError("runtime_timeout", "Coding sandbox exceeded 16 minutes") from exc
    finally:
        import shutil
        shutil.rmtree(clean_home, ignore_errors=True)
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CodingWorkerError("runtime_protocol", "gca-local returned invalid JSON") from exc
    if completed.returncode != 0 or payload.get("ok") is not True:
        message = str(payload.get("error") or "gca-local rejected the operation")[:2000]
        retryable = "Groq returned HTTP 429" in message or "request failed" in message.lower()
        raise CodingWorkerError("provider" if retryable else "runtime", message, retryable=retryable)
    return payload


async def _record_step(
    conn, row, *, phase: str, status: str, sequence_no: int,
    tool_name: str | None = None, result: dict | None = None,
    started: float | None = None,
) -> None:
    summary = result or {}
    digest = hashlib.sha256(
        json.dumps(summary, sort_keys=True, default=str).encode()
    ).hexdigest()
    await conn.execute(
        """INSERT INTO coding_run_steps
           (run_id,sequence_no,phase,tool_name,status,result_summary,result_hash,
            duration_ms,started_at,completed_at)
           VALUES($1,$2,$3,$4,$5,$6::jsonb,$7,$8,now(),now())
           ON CONFLICT(run_id,sequence_no) DO UPDATE SET phase=EXCLUDED.phase,
             tool_name=EXCLUDED.tool_name,status=EXCLUDED.status,
             result_summary=EXCLUDED.result_summary,result_hash=EXCLUDED.result_hash,
             duration_ms=EXCLUDED.duration_ms,completed_at=now()""",
        row["id"], sequence_no, phase, tool_name, status,
        json.dumps(summary, default=str), digest,
        int((time.monotonic() - started) * 1000) if started else None,
    )


async def _mark_linked_build_failed(conn, row, category: str, message: str) -> None:
    proposal_id = await conn.fetchval(
        """UPDATE candidate_builds SET status='failed',completed_at=now(),
           error_message=$1,checkpoint=checkpoint||$2::jsonb,updated_at=now()
           WHERE coding_run_id=$3 AND status NOT IN ('validated','cancelled','failed')
           RETURNING proposal_id""",
        sanitized_excerpt(message, 1000),
        json.dumps({
            "runtime": "durable_coding_v1",
            "phase": "failed",
            "category": category,
        }), row["id"],
    )
    if not proposal_id:
        return
    await conn.execute(
        """UPDATE improvement_proposals
           SET candidate_state='diagnosis_only',
               candidate_manifest=candidate_manifest||$1::jsonb,updated_at=now()
           WHERE id=$2""",
        json.dumps({
            "coding_run_id": str(row["id"]),
            "failure_category": category,
            "canary_eligible": False,
        }), proposal_id,
    )
    payload = json.dumps({
        "coding_run_id": str(row["id"]),
        "category": category,
        "contains_private_evidence": False,
    })
    await conn.execute(
        """INSERT INTO improvement_notifications
           (proposal_id,channel,event_type,status,sanitized_payload)
           VALUES($1,'admin','coding_candidate_failed','sent',$2::jsonb),
                 ($1,'grafana','coding_candidate_failed','sent',$2::jsonb)
           ON CONFLICT(proposal_id,channel,event_type) DO UPDATE SET
             status='sent',sanitized_payload=excluded.sanitized_payload,created_at=now()""",
        proposal_id, payload,
    )


async def _plan(pool, row) -> None:
    settings = get_settings()
    coding_key = settings.coding_groq_api_key.strip()
    if not coding_key:
        raise CodingWorkerError("configuration", "CODING_GROQ_API_KEY is unavailable")
    with tempfile.TemporaryDirectory(prefix="hosted-coding-plan-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        state = root / "state"
        workspace.mkdir()
        state.mkdir()
        started = time.monotonic()
        commit = await _repository_snapshot(row["repository"], row["base_ref"], workspace)
        request_path = root / "request.txt"
        request_path.write_text(decrypted_request(row), encoding="utf-8")
        request_path.chmod(0o600)
        payload = await asyncio.to_thread(
            _run_local,
            [
                "plan-request", "--workspace", str(workspace),
                "--request-file", str(request_path), "--allow-cloud-source", "true",
                "--model", row["planner_model"], "--state-dir", str(state),
            ],
            coding_key=coding_key,
        )
        plan_path = Path(payload["plan_path"])
        plan_bytes = plan_path.read_bytes()
        if len(plan_bytes) > MAX_PLAN_BYTES:
            raise CodingWorkerError("plan_limit", "Frozen plan exceeds one MiB")
        manifest_path = Path(payload["preview"]["approval_manifest"])
        manifest_bytes = manifest_path.read_bytes()
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise CodingWorkerError("manifest_limit", "Approval manifest exceeds eight MiB")
        plan_hash = hashlib.sha256(plan_bytes).hexdigest()
        action_hash = str(payload["preview"]["approval_token"])
        if action_hash != plan_hash:
            raise CodingWorkerError("integrity", "Plan and approval hashes differ")
        plan = json.loads(plan_bytes)
        _validate_hosted_plan(plan)
        manifest = json.loads(manifest_bytes)
        async with pool.acquire() as conn, conn.transaction():
            await _record_step(
                conn, row, phase="planning", status="completed", sequence_no=0,
                tool_name="gca-local plan-request",
                result={
                    "base_commit": commit, "plan_hash": plan_hash,
                    "changes": manifest.get("changes", []),
                    "model": payload.get("model"),
                }, started=started,
            )
            await conn.execute(
                """UPDATE coding_runs SET status='awaiting_approval',
                   current_phase='awaiting_approval',base_commit=$1,plan_hash=$2,
                   encrypted_plan=$3,encrypted_approval_manifest=$4,
                   approval_status='pending',approval_action_hash=$2,
                   approval_requested_at=now(),
                   approval_expires_at=now()+($5 * interval '1 hour'),
                   input_tokens=$6,output_tokens=$7,lease_owner=NULL,
                   lease_expires_at=NULL,heartbeat_at=now(),updated_at=now()
                   WHERE id=$8 AND lease_owner=$9""",
                commit, plan_hash, encrypt_private_payload(plan_bytes), encrypt_json(manifest),
                settings.coding_approval_ttl_hours,
                int(payload.get("input_tokens") or 0),
                int(payload.get("output_tokens") or 0), row["id"], row["lease_owner"],
            )
            for artifact_type, artifact_payload in (
                ("plan", plan), ("approval_manifest", manifest),
            ):
                raw = json.dumps(artifact_payload, sort_keys=True).encode()
                await conn.execute(
                    """INSERT INTO coding_artifacts
                       (run_id,user_id,artifact_type,content_hash,encrypted_payload,
                        verification_status,verified_at)
                       VALUES($1,$2,$3,$4,$5,'verified',now())
                       ON CONFLICT DO NOTHING""",
                    row["id"], row["user_id"], artifact_type,
                    hashlib.sha256(raw).hexdigest(), encrypt_json(artifact_payload),
                )
            await append_coding_event(
                conn, run_id=str(row["id"]), user_id=row["user_id"],
                event_type="coding_approval_required", phase="awaiting_approval",
                message="A validated sandbox diff is ready for human review",
                payload={
                    "action_hash": action_hash,
                    "expires_in_hours": settings.coding_approval_ttl_hours,
                    "changed_files": len(manifest.get("changes", [])),
                },
            )


def _validate_hosted_plan(plan: dict) -> None:
    actions = plan.get("actions")
    if not isinstance(actions, list) or not actions:
        raise CodingWorkerError("plan_policy", "Hosted plan has no actions")
    for action in actions:
        if not isinstance(action, dict):
            raise CodingWorkerError("plan_policy", "Hosted plan action is invalid")
        path = str(action.get("path") or "")
        if action.get("tool") in {"apply_exact_patch", "create_file"} and (
            path.startswith(".github/")
            or path in {"Dockerfile", "Dockerfile.candidate"}
            or path.startswith("migrations/")
        ):
            raise CodingWorkerError(
                "plan_policy", f"Hosted self-service plan cannot modify {path}"
            )


async def _publish_draft(repository: str, base_ref: str, base_commit: str, run_id: str,
                         plan_hash: str, workspace: Path, paths: list[str],
                         candidate_build_id: str | None = None) -> dict:
    base_url = f"https://api.github.com/repos/{repository}"
    branch = f"coding/{run_id[:8]}-{plan_hash[:8]}"
    async with httpx.AsyncClient(timeout=60) as client:
        client.headers.update(await github_api_headers(client, repository))
        commit_response = await client.get(f"{base_url}/git/commits/{base_commit}")
        commit_response.raise_for_status()
        base_tree = commit_response.json()["tree"]["sha"]
        tree_response = await client.get(
            f"{base_url}/git/trees/{base_tree}", params={"recursive": "1"}
        )
        tree_response.raise_for_status()
        modes = {item["path"]: item["mode"] for item in tree_response.json().get("tree", [])}
        entries = []
        for path in paths:
            content = (workspace / path).read_bytes()
            blob = await client.post(
                f"{base_url}/git/blobs",
                json={"content": base64.b64encode(content).decode(), "encoding": "base64"},
            )
            blob.raise_for_status()
            entries.append({
                "path": path, "mode": modes.get(path, "100644"),
                "type": "blob", "sha": blob.json()["sha"],
            })
        new_tree = await client.post(
            f"{base_url}/git/trees", json={"base_tree": base_tree, "tree": entries}
        )
        new_tree.raise_for_status()
        commit = await client.post(
            f"{base_url}/git/commits",
            json={
                "message": f"coding agent draft {run_id[:8]}",
                "tree": new_tree.json()["sha"], "parents": [base_commit],
            },
        )
        commit.raise_for_status()
        ref = await client.post(
            f"{base_url}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": commit.json()["sha"]},
        )
        if ref.status_code == 422:
            raise CodingWorkerError("publication_conflict", "Coding branch already exists")
        ref.raise_for_status()
        pull = await client.post(
            f"{base_url}/pulls",
            json={
                "title": f"Coding agent draft: {run_id[:8]}",
                "head": branch, "base": base_ref, "draft": True,
                "body": (
                    f"Durable coding run `{run_id}`. Plan `{plan_hash}` was explicitly "
                    "approved and rerun in a fresh credential-free validation sandbox. "
                    "This draft is not automatically merged or deployed."
                    + (
                        f"\n\nCandidate build: `{candidate_build_id}`"
                        if candidate_build_id else ""
                    )
                ),
            },
        )
        pull.raise_for_status()
    return {
        "branch": branch, "commit": commit.json()["sha"],
        "number": pull.json()["number"], "url": pull.json()["html_url"],
    }


async def _execute(pool, row) -> None:
    if not row["encrypted_plan"] or not row["plan_hash"]:
        raise CodingWorkerError("integrity", "Approved run has no frozen plan")
    plan_bytes = decrypt_private_payload(row["encrypted_plan"])
    if hashlib.sha256(plan_bytes).hexdigest() != row["plan_hash"]:
        raise CodingWorkerError("integrity", "Frozen plan bytes do not match approval")
    plan = json.loads(plan_bytes)
    _validate_hosted_plan(plan)
    with tempfile.TemporaryDirectory(prefix="hosted-coding-execute-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        commit = await _repository_snapshot(row["repository"], row["base_commit"], workspace)
        if commit != row["base_commit"]:
            raise CodingWorkerError("integrity", "Base commit changed during execution")
        plan_path = root / "plan.json"
        plan_path.write_bytes(plan_bytes)
        plan_path.chmod(0o600)
        started = time.monotonic()
        payload = await asyncio.to_thread(
            _run_local,
            [
                "execute-plan", "--workspace", str(workspace),
                "--plan", str(plan_path), "--approve", row["approval_action_hash"],
            ],
        )
        changed_paths = [
            item["path"] for item in payload.get("changes", []) if item.get("changed") is True
        ]
        if not changed_paths:
            raise CodingWorkerError("postcondition", "Approved plan produced no changed files")
        async with pool.acquire() as conn:
            linked_build_id = await conn.fetchval(
                "SELECT id FROM candidate_builds WHERE coding_run_id=$1",
                row["id"],
            )
        publish_paths = list(changed_paths)
        if linked_build_id:
            manifest_directory = workspace / ".improvement-proposals"
            manifest_directory.mkdir(mode=0o700, exist_ok=True)
            manifest_relative = (
                f".improvement-proposals/{linked_build_id}.candidate.json"
            )
            manifest_payload = {
                "version": 1,
                "build_id": str(linked_build_id),
                "coding_run_id": str(row["id"]),
                "base_commit": row["base_commit"],
                "plan_hash": row["plan_hash"],
                "files": changed_paths,
            }
            (workspace / manifest_relative).write_text(
                json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            publish_paths.append(manifest_relative)
        publication = await _publish_draft(
            row["repository"], row["base_ref"], row["base_commit"], str(row["id"]),
            row["plan_hash"], workspace, publish_paths,
            str(linked_build_id) if linked_build_id else None,
        )
        async with pool.acquire() as conn, conn.transaction():
            await _record_step(
                conn, row, phase="executing", status="completed", sequence_no=1,
                tool_name="gca-local execute-plan",
                result={"changed_paths": changed_paths, "validation": "passed"},
                started=started,
            )
            await conn.execute(
                """UPDATE coding_runs SET status='published',current_phase='draft_pr',
                   publication_status='draft_pr',branch_name=$1,pull_request_number=$2,
                   pull_request_url=$3,candidate_commit=$4,
                   available_at=now()+interval '20 seconds',
                   lease_owner=NULL,lease_expires_at=NULL,
                   heartbeat_at=now(),updated_at=now() WHERE id=$5 AND lease_owner=$6""",
                publication["branch"], publication["number"], publication["url"],
                publication["commit"], row["id"], row["lease_owner"],
            )
            if linked_build_id:
                expected_hashes = {
                    str(action.get("path")): str(action.get("expected_sha256") or "")
                    for action in plan.get("actions", [])
                    if isinstance(action, dict)
                    and action.get("tool") in {"apply_exact_patch", "create_file"}
                }
                change_types = {
                    str(action.get("path")): (
                        "create" if action.get("tool") == "create_file" else "replace"
                    )
                    for action in plan.get("actions", [])
                    if isinstance(action, dict)
                    and action.get("tool") in {"apply_exact_patch", "create_file"}
                }
                await conn.execute(
                    "DELETE FROM candidate_build_files WHERE build_id=$1",
                    linked_build_id,
                )
                for path in changed_paths:
                    content = (workspace / path).read_text(encoding="utf-8")
                    await conn.execute(
                        """INSERT INTO candidate_build_files
                           (build_id,path,change_type,preimage_hash,result_hash,content)
                           VALUES($1,$2,$3,$4,$5,$6)""",
                        linked_build_id, path, change_types.get(path, "replace"),
                        expected_hashes.get(path) or None,
                        hashlib.sha256(content.encode()).hexdigest(), content,
                    )
                proposal_id = await conn.fetchval(
                    """UPDATE candidate_builds SET status='drafted',candidate_commit=$1,
                       checkpoint=checkpoint||$2::jsonb,updated_at=now()
                       WHERE id=$3 RETURNING proposal_id""",
                    publication["commit"], json.dumps({
                        "runtime": "durable_coding_v1",
                        "coding_run_id": str(row["id"]),
                        "draft_pr_url": publication["url"],
                        "phase": "awaiting_trusted_ci",
                    }), linked_build_id,
                )
                await conn.execute(
                    """UPDATE improvement_proposals
                       SET candidate_kind='code',candidate_state='implementation_draft',
                           candidate_version=$1,candidate_manifest=candidate_manifest||$2::jsonb,
                           updated_at=now() WHERE id=$3""",
                    publication["commit"], json.dumps({
                        "coding_run_id": str(row["id"]),
                        "candidate_build_id": str(linked_build_id),
                        "draft_pr_url": publication["url"],
                        "canary_eligible": False,
                    }), proposal_id,
                )
                await conn.execute(
                    """INSERT INTO improvement_notifications
                       (proposal_id,channel,event_type,status,sanitized_payload)
                       VALUES($1,'admin','coding_candidate_draft_ready','sent',$2::jsonb),
                             ($1,'grafana','coding_candidate_draft_ready','sent',$2::jsonb)
                       ON CONFLICT(proposal_id,channel,event_type) DO UPDATE SET
                         status='sent',sanitized_payload=excluded.sanitized_payload,
                         created_at=now()""",
                    proposal_id, json.dumps({
                        "coding_run_id": str(row["id"]),
                        "candidate_build_id": str(linked_build_id),
                        "pull_request_url": publication["url"],
                        "contains_private_evidence": False,
                    }),
                )
            raw_publication = json.dumps(publication, sort_keys=True).encode()
            await conn.execute(
                """INSERT INTO coding_artifacts
                   (run_id,user_id,artifact_type,content_hash,external_url,metadata,
                    verification_status,verified_at)
                   VALUES($1,$2,'pull_request',$3,$4,$5::jsonb,'verified',now())""",
                row["id"], row["user_id"], hashlib.sha256(raw_publication).hexdigest(),
                publication["url"], json.dumps(publication),
            )
            await append_coding_event(
                conn, run_id=str(row["id"]), user_id=row["user_id"],
                event_type="coding_draft_published", phase="draft_pr",
                message="Approved validated changes were published as a draft PR",
                payload={"url": publication["url"], "changed_files": len(changed_paths)},
            )
        try:
            from app.coding.cache_policy import put_cache_entry
            await put_cache_entry(
                pool, user_id=row["user_id"], entity_type="validation_evidence",
                scope_key=f"{row['repository']}:{publication['commit']}",
                producer_version=row["tool_policy_version"],
                source_version=row["plan_hash"],
                payload={
                    "candidate_commit": publication["commit"],
                    "changed_paths": changed_paths,
                    "sandbox_validation": "passed",
                },
            )
        except Exception as exc:
            # Cache population is optional and can never change execution truth.
            logger.warning("coding validation cache skipped type=%s", type(exc).__name__)


async def _monitor_ci(pool, row) -> None:
    settings = get_settings()
    started_at = row["started_at"]
    if (
        started_at
        and datetime.now(timezone.utc) - started_at
        > timedelta(
            minutes=max(5, settings.coding_ci_timeout_minutes)
        )
    ):
        raise CodingWorkerError(
            "ci_timeout", "Trusted CI did not finish within the configured deadline"
        )
    commit = str(row["candidate_commit"] or "")
    if not commit:
        raise CodingWorkerError("integrity", "Published coding run has no candidate commit")
    repository = row["repository"]
    async with httpx.AsyncClient(timeout=30) as client:
        client.headers.update(await github_api_headers(client, repository))
        response = await client.get(
            f"https://api.github.com/repos/{repository}/commits/{commit}/check-runs",
            params={"per_page": 100},
        )
        response.raise_for_status()
    checks = list(response.json().get("check_runs") or [])
    terminal_success = {"success", "neutral", "skipped"}
    failures = [
        check for check in checks
        if check.get("status") == "completed"
        and check.get("conclusion") not in terminal_success
    ]
    pending = [check for check in checks if check.get("status") != "completed"]
    if failures:
        names = ", ".join(str(check.get("name") or "check") for check in failures[:10])
        raise CodingWorkerError("ci_validation", f"Trusted CI failed: {names}")
    if not checks or pending:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """UPDATE coding_runs SET status='ci_running',current_phase='ci_running',
                   publication_status='ci_running',available_at=now()+interval '30 seconds',
                   lease_owner=NULL,lease_expires_at=NULL,heartbeat_at=now(),updated_at=now()
                   WHERE id=$1 AND lease_owner=$2""",
                row["id"], row["lease_owner"],
            )
            await append_coding_event(
                conn, run_id=str(row["id"]), user_id=row["user_id"],
                event_type="coding_ci_waiting", phase="ci_running",
                message="Waiting for trusted GitHub checks",
                payload={"checks": len(checks), "pending": len(pending)},
            )
        return
    check_url = next(
        (str(check.get("details_url")) for check in checks if check.get("details_url")),
        row["pull_request_url"],
    )
    evidence = {
        "commit": commit,
        "checks": [
            {"name": check.get("name"), "conclusion": check.get("conclusion")}
            for check in checks
        ],
    }
    raw = json.dumps(evidence, sort_keys=True).encode()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """UPDATE coding_runs SET status='completed',current_phase='ci_passed',
               publication_status='ci_passed',ci_check_url=$1,completed_at=now(),
               lease_owner=NULL,lease_expires_at=NULL,heartbeat_at=now(),updated_at=now()
               WHERE id=$2 AND lease_owner=$3""",
            check_url, row["id"], row["lease_owner"],
        )
        await conn.execute(
            """INSERT INTO coding_artifacts
               (run_id,user_id,artifact_type,content_hash,external_url,metadata,
                verification_status,verified_at)
               VALUES($1,$2,'ci_attestation',$3,$4,$5::jsonb,'verified',now())
               ON CONFLICT DO NOTHING""",
            row["id"], row["user_id"], hashlib.sha256(raw).hexdigest(),
            check_url, json.dumps(evidence),
        )
        await append_coding_event(
            conn, run_id=str(row["id"]), user_id=row["user_id"],
            event_type="coding_ci_passed", phase="ci_passed",
            message="All trusted GitHub checks passed",
            payload={"check_url": check_url, "checks": len(checks)},
        )
    try:
        from app.coding.cache_policy import put_cache_entry
        await put_cache_entry(
            pool, user_id=row["user_id"], entity_type="validation_evidence",
            scope_key=f"{repository}:{commit}:trusted-ci",
            producer_version=row["tool_policy_version"], source_version=commit,
            payload=evidence,
        )
    except Exception as exc:
        logger.warning("coding CI cache skipped type=%s", type(exc).__name__)


async def process_one_coding_run(pool) -> bool:
    row = await _claim(pool)
    if not row:
        return False
    stop = asyncio.Event()
    heartbeat = asyncio.create_task(
        _heartbeat(pool, str(row["id"]), row["lease_owner"], stop)
    )
    try:
        if row["status"] == "planning":
            await _plan(pool, row)
        elif row["status"] == "executing":
            await _execute(pool, row)
        else:
            await _monitor_ci(pool, row)
    except CodingWorkerError as exc:
        retry = exc.retryable and row["attempt_count"] < row["max_attempts"]
        status = (
            "approved" if retry and row["status"] == "executing"
            else "queued" if retry else "failed"
        )
        async with pool.acquire() as conn, conn.transaction():
            safe_message = sanitized_excerpt(str(exc), 2000)
            await conn.execute(
                """UPDATE coding_runs SET status=$1,current_phase=$2,error_category=$3,
                   error_message=$4,available_at=CASE WHEN $1='queued'
                     OR $1='approved' THEN now()+interval '5 minutes' ELSE available_at END,
                   completed_at=CASE WHEN $1='failed' THEN now() ELSE NULL END,
                   lease_owner=NULL,lease_expires_at=NULL,updated_at=now()
                   WHERE id=$5 AND lease_owner=$6""",
                status, "retry_wait" if retry else "failed", exc.category,
                safe_message, row["id"], row["lease_owner"],
            )
            await append_coding_event(
                conn, run_id=str(row["id"]), user_id=row["user_id"],
                event_type="coding_retry_scheduled" if retry else "coding_failed",
                phase="retry_wait" if retry else "failed",
                message=safe_message[:1000], payload={"category": exc.category},
            )
            if not retry:
                await _mark_linked_build_failed(
                    conn, row, exc.category, safe_message,
                )
    except Exception as exc:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """UPDATE coding_runs SET status='failed',current_phase='failed',
                   error_category='internal',error_message=$1,completed_at=now(),
                   lease_owner=NULL,lease_expires_at=NULL,updated_at=now()
                   WHERE id=$2 AND lease_owner=$3""",
                type(exc).__name__, row["id"], row["lease_owner"],
            )
            await append_coding_event(
                conn, run_id=str(row["id"]), user_id=row["user_id"],
                event_type="coding_failed", phase="failed",
                message="Coding worker encountered an internal bounded failure",
                payload={"category": "internal", "type": type(exc).__name__},
            )
            await _mark_linked_build_failed(
                conn, row, "internal", type(exc).__name__,
            )
    finally:
        stop.set()
        await heartbeat
    return True


async def coding_worker_loop(pool, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            worked = await process_one_coding_run(pool)
        except Exception as exc:
            # A single malformed record or transient database failure must not kill the
            # durable worker service. Individual claimed runs are finalized inside
            # process_one_coding_run; this is the outer availability boundary.
            worked = False
            logger.exception(
                "coding worker iteration failed type=%s", type(exc).__name__,
            )
        if worked:
            continue
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=get_settings().coding_worker_poll_seconds
            )
        except TimeoutError:
            pass
