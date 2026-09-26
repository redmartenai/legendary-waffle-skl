"""Operations seed: transport. The live drop run the Transport page shows.

14 buses: Route 07 (from the base seed) held at the ORR junction and 12 min late, Routes 01–13 on time, Route 12
already back (a short route), and Bus 14 in the depot. The run is anchored to the moment the seed runs: every
bus left school ~17 minutes ago and its GPS fixes are replayed through the real tracking engine
(``apps.transport.engine``), so trip state, stop events and ETAs are exactly what live tracking would have made.
No notifications are sent (``services.ingest_positions`` is not used).

Phones: ``cmd._phone(5500..5799)``.
"""

import random
from datetime import datetime, time, timedelta

from django.utils import timezone

from apps.accounts.models import Role, User
from apps.academics.models import Student
from apps.core.utils import school_today, school_tz
from apps.transport import engine
from apps.transport.models import (
    BoardingEvent,
    Direction,
    Route,
    Stop,
    StudentTransport,
    TransportAbsence,
    TransportException,
    Trip,
    TripIncident,
    TripPosition,
    TripStopEvent,
    Vehicle,
)
from apps.transport.services import ensure_trips_for_date, refresh_route_length, route_plan

PHONES = range(5500, 5800)
SCHOOL_STOP = ("Sunrise Public School", 13.0358, 77.5970)
DWELL_S = engine.DEFAULT_DWELL_SECONDS
RUN_MINUTES = 17  # how long ago the drop run left school
ROUTE_07_SPEED_KMH = 7.8  # what Route 07's own timetable implies (8.9 km in 58 min with stops)
ROUTE_07_BASE_SPEED_KMH = 20  # the base seed's value, restored by clear()

# code: riders, absent today, speed km/h, stops (first pickup → last pickup; the school is appended)
ROUTES = {
    "01": (29, 1, 12, [("Yeshwanthpur Circle", 13.0236, 77.5504), ("Mathikere", 13.0331, 77.5622), ("Gokula Extension", 13.0390, 77.5670), ("BEL Circle", 13.0419, 77.5752), ("Dollars Colony", 13.0409, 77.5862)]),
    "02": (31, 0, 12, [("Rajmahal Vilas", 13.0006, 77.5781), ("Sadashivanagar", 13.0087, 77.5809), ("Mekhri Circle", 13.0148, 77.5837), ("Ganganagar", 13.0262, 77.5903), ("CBI Road", 13.0306, 77.5936)]),
    "03": (32, 2, 12, [("Kothanur", 13.0613, 77.6453), ("Hennur Cross", 13.0441, 77.6391), ("HBR Layout", 13.0361, 77.6322), ("Veerannapalya", 13.0396, 77.6181), ("Kaval Byrasandra", 13.0305, 77.6123)]),
    "04": (27, 1, 11, [("Benson Town", 13.0046, 77.6036), ("Cholanayakanahalli", 13.0161, 77.5991), ("RT Nagar Main Road", 13.0211, 77.5956), ("Sultanpalya", 13.0271, 77.6011)]),
    "05": (33, 0, 12, [("Kammanahalli", 13.0152, 77.6396), ("Lingarajapuram", 13.0129, 77.6291), ("Cox Town", 13.0006, 77.6236), ("Frazer Town", 12.9991, 77.6141), ("Pillanna Garden", 13.0146, 77.6106), ("Tannery Road", 13.0241, 77.6091)]),
    "06": (30, 1, 13, [("Hegde Nagar", 13.0741, 77.6321), ("Thanisandra", 13.0606, 77.6331), ("Rachenahalli", 13.0661, 77.6211), ("Manyata Tech Park", 13.0471, 77.6201), ("Hebbal Kempapura", 13.0481, 77.6061)]),
    "08": (28, 1, 14, [("Yelahanka New Town", 13.1006, 77.5871), ("Jakkur", 13.0771, 77.6011), ("Amruthahalli", 13.0681, 77.5981), ("Byatarayanapura", 13.0621, 77.5931)]),
    "09": (31, 1, 12, [("Vidyaranyapura", 13.0781, 77.5561), ("Doddabommasandra", 13.0691, 77.5626), ("Canara Bank Layout", 13.0621, 77.5691), ("New BEL Road", 13.0451, 77.5721), ("Sanjaynagar", 13.0386, 77.5791)]),
    "10": (26, 1, 12, [("Horamavu", 13.0291, 77.6601), ("Banaswadi", 13.0141, 77.6511), ("Kalyan Nagar", 13.0246, 77.6411), ("HRBR Layout", 13.0206, 77.6331), ("Hennur Main Road", 13.0301, 77.6241)]),
    "11": (33, 2, 12, [("Seshadripuram", 12.9931, 77.5721), ("Vyalikaval", 13.0021, 77.5741), ("Palace Grounds", 13.0001, 77.5921), ("Jayamahal", 13.0076, 77.5991), ("Guddadahalli", 13.0216, 77.5881)]),
    "12": (30, 0, 14, [("RT Nagar Post Office", 13.0241, 77.5961), ("Ganganagar North", 13.0296, 77.5886)]),
    "13": (25, 1, 12, [("Allalasandra", 13.0886, 77.5836), ("Tata Nagar", 13.0706, 77.5886), ("Dasarahalli", 13.0561, 77.5941), ("Bhoopasandra", 13.0441, 77.5811)]),
}
REGISTRATIONS = {
    "01": "KA 05 MN 1104", "02": "KA 05 MN 1187", "03": "KA 05 MP 3012", "04": "KA 05 MP 3368", "05": "KA 05 MQ 4420",
    "06": "KA 05 MQ 4471", "08": "KA 05 MR 5129", "09": "KA 05 MR 5163", "10": "KA 05 MS 6035", "11": "KA 05 MS 6092",
    "12": "KA 05 MT 7218", "13": "KA 05 MT 7254", "14": "KA 05 MU 8340",
}
DRIVERS = ["Manjunath H.", "Ravi Shankar", "Venkatesh B.", "Imran Pasha", "Nagaraj K.", "Prakash Rao", "Syed Abrar", "Mahesh Gowda", "Anand Kumar", "Srinivas M.", "Basavaraj P.", "Joseph D'Souza"]
ATTENDANTS = ["Savitha N.", "Rekha M.", "Shobha K.", "Farzana B.", "Geetha R.", "Mary Thomas", "Pushpa L.", "Kavya S.", "Nirmala D.", "Asha P.", "Roopa G.", "Lalitha V."]
MY_CODES = list(ROUTES)
MY_VEHICLES = [f"Bus {c}" for c in MY_CODES] + ["Bus 14"]


def clear(cmd, school):
    today = school_today(school)
    mine = Route.objects.filter(code__in=MY_CODES)
    r07 = Route.objects.filter(code="07").first()
    rider_ids = StudentTransport.objects.filter(route__in=list(mine) + ([r07] if r07 else [])).values_list("student_id", flat=True)
    # Seeded absences have no author; ones families made in the app keep theirs.
    TransportAbsence.objects.filter(student_id__in=list(rider_ids), created_by__isnull=True).delete()
    TransportException.objects.all().delete()
    if r07 is not None:
        seeded = Trip.objects.filter(route=r07, direction=Direction.DROP).filter(
            positions__source=TripPosition.Source.SIMULATOR
        ).distinct()
        for trip in list(seeded) + list(Trip.objects.filter(route=r07, direction=Direction.DROP, service_date=today)):
            TripPosition.objects.filter(trip=trip).delete()
            TripStopEvent.objects.filter(trip=trip).delete()
            BoardingEvent.objects.filter(trip=trip).delete()
            TripIncident.objects.filter(trip=trip).delete()
            Trip.objects.filter(pk=trip.pk).update(
                status=Trip.Status.SCHEDULED, state={}, started_at=None, ended_at=None, last_position_at=None,
                scheduled_start=r07.drop_start, empty_check_confirmed_at=None, auto_closed=False,
            )
        Route.objects.filter(pk=r07.pk).update(avg_speed_kmh=ROUTE_07_BASE_SPEED_KMH)
    mine.delete()
    Vehicle.objects.filter(label__in=MY_VEHICLES).delete()
    User.objects.filter(phone__in=[cmd._phone(n) for n in PHONES]).delete()


def seed(cmd, school):
    rng = random.Random(26092026)
    tz = school_tz(school)
    today = school_today(school)
    now = timezone.now()
    # The drop run left school RUN_MINUTES ago (on the minute), or at midnight if that's still today.
    start = (now - timedelta(minutes=RUN_MINUTES, seconds=30)).astimezone(tz).replace(second=0, microsecond=0)
    if start.date() != today:
        start = datetime.combine(today, time(0, 0), tzinfo=tz)

    r07 = Route.objects.filter(code="07").select_related("vehicle", "driver", "attendant").first()
    routes = _routes(cmd, school, rng, start)
    Vehicle.objects.create(
        registration_no=REGISTRATIONS["14"], label="Bus 14", capacity=40, gps_device_id=None,
        has_gps_tracker=True, has_panic_button=True, has_cctv=True, has_speed_governor=True, **_papers(today, 12),
    )
    riders = _riders(cmd, routes, rng)

    absent: dict = {}
    for route in routes:
        absent[route.code] = _absences(route, ROUTES[route.code][1], today, rng, avoid=set())
    if r07 is not None:
        Route.objects.filter(pk=r07.pk).update(avg_speed_kmh=ROUTE_07_SPEED_KMH)
        r07.refresh_from_db()
        ensure_trips_for_date(r07, today)
        aarav = [s.id for (k, n), s in cmd.students.items() if n in ("Aarav Sharma", "Diya Sharma")]
        absent["07"] = _absences(r07, 2, today, rng, avoid=set(aarav))

    trips = {}
    for route in routes + ([r07] if r07 else []):
        _complete_morning(route, today, tz, now)
        trip = Trip.objects.get(route=route, direction=Direction.DROP, service_date=today)
        if route.code == "07":
            # Left 2 min late, then stuck at the junction: its last fix is 12 min after the junction's planned time.
            planned_orr = start + timedelta(minutes=Stop.objects.get(route=route, name="ORR Junction").drop_offset_min)
            until = min(now - timedelta(seconds=5), planned_orr + timedelta(minutes=12, seconds=rng.randint(5, 20)))
            _run(trip, start, until, rng, depart_after_s=120, speed_mps=7.0, hold_at="ORR Junction", absent=absent["07"])
            orr = Stop.objects.get(route=route, name="ORR Junction")
            held_since = TripStopEvent.objects.filter(trip=trip, stop=orr, kind="arrived").values_list("at", flat=True).first()
            TripIncident.objects.create(
                trip=trip, kind=TripIncident.Kind.DELAY, at=(held_since or now) + timedelta(minutes=4),
                details={"reason": "Traffic at the Outer Ring Road junction", "by": "Transport desk"},
            )
        else:
            _run(trip, start, now - timedelta(seconds=rng.randint(3, 12)), rng, depart_after_s=rng.randint(0, 30), speed_mps=route.avg_speed_kmh / 3.6, absent=absent[route.code])
        trips[route.code] = trip

    _exceptions(cmd, routes, r07, trips, absent, riders, now, tz)
    cmd.stdout.write(f"  transport: {len(routes) + (1 if r07 else 0)} routes, {len(routes) + (2 if r07 else 1)} buses, drop run since {start:%H:%M}")


# ---------------------------------------------------------------- routes, crews and riders


def _routes(cmd, school, rng, start) -> list:
    routes = []
    for i, (code, (_riders, _absent, speed, stops)) in enumerate(ROUTES.items()):
        driver = cmd._user(cmd._phone(PHONES[0] + 2 * i), DRIVERS[i], school, Role.DRIVER, title=f"Driver · Route {code}")
        attendant = cmd._user(cmd._phone(PHONES[0] + 2 * i + 1), ATTENDANTS[i], school, Role.ATTENDANT, title=f"Attendant · Route {code}")
        vehicle = Vehicle.objects.create(
            registration_no=REGISTRATIONS[code], label=f"Bus {code}", capacity=40 if len(stops) > 3 else 32,
            has_gps_tracker=True, has_panic_button=True, has_cctv=True, has_speed_governor=True, **_papers(start.date(), i),
        )
        points = stops + [SCHOOL_STOP]
        route = Route.objects.create(
            code=code, name=f"Route {code}", path=[[lat, lng] for _n, lat, lng in points], avg_speed_kmh=speed,
            vehicle=vehicle, driver=driver, attendant=attendant, pickup_start=time(7, 0), drop_start=start.time(),
        )
        refresh_route_length(route)
        geometry = engine.RouteGeometry(route.path)
        v = speed / 3.6
        distances = [geometry.project(lat, lng).distance_m for _n, lat, lng in points]
        total = geometry.length_m
        n = len(points)
        for seq, ((name, lat, lng), d) in enumerate(zip(points, distances), start=1):
            # Timetables the engine agrees with: travel at the route's speed plus a dwell at each stop before.
            pickup = (d / v + (seq - 1) * DWELL_S) / 60
            drop = ((total - d) / v + max(0, n - seq - 1) * DWELL_S) / 60 if seq < n else 0
            Stop.objects.create(
                route=route, name=name, lat=lat, lng=lng, sequence=seq, pickup_offset_min=round(pickup),
                drop_offset_min=round(drop), is_school=name == SCHOOL_STOP[0],
            )
        ensure_trips_for_date(route, school_today(school))
        routes.append(route)
    return routes


def _papers(today, i: int) -> dict:
    """Fitness, permit, insurance and PUC certificates, valid for a few more months (staggered per bus)."""
    return {
        "fitness_valid_until": today + timedelta(days=150 + 17 * i),
        "permit_valid_until": today + timedelta(days=120 + 11 * i),
        "insurance_valid_until": today + timedelta(days=200 + 9 * i),
        "puc_valid_until": today + timedelta(days=60 + 7 * i),
    }


def _riders(cmd, routes, rng) -> dict:
    taken = set(StudentTransport.objects.values_list("student_id", flat=True))
    family = {s.id for (k, n), s in cmd.students.items() if n in ("Aarav Sharma", "Diya Sharma")}
    pool = [s for s in Student.objects.filter(is_active=True).order_by("admission_no") if s.id not in taken | family]
    rng.shuffle(pool)
    out = {}
    for route in routes:
        stops = list(Stop.objects.filter(route=route, is_school=False).order_by("sequence"))
        count = ROUTES[route.code][0]
        chosen, pool = pool[:count], pool[count:]
        rows = [StudentTransport(school=route.school, student=s, route=route, pickup_stop=(stop := rng.choice(stops)), drop_stop=stop) for s in chosen]
        StudentTransport.objects.bulk_create(rows)
        out[route.code] = chosen
    return out


def _absences(route, count, today, rng, avoid) -> list:
    riders = [r.student for r in StudentTransport.objects.filter(route=route, is_active=True).select_related("student").order_by("student__full_name")]
    riders = [s for s in riders if s.id not in avoid]
    chosen = rng.sample(riders, min(count, len(riders)))
    for s in chosen:
        TransportAbsence.objects.create(student=s, service_date=today, direction=TransportAbsence.Scope.BOTH)
    return chosen


def _complete_morning(route, today, tz, now):
    """This morning's pickup ran (the base seed already closed Route 07's)."""
    pickup = Trip.objects.filter(route=route, direction=Direction.PICKUP, service_date=today, status=Trip.Status.SCHEDULED).first()
    if pickup is None:
        return
    begun = datetime.combine(today, pickup.scheduled_start, tzinfo=tz)
    ended = begun + timedelta(minutes=max(s.pickup_offset_min for s in route.stops.all()) + 2)
    if ended < now:
        Trip.objects.filter(pk=pickup.pk).update(status=Trip.Status.COMPLETED, started_at=begun, ended_at=ended)


# ---------------------------------------------------------------- replaying the run through the engine


def _run(trip, start, until, rng, *, depart_after_s, speed_mps, absent, hold_at=None):
    """Drive the bus from the school gate along its drop route and feed each GPS fix (up to ``until``) to the engine."""
    trip = Trip.objects.select_related("route", "school", "attendant").get(pk=trip.pk)
    plan = route_plan(trip.route, Direction.DROP)
    until = until.timestamp()

    def timeline(depart: float) -> list:
        """Piecewise legs (t0, t1, d0, d1): moving between stops, dwelling at them, or held."""
        legs, t, d = [], depart, 0.0
        for stop in plan.stops[1:]:
            t1 = t + (stop.distance_m - d) / speed_mps
            legs.append((t, t1, d, stop.distance_m))
            t, d = t1, stop.distance_m
            if stop.name == hold_at:
                legs.append((t, float("inf"), d, d))
                break
            legs.append((t, t + DWELL_S, d, d))
            t += DWELL_S
        return legs

    def cruising(legs) -> bool:
        # Pulled away from a stop 2+ minutes ago (the engine's smoothed speed has settled) and not yet at the next.
        return any(t0 + 120 <= until < t1 - 40 and d0 != d1 for t0, t1, d0, d1 in legs) or until > legs[-1][1]

    options = [depart_after_s] if hold_at else [depart_after_s + k * sign * 15 for k in range(8) for sign in (1, -1)]
    depart = start.timestamp() + next((o for o in options if cruising(timeline(start.timestamp() + o))), depart_after_s)
    legs = timeline(depart)
    end = legs[-1][1]

    def progress(at: float) -> tuple[float, float]:
        for t0, t1, d0, d1 in legs:
            if at <= t1:
                if at < t0:
                    return 0.0, 0.0
                frac = 0.0 if t1 == t0 or t1 == float("inf") else (at - t0) / (t1 - t0)
                return d0 + frac * (d1 - d0), (0.0 if d0 == d1 else speed_mps)
        return legs[-1][3], 0.0

    last = min(until, end + 60)
    times = [depart - 90, depart - 30]
    at = depart + 15
    while at < last:
        times.append(at)
        at += 20
    times.append(last)

    state = engine.TripState()
    positions, stop_events = [], []
    for at in times:
        dist, speed = progress(at)
        lat, lng = plan.geometry.point_at(dist)
        fix = engine.Fix(lat=lat, lng=lng, at=at, speed_mps=speed, accuracy_m=8.0)
        state, events, accepted = engine.advance(state, plan.geometry, plan.stops, fix)
        if not accepted:
            continue
        when = datetime.fromtimestamp(at, tz=start.tzinfo)
        positions.append(TripPosition(school=trip.school, trip=trip, lat=lat, lng=lng, speed_mps=speed, heading=state.heading, accuracy_m=8.0, recorded_at=when, source=TripPosition.Source.SIMULATOR))
        for ev in events:
            if ev.kind in ("arrived", "departed", "skipped"):
                stop_events.append(TripStopEvent(school=trip.school, trip=trip, stop=plan.stop_models[ev.stop_id], kind=ev.kind, at=when))
    TripPosition.objects.bulk_create(positions)
    TripStopEvent.objects.bulk_create(stop_events)

    started = datetime.fromtimestamp(depart - 120, tz=start.tzinfo)
    fields = {
        "state": state.to_dict(),
        "status": Trip.Status.ACTIVE,
        "started_at": started,
        "scheduled_start": start.time(),
        "last_position_at": positions[-1].recorded_at if positions else None,
    }
    if state.final_arrived_at is not None:
        fields.update(status=Trip.Status.COMPLETED, ended_at=datetime.fromtimestamp(state.final_arrived_at + 150, tz=start.tzinfo), empty_check_confirmed_at=datetime.fromtimestamp(state.final_arrived_at + 120, tz=start.tzinfo))
    Trip.objects.filter(pk=trip.pk).update(**fields)

    # Everyone travelling boarded at the gate; riders whose stop the bus has left got off there.
    left = {e.stop_id: e.at for e in stop_events if e.kind == "departed"}
    absent_ids = {s.id for s in absent}
    boarding = []
    for rider in StudentTransport.objects.filter(route=trip.route, is_active=True).select_related("drop_stop"):
        if rider.student_id in absent_ids:
            continue
        boarding.append(BoardingEvent(school=trip.school, trip=trip, student_id=rider.student_id, kind="boarded", at=started + timedelta(seconds=30), recorded_by=trip.attendant))
        if rider.drop_stop_id in left:
            boarding.append(BoardingEvent(school=trip.school, trip=trip, student_id=rider.student_id, kind="dropped", at=left[rider.drop_stop_id], recorded_by=trip.attendant))
    BoardingEvent.objects.bulk_create(boarding)


# ---------------------------------------------------------------- pickup & drop exceptions


def _exceptions(cmd, routes, r07, trips, absent, riders, now, tz):
    desk = User.objects.filter(phone=cmd._phone(41)).first()
    by_code = {r.code: r for r in routes}

    # 1. Held: a child whose stop the bus has reached, and the adult waiting wasn't on the pickup list.
    held = None
    for route in routes:
        trip = trips[route.code]
        reached = dict(TripStopEvent.objects.filter(trip=trip, kind="arrived").values_list("stop_id", "at"))
        absent_ids = {s.id for s in absent[route.code]}
        for st in StudentTransport.objects.filter(route=route, drop_stop_id__in=reached).select_related("student__class_group").order_by("student__class_group__grade", "student__full_name"):
            if st.student_id not in absent_ids and st.student.class_group.grade in ("3", "2", "4"):
                held = (route, trip, st, reached[st.drop_stop_id])
                break
        if held:
            break
    if held:
        route, trip, st, at = held
        called = at + timedelta(minutes=3)
        item = TransportException.objects.create(
            kind=TransportException.Kind.HELD, route=route, trip=trip, from_stop=st.drop_stop, occurred_at=at, reported_by=route.attendant,
            note=f"Unregistered adult at pickup · parent called {called.astimezone(tz):%-I:%M %p}",
        )
        item.students.set([st.student])

    if r07 is not None:
        trip = trips["07"]
        stops = {s.name: s for s in Stop.objects.filter(route=r07)}
        absent_ids = {s.id for s in absent["07"]}
        # 2. Stop change: a rider getting off one stop earlier today (a grandparent's house).
        order = [s.name for s in route_plan(r07, Direction.DROP).stops]
        movable = [
            st for st in StudentTransport.objects.filter(route=r07, drop_stop__name__in=["Maple Residency Gate", "Palm Grove"]).select_related("student", "drop_stop").order_by("student__full_name")
            if st.student_id not in absent_ids and st.student.full_name != "Aarav Sharma"
        ]
        if movable:
            st = movable[0]
            to = stops[order[order.index(st.drop_stop.name) - 1]]
            item = TransportException.objects.create(
                kind=TransportException.Kind.STOP_CHANGE, route=r07, trip=trip, from_stop=st.drop_stop, to_stop=to,
                occurred_at=now - timedelta(minutes=40), reported_by=desk, note="Parent's request, confirmed by phone",
            )
            item.students.set([st.student])
        # 4. Off manifest: absent today, so off the drop list.
        if absent["07"]:
            item = TransportException.objects.create(
                kind=TransportException.Kind.OFF_MANIFEST, route=r07, trip=trip, occurred_at=now - timedelta(minutes=55), reported_by=desk,
                note="Absent today · removed from today's Route 07 drop list",
            )
            item.students.set([absent["07"][-1]])

    # 3. Not scanned: two riders on Route 11 boarded without their ID card being scanned.
    route = by_code.get("11")
    if route is not None:
        absent_ids = {s.id for s in absent["11"]}
        pair = [s for s in riders["11"] if s.id not in absent_ids][:2]
        item = TransportException.objects.create(
            kind=TransportException.Kind.NOT_SCANNED, route=route, trip=trips["11"], occurred_at=now - timedelta(minutes=RUN_MINUTES - 1),
            reported_by=route.attendant, note="Boarded without ID scan · attendant confirming",
        )
        item.students.set(pair)
