"""Web console frame and dashboard: context, ⌘K search, the dashboard aggregate."""

import pytest

from apps.academics.models import Student

from .conftest import PARENT_MEERA, PRINCIPAL, TEACHER_ANITA, api


@pytest.mark.django_db
def test_context_names_the_term_and_counts_the_tray(in_ghis):
    ctx = api(PRINCIPAL).get("/api/v1/console/context")
    assert ctx.status_code == 200
    body = ctx.json()
    assert {"today", "term", "approvals", "unread_notifications", "academic_year"} <= set(body)
    assert api(TEACHER_ANITA).get("/api/v1/console/context").status_code == 403
    assert api(PARENT_MEERA).get("/api/v1/console/context").status_code == 403


@pytest.mark.django_db
def test_search_finds_students_and_needs_two_letters(in_ghis):
    student = Student.objects.filter(is_active=True).first()
    principal = api(PRINCIPAL)
    assert principal.get("/api/v1/console/search", {"q": "a"}).json()["items"] == []
    hits = principal.get("/api/v1/console/search", {"q": student.full_name[:5]}).json()["items"]
    assert any(h["kind"] == "student" and h["id"] == str(student.id) for h in hits)
    assert api(TEACHER_ANITA).get("/api/v1/console/search", {"q": "ab"}).status_code == 403


@pytest.mark.django_db
def test_dashboard_aggregate(in_ghis):
    res = api(PRINCIPAL).get("/api/v1/console/dashboard")
    assert res.status_code == 200
    body = res.json()
    assert {"register", "cover", "exams", "intray", "buses", "fees", "coming_up"} <= set(body)
    reg = body["register"]
    assert reg["total"] == Student.objects.filter(is_active=True).count()
    # Every marked section appears once in the grade × letter grid.
    assert len(reg["cells"]) == reg["sections"]
    assert float(body["fees"]["collected"]) <= float(body["fees"]["billed"])
    assert api(TEACHER_ANITA).get("/api/v1/console/dashboard").status_code == 403
