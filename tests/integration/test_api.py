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


def test_health_auth_and_route_protection():
    from app.api.main import app

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
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
        assert "Too many active runs" in limited.json()["detail"]


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
                            deployment_version,started_at,completed_at,input_tokens)
                           VALUES($1,'fixture@example.com','fixture','completed',$2,$3,
                                  now()-interval '2 seconds',now(),100)""",
                        marker, f"{marker}-control-{index}", source,
                    )
                    await conn.execute(
                        """INSERT INTO agent_runs
                           (session_id,user_id,request,status,idempotency_key,
                            deployment_version,started_at,completed_at,input_tokens,
                            side_effect_integrity)
                           VALUES($1,'fixture@example.com','fixture','partial',$2,$3,
                                  now()-interval '4 seconds',now(),200,0)""",
                        marker, f"{marker}-candidate-{index}", candidate,
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
        assert changed == 1
        assert canary["status"] == "rolled_back"
        assert "failure_rate" in canary["rollback_reason"]
        assert proposal == "rolled_back"
        assert evaluation["passed"] is False
