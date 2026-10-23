"""The shutdown watch, replayed against real GMG shutdowns."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.cook_planner.shutdown import (
    COMPLETE,
    COOLING,
    IDLE,
    INCOMPLETE,
    STOPPING,
    ShutdownWatch,
)

T0 = datetime(2026, 10, 16, 20, 3, 10, tzinfo=timezone(timedelta(hours=11)))


def _run(w: ShutdownWatch, frames: list[tuple[int, str, str, float]]):
    events = []
    for sec, mode, action, pit in frames:
        events += w.update(T0 + timedelta(seconds=sec), mode=mode, action=action, pit_c=pit)
    return events


def test_normal_shutdown_with_fan_cool_down() -> None:
    # 16 Oct: power-off frame reads "off", the fan shows 30 s later and runs 6 min.
    w = ShutdownWatch()
    ev = _run(w, [(0, "heat", "heating", 192.8), (30, "off", "off", 199.4), (60, "off", "fan", 198.3)])
    assert w.state == COOLING
    assert [e.title for e in ev] == ["Cool-down running"]
    ev = _run(w, [(390, "off", "fan", 120.0), (420, "off", "off", 116.7)])
    assert w.state == COMPLETE
    assert [e.title for e in ev] == ["Shutdown complete"]
    assert w.history[-1] == {"at": (T0 + timedelta(seconds=30)).isoformat(), "pit_c": 199.4,
                             "fan_cool_down": True, "fan_min": 6.0}


def test_shutdown_without_fan_cool_down_alerts_once() -> None:
    # 24 Oct: on -> off at a ~117C pit and the fan never ran.
    w = ShutdownWatch()
    ev = _run(w, [(0, "heat", "heating", 123.3), (30, "off", "off", 117.2), (60, "off", "off", 116.1)])
    assert w.state == STOPPING and ev == []
    ev = _run(w, [(120, "off", "off", 115.0), (150, "off", "off", 114.4), (180, "off", "off", 113.9)])
    assert w.state == INCOMPLETE
    assert [e.title for e in ev] == ["Shutdown incomplete"]
    assert "still 115C" in ev[0].message
    assert w.history[-1]["fan_cool_down"] is False and w.history[-1]["pit_c"] == 117.2
    ev = _run(w, [(1800, "off", "off", 64.0)])
    assert w.state == IDLE and ev == []


def test_cool_grill_needs_no_cool_down() -> None:
    w = ShutdownWatch()
    ev = _run(w, [(0, "heat", "heating", 70.0), (30, "off", "off", 60.0), (130, "off", "off", 59.0)])
    assert w.state == COMPLETE and ev == []


def test_offline_decides_nothing_and_relighting_resets() -> None:
    w = ShutdownWatch()
    _run(w, [(0, "heat", "heating", 200.0)])
    assert _run(w, [(30, None, None, None), (300, None, None, None)]) == []
    assert w.state == IDLE and w.was_heating
    _run(w, [(330, "off", "off", 150.0)])
    assert w.state == STOPPING
    _run(w, [(360, "heat", "heating", 150.0)])
    assert w.state == IDLE


def test_round_trip() -> None:
    w = ShutdownWatch()
    _run(w, [(0, "heat", "heating", 199.0), (30, "off", "off", 199.0)])
    w2 = ShutdownWatch.from_dict(w.as_dict())
    assert w2.as_dict() == w.as_dict()
    assert ShutdownWatch.from_dict({"state": "bogus"}).state == IDLE
