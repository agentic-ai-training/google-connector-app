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
