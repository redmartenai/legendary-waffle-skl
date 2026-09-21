"""
Live-tracking engine: pure functions, no database access.

Given a route polyline, its stops and a stream of GPS fixes, this module keeps
the bus's progress along the route, detects stop arrivals/departures, filters
GPS glitches and estimates arrival times. Keeping it pure means the whole
tracking behaviour is covered by fast unit tests, so new features elsewhere
can't silently change it.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

EARTH_RADIUS_M = 6_371_000.0
MAX_ACCEPTED_ACCURACY_M = 150.0  # worse fixes are ignored
MAX_PLAUSIBLE_SPEED_MPS = 40.0  # ~144 km/h; faster jumps are GPS glitches
OFF_ROUTE_THRESHOLD_M = 250.0
OFF_ROUTE_STREAK = 3  # consecutive off-route fixes before we raise an alert
DRIVE_THROUGH_MAX_OFFSET_M = 100.0
SPEED_EWMA_ALPHA = 0.35
DEFAULT_DWELL_SECONDS = 30
SIGNAL_LIVE_SECONDS = 30
SIGNAL_WEAK_SECONDS = 120


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


@dataclass(frozen=True)
class Projection:
    distance_m: float  # distance along the route from its start
    offset_m: float  # perpendicular distance from the route


class RouteGeometry:
    """A route polyline in local planar metres (accurate to well under 1% at city scale)."""

    def __init__(self, points):
        pts = [(float(lat), float(lng)) for lat, lng in points]
        if len(pts) < 2:
            raise ValueError("A route needs at least two points.")
        self.points = pts
        mean_lat = sum(p[0] for p in pts) / len(pts)
        self._cos_lat = math.cos(math.radians(mean_lat))
        self._lat0, self._lng0 = pts[0]
        self.xy = [self._to_xy(lat, lng) for lat, lng in pts]
        cumulative = [0.0]
        for (x1, y1), (x2, y2) in zip(self.xy, self.xy[1:]):
            cumulative.append(cumulative[-1] + math.hypot(x2 - x1, y2 - y1))
        self.cumulative = cumulative

    @property
    def length_m(self) -> float:
        return self.cumulative[-1]

    def _to_xy(self, lat: float, lng: float) -> tuple[float, float]:
        x = math.radians(lng - self._lng0) * EARTH_RADIUS_M * self._cos_lat
        y = math.radians(lat - self._lat0) * EARTH_RADIUS_M
        return x, y

    def _from_xy(self, x: float, y: float) -> tuple[float, float]:
        lat = self._lat0 + math.degrees(y / EARTH_RADIUS_M)
        lng = self._lng0 + math.degrees(x / (EARTH_RADIUS_M * self._cos_lat))
        return lat, lng

    def _scan(self, px: float, py: float, lo: float, hi: float) -> Projection | None:
        best: Projection | None = None
        for i in range(len(self.xy) - 1):
            seg_start, seg_end = self.cumulative[i], self.cumulative[i + 1]
            if seg_end < lo or seg_start > hi:
                continue
            (x1, y1), (x2, y2) = self.xy[i], self.xy[i + 1]
            dx, dy = x2 - x1, y2 - y1
            length_sq = dx * dx + dy * dy
            t = 0.0 if length_sq == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length_sq))
            qx, qy = x1 + t * dx, y1 + t * dy
            offset = math.hypot(px - qx, py - qy)
            if best is None or offset < best.offset_m:
                best = Projection(seg_start + t * (seg_end - seg_start), offset)
        return best

    def project(self, lat: float, lng: float, *, near_m: float | None = None) -> Projection:
        """Snap a point to the route. ``near_m`` prefers the stretch just around the last
        known progress, so routes that double back don't snap to the wrong side."""
        px, py = self._to_xy(lat, lng)
        everywhere = (-math.inf, math.inf)
        if near_m is not None:
            nearby = self._scan(px, py, near_m - 300.0, near_m + 3000.0)
            if nearby is not None and nearby.offset_m <= OFF_ROUTE_THRESHOLD_M:
                return nearby
            anywhere = self._scan(px, py, *everywhere)
            if nearby is None:
                return anywhere
            return anywhere if anywhere.offset_m + 50.0 < nearby.offset_m else nearby
        return self._scan(px, py, *everywhere)

    def point_at(self, distance_m: float) -> tuple[float, float]:
        distance_m = max(0.0, min(distance_m, self.length_m))
        lo, hi = 0, len(self.cumulative) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if self.cumulative[mid] <= distance_m:
                lo = mid
            else:
                hi = mid
        seg_len = self.cumulative[hi] - self.cumulative[lo]
        t = 0.0 if seg_len == 0 else (distance_m - self.cumulative[lo]) / seg_len
        (x1, y1), (x2, y2) = self.xy[lo], self.xy[hi]
        return self._from_xy(x1 + t * (x2 - x1), y1 + t * (y2 - y1))

    def bearing_at(self, distance_m: float) -> float:
        a = self.point_at(max(0.0, distance_m - 5))
        b = self.point_at(min(self.length_m, distance_m + 5))
        dx = math.radians(b[1] - a[1]) * math.cos(math.radians(a[0]))
        dy = math.radians(b[0] - a[0])
        return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


@dataclass(frozen=True)
class StopInfo:
    id: str
    name: str
    lat: float
    lng: float
    order: int
    distance_m: float
    radius_m: float
    offset_min: int  # scheduled minutes after the trip's start time


@dataclass(frozen=True)
class Fix:
    lat: float
    lng: float
    at: float  # epoch seconds
    speed_mps: float | None = None
    heading: float | None = None
    accuracy_m: float | None = None


@dataclass(frozen=True)
class Event:
    kind: str  # arrived | departed | skipped | final_arrived | off_route | back_on_route | speeding
    at: float
    stop_id: str | None = None
    value: float | None = None


@dataclass
class TripState:
    progress_m: float = 0.0
    speed_mps: float | None = None
    last_fix_at: float | None = None
    last_lat: float | None = None
    last_lng: float | None = None
    last_speed_kmh: float | None = None
    heading: float | None = None
    offset_m: float = 0.0
    off_route_streak: int = 0
    arrived: dict = field(default_factory=dict)  # stop id -> epoch seconds
    departed: dict = field(default_factory=dict)
    skipped: list = field(default_factory=list)
    final_arrived_at: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> TripState:
        if not data:
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        state = cls(**known)
        state.arrived = dict(state.arrived or {})
        state.departed = dict(state.departed or {})
        state.skipped = list(state.skipped or [])
        return state

    def copy(self) -> TripState:
        return TripState.from_dict(self.to_dict())


def advance(
    state: TripState,
    geometry: RouteGeometry,
    stops: list[StopInfo] | tuple[StopInfo, ...],
    fix: Fix,
    *,
    speed_limit_kmh: float = 40.0,
) -> tuple[TripState, list[Event], bool]:
    """Apply one GPS fix. Returns (new state, events, accepted)."""
    events: list[Event] = []

    if fix.accuracy_m is not None and fix.accuracy_m > MAX_ACCEPTED_ACCURACY_M:
        return state, events, False
    if state.last_fix_at is not None:
        if fix.at <= state.last_fix_at:
            return state, events, False  # duplicate or out-of-order (e.g. a late offline batch)
        dt = fix.at - state.last_fix_at
        jump = haversine_m(state.last_lat, state.last_lng, fix.lat, fix.lng)
        if jump > 100.0 and jump / dt > MAX_PLAUSIBLE_SPEED_MPS:
            return state, events, False  # teleport: a GPS glitch

    first_fix = state.last_fix_at is None
    projection = geometry.project(fix.lat, fix.lng, near_m=None if first_fix else state.progress_m)
    new = state.copy()

    previous_progress = state.progress_m
    progress = projection.distance_m
    if not first_fix and progress < previous_progress:
        progress = previous_progress  # GPS drift backwards while waiting: hold position

    measured_speed = None
    if not first_fix:
        dt = fix.at - state.last_fix_at
        measured_speed = max(0.0, (progress - previous_progress) / dt) if dt > 0 else 0.0
        new.speed_mps = (
            measured_speed
            if state.speed_mps is None
            else SPEED_EWMA_ALPHA * measured_speed + (1 - SPEED_EWMA_ALPHA) * state.speed_mps
        )

    reported = fix.speed_mps if fix.speed_mps is not None else measured_speed
    new.last_speed_kmh = None if reported is None else round(reported * 3.6, 1)
    new.progress_m = progress
    new.last_fix_at = fix.at
    new.last_lat, new.last_lng = fix.lat, fix.lng
    new.heading = fix.heading if fix.heading is not None else geometry.bearing_at(progress)
    new.offset_m = projection.offset_m

    if projection.offset_m > OFF_ROUTE_THRESHOLD_M:
        new.off_route_streak = state.off_route_streak + 1
        if new.off_route_streak == OFF_ROUTE_STREAK:
            events.append(Event("off_route", fix.at, value=round(projection.offset_m)))
    else:
        if state.off_route_streak >= OFF_ROUTE_STREAK:
            events.append(Event("back_on_route", fix.at))
        new.off_route_streak = 0

    if new.last_speed_kmh is not None and new.last_speed_kmh > speed_limit_kmh + 2:
        events.append(Event("speeding", fix.at, value=new.last_speed_kmh))

    last_stop_id = stops[-1].id if stops else None
    on_route = projection.offset_m <= DRIVE_THROUGH_MAX_OFFSET_M
    for stop in stops:
        if stop.id in new.skipped:
            continue
        distance = haversine_m(fix.lat, fix.lng, stop.lat, stop.lng)
        if stop.id not in new.arrived:
            inside = distance <= stop.radius_m
            # Sparse fixes can jump over a stop's circle; crossing it along the route counts too.
            crossed = (not first_fix) and on_route and previous_progress < stop.distance_m <= progress
            if inside or crossed:
                new.arrived[stop.id] = fix.at
                events.append(Event("arrived", fix.at, stop.id))
                if stop.id == last_stop_id:
                    new.final_arrived_at = fix.at
                    events.append(Event("final_arrived", fix.at, stop.id))
                if not inside and distance > stop.radius_m * 1.5:
                    new.departed[stop.id] = fix.at
                    events.append(Event("departed", fix.at, stop.id))
            elif progress > stop.distance_m + max(2 * stop.radius_m, 120.0):
                new.skipped.append(stop.id)
                events.append(Event("skipped", fix.at, stop.id))
        elif stop.id not in new.departed and distance > stop.radius_m * 1.5 and progress > stop.distance_m:
            new.departed[stop.id] = fix.at
            events.append(Event("departed", fix.at, stop.id))

    return new, events, True


def eta_speed_mps(state: TripState, scheduled_speed_mps: float) -> float:
    """Blend live speed with the route's typical speed so a red light doesn't make ETAs explode."""
    live = state.speed_mps
    if live is None or live < 1.5:
        return scheduled_speed_mps
    return max(2.5, min(22.0, 0.6 * live + 0.4 * scheduled_speed_mps))


def stop_etas(
    state: TripState,
    stops,
    scheduled_speed_mps: float,
    dwell_seconds: int = DEFAULT_DWELL_SECONDS,
) -> dict[str, int | None]:
    """Seconds from the last fix to each stop not yet reached (None for reached/skipped stops)."""
    speed = eta_speed_mps(state, scheduled_speed_mps)
    result: dict[str, int | None] = {}
    stops_before = 0
    for stop in stops:
        if stop.id in state.arrived or stop.id in state.skipped:
            result[stop.id] = None
            continue
        remaining = max(0.0, stop.distance_m - state.progress_m)
        result[stop.id] = int(round(remaining / speed + stops_before * dwell_seconds))
        stops_before += 1
    return result


def signal_status(state: TripState, now: float) -> str:
    if state.last_fix_at is None:
        return "none"
    age = now - state.last_fix_at
    if age <= SIGNAL_LIVE_SECONDS:
        return "live"
    if age <= SIGNAL_WEAK_SECONDS:
        return "weak"
    return "lost"
