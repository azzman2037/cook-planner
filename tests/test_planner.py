"""Unit tests for the pure planning model."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.cook_planner.planner import (
    ProfileError,
    build_plan,
    forecast,
    fraction_at_temp,
    load_profiles,
    next_action,
    parse_profiles,
    temp_at_fraction,
)

TZ = ZoneInfo("Australia/Sydney")
PROFILES = load_profiles(
    Path(__file__).parents[1] / "custom_components" / "cook_planner" / "profiles.json"
)
NOW = datetime(2026, 10, 4, 6, 0, tzinfo=TZ)
SERVE = datetime(2026, 10, 4, 18, 0, tzinfo=TZ)
ALL_CUTS = [(m, c) for m, cuts in PROFILES.cuts.items() for c in cuts]


def test_profile_table_covers_requested_meats() -> None:
    assert set(PROFILES.cuts) == {"chicken", "beef_brisket", "pork_shoulder", "lamb_shoulder"}
    assert set(PROFILES.cuts["chicken"]) == {"thigh", "breast", "whole_butterflied", "whole"}
    assert set(PROFILES.cuts["beef_brisket"]) == {"point", "flat", "whole"}


@pytest.mark.parametrize(("meat", "cut"), ALL_CUTS)
def test_every_cut_plans_backwards_from_serve_time(meat: str, cut: str) -> None:
    prof = PROFILES.cut(meat, cut)
    plan = build_plan(PROFILES, prof, weight_kg=prof.ref_kg, serve_by=SERVE, now=NOW - timedelta(days=1))
    assert plan.late_by_min == 0
    assert plan.phases[0].start == plan.start_by
    assert plan.phases[-1].end == SERVE
    for a, b in zip(plan.phases, plan.phases[1:], strict=False):
        assert a.end == b.start
    assert plan.pull_at - plan.meat_on == timedelta(hours=plan.cook_hours)
    # Planned curve ends at pull temp at the planned pull time.
    assert plan.planned_temp_at(plan.pull_at) == pytest.approx(prof.pull_c)


@pytest.mark.parametrize(("meat", "cut"), ALL_CUTS)
def test_pit_recommendations_stay_inside_profile_band(meat: str, cut: str) -> None:
    prof = PROFILES.cut(meat, cut)
    low, high = prof.pit_band_c
    for ph in build_plan(PROFILES, prof, weight_kg=prof.ref_kg, serve_by=SERVE, now=NOW).phases:
        if ph.key in ("preheat", "smoke"):
            assert low <= ph.pit_c <= high


def test_chicken_breast_pulls_at_72_and_checks_carryover() -> None:
    prof = PROFILES.cut("chicken", "breast")
    assert prof.pull_c == 72
    assert prof.carryover_check_c == 74
    plan = build_plan(PROFILES, prof, weight_kg=1, serve_by=SERVE, now=NOW)
    assert "reaches 74C" in plan.phases[-1].action


def test_reference_weight_gives_reference_time() -> None:
    prof = PROFILES.cut("pork_shoulder", "bone_in")
    plan = build_plan(PROFILES, prof, weight_kg=prof.ref_kg, serve_by=SERVE, now=NOW, wrap="none")
    assert plan.cook_hours == pytest.approx(prof.ref_hours)


def test_heavier_takes_longer_but_less_per_kg() -> None:
    prof = PROFILES.cut("beef_brisket", "point")
    small = build_plan(PROFILES, prof, weight_kg=3, serve_by=SERVE, now=NOW, wrap="none")
    big = build_plan(PROFILES, prof, weight_kg=6, serve_by=SERVE, now=NOW, wrap="none")
    assert big.cook_hours > small.cook_hours
    assert big.cook_hours / 6 < small.cook_hours / 3


def test_wrapping_shortens_only_the_post_wrap_part() -> None:
    prof = PROFILES.cut("beef_brisket", "point")
    none = build_plan(PROFILES, prof, weight_kg=4, serve_by=SERVE, now=NOW, wrap="none")
    paper = build_plan(PROFILES, prof, weight_kg=4, serve_by=SERVE, now=NOW, wrap="paper")
    foil = build_plan(PROFILES, prof, weight_kg=4, serve_by=SERVE, now=NOW, wrap="foil")
    assert foil.cook_hours < paper.cook_hours < none.cook_hours
    fw = fraction_at_temp(prof.curve, prof.wrap_at_c)
    assert paper.cook_hours == pytest.approx(none.cook_hours * (fw + (1 - fw) * 0.85))
    # Time to the wrap point is unchanged.
    assert paper.minutes_at_fraction(fw) == pytest.approx(none.minutes_at_fraction(fw))


def test_thickness_is_blended_in() -> None:
    prof = PROFILES.cut("beef_brisket", "flat")
    base = build_plan(PROFILES, prof, weight_kg=2.5, serve_by=SERVE, now=NOW, wrap="none")
    thick = build_plan(PROFILES, prof, weight_kg=2.5, thickness_cm=6, serve_by=SERVE, now=NOW, wrap="none")
    assert thick.cook_hours > base.cook_hours


def test_late_plan_starts_now_and_reports_shortfall() -> None:
    prof = PROFILES.cut("beef_brisket", "whole")
    plan = build_plan(PROFILES, prof, weight_kg=8, serve_by=SERVE, now=NOW)
    assert plan.start_by == NOW
    assert plan.late_by_min > 0
    advice, _ = next_action(plan, stage="planned", probe_c=None, pit_c=None, wrapped=False, fc=None)
    assert "late" in advice


def test_curve_round_trip() -> None:
    curve = PROFILES.cut("beef_brisket", "point").curve
    for t in (10, 40, 65, 70, 80, 94):
        assert temp_at_fraction(curve, fraction_at_temp(curve, t)) == pytest.approx(t)


def _cooking_plan():
    prof = PROFILES.cut("pork_shoulder", "bone_in")
    plan = build_plan(PROFILES, prof, weight_kg=3.5, serve_by=SERVE, now=NOW, wrap="none")
    return prof, plan


def test_forecast_on_plan_lands_on_planned_pull() -> None:
    _prof, plan = _cooking_plan()
    meat_on = plan.meat_on
    when = meat_on + timedelta(hours=3)
    probe = plan.planned_temp_at(when, meat_on)
    fc = forecast(plan, now=when, meat_on=meat_on, probe_c=probe, rate_c_per_h=None)
    assert abs(fc.delta_min) < 1
    assert fc.eta_low < fc.eta < fc.eta_high


def test_forecast_behind_when_probe_lags() -> None:
    _prof, plan = _cooking_plan()
    when = plan.meat_on + timedelta(hours=4)
    lagging = plan.planned_temp_at(plan.meat_on + timedelta(hours=3), plan.meat_on)
    fc = forecast(plan, now=when, meat_on=plan.meat_on, probe_c=lagging, rate_c_per_h=None)
    assert fc.delta_min > 60
    advice, pit = next_action(plan, stage="cooking", probe_c=lagging, pit_c=115, wrapped=False, fc=fc)
    assert "behind" in advice
    assert pit == 125  # +10, inside the 107-135 band


def test_forecast_ahead_suggests_dropping_pit_within_band() -> None:
    prof, plan = _cooking_plan()
    when = plan.meat_on + timedelta(hours=2)
    leading = plan.planned_temp_at(plan.meat_on + timedelta(hours=4), plan.meat_on)
    fc = forecast(plan, now=when, meat_on=plan.meat_on, probe_c=leading, rate_c_per_h=None)
    assert fc.delta_min < -30
    _, pit = next_action(plan, stage="cooking", probe_c=leading, pit_c=115, wrapped=False, fc=fc)
    assert pit == max(115 - 10, prof.pit_band_c[0])


def test_rate_is_used_only_after_the_stall() -> None:
    _prof, plan = _cooking_plan()
    when = plan.meat_on + timedelta(hours=5)
    in_stall = forecast(plan, now=when, meat_on=plan.meat_on, probe_c=70, rate_c_per_h=8)
    assert in_stall.method == "pace"
    assert in_stall.in_stall
    after = forecast(plan, now=when, meat_on=plan.meat_on, probe_c=85, rate_c_per_h=5)
    assert after.method == "pace+rate"


def test_wrap_prompt_then_on_track() -> None:
    prof = PROFILES.cut("beef_brisket", "point")
    plan = build_plan(PROFILES, prof, weight_kg=4, serve_by=SERVE, now=NOW, wrap="paper")
    advice, pit = next_action(plan, stage="cooking", probe_c=72, pit_c=115, wrapped=False, fc=None)
    assert advice.startswith("Wrap now")
    assert pit == 120
    advice, _ = next_action(plan, stage="cooking", probe_c=72, pit_c=120, wrapped=True, fc=None)
    assert not advice.startswith("Wrap now")


def test_chicken_thigh_raises_pit_after_45_minutes() -> None:
    prof = PROFILES.cut("chicken", "thigh")
    plan = build_plan(PROFILES, prof, weight_kg=1.5, serve_by=SERVE, now=NOW)
    _, before = next_action(plan, stage="cooking", probe_c=50, pit_c=120, wrapped=False, fc=None, elapsed_min=30)
    _, after = next_action(plan, stage="cooking", probe_c=60, pit_c=120, wrapped=False, fc=None, elapsed_min=50)
    assert (before, after) == (120, 175)


def test_pull_advice_when_target_reached() -> None:
    prof, plan = _cooking_plan()
    advice, pit = next_action(plan, stage="cooking", probe_c=95.5, pit_c=120, wrapped=False, fc=None)
    assert advice.startswith("Pull now")
    assert pit == prof.hold_pit_c


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(curve=[[0, 4], [0.5, 60]]),
        lambda c: c.update(curve=[[0, 4], [0.5, 70], [0.4, 72], [1, 95]]),
        lambda c: c.update(pull_c=90),
        lambda c: c.update(pit_c=200),
    ],
)
def test_bad_profiles_are_rejected(mutate) -> None:
    import json

    raw = json.loads(
        (Path(__file__).parents[1] / "custom_components" / "cook_planner" / "profiles.json").read_text()
    )
    mutate(raw["meats"]["pork_shoulder"]["cuts"]["bone_in"])
    with pytest.raises(ProfileError):
        parse_profiles(raw)
