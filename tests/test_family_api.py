import pytest

from apps.academics.models import Student

from .conftest import PARENT_RAHUL, STUDENT_KABIR, api


def _student(name):
    return Student.objects.get(full_name=name)


@pytest.mark.django_db
def test_parent_sees_only_own_children(parent):
    body = parent.get("/api/v1/parent/children").json()
    assert sorted(c["name"] for c in body["children"]) == ["Aarav Iyer", "Diya Iyer"]


@pytest.mark.django_db
def test_parent_cannot_open_another_family_child(parent, in_ghis):
    kabir = _student("Kabir Sharma")
    assert parent.get(f"/api/v1/students/{kabir.id}/summary").status_code == 404
    assert parent.get(f"/api/v1/students/{kabir.id}/fees").status_code == 404


@pytest.mark.django_db
def test_home_summary_has_everything_for_the_card(parent, in_ghis):
    aarav = _student("Aarav Iyer")
    body = parent.get(f"/api/v1/students/{aarav.id}/summary").json()
    assert body["student"]["class"]["label"] == "Grade 8 · B"
    assert body["attendance"]["today"] == "present"
    assert body["fees"]["total_due"] == "51500.00"
    assert body["bus"]["enrolled"] is True and body["bus"]["pickup_stop"] == "MG Road"
    # The home card matches these ids against the live trip's stops.
    assignment = aarav.transport
    assert body["bus"]["pickup_stop_id"] == str(assignment.pickup_stop_id)
    assert body["bus"]["drop_stop_id"] == str(assignment.drop_stop_id)
    assert body["latest_remark"]["author"] == "Anita Rao"
    assert body["latest_result"]["exam"] == "Term 1"
    assert [p["exam"] for p in body["trend"]] == ["Unit Test 1", "Term 1"]


@pytest.mark.django_db
def test_attendance_month_and_results(parent, in_ghis):
    aarav = _student("Aarav Iyer")
    month = parent.get(f"/api/v1/students/{aarav.id}/attendance").json()
    assert month["summary"]["school_days"] > 0
    results = parent.get(f"/api/v1/students/{aarav.id}/results").json()
    assert results["exams"][0]["subjects"][0]["grade"]
    assert results["exams"][0]["subjects"][0]["class_average"] is not None


@pytest.mark.django_db
def test_student_login_sees_own_record(ghis, in_ghis):
    client = api(STUDENT_KABIR, ghis)
    me = client.get("/api/v1/student/me").json()
    assert me["name"] == "Kabir Sharma"
    assert client.get(f"/api/v1/students/{me['id']}/homework").status_code == 200
    aarav = _student("Aarav Iyer")
    assert client.get(f"/api/v1/students/{aarav.id}/summary").status_code == 404


@pytest.mark.django_db
def test_kabir_parent_sees_overdue_fee(ghis, in_ghis):
    kabir = _student("Kabir Sharma")
    body = api(PARENT_RAHUL, ghis).get(f"/api/v1/students/{kabir.id}/fees").json()
    assert body["overdue"] is True
    assert body["receipts"][0]["receipt_no"].startswith("GHIS/2026-27/")


@pytest.mark.django_db
def test_announcements_and_ack(parent):
    items = parent.get("/api/v1/announcements").json()["items"]
    ptm = next(i for i in items if i["requires_ack"])
    assert ptm["acknowledged"] is False
    assert parent.post(f"/api/v1/announcements/{ptm['id']}/ack").status_code == 200
    again = parent.get("/api/v1/announcements").json()["items"]
    assert next(i for i in again if i["id"] == ptm["id"])["acknowledged"] is True


@pytest.mark.django_db
def test_notification_inbox(parent):
    body = parent.get("/api/v1/notifications").json()
    assert body["unread"] >= 1
    parent.post("/api/v1/notifications/read", {"all": True}, format="json")
    assert parent.get("/api/v1/notifications").json()["unread"] == 0
