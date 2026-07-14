import os

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
