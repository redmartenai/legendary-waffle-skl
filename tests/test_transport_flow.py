"""End-to-end live tracking through the API: driver app -> ingestion -> alerts -> parent view.

A fake clock replays a trip as it happens in real life: each batch of GPS fixes
arrives just after it was recorded, exactly like the driver app sends them.
"""

import time
from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from apps.academics.models import Student
from apps.notifications.models import Notification
from apps.transport import services
from apps.transport.models import Route, Trip, TripViewLog

from .conftest import PARENT_MEERA, PARENT_RAHUL, SPS_PARENT, api, user


class FakeClock:
    def __init__(self):
        self.t = time.time()

    def time(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(services, "time", fake)
    return fake


def _pickup_trip():
    route = Route.objects.get(code="R4")
    services.ensure_trips_for_date(route, timezone.localdate())
    trip = Trip.objects.get(route=route, direction="pickup", service_date=timezone.localdate())
    # Schedule "now" so delay checks are meaningful whatever time the tests run.
    trip.scheduled_start = timezone.localtime().time().replace(second=0, microsecond=0)
    trip.save()
    return trip


def _positions(trip, start_m, end_m, step_m=60, seconds=6, t0=None):
    plan = services.route_plan(trip.route, trip.direction)
    t0 = t0 or timezone.now()
    fixes, d, i = [], start_m, 0
    while d <= end_m:
        lat, lng = plan.geometry.point_at(d)
        fixes.append(
            {
                "client_id": f"t-{trip.id}-{int(d)}",
                "lat": lat,
                "lng": lng,
                "speed": 7.0,
                "accuracy": 8,
                "recorded_at": (t0 + timedelta(seconds=i * seconds)).isoformat(),
            }
        )
        d += step_m
        i += 1
    return fixes


def _send(driver, trip, fixes, clock, batch=20, after_batch=None):
    responses = []
    for start in range(0, len(fixes), batch):
        chunk = fixes[start : start + batch]
        clock.t = datetime.fromisoformat(chunk[-1]["recorded_at"]).timestamp() + 1  # uploaded right after
        response = driver.post(f"/api/v1/driver/trips/{trip.id}/positions", {"positions": chunk}, format="json")
        assert response.status_code == 200, response.content
        responses.append(response.json())
        if after_batch:
            after_batch(start, chunk)
    return responses


def _bus_alerts(phone, trip):
    return list(Notification.objects.filter(user=user(phone), dedupe_key__startswith=f"bus:{trip.id}"))


def _kinds(alerts):
    return sorted(n.dedupe_key.rsplit(":", 1)[-1] for n in alerts)


@pytest.mark.django_db
def test_full_trip_live_tracking_and_alerts(ghis, in_ghis, parent, driver, clock):
    trip = _pickup_trip()
    aarav = Student.objects.get(full_name="Aarav Iyer")

    # Before the trip starts, parents see the schedule but no location.
    live = parent.get(f"/api/v1/transport/trips/{trip.id}/live").json()
    assert live["status"] == "scheduled" and live["position"] is None

    assert driver.get("/api/v1/driver/trips").status_code == 200
    started = driver.post(f"/api/v1/driver/trips/{trip.id}/start")
    assert started.status_code == 200 and started.json()["status"] == "active"
    assert driver.post(f"/api/v1/driver/trips/{trip.id}/start").status_code == 200  # a double tap is harmless

    started_alerts = [n for n in _bus_alerts(PARENT_MEERA, trip) if n.dedupe_key.endswith(":started")]
    assert len(started_alerts) == 1  # one alert for Aarav and Diya together, not one per child
    assert "Aarav and Diya" in started_alerts[0].body

    route_length = services.route_plan(trip.route, trip.direction).geometry.length_m
    fixes = _positions(trip, 0, route_length)

    def check_midway(start, chunk):
        if start == 0:
            retry = driver.post(f"/api/v1/driver/trips/{trip.id}/positions", {"positions": chunk}, format="json")
            assert retry.json()["stored"] == 0  # a retried upload is idempotent
        if start == 20:
            mid = parent.get(f"/api/v1/transport/trips/{trip.id}/live").json()
            assert mid["status"] == "active" and mid["signal"] == "live"
            assert mid["position"] is not None and mid["next_stop"] is not None

    _send(driver, trip, fixes, clock, batch=20, after_batch=check_midway)

    kinds = _kinds(_bus_alerts(PARENT_MEERA, trip))
    assert kinds.count("started") == 1
    assert kinds.count("approach") == 1  # "about N min away" exactly once
    assert kinds.count("arrived") == 1  # "bus at MG Road" exactly once
    assert kinds.count("reached") == 1  # "reached school"

    rahul = _kinds(_bus_alerts(PARENT_RAHUL, trip))  # Kabir, Lake View
    assert rahul.count("approach") == 1 and rahul.count("arrived") == 1

    # Watching the live map is logged (children's location is sensitive).
    assert TripViewLog.objects.filter(trip=trip, user=user(PARENT_MEERA)).exists()

    # Ending requires the bus-empty check.
    assert driver.post(f"/api/v1/driver/trips/{trip.id}/end", {}, format="json").status_code == 400
    ended = driver.post(f"/api/v1/driver/trips/{trip.id}/end", {"empty_check_confirmed": True}, format="json")
    assert ended.status_code == 200 and ended.json()["status"] == "completed"

    # After the trip, families no longer see the bus location.
    after = parent.get(f"/api/v1/transport/trips/{trip.id}/live").json()
    assert after["status"] == "completed" and after["position"] is None
    assert parent.get(f"/api/v1/students/{aarav.id}/transport").json()["enrolled"] is True


@pytest.mark.django_db
def test_not_travelling_today_suppresses_alerts(ghis, in_ghis, parent, driver, clock):
    trip = _pickup_trip()
    for name in ("Aarav Iyer", "Diya Iyer"):
        student = Student.objects.get(full_name=name)
        response = parent.post(f"/api/v1/students/{student.id}/transport/absences", {"direction": "pickup"}, format="json")
        assert response.status_code == 201

    driver.post(f"/api/v1/driver/trips/{trip.id}/start")
    _send(driver, trip, _positions(trip, 0, 3600), clock, batch=10)
    assert _bus_alerts(PARENT_MEERA, trip) == []
    assert "arrived" in _kinds(_bus_alerts(PARENT_RAHUL, trip))  # other families still get theirs

    roster = driver.get(f"/api/v1/driver/trips/{trip.id}/roster").json()
    mg_road = next(s for s in roster["stops"] if s["name"] == "MG Road")
    assert all(s["absent"] for s in mg_road["students"] if s["name"] in {"Aarav Iyer", "Diya Iyer"})


@pytest.mark.django_db
def test_alert_threshold_follows_parent_preference(ghis, in_ghis, parent, driver, clock):
    trip = _pickup_trip()
    aarav = Student.objects.get(full_name="Aarav Iyer")
    assert parent.put(f"/api/v1/students/{aarav.id}/transport/preferences", {"alert_minutes": 7}, format="json").status_code == 400
    # Siblings share a stop and get one alert, timed by the earliest preference, so set both.
    for name in ("Aarav Iyer", "Diya Iyer"):
        child = Student.objects.get(full_name=name)
        assert parent.put(f"/api/v1/students/{child.id}/transport/preferences", {"alert_minutes": 5}, format="json").status_code == 200

    driver.post(f"/api/v1/driver/trips/{trip.id}/start")
    _send(driver, trip, _positions(trip, 0, 3000), clock, batch=5)
    approach = [n for n in _bus_alerts(PARENT_MEERA, trip) if n.dedupe_key.endswith(":approach")]
    assert len(approach) == 1
    minutes = int(approach[0].title.split("about ")[1].split(" min")[0])
    assert minutes <= 5


@pytest.mark.django_db
def test_offline_backlog_does_not_send_stale_stop_alerts(ghis, in_ghis, driver):
    trip = _pickup_trip()
    driver.post(f"/api/v1/driver/trips/{trip.id}/start")
    # The phone was offline for the whole trip and uploads everything 30 minutes later.
    fixes = _positions(trip, 0, 3000, t0=timezone.now() - timedelta(minutes=45))
    response = driver.post(f"/api/v1/driver/trips/{trip.id}/positions", {"positions": fixes}, format="json")
    assert "arrived" in response.json()["events"]  # the history is recorded...
    stale = [n for n in _bus_alerts(PARENT_MEERA, trip) if n.dedupe_key.endswith((":arrived", ":approach"))]
    assert stale == []  # ...but nobody is told the bus is "at the stop" half an hour late


@pytest.mark.django_db
def test_future_dated_fixes_do_not_stall_tracking(ghis, in_ghis, driver):
    trip = _pickup_trip()
    driver.post(f"/api/v1/driver/trips/{trip.id}/start")
    skewed = _positions(trip, 0, 120, t0=timezone.now() + timedelta(hours=2))
    response = driver.post(f"/api/v1/driver/trips/{trip.id}/positions", {"positions": skewed}, format="json")
    assert response.json()["rejected_future"] == len(skewed)
    good = _positions(trip, 0, 120, t0=timezone.now() - timedelta(seconds=20))
    for fix in good:
        fix["client_id"] += "-ok"
    response = driver.post(f"/api/v1/driver/trips/{trip.id}/positions", {"positions": good}, format="json")
    assert response.json()["accepted"] == len(good)


@pytest.mark.django_db
def test_parents_on_other_routes_and_schools_cannot_watch(ghis, sps, in_ghis):
    trip = _pickup_trip()
    other_parent = api("+919900000100", ghis)  # guardian of a child who isn't on Route 4
    assert other_parent.get(f"/api/v1/transport/trips/{trip.id}/live").status_code == 404
    assert api(SPS_PARENT, sps).get(f"/api/v1/transport/trips/{trip.id}/live").status_code == 404


@pytest.mark.django_db
def test_boarding_events_notify_parent(ghis, in_ghis, driver):
    trip = _pickup_trip()
    aarav = Student.objects.get(full_name="Aarav Iyer")
    driver.post(f"/api/v1/driver/trips/{trip.id}/start")
    attendant = api("+919800000008", ghis)
    body = {"student_id": str(aarav.id), "kind": "boarded"}
    assert attendant.post(f"/api/v1/driver/trips/{trip.id}/boarding", body, format="json").status_code == 200
    attendant.post(f"/api/v1/driver/trips/{trip.id}/boarding", body, format="json")  # retry
    boarded = [n for n in _bus_alerts(PARENT_MEERA, trip) if n.dedupe_key.endswith(":boarded")]
    assert len(boarded) == 1 and boarded[0].title == "Aarav boarded the bus"


@pytest.mark.django_db
def test_sos_reaches_transport_staff(ghis, in_ghis, driver):
    trip = _pickup_trip()
    driver.post(f"/api/v1/driver/trips/{trip.id}/start")
    response = driver.post(f"/api/v1/driver/trips/{trip.id}/sos", {"lat": 17.48, "lng": 78.40, "note": "Flat tyre"}, format="json")
    assert response.status_code == 201
    staff_alert = Notification.objects.filter(user=user("+919800000006"), category="safety", data__type="sos")
    assert staff_alert.count() == 1 and staff_alert.first().priority == "critical"


@pytest.mark.django_db
def test_traccar_device_feed(ghis, in_ghis, driver, client, settings):
    trip = _pickup_trip()
    driver.post(f"/api/v1/driver/trips/{trip.id}/start")
    lat, lng = services.route_plan(trip.route, trip.direction).geometry.point_at(200)
    body = {
        "position": {
            "id": 991, "latitude": lat, "longitude": lng, "speed": 12.0, "course": 180, "valid": True,
            "fixTime": timezone.now().isoformat(), "accuracy": 0,
        },
        "device": {"uniqueId": "358899051234567", "name": "Bus 4"},
    }
    denied = client.post("/api/v1/transport/ingest/traccar", body, content_type="application/json")
    assert denied.status_code == 403
    accepted = client.post(
        "/api/v1/transport/ingest/traccar", body, content_type="application/json",
        headers={"X-Ingest-Secret": settings.EDUFLOW["TRACCAR_SHARED_SECRET"]},
    )
    assert accepted.status_code == 200 and accepted.json()["accepted"] == 1
    trip.refresh_from_db()
    assert trip.last_position_at is not None


@pytest.mark.django_db
def test_live_updates_are_published_to_the_trip_channel(ghis, in_ghis, driver, clock, monkeypatch, django_capture_on_commit_callbacks):
    from apps.realtime import client as realtime
    from apps.realtime import tasks

    published = []

    class RecordingTask:
        def enqueue(self, channel, data):
            published.append((channel, data))

    monkeypatch.setattr(realtime, "enabled", lambda: True)
    monkeypatch.setattr(tasks, "publish_to_centrifugo", RecordingTask())
    trip = _pickup_trip()
    with django_capture_on_commit_callbacks(execute=True):
        driver.post(f"/api/v1/driver/trips/{trip.id}/start")
        _send(driver, trip, _positions(trip, 0, 300), clock, batch=10)
    trip_updates = [d for c, d in published if c == f"trip:{trip.id}"]
    assert trip_updates, "no live update published"
    last = trip_updates[-1]
    assert last["type"] == "trip.live" and last["trip"]["position"] is not None
    assert "staff" not in last["trip"]  # parent-safe payload on the shared channel


@pytest.mark.django_db
def test_monitor_flags_lost_signal_and_closes_forgotten_trips(ghis, in_ghis, driver, clock):
    trip = _pickup_trip()
    driver.post(f"/api/v1/driver/trips/{trip.id}/start")
    _send(driver, trip, _positions(trip, 0, 300), clock, batch=10)
    trip.refresh_from_db()
    summary = services.monitor_active_trips(now=trip.last_position_at.timestamp() + 600)
    assert summary["signal_alerts"] >= 1
    summary = services.monitor_active_trips(now=trip.started_at.timestamp() + 5 * 3600)
    assert summary["auto_closed"] == 1
    trip.refresh_from_db()
    assert trip.status == "completed" and trip.auto_closed is True
