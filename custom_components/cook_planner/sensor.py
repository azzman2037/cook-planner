"""Sensors exposing the plan, live forecast and advice."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from . import CookPlannerConfigEntry
from .const import STAGES
from .entity import CookPlannerEntity
from .manager import CookManager

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class CookSensorDescription(SensorEntityDescription):
    """Sensor bound to a manager value."""

    value_fn: Callable[[CookManager], StateType | datetime]
    attrs_fn: Callable[[CookManager], Mapping[str, Any] | None] = lambda _m: None


def _plan_attrs(m: CookManager) -> Mapping[str, Any] | None:
    if m.plan is None:
        return None
    attrs = dict(m.plan.as_dict())
    attrs["curve"] = m.plan.curve_points(m.meat_on)
    attrs["notes"] = m.plan.profile.notes
    attrs["meat_on_actual"] = m.meat_on.isoformat() if m.meat_on else None
    attrs["wrapped_at"] = m.wrapped_at.isoformat() if m.wrapped_at else None
    return attrs


def _eta_attrs(m: CookManager) -> Mapping[str, Any] | None:
    fc = m.forecast
    if fc is None:
        return None
    return {
        "eta_low": fc.eta_low.isoformat(),
        "eta_high": fc.eta_high.isoformat(),
        "method": fc.method,
        "in_stall": fc.in_stall,
        "probe_1_c": m.probe_1_c,
        "probe_2_c": m.probe_2_c,
        "pit_c": m.pit_c,
    }


def _planned_pull(m: CookManager) -> datetime | None:
    if m.plan is None:
        return None
    if m.meat_on is not None:
        return m.meat_on + timedelta(minutes=m.plan.cook_minutes)
    return m.plan.pull_at


def _planned_probe(m: CookManager) -> float | None:
    if m.plan is None or m.meat_on is None:
        return None
    return round(m.plan.planned_temp_at(dt_util.now(), m.meat_on), 1)


def _log_attrs(m: CookManager) -> Mapping[str, Any]:
    return {"cooks": m.log[-20:]}


SENSORS: tuple[CookSensorDescription, ...] = (
    CookSensorDescription(
        key="stage",
        device_class=SensorDeviceClass.ENUM,
        options=STAGES,
        icon="mdi:list-status",
        value_fn=lambda m: m.stage,
    ),
    CookSensorDescription(
        key="plan",
        icon="mdi:clipboard-text-clock",
        value_fn=lambda m: m.plan.summary() if m.plan else None,
        attrs_fn=_plan_attrs,
    ),
    CookSensorDescription(
        key="next_action",
        icon="mdi:chef-hat",
        value_fn=lambda m: m.advice[:255],
    ),
    CookSensorDescription(
        key="start_by",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-start",
        value_fn=lambda m: m.plan.start_by if m.plan else None,
    ),
    CookSensorDescription(
        key="planned_pull",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-end",
        value_fn=_planned_pull,
    ),
    CookSensorDescription(
        key="eta",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:timer-check",
        value_fn=lambda m: m.forecast.eta if m.forecast else None,
        attrs_fn=_eta_attrs,
    ),
    CookSensorDescription(
        key="schedule_delta",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:clock-alert",
        value_fn=lambda m: round(m.forecast.delta_min) if m.forecast else None,
    ),
    CookSensorDescription(
        key="progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:progress-clock",
        value_fn=lambda m: round(m.forecast.progress * 100) if m.forecast else None,
    ),
    CookSensorDescription(
        key="recommended_pit",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=0,
        icon="mdi:thermometer-auto",
        value_fn=lambda m: m.recommended_pit_c,
    ),
    CookSensorDescription(
        key="planned_probe",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:chart-bell-curve-cumulative",
        value_fn=_planned_probe,
    ),
    CookSensorDescription(
        key="probe_rate",
        native_unit_of_measurement="°C/h",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:thermometer-chevron-up",
        value_fn=lambda m: m.rate_c_per_h,
    ),
    CookSensorDescription(
        key="cook_log",
        icon="mdi:notebook",
        value_fn=lambda m: len(m.log),
        attrs_fn=_log_attrs,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CookPlannerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    async_add_entities(CookSensor(entry.runtime_data, d) for d in SENSORS)


class CookSensor(CookPlannerEntity, SensorEntity):
    """A Cook Planner sensor."""

    entity_description: CookSensorDescription
    _unrecorded_attributes = frozenset({"curve", "phases", "cooks", "notes"})

    def __init__(self, manager: CookManager, description: CookSensorDescription) -> None:
        super().__init__(manager, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType | datetime:
        return self.entity_description.value_fn(self.manager)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        return self.entity_description.attrs_fn(self.manager)
