"""Fees: plans and instalments, obligations with scholarships, idempotent payments with sequential receipts,
refunds through the approvals queue, statements, defaulters, collections, family access, isolation, RLS."""

import datetime
import uuid
from decimal import Decimal

import pytest

from eduflow.core import db_context
from eduflow.core.db_context import DbContext
from eduflow.fees.models import FeePlan, Payment, StudentFee
from eduflow.notifications.models import Notification

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clock(time_machine):
    time_machine.move_to(datetime.datetime(2026, 9, 20, 6, 0, tzinfo=datetime.UTC))


def _ok(response, expect=200):
    assert response.status_code == expect, response.content
    return response.json() if response.content else None


def _plan(client, world, expect=201, **extra):
    body = {
        "name": "Grade 5 tuition",
        "academic_year_id": str(world.year.pk),
        "grade_id": str(world.grade.pk),
        "instalments": [
            {"label": "Term 1", "due_date": "2026-06-15", "amount": "30000"},
            {"label": "Term 2", "due_date": "2026-09-15", "amount": "30000"},
            {"label": "Term 3", "due_date": "2027-01-15", "amount": "30000"},
        ],
        **extra,
    }
    return _ok(client.post("/api/v1/fee-plans", body, format="json"), expect)


def _pay(client, student, amount, expect=201, **extra):
    body = {"student_id": str(student.pk), "amount": amount, "mode": "upi", **extra}
    return _ok(client.post("/api/v1/fee-payments", body, format="json"), expect)


@pytest.fixture
def billed(world, as_member):
    admin = as_member(world.admin)
    plan = _plan(admin, world)
    assert _ok(
        admin.post(
            f"/api/v1/fee-plans/{plan['id']}/assign", {"section_id": str(world.section_a.pk)}, format="json"
        )
    ) == {"assigned": 1}
    return plan


def test_plans_validate_instalments(world, as_member):
    admin = as_member(world.admin)
    plan = _plan(admin, world)
    assert plan["total"] == "90000.00"
    _plan(admin, world, expect=409)
    bad = _plan(
        admin,
        world,
        name="Other",
        instalments=[{"label": "X", "due_date": "2028-01-01", "amount": "5"}],
        expect=400,
    )
    assert "instalments[0].due_date" in bad["error"]["fields"]
    _plan(
        admin,
        world,
        name="Dup",
        instalments=[{"label": "X", "due_date": "2026-07-01", "amount": "5"}] * 2,
        expect=400,
    )
    updated = _ok(
        admin.patch(
            f"/api/v1/fee-plans/{plan['id']}",
            {"instalments": [{"label": "Year", "due_date": "2026-07-01", "amount": "80000"}]},
            format="json",
        )
    )
    assert updated["total"] == "80000.00"


def test_statement_overdue_and_scholarship(world, billed, as_member):
    admin = as_member(world.admin)
    statement = _ok(admin.get(f"/api/v1/students/{world.student.pk}/fees"))
    assert (statement["total"], statement["due_to_date"], statement["overdue"]) == (
        "90000.00",
        "60000.00",
        "60000.00",
    )
    fee = StudentFee.objects.get(student=world.student)
    _ok(
        admin.patch(
            f"/api/v1/student-fees/{fee.pk}",
            {"scholarship_percent": "25", "scholarship_note": "Merit"},
            format="json",
        )
    )
    _pay(admin, world.student, "20000")
    statement = _ok(as_member(world.parent).get(f"/api/v1/students/{world.student.pk}/fees"))
    assert (
        statement["total"],
        statement["due_to_date"],
        statement["net_paid"],
        statement["overdue"],
        statement["next_due"],
    ) == ("67500.00", "45000.00", "20000.00", "25000.00", "2026-06-15")
    assert as_member(world.parent).get(f"/api/v1/students/{world.other_student.pk}/fees").status_code == 404


def test_payments_get_receipts_and_are_idempotent(world, billed, as_member):
    accountant = as_member(world.admin)
    first = _pay(accountant, world.student, "1000", client_key="k-1")
    assert first["receipt_number"] == "R-2026-000001"
    replay = accountant.post(
        "/api/v1/fee-payments",
        {"student_id": str(world.student.pk), "amount": "1000", "mode": "upi"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="k-1",
    )
    assert (replay.status_code, replay.json()["id"]) == (200, first["id"])
    _pay(accountant, world.student, "999", client_key="k-1", expect=409)
    second = _pay(accountant, world.student, "500", mode="cash")
    assert second["receipt_number"] == "R-2026-000002"
    _pay(accountant, world.student, "5", paid_on="2026-12-01", expect=400)
    assert Payment.objects.count() == 2
    assert Notification.objects.filter(recipient=world.parent, kind="fees").count() == 2
    assert [
        p["receipt_number"] for p in _ok(as_member(world.parent).get("/api/v1/fee-payments"))["results"]
    ] == ["R-2026-000002", "R-2026-000001"]


def test_refunds_are_approved_in_the_queue(world, billed, as_member):
    admin = as_member(world.admin)
    payment = _pay(admin, world.student, "10000")
    _ok(
        admin.post(
            f"/api/v1/fee-payments/{payment['id']}/refunds",
            {"amount": "10000.01", "reason": "x"},
            format="json",
        ),
        400,
    )
    refund = _ok(
        admin.post(
            f"/api/v1/fee-payments/{payment['id']}/refunds",
            {"amount": "4000", "reason": "Overpaid"},
            format="json",
        ),
        201,
    )
    _ok(
        admin.post(
            f"/api/v1/fee-payments/{payment['id']}/refunds",
            {"amount": "6000.01", "reason": "x"},
            format="json",
        ),
        400,
    )
    queue = _ok(as_member(world.principal).get("/api/v1/approvals"))
    assert [(i["kind"], i["amount"]) for i in queue] == [("refund", "4000.00")]
    _ok(
        as_member(world.principal).post(
            f"/api/v1/approvals/refund/{refund['id']}/decision", {"decision": "approve"}, format="json"
        )
    )
    statement = _ok(admin.get(f"/api/v1/students/{world.student.pk}/fees"))
    assert (statement["refunded"], statement["net_paid"]) == ("4000.00", "6000.00")
    collections = _ok(admin.get("/api/v1/fees/collections?from=2026-09-01&to=2026-09-30"))
    assert (collections["received"], collections["refunded"], collections["net"], collections["by_mode"]) == (
        "10000.00",
        "4000.00",
        "6000.00",
        {"upi": "10000.00"},
    )


def test_defaulters(world, billed, as_member):
    admin = as_member(world.admin)
    rows = _ok(admin.get("/api/v1/fees/defaulters"))
    assert [(r["student"]["full_name"], r["overdue"]) for r in rows] == [("Asha", "60000.00")]
    assert _ok(admin.get("/api/v1/fees/defaulters?min_overdue=70000")) == []
    _pay(admin, world.student, "60000")
    assert _ok(admin.get(f"/api/v1/fees/defaulters?section_id={world.section_a.pk}")) == []
    assert as_member(world.parent).get("/api/v1/fees/defaulters").status_code == 403


def test_only_the_office_records_money(world, billed, as_member):
    for member in ("teacher", "parent", "student_member"):
        _pay(as_member(getattr(world, member)), world.student, "1", expect=403)
    assert as_member(world.parent).post("/api/v1/fee-plans", {}, format="json").status_code == 403


FEE_MATRIX = [
    ("get", "/api/v1/fee-plans/{plan}", None),
    ("patch", "/api/v1/fee-plans/{plan}", {"name": "x"}),
    ("post", "/api/v1/fee-plans/{plan}/assign", {"section_id": "{section}"}),
    ("get", "/api/v1/student-fees/{fee}", None),
    ("patch", "/api/v1/student-fees/{fee}", {"scholarship_percent": "10"}),
    ("get", "/api/v1/fee-payments/{payment}", None),
    ("get", "/api/v1/fee-payments/{payment}/refunds", None),
    ("post", "/api/v1/fee-payments/{payment}/refunds", {"amount": "1", "reason": "x"}),
    ("get", "/api/v1/students/{student}/fees", None),
]
FEE_MATRIX_PATHS = {p for _, p, _ in FEE_MATRIX}


@pytest.mark.parametrize(("method", "path", "body"), FEE_MATRIX)
def test_another_schools_fees_answer_like_unknown(world, other_world, as_member, method, path, body):
    admin_b = as_member(other_world.admin)
    plan = _plan(admin_b, other_world)
    admin_b.post(
        f"/api/v1/fee-plans/{plan['id']}/assign", {"section_id": str(other_world.section_a.pk)}, format="json"
    )
    payment = _pay(admin_b, other_world.student, "100")
    real = {
        "plan": plan["id"],
        "fee": StudentFee.objects.get().pk,
        "payment": payment["id"],
        "student": other_world.student.pk,
        "section": world.section_a.pk,
    }
    missing = {**real, **{k: uuid.uuid4() for k in ("plan", "fee", "payment", "student")}}
    client = as_member(world.admin)

    def call(values):
        url = path.format(**values)
        payload = {k: v.format(**values) for k, v in body.items()} if body else None
        return (
            getattr(client, method)(url, payload, format="json") if payload else getattr(client, method)(url)
        )

    assert call(real).status_code == call(missing).status_code == 404
    assert FeePlan.objects.get(pk=plan["id"]).name == "Grade 5 tuition"


def test_fees_are_under_rls(world, other_world, as_member):
    _pay(as_member(other_world.admin), other_world.student, "100")
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        assert not Payment.objects.exists()
    assert Decimal("100") == Payment.objects.get().amount
