"""Runtime state for one Cook Planner entry: inputs, plan, live tracking, log."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    CONF_PIT_ENTITY,
    CONF_PROBE_1,
    CONF_PROBE_2,
    DOMAIN,
    LOG_KEEP,
    LOGGER,
    RATE_MIN_SPAN_MIN,
    RATE_WINDOW_MIN,
    SAMPLE_KEEP_MIN,
    STAGE_COOKING,
    STAGE_IDLE,
    STAGE_PLANNED,
    STAGE_PREHEAT,
    STAGE_REST,
    STORAGE_VERSION,
    UPDATE_INTERVAL_S,
)
from .planner import Forecast, Plan, Profiles, build_plan, forecast, load_profiles, next_action

PROFILES_PATH = Path(__file__).parent / "profiles.json"
# Readings outside this window are treated as "no probe" (e.g. an empty socket
# reporting 316C on some GMG firmwares).
PROBE_VALID_C = (-20.0, 290.0)


def _read_temp_c(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Read a temperature in degC from a sensor state or a climate's current temp."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    if state.domain == "climate":
        raw = state.attributes.get("current_temperature")
        unit = hass.config.units.temperature_unit
    else:
        raw = state.state
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT, hass.config.units.temperature_unit)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if unit and unit != "°C":
        value = TemperatureConverter.convert(value, unit, "°C")
    return value


class CookManager:
    """Owns the cook inputs, plan and live tracking for one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str, options: dict[str, Any]) -> None:
        """Initialise; call :meth:`async_setup` before use."""
        self.hass = hass
        self.entry_id = entry_id
        self.pit_entity: str | None = options.get(CONF_PIT_ENTITY)
        self.probe_1_entity: str = options[CONF_PROBE_1]
        self.probe_2_entity: str | None = options.get(CONF_PROBE_2)
        self.profiles: Profiles
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._samples: deque[tuple[datetime, float]] = deque()

        # Inputs
        self.meat: str = ""
        self.cut: str = ""
        self.weight_kg: float = 3.0
        self.thickness_cm: float = 0.0
        self.wrap: str = ""
        self.serve_by: datetime = dt_util.now()
        # Session
        self.stage: str = STAGE_IDLE
        self.plan: Plan | None = None
        self.plan_inputs: dict[str, Any] | None = None
        self.started_at: datetime | None = None
        self.meat_on: datetime | None = None
        self.wrapped_at: datetime | None = None
        self.pulled_at: datetime | None = None
        self.log: list[dict[str, Any]] = []
        # Derived each refresh
        self.probe_1_c: float | None = None
        self.probe_2_c: float | None = None
        self.pit_c: float | None = None
        self.rate_c_per_h: float | None = None
        self.forecast: Forecast | None = None
        self.advice: str = "Choose a meat, cut, weight and serve time, then Generate plan"
        self.recommended_pit_c: float | None = None

    # ------------------------------------------------------------------ setup

    async def async_setup(self) -> None:
        """Load profiles and any persisted session, start listening."""
        self.profiles = await self.hass.async_add_executor_job(load_profiles, PROFILES_PATH)
        first_meat = next(iter(self.profiles.cuts))
        self.meat = first_meat
        self.cut = next(iter(self.profiles.cuts[first_meat]))
        self.wrap = self.cut_profile.wrap_default
        self.serve_by = self._default_serve_by()
        await self._async_restore()

        tracked = [e for e in (self.pit_entity, self.probe_1_entity, self.probe_2_entity) if e]
        self._unsubs.append(async_track_state_change_event(self.hass, tracked, self._on_state))
        self._unsubs.append(
            async_track_time_interval(self.hass, self._on_tick, timedelta(seconds=UPDATE_INTERVAL_S))
        )
        self._refresh()

    async def async_unload(self) -> None:
        """Stop listening and flush state."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self._store.async_save(self._as_store())

    def _default_serve_by(self) -> datetime:
        now = dt_util.now()
        serve = now.replace(hour=18, minute=0, second=0, microsecond=0)
        return serve if serve > now + timedelta(hours=1) else serve + timedelta(days=1)

    # -------------------------------------------------------------- listeners

    @callback
    def async_add_listener(self, update: Callable[[], None]) -> CALLBACK_TYPE:
        """Register an entity update callback."""
        self._listeners.append(update)

        @callback
        def _remove() -> None:
            self._listeners.remove(update)

        return _remove

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    @callback
    def _on_state(self, _event: Event[EventStateChangedData]) -> None:
        self._refresh()

    @callback
    def _on_tick(self, _now: datetime) -> None:
        self._refresh()

    # ----------------------------------------------------------------- inputs

    @property
    def cut_profile(self):
        """Currently selected cut profile."""
        return self.profiles.cut(self.meat, self.cut)

    def cut_options(self) -> dict[str, str]:
        """Cut key -> name for the selected meat."""
        return {k: c.name for k, c in self.profiles.cuts[self.meat].items()}

    @callback
    def set_meat(self, meat: str) -> None:
        """Select a meat; resets the cut and wrap to that meat's defaults."""
        if meat not in self.profiles.cuts:
            raise HomeAssistantError(f"Unknown meat {meat}")
        self.meat = meat
        self.cut = next(iter(self.profiles.cuts[meat]))
        self.wrap = self.cut_profile.wrap_default
        self._input_changed()

    @callback
    def set_cut(self, cut: str) -> None:
        """Select a cut of the current meat."""
        if cut not in self.profiles.cuts[self.meat]:
            raise HomeAssistantError(f"Unknown cut {cut} for {self.meat}")
        self.cut = cut
        self.wrap = self.cut_profile.wrap_default
        self._input_changed()

    @callback
    def set_input(self, name: str, value: Any) -> None:
        """Set weight_kg, thickness_cm, wrap or serve_by."""
        if name == "wrap" and value not in self.profiles.wrap_methods:
            raise HomeAssistantError(f"Unknown wrap method {value}")
        setattr(self, name, value)
        self._input_changed()

    @callback
    def _input_changed(self) -> None:
        if self.stage == STAGE_PLANNED:
            # The shown plan no longer matches the inputs: rebuild it.
            self._build_plan()
        self._save()
        self._refresh()

    # ---------------------------------------------------------------- actions

    @callback
    def generate_plan(self) -> None:
        """Build a plan from the current inputs."""
        if self.stage in (STAGE_PREHEAT, STAGE_COOKING, STAGE_REST):
            raise HomeAssistantError("A cook is in progress: end it before planning a new one")
        self._build_plan()
        self.stage = STAGE_PLANNED
        self._save()
        self._refresh()

    def _build_plan(self) -> None:
        self.plan_inputs = {
            "meat": self.meat,
            "cut": self.cut,
            "weight_kg": self.weight_kg,
            "thickness_cm": self.thickness_cm or None,
            "wrap": self.wrap,
            "serve_by": self.serve_by.isoformat(),
        }
        self.plan = self._plan_from_inputs(self.plan_inputs, dt_util.now())

    def _plan_from_inputs(self, inputs: dict[str, Any], now: datetime) -> Plan:
        return build_plan(
            self.profiles,
            self.profiles.cut(inputs["meat"], inputs["cut"]),
            weight_kg=float(inputs["weight_kg"]),
            thickness_cm=inputs.get("thickness_cm"),
            wrap=inputs.get("wrap"),
            serve_by=dt_util.parse_datetime(inputs["serve_by"]) or self.serve_by,
            now=now,
        )

    @callback
    def start(self) -> None:
        """Start the cook (preheat). Ignition itself stays with the user."""
        if self.plan is None:
            self.generate_plan()
        if self.stage not in (STAGE_PLANNED,):
            raise HomeAssistantError(f"Cannot start from stage {self.stage}")
        self.stage = STAGE_PREHEAT
        self.started_at = dt_util.now()
        self._save()
        self._refresh()

    @callback
    def mark_meat_on(self) -> None:
        """Meat is on: the cook clock starts. Re-anchors the plan to now."""
        if self.stage not in (STAGE_PLANNED, STAGE_PREHEAT):
            raise HomeAssistantError(f"Cannot put meat on from stage {self.stage}")
        self.started_at = self.started_at or dt_util.now()
        self.meat_on = dt_util.now()
        self.stage = STAGE_COOKING
        self._samples.clear()
        self._save()
        self._refresh()

    @callback
    def mark_wrapped(self) -> None:
        """Record the wrap."""
        if self.stage != STAGE_COOKING:
            raise HomeAssistantError("Wrap only applies while cooking")
        self.wrapped_at = dt_util.now()
        self._save()
        self._refresh()

    @callback
    def mark_pulled(self) -> None:
        """Meat is off: start the rest."""
        if self.stage != STAGE_COOKING:
            raise HomeAssistantError("Pull only applies while cooking")
        self.pulled_at = dt_util.now()
        self.stage = STAGE_REST
        self._save()
        self._refresh()

    @callback
    def end(self) -> None:
        """Finish (or abandon) the cook and write it to the log."""
        if self.plan is not None and self.meat_on is not None:
            self.log.append(self._log_entry())
            self.log = self.log[-LOG_KEEP:]
        self.stage = STAGE_IDLE
        self.plan = None
        self.plan_inputs = None
        self.started_at = self.meat_on = self.wrapped_at = self.pulled_at = None
        self._samples.clear()
        self._save()
        self._refresh()

    def _log_entry(self) -> dict[str, Any]:
        assert self.plan is not None
        assert self.meat_on is not None
        end = self.pulled_at or dt_util.now()
        actual_h = (end - self.meat_on).total_seconds() / 3600
        return {
            "ended": dt_util.now().isoformat(),
            **(self.plan_inputs or {}),
            "planned_cook_h": round(self.plan.cook_hours, 2),
            "actual_cook_h": round(actual_h, 2),
            "ratio_actual_to_plan": round(actual_h / self.plan.cook_hours, 3)
            if self.plan.cook_hours
            else None,
            "meat_on": self.meat_on.isoformat(),
            "wrapped_at": self.wrapped_at.isoformat() if self.wrapped_at else None,
            "pulled_at": self.pulled_at.isoformat() if self.pulled_at else None,
            "pulled": self.pulled_at is not None,
            "final_probe_1_c": self.probe_1_c,
        }

    # ---------------------------------------------------------------- refresh

    @callback
    def _refresh(self) -> None:
        now = dt_util.now()
        self.pit_c = _read_temp_c(self.hass, self.pit_entity)
        self.probe_1_c = self._probe(self.probe_1_entity)
        self.probe_2_c = self._probe(self.probe_2_entity)
        self._sample(now)

        self.forecast = None
        plan = self.plan
        elapsed = (now - self.meat_on).total_seconds() / 60 if self.meat_on else None
        if plan is not None and self.stage == STAGE_COOKING and self.meat_on and self.probe_1_c is not None:
            self.forecast = forecast(
                plan, now=now, meat_on=self.meat_on, probe_c=self.probe_1_c, rate_c_per_h=self.rate_c_per_h
            )
        if plan is None:
            self.advice = "Choose a meat, cut, weight and serve time, then Generate plan"
            self.recommended_pit_c = None
        else:
            self.advice, self.recommended_pit_c = next_action(
                plan,
                stage=self.stage,
                probe_c=self.probe_1_c,
                pit_c=self.pit_c,
                wrapped=self.wrapped_at is not None,
                fc=self.forecast,
                elapsed_min=elapsed,
            )
        self._notify()

    def _probe(self, entity_id: str | None) -> float | None:
        value = _read_temp_c(self.hass, entity_id)
        if value is None or not PROBE_VALID_C[0] <= value <= PROBE_VALID_C[1]:
            return None
        return value

    def _sample(self, now: datetime) -> None:
        if self.probe_1_c is None:
            return
        if not self._samples or (now - self._samples[-1][0]).total_seconds() >= 20:
            self._samples.append((now, self.probe_1_c))
        cutoff = now - timedelta(minutes=SAMPLE_KEEP_MIN)
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        self.rate_c_per_h = rate_from_samples(list(self._samples), now)

    # ------------------------------------------------------------ persistence

    def _as_store(self) -> dict[str, Any]:
        def iso(d: datetime | None) -> str | None:
            return d.isoformat() if d else None

        return {
            "inputs": {
                "meat": self.meat,
                "cut": self.cut,
                "weight_kg": self.weight_kg,
                "thickness_cm": self.thickness_cm,
                "wrap": self.wrap,
                "serve_by": self.serve_by.isoformat(),
            },
            "session": {
                "stage": self.stage,
                "plan_inputs": self.plan_inputs,
                "plan_created": iso(self.plan.created) if self.plan else None,
                "started_at": iso(self.started_at),
                "meat_on": iso(self.meat_on),
                "wrapped_at": iso(self.wrapped_at),
                "pulled_at": iso(self.pulled_at),
            },
            "log": self.log,
        }

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(self._as_store, 5)

    async def _async_restore(self) -> None:
        data = await self._store.async_load()
        if not data:
            return
        inputs = data.get("inputs", {})
        if inputs.get("meat") in self.profiles.cuts and inputs.get("cut") in self.profiles.cuts[inputs["meat"]]:
            self.meat, self.cut = inputs["meat"], inputs["cut"]
        self.weight_kg = float(inputs.get("weight_kg", self.weight_kg))
        self.thickness_cm = float(inputs.get("thickness_cm") or 0)
        if inputs.get("wrap") in self.profiles.wrap_methods:
            self.wrap = inputs["wrap"]
        serve = dt_util.parse_datetime(inputs.get("serve_by") or "")
        if serve is not None:
            self.serve_by = serve
        self.log = list(data.get("log", []))

        session = data.get("session") or {}
        try:
            if session.get("plan_inputs"):
                created = dt_util.parse_datetime(session.get("plan_created") or "") or dt_util.now()
                self.plan_inputs = session["plan_inputs"]
                self.plan = self._plan_from_inputs(self.plan_inputs, created)
                self.stage = session.get("stage", STAGE_PLANNED)
            for key in ("started_at", "meat_on", "wrapped_at", "pulled_at"):
                setattr(self, key, dt_util.parse_datetime(session.get(key) or ""))
        except (KeyError, ValueError) as err:
            LOGGER.warning("Discarding unreadable saved cook session: %s", err)
            self.stage, self.plan, self.plan_inputs = STAGE_IDLE, None, None


def rate_from_samples(samples: list[tuple[datetime, float]], now: datetime) -> float | None:
    """Least-squares slope (degC/h) over the last RATE_WINDOW_MIN minutes."""
    window = [(t, v) for t, v in samples if t >= now - timedelta(minutes=RATE_WINDOW_MIN)]
    if len(window) < 3:
        return None
    span = (window[-1][0] - window[0][0]).total_seconds() / 60
    if span < RATE_MIN_SPAN_MIN:
        return None
    t0 = window[0][0]
    xs = [(t - t0).total_seconds() / 3600 for t, _ in window]
    ys = [v for _, v in window]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / den, 1)
