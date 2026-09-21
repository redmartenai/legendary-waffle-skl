"""Unit tests for the pure tracking engine. These guard live-tracking behaviour against regressions."""

import pytest

from apps.transport import engine

# A straight-ish 3 km route heading south, with stops at 1 km, 2 km and the end.
PATH = [[17.5000, 78.4000], [17.4910, 78.4000], [17.4820, 78.4000], [17.4730, 78.4000]]


def _plan():
    geometry = engine.RouteGeometry(PATH)
    stops = []
    for index, (lat, lng) in enumerate(PATH[1:], start=1):
        stops.append(
            engine.StopInfo(
                id=f"s{index}", name=f"Stop {index}", lat=lat, lng=lng, order=index - 1,
                distance_m=geometry.project(lat, lng).distance_m, radius_m=75.0, offset_min=index * 4,
            )
        )
    return geometry, stops


def _drive(geometry, stops, distances, start=1_000.0, step=10.0, **fix_kwargs):
    state, events = engine.TripState(), []
    for i, d in enumerate(distances):
        lat, lng = geometry.point_at(d)
        state, new, ok = engine.advance(state, geometry, stops, engine.Fix(lat=lat, lng=lng, at=start + i * step, **fix_kwargs))
        events.extend(new)
    return state, events


def test_geometry_length_and_projection():
    geometry, _ = _plan()
    assert geometry.length_m == pytest.approx(3000, rel=0.01)
    lat, lng = geometry.point_at(1500)
    projection = geometry.project(lat, lng)
    assert projection.distance_m == pytest.approx(1500, abs=1)
    assert projection.offset_m < 1


def test_eta_counts_down_as_the_bus_moves():
    geometry, stops = _plan()
    state, _ = _drive(geometry, stops, [0, 80, 160, 240])
    first = engine.stop_etas(state, stops, 6.0)["s2"]
    state, _ = _drive(geometry, stops, [0, 80, 160, 240, 320, 400, 480])
    later = engine.stop_etas(state, stops, 6.0)["s2"]
    assert later < first
    assert first == pytest.approx((stops[1].distance_m - 240) / engine.eta_speed_mps(state, 6.0) + 30, rel=0.35)


def test_arrival_departure_and_final_arrival_events():
    geometry, stops = _plan()
    distances = [0, 400, 800, 990, 1000, 1010, 1200, 1600, 2000, 2400, 2800, 3000]
    state, events = _drive(geometry, stops, distances)
    kinds = [(e.kind, e.stop_id) for e in events]
    assert ("arrived", "s1") in kinds and ("departed", "s1") in kinds
    assert ("arrived", "s2") in kinds
    assert ("final_arrived", "s3") in kinds
    assert state.final_arrived_at is not None
    assert engine.stop_etas(state, stops, 6.0)["s1"] is None  # reached stops have no ETA


def test_sparse_fixes_that_jump_over_a_stop_still_count_as_arrival():
    geometry, stops = _plan()
    # 300 m jumps skip right over stop 1's 75 m circle.
    state, events = _drive(geometry, stops, [0, 300, 600, 900, 1200, 1500], step=30)
    assert ("arrived", "s1") in [(e.kind, e.stop_id) for e in events]
    assert "s1" not in state.skipped


def test_gps_glitches_are_rejected():
    geometry, stops = _plan()
    state, _ = _drive(geometry, stops, [0, 100])
    # A fix 5 km away one second later is impossible for a bus.
    far = engine.Fix(lat=17.55, lng=78.45, at=state.last_fix_at + 1)
    new, events, ok = engine.advance(state, geometry, stops, far)
    assert ok is False and new is state and events == []
    # Poor accuracy is ignored too.
    lat, lng = geometry.point_at(150)
    _, _, ok = engine.advance(state, geometry, stops, engine.Fix(lat=lat, lng=lng, at=state.last_fix_at + 5, accuracy_m=500))
    assert ok is False
    # So are duplicates and out-of-order fixes from an offline batch.
    _, _, ok = engine.advance(state, geometry, stops, engine.Fix(lat=lat, lng=lng, at=state.last_fix_at))
    assert ok is False


def test_progress_does_not_run_backwards_while_waiting():
    geometry, stops = _plan()
    state, _ = _drive(geometry, stops, [0, 500, 480, 470], step=60)
    assert state.progress_m == pytest.approx(500, abs=2)


def test_speeding_and_off_route_events():
    geometry, stops = _plan()
    _, events = _drive(geometry, stops, [0, 100, 200], speed_mps=15.0)  # 54 km/h
    assert any(e.kind == "speeding" for e in events)

    state, _ = _drive(geometry, stops, [0, 100])
    off = []
    for i in range(3):
        lat, lng = geometry.point_at(150 + i * 30)
        # ~420 m east of the road; 30 s apart so it's a plausible drive, not a GPS glitch.
        state, new, _ = engine.advance(state, geometry, stops, engine.Fix(lat=lat, lng=lng + 0.004, at=state.last_fix_at + 30))
        off.extend(new)
    assert [e.kind for e in off].count("off_route") == 1  # alert once, not on every fix


def test_signal_status():
    state = engine.TripState(last_fix_at=1000.0)
    assert engine.signal_status(state, 1010) == "live"
    assert engine.signal_status(state, 1100) == "weak"
    assert engine.signal_status(state, 1300) == "lost"
    assert engine.signal_status(engine.TripState(), 1300) == "none"


def test_state_round_trips_through_json():
    geometry, stops = _plan()
    state, _ = _drive(geometry, stops, [0, 500, 1000])
    assert engine.TripState.from_dict(state.to_dict()).to_dict() == state.to_dict()
