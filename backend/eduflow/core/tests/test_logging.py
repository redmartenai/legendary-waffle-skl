import pytest

from eduflow.core.logging import REDACTED, get_logger, redact
from eduflow.core.request_context import normalize_request_id

pytestmark = pytest.mark.django_db


# ----------------------------------------------------------------------------- request IDs
def test_request_id_generated_and_echoed(api_client):
    response = api_client.get("/api/v1/health/live")
    request_id = response["X-Request-ID"]
    assert len(request_id) == 32
    assert int(request_id, 16) >= 0  # uuid4 hex


def test_valid_client_request_id_is_kept(api_client):
    response = api_client.get("/api/v1/health/live", HTTP_X_REQUEST_ID="mobile-7f3a9c21.attempt-2")
    assert response["X-Request-ID"] == "mobile-7f3a9c21.attempt-2"


@pytest.mark.parametrize(
    "bad", ["short", "has space here", "x" * 129, "line\nbreak-injection", "<script>alert(1)"]
)
def test_unsafe_client_request_id_is_replaced(bad):
    assert normalize_request_id(bad) != bad


def test_access_log_line_carries_request_id(api_client, json_logs):
    response = api_client.get("/api/v1/health/live?phone=9876543210", HTTP_X_REQUEST_ID="trace-abc-123456")
    [line] = json_logs.find("http_request")
    assert line["request_id"] == "trace-abc-123456" == response["X-Request-ID"]
    assert line["method"] == "GET"
    assert line["path"] == "/api/v1/health/live"
    assert line["status"] == 200
    assert isinstance(line["duration_ms"], float)
    assert line["level"] == "info"
    assert "timestamp" in line
    # The query string (which can carry personal data) is not logged.
    assert "9876543210" not in json_logs.text


def test_request_id_is_cleared_after_request(api_client, json_logs):
    api_client.get("/api/v1/health/live", HTTP_X_REQUEST_ID="first-request-0001")
    get_logger("test").info("outside_request")
    [line] = json_logs.find("outside_request")
    assert "request_id" not in line


# ----------------------------------------------------------------------------- redaction
def test_redact_masks_sensitive_keys_recursively():
    data = {
        "password": "hunter2",
        "user": {"phone": "+911234567890", "Authorization": "Bearer abc.def.ghi"},
        "headers": [{"X-Api-Key": "k-123"}, {"cookie": "sessionid=xyz"}],
        "refresh": "r-token",
        "access": "a-token",
        "code": "123456",
        "accessed_at": "2026-10-08",
        "client_secret": "cs",
        "tokens": ["t1", "t2"],
    }
    out = redact(data)
    assert out["password"] == REDACTED
    assert out["user"]["Authorization"] == REDACTED
    assert out["user"]["phone"] == "+911234567890"  # not a secret, but personal data is not logged by policy
    assert out["headers"][0]["X-Api-Key"] == REDACTED
    assert out["headers"][1]["cookie"] == REDACTED
    assert out["refresh"] == out["access"] == out["code"] == out["client_secret"] == REDACTED
    assert out["tokens"] == REDACTED
    assert out["accessed_at"] == "2026-10-08"  # "access" only matches exactly
    assert data["password"] == "hunter2"  # input not mutated


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("auth header was Bearer eyJabc.def.ghi", "eyJabc"),
        ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig in body", "eyJhbGciOiJIUzI1NiJ9"),
        ("connect redis://default:s3cr3tpw@redis:6379/0 failed", "s3cr3tpw"),
        ("login failed password=hunter2 for user", "hunter2"),
        ("otp: 482913 sent", "482913"),
        ("Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ],
)
def test_redact_masks_secrets_inside_strings(raw, secret):
    assert secret not in redact(raw)


def test_secrets_never_reach_rendered_logs(json_logs):
    log = get_logger("test")
    log.info(
        "login_attempt",
        password="hunter2",
        refresh="eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln",
        payload={"otp": "482913", "identifier": "teacher@example.com"},
        note="client sent Authorization: Bearer abcdef123456",
    )
    # stdlib loggers (Django, Celery, libraries) go through the same pipeline.
    import logging

    logging.getLogger("third.party").warning("db url postgres://app:pgpass99@db/eduflow")

    text = json_logs.text
    for secret in ("hunter2", "eyJhbGciOiJIUzI1NiJ9", "482913", "abcdef123456", "pgpass99"):
        assert secret not in text, secret
    [entry] = json_logs.find("login_attempt")
    assert entry["password"] == REDACTED
    assert entry["payload"]["identifier"] == "teacher@example.com"
