import pytest

from eduflow.core import health

pytestmark = pytest.mark.django_db


def test_live_is_public_and_ok(api_client):
    response = api_client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_checks_real_database_and_redis(api_client):
    response = api_client.get("/api/v1/health/ready")
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["cache"] == "ok"


def test_ready_returns_503_without_leaking_details(api_client, monkeypatch, json_logs):
    def broken() -> None:
        raise ConnectionError("redis://:hunter2@internal-redis.local:6379 refused")

    monkeypatch.setattr(health, "check_redis", broken)
    response = api_client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "checks": {"database": "ok", "cache": "failed"}}
    assert b"internal-redis" not in response.content
    assert b"hunter2" not in response.content
    # The failure is logged (type only, no exception text), for operators.
    [entry] = json_logs.find("readiness_check_failed")
    assert entry["check"] == "cache"
    assert entry["error_type"] == "ConnectionError"
    assert "hunter2" not in json_logs.text


def test_storage_check_included_when_enabled(settings, monkeypatch, api_client):
    settings.STORAGE_HEALTHCHECK_ENABLED = True
    calls = []
    monkeypatch.setattr(health, "check_storage", lambda: calls.append("storage"))
    response = api_client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["storage"] == "ok"
    assert calls == ["storage"]


def test_trailing_slash_is_not_redirected(api_client):
    # The client never uses trailing slashes. APPEND_SLASH is off, so no 301 dance on POSTs.
    response = api_client.get("/api/v1/health/live/")
    assert response.status_code == 404
