import os
import asyncio
import json
import uuid

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION_TESTS") != "1",
    reason="set RUN_INTEGRATION_TESTS=1 with PostgreSQL available",
)


def test_health_auth_and_route_protection(caplog):
    from app.api.main import app

    with TestClient(app) as client:
        health = client.get("/health", headers={"X-Request-ID": "integration-request-123"})
        assert health.json()["status"] == "ok"
        assert health.json()["executor_role"] in {"control", "candidate"}
        assert "deployment_version" in health.json()
        assert "executor_version" in health.json()
        assert health.headers["x-request-id"] == "integration-request-123"
        assert any(
            '"request_id":"integration-request-123"' in record.message
            for record in caplog.records
        )
        generated = client.get("/health", headers={"X-Request-ID": "unsafe value"})
        assert generated.headers["x-request-id"] != "unsafe value"
        assert len(generated.headers["x-request-id"]) == 32
        protected = client.get(
            "/runs/private-dynamic-id?token=must-not-appear-in-telemetry"
        )
        assert protected.status_code == 401
        request_logs = "\n".join(
            record.message for record in caplog.records
            if '"event":"http_request"' in record.message
        )
        assert '"path":"/runs/{run_id}"' in request_logs
        assert "private-dynamic-id" not in request_logs
        assert "must-not-appear-in-telemetry" not in request_logs
        preflight = client.options(
            "/chat",
            headers={
                "Origin": "https://google-agent-preview.vercel.app",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == (
            "https://google-agent-preview.vercel.app"
        )
        assert client.post(
            "/chat", json={"message": "hi", "session_id": "test"}
        ).status_code == 401
        token = client.post(
            "/auth/token", json={"email": "user@example.com"}
        ).json()["access_token"]
        assert client.get(
            "/admin/prompts", headers={"Authorization": f"Bearer {token}"}
        ).status_code == 403
        admin = client.post(
            "/auth/token", json={"email": "achintyat256@gmail.com"}
        ).json()["access_token"]
        response = client.get(
            "/admin/prompts", headers={"Authorization": f"Bearer {admin}"}
        )
        assert response.status_code == 200
        assert len(response.json()["prompts"]) >= 2
        flags = client.get(
            "/admin/feature-flags", headers={"Authorization": f"Bearer {admin}"}
        )
        assert flags.status_code == 200
        assert any(item["name"] == "live_rl" for item in flags.json()["feature_flags"])
        locked = client.put(
            "/admin/feature-flags/live_rl",
            headers={"Authorization": f"Bearer {admin}"},
            json={"enabled": True, "config": {}},
        )
        assert locked.status_code == 409


def test_embedding_queue_admission_is_bounded_per_user():
    from app.api.main import app
    from app.config.settings import get_settings
    from app.db.connection import get_pool
    from app.rag.jobs import enqueue_tool_result

    with TestClient(app) as client:
        settings = get_settings()
        original = settings.max_embedding_jobs_per_user

        async def attempt():
            settings.max_embedding_jobs_per_user = 0
            try:
                return await enqueue_tool_result(
                    "search_gmail", {"query": "bounded"},
                    {"messages": [{"id": "bounded", "body": "safe"}]},
                    await get_pool(), "bounded@example.com",
                )
            finally:
                settings.max_embedding_jobs_per_user = original

        assert client.portal.call(attempt) is False


def test_contextual_workflow_and_failure_inbox_are_durable():
    from app.api.main import app
    from app.db.connection import get_pool
    from app.improvements.failure_intelligence import record_failure_incident

    user_id = f"failure-routing-{uuid.uuid4()}@example.com"
    session_id = f"failure-routing-{uuid.uuid4()}"
    occurrence = f"integration:{uuid.uuid4()}"
    incident_id = run_id = None
    with TestClient(app) as client:
        token = client.post("/auth/token", json={"email": user_id}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        response = client.post("/runs", headers=headers, json={
            "session_id": session_id,
            "message": (
                "Create a sheet of the names of last 20 people who did mails to me, "
                "use its Drive link in Google Chat, and schedule a Calendar Meet invite "
                "tomorrow at 10 AM to fixture@example.com"
            ),
        })
        assert response.status_code == 202
        assert response.json()["status"] == "awaiting_clarification"
        run_id = response.json()["run_id"]
        run = client.get(f"/runs/{run_id}", headers=headers).json()
        assert [(item["service"], item["operation"]) for item in run["steps"]] == [
            ("gmail", "recent_senders"), ("sheets", "create_and_write"),
            ("chat", "send"), ("calendar", "create"),
        ]
        assert run["steps"][2]["dependencies"] == ["execute_sheets"]
        assert run["steps"][3]["dependencies"] == ["execute_sheets"]

        async def seed_incident():
            return await record_failure_incident(
                await get_pool(), occurrence_key=occurrence, run_id=uuid.UUID(run_id),
                session_id=session_id, user_id=user_id, message="private@example.com failed",
                intent_kind="workspace_action", stage="validation", category="planning",
                component="typed_planner", error="Invalid execution plan: unknown operation",
                breaking_point="Plan validation",
            )
        incident = client.portal.call(seed_incident)
        incident_id = str(incident["id"])
        admin = client.post(
            "/auth/token", json={"email": "achintyat256@gmail.com"}
        ).json()["access_token"]
        admin_headers = {"Authorization": f"Bearer {admin}"}
        inbox = client.get("/admin/failure-incidents", headers=admin_headers)
        assert inbox.status_code == 200
        match = next(item for item in inbox.json()["incidents"] if item["id"] == incident_id)
        assert len(match["improvement_options"]) == 2
        decision = client.post(
            f"/admin/failure-incidents/{incident_id}/decision", headers=admin_headers,
            json={"decision": "choose_A"},
        )
        assert decision.status_code == 200
        assert decision.json()["proposal"]["candidate_state"] == "diagnosis_only"

        async def cleanup():
            pool = await get_pool()
            async with pool.acquire() as conn, conn.transaction():
                proposal = await conn.fetchval(
                    "SELECT proposal_id FROM failure_incidents WHERE id=$1", uuid.UUID(incident_id)
                )
                if proposal:
                    await conn.execute("DELETE FROM improvement_proposals WHERE id=$1", proposal)
                await conn.execute("DELETE FROM failure_incidents WHERE id=$1", uuid.UUID(incident_id))
                await conn.execute("DELETE FROM agent_runs WHERE id=$1", uuid.UUID(run_id))
        client.portal.call(cleanup)


def test_private_okf_bundle_is_namespaced_and_excluded_by_default(tmp_path):
    from app.api.main import app
    from app.config.settings import get_settings
    from app.db.connection import get_pool
    from app.okf.loader import sync_bundle
    from app.okf.retriever import retrieve_operational_knowledge

    private_doc = tmp_path / "confidential.md"
    private_doc.write_text(
        """---
type: policy
title: Confidential fixture
owner: test-admin
version: 1
timestamp: 2026-07-20T00:00:00Z
visibility: private
publication_status: approved
approved_by: test-admin
approved_at: 2026-07-20T00:00:00Z
---
# Confidential fixture
ultraviolet-private-knowledge-marker
""",
        encoding="utf-8",
    )
    with TestClient(app) as client:
        settings = get_settings()
        original = settings.okf_private_bundle_path

        async def verify():
            settings.okf_private_bundle_path = str(tmp_path)
            try:
                pool = await get_pool()
                await sync_bundle(pool)
                public = await retrieve_operational_knowledge(
                    "ultraviolet private knowledge marker"
                )
                private = await retrieve_operational_knowledge(
                    "ultraviolet private knowledge marker", include_private=True
                )
                return public, private
            finally:
                settings.okf_private_bundle_path = original
                async with (await get_pool()).acquire() as conn:
                    await conn.execute(
                        "DELETE FROM okf_documents WHERE id='private/confidential.md'"
                    )

        public, private = client.portal.call(verify)
        assert public == []
        assert private[0]["id"] == "private/confidential.md"


def test_okf_candidate_is_staged_as_validated_immutable_overlay():
    from app.api.main import app
    from app.db.connection import get_pool
    from app.okf.candidates import stage_okf_candidate_bundle

    candidate = """---
type: policy
title: Candidate retry policy
owner: workspace-agent
version: 1
timestamp: 2026-07-21
visibility: public
publication_status: draft
tools: [search_gmail]
---
# Retry policy
Use bounded retry only for safe reads.
"""
    with TestClient(app) as client:
        async def exercise():
            pool = await get_pool()
            async with pool.acquire() as conn, conn.transaction():
                bundle_hash = await stage_okf_candidate_bundle(
                    conn, [{
                        "path": "knowledge/policies/candidate-retry.md",
                        "change_type": "create", "content": candidate,
                    }], source_version="fixture-commit",
                    validation_report={"passed": True, "trusted_identity": "fixture-ci"},
                    privacy_report={"pii_scan": "passed"},
                    security_report={"secret_scan": "passed"},
                )
                status = await conn.fetchval(
                    "SELECT publication_status FROM okf_bundle_versions WHERE bundle_hash=$1",
                    bundle_hash,
                )
                document = await conn.fetchval(
                    """SELECT title FROM okf_bundle_documents
                       WHERE bundle_hash=$1 AND document_id='policies/candidate-retry.md'""",
                    bundle_hash,
                )
                await conn.execute(
                    "DELETE FROM okf_bundle_versions WHERE bundle_hash=$1", bundle_hash,
                )
                return status, document

        status, document = client.portal.call(exercise)
        assert status == "validated"
        assert document == "Candidate retry policy"


def test_private_tool_result_store_is_encrypted_and_tenant_scoped():
    from app.api.main import app
    from app.db.connection import get_pool
    from app.tools.result_store import load_private_tool_result, store_private_tool_result

    user_id = f"private-result-{uuid.uuid4()}@example.com"
    with TestClient(app) as client:
        token = client.post("/auth/token", json={"email": user_id}).json()["access_token"]
        response = client.post(
            "/runs", headers={"Authorization": f"Bearer {token}"},
            json={"session_id": f"private-{uuid.uuid4()}", "message": "what can you do?"},
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]

        async def exercise():
            pool = await get_pool()
            async with pool.acquire() as conn:
                step_id = await conn.fetchval(
                    "SELECT id FROM agent_run_steps WHERE run_id=$1 ORDER BY sequence_no LIMIT 1",
                    uuid.UUID(run_id),
                )
            raw = {"body_plain": "tenant private fixture", "id": "message-1"}
            reference = await store_private_tool_result(
                pool, user_id=user_id, run_id=run_id, step_id=str(step_id),
                tool_name="get_gmail_message", result=raw,
            )
            loaded = await load_private_tool_result(pool, reference, user_id)
            denied = False
            try:
                await load_private_tool_result(pool, reference, "different@example.com")
            except ValueError:
                denied = True
            async with pool.acquire() as conn:
                encrypted = await conn.fetchval(
                    "SELECT encrypted_payload FROM private_tool_results WHERE run_id=$1",
                    uuid.UUID(run_id),
                )
                await conn.execute("DELETE FROM agent_runs WHERE id=$1", uuid.UUID(run_id))
            return reference, loaded, encrypted, denied

        reference, loaded, encrypted, denied = client.portal.call(exercise)
        assert reference.startswith("private-tool-result:")
        assert loaded["body_plain"] == "tenant private fixture"
        assert "tenant private fixture" not in encrypted
        assert denied is True


def test_frontend_candidate_handoff_is_attested_and_user_scoped():
    from app.api.main import app
    from app.db.connection import get_pool

    marker = str(uuid.uuid4())
    proposal_key = f"frontend-canary-{marker}"
    version = "a" * 40
    selected_email = "frontend-selected@example.com"
    with TestClient(app) as client:
        selected_token = client.post(
            "/auth/token", json={"email": selected_email},
        ).json()["access_token"]
        other_token = client.post(
            "/auth/token", json={"email": "frontend-other@example.com"},
        ).json()["access_token"]

        async def seed():
            pool = await get_pool()
            async with pool.acquire() as conn, conn.transaction():
                proposal_id = await conn.fetchval(
                    """INSERT INTO improvement_proposals
                       (proposal_key,proposal_type,title,sanitized_summary,status,
                        source_version,candidate_version,content_hash,candidate_kind,
                        candidate_state,candidate_manifest,deployment_evidence)
                       VALUES($1,'policy','Frontend fixture','safe','canary_active',$2,$3,$4,
                              'code','deployed_canary',$5::jsonb,$6::jsonb)
                       RETURNING id""",
                    proposal_key, "b" * 40, version, marker,
                    json.dumps({
                        "runtime_surfaces": ["frontend"],
                        "applicability": {"rag_modes": ["none"]},
                    }),
                    json.dumps({
                        "verified": True,
                        "candidate_version": version,
                        "frontend_source_commit": version,
                        "frontend_deployment_id": "dpl_frontend_fixture",
                        "frontend_url": "https://candidate-fixture.vercel.app",
                        "trusted_identity": "github-actions:fixture",
                    }),
                )
                await conn.execute(
                    """INSERT INTO improvement_canaries
                       (proposal_id,cohort,status,control_version,candidate_version,
                        started_at,routing_enabled,traffic_percent,allowed_users,denied_users)
                       VALUES($1,'{}','active',$2,$3,now(),TRUE,5,$4,$5)""",
                    proposal_id, "b" * 40, version, [selected_email], [],
                )

        async def cleanup():
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM improvement_proposals WHERE proposal_key=$1",
                    proposal_key,
                )

        client.portal.call(seed)
        selected = client.get(
            "/auth/frontend-candidate",
            headers={"Authorization": f"Bearer {selected_token}"},
        )
        assert selected.status_code == 200
        assert selected.json() == {
            "eligible": True,
            "url": "https://candidate-fixture.vercel.app",
            "candidate_version": version,
            "canary_id": selected.json()["canary_id"],
            "reason": "explicit allowlist",
        }
        other = client.get(
            "/auth/frontend-candidate",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert other.status_code == 200
        assert other.json() == {"eligible": False}
        client.portal.call(cleanup)


def test_okf_overlay_requires_and_records_independent_human_approval():
    from app.api.main import app
    from app.db.connection import get_pool
    from app.okf.candidates import stage_okf_candidate_bundle

    marker = str(uuid.uuid4())
    proposal_key = f"mixed-okf-{marker}"
    content = """---
type: policy
title: Mixed candidate policy
owner: workspace-agent
version: 1
timestamp: 2026-07-21
visibility: public
publication_status: draft
---
Use bounded metadata projection.
"""
    with TestClient(app) as client:
        admin = client.post(
            "/auth/token", json={"email": "achintyat256@gmail.com"}
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {admin}"}

        async def seed():
            pool = await get_pool()
            async with pool.acquire() as conn, conn.transaction():
                bundle_hash = await stage_okf_candidate_bundle(
                    conn, [{
                        "path": "knowledge/policies/mixed-candidate.md",
                        "change_type": "create", "content": content,
                    }], source_version="fixture-commit",
                    validation_report={"passed": True, "trusted_identity": "fixture-ci"},
                    privacy_report={"pii_scan": "passed"},
                    security_report={"secret_scan": "passed"},
                )
                await conn.execute(
                    """INSERT INTO improvement_proposals
                       (proposal_key,proposal_type,title,sanitized_summary,status,
                        content_hash,candidate_kind,candidate_state,candidate_manifest)
                       VALUES($1,'policy','Mixed OKF','safe','awaiting_review',$2,'code',
                              'validated_implementation',$3::jsonb)""",
                    proposal_key, marker, json.dumps({
                        "okf_bundle_hash": bundle_hash,
                        "okf_approval_status": "awaiting_review",
                        "applicability": {"rag_modes": ["none"]},
                    }),
                )
                return bundle_hash

        bundle_hash = client.portal.call(seed)
        response = client.post(
            f"/admin/improvements/{proposal_key}/okf-publication-decision",
            headers=headers,
            json={"decision": "approved", "proposal_hash": marker},
        )
        assert response.status_code == 200

        async def verify_and_cleanup():
            pool = await get_pool()
            async with pool.acquire() as conn, conn.transaction():
                manifest = await conn.fetchval(
                    "SELECT candidate_manifest FROM improvement_proposals WHERE proposal_key=$1",
                    proposal_key,
                )
                status = await conn.fetchval(
                    "SELECT publication_status FROM okf_bundle_versions WHERE bundle_hash=$1",
                    bundle_hash,
                )
                await conn.execute(
                    "DELETE FROM improvement_proposals WHERE proposal_key=$1", proposal_key,
                )
                await conn.execute(
                    "DELETE FROM okf_bundle_versions WHERE bundle_hash=$1", bundle_hash,
                )
                return manifest, status

        manifest, status = client.portal.call(verify_and_cleanup)
        assert manifest["okf_approval_status"] == "approved"
        assert status == "validated"


def test_hierarchical_rag_stores_and_expands_tenant_parent(monkeypatch):
    from app.api.main import app
    from app.db.connection import get_pool
    from app.rag.ingestion import index_tool_result
    from app.rag.retriever import hybrid_retrieve

    class FakeEmbedder:
        async def aembed_documents(self, texts):
            return [[0.01] * 768 for _ in texts]

    async def unavailable_query(*_args, **_kwargs):
        raise RuntimeError("dense retrieval deliberately disabled for lexical fixture")

    monkeypatch.setattr(
        "app.rag.retriever.NomicEmbedder.aembed_query", unavailable_query,
    )
    source_id = f"hierarchical-{uuid.uuid4()}"
    user_id = "hierarchical@example.com"
    content = "# Recovery\n" + "reliable worker context " * 180 + "violetparentmarker"
    with TestClient(app) as client:
        async def exercise():
            pool = await get_pool()
            indexed = await index_tool_result(
                "get_drive_file", {"file_id": source_id},
                {"id": source_id, "name": "Recovery", "content": content},
                pool, FakeEmbedder(), user_id,
            )
            results = await hybrid_retrieve(
                "violetparentmarker", pool=pool, user_id=user_id,
                filters={"source": "drive"}, top_k=3,
            )
            unrelated = await hybrid_retrieve(
                "violetparentmarker", pool=pool, user_id="unrelated@example.com",
                filters={"source": "drive"}, top_k=3,
            )
            unchanged = await index_tool_result(
                "get_drive_file", {"file_id": source_id},
                {"id": source_id, "name": "Recovery", "content": content},
                pool, FakeEmbedder(), user_id,
            )
            await index_tool_result(
                "get_drive_file", {"file_id": source_id},
                {"id": source_id, "name": "Recovery",
                 "content": "# Recovery\nshort violetparentmarker"},
                pool, FakeEmbedder(), user_id,
            )
            async with pool.acquire() as conn:
                parents = await conn.fetchval(
                    """SELECT count(*) FROM rag_parent_sections
                       WHERE user_id=$1 AND source_id=$2 AND deleted_at IS NULL""",
                    user_id, source_id,
                )
                tombstones = await conn.fetchval(
                    """SELECT count(*) FROM rag_chunks
                       WHERE user_id=$1 AND source_id=$2 AND deleted_at IS NOT NULL""",
                    user_id, source_id,
                )
                await conn.execute(
                    "DELETE FROM rag_chunks WHERE user_id=$1 AND source_id=$2",
                    user_id, source_id,
                )
                await conn.execute(
                    "DELETE FROM rag_parent_sections WHERE user_id=$1 AND source_id=$2",
                    user_id, source_id,
                )
            return indexed, parents, results, unrelated, unchanged, tombstones

        indexed, parents, results, unrelated, unchanged, tombstones = client.portal.call(
            exercise
        )
        assert indexed > 1
        assert parents >= 1
        assert results[0]["parent_expanded"] is True
        assert "violetparentmarker" in results[0]["content"]
        assert len(results[0]["content"]) > len(results[0]["child_content"])
        assert results[0]["citation"]["parent_id"]
        assert results[0]["citation"]["context_level"] == "parent"
        assert unrelated == []
        assert unchanged == 0
        assert tombstones >= 1


def test_run_survives_browser_disconnect_and_can_be_cancelled_after_reconnect():
    from app.api.main import app
    from app.db.connection import get_pool

    email = f"reconnect-{uuid.uuid4()}@example.com"
    session_id = f"reconnect-{uuid.uuid4()}"
    with TestClient(app) as first_browser:
        token = first_browser.post("/auth/token", json={"email": email}).json()[
            "access_token"
        ]
        created = first_browser.post(
            "/runs", headers={"Authorization": f"Bearer {token}"},
            json={"message": "Send an email to pilot@example.com",
                  "session_id": session_id},
        ).json()
        run_id = created["run_id"]

    with TestClient(app) as reconnected_browser:
        headers = {"Authorization": f"Bearer {token}"}
        restored = reconnected_browser.get(f"/runs/{run_id}", headers=headers)
        assert restored.status_code == 200
        assert restored.json()["status"] == "awaiting_approval"
        cancelled = reconnected_browser.post(f"/runs/{run_id}/cancel", headers=headers)
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"

        async def cleanup():
            async with (await get_pool()).acquire() as conn:
                await conn.execute("DELETE FROM agent_runs WHERE id=$1", uuid.UUID(run_id))

        reconnected_browser.portal.call(cleanup)


def test_prompt_bandit_requires_human_activation_and_evidence():
    from app.api.main import app
    from app.db.connection import get_pool

    name = f"bandit-{uuid.uuid4()}"
    with TestClient(app) as client:
        admin = client.post(
            "/auth/token", json={"email": "achintyat256@gmail.com"}
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {admin}"}
        prompts = client.get("/admin/prompts", headers=headers).json()["prompts"]
        candidates = [item for item in prompts if item["name"] == "supervisor_system"]
        created = client.post("/admin/experiments", headers=headers, json={
            "name": name, "prompt_name": "supervisor_system",
            "control_id": candidates[0]["id"], "variant_id": candidates[1]["id"],
            "selection_policy": "thompson",
        })
        assert created.status_code == 200
        assert created.json()["status"] == "draft"
        refused = client.post(f"/admin/experiments/{name}/activate", headers=headers,
                              json={"confirmation": "yes", "evidence": {}})
        assert refused.status_code == 409
        activated = client.post(f"/admin/experiments/{name}/activate", headers=headers,
                                json={
                                    "confirmation": "ACTIVATE LOW RISK EXPERIMENT",
                                    "evidence": {"suite_version": "test-offline-v1"},
                                })
        assert activated.status_code == 200
        assert activated.json()["validated"] is True

        async def cleanup():
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM prompt_experiments WHERE name=$1", name)

        client.portal.call(cleanup)


def test_feedback_preserves_retrieved_context():
    from app.api.main import app
    from app.db.connection import get_pool

    session_id = f"feedback-test-{uuid.uuid4()}"
    context = [{"source": "gmail", "content": "Budget meeting", "score": 0.9}]

    with TestClient(app) as client:
        token = client.post(
            "/auth/token", json={"email": "user@example.com"}
        ).json()["access_token"]

        async def seed_and_read(read=False):
            pool = await get_pool()
            async with pool.acquire() as conn:
                if read:
                    return await conn.fetchval(
                        "SELECT retrieved_docs FROM feedback WHERE session_id=$1",
                        session_id,
                    )
                await conn.execute(
                    """INSERT INTO conversation_history
                       (session_id,user_id,role,content)
                       VALUES($1,'user@example.com','user','What is the budget?')""",
                    session_id,
                )
                await conn.execute(
                    """INSERT INTO conversation_history
                       (session_id,user_id,role,content,tool_results)
                       VALUES($1,'user@example.com','assistant','It is in the email.',
                              $2::jsonb)""",
                    session_id,
                    json.dumps(context),
                )
        client.portal.call(seed_and_read)
        response = client.post(
            "/feedback",
            json={"session_id": session_id, "rating": 1},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json() == {"status": "recorded", "learning_candidate": False}
        stored = client.portal.call(seed_and_read, True)
        assert (json.loads(stored) if isinstance(stored, str) else stored) == context


def test_consented_trajectory_is_sanitized_and_assigned_without_leakage():
    from app.api.main import app
    from app.db.connection import get_pool

    user_id = f"learning-{uuid.uuid4()}@example.com"
    private_email = "private.person@company.test"
    run_key = f"learning-run-{uuid.uuid4()}"
    with TestClient(app) as client:
        token = client.post("/auth/token", json={"email": user_id}).json()["access_token"]

        async def seed():
            pool = await get_pool()
            async with pool.acquire() as conn:
                return await conn.fetchval(
                    """INSERT INTO agent_runs
                       (session_id,user_id,request,status,idempotency_key,plan,result,
                        incident_summary)
                       VALUES($1,$2,$3,'completed',$1,$4::jsonb,$5::jsonb,$6::jsonb)
                       RETURNING id""",
                    run_key, user_id, f"Email {private_email}",
                    json.dumps({"objective": f"Email {private_email}"}),
                    json.dumps({"output": f"Prepared for {private_email}"}),
                    json.dumps({"error": f"token=do-not-store for {private_email}"}),
                )

        async def read_and_cleanup(run_id):
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT state,decision,observation,dataset_split,sanitized,consented
                       FROM learning_trajectories WHERE run_id=$1""", run_id,
                )
                await conn.execute("DELETE FROM agent_runs WHERE id=$1", run_id)
                return dict(row)

        run_id = client.portal.call(seed)
        response = client.post(
            "/feedback", headers={"Authorization": f"Bearer {token}"},
            json={"run_id": str(run_id), "rating": -1, "categories": ["wrong_data"],
                  "comment": f"Wrong recipient {private_email}",
                  "consented_for_learning": True},
        )
        assert response.status_code == 200
        trajectory = client.portal.call(read_and_cleanup, run_id)
        serialized = json.dumps(trajectory, default=str)
        assert private_email not in serialized
        assert "do-not-store" not in serialized
        assert trajectory["dataset_split"] in {"train", "validation", "test"}
        assert trajectory["sanitized"] is True
        assert trajectory["consented"] is True


def test_durable_high_risk_run_requires_action_bound_approval():
    from app.api.main import app

    with TestClient(app) as client:
        token = client.post(
            "/auth/token", json={"email": "user@example.com"}
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        created = client.post(
            "/runs",
            headers=headers,
            json={
                "message": "Send an email to another@example.com",
                "session_id": f"run-test-{uuid.uuid4()}",
            },
        )
        assert created.status_code == 202
        payload = created.json()
        assert payload["status"] == "awaiting_approval"
        run = client.get(f"/runs/{payload['run_id']}", headers=headers).json()
        assert run["approval"]["action_hash"]
        rejected = client.post(
            f"/runs/{payload['run_id']}/approve",
            headers=headers,
            json={
                "approved": False,
                "action_hash": run["approval"]["action_hash"],
                "note": "integration test",
            },
        )
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "cancelled"
        final = client.get(f"/runs/{payload['run_id']}", headers=headers).json()
        assert final["status"] == "cancelled"
        events = client.get(
            f"/runs/{payload['run_id']}/events", headers=headers
        ).json()["events"]
        assert any(event["event_type"] == "approval_rejected" for event in events)


def test_run_idempotency_and_cross_user_isolation():
    from app.api.main import app

    with TestClient(app) as client:
        first_token = client.post(
            "/auth/token", json={"email": "first@example.com"}
        ).json()["access_token"]
        second_token = client.post(
            "/auth/token", json={"email": "second@example.com"}
        ).json()["access_token"]
        first = {"Authorization": f"Bearer {first_token}"}
        second = {"Authorization": f"Bearer {second_token}"}
        key = f"idempotency-{uuid.uuid4()}"
        body = {
            "message": "List recent Gmail messages",
            "session_id": f"isolation-{uuid.uuid4()}",
            "idempotency_key": key,
        }
        created = client.post("/runs", headers=first, json=body).json()
        duplicate = client.post("/runs", headers=first, json=body).json()
        assert duplicate["run_id"] == created["run_id"]
        assert duplicate["created"] is False
        assert client.get(f"/runs/{created['run_id']}", headers=second).status_code == 404
        assert client.get(
            f"/sessions/{body['session_id']}/runs", headers=second
        ).json() == {"runs": []}


def test_filterable_run_history_remains_tenant_scoped():
    from app.api.main import app
    from app.db.connection import get_pool

    first_email = f"history-first-{uuid.uuid4()}@example.com"
    second_email = f"history-second-{uuid.uuid4()}@example.com"
    marker = f"history-{uuid.uuid4()}"
    with TestClient(app) as client:
        first_token = client.post("/auth/token", json={"email": first_email}).json()[
            "access_token"
        ]

        async def seed_and_cleanup(cleanup=False):
            pool = await get_pool()
            async with pool.acquire() as conn:
                if cleanup:
                    await conn.execute("DELETE FROM agent_runs WHERE session_id=$1", marker)
                    return
                for user, status in ((first_email, "completed"), (second_email, "failed")):
                    run_id = await conn.fetchval(
                        """INSERT INTO agent_runs
                           (session_id,user_id,request,status,current_phase,idempotency_key,
                            deployment_version,completed_at)
                           VALUES($1,$2,'History fixture',$3,$3,$4,'history-v1',now())
                           RETURNING id""",
                        marker, user, status, f"{marker}-{user}",
                    )
                    await conn.execute(
                        """INSERT INTO agent_run_steps
                           (run_id,step_key,sequence_no,title,service,operation,status)
                           VALUES($1,'drive',1,'Drive fixture','drive','search',$2)""",
                        run_id, status,
                    )

        client.portal.call(seed_and_cleanup)
        response = client.get(
            f"/runs?session_id={marker}&service=drive&deployment_version=history-v1",
            headers={"Authorization": f"Bearer {first_token}"},
        )
        client.portal.call(seed_and_cleanup, True)
        assert response.status_code == 200
        rows = response.json()["runs"]
        assert len(rows) == 1
        assert rows[0]["user_id"] == first_email
        assert rows[0]["services"] == ["drive"]


def test_preserve_compensation_never_calls_an_external_api():
    from app.api.main import app
    from app.db.connection import get_pool

    email = f"cleanup-{uuid.uuid4()}@example.com"
    marker = f"cleanup-{uuid.uuid4()}"
    with TestClient(app) as client:
        token = client.post("/auth/token", json={"email": email}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        async def seed_and_cleanup(cleanup=False):
            pool = await get_pool()
            async with pool.acquire() as conn:
                if cleanup:
                    await conn.execute("DELETE FROM agent_runs WHERE session_id=$1", marker)
                    return None
                run_id = await conn.fetchval(
                    """INSERT INTO agent_runs
                       (session_id,user_id,request,status,current_phase,idempotency_key,completed_at)
                       VALUES($1,$2,'Cleanup fixture','partial','partial',$1,now()) RETURNING id""",
                    marker, email,
                )
                artifact_id = await conn.fetchval(
                    """INSERT INTO agent_artifacts
                       (run_id,user_id,artifact_type,external_id,verification_status,
                        cleanup_state,safe_to_delete)
                       VALUES($1,$2,'sheets','sheet-fixture','verified','retained',TRUE)
                       RETURNING id""",
                    run_id, email,
                )
                return str(run_id), str(artifact_id)

        run_id, artifact_id = client.portal.call(seed_and_cleanup)
        response = client.post(
            f"/runs/{run_id}/artifacts/{artifact_id}/cleanup-request",
            headers=headers, json={"action": "preserve"},
        )
        client.portal.call(seed_and_cleanup, True)
        assert response.status_code == 200
        assert response.json()["status"] == "completed"
        assert response.json()["action_hash"] is None


def test_account_export_is_tenant_scoped_and_excludes_oauth_secrets():
    from app.api.main import app
    from app.db.connection import get_pool

    first_email = f"export-first-{uuid.uuid4()}@example.com"
    second_email = f"export-second-{uuid.uuid4()}@example.com"
    with TestClient(app) as client:
        first_token = client.post("/auth/token", json={"email": first_email}).json()[
            "access_token"
        ]

        async def seed():
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.executemany(
                    """INSERT INTO conversation_history
                       (session_id,user_id,role,content) VALUES($1,$2,'user',$3)""",
                    [("export-first", first_email, "first-private"),
                     ("export-second", second_email, "second-private")],
                )

        async def cleanup():
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM conversation_history WHERE user_id=ANY($1::text[])",
                    [first_email, second_email],
                )

        client.portal.call(seed)
        response = client.get(
            "/auth/account-data/export",
            headers={"Authorization": f"Bearer {first_token}"},
        )
        client.portal.call(cleanup)
        assert response.status_code == 200
        payload = response.json()
        assert payload["user_id"] == first_email
        assert payload["oauth_credentials_excluded"] is True
        serialized = json.dumps(payload)
        assert "first-private" in serialized
        assert "second-private" not in serialized
        assert "encrypted_credentials" not in serialized


def test_per_user_active_run_limit_is_enforced():
    from app.api.main import app

    email = f"limit-{uuid.uuid4()}@example.com"
    with TestClient(app) as client:
        token = client.post("/auth/token", json={"email": email}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        for index in range(3):
            response = client.post("/runs", headers=headers, json={
                "message": f"Send an email to recipient{index}@example.com",
                "session_id": f"limit-{uuid.uuid4()}",
            })
            assert response.status_code == 202
            assert response.json()["status"] == "awaiting_approval"
        limited = client.post("/runs", headers=headers, json={
            "message": "Send an email to last@example.com",
            "session_id": f"limit-{uuid.uuid4()}",
        })
        assert limited.status_code == 429
        assert "Too many active runs" in limited.json()["detail"]["message"]
        assert limited.json()["detail"]["stage"] == "admission"
        assert limited.json()["detail"]["incident_id"]


def test_worker_executes_dependency_steps_and_recovers_expired_lease():
    from types import SimpleNamespace

    from app.api.main import app
    from app.db.connection import get_pool
    from app.runs.repository import create_run, get_run
    from app.runs.worker import claim_run, execute_run

    class FakeGraph:
        def __init__(self):
            self.services = []
            self.active = 0
            self.max_active = 0

        async def ainvoke(self, state, config):
            self.services.append(state["forced_service"])
            service = state["forced_service"]
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.02)
            self.active -= 1
            return {
                "output": f"verified {service}",
                "tool_results": [{"id": f"{service}-resource"}],
                "task_complete": True,
            }

    with TestClient(app) as client:
        async def exercise():
            pool = await get_pool()
            run, _ = await create_run(
                pool, "worker@example.com",
                "List recent Gmail messages and Drive files",
                f"worker-{uuid.uuid4()}", f"worker-{uuid.uuid4()}",
            )
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE agent_runs SET status='running',lease_owner='dead-worker',
                       lease_expires_at=now()-interval '1 minute',
                       queued_at=now()-interval '100 years' WHERE id=$1""",
                    run["id"],
                )
                await conn.execute(
                    """UPDATE agent_run_steps SET status='running',attempt_count=1,
                       started_at=now()-interval '2 minutes'
                       WHERE run_id=$1 AND sequence_no=1""",
                    run["id"],
                )
            claimed = await claim_run(pool, "replacement-worker")
            graph = FakeGraph()
            fake_app = SimpleNamespace(state=SimpleNamespace(agent_graph=graph))
            await execute_run(fake_app, pool, claimed)
            completed = await get_run(pool, run["id"], "worker@example.com")
            return completed, graph.services, graph.max_active

        completed, services, max_active = client.portal.call(exercise)
        assert completed["status"] == "completed"
        assert [step["status"] for step in completed["steps"]] == [
            "completed", "completed",
        ]
        assert set(services) == {"gmail", "drive"}
        assert max_active == 2


def test_expired_write_lease_requires_reconciliation_and_blocks_resume():
    from app.api.main import app
    from app.db.connection import get_pool
    from app.runs.repository import create_run, get_run
    from app.runs.worker import claim_run

    user_id = "stale-write@example.com"
    with TestClient(app) as client:
        async def exercise():
            pool = await get_pool()
            marker = f"stale-write-{uuid.uuid4()}"
            run, _ = await create_run(
                pool, user_id,
                "Send an email to fixture@example.com with subject Lease and body Test "
                "without asking",
                marker, marker,
            )
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE agent_runs SET status='running',lease_owner='dead-worker',
                       lease_expires_at=now()-interval '1 minute',
                       queued_at=now()-interval '100 years' WHERE id=$1""",
                    run["id"],
                )
                await conn.execute(
                    """UPDATE agent_run_steps SET status='running',attempt_count=1,
                       started_at=now()-interval '2 minutes' WHERE run_id=$1""",
                    run["id"],
                )
            recovered = await claim_run(pool, "replacement-worker")
            stored = await get_run(pool, run["id"], user_id)
            incident_count = await pool.fetchval(
                "SELECT count(*) FROM failure_incidents WHERE run_id=$1", run["id"]
            )
            return recovered, stored, incident_count

        recovered, stored, incident_count = client.portal.call(exercise)
        assert recovered["_terminal_recovery"] is True
        assert stored["status"] == "failed"
        assert stored["current_phase"] == "reconciliation"
        assert stored["error_category"] == "worker_reconciliation"
        assert stored["side_effect_integrity"] == 0
        assert stored["steps"][0]["status"] == "failed"
        assert incident_count == 1

        token = client.post("/auth/token", json={"email": user_id}).json()["access_token"]
        response = client.post(
            f"/runs/{stored['id']}/resume",
            headers={"Authorization": f"Bearer {token}"},
            json={"retry_failed_step": True},
        )
        assert response.status_code == 409
        assert "may already have completed" in response.json()["detail"]


def test_expired_read_lease_with_exhausted_budget_fails_without_side_effect_risk():
    from app.api.main import app
    from app.db.connection import get_pool
    from app.runs.repository import create_run, get_run
    from app.runs.worker import claim_run

    user_id = "stale-read@example.com"
    with TestClient(app) as client:
        async def exercise():
            pool = await get_pool()
            marker = f"stale-read-{uuid.uuid4()}"
            run, _ = await create_run(
                pool, user_id, "List recent Gmail messages", marker, marker,
            )
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE agent_runs SET status='running',lease_owner='dead-worker',
                       lease_expires_at=now()-interval '1 minute',
                       queued_at=now()-interval '100 years' WHERE id=$1""",
                    run["id"],
                )
                await conn.execute(
                    """UPDATE agent_run_steps SET status='running',
                       attempt_count=max_attempts,started_at=now()-interval '2 minutes'
                       WHERE run_id=$1""",
                    run["id"],
                )
            recovered = await claim_run(pool, "replacement-worker")
            stored = await get_run(pool, run["id"], user_id)
            return recovered, stored

        recovered, stored = client.portal.call(exercise)
        assert recovered["_terminal_recovery"] is True
        assert stored["status"] == "failed"
        assert stored["current_phase"] == "failed"
        assert stored["error_category"] == "worker"
        assert stored["side_effect_integrity"] == 100
        assert stored["incident_summary"]["recoverable"] is True


def test_durable_informational_run_completes_without_graph_or_model_calls():
    from types import SimpleNamespace

    from app.api.main import app
    from app.db.connection import get_pool
    from app.runs.repository import create_run, get_run
    from app.runs.worker import execute_run

    class ForbiddenGraph:
        async def ainvoke(self, *_args, **_kwargs):
            raise AssertionError("Informational runs must not invoke the agent graph")

    with TestClient(app) as client:
        async def exercise():
            pool = await get_pool()
            marker = f"information-{uuid.uuid4()}"
            run, _ = await create_run(
                pool, "information@example.com",
                "what can you do and what is your name?", marker, marker,
            )
            async with pool.acquire() as conn:
                claimed = await conn.fetchrow(
                    """UPDATE agent_runs SET status='running',lease_owner='information-test',
                       lease_expires_at=now()+interval '1 minute' WHERE id=$1 RETURNING *""",
                    run["id"],
                )
            fake_app = SimpleNamespace(state=SimpleNamespace(agent_graph=ForbiddenGraph()))
            await execute_run(fake_app, pool, dict(claimed))
            completed = await get_run(pool, run["id"], "information@example.com")
            async with pool.acquire() as conn:
                model_calls = await conn.fetchval(
                    "SELECT count(*) FROM agent_model_calls WHERE run_id=$1", run["id"]
                )
                tool_calls = await conn.fetchval(
                    "SELECT count(*) FROM agent_tool_attempts WHERE run_id=$1", run["id"]
                )
                await conn.execute("DELETE FROM agent_runs WHERE id=$1", run["id"])
            return completed, model_calls, tool_calls

        completed, model_calls, tool_calls = client.portal.call(exercise)
        assert completed["status"] == "completed"
        assert completed["technical_completion"] == 100
        assert completed["functional_completion"] == 100
        assert completed["user_visible_completion"] == 100
        assert "Google Workspace Agent" in completed["result"]["output"]
        assert model_calls == 0
        assert tool_calls == 0


def test_diagnosis_only_proposal_cannot_be_approved_for_canary():
    from app.api.main import app
    from app.db.connection import get_pool

    marker = str(uuid.uuid4())
    proposal_key = f"diagnosis-{marker}"
    with TestClient(app) as client:
        admin = client.post(
            "/auth/token", json={"email": "achintyat256@gmail.com"}
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {admin}"}

        async def seed():
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO improvement_proposals
                       (proposal_key,proposal_type,title,sanitized_summary,status,
                        content_hash,candidate_kind,candidate_state)
                       VALUES($1,'policy','Diagnosis','No private content','awaiting_review',
                              $2,'diagnosis','diagnosis_only')""",
                    proposal_key, marker,
                )

        async def cleanup():
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM improvement_proposals WHERE proposal_key=$1", proposal_key
                )

        client.portal.call(seed)
        response = client.post(
            f"/admin/improvements/{proposal_key}/canary-decision",
            headers=headers,
            json={"decision": "approved", "proposal_hash": marker},
        )
        assert response.status_code == 409
        assert "diagnosis-only" in response.json()["detail"]
        candidate = client.put(
            f"/admin/improvements/{proposal_key}/candidate",
            headers=headers,
            json={
                "candidate_kind": "code",
                "base_version": "abcdef1",
                "candidate_version": "abcdef2",
                "exact_diff": "--- app/example.py\n+++ app/example.py\n+VALUE = 2",
                "files": [{
                    "path": "app/example.py", "change_type": "create",
                    "content": "VALUE = 2\n",
                }],
                "validation_report": {
                    "passed": True, "commands": ["pytest tests/unit -q"],
                },
                "rollback_plan": {"action": "restore abcdef1"},
                "applicability": {
                    "services": ["gmail"], "operations": ["search"],
                    "rag_modes": ["none"],
                },
            },
        )
        assert candidate.status_code == 200
        candidate_hash = candidate.json()["content_hash"]
        approved = client.post(
            f"/admin/improvements/{proposal_key}/canary-decision",
            headers=headers,
            json={"decision": "approved", "proposal_hash": candidate_hash},
        )
        assert approved.status_code == 409
        assert "trusted CI" in approved.json()["detail"]

        async def attach_trusted_ci_fixture():
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE improvement_proposals SET validation_report=$1::jsonb
                       WHERE proposal_key=$2""",
                    json.dumps({"passed": True, "commands": ["pytest tests/unit -q"],
                                "trusted_identity": "github-actions:test"}),
                    proposal_key,
                )

        client.portal.call(attach_trusted_ci_fixture)
        approved = client.post(
            f"/admin/improvements/{proposal_key}/canary-decision",
            headers=headers,
            json={"decision": "approved", "proposal_hash": candidate_hash},
        )
        assert approved.status_code == 200
        blocked_activation = client.post(
            f"/admin/improvements/{proposal_key}/activate-canary",
            headers=headers,
            json={"decision": "approved", "proposal_hash": candidate_hash},
        )
        assert blocked_activation.status_code == 409
        deployed = client.put(
            f"/admin/improvements/{proposal_key}/deployment-evidence",
            headers=headers,
            json={
                "candidate_version": "abcdef2", "deployment_id": "fixture-deploy",
                "deployment_url": "https://example.invalid/deploy",
                "verified": True, "smoke_tests": {"passed": True},
            },
        )
        assert deployed.status_code == 409
        assert "trusted isolated deployment controller" in deployed.json()["detail"]

        async def attach_trusted_deployment_fixture():
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE improvement_proposals SET deployment_evidence=$1::jsonb
                       WHERE proposal_key=$2""",
                    json.dumps({
                        "candidate_version": "abcdef2", "verified": True,
                        "smoke_tests": {"passed": True},
                        "trusted_identity": "github-actions:test-deploy",
                    }), proposal_key,
                )

        client.portal.call(attach_trusted_deployment_fixture)
        activated = client.post(
            f"/admin/improvements/{proposal_key}/activate-canary",
            headers=headers,
            json={"decision": "approved", "proposal_hash": candidate_hash},
        )
        assert activated.status_code == 200
        refreshed = client.get("/admin/improvements", headers=headers).json()["proposals"]
        stored = next(item for item in refreshed if item["proposal_key"] == proposal_key)
        assert stored["candidate_state"] == "deployed_canary"
        client.portal.call(cleanup)


def test_canary_regression_is_evaluated_and_rolled_back():
    from app.api.main import app
    from app.db.connection import get_pool
    from app.improvements.analyzer import evaluate_active_canaries

    marker = str(uuid.uuid4())
    source = f"control-{marker}"
    candidate = f"candidate-{marker}"
    proposal_key = f"canary-{marker}"

    with TestClient(app) as client:
        async def exercise():
            pool = await get_pool()
            async with pool.acquire() as conn, conn.transaction():
                proposal_id = await conn.fetchval(
                    """INSERT INTO improvement_proposals
                       (proposal_key,proposal_type,title,sanitized_summary,status,
                        source_version,candidate_version,content_hash)
                       VALUES($1,'policy','Fixture','No user content','canary_active',$2,$3,$4)
                       RETURNING id""",
                    proposal_key, source, candidate, marker,
                )
                canary_id = await conn.fetchval(
                    """INSERT INTO improvement_canaries
                       (proposal_id,cohort,status,control_version,candidate_version,started_at)
                       VALUES($1,'{}','active',$2,$3,now()-interval '2 hours') RETURNING id""",
                    proposal_id, source, candidate,
                )
                for index in range(5):
                    await conn.execute(
                        """INSERT INTO agent_runs
                           (session_id,user_id,request,status,idempotency_key,
                            deployment_version,executor_version,canary_id,cohort_assignment,
                            started_at,completed_at,input_tokens)
                           VALUES($1,'fixture@example.com','fixture','completed',$2,$3,$3,$4,
                                  'control',now()-interval '2 seconds',now(),100)""",
                        marker, f"{marker}-control-{index}", source, canary_id,
                    )
                    await conn.execute(
                        """INSERT INTO agent_runs
                           (session_id,user_id,request,status,idempotency_key,
                            deployment_version,executor_version,canary_id,cohort_assignment,
                            started_at,completed_at,input_tokens,
                            side_effect_integrity)
                           VALUES($1,'fixture@example.com','fixture','partial',$2,$3,$3,$4,
                                  'candidate',now()-interval '4 seconds',now(),200,0)""",
                        marker, f"{marker}-candidate-{index}", candidate, canary_id,
                    )
            changed = await evaluate_active_canaries(pool)
            async with pool.acquire() as conn:
                canary = await conn.fetchrow(
                    "SELECT status,rollback_reason FROM improvement_canaries WHERE id=$1",
                    canary_id,
                )
                proposal = await conn.fetchval(
                    "SELECT status FROM improvement_proposals WHERE id=$1", proposal_id,
                )
                evaluation = await conn.fetchrow(
                    "SELECT passed,regressions FROM improvement_evaluations WHERE proposal_id=$1",
                    proposal_id,
                )
                await conn.execute("DELETE FROM improvement_proposals WHERE id=$1", proposal_id)
                await conn.execute("DELETE FROM agent_runs WHERE session_id=$1", marker)
            return changed, dict(canary), proposal, dict(evaluation)

        changed, canary, proposal, evaluation = client.portal.call(exercise)
        # The background analyzer and this explicit call intentionally race on
        # the same row lock. Either caller may perform the one allowed state
        # transition; the durable state below is the authoritative assertion.
        assert changed in (0, 1)
        assert canary["status"] == "rolled_back"
        assert "failure_rate" in canary["rollback_reason"]
        assert proposal == "rolled_back"
        assert evaluation["passed"] is False
