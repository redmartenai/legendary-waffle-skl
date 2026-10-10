"""The central approvals queue: aggregation by permission, delegation to the owning module, isolation."""

import uuid

import pytest

from eduflow.attendance.models import AttendanceCorrection, AttendanceRecord
from eduflow.attendance.tests.conftest import YESTERDAY, absent, take
from eduflow.audit.models import AuditEvent

pytestmark = pytest.mark.django_db


@pytest.fixture
def pending(world, class_a, school_day, api_as):
    take(api_as(world.teacher), world.section_a, day=YESTERDAY, entries=absent(class_a.bala))
    record = AttendanceRecord.objects.get(student=class_a.bala)
    response = api_as(world.teacher).post(
        "attendance/corrections",
        {"record_id": str(record.pk), "new_status": "present", "reason": "Late bus"},
        expect=201,
    )
    return response["id"]


def test_the_queue_lists_what_the_caller_may_decide(world, pending, api_as):
    items = api_as(world.principal).get("approvals")
    assert [(i["kind"], i["id"]) for i in items] == [("attendance_correction", pending)]
    assert items[0]["requested_by"] == world.teacher.user.full_name
    assert "Bala" in items[0]["subject"]
    for role_member in (world.teacher, world.parent):
        api_as(role_member).get("approvals", expect=403)


def test_a_decision_is_the_modules_own_decision(world, pending, api_as):
    response = api_as(world.principal).post(
        f"approvals/attendance_correction/{pending}/decision", {"decision": "reject", "note": "No proof"}
    )
    assert response["decision"] == "decline"
    assert AttendanceCorrection.objects.get(pk=pending).status == "declined"
    assert AuditEvent.objects.filter(action="attendance.correction.declined").exists()
    assert api_as(world.principal).get("approvals") == []
    again = api_as(world.principal).post(
        f"approvals/attendance_correction/{pending}/decision", {"decision": "approve"}, expect=409
    )
    assert again["error"]["code"] == "conflict"


def test_module_rules_still_apply_through_the_queue(world, pending, api_as):
    # The requester cannot approve their own correction, whichever endpoint they use.
    from eduflow.authz.models import MembershipRole, Role

    MembershipRole.objects.create(
        school=world.school,
        membership=world.teacher,
        role=Role.objects.get(school=world.school, key="principal"),
    )
    from eduflow.authz.services import bump_rbac_version

    bump_rbac_version(world.school.pk)
    api_as(world.teacher).post(
        f"approvals/attendance_correction/{pending}/decision", {"decision": "approve"}, expect=403
    )


def test_unknown_kinds_and_foreign_ids_are_404(world, other_world, pending, api_as):
    api_as(world.principal).post(
        f"approvals/no_such_kind/{pending}/decision", {"decision": "approve"}, expect=404
    )
    api_as(other_world.principal).post(
        f"approvals/attendance_correction/{pending}/decision", {"decision": "approve"}, expect=404
    )
    api_as(world.principal).post(
        f"approvals/attendance_correction/{uuid.uuid4()}/decision", {"decision": "approve"}, expect=404
    )
    api_as(world.principal).post(
        f"approvals/attendance_correction/{pending}/decision", {"decision": "maybe"}, expect=400
    )
    assert AttendanceCorrection.objects.get(pk=pending).status == "pending"


APPROVAL_MATRIX_PATHS = {
    "/api/v1/approvals/<slug:kind>/{correction}/decision"
}  # covered by the 404 test above
