import pytest
import yaml
from django.core.management import call_command


def test_schema_generates_and_validates(tmp_path):
    out = tmp_path / "openapi.yaml"
    call_command("spectacular", "--file", str(out), "--validate", "--fail-on-warn")
    schema = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert schema["openapi"].startswith("3.1")
    assert "/api/v1/health/live" in schema["paths"]
    assert "/api/v1/health/ready" in schema["paths"]


@pytest.mark.django_db
def test_schema_endpoint_served_when_docs_enabled(api_client):
    # API_DOCS_ENABLED is on in test settings and off by default in production (enforced).
    response = api_client.get("/api/v1/schema", HTTP_ACCEPT="application/vnd.oai.openapi+json")
    assert response.status_code == 200


PHASE_2_PATHS = [
    "/api/v1/auth/password/login",
    "/api/v1/auth/password/change",
    "/api/v1/auth/otp/request",
    "/api/v1/auth/otp/verify",
    "/api/v1/auth/token/refresh",
    "/api/v1/auth/logout",
    "/api/v1/auth/logout-all",
    "/api/v1/auth/sessions",
    "/api/v1/auth/sessions/{session_id}",
    "/api/v1/me",
    "/api/v1/me/permissions",
    "/api/v1/schools/lookup",
    "/api/v1/school",
    "/api/v1/memberships",
    "/api/v1/memberships/{membership_id}",
    "/api/v1/memberships/{membership_id}/roles",
    "/api/v1/memberships/{membership_id}/roles/{role_id}",
    "/api/v1/roles",
    "/api/v1/roles/{role_id}",
    "/api/v1/permissions",
    "/api/v1/audit-events",
    "/api/v1/platform/schools",
    "/api/v1/platform/schools/{school_id}",
    "/api/v1/platform/users/{user_id}",
]


def test_phase_2_contract_is_documented(tmp_path):
    out = tmp_path / "openapi.yaml"
    call_command("spectacular", "--file", str(out), "--validate", "--fail-on-warn")
    schema = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert set(PHASE_2_PATHS) <= set(schema["paths"])
    assert schema["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"

    login = schema["paths"]["/api/v1/auth/password/login"]["post"]
    assert login.get("security") in ([], [{}], None) or {} in login["security"]
    assert {"200", "401", "429"} <= set(login["responses"])

    roles = schema["paths"]["/api/v1/roles"]["get"]
    assert {"bearerAuth": []} in roles["security"]
    assert any(p["name"] == "X-School-Id" and p["in"] == "header" for p in roles["parameters"])
    assert {"401", "403"} <= set(roles["responses"])
