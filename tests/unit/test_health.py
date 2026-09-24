from fastapi.testclient import TestClient

from training_service.main import app

client = TestClient(app)


def test_health_endpoint() -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
