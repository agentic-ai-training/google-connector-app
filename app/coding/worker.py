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
import stat
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
from app.coding.execution_fence import BorrowedConnection, CodingExecutionFenced, coding_commit, require_coding_authority
from app.coding.owned_process import OwnedProcessError, run_owned
from app.coding import publication_receipts
from app.coding.ci_trust import CITrustError, latest_policy, require_same_policy, verify_run


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
        from app.runs.workflow_ownership import postgres_claim_predicate
        ownership = await postgres_claim_predicate(conn)
        row = await conn.fetchrow(
            """SELECT * FROM coding_runs
               WHERE executor_version=$1 AND deleted_at IS NULL AND available_at<=now()
                 AND __WORKFLOW_OWNER__
                 AND (status IN ('queued','approved') OR
                      (status IN ('planning','executing','ci_running')
                       AND lease_expires_at<now()) OR
                      (status='published' AND NOT EXISTS (
                         SELECT 1 FROM candidate_builds b WHERE b.coding_run_id=coding_runs.id
                      )))
               ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1""".replace('__WORKFLOW_OWNER__', ownership),
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
                         AND status IN ('planning','executing','ci_running')""",
                    settings.coding_worker_lease_seconds, run_id, owner,
                )


async def _repository_snapshot(repository: str, ref: str, destination: Path, *, guard) -> str:
    base_url = f"https://api.github.com/repos/{repository}"
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        await guard()
        client.headers.update(await github_api_headers(client, repository))
        await guard()
        commit_response = await client.get(f"{base_url}/commits/{ref}")
        if commit_response.status_code in {403, 404}:
            raise CodingWorkerError(
                "repository_access", "GitHub App cannot access the requested repository/ref"
            )
        commit_response.raise_for_status()
        commit = commit_response.json()["sha"]
        await guard()
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


async def _run_local(arguments: list[str], *, coding_key: str | None = None, guard=None) -> dict:
    settings = get_settings()
    binary = Path(settings.coding_local_runner_binary).resolve()
    if not binary.is_file():
        raise CodingWorkerError("runtime_unavailable", "gca-local is not installed")
    if (not arguments or arguments[0] not in {'doctor','plan-request','execute-plan'}
            or (coding_key and arguments[0] != 'plan-request')
            or (arguments[0] == 'plan-request' and not coding_key)):
        raise CodingWorkerError('runtime_policy', 'The hosted CLI command/key pairing is not allowed')
    clean_home = tempfile.mkdtemp(prefix="hosted-coding-home-")
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": clean_home,
        "CI": "true",
        # Hosted generated tests must never fall back to execution on the core host.
        "GCA_REQUIRE_ISOLATED_VALIDATION": "1",
    }
    for setting, variable in (
        ("coding_validation_launcher", "GCA_VALIDATION_LAUNCHER"),
        ("coding_validation_config", "GCA_VALIDATION_CONFIG"),
    ):
        value = getattr(settings, setting, "")
        if value:
            environment[variable] = str(Path(value).resolve())
    if coding_key:
        environment["CODING_GROQ_API_KEY"] = coding_key
    try:
        code, output = await run_owned([str(binary), *arguments], cwd=clean_home,
            env=environment, guard=guard, output_limit=MAX_PLAN_BYTES+MAX_MANIFEST_BYTES)
    except OwnedProcessError as exc:
        raise CodingWorkerError('runtime_' + exc.category, 'Coding CLI exceeded its bounded process contract') from None
    finally:
        import shutil
        shutil.rmtree(clean_home, ignore_errors=True)
    try:
        payload = json.loads(output)
    except (TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise CodingWorkerError("runtime_protocol", "gca-local returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise CodingWorkerError('runtime_protocol', 'gca-local returned a non-object response')
    if code != 0 or payload.get("ok") is not True:
        message = str(payload.get("error") or "gca-local rejected the operation")[:2000]
        retryable = "Groq returned HTTP 429" in message or "request failed" in message.lower()
        raise CodingWorkerError("provider" if retryable else "runtime",
            'Coding CLI rejected the operation; private output was not exposed', retryable=retryable)
    return payload


def _bounded_private_file(value, root, limit):
    path = Path(value)
    try:
        relative = path.relative_to(root.absolute())
    except ValueError:
        try:
            relative = path.relative_to(root.resolve())
        except ValueError:
            raise CodingWorkerError('runtime_protocol', 'CLI evidence path is outside its private state directory') from None
    if not relative.parts or any(part in {'.','..'} for part in relative.parts):
        raise CodingWorkerError('runtime_protocol', 'CLI evidence path is invalid')
    directory = os.open(root.resolve(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(descriptor, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise CodingWorkerError('runtime_protocol', 'CLI evidence is not a bounded regular file')
        value = handle.read(limit + 1)
        if len(value) > limit:
            raise CodingWorkerError('runtime_protocol', 'CLI evidence exceeds its byte limit')
        return value


async def _cache_observation(conn, row, *, scope_key, source_version, payload):
    try:
        from app.coding.cache_policy import put_cache_entry
        # SQL-only cache population participates in the run lock/transaction.
        # A failed cache write rolls back only this savepoint, not business truth.
        async with conn.transaction():
            await put_cache_entry(BorrowedConnection(conn), user_id=row['user_id'], entity_type='validation_evidence',
                scope_key=scope_key, producer_version=row['tool_policy_version'], source_version=source_version, payload=payload)
    except Exception as exc:
        logger.warning('Coding observation cache skipped type=%s', type(exc).__name__)


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
    await require_coding_authority(pool, row)
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
        async def guard():
            await require_coding_authority(pool, row)
        commit = await _repository_snapshot(row["repository"], row["base_ref"], workspace, guard=guard)
        await guard()
        request_path = root / "request.txt"
        request_path.write_text(decrypted_request(row), encoding="utf-8")
        request_path.chmod(0o600)
        payload = await _run_local(
            [
                "plan-request", "--workspace", str(workspace),
                "--request-file", str(request_path), "--allow-cloud-source", "true",
                "--model", row["planner_model"], "--state-dir", str(state),
            ],
            coding_key=coding_key, guard=guard,
        )
        plan_path = Path(payload["plan_path"])
        plan_bytes = _bounded_private_file(plan_path, state, MAX_PLAN_BYTES)
        if len(plan_bytes) > MAX_PLAN_BYTES:
            raise CodingWorkerError("plan_limit", "Frozen plan exceeds one MiB")
        manifest_path = Path(payload["preview"]["approval_manifest"])
        manifest_bytes = _bounded_private_file(manifest_path, state, MAX_MANIFEST_BYTES)
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise CodingWorkerError("manifest_limit", "Approval manifest exceeds eight MiB")
        plan_hash = hashlib.sha256(plan_bytes).hexdigest()
        action_hash = str(payload["preview"]["approval_token"])
        if action_hash != plan_hash:
            raise CodingWorkerError("integrity", "Plan and approval hashes differ")
        plan = json.loads(plan_bytes)
        _validate_hosted_plan(plan)
        manifest = json.loads(manifest_bytes)
        async with coding_commit(pool, row) as conn:
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
        writes = action.get('tool') in {'apply_exact_patch','create_file'}
        parsed = PurePosixPath(path)
        if writes and (not path or parsed.is_absolute() or '..' in parsed.parts or '\\' in path or '\x00' in path
                or parsed.as_posix() != path):
            raise CodingWorkerError('plan_policy', 'Hosted write paths must be canonical relative paths')
        if writes and (
            path.startswith(".github/")
            or path.startswith('.improvement-proposals/')
            or path in {"Dockerfile", "Dockerfile.candidate"}
            or path.startswith("migrations/")
        ):
            raise CodingWorkerError(
                "plan_policy", f"Hosted self-service plan cannot modify {path}"
            )


async def _publish_draft(repository: str, base_ref: str, base_commit: str, run_id: str,
                         plan_hash: str, workspace: Path, paths: list[str],
                         candidate_build_id: str | None = None, *, guard) -> dict:
    base_url = f"https://api.github.com/repos/{repository}"
    branch = f"coding/{run_id}-{plan_hash[:16]}"
    async with httpx.AsyncClient(timeout=60) as client:
        await guard()
        client.headers.update(await github_api_headers(client, repository))
        await guard()
        commit_response = await client.get(f"{base_url}/git/commits/{base_commit}")
        commit_response.raise_for_status()
        base_tree = commit_response.json()["tree"]["sha"]
        await guard()
        tree_response = await client.get(
            f"{base_url}/git/trees/{base_tree}", params={"recursive": "1"}
        )
        tree_response.raise_for_status()
        modes = {item["path"]: item["mode"] for item in tree_response.json().get("tree", [])}
        entries = []
        for path in paths:
            content = (workspace / path).read_bytes()
            await guard()
            blob = await client.post(
                f"{base_url}/git/blobs",
                json={"content": base64.b64encode(content).decode(), "encoding": "base64"},
            )
            blob.raise_for_status()
            entries.append({
                "path": path, "mode": modes.get(path, "100644"),
                "type": "blob", "sha": blob.json()["sha"],
            })
        await guard()
        new_tree = await client.post(
            f"{base_url}/git/trees", json={"base_tree": base_tree, "tree": entries}
        )
        new_tree.raise_for_status()
        await guard()
        commit = await client.post(
            f"{base_url}/git/commits",
            json={
                "message": f"coding agent draft {run_id[:8]}",
                "tree": new_tree.json()["sha"], "parents": [base_commit],
            },
        )
        commit.raise_for_status()
        await guard()
        ref = await client.post(
            f"{base_url}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": commit.json()["sha"]},
        )
        if ref.status_code == 422:
            raise CodingWorkerError("publication_conflict", "Coding branch already exists")
        ref.raise_for_status()
        await guard()
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


def _publication_files(manifest):
    files = manifest.get('files')
    if not isinstance(files, dict) or not files:
        raise publication_receipts.PublicationReconciliationRequired()
    decoded = {}
    for path, encoded in files.items():
        parsed = PurePosixPath(path)
        if (not path or parsed.is_absolute() or '..' in parsed.parts or '\\' in path or '\x00' in path
                or parsed.as_posix() != path):
            raise publication_receipts.PublicationReconciliationRequired()
        decoded[path] = base64.b64decode(encoded, validate=True)
    return decoded


async def _verify_publication(row, manifest, *, guard, publication=None):
    """Read-only reconciliation of exact head, base, file delta and open draft PR.

    A missing ref/PR is uncertain, not permission to repeat a prior dispatch.
    No credentials, network responses or private file contents enter run events.
    """
    fail = publication_receipts.PublicationReconciliationRequired
    repository = row['repository']
    branch = f"coding/{row['id']}-{row['plan_hash'][:16]}"
    base_url = f'https://api.github.com/repos/{repository}'
    async with httpx.AsyncClient(timeout=60) as client:
        await guard()
        client.headers.update(await github_api_headers(client, repository))
        async def get(path, **kwargs):
            await guard()
            response = await client.get(base_url + path, **kwargs)
            if response.status_code != 200:
                raise fail()
            return response.json()
        ref = await get(f'/git/ref/heads/{branch}')
        commit = ref.get('object', {}).get('sha', '')
        if not re.fullmatch('[0-9a-f]{40}', commit):
            raise fail()
        if publication and (publication.get('branch') != branch or publication.get('commit') != commit):
            raise fail()
        head = await get(f'/git/commits/{commit}')
        if [p.get('sha') for p in head.get('parents', [])] != [row['base_commit']]:
            raise fail()
        base = await get(f"/git/commits/{row['base_commit']}")
        async def tree(sha):
            result = await get(f'/git/trees/{sha}', params={'recursive': '1'})
            if result.get('truncated') is not False or not isinstance(result.get('tree'), list):
                raise fail()
            return {item['path']: (item['mode'], item['type'], item['sha']) for item in result['tree']
                    if item['type'] != 'tree'}
        old = await tree(base['tree']['sha'])
        actual = await tree(head['tree']['sha'])
        expected = dict(old)
        for path, content in _publication_files(manifest).items():
            # Git object identity, not a security digest or content authentication.
            sha = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content,
                usedforsecurity=False).hexdigest()
            mode = old.get(path, ('100644',))[0]
            if mode not in {'100644', '100755'}:
                raise fail()
            expected[path] = (mode, 'blob', sha)
        if actual != expected:
            raise fail()
        pulls = await get('/pulls', params={'state': 'open', 'head': f"{repository.split('/')[0]}:{branch}",
            'base': row['base_ref'], 'per_page': '100'})
        if not isinstance(pulls, list) or len(pulls) != 1:
            raise fail()
        pull = await get(f"/pulls/{pulls[0]['number']}")
        number = pull.get('number')
        url = f'https://github.com/{repository}/pull/{number}'
        if (type(number) is not int or number <= 0 or pull.get('state') != 'open' or pull.get('draft') is not True
                or pull.get('head', {}).get('sha') != commit or pull.get('head', {}).get('ref') != branch
                or pull.get('head', {}).get('repo', {}).get('full_name') != repository
                or pull.get('base', {}).get('ref') != row['base_ref']
                or pull.get('base', {}).get('repo', {}).get('full_name') != repository or pull.get('html_url') != url
                or (publication and (publication.get('number') != number or publication.get('url') != url))):
            raise fail()
    return {'branch': branch, 'commit': commit, 'number': number, 'url': url}


async def _publish_frozen_unchecked(pool, row, manifest, *, guard, publication=None, dispatched=False):
    if publication is None and not dispatched:
        # A SQL-only write-ahead transition must commit before any GitHub mutation.
        await publication_receipts.dispatch(pool, row)
        with tempfile.TemporaryDirectory(prefix='coding-publication-') as temporary:
            workspace = Path(temporary)
            for path, content in _publication_files(manifest).items():
                destination = workspace / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            publication = await _publish_draft(row['repository'], row['base_ref'], row['base_commit'], str(row['id']),
                row['plan_hash'], workspace, list(manifest['files']), manifest.get('linked_build_id'), guard=guard)
        # This late receipt may survive lease loss/cancellation, not tenant erasure.
        await publication_receipts.returned(pool, row, publication)
    verified = await _verify_publication(row, manifest, guard=guard, publication=publication)
    if dispatched and publication is None:
        await publication_receipts.returned(pool, row, verified)
    return verified


async def _publish_frozen(pool, row, manifest, *, guard, publication=None, dispatched=False):
    try:
        return await _publish_frozen_unchecked(pool, row, manifest, guard=guard,
            publication=publication, dispatched=dispatched)
    except CodingExecutionFenced:
        raise  # Preserve the winning owner/cancellation; receipt remains for later reconciliation.
    except Exception:
        try:
            saved = await publication_receipts.load(pool, row)
        except CodingExecutionFenced:
            raise
        except Exception:
            raise publication_receipts.PublicationReconciliationRequired() from None
        if saved is not None and saved[0] == 'prepared':
            # The dispatch transition did not commit, so this protocol could not
            # have called a mutating RPC. Retry the same frozen intent, not the CLI.
            raise CodingWorkerError('publication_pre_dispatch',
                'Publication was not dispatched; validated frozen files are retained for a bounded retry', retryable=True) from None
        raise publication_receipts.PublicationReconciliationRequired() from None


async def _prepare_publication(pool, row, plan_bytes, *, guard, workspace):
    saved = await publication_receipts.load(pool, row)
    if saved is not None:
        state, manifest, result = saved
        publication = await _publish_frozen(pool, row, manifest, guard=guard, publication=result,
            dispatched=state == 'dispatched')
        return manifest, publication
    commit = await _repository_snapshot(row['repository'], row['base_commit'], workspace, guard=guard)
    await guard()
    if commit != row['base_commit']:
        raise CodingWorkerError('integrity', 'Base commit changed during execution')
    plan_path = workspace.parent / 'plan.json'
    plan_path.write_bytes(plan_bytes)
    plan_path.chmod(0o600)
    payload = await _run_local(['execute-plan', '--workspace', str(workspace), '--plan', str(plan_path),
        '--approve', row['approval_action_hash']], guard=guard)
    changed_paths = [item['path'] for item in payload.get('changes', []) if item.get('changed') is True]
    if not changed_paths or len(set(changed_paths)) != len(changed_paths):
        raise CodingWorkerError('postcondition', 'Approved plan produced no unique changed files')
    plan = json.loads(plan_bytes)
    approved_paths = {action['path'] for action in plan['actions']
        if action.get('tool') in {'apply_exact_patch', 'create_file'}}
    if not set(changed_paths) <= approved_paths:
        raise CodingWorkerError('postcondition', 'Changed files exceed the approved mutation paths')
    async with pool.acquire() as conn:
        linked = await conn.fetchval('SELECT id FROM candidate_builds WHERE coding_run_id=$1', row['id'])
    files = {path: base64.b64encode(_bounded_private_file(str(workspace / path), workspace,
        limit=MAX_MANIFEST_BYTES)).decode() for path in changed_paths}
    if linked:
        # Governed candidate_build_files stores text. Reject unsupported binary
        # candidates before any publication rather than failing bookkeeping later.
        for content in files.values():
            try:
                base64.b64decode(content, validate=True).decode('utf-8')
            except UnicodeError:
                raise CodingWorkerError('postcondition', 'Governed candidate files must be valid UTF-8 text') from None
        path = f'.improvement-proposals/{linked}.candidate.json'
        value = {'version': 1, 'build_id': str(linked), 'coding_run_id': str(row['id']),
            'base_commit': row['base_commit'], 'plan_hash': row['plan_hash'], 'files': changed_paths}
        files[path] = base64.b64encode((json.dumps(value, indent=2, sort_keys=True) + '\n').encode()).decode()
    manifest = {'version': 1, 'files': files, 'changed_paths': changed_paths,
        'linked_build_id': str(linked) if linked else None, 'validation': 'passed'}
    _publication_files(manifest)
    await publication_receipts.prepare(pool, row, manifest)
    publication = await _publish_frozen(pool, row, manifest, guard=guard)
    return manifest, publication


async def _execute(pool, row) -> None:
    await require_coding_authority(pool, row)
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
        async def guard():
            await require_coding_authority(pool, row)
        started = time.monotonic()
        manifest, publication = await _prepare_publication(pool, row, plan_bytes, guard=guard, workspace=workspace)
        changed_paths = manifest['changed_paths']
        linked_build_id = manifest['linked_build_id']
        frozen_files = _publication_files(manifest)
        async with coding_commit(pool, row) as conn:
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
                    content = frozen_files[path].decode('utf-8')
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
            await _cache_observation(
                conn, row,
                scope_key=f"{row['repository']}:{publication['commit']}",
                source_version=row["plan_hash"],
                payload={
                    "candidate_commit": publication["commit"],
                    "changed_paths": changed_paths,
                    "sandbox_validation": "passed",
                },
            )


async def _monitor_ci(pool, row) -> None:
    await require_coding_authority(pool, row)
    settings = get_settings()
    async with pool.acquire() as conn:
        started_at = await conn.fetchval("SELECT min(created_at) FROM coding_artifacts WHERE run_id=$1 AND artifact_type='pull_request'", row['id'])
    if started_at is None:
        raise CITrustError('recorded_publication_start')
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
    repository = row['repository']
    async with pool.acquire() as conn:
        policy = await latest_policy(conn, repository)
    saved = await publication_receipts.load(pool, row)
    if saved is None or saved[0] != 'returned':
        raise CITrustError('verified_publication_receipt')
    files = _publication_files(saved[1])
    async def guard():
        await require_coding_authority(pool, row)
    evidence = await verify_run(policy, commit=commit, pull_number=row['pull_request_number'],
        base_ref=row['base_ref'], branch=row['branch_name'],
        file_hashes={path: hashlib.sha256(content).hexdigest() for path, content in files.items()}, guard=guard)
    if evidence['status'] == 'failed':
        raise CodingWorkerError('ci_validation', 'Required trusted CI jobs or steps did not succeed')
    if evidence['status'] == 'pending':
        async with coding_commit(pool, row) as conn:
            await require_same_policy(conn, policy)
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
                payload={'policy_id': str(policy['id'])},
            )
        return
    check_url = evidence['url']
    raw = json.dumps(evidence, sort_keys=True).encode()
    async with coding_commit(pool, row) as conn:
        await require_same_policy(conn, policy)
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
            payload={'check_url': check_url, 'policy_id': str(policy['id']), 'jobs': len(evidence['jobs'])},
        )
        await _cache_observation(
            conn, row,
            scope_key=f"{repository}:{commit}:trusted-ci",
            source_version=commit,
            payload=evidence,
        )


async def process_one_coding_run(pool) -> bool:
    row = await _claim(pool)
    if not row:
        return False
    from app.runs.workflow_ownership import require_postgres_lease
    await require_postgres_lease(pool, 'coding', row)
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
    except CodingExecutionFenced as exc:
        # Another owner/cancellation/source change won. Never overwrite its
        # status or emit a contradictory candidate success/failure event.
        logger.info('Coding phase stopped after a live-authority fence')
        if exc.reason == 'current_plan_approval_required':
            try:
                async with coding_commit(pool, row, approval_required=False) as conn:
                    await conn.execute('''UPDATE coding_runs SET status='blocked',current_phase='approval_required',
                      approval_status=CASE WHEN approval_expires_at<=clock_timestamp() THEN 'expired' ELSE approval_status END,
                      error_category='approval_required',error_message='Current frozen-plan approval is required; reconcile any prior effects',
                      lease_owner=NULL,lease_expires_at=NULL,updated_at=now() WHERE id=$1''', row['id'])
                    await append_coding_event(conn,run_id=str(row['id']),user_id=row['user_id'],
                        event_type='coding_approval_expired',phase='approval_required',
                        message='Further coding actions are blocked; earlier effects may require reconciliation')
            except CodingExecutionFenced:
                pass  # Cancellation/replacement won before the metadata commit.
    except CITrustError as exc:
        async with coding_commit(pool, row, approval_required=False) as conn:
            retry = row['status'] == 'ci_running' and exc.category in {'available_github_evidence', 'available_complete_github_evidence'}
            await conn.execute('''UPDATE coding_runs SET status=$1,current_phase='ci_evidence_required',
              error_category='ci_trust',error_message=$2,lease_owner=NULL,lease_expires_at=NULL,
              available_at=now()+interval '60 seconds',updated_at=now() WHERE id=$3''',
              'ci_running' if retry else 'blocked', str(exc), row['id'])
            await append_coding_event(conn, run_id=str(row['id']), user_id=row['user_id'],
                event_type='coding_ci_evidence_required', phase='ci_evidence_required', message=str(exc),
                payload={'read_only_retry': retry})
    except publication_receipts.PublicationReconciliationRequired:
        async with coding_commit(pool, row, approval_required=False) as conn:
            await conn.execute('''UPDATE coding_runs SET status='reconciling',current_phase='publication_reconciliation',
              error_category='publication_reconciliation',error_message=$1,lease_owner=NULL,lease_expires_at=NULL,
              updated_at=now() WHERE id=$2''', str(publication_receipts.PublicationReconciliationRequired()), row['id'])
            await append_coding_event(conn, run_id=str(row['id']), user_id=row['user_id'],
                event_type='coding_publication_reconciliation_required', phase='publication_reconciliation',
                message='Read-only publication reconciliation is required; no automatic repeated writes or success claim')
    except CodingWorkerError as exc:
        retry = exc.retryable and row["attempt_count"] < row["max_attempts"]
        status = (
            "approved" if retry and row["status"] == "executing"
            else "queued" if retry else "failed"
        )
        async with coding_commit(pool, row, approval_required=False) as conn:
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
        async with coding_commit(pool, row, approval_required=False) as conn:
            has_publication = row['status'] == 'executing' and await conn.fetchval(
                'SELECT EXISTS(SELECT 1 FROM coding_publication_receipts WHERE run_id=$1)', row['id'])
            if has_publication:
                await conn.execute('''UPDATE coding_runs SET status='reconciling',current_phase='publication_reconciliation',
                  error_category='publication_reconciliation',error_message='Retained publication evidence requires read-only reconciliation',
                  lease_owner=NULL,lease_expires_at=NULL,updated_at=now() WHERE id=$1''', row['id'])
                await append_coding_event(conn, run_id=str(row['id']), user_id=row['user_id'],
                    event_type='coding_publication_reconciliation_required', phase='publication_reconciliation',
                    message='Local publication bookkeeping failed; retained evidence must be reconciled before any further writes')
                return True
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
