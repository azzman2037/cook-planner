"""Pure planning and forecasting model for Cook Planner.

No Home Assistant imports: everything here is plain Python so it can be unit
tested in isolation and reused by the Home Assistant entities.

Model
-----
* Cook time (meat on -> pull) for a cut is ``ref_hours`` at ``ref_kg``, scaled by
  ``(weight / ref_kg) ** weight_exponent``. An exponent below 1 reflects that
  heat has to travel through thickness, not mass: a 6 kg brisket is not twice
  the cook of a 3 kg one. When a thickness is supplied and the profile has a
  reference thickness, a thickness estimate ``(t / ref_t) ** 1.5`` is averaged
  in.
* ``curve`` is the planned probe temperature against fraction of the unwrapped
  cook. Wrapping compresses everything after the wrap point by the wrap
  method's ``post_wrap_time_factor``.
* Live ETA blends "pace" (how far behind/ahead of the planned curve we are,
  extrapolated) with the measured rate of rise once the meat is out of the
  stall, where the rate becomes a reliable predictor.

All of these numbers are starting assumptions; the cook log exists so they can
be re-fitted from real cooks.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

THICKNESS_EXPONENT = 1.5
MIN_PACE = 0.5
MAX_PACE = 2.5
PACE_MIN_ELAPSED_MIN = 20.0
RATE_MIN_C_PER_H = 1.0
BEHIND_ACTION_MIN = 20
AHEAD_ACTION_MIN = 30
PIT_NUDGE_C = 10
ETA_RANGE = 0.15
FLIP_WINDOW_MIN = 5
# Recipes
PIT_READY_MARGIN_C = 10  # a stage timer that waits for heat starts this close to the set temp
HEAT_WAIT_MAX_MIN = 20  # ...or after this long, whatever the pit says
HEAT_UP_EST_MIN = 10  # planned allowance for the pit to climb before a heat-gated timer
PROMPT_SHOW_MIN = 5  # a fresh prompt stays the headline advice this long
UNTIL_KINDS = ("probe_c", "minutes", "pit_c", "pull")


class ProfileError(ValueError):
    """Raised when profiles.json is malformed."""


@dataclass(frozen=True)
class WrapMethod:
    """A wrap option and its effect on post-wrap cook time."""

    key: str
    name: str
    post_wrap_time_factor: float


@dataclass(frozen=True)
class RecipePrompt:
    """A reminder inside a recipe stage.

    Fires once at ``at_min`` (minutes into the stage) or when probe 1 reaches
    ``at_probe_c``; or repeatedly every ``every_min`` starting at ``from_min``.
    """

    text: str
    at_min: float | None = None
    every_min: float | None = None
    from_min: float = 0.0
    at_probe_c: float | None = None


@dataclass(frozen=True)
class RecipeStage:
    """One step of a recipe: a pit temperature held until an exit trigger."""

    key: str
    name: str
    icon: str
    pit_c: float
    until: str  # one of UNTIL_KINDS
    until_value: float | None
    minutes: float  # planned length at the reference weight
    scale: bool  # stretch ``minutes`` with weight
    enter: str  # what to do when the stage starts
    prompts: tuple[RecipePrompt, ...] = ()
    timer_from_pit: bool = False  # stage clock waits for the pit to come up to heat
    wrap: bool = False  # entering this stage counts as "wrapped"


@dataclass(frozen=True)
class CutProfile:
    """Everything the planner needs to know about one meat + cut."""

    meat_key: str
    meat_name: str
    key: str
    name: str
    pit_c: float
    pit_band_c: tuple[float, float]
    finish_pit_c: float | None
    finish_after_min: float | None
    turn_after_min: float | None
    wrap_default: str
    wrap_at_c: float | None
    pull_c: float | None
    carryover_check_c: float | None
    safe_min_c: float
    probe_1_role: str
    probe_2_role: str | None
    probe_2_target_c: float | None
    rest_min: float
    hold_pit_c: float | None
    ref_kg: float
    ref_hours: float
    weight_exponent: float
    ref_thickness_cm: float | None
    curve: tuple[tuple[float, float], ...]
    stall_c: tuple[float, float] | None
    notes: str
    recipe: str | None = None
    pellets: str = ""
    prep: str = ""
    rest_note: str = ""
    uses_probe: bool = True
    stages: tuple[RecipeStage, ...] = ()

    @property
    def is_recipe(self) -> bool:
        """Return True for a staged recipe profile."""
        return bool(self.stages)

    @property
    def full_key(self) -> str:
        """Return ``meat.cut``."""
        return f"{self.meat_key}.{self.key}"

    @property
    def label(self) -> str:
        """Return a human label such as ``Beef brisket - Point end (deckle)``."""
        return f"{self.meat_name} - {self.name}"


@dataclass(frozen=True)
class Profiles:
    """The full profile table."""

    meats: dict[str, str]
    cuts: dict[str, dict[str, CutProfile]]
    wrap_methods: dict[str, WrapMethod]
    preheat_min: float

    def cut(self, meat: str, cut: str) -> CutProfile:
        """Return one cut profile."""
        return self.cuts[meat][cut]


def _req(data: dict[str, Any], key: str, where: str) -> Any:
    if key not in data:
        raise ProfileError(f"{where}: missing '{key}'")
    return data[key]


def parse_profiles(raw: dict[str, Any]) -> Profiles:
    """Validate and parse the profiles document."""
    wraps = {
        key: WrapMethod(key, w["name"], float(w["post_wrap_time_factor"]))
        for key, w in _req(raw, "wrap_methods", "root").items()
    }
    if "none" not in wraps:
        raise ProfileError("wrap_methods must include 'none'")
    meats: dict[str, str] = {}
    cuts: dict[str, dict[str, CutProfile]] = {}
    for meat_key, meat in _req(raw, "meats", "root").items():
        meats[meat_key] = meat["name"]
        cuts[meat_key] = {}
        for cut_key, c in _req(meat, "cuts", meat_key).items():
            where = f"{meat_key}.{cut_key}"
            if "stages" in c:
                cuts[meat_key][cut_key] = _parse_recipe(meat_key, meat["name"], cut_key, c, where)
                continue
            curve = tuple((float(f), float(t)) for f, t in _req(c, "curve", where))
            _validate_curve(curve, float(_req(c, "pull_c", where)), where)
            wrap = c.get("wrap")
            if wrap is not None and wrap.get("default", "none") not in wraps:
                raise ProfileError(f"{where}: unknown wrap default {wrap.get('default')}")
            band = tuple(float(x) for x in _req(c, "pit_band_c", where))
            pit = float(_req(c, "pit_c", where))
            if not band[0] <= pit <= band[1]:
                raise ProfileError(f"{where}: pit_c {pit} outside pit_band_c {band}")
            time = _req(c, "time", where)
            p2 = c.get("probe_2")
            stall = c.get("stall_c")
            cuts[meat_key][cut_key] = CutProfile(
                meat_key=meat_key,
                meat_name=meat["name"],
                key=cut_key,
                name=c["name"],
                pit_c=pit,
                pit_band_c=(band[0], band[1]),
                finish_pit_c=_opt_float(c.get("finish_pit_c")),
                finish_after_min=_opt_float(c.get("finish_after_min")),
                turn_after_min=_opt_float(c.get("turn_after_min")),
                wrap_default=(wrap or {}).get("default", "none"),
                wrap_at_c=_opt_float((wrap or {}).get("at_c")),
                pull_c=float(c["pull_c"]),
                carryover_check_c=_opt_float(c.get("carryover_check_c")),
                safe_min_c=float(c.get("safe_min_c", 63)),
                probe_1_role=c.get("probe_1_role", "Thickest part"),
                probe_2_role=(p2 or {}).get("role"),
                probe_2_target_c=_opt_float((p2 or {}).get("target_c")),
                rest_min=float(c.get("rest_min", 0)),
                hold_pit_c=_opt_float(c.get("hold_pit_c")),
                ref_kg=float(_req(time, "ref_kg", where)),
                ref_hours=float(_req(time, "ref_hours", where)),
                weight_exponent=float(time.get("weight_exponent", 0.67)),
                ref_thickness_cm=_opt_float(time.get("ref_thickness_cm")),
                curve=curve,
                stall_c=(float(stall[0]), float(stall[1])) if stall else None,
                notes=c.get("notes", ""),
            )
    return Profiles(meats, cuts, wraps, float(raw.get("preheat_min", 20)))


def load_profiles(path: Path) -> Profiles:
    """Load and validate a profiles JSON file."""
    return parse_profiles(json.loads(path.read_text(encoding="utf-8")))


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _validate_curve(curve: tuple[tuple[float, float], ...], pull_c: float, where: str) -> None:
    if len(curve) < 2 or curve[0][0] != 0 or curve[-1][0] != 1:
        raise ProfileError(f"{where}: curve must start at fraction 0 and end at 1")
    for (f1, t1), (f2, t2) in itertools.pairwise(curve):
        if f2 <= f1 or t2 < t1:
            raise ProfileError(f"{where}: curve must increase in time and not fall in temp")
    if curve[-1][1] != pull_c:
        raise ProfileError(f"{where}: curve must end at pull_c ({pull_c})")


# ---------------------------------------------------------------------------
# Curve helpers
# ---------------------------------------------------------------------------


def temp_at_fraction(curve: tuple[tuple[float, float], ...], frac: float) -> float:
    """Planned probe temperature at a fraction of the unwrapped cook."""
    frac = min(max(frac, 0.0), 1.0)
    for (f1, t1), (f2, t2) in itertools.pairwise(curve):
        if frac <= f2:
            return t1 + (t2 - t1) * (frac - f1) / (f2 - f1)
    return curve[-1][1]


def fraction_at_temp(curve: tuple[tuple[float, float], ...], temp: float) -> float:
    """Inverse of :func:`temp_at_fraction` (first fraction reaching ``temp``)."""
    if temp <= curve[0][1]:
        return 0.0
    for (f1, t1), (f2, t2) in itertools.pairwise(curve):
        if temp <= t2:
            if t2 == t1:
                return f1
            return f1 + (f2 - f1) * (temp - t1) / (t2 - t1)
    return 1.0


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Phase:
    """One step of the cook schedule."""

    key: str
    name: str
    start: datetime
    end: datetime
    pit_c: float | None
    exit: str
    action: str
    icon: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Serialise for state attributes and storage."""
        return {
            "key": self.key,
            "icon": self.icon or self.key,
            "name": self.name,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "pit_c": self.pit_c,
            "exit": self.exit,
            "action": self.action,
        }


@dataclass(frozen=True)
class Plan:
    """A generated cook plan."""

    profile: CutProfile
    weight_kg: float
    thickness_cm: float | None
    wrap: str
    serve_by: datetime
    created: datetime
    unwrapped_hours: float
    wrap_fraction: float | None
    wrap_factor: float
    cook_hours: float
    start_by: datetime
    meat_on: datetime
    pull_at: datetime
    late_by_min: float
    phases: tuple[Phase, ...] = field(default_factory=tuple)
    stage_minutes: tuple[float, ...] = ()

    @property
    def cook_minutes(self) -> float:
        """Planned meat-on to pull, minutes."""
        return self.cook_hours * 60

    def minutes_at_fraction(self, frac: float) -> float:
        """Planned minutes after meat-on to reach a curve fraction."""
        frac = min(max(frac, 0.0), 1.0)
        unwrapped = self.unwrapped_hours * 60
        fw = self.wrap_fraction
        if fw is None or frac <= fw:
            return frac * unwrapped
        return fw * unwrapped + (frac - fw) * unwrapped * self.wrap_factor

    def planned_temp_at(self, when: datetime, meat_on: datetime | None = None) -> float:
        """Planned probe temperature at a wall-clock time."""
        start = meat_on or self.meat_on
        minutes = (when - start).total_seconds() / 60
        if minutes <= 0:
            return self.profile.curve[0][1]
        unwrapped = self.unwrapped_hours * 60
        fw = self.wrap_fraction
        if fw is None or minutes <= fw * unwrapped:
            frac = minutes / unwrapped
        else:
            frac = fw + (minutes - fw * unwrapped) / (unwrapped * self.wrap_factor)
        return temp_at_fraction(self.profile.curve, frac)

    def curve_points(self, meat_on: datetime | None = None, step_min: int = 15) -> list[list[Any]]:
        """Planned probe curve as ``[iso_time, temp]`` points (for charts)."""
        start = meat_on or self.meat_on
        total = int(self.cook_minutes)
        points = []
        for m in [*range(0, total, step_min), total]:
            when = start + timedelta(minutes=m)
            points.append([when.isoformat(), round(self.planned_temp_at(when, start), 1)])
        return points

    def summary(self) -> str:
        """One-line description for a sensor state (max 255 chars)."""
        hrs = self.cook_hours
        h, m = int(hrs), round((hrs - int(hrs)) * 60)
        if m == 60:
            h, m = h + 1, 0
        return f"{self.profile.label} {self.weight_kg:g} kg - {h}h{m:02d} cook"

    def as_dict(self) -> dict[str, Any]:
        """Serialise for storage / attributes."""
        return {
            "meat": self.profile.meat_key,
            "cut": self.profile.key,
            "label": self.profile.label,
            "weight_kg": self.weight_kg,
            "thickness_cm": self.thickness_cm,
            "wrap": self.wrap,
            "serve_by": self.serve_by.isoformat(),
            "created": self.created.isoformat(),
            "cook_hours": round(self.cook_hours, 2),
            "start_by": self.start_by.isoformat(),
            "meat_on": self.meat_on.isoformat(),
            "pull_at": self.pull_at.isoformat(),
            "late_by_min": round(self.late_by_min),
            "pit_c": self.profile.pit_c,
            "pull_c": self.profile.pull_c,
            "probe_1_role": self.profile.probe_1_role,
            "probe_2_role": self.profile.probe_2_role,
            "probe_2_target_c": self.profile.probe_2_target_c,
            "phases": [p.as_dict() for p in self.phases],
            "recipe": self.profile.recipe,
            "pellets": self.profile.pellets or None,
            "prep": self.profile.prep or None,
            "rest_note": self.profile.rest_note or None,
            "uses_probe": self.profile.uses_probe,
            "steps": len(self.profile.stages) or None,
        }


def estimate_unwrapped_hours(profile: CutProfile, weight_kg: float, thickness_cm: float | None) -> float:
    """Meat-on to pull, unwrapped, at the base pit temperature."""
    if weight_kg <= 0:
        raise ValueError("weight must be positive")
    by_weight = profile.ref_hours * (weight_kg / profile.ref_kg) ** profile.weight_exponent
    if thickness_cm and profile.ref_thickness_cm:
        by_thick = profile.ref_hours * (thickness_cm / profile.ref_thickness_cm) ** THICKNESS_EXPONENT
        return (by_weight + by_thick) / 2
    return by_weight


def build_plan(
    profiles: Profiles,
    profile: CutProfile,
    *,
    weight_kg: float,
    serve_by: datetime,
    now: datetime,
    wrap: str | None = None,
    thickness_cm: float | None = None,
) -> Plan:
    """Build a schedule working backwards from ``serve_by``."""
    if profile.is_recipe:
        return _build_recipe_plan(profiles, profile, weight_kg=weight_kg, serve_by=serve_by, now=now)
    wrap_key = wrap or profile.wrap_default
    if wrap_key not in profiles.wrap_methods:
        raise ValueError(f"unknown wrap method {wrap_key}")
    if profile.wrap_at_c is None:
        wrap_key = "none"
    unwrapped = estimate_unwrapped_hours(profile, weight_kg, thickness_cm)
    factor = profiles.wrap_methods[wrap_key].post_wrap_time_factor
    fw = fraction_at_temp(profile.curve, profile.wrap_at_c) if wrap_key != "none" else None
    cook = unwrapped * (fw + (1 - fw) * factor) if fw is not None else unwrapped

    preheat = timedelta(minutes=profiles.preheat_min)
    rest = timedelta(minutes=profile.rest_min)
    cook_td = timedelta(hours=cook)
    start_by = serve_by - rest - cook_td - preheat
    late_by = max(0.0, (now - start_by).total_seconds() / 60)
    if late_by > 0:
        start_by = now
    meat_on = start_by + preheat
    pull_at = meat_on + cook_td

    plan = Plan(
        profile=profile,
        weight_kg=weight_kg,
        thickness_cm=thickness_cm,
        wrap=wrap_key,
        serve_by=serve_by,
        created=now,
        unwrapped_hours=unwrapped,
        wrap_fraction=fw,
        wrap_factor=factor,
        cook_hours=cook,
        start_by=start_by,
        meat_on=meat_on,
        pull_at=pull_at,
        late_by_min=late_by,
    )
    return _with_phases(plan, profiles)


def _with_phases(plan: Plan, profiles: Profiles) -> Plan:
    p = plan.profile
    phases: list[Phase] = [
        Phase(
            "preheat",
            "Preheat",
            plan.start_by,
            plan.meat_on,
            p.pit_c,
            f"Pit within 8C of {p.pit_c:g}C",
            f"Light the smoker and set {p.pit_c:g}C",
        )
    ]
    # When does the pit change for the finish?
    change_at: datetime | None = None
    change_reason = ""
    if p.finish_pit_c is not None and p.finish_after_min is not None:
        change_at = plan.meat_on + timedelta(minutes=min(p.finish_after_min, plan.cook_minutes))
        change_reason = f"After {p.finish_after_min:g} min"
    elif p.wrap_at_c is not None:
        frac = fraction_at_temp(p.curve, p.wrap_at_c)
        change_at = plan.meat_on + timedelta(minutes=plan.minutes_at_fraction(frac))
        change_reason = f"Probe {p.wrap_at_c:g}C"

    smoke_end = change_at or plan.pull_at
    turn_at = plan.meat_on + timedelta(minutes=p.turn_after_min) if p.turn_after_min is not None else None
    if turn_at is not None and turn_at < smoke_end:
        phases.append(
            Phase(
                "smoke",
                "Cook side 1",
                plan.meat_on,
                turn_at,
                p.pit_c,
                f"After {p.turn_after_min:g} min",
                f"Meat on, probe 1 in the {p.probe_1_role.lower()}",
            )
        )
        phases.append(
            Phase(
                "turn",
                "Flip & cook side 2",
                turn_at,
                smoke_end,
                p.pit_c,
                change_reason or f"Probe {p.pull_c:g}C",
                "Flip the pieces (re-seat the probe in the thickest one)",
            )
        )
    else:
        phases.append(
            Phase(
                "smoke",
                "Smoke",
                plan.meat_on,
                smoke_end,
                p.pit_c,
                change_reason or f"Probe {p.pull_c:g}C",
                f"Meat on, probe 1 in the {p.probe_1_role.lower()}",
            )
        )
    if change_at is not None and change_at < plan.pull_at:
        wrap_name = profiles.wrap_methods[plan.wrap].name
        if plan.wrap != "none":
            name, action = "Wrap & finish", f"Wrap in {wrap_name.lower()}"
        elif p.finish_after_min is not None:
            name, action = "Finish", "Raise the pit to crisp and finish"
        else:
            name, action = "Push through stall", "Raise the pit to push through the stall"
        finish_pit = p.finish_pit_c if p.finish_pit_c is not None else p.pit_c
        if finish_pit != p.pit_c:
            action += f", set {finish_pit:g}C"
        phases.append(Phase("finish", name, change_at, plan.pull_at, finish_pit, f"Probe {p.pull_c:g}C", action))
    rest_action = f"Pull at {p.pull_c:g}C and rest {p.rest_min:g} min"
    if p.hold_pit_c is not None:
        rest_action += f" (hold at {p.hold_pit_c:g}C)"
    if p.carryover_check_c is not None:
        rest_action += f"; check it reaches {p.carryover_check_c:g}C"
    phases.append(
        Phase(
            "rest",
            "Rest",
            plan.pull_at,
            plan.pull_at + timedelta(minutes=p.rest_min),
            p.hold_pit_c,
            f"{p.rest_min:g} min",
            rest_action,
        )
    )
    return Plan(**{**plan.__dict__, "phases": tuple(phases)})


# ---------------------------------------------------------------------------
# Live forecast
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Forecast:
    """Live estimate of when the meat reaches pull temperature."""

    eta: datetime
    eta_low: datetime
    eta_high: datetime
    delta_min: float
    progress: float
    method: str
    in_stall: bool


def forecast(
    plan: Plan,
    *,
    now: datetime,
    meat_on: datetime,
    probe_c: float,
    rate_c_per_h: float | None,
) -> Forecast:
    """Estimate pull time from the live probe reading.

    ``delta_min`` is positive when running behind the planned pull time.
    """
    p = plan.profile
    planned_pull = meat_on + timedelta(minutes=plan.cook_minutes)
    if probe_c >= p.pull_c:
        return Forecast(now, now, now, (now - planned_pull).total_seconds() / 60, 1.0, "done", False)

    frac = fraction_at_temp(p.curve, probe_c)
    planned_elapsed = plan.minutes_at_fraction(frac)
    elapsed = max((now - meat_on).total_seconds() / 60, 0.0)
    if planned_elapsed >= PACE_MIN_ELAPSED_MIN and elapsed >= PACE_MIN_ELAPSED_MIN:
        pace = min(max(elapsed / planned_elapsed, MIN_PACE), MAX_PACE)
    else:
        pace = 1.0
    remaining = (plan.cook_minutes - planned_elapsed) * pace
    eta = now + timedelta(minutes=remaining)
    method = "pace"

    in_stall = bool(p.stall_c and p.stall_c[0] <= probe_c <= p.stall_c[1])
    past_stall = p.stall_c is None or probe_c > p.stall_c[1]
    if past_stall and rate_c_per_h is not None and rate_c_per_h >= RATE_MIN_C_PER_H and probe_c > 40:
        by_rate = now + timedelta(hours=(p.pull_c - probe_c) / rate_c_per_h)
        eta = eta + (by_rate - eta) / 2
        method = "pace+rate"

    spread = max((eta - now) * ETA_RANGE, timedelta(minutes=5))
    delta = (eta - planned_pull).total_seconds() / 60
    progress = planned_elapsed / plan.cook_minutes if plan.cook_minutes else 1.0
    return Forecast(eta, eta - spread, eta + spread, delta, min(progress, 1.0), method, in_stall)


def next_action(
    plan: Plan,
    *,
    stage: str,
    probe_c: float | None,
    pit_c: float | None,
    wrapped: bool,
    fc: Forecast | None,
    elapsed_min: float | None = None,
) -> tuple[str, float | None]:
    """Return (advice text, recommended pit setpoint)."""
    p = plan.profile
    low, high = p.pit_band_c
    if stage == "planned":
        late = f" (already {plan.late_by_min:.0f} min late for the serve time)" if plan.late_by_min else ""
        return f"Light the smoker at {plan.start_by:%H:%M} and set {p.pit_c:g}C{late}", p.pit_c
    if stage == "preheat":
        if pit_c is not None and pit_c >= p.pit_c - 8:
            return f"Pit is up to temp: meat on, probe 1 in the {p.probe_1_role.lower()}", p.pit_c
        return f"Preheating to {p.pit_c:g}C", p.pit_c
    if stage == "rest":
        if p.carryover_check_c is not None and probe_c is not None and probe_c < p.carryover_check_c:
            return f"Resting: confirm carryover reaches {p.carryover_check_c:g}C", p.hold_pit_c
        return f"Resting, serve at {plan.serve_by:%H:%M}", p.hold_pit_c
    if stage == "done":
        return "Cook finished", None
    if probe_c is None:
        return "Probe 1 not reading: check it is plugged in", None

    pit = p.pit_c
    finish = next((ph for ph in plan.phases if ph.key == "finish"), None)
    if finish is not None and finish.pit_c is not None:
        by_temp = p.finish_after_min is None and p.wrap_at_c is not None and probe_c >= p.wrap_at_c
        by_time = p.finish_after_min is not None and elapsed_min is not None and elapsed_min >= p.finish_after_min
        if by_temp or by_time:
            pit = finish.pit_c
    if probe_c >= p.pull_c:
        return f"Pull now ({probe_c:.0f}C) and rest {p.rest_min:g} min", p.hold_pit_c
    if (
        p.turn_after_min is not None
        and elapsed_min is not None
        and p.turn_after_min <= elapsed_min < p.turn_after_min + FLIP_WINDOW_MIN
    ):
        return f"Flip now ({elapsed_min:.0f} min in), probe {probe_c:.0f}C", pit
    if plan.wrap != "none" and not wrapped and p.wrap_at_c is not None and probe_c >= p.wrap_at_c:
        return f"Wrap now in {plan.wrap} (probe {probe_c:.0f}C)", pit
    if fc is not None and fc.delta_min >= BEHIND_ACTION_MIN and pit < high:
        new = min(pit + PIT_NUDGE_C, high)
        return f"{fc.delta_min:.0f} min behind: raise pit to {new:g}C", new
    if fc is not None and -fc.delta_min >= AHEAD_ACTION_MIN and pit > low:
        new = max(pit - PIT_NUDGE_C, low)
        return f"{-fc.delta_min:.0f} min ahead: drop pit to {new:g}C or plan a longer rest", new
    if fc is not None and fc.in_stall:
        return f"In the stall ({probe_c:.0f}C): hold steady", pit
    return f"On track: pit {pit:g}C, pull at {p.pull_c:g}C", pit


# ---------------------------------------------------------------------------
# Staged recipes
# ---------------------------------------------------------------------------
#
# A recipe profile replaces the single smoke/finish/wrap model with an ordered
# list of stages. Each stage holds a pit temperature until its exit trigger:
#
#   {"probe_c": 48}   probe 1 reaches 48C (falls back to the planned time if no probe)
#   {"minutes": 180}  the stage clock reaches 180 min (timed recipes, no probe needed)
#   {"pit_c": 245}    the pit reaches 245C (e.g. waiting for sear heat)
#   {"pull": true}    final stage: probe 1 at pull_c (and probe 2 at its target)
#
# The runtime (``recipe_tick``) is pure: the manager owns a ``RecipeRun`` and
# turns the returned events into notifications.


def _parse_recipe(meat_key: str, meat_name: str, cut_key: str, c: dict[str, Any], where: str) -> CutProfile:
    uses_probe = bool(c.get("probe", True))
    pull_c = _opt_float(c.get("pull_c"))
    stages: list[RecipeStage] = []
    raw_stages = _req(c, "stages", where)
    if not raw_stages:
        raise ProfileError(f"{where}: a recipe needs at least one stage")
    for i, s in enumerate(raw_stages):
        sw = f"{where}.stages[{i}]"
        until = _req(s, "until", sw)
        if not isinstance(until, dict) or len(until) != 1 or next(iter(until)) not in UNTIL_KINDS:
            raise ProfileError(f"{sw}: until must be one of {UNTIL_KINDS}")
        kind, value = next(iter(until.items()))
        last = i == len(raw_stages) - 1
        if kind == "pull" and not last:
            raise ProfileError(f"{sw}: only the last stage can end at the pull")
        if last and kind not in ("pull", "minutes"):
            raise ProfileError(f"{sw}: the last stage must end at the pull or after a time")
        if kind in ("probe_c", "pull") and not uses_probe:
            raise ProfileError(f"{sw}: probe trigger in a recipe marked probe: false")
        if kind == "pull" and pull_c is None:
            raise ProfileError(f"{sw}: pull stage needs pull_c")
        pit = float(_req(s, "pit_c", sw))
        if not 65 <= pit <= 288:
            raise ProfileError(f"{sw}: pit_c {pit} outside the grill's 65-288C range")
        prompts = []
        for j, pr in enumerate(s.get("prompts", [])):
            pw = f"{sw}.prompts[{j}]"
            prompt = RecipePrompt(
                text=_req(pr, "text", pw),
                at_min=_opt_float(pr.get("at_min")),
                every_min=_opt_float(pr.get("every_min")),
                from_min=float(pr.get("from_min", 0)),
                at_probe_c=_opt_float(pr.get("at_probe_c")),
            )
            if sum(x is not None for x in (prompt.at_min, prompt.every_min, prompt.at_probe_c)) != 1:
                raise ProfileError(f"{pw}: give exactly one of at_min, every_min, at_probe_c")
            if prompt.at_probe_c is not None and not uses_probe:
                raise ProfileError(f"{pw}: probe prompt in a recipe marked probe: false")
            prompts.append(prompt)
        stages.append(
            RecipeStage(
                key=s.get("key", f"step{i + 1}"),
                name=_req(s, "name", sw),
                icon=s.get("icon", "smoke"),
                pit_c=pit,
                until=kind,
                until_value=None if kind == "pull" else float(value),
                minutes=float(s.get("minutes", value if kind == "minutes" else 0)),
                scale=bool(s.get("scale", kind in ("probe_c", "pull"))),
                enter=_req(s, "enter", sw),
                prompts=tuple(prompts),
                timer_from_pit=bool(s.get("timer_from_pit", False)),
                wrap=bool(s.get("wrap", False)),
            )
        )
        if stages[-1].minutes <= 0:
            raise ProfileError(f"{sw}: needs planned minutes")

    # Probe targets must rise through the recipe.
    targets = [st.until_value for st in stages if st.until == "probe_c"] + (
        [pull_c] if stages[-1].until == "pull" else []
    )
    if any(b <= a for a, b in itertools.pairwise(targets)):
        raise ProfileError(f"{where}: probe targets must rise from stage to stage")

    curve = _recipe_curve(stages, pull_c) if uses_probe and pull_c is not None else ((0.0, 4.0), (1.0, 4.0))
    time = c.get("time", {})
    p2 = c.get("probe_2")
    pits = [st.pit_c for st in stages]
    return CutProfile(
        meat_key=meat_key,
        meat_name=meat_name,
        key=cut_key,
        name=c["name"],
        pit_c=pits[0],
        pit_band_c=(min(pits), max(pits)),
        finish_pit_c=None,
        finish_after_min=None,
        turn_after_min=None,
        wrap_default="none",
        wrap_at_c=None,
        pull_c=pull_c,
        carryover_check_c=_opt_float(c.get("carryover_check_c")),
        safe_min_c=float(c.get("safe_min_c", 63)),
        probe_1_role=c.get("probe_1_role", "Thickest part"),
        probe_2_role=(p2 or {}).get("role"),
        probe_2_target_c=_opt_float((p2 or {}).get("target_c")),
        rest_min=float(c.get("rest_min", 0)),
        hold_pit_c=_opt_float(c.get("hold_pit_c")),
        ref_kg=float(time.get("ref_kg", 1.0)),
        ref_hours=sum(st.minutes for st in stages) / 60,
        weight_exponent=float(time.get("weight_exponent", 0.67)),
        ref_thickness_cm=None,
        curve=curve,
        stall_c=(float(c["stall_c"][0]), float(c["stall_c"][1])) if c.get("stall_c") else None,
        notes=c.get("notes", ""),
        recipe=str(c.get("recipe", "")) or cut_key,
        pellets=c.get("pellets", ""),
        prep=c.get("prep", ""),
        rest_note=c.get("rest_note", ""),
        uses_probe=uses_probe,
        stages=tuple(stages),
    )


def _recipe_curve(stages: list[RecipeStage], pull_c: float) -> tuple[tuple[float, float], ...]:
    """Planned probe curve from the stage targets, at the reference weight."""
    total = sum(st.minutes for st in stages)
    points: list[tuple[float, float]] = [(0.0, 4.0)]
    done = 0.0
    for st in stages:
        done += st.minutes
        frac = round(done / total, 4)
        if st.until == "probe_c" and frac < 1:
            points.append((frac, st.until_value))
    points.append((1.0, pull_c))
    return tuple(points)


def recipe_stage_minutes(profile: CutProfile, weight_kg: float) -> tuple[float, ...]:
    """Planned minutes for each stage at this weight (heat-up allowance included)."""
    if weight_kg <= 0:
        raise ValueError("weight must be positive")
    factor = (weight_kg / profile.ref_kg) ** profile.weight_exponent
    return tuple(
        st.minutes * (factor if st.scale else 1.0) + (HEAT_UP_EST_MIN if st.timer_from_pit else 0.0)
        for st in profile.stages
    )


def _until_text(st: RecipeStage, p: CutProfile) -> str:
    if st.until == "probe_c":
        return f"Probe {st.until_value:g}C"
    if st.until == "pit_c":
        return f"Pit {st.until_value:g}C"
    if st.until == "minutes":
        return f"After {st.until_value:g} min"
    return f"Probe {p.pull_c:g}C" + (
        f", {p.probe_2_role.lower()} {p.probe_2_target_c:g}C" if p.probe_2_target_c else ""
    )


def _build_recipe_plan(
    profiles: Profiles, profile: CutProfile, *, weight_kg: float, serve_by: datetime, now: datetime
) -> Plan:
    stage_min = recipe_stage_minutes(profile, weight_kg)
    cook_min = sum(stage_min)
    preheat = timedelta(minutes=profiles.preheat_min)
    rest = timedelta(minutes=profile.rest_min)
    cook_td = timedelta(minutes=cook_min)
    start_by = serve_by - rest - cook_td - preheat
    late_by = max(0.0, (now - start_by).total_seconds() / 60)
    if late_by > 0:
        start_by = now
    meat_on = start_by + preheat
    pull_at = meat_on + cook_td

    first = profile.stages[0]
    phases = [
        Phase(
            "preheat",
            "Preheat",
            start_by,
            meat_on,
            first.pit_c,
            f"Pit within 8C of {first.pit_c:g}C",
            f"Light the smoker and set {first.pit_c:g}C",
            "preheat",
        )
    ]
    t = meat_on
    for st, mins_ in zip(profile.stages, stage_min, strict=True):
        end = t + timedelta(minutes=mins_)
        phases.append(Phase(st.key, st.name, t, end, st.pit_c, _until_text(st, profile), st.enter, st.icon))
        t = end
    rest_action = profile.rest_note or f"Rest {profile.rest_min:g} min"
    if profile.carryover_check_c is not None:
        rest_action += f" Check it reaches {profile.carryover_check_c:g}C."
    phases.append(
        Phase(
            "rest",
            "Rest",
            pull_at,
            pull_at + rest,
            profile.hold_pit_c,
            f"{profile.rest_min:g} min",
            rest_action,
            "rest",
        )
    )
    return Plan(
        profile=profile,
        weight_kg=weight_kg,
        thickness_cm=None,
        wrap="none",
        serve_by=serve_by,
        created=now,
        unwrapped_hours=cook_min / 60,
        wrap_fraction=None,
        wrap_factor=1.0,
        cook_hours=cook_min / 60,
        start_by=start_by,
        meat_on=meat_on,
        pull_at=pull_at,
        late_by_min=late_by,
        phases=tuple(phases),
        stage_minutes=stage_min,
    )


@dataclass
class RecipeRun:
    """Mutable progress through a recipe (owned and persisted by the manager)."""

    index: int = 0
    entered: datetime | None = None
    clock_at: datetime | None = None
    fired: set[str] = field(default_factory=set)
    last_prompt: str | None = None
    last_prompt_at: datetime | None = None
    ready: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Serialise for storage."""
        iso = lambda d: d.isoformat() if d else None  # noqa: E731
        return {
            "index": self.index,
            "entered": iso(self.entered),
            "clock_at": iso(self.clock_at),
            "fired": sorted(self.fired),
            "last_prompt": self.last_prompt,
            "last_prompt_at": iso(self.last_prompt_at),
            "ready": self.ready,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RecipeRun:
        """Restore from storage."""
        parse = lambda s: datetime.fromisoformat(s) if s else None  # noqa: E731
        return cls(
            index=int(d.get("index", 0)),
            entered=parse(d.get("entered")),
            clock_at=parse(d.get("clock_at")),
            fired=set(d.get("fired", [])),
            last_prompt=d.get("last_prompt"),
            last_prompt_at=parse(d.get("last_prompt_at")),
            ready=bool(d.get("ready", False)),
        )


@dataclass(frozen=True)
class RecipeEvent:
    """Something the cook should be told about (one notification)."""

    key: str
    kind: str  # "stage" | "prompt" | "ready"
    title: str
    message: str


@dataclass(frozen=True)
class RecipeStatus:
    """Result of one recipe tick."""

    advice: str
    title: str
    icon: str
    pit_c: float | None
    step: int
    steps: int
    remaining_min: float | None
    ready: bool
    events: tuple[RecipeEvent, ...]


def _stage_event(plan: Plan, i: int, prev_pit: float | None) -> RecipeEvent:
    st = plan.profile.stages[i]
    msg = st.enter
    if prev_pit is not None and abs(st.pit_c - prev_pit) >= 1 and f"{st.pit_c:g}C" not in msg:
        msg += f" (pit {'up' if st.pit_c > prev_pit else 'down'} to {st.pit_c:g}C)"
    return RecipeEvent(f"s{i}", "stage", f"Step {i + 1}/{len(plan.profile.stages)}: {st.name}", msg)


def _stage_done(
    st: RecipeStage,
    p: CutProfile,
    planned: float,
    clock: float,
    waited: float,
    probe_c: float | None,
    probe2_c: float | None,
    pit_c: float | None,
) -> bool:
    if st.until == "minutes":
        return clock >= st.until_value
    if st.until == "pit_c":
        return (pit_c is not None and pit_c >= st.until_value) or waited >= max(2 * planned, 30)
    target = st.until_value if st.until == "probe_c" else p.pull_c
    if probe_c is None:
        # No probe reading: intermediate steps move on by the planned time, but the
        # pull is never called on time alone - safe doneness needs a temperature.
        return st.until != "pull" and clock >= planned
    if probe_c < target:
        return False
    if st.until == "pull" and p.probe_2_target_c and probe2_c is not None:
        return probe2_c >= p.probe_2_target_c
    return True


def _prompt_due(pr: RecipePrompt, clock: float, probe_c: float | None) -> int | None:
    """Return an occurrence number when the prompt is due now, else None."""
    if pr.at_probe_c is not None:
        return 0 if probe_c is not None and probe_c >= pr.at_probe_c else None
    if pr.at_min is not None:
        return 0 if clock >= pr.at_min else None
    if clock < pr.from_min:
        return None
    return int((clock - pr.from_min) // pr.every_min)


def recipe_tick(
    plan: Plan,
    run: RecipeRun,
    *,
    now: datetime,
    probe_c: float | None,
    probe2_c: float | None,
    pit_c: float | None,
    advance: bool = False,
) -> RecipeStatus:
    """Advance a recipe run and report advice plus any new events.

    ``advance`` forces the next stage (the cook pressed "Next step").
    """
    p = plan.profile
    stages = p.stages
    last = len(stages) - 1
    events: list[RecipeEvent] = []

    def emit(ev: RecipeEvent) -> None:
        if ev.key in run.fired:
            return
        run.fired.add(ev.key)
        events.append(ev)
        run.last_prompt, run.last_prompt_at = f"{ev.title}: {ev.message}", now

    if run.entered is None:
        run.entered = now
        emit(_stage_event(plan, 0, None))

    for _ in range(len(stages)):
        st = stages[run.index]
        planned = plan.stage_minutes[run.index] - (HEAT_UP_EST_MIN if st.timer_from_pit else 0)
        waited = (now - run.entered).total_seconds() / 60
        if run.clock_at is None and (
            not st.timer_from_pit
            or (pit_c is not None and pit_c >= st.pit_c - PIT_READY_MARGIN_C)
            or waited >= HEAT_WAIT_MAX_MIN
        ):
            run.clock_at = now
        clock = (now - run.clock_at).total_seconds() / 60 if run.clock_at else 0.0
        done = advance or _stage_done(st, p, planned, clock, waited, probe_c, probe2_c, pit_c)
        for j, pr in enumerate(st.prompts):
            if done and pr.every_min is not None:
                continue  # no "spritz" in the same breath as "wrap it"
            n = _prompt_due(pr, clock, probe_c) if run.clock_at else None
            if n is not None:
                emit(RecipeEvent(f"s{run.index}p{j}#{n}", "prompt", st.name, pr.text))
        advance = False
        if not done:
            break
        if run.index == last:
            run.ready = True
            break
        prev_pit = st.pit_c
        run.index += 1
        run.entered, run.clock_at = now, None
        emit(_stage_event(plan, run.index, prev_pit))

    st = stages[run.index]
    planned = plan.stage_minutes[run.index] - (HEAT_UP_EST_MIN if st.timer_from_pit else 0)
    clock = (now - run.clock_at).total_seconds() / 60 if run.clock_at else 0.0
    if st.until == "minutes":
        remaining = max(0.0, st.until_value - clock)
    elif st.until == "pit_c":
        remaining = None
    else:
        remaining = max(0.0, planned - clock)

    if run.ready:
        if p.uses_probe and probe_c is not None and st.until == "pull":
            advice = f"Pull now ({probe_c:.0f}C)"
        else:
            advice = f"Pull now: {st.name} done"
        advice += f", rest {p.rest_min:g} min" if p.rest_min else ""
        emit(RecipeEvent("ready", "ready", "Take it off", advice))
        return RecipeStatus(advice, "Take it off", "check", None, run.index, last + 1, 0.0, True, tuple(events))

    fresh = run.last_prompt_at and (now - run.last_prompt_at).total_seconds() / 60 < PROMPT_SHOW_MIN
    if fresh and run.last_prompt:
        advice = run.last_prompt
    elif st.until == "pit_c":
        advice = f"{st.name}: waiting for the pit to reach {st.until_value:g}C" + (
            f" (now {pit_c:.0f}C)" if pit_c is not None else ""
        )
    elif st.timer_from_pit and run.clock_at is None:
        advice = f"{st.name}: waiting for the pit to reach {st.pit_c:g}C before the timer starts"
    elif st.until == "minutes":
        advice = f"{st.name}: {remaining:.0f} min left at {st.pit_c:g}C"
    elif st.until == "pull" and probe_c is None and clock >= planned:
        advice = f"Check with an instant-read thermometer: pull at {p.pull_c:g}C, then press Pulled"
    else:
        target = st.until_value if st.until == "probe_c" else p.pull_c
        now_txt = f"{probe_c:.0f}C" if probe_c is not None else "no probe reading"
        advice = f"{st.name}: until probe {target:g}C (now {now_txt}), pit {st.pit_c:g}C"
    title = f"Step {run.index + 1} of {last + 1} · {st.name}"
    return RecipeStatus(advice, title, st.icon, st.pit_c, run.index, last + 1, remaining, False, tuple(events))


def recipe_forecast(
    plan: Plan,
    run: RecipeRun,
    *,
    now: datetime,
    meat_on: datetime,
    probe_c: float | None,
    rate_c_per_h: float | None,
) -> Forecast:
    """ETA from the current stage's progress plus the planned length of the rest."""
    p = plan.profile
    planned_pull = meat_on + timedelta(minutes=plan.cook_minutes)
    if run.ready:
        return Forecast(now, now, now, (now - planned_pull).total_seconds() / 60, 1.0, "done", False)
    st = p.stages[run.index]
    planned = plan.stage_minutes[run.index]
    clock = (now - run.clock_at).total_seconds() / 60 if run.clock_at else 0.0
    if st.until == "minutes":
        left = max(0.0, st.until_value - clock) + (0 if run.clock_at else HEAT_UP_EST_MIN)
    else:
        left = max(5.0, planned - clock)
        target = st.until_value if st.until == "probe_c" else p.pull_c
        if (
            st.until in ("probe_c", "pull")
            and probe_c is not None
            and rate_c_per_h is not None
            and rate_c_per_h >= RATE_MIN_C_PER_H
            and probe_c > 30
        ):
            left = (left + max(0.0, (target - probe_c) / rate_c_per_h * 60)) / 2
    left += sum(plan.stage_minutes[run.index + 1 :])
    eta = now + timedelta(minutes=left)
    elapsed = max((now - meat_on).total_seconds() / 60, 0.0)
    spread = max(timedelta(minutes=left) * ETA_RANGE, timedelta(minutes=5))
    in_stall = bool(p.stall_c and probe_c is not None and p.stall_c[0] <= probe_c <= p.stall_c[1])
    progress = elapsed / (elapsed + left) if elapsed + left else 1.0
    return Forecast(
        eta,
        eta - spread,
        eta + spread,
        (eta - planned_pull).total_seconds() / 60,
        min(progress, 1.0),
        "recipe",
        in_stall,
    )


def planned_pit(
    plan: Plan, *, stage: str, probe_c: float | None, elapsed_min: float | None, run: RecipeRun | None
) -> float | None:
    """The pit temperature the plan calls for right now (no behind/ahead nudges)."""
    p = plan.profile
    if stage == "preheat":
        return p.pit_c
    if stage != "cooking":
        return None
    if p.is_recipe:
        return p.stages[run.index].pit_c if run else p.pit_c
    finish = next((ph for ph in plan.phases if ph.key == "finish"), None)
    if finish is not None and finish.pit_c is not None:
        by_temp = (
            p.finish_after_min is None and p.wrap_at_c is not None and probe_c is not None and probe_c >= p.wrap_at_c
        )
        by_time = p.finish_after_min is not None and elapsed_min is not None and elapsed_min >= p.finish_after_min
        if by_temp or by_time:
            return finish.pit_c
    return p.pit_c
