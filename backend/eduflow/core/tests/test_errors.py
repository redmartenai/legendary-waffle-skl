import pytest

# Requests run under the RLS database context (DatabaseContextMiddleware), so they touch the database.
pytestmark = [pytest.mark.urls("eduflow.core.tests.urls"), pytest.mark.django_db]


def _error(response):
    body = response.json()
    assert set(body) == {"error"}, body
    error = body["error"]
    assert error["request_id"] == response["X-Request-ID"]
    return error


def test_validation_error_has_field_messages(api_client):
    response = api_client.get("/test/boom/validation")
    assert response.status_code == 400
    error = _error(response)
    assert error["code"] == "validation_error"
    assert "name" in error["fields"]


def test_non_field_validation_error(api_client):
    error = _error(api_client.get("/test/boom/validation-list"))
    assert error["fields"] == {"non_field_errors": ["bad thing"]}


def test_throttled_reports_retry_after(api_client):
    response = api_client.get("/test/boom/throttled")
    assert response.status_code == 429
    error = _error(response)
    assert error["code"] == "rate_limited"
    assert error["retry_after_seconds"] == 13
    assert response["Retry-After"] == "13"


def test_permission_denied_does_not_echo_internal_reason(api_client):
    response = api_client.get("/test/boom/denied")
    assert response.status_code == 403
    error = _error(response)
    assert error["code"] == "permission_denied"
    assert "secret internal reason" not in response.content.decode()


def test_django_404_inside_api_view(api_client):
    response = api_client.get("/test/boom/django404")
    assert response.status_code == 404
    assert _error(response)["code"] == "not_found"
    assert "internal lookup detail" not in response.content.decode()


def test_unhandled_exception_is_generic_500_and_logged(api_client, json_logs):
    response = api_client.get("/test/boom/crash")
    assert response.status_code == 500
    error = _error(response)
    assert error == {
        "code": "server_error",
        "message": "The server had a problem. Please try again.",
        "request_id": response["X-Request-ID"],
    }
    assert "hunter2" not in response.content.decode()
    [entry] = json_logs.find("unhandled_api_exception")
    assert entry["request_id"] == response["X-Request-ID"]
    assert "RuntimeError" in entry["exception"]
    # Inline "password=..." inside the exception message is masked in the log as well.
    assert "hunter2" not in json_logs.text


def test_unknown_url_returns_json_envelope(api_client):
    response = api_client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response["Content-Type"].startswith("application/json")
    assert _error(response)["code"] == "not_found"


def test_endpoints_are_private_by_default(api_client):
    response = api_client.get("/test/private")
    assert response.status_code in (401, 403)
    assert _error(response)["code"] in ("not_authenticated", "permission_denied")
    assert "leaked" not in response.content.decode()


def test_method_not_allowed(api_client):
    response = api_client.post("/api/v1/health/live", {})
    assert response.status_code == 405
    assert _error(response)["code"] == "method_not_allowed"
