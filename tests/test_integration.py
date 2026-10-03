"""End-to-end tests of the integration inside a test Home Assistant."""
from __future__ import annotations

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.cook_planner.const import CONF_PIT_ENTITY, CONF_PROBE_1, CONF_PROBE_2, DOMAIN

PIT = "climate.smoker"
P1 = "sensor.probe_1"
P2 = "sensor.probe_2"
DATA = {CONF_PIT_ENTITY: PIT, CONF_PROBE_1: P1, CONF_PROBE_2: P2}


def _set(hass: HomeAssistant, p1: float | str, p2: float | str = 316.1, pit: float = 116) -> None:
    hass.states.async_set(PIT, "heat", {"current_temperature": pit, "temperature": 115})
    hass.states.async_set(P1, str(p1), {"unit_of_measurement": "°C", "device_class": "temperature"})
    hass.states.async_set(P2, str(p2), {"unit_of_measurement": "°C", "device_class": "temperature"})


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    await hass.config.async_set_time_zone("Australia/Sydney")
    hass.config.units = __import__("homeassistant.util.unit_system", fromlist=["METRIC_SYSTEM"]).METRIC_SYSTEM
    _set(hass, 5)
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id=P1)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _press(hass: HomeAssistant, key: str) -> None:
    await hass.services.async_call("button", "press", {"entity_id": f"button.cook_planner_{key}"}, blocking=True)
    await hass.async_block_till_done()


async def test_config_flow(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    bad = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PIT_ENTITY: PIT, CONF_PROBE_1: P1, CONF_PROBE_2: P1}
    )
    assert bad["errors"] == {CONF_PROBE_2: "same_probe"}
    ok = await hass.config_entries.flow.async_configure(bad["flow_id"], DATA)
    assert ok["type"] is FlowResultType.CREATE_ENTRY
    assert ok["data"] == DATA


async def test_select_meat_filters_cuts(hass: HomeAssistant) -> None:
    await _setup(hass)
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.cook_planner_meat", "option": "Beef brisket"}, blocking=True
    )
    cut = hass.states.get("select.cook_planner_cut")
    assert cut.attributes["options"] == ["Point end (deckle)", "Flat", "Whole (packer)"]
    assert hass.states.get("select.cook_planner_wrap").state == "Butcher paper"
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.cook_planner_meat", "option": "Pork shoulder"}, blocking=True
    )
    assert hass.states.get("select.cook_planner_cut").state == "Bone-in"
    assert hass.states.get("select.cook_planner_wrap").state == "No wrap"


async def test_full_cook_flow(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    tz = dt_util.get_time_zone("Australia/Sydney")
    freezer.move_to(dt_util.as_utc(dt_util.now(tz).replace(year=2026, month=10, day=4, hour=6, minute=0)))
    entry = await _setup(hass)
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.cook_planner_meat", "option": "Pork shoulder"}, blocking=True
    )
    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.cook_planner_weight", "value": 3.5}, blocking=True
    )
    serve = dt_util.now().replace(hour=18, minute=0)
    await hass.services.async_call(
        "datetime", "set_value", {"entity_id": "datetime.cook_planner_serve_by", "datetime": serve.isoformat()},
        blocking=True,
    )
    await _press(hass, "generate_plan")

    assert hass.states.get("sensor.cook_planner_stage").state == "planned"
    plan = hass.states.get("sensor.cook_planner_plan")
    assert plan.state.startswith("Pork shoulder - Bone-in 3.5 kg - 10h30")
    assert [p["key"] for p in plan.attributes["phases"]] == ["preheat", "smoke", "finish", "rest"]
    start_by = dt_util.parse_datetime(hass.states.get("sensor.cook_planner_start_by").state)
    assert dt_util.as_local(start_by).strftime("%H:%M") == "06:10"
    assert "Light the smoker at 06:10" in hass.states.get("sensor.cook_planner_next_action").state

    await _press(hass, "start_cook")
    assert hass.states.get("sensor.cook_planner_stage").state == "preheat"
    await _press(hass, "meat_on")
    assert hass.states.get("sensor.cook_planner_stage").state == "cooking"

    # Probe 2 reads 316C (empty socket): must be ignored, not treated as a hot probe.
    for minutes, temp in [(0, 5.0), (5, 5.4), (10, 5.9), (15, 6.5)]:
        freezer.tick(timedelta(minutes=5) if minutes else timedelta(0))
        _set(hass, temp)
        await hass.async_block_till_done()
    eta = hass.states.get("sensor.cook_planner_eta")
    assert eta.state not in ("unknown", "unavailable")
    assert eta.attributes["probe_2_c"] is None
    assert hass.states.get("sensor.cook_planner_probe_rate").state not in ("unknown", "unavailable")
    assert hass.states.get("sensor.cook_planner_planned_probe").state not in ("unknown", "unavailable")

    # Jump 5 h with the probe well behind the plan -> behind, pit nudge suggested.
    freezer.tick(timedelta(hours=5))
    _set(hass, 40)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.cook_planner_behind_schedule").state) > 20
    assert "behind" in hass.states.get("sensor.cook_planner_next_action").state
    assert float(hass.states.get("sensor.cook_planner_recommended_pit").state) == 125

    # Restart mid-cook: the session survives.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.cook_planner_stage").state == "cooking"

    _set(hass, 95.5)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.cook_planner_next_action").state.startswith("Pull now")
    await _press(hass, "pulled")
    assert hass.states.get("sensor.cook_planner_stage").state == "rest"
    await _press(hass, "end_cook")
    assert hass.states.get("sensor.cook_planner_stage").state == "idle"
    log = hass.states.get("sensor.cook_planner_cook_log")
    assert log.state == "1"
    cook = log.attributes["cooks"][0]
    assert cook["meat"] == "pork_shoulder"
    assert cook["planned_cook_h"] == pytest.approx(10.5)
    assert cook["actual_cook_h"] > 5


async def test_cannot_plan_mid_cook(hass: HomeAssistant) -> None:
    await _setup(hass)
    await _press(hass, "generate_plan")
    await _press(hass, "meat_on")
    with pytest.raises(Exception, match="in progress"):
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.cook_planner_generate_plan"}, blocking=True
        )


async def test_unload(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
