"""Unit tests for staged recipes: parsing, stepping, prompts and ETA."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.cook_planner.planner import (
    ProfileError,
    RecipeRun,
    build_plan,
    load_profiles,
    parse_profiles,
    planned_pit,
    recipe_forecast,
    recipe_tick,
)

TZ = ZoneInfo("Australia/Sydney")
PATH = Path(__file__).parents[1] / "custom_components" / "cook_planner" / "profiles.json"
PROFILES = load_profiles(PATH)
T0 = datetime(2026, 10, 17, 12, 0, tzinfo=TZ)
RECIPES = [(m, c) for m, cuts in PROFILES.cuts.items() for c, p in cuts.items() if p.is_recipe]


def _plan(meat: str, cut: str, kg: float | None = None):
    prof = PROFILES.cut(meat, cut)
    return build_plan(PROFILES, prof, weight_kg=kg or prof.ref_kg, serve_by=T0 + timedelta(days=1), now=T0)


class Cook:
    """Drive a recipe run through time and sensor readings."""

    def __init__(self, plan) -> None:
        self.plan, self.run, self.now = plan, RecipeRun(), T0
        self.p1: float | None = 5.0
        self.p2: float | None = None
        self.pit: float | None = 110.0
        self.events: list = []

    def tick(self, minutes: float = 0, advance: bool = False, **readings):
        self.now += timedelta(minutes=minutes)
        for k, v in readings.items():
            setattr(self, k, v)
        st = recipe_tick(
            self.plan, self.run, now=self.now, probe_c=self.p1, probe2_c=self.p2, pit_c=self.pit, advance=advance
        )
        self.events.extend(st.events)
        return st

    def event_keys(self) -> list[str]:
        return [e.key for e in self.events]


def test_guide_and_house_recipes_are_in_the_menu() -> None:
    ids = sorted(PROFILES.cut(m, c).recipe for m, c in RECIPES)
    assert ids == ["1.1", "1.2", "2.1", "2.2", "2.3", "2.4", "3.1", "3.2", "4.1", "5.1", "5.2"]
    assert PROFILES.meats["sausages"] == "Sausages"
    assert PROFILES.meats["vegetables"] == "Vegetables"


@pytest.mark.parametrize(("meat", "cut"), RECIPES)
def test_recipe_pits_and_pulls_are_safe(meat: str, cut: str) -> None:
    prof = PROFILES.cut(meat, cut)
    assert all(65 <= st.pit_c <= 288 for st in prof.stages)  # Jim Bowie range
    if prof.pull_c is not None and meat in ("chicken", "sausages"):
        assert prof.pull_c >= 74  # poultry / mince food-safety floor


@pytest.mark.parametrize(
    ("meat", "cut", "lo_h", "hi_h"),
    [
        ("chicken", "spatchcock_recipe", 1.3, 1.8),  # 45-60 + 35-45 min
        ("chicken", "smoked_seared_thighs", 1.1, 1.4),
        ("pork_shoulder", "pulled_pork_recipe", 7.5, 10.5),  # 4-6 h + 3-4 h + 0.5 h
        ("pork_shoulder", "spare_ribs_321", 5.0, 6.0),
        ("lamb_shoulder", "lamb_rack_reverse_sear", 1.0, 1.4),
        ("lamb_shoulder", "pulled_lamb_shoulder", 7.0, 8.5),
        ("sausages", "smoked_sausages", 1.1, 1.5),
        ("vegetables", "hasselback_potatoes", 1.3, 1.6),
        ("vegetables", "balsamic_veg", 1.0, 1.3),
        ("pork_shoulder", "rolled_roast_crackle", 4.3, 4.8),  # ~4h35 at 2 kg / 12 cm
        ("pork_shoulder", "rolled_pulled", 6.2, 6.8),  # ~6h30 at 2 kg / 12 cm
    ],
)
def test_planned_time_matches_the_guide(meat: str, cut: str, lo_h: float, hi_h: float) -> None:
    assert lo_h <= _plan(meat, cut).cook_hours <= hi_h


def _rolled(cut: str, kg: float = 2.0, cm: float | None = None):
    prof = PROFILES.cut("pork_shoulder", cut)
    return build_plan(PROFILES, prof, weight_kg=kg, thickness_cm=cm, serve_by=T0 + timedelta(days=1), now=T0)


def test_rolled_roast_time_follows_the_roll_diameter_not_its_length() -> None:
    base = _rolled("rolled_pulled").cook_hours
    assert _rolled("rolled_pulled", cm=12).cook_hours == pytest.approx(base)
    # A longer roll of the same diameter cooks in the same time.
    assert _rolled("rolled_pulled", kg=3, cm=12).cook_hours == pytest.approx(base)
    assert _rolled("rolled_pulled", kg=3).cook_hours > base  # no diameter given: weight decides
    fat, thin = _rolled("rolled_pulled", cm=14), _rolled("rolled_pulled", cm=10)
    assert fat.cook_hours == pytest.approx(base * (14 / 12) ** 1.5)  # all three steps scale
    assert thin.cook_hours == pytest.approx(base * (10 / 12) ** 1.5)
    assert fat.thickness_cm == 14
    assert "2 kg, 14 cm thick" in fat.summary()


def test_diameter_is_clamped_and_ignored_where_it_does_not_apply() -> None:
    base = _rolled("rolled_pulled").cook_hours
    assert _rolled("rolled_pulled", cm=3).cook_hours == pytest.approx(base * 0.5**1.5)
    ribs = _rolled("spare_ribs_321")
    assert _rolled("spare_ribs_321", cm=20).cook_hours == pytest.approx(ribs.cook_hours)
    assert _rolled("spare_ribs_321", cm=20).thickness_cm is None


def test_crackle_step_is_not_scaled_by_size() -> None:
    small, big = _rolled("rolled_roast_crackle", cm=9), _rolled("rolled_roast_crackle", cm=15)
    crackle = lambda p: next(ph for ph in p.phases if ph.key == "crackle")  # noqa: E731
    assert crackle(small).end - crackle(small).start == crackle(big).end - crackle(big).start == timedelta(minutes=20)


def test_rolled_roast_steps_to_crackle_and_pulls_at_90() -> None:
    c = Cook(_rolled("rolled_roast_crackle", cm=12))
    st = c.tick(p1=5, pit=118)
    assert st.pit_c == 120
    st = c.tick(160, p1=65.2)
    assert (c.run.index, st.pit_c) == (1, 160)
    st = c.tick(85, p1=85.4, pit=160)
    assert (c.run.index, st.pit_c) == (2, 250)
    assert "oil and salt" in c.events[-1].message
    c.tick(15, p1=88, pit=245)
    assert "oven grill" in c.events[-1].message
    st = c.tick(3, p1=90.3)
    assert st.ready


def test_rolled_recipes_meet_the_rolled_meat_safety_floor() -> None:
    for cut in ("rolled_roast_crackle", "rolled_pulled"):
        prof = PROFILES.cut("pork_shoulder", cut)
        assert prof.safe_min_c == 75
        assert prof.pull_c >= 88


def test_recipe_plan_phases_follow_the_stages() -> None:
    plan = _plan("pork_shoulder", "pulled_pork_recipe")
    assert [p.key for p in plan.phases] == ["preheat", "bark", "wrap", "tender", "rest"]
    assert [p.pit_c for p in plan.phases] == [105, 105, 125, 125, None]
    assert plan.phases[1].exit == "Probe 72C"
    assert plan.phases[-1].end == plan.serve_by


def test_spatchcock_steps_on_probe_and_waits_for_the_thigh() -> None:
    c = Cook(_plan("chicken", "spatchcock_recipe"))
    st = c.tick()
    assert c.event_keys() == ["s0"]
    assert st.pit_c == 105
    c.tick(30, p1=40)
    assert c.run.index == 0
    st = c.tick(15, p1=48.5)
    assert c.run.index == 1
    assert c.events[-1].message == "Pit up to 190C to render the skin. Don't flip."
    assert st.pit_c == 190
    c.tick(15, p1=71)
    assert c.events[-1].message == "Brush lightly with melted butter or glaze."
    st = c.tick(10, p1=75, p2=78)  # breast done, thigh not yet
    assert not st.ready
    st = c.tick(3, p1=76, p2=80.5)
    assert st.ready
    assert st.advice.startswith("Pull now (76C)")
    c.tick(1)
    assert c.event_keys().count("ready") == 1


def test_pulled_pork_spritz_repeats_every_45_minutes_after_2_hours() -> None:
    c = Cook(_plan("pork_shoulder", "pulled_pork_recipe"))
    c.tick(p1=5)
    for m in range(0, 230, 5):
        c.tick(5, p1=min(60, 5 + m / 4))
    spritz = [k for k in c.event_keys() if k.startswith("s0p0")]
    assert spritz == ["s0p0#0", "s0p0#1", "s0p0#2"]  # 120, 165, 210 min
    st = c.tick(5, p1=72.5)
    assert c.run.index == 1
    assert st.pit_c == 125
    assert "Double-wrap" in c.events[-1].message


def test_timed_ribs_need_no_probe() -> None:
    c = Cook(_plan("pork_shoulder", "spare_ribs_321"))
    c.p1 = None
    c.tick()
    c.tick(179)
    assert c.run.index == 0
    c.tick(1)
    assert c.run.index == 1
    c.tick(105)
    assert c.run.index == 2
    c.tick(30)
    assert "Bend test" in c.events[-1].message
    st = c.tick(10)
    assert st.ready
    assert st.advice == "Pull now: Glaze & set (1 h) done, rest 10 min"


def test_veg_roast_timer_waits_for_the_pit() -> None:
    c = Cook(_plan("vegetables", "balsamic_veg"))
    c.p1 = None
    c.tick(pit=110)
    c.tick(30)
    assert c.run.index == 1
    st = c.tick(6, pit=150)
    assert "waiting for the pit" in st.advice
    c.tick(2, pit=183)  # within 10C of 190: the timer starts
    c.tick(12, pit=190)
    assert c.events[-1].message == "Toss / flip the vegetables."
    st = c.tick(10)
    assert c.run.index == 2


def test_lamb_rack_waits_for_sear_heat_off_the_grill() -> None:
    c = Cook(_plan("lamb_shoulder", "lamb_rack_reverse_sear"))
    c.tick(p1=20, pit=100)
    c.tick(45, p1=48.2)
    assert c.run.index == 1
    assert "tent it with foil" in c.events[-1].message
    st = c.tick(6, pit=200)
    assert "waiting for the pit to reach 245C" in st.advice
    c.tick(6, pit=246)
    assert c.run.index == 2
    st = c.tick(5, p1=55.4)
    assert st.ready


def test_pull_is_never_called_on_time_without_a_probe() -> None:
    plan = _plan("chicken", "smoked_seared_thighs")
    c = Cook(plan)
    c.tick()
    c.tick(45, advance=True)  # cook moves on by hand
    c.tick(20, advance=True)
    assert c.run.index == 2
    st = c.tick(60, p1=None)
    assert not st.ready
    assert "instant-read" in st.advice


def test_next_step_on_the_last_step_marks_ready() -> None:
    c = Cook(_plan("sausages", "smoked_sausages"))
    c.tick()
    for _ in range(3):
        st = c.tick(1, advance=True)
    assert st.ready


def test_restart_does_not_repeat_notifications() -> None:
    c = Cook(_plan("pork_shoulder", "pulled_pork_recipe"))
    c.tick()
    c.tick(125, p1=55)
    saved = c.run.as_dict()
    c.run = RecipeRun.from_dict(json.loads(json.dumps(saved)))
    before = len(c.events)
    c.tick(5, p1=56)
    assert len(c.events) == before


def test_recipe_eta_counts_the_remaining_steps() -> None:
    plan = _plan("pork_shoulder", "spare_ribs_321")
    run = RecipeRun()
    recipe_tick(plan, run, now=T0, probe_c=None, probe2_c=None, pit_c=105)
    fc = recipe_forecast(plan, run, now=T0 + timedelta(minutes=60), meat_on=T0, probe_c=None, rate_c_per_h=None)
    assert fc.eta == T0 + timedelta(minutes=325)
    assert abs(fc.delta_min) < 0.5


def test_planned_pit_follows_the_current_step() -> None:
    plan = _plan("chicken", "spatchcock_recipe")
    run = RecipeRun()
    assert planned_pit(plan, stage="planned", probe_c=None, elapsed_min=None, run=None) is None
    assert planned_pit(plan, stage="preheat", probe_c=None, elapsed_min=None, run=None) == 105
    run.index = 1
    assert planned_pit(plan, stage="cooking", probe_c=50, elapsed_min=60, run=run) == 190
    assert planned_pit(plan, stage="rest", probe_c=None, elapsed_min=None, run=run) is None
    legacy = build_plan(
        PROFILES, PROFILES.cut("chicken", "thigh"), weight_kg=1, serve_by=T0 + timedelta(days=1), now=T0
    )
    assert planned_pit(legacy, stage="cooking", probe_c=50, elapsed_min=30, run=None) == 120
    assert planned_pit(legacy, stage="cooking", probe_c=60, elapsed_min=50, run=None) == 175


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda r: r["stages"][0]["until"].update({"pull": True}) or r["stages"][0]["until"].pop("probe_c"),
            "only the last",
        ),
        (lambda r: r["stages"][-1].update(until={"probe_c": 90}), "last stage must end"),
        (lambda r: r["stages"][0].update(until={"weird": 1}), "until must be one of"),
        (lambda r: r["stages"][0].update(pit_c=320), "65-288C"),
        (lambda r: r["stages"][0]["until"].update(probe_c=80), "must rise"),
        (lambda r: r.update(probe=False), "probe: false"),
        (lambda r: r["stages"][1]["prompts"][0].update(at_min=5), "exactly one"),
    ],
)
def test_bad_recipes_are_rejected(mutate, match: str) -> None:
    raw = json.loads(PATH.read_text())
    recipe = copy.deepcopy(raw["meats"]["chicken"]["cuts"]["spatchcock_recipe"])
    mutate(recipe)
    raw["meats"]["chicken"]["cuts"]["spatchcock_recipe"] = recipe
    with pytest.raises(ProfileError, match=match):
        parse_profiles(raw)
