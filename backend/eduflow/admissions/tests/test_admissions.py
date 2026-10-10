"""Admissions: pipeline, offer approval in the central queue, enrolment through the people services, online
applications, documents, permissions, isolation and RLS."""

import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from eduflow.admissions.models import Application, StageChange
from eduflow.audit.models import AuditEvent
from eduflow.core import db_context
from eduflow.core.db_context import DbContext
from eduflow.people.models import Enrollment, Guardian, Student, StudentGuardian

pytestmark = pytest.mark.django_db


def _create(client, world, expect=201, **extra):
    body = {
        "child_name": "Ishaan Rao",
        "grade_id": str(world.grade.pk),
        "parent_name": "Neha Rao",
        "phone": "+919811122233",
        "source": "walk_in",
        "fee_quoted": "72000.00",
        **extra,
    }
    response = client.post("/api/v1/admissions", body, format="json")
    assert response.status_code == expect, response.content
    return response.json()


def _post(client, path, body=None, expect=200):
    response = client.post(f"/api/v1/{path}", body or {}, format="json")
    assert response.status_code == expect, response.content
    return response.json()


@pytest.fixture
def offer(world, as_member):
    app = _create(as_member(world.admin), world)
    _post(
        as_member(world.admin),
        f"admissions/{app['id']}/move",
        {"stage": "offer", "note": "Assessment passed"},
    )
    return app["id"]


def test_the_pipeline_moves_forwards_and_keeps_history(world, as_member):
    admin = as_member(world.admin)
    app = _create(admin, world)
    assert (app["stage"], app["fee_quoted"]) == ("enquiry", "72000.00")
    _post(admin, f"admissions/{app['id']}/move", {"stage": "assessment"})  # a visit may be skipped
    back = _post(admin, f"admissions/{app['id']}/move", {"stage": "visit"}, expect=400)
    assert "stage" in back["error"]["fields"]
    _post(admin, f"admissions/{app['id']}/move", {"stage": "enrolled"}, expect=400)
    dropped = _post(admin, f"admissions/{app['id']}/move", {"stage": "dropped", "note": "Moved city"})
    assert [h["to_stage"] for h in dropped["history"]] == ["enquiry", "assessment", "dropped"]
    _post(admin, f"admissions/{app['id']}/move", {"stage": "offer"}, expect=409)
    assert AuditEvent.objects.filter(action="admissions.application.moved").count() == 2


def test_validation(world, other_world, as_member):
    admin = as_member(world.admin)
    assert "phone" in _create(admin, world, phone="", expect=400)["error"]["fields"]
    assert (
        "grade_id" in _create(admin, world, grade_id=str(other_world.grade.pk), expect=400)["error"]["fields"]
    )
    assert "fee_quoted" in _create(admin, world, fee_quoted="-1", expect=400)["error"]["fields"]
    assert "source" in _create(admin, world, source="tv", expect=400)["error"]["fields"]


def test_an_offer_is_decided_in_the_approvals_queue(world, offer, as_member):
    principal = as_member(world.principal)
    queue = principal.get("/api/v1/approvals").json()
    assert [(i["kind"], i["id"], i["amount"]) for i in queue] == [("admission", offer, "72000.00")]
    _post(principal, f"approvals/admission/{offer}/decision", {"decision": "approve"})
    assert Application.objects.get(pk=offer).offer_decision == "approved"
    assert principal.get("/api/v1/approvals").json() == []
    _post(principal, f"approvals/admission/{offer}/decision", {"decision": "approve"}, expect=409)


def test_a_declined_offer_drops_the_application(world, offer, as_member):
    _post(
        as_member(world.principal),
        f"approvals/admission/{offer}/decision",
        {"decision": "decline", "note": "Full"},
    )
    assert Application.objects.get(pk=offer).stage == "dropped"


def test_enrolment_creates_the_student_through_the_people_services(world, offer, as_member):
    admin = as_member(world.admin)
    _post(
        admin,
        f"admissions/{offer}/enrol",
        {"section_id": str(world.section_b.pk), "admission_number": "S-9"},
        expect=409,
    )
    _post(as_member(world.principal), f"approvals/admission/{offer}/decision", {"decision": "approve"})
    enrolled = _post(
        admin, f"admissions/{offer}/enrol", {"section_id": str(world.section_b.pk), "admission_number": "S-9"}
    )
    assert enrolled["stage"] == "enrolled"
    student = Student.objects.get(pk=enrolled["student_id"])
    assert (student.first_name, student.last_name, student.admission_number) == ("Ishaan", "Rao", "S-9")
    guardian = Guardian.objects.get(full_name="Neha Rao")
    assert StudentGuardian.objects.filter(student=student, guardian=guardian, is_primary=True).exists()
    assert Enrollment.objects.get(student=student).section_id == world.section_b.pk
    assert AuditEvent.objects.filter(action="people.student.created", target_id=str(student.pk)).exists()
    _post(
        admin,
        f"admissions/{offer}/enrol",
        {"section_id": str(world.section_b.pk), "admission_number": "S-10"},
        expect=409,
    )


def test_enrolment_checks_the_section_and_the_admission_number(world, offer, as_member, make_member):
    from eduflow.academics.models import Grade, Section

    _post(as_member(world.principal), f"approvals/admission/{offer}/decision", {"decision": "approve"})
    grade6 = Grade.objects.create(school=world.school, name="Grade 6", code="g6")
    other = Section.objects.create(
        school=world.school, academic_year=world.year, grade=grade6, name="A", code="6a"
    )
    admin = as_member(world.admin)
    assert (
        "section_id"
        in _post(
            admin,
            f"admissions/{offer}/enrol",
            {"section_id": str(other.pk), "admission_number": "S-9"},
            expect=400,
        )["error"]["fields"]
    )
    _post(
        admin,
        f"admissions/{offer}/enrol",
        {"section_id": str(world.section_b.pk), "admission_number": "S-1"},
        expect=409,
    )  # taken
    assert Application.objects.get(pk=offer).stage == "offer"  # nothing half-done
    assert not Guardian.objects.filter(full_name="Neha Rao").exists()


def test_online_applications_create_enquiries_only(world, api_client, settings):
    body = {
        "school_code": world.school.code,
        "child_name": "Online Child",
        "grade_id": str(world.grade.pk),
        "parent_name": "Online Parent",
        "email": "online@example.test",
        "message": "We are moving to the city.",
    }
    response = api_client.post("/api/v1/admissions/apply", body, format="json")
    assert response.status_code == 201
    app = Application.objects.get(pk=response.json()["reference"])
    assert (app.stage, app.source, app.notes) == ("enquiry", "website", "We are moving to the city.")
    assert set(response.json()) == {"reference", "message"}
    assert (
        api_client.post(
            "/api/v1/admissions/apply", {**body, "school_code": "nope"}, format="json"
        ).status_code
        == 404
    )
    assert (
        api_client.post("/api/v1/admissions/apply", {**body, "stage": "offer"}, format="json").status_code
        == 400
    )
    from django.core.cache import cache

    cache.clear()
    settings.RATE_LIMITS = {**settings.RATE_LIMITS, "admission_apply_ip": "1/h"}
    codes = [api_client.post("/api/v1/admissions/apply", body, format="json").status_code for _ in range(2)]
    assert codes == [201, 429]


def test_documents_are_attached_and_downloaded_by_the_office(world, as_member):
    admin = as_member(world.admin)
    app = _create(admin, world)
    pdf = SimpleUploadedFile("tc.pdf", b"%PDF-1.4 transfer certificate")
    doc = admin.post(
        f"/api/v1/admissions/{app['id']}/documents", {"title": "TC", "file": pdf}, format="multipart"
    ).json()
    assert admin.get(f"/api/v1/admissions/{app['id']}/documents").json()[0]["filename"] == "tc.pdf"
    downloaded = admin.get(f"/api/v1/admissions/{app['id']}/documents/{doc['id']}")
    assert b"".join(downloaded) == b"%PDF-1.4 transfer certificate"
    assert (
        as_member(world.teacher).get(f"/api/v1/admissions/{app['id']}/documents/{doc['id']}").status_code
        == 403
    )


@pytest.mark.parametrize("member", ["teacher", "parent", "student_member"])
def test_only_the_office_handles_admissions(world, as_member, member):
    client = as_member(getattr(world, member))
    assert client.get("/api/v1/admissions").status_code == 403
    _create(client, world, expect=403)


ADMISSION_MATRIX = [
    ("get", "/api/v1/admissions/{application}", None),
    ("patch", "/api/v1/admissions/{application}", {"notes": "x"}),
    ("post", "/api/v1/admissions/{application}/move", {"stage": "dropped"}),
    ("post", "/api/v1/admissions/{application}/enrol", {"section_id": "{section}", "admission_number": "Z"}),
    ("get", "/api/v1/admissions/{application}/documents", None),
    ("get", "/api/v1/admissions/{application}/documents/{document}", None),
]
ADMISSION_MATRIX_PATHS = {p for _, p, _ in ADMISSION_MATRIX}


@pytest.mark.parametrize(("method", "path", "body"), ADMISSION_MATRIX)
def test_another_schools_application_answers_like_an_unknown_one(
    world, other_world, as_member, method, path, body
):
    foreign = _create(as_member(other_world.admin), other_world)["id"]
    ids = {"application": foreign, "section": world.section_b.pk, "document": uuid.uuid4()}
    missing = {**ids, "application": uuid.uuid4()}
    client = as_member(world.admin)

    def call(values):
        url = path.format(**values)
        payload = {k: (v.format(**values) if isinstance(v, str) else v) for k, v in (body or {}).items()}
        return getattr(client, method)(url, payload, format="json") if body else getattr(client, method)(url)

    assert call(ids).status_code == call(missing).status_code == 404
    assert Application.objects.get(pk=foreign).stage == "enquiry"


def test_admissions_are_under_rls(world, other_world, as_member):
    _create(as_member(other_world.admin), other_world)
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        assert not Application.objects.exists()
        assert not StageChange.objects.exists()
