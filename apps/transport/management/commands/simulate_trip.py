"""
Drive a bus along a route so live tracking can be tested without a real bus.

    python manage.py simulate_trip --school GHIS --route R4 --direction pickup
    python manage.py simulate_trip --route R4 --fast          # no waiting (tests, demos)
    python manage.py simulate_trip --route R4 --speedup 4     # 4x faster than real time

Fixes go through the same ingest pipeline as the driver app and GPS devices.
"""

import math
import random
import time
import uuid

from django.core.management.base import BaseCommand, CommandError

from apps.core.tenant import use_school
from apps.core.utils import school_now, school_today
from apps.tenancy.models import School
from apps.transport import services
from apps.transport.models import (
    BoardingEvent,
    Direction,
    Route,
    Trip,
    TripIncident,
    TripPosition,
    TripStopEvent,
    TripViewLog,
)


class _SimClock:
    def __init__(self, start: float):
        self.t = start

    def time(self) -> float:
        return self.t


class Command(BaseCommand):
    help = "Simulate a bus trip along a route (live-tracking test harness)."

    def add_arguments(self, parser):
        parser.add_argument("--school", default="GHIS")
        parser.add_argument("--route", default="R4")
        parser.add_argument("--direction", choices=[Direction.PICKUP, Direction.DROP], default=Direction.PICKUP)
        parser.add_argument("--speed-kmh", type=float, default=26.0)
        parser.add_argument("--interval", type=float, default=3.0, help="Seconds between GPS fixes")
        parser.add_argument("--dwell", type=float, default=25.0, help="Seconds spent at each stop")
        parser.add_argument("--speedup", type=float, default=1.0, help="Run faster than real time")
        parser.add_argument("--fast", action="store_true", help="Don't wait between fixes")
        parser.add_argument("--no-reset", action="store_true", help="Keep today's trip history")
        parser.add_argument("--no-end", action="store_true", help="Leave the trip running at the end")
        parser.add_argument("--seed", type=int, default=7)

    def handle(self, *args, **options):
        school = School.objects.filter(code=options["school"].upper()).first()
        if school is None:
            raise CommandError(f"No school with code {options['school']}. Run seed_demo first.")
        random.seed(options["seed"])
        # The simulated bus lives on its own clock (faster than real time with --speedup/--fast);
        # tracking services read "now" from it so ETAs, staleness and alerts stay consistent.
        self.clock = _SimClock(time.time())
        real_clock = services.time
        services.time = self.clock
        try:
            with use_school(school):
                self._run(school, options)
        finally:
            services.time = real_clock

    def _run(self, school, options):
        route = Route.objects.filter(code=options["route"]).first()
        if route is None:
            raise CommandError(f"No route {options['route']} at {school.code}.")
        today = school_today(school)
        services.ensure_trips_for_date(route, today)
        trip = Trip.objects.select_related("route", "vehicle", "school", "driver").get(
            route=route, direction=options["direction"], service_date=today
        )
        if not options["no_reset"]:
            self._reset(trip)

        services.start_trip(trip, trip.driver)
        trip.refresh_from_db()
        plan = services.route_plan(route, trip.direction)
        speed = options["speed_kmh"] / 3.6
        interval = options["interval"]
        self.stdout.write(
            self.style.SUCCESS(
                f"Trip {trip.id} started: {route.name} ({trip.direction}), "
                f"{plan.geometry.length_m / 1000:.1f} km at {options['speed_kmh']:.0f} km/h"
            )
        )

        stop_marks = sorted(s.distance_m for s in plan.stops)
        clock = self.clock.t
        distance = 0.0
        dwell_left = 0.0
        next_mark = 0
        while True:
            # Pause at each stop like a real bus, then carry on.
            if dwell_left > 0:
                dwell_left -= interval
            else:
                distance = min(plan.geometry.length_m, distance + speed * interval)
                while next_mark < len(stop_marks) and distance >= stop_marks[next_mark]:
                    distance = stop_marks[next_mark]
                    dwell_left = options["dwell"]
                    next_mark += 1
                    break
            clock += interval
            self.clock.t = clock
            lat, lng = plan.geometry.point_at(distance)
            # A few metres of GPS noise, as real receivers have.
            jitter = 4.0 / 111_320
            lat += random.uniform(-jitter, jitter)
            lng += random.uniform(-jitter, jitter) / max(0.2, math.cos(math.radians(lat)))
            moving = dwell_left <= 0
            fix = {
                "lat": lat,
                "lng": lng,
                "at": clock,
                "speed_mps": speed if moving else 0.0,
                "heading": plan.geometry.bearing_at(distance),
                "accuracy_m": 8.0,
                "client_id": f"sim-{uuid.uuid4()}",
            }
            result = services.ingest_positions(trip, [fix], TripPosition.Source.SIMULATOR)
            trip.refresh_from_db()
            live = services.live_state(trip, now=clock)
            nxt = live["next_stop"]
            label = f"{nxt['name']} in {nxt['eta_seconds'] // 60 if nxt['eta_seconds'] is not None else '?'} min" if nxt else "—"
            events = f"  events: {', '.join(result['events'])}" if result["events"] else ""
            self.stdout.write(f"{distance:7.0f} m  next: {label}{events}")

            finished = distance >= plan.geometry.length_m and dwell_left <= 0
            if finished:
                break
            if not options["fast"]:
                time.sleep(max(0.05, interval / max(options["speedup"], 0.1)))

        if not options["no_end"]:
            services.end_trip(trip, trip.driver, empty_check_confirmed=True)
            self.stdout.write(self.style.SUCCESS("Trip completed (bus-empty check confirmed)."))

    def _reset(self, trip: Trip) -> None:
        from apps.notifications.models import Notification

        TripPosition.objects.filter(trip=trip).delete()
        TripStopEvent.objects.filter(trip=trip).delete()
        BoardingEvent.objects.filter(trip=trip).delete()
        TripIncident.objects.filter(trip=trip).delete()
        TripViewLog.objects.filter(trip=trip).delete()
        Notification.objects.filter(dedupe_key__startswith=f"bus:{trip.id}").delete()
        # Schedule the demo trip for "now" so ETAs and delay checks make sense at any hour.
        now_local = school_now(trip.school)
        Trip.objects.filter(pk=trip.pk).update(
            scheduled_start=now_local.time().replace(second=0, microsecond=0),
            status=Trip.Status.SCHEDULED,
            started_at=None,
            ended_at=None,
            last_position_at=None,
            state={},
            empty_check_confirmed_at=None,
            auto_closed=False,
        )
        trip.refresh_from_db()
