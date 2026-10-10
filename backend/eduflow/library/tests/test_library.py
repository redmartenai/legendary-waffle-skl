"""Library: catalogue, issue and return with the school's settings, fines only with a rate, renewals, loan
limits, family visibility, the daily overdue reminder job, isolation and RLS."""

import datetime
import uuid
from decimal import Decimal

import pytest

from eduflow.core import db_context
from eduflow.core.db_context import DbContext
from eduflow.library import services
from eduflow.library.models import Book, Copy, Loan
from eduflow.notifications.models import Notification
from eduflow.tenancy import jobs

pytestmark = pytest.mark.django_db

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def _ok(response, expect=200):
    assert response.status_code == expect, response.content
    return response.json() if response.content else None


def _day(time_machine, day):
    time_machine.move_to(datetime.datetime(2026, 7, day, 10, 0, tzinfo=IST), tick=False)


@pytest.fixture
def book(world, as_member):
    body = {
        "title": "Wings of Fire",
        "authors": "A. P. J. Abdul Kalam",
        "copies": [{"accession_number": "A-1"}, {"accession_number": "A-2"}],
    }
    return _ok(as_member(world.admin).post("/api/v1/library/books", body, format="json"), 201)


def _issue(client, copy_id, expect=201, **extra):
    return _ok(client.post("/api/v1/library/loans", {"copy_id": copy_id, **extra}, format="json"), expect)


def test_catalogue_is_visible_to_every_reader(world, book, as_member):
    assert book["available"] == 2
    for member in ("student_member", "parent", "teacher"):
        found = _ok(as_member(getattr(world, member)).get("/api/v1/library/books?q=wings"))["results"]
        assert [b["title"] for b in found] == ["Wings of Fire"], member
    _ok(
        as_member(world.admin).post(
            f"/api/v1/library/books/{book['id']}/copies", {"accession_number": "A-1"}, format="json"
        ),
        409,
    )
    _ok(as_member(world.student_member).post("/api/v1/library/books", {"title": "x"}, format="json"), 403)


def test_issue_return_and_fines(world, book, as_member, time_machine):
    _day(time_machine, 1)
    admin = as_member(world.admin)
    copy = book["copies"][0]["id"]
    _issue(admin, copy, student_id=str(world.student.pk), expect=400)  # no loan period: a due date is needed
    _ok(admin.put("/api/v1/library/settings", {"loan_days": 14}, format="json"))
    loan = _issue(admin, copy, student_id=str(world.student.pk))
    assert loan["due_on"] == "2026-07-15"
    _issue(admin, copy, student_id=str(world.other_student.pk), expect=409)  # on loan
    _issue(
        admin,
        book["copies"][1]["id"],
        student_id=str(world.student.pk),
        staff_id=str(world.teacher_staff.pk),
        expect=400,
    )
    _day(time_machine, 20)
    admin = as_member(world.admin)  # tokens are short-lived: sign in again after moving the clock
    assert _ok(admin.get("/api/v1/library/loans?overdue=true"))["results"][0]["id"] == loan["id"]
    returned = _ok(admin.post(f"/api/v1/library/loans/{loan['id']}/return", {}, format="json"))
    assert (returned["returned_on"], returned["fine"]) == ("2026-07-20", "0.00")  # no fine rate set
    _ok(admin.post(f"/api/v1/library/loans/{loan['id']}/fine-paid"), 409)
    _ok(admin.put("/api/v1/library/settings", {"loan_days": 14, "fine_per_day": "2.50"}, format="json"))
    late = _issue(admin, copy, staff_id=str(world.teacher_staff.pk), due_on="2026-07-21")
    _day(time_machine, 25)
    admin = as_member(world.admin)
    assert _ok(admin.post(f"/api/v1/library/loans/{late['id']}/return", {}, format="json"))["fine"] == "10.00"
    assert _ok(admin.post(f"/api/v1/library/loans/{late['id']}/fine-paid"))["fine_paid"] is True
    lost = _issue(admin, copy, student_id=str(world.student.pk))
    _ok(admin.post(f"/api/v1/library/loans/{lost['id']}/return", {"lost": True}, format="json"))
    assert Copy.objects.get(pk=copy).status == "lost"


def test_renewals_and_limits(world, book, as_member, time_machine):
    _day(time_machine, 1)
    admin = as_member(world.admin)
    _ok(admin.put("/api/v1/library/settings", {"loan_days": 7, "max_loans": 1}, format="json"))
    loan = _issue(admin, book["copies"][0]["id"], student_id=str(world.student.pk))
    _issue(admin, book["copies"][1]["id"], student_id=str(world.student.pk), expect=409)
    _ok(admin.post(f"/api/v1/library/loans/{loan['id']}/renew", {"due_on": "2026-07-05"}, format="json"), 400)
    renewed = _ok(
        admin.post(f"/api/v1/library/loans/{loan['id']}/renew", {"due_on": "2026-07-20"}, format="json")
    )
    assert (renewed["due_on"], renewed["renewals"]) == ("2026-07-20", 1)


def test_families_see_their_own_loans(world, book, as_member, time_machine):
    _day(time_machine, 1)
    admin = as_member(world.admin)
    _issue(admin, book["copies"][0]["id"], student_id=str(world.student.pk), due_on="2026-07-10")
    _issue(admin, book["copies"][1]["id"], student_id=str(world.other_student.pk), due_on="2026-07-10")
    for member in ("parent", "student_member"):
        loans = _ok(as_member(getattr(world, member)).get("/api/v1/library/loans"))["results"]
        assert [loan["student"]["full_name"] for loan in loans] == ["Asha"], member
    _day(time_machine, 12)
    results = jobs.run("daily", world.school)
    assert results["library.overdue_reminders"] == "2"
    assert Notification.objects.filter(
        recipient=world.parent, title__startswith="Library book overdue"
    ).exists()


def test_fine_calculation_needs_a_rate(world):
    loan = Loan(school=world.school, due_on=datetime.date(2026, 7, 1))
    assert services.fine_for(loan, datetime.date(2026, 7, 5)) == Decimal(0)


LIBRARY_MATRIX = [
    ("get", "/api/v1/library/books/{book}", None),
    ("patch", "/api/v1/library/books/{book}", {"title": "x"}),
    ("post", "/api/v1/library/books/{book}/copies", {"accession_number": "Z-1"}),
    ("post", "/api/v1/library/copies/{copy}/status", {"status": "withdrawn"}),
    ("get", "/api/v1/library/loans/{loan}", None),
    ("post", "/api/v1/library/loans/{loan}/return", {"lost": False}),
    ("post", "/api/v1/library/loans/{loan}/renew", {"due_on": "2027-01-01"}),
    ("post", "/api/v1/library/loans/{loan}/fine-paid", None),
]
LIBRARY_MATRIX_PATHS = {p for _, p, _ in LIBRARY_MATRIX}


@pytest.mark.parametrize(("method", "path", "body"), LIBRARY_MATRIX)
def test_another_schools_library_answers_like_unknown(world, other_world, as_member, method, path, body):
    b = Book.objects.create(school=other_world.school, title="Foreign")
    c = Copy.objects.create(school=other_world.school, book=b, accession_number="F-1")
    loan = Loan.objects.create(
        school=other_world.school,
        copy=c,
        student=other_world.student,
        issued_on="2026-07-01",
        due_on="2026-07-10",
    )
    real = {"book": b.pk, "copy": c.pk, "loan": loan.pk}
    missing = {k: uuid.uuid4() for k in real}
    client = as_member(world.admin)

    def call(values):
        url = path.format(**values)
        return getattr(client, method)(url, body, format="json") if body else getattr(client, method)(url)

    assert call(real).status_code == call(missing).status_code == 404
    assert Loan.objects.get(pk=loan.pk).returned_on is None


def test_library_is_under_rls(world, other_world):
    Book.objects.create(school=other_world.school, title="Foreign")
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        assert not Book.objects.exists()
