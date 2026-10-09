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


PHASE_3_RESOURCES = [
    "campuses",
    "academic-years",
    "departments",
    "grades",
    "sections",
    "subjects",
    "staff",
    "students",
    "guardians",
    "student-guardians",
    "enrollments",
    "teacher-assignments",
]


def test_phase_3_contract_is_documented(tmp_path):
    out = tmp_path / "openapi.yaml"
    call_command("spectacular", "--file", str(out), "--validate", "--fail-on-warn")
    schema = yaml.safe_load(out.read_text(encoding="utf-8"))
    paths = schema["paths"]
    for resource in PHASE_3_RESOURCES:
        assert f"/api/v1/{resource}" in paths
        assert f"/api/v1/{resource}/{{id}}" in paths
    assert "/api/v1/enrollments/{id}/end" in paths
    assert "/api/v1/enrollments/{id}/transfer" in paths

    listing = paths["/api/v1/sections"]["get"]
    names = {p["name"] for p in listing["parameters"]}
    assert {"X-School-Id", "academic_year_id", "grade_id", "status", "cursor", "page_size"} <= names
    page = listing["responses"]["200"]["content"]["application/json"]["schema"]
    assert page["$ref"].endswith("PaginatedSectionList")
    assert {"400", "401", "403"} <= set(listing["responses"])
    assert {"401", "403", "404"} <= set(paths["/api/v1/students/{id}"]["get"]["responses"])
    assert "delete" not in paths["/api/v1/students/{id}"]


def test_phase_4_invitation_contract_is_documented(tmp_path):
    out = tmp_path / "openapi.yaml"
    call_command("spectacular", "--file", str(out), "--validate", "--fail-on-warn")
    schema = yaml.safe_load(out.read_text(encoding="utf-8"))
    paths = schema["paths"]
    for path in (
        "/api/v1/invitations",
        "/api/v1/invitations/{id}",
        "/api/v1/invitations/{id}/resend",
        "/api/v1/invitations/{id}/revoke",
        "/api/v1/invitations/preview",
        "/api/v1/invitations/verification",
        "/api/v1/invitations/accept",
    ):
        assert path in paths
    invitation = schema["components"]["schemas"]["InvitationOut"]["properties"]
    assert "recipient_hint" in invitation
    assert not {"token", "token_digest", "recipient", "code"} & set(invitation)
    assert (
        "security" not in paths["/api/v1/invitations/preview"]["post"]
        or {} in paths["/api/v1/invitations/preview"]["post"]["security"]
    )


def test_phase_5_academic_engine_contract_is_documented(tmp_path):
    out = tmp_path / "openapi.yaml"
    call_command("spectacular", "--file", str(out), "--validate", "--fail-on-warn")
    schema = yaml.safe_load(out.read_text(encoding="utf-8"))
    paths = schema["paths"]
    for resource in ("terms", "rooms", "timetables", "timetable-periods", "timetable-slots", "lessons"):
        assert f"/api/v1/{resource}" in paths
        assert f"/api/v1/{resource}/{{id}}" in paths
    for action in ("publish", "archive", "copy"):
        assert f"/api/v1/timetables/{{id}}/{action}" in paths
    for path in (
        "/api/v1/schedule/me",
        "/api/v1/staff/{id}/schedule",
        "/api/v1/students/{id}/schedule",
        "/api/v1/sections/{id}/schedule",
    ):
        names = {p["name"] for p in paths[path]["get"]["parameters"]}
        assert {"X-School-Id", "date_from", "date_to"} <= names
    assert "delete" not in paths["/api/v1/lessons/{id}"]
    slot = schema["components"]["schemas"]["SlotOut"]["properties"]
    assert not {"is_live", "effective_from", "start_time", "staff_id"} & set(slot)  # copies stay internal
