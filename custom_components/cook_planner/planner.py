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


class ProfileError(ValueError):
    """Raised when profiles.json is malformed."""


@dataclass(frozen=True)
class WrapMethod:
    """A wrap option and its effect on post-wrap cook time."""

    key: str
    name: str
    post_wrap_time_factor: float


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
    wrap_default: str
    wrap_at_c: float | None
    pull_c: float
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

    def as_dict(self) -> dict[str, Any]:
        """Serialise for state attributes and storage."""
        return {
            "key": self.key,
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
        }


def estimate_unwrapped_hours(
    profile: CutProfile, weight_kg: float, thickness_cm: float | None
) -> float:
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
            "preheat", "Preheat", plan.start_by, plan.meat_on, p.pit_c,
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
    phases.append(
        Phase(
            "smoke", "Smoke", plan.meat_on, smoke_end, p.pit_c,
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
        phases.append(
            Phase("finish", name, change_at, plan.pull_at, finish_pit, f"Probe {p.pull_c:g}C", action)
        )
    rest_action = f"Pull at {p.pull_c:g}C and rest {p.rest_min:g} min"
    if p.hold_pit_c is not None:
        rest_action += f" (hold at {p.hold_pit_c:g}C)"
    if p.carryover_check_c is not None:
        rest_action += f"; check it reaches {p.carryover_check_c:g}C"
    phases.append(
        Phase(
            "rest", "Rest", plan.pull_at, plan.pull_at + timedelta(minutes=p.rest_min),
            p.hold_pit_c, f"{p.rest_min:g} min", rest_action,
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
        by_time = (
            p.finish_after_min is not None
            and elapsed_min is not None
            and elapsed_min >= p.finish_after_min
        )
        if by_temp or by_time:
            pit = finish.pit_c
    if probe_c >= p.pull_c:
        return f"Pull now ({probe_c:.0f}C) and rest {p.rest_min:g} min", p.hold_pit_c
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
