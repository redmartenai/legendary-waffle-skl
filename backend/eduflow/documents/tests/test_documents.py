"""Documents: content-checked uploads, audience and scope rules, audited downloads, isolation, RLS."""

import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from eduflow.audit.models import AuditEvent
from eduflow.core import db_context
from eduflow.core.db_context import DbContext
from eduflow.documents import files
from eduflow.documents.models import Document

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


def _upload(client, *, data=PDF, name="report.pdf", expect=201, **fields):
    body = {
        "file": SimpleUploadedFile(name, data),
        "title": "Report card",
        "audiences": ["parents"],
        **fields,
    }
    response = client.post("/api/v1/documents", body, format="multipart")
    assert response.status_code == expect, response.content
    return response.json()


def _ids(client, path="/api/v1/documents"):
    return {d["id"] for d in client.get(path).json()["results"]}


@pytest.mark.parametrize(
    ("data", "name"),
    [
        (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "x.svg"),
        (b"<html><script>alert(1)</script></html>", "x.pdf"),
        (b"MZ\x90\x00" + b"\x00" * 64, "setup.exe"),
        (b"PK\x03\x04garbage", "x.docx"),
        (b"", "empty.pdf"),
    ],
)
def test_uploads_are_identified_by_content(world, as_member, data, name):
    response = _upload(as_member(world.admin), data=data, name=name, expect=400)
    assert "file" in response["error"]["fields"]
    assert not Document.objects.exists()


def test_types_and_size_limit():
    assert files.identify(PDF)[0] == "application/pdf"
    assert files.identify(b"Notes for the PTM")[0].startswith("text/plain")
    upload = SimpleUploadedFile("big.pdf", b"%PDF-" + b"0" * files.MAX_BYTES)
    with pytest.raises(Exception, match="10 MB"):
        files.store(uuid.uuid4(), upload)


def test_audiences_and_scopes_decide_who_sees_what(world, as_member):
    admin = as_member(world.admin)
    for_parents = _upload(admin, student_id=str(world.student.pk), audiences=["parents"])["id"]
    for_teachers = _upload(admin, student_id=str(world.student.pk), audiences=["teachers"])["id"]
    other_child = _upload(admin, student_id=str(world.other_student.pk), audiences=["parents", "teachers"])[
        "id"
    ]
    circular = _upload(admin, audiences=["parents", "students"])["id"]
    staff_only = _upload(admin, audiences=["staff"])["id"]
    assert _ids(as_member(world.parent)) == {for_parents, circular}
    assert _ids(as_member(world.teacher)) == {for_teachers, staff_only}  # B's student is not theirs
    assert _ids(as_member(world.student_member)) == {circular}
    assert _ids(admin) >= {for_parents, for_teachers, other_child, circular, staff_only}
    assert as_member(world.parent).get(f"/api/v1/documents/{other_child}").status_code == 404


def test_downloads_are_scoped_and_audited(world, as_member):
    doc = _upload(as_member(world.admin), student_id=str(world.student.pk), audiences=["parents"])
    response = as_member(world.parent).get(f"/api/v1/documents/{doc['id']}/download")
    assert response.status_code == 200
    assert b"".join(response) == PDF
    assert response["Content-Disposition"].startswith("attachment;")
    assert "sandbox" in response["Content-Security-Policy"]
    assert AuditEvent.objects.filter(action="documents.document.downloaded", target_id=doc["id"]).exists()
    assert as_member(world.teacher).get(f"/api/v1/documents/{doc['id']}/download").status_code == 404
    assert "storage_key" not in str(as_member(world.admin).get(f"/api/v1/documents/{doc['id']}").json())


def test_teachers_upload_only_about_students_they_teach(world, as_member):
    teacher = as_member(world.teacher)
    _upload(teacher, student_id=str(world.student.pk), audiences=["parents", "class_teacher"])
    _upload(teacher, student_id=str(world.other_student.pk), expect=404)  # not in scope
    _upload(teacher, audiences=["parents"], expect=403)  # school-wide documents need school scope
    _upload(teacher, student_id=str(world.student.pk), audiences=["accountant"], expect=400)
    _upload(as_member(world.parent), expect=403)


def test_archiving_hides_documents_and_needs_school_scope(world, as_member):
    doc = _upload(as_member(world.admin), audiences=["parents"])
    assert as_member(world.teacher).delete(f"/api/v1/documents/{doc['id']}").status_code == 403
    assert as_member(world.admin).delete(f"/api/v1/documents/{doc['id']}").status_code == 204
    assert doc["id"] not in _ids(as_member(world.parent))
    assert as_member(world.parent).get(f"/api/v1/documents/{doc['id']}/download").status_code == 404


DOCUMENT_MATRIX = [
    ("get", "/api/v1/documents/{document}"),
    ("delete", "/api/v1/documents/{document}"),
    ("get", "/api/v1/documents/{document}/download"),
]
DOCUMENT_MATRIX_PATHS = {p for _, p in DOCUMENT_MATRIX}


@pytest.mark.parametrize(("method", "path"), DOCUMENT_MATRIX)
def test_another_schools_document_answers_like_an_unknown_one(world, other_world, as_member, method, path):
    foreign = _upload(as_member(other_world.admin), audiences=["parents"])["id"]
    client = as_member(world.admin)
    theirs = getattr(client, method)(path.format(document=foreign))
    missing = getattr(client, method)(path.format(document=uuid.uuid4()))
    assert theirs.status_code == missing.status_code == 404
    assert Document.objects.get(pk=foreign).archived_at is None


def test_documents_are_under_rls(world, other_world, as_member):
    _upload(as_member(other_world.admin), audiences=["parents"])
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        assert not Document.objects.exists()
