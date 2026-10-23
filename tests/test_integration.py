"""End-to-end tests of the integration inside a test Home Assistant."""

from __future__ import annotations

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
    async_mock_service,
)

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
    hass.data["pit_calls"] = async_mock_service(hass, "climate", "set_temperature")
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
        "select", "select_option", {"entity_id": "select.cook_planner_meat", "option": "Pork"}, blocking=True
    )
    assert hass.states.get("select.cook_planner_cut").state == "Shoulder, bone-in"
    assert hass.states.get("select.cook_planner_wrap").state == "No wrap"


async def test_full_cook_flow(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    tz = dt_util.get_time_zone("Australia/Sydney")
    freezer.move_to(dt_util.as_utc(dt_util.now(tz).replace(year=2026, month=10, day=4, hour=6, minute=0)))
    entry = await _setup(hass)
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.cook_planner_meat", "option": "Pork"}, blocking=True
    )
    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.cook_planner_weight", "value": 3.5}, blocking=True
    )
    serve = dt_util.now().replace(hour=18, minute=0)
    await hass.services.async_call(
        "datetime",
        "set_value",
        {"entity_id": "datetime.cook_planner_serve_by", "datetime": serve.isoformat()},
        blocking=True,
    )
    await _press(hass, "generate_plan")

    assert hass.states.get("sensor.cook_planner_stage").state == "planned"
    plan = hass.states.get("sensor.cook_planner_plan")
    assert plan.state.startswith("Pork - Shoulder, bone-in 3.5 kg - 10h30")
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


async def test_stale_serve_by_rolls_forward(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    tz = dt_util.get_time_zone("Australia/Sydney")
    freezer.move_to(dt_util.as_utc(dt_util.now(tz).replace(year=2026, month=10, day=11, hour=16, minute=0)))
    await _setup(hass)
    stale = dt_util.now().replace(day=5, hour=18, minute=0)
    await hass.services.async_call(
        "datetime",
        "set_value",
        {"entity_id": "datetime.cook_planner_serve_by", "datetime": stale.isoformat()},
        blocking=True,
    )
    await _press(hass, "generate_plan")
    serve = dt_util.as_local(dt_util.parse_datetime(hass.states.get("datetime.cook_planner_serve_by").state))
    assert (serve.day, serve.hour) == (11, 18)
    assert "late" not in hass.states.get("sensor.cook_planner_next_action").state


async def test_single_fillet_weight_is_accepted(hass: HomeAssistant) -> None:
    await _setup(hass)
    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.cook_planner_weight", "value": 0.1}, blocking=True
    )
    assert float(hass.states.get("number.cook_planner_weight").state) == 0.1
    await _press(hass, "generate_plan")
    assert "0.1 kg" in hass.states.get("sensor.cook_planner_plan").state


async def test_unload(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def _select(hass: HomeAssistant, meat: str, cut: str) -> None:
    for key, opt in (("meat", meat), ("cut", cut)):
        await hass.services.async_call(
            "select", "select_option", {"entity_id": f"select.cook_planner_{key}", "option": opt}, blocking=True
        )


async def test_recipe_cook_notifies_and_drives_the_pit(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    tz = dt_util.get_time_zone("Australia/Sydney")
    freezer.move_to(dt_util.as_utc(dt_util.now(tz).replace(year=2026, month=10, day=17, hour=15, minute=0)))
    await _setup(hass)
    events = async_capture_events(hass, "cook_planner_prompt")
    pit_calls = hass.data["pit_calls"]
    await _select(hass, "Chicken", "Spatchcock herb-butter · recipe")
    details = hass.states.get("select.cook_planner_cut").attributes["details"]
    assert details["Spatchcock herb-butter · recipe"]["recipe"] == "1.1"
    assert details["Breast"]["recipe"] is None
    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.cook_planner_weight", "value": 1.8}, blocking=True
    )
    await _press(hass, "generate_plan")
    plan = hass.states.get("sensor.cook_planner_plan")
    assert [p["icon"] for p in plan.attributes["phases"]] == ["preheat", "smoke", "flame", "rest"]
    assert plan.attributes["pellets"].startswith("Apple")

    await _press(hass, "start_cook")
    assert [c.data["temperature"] for c in pit_calls] == [105]  # grill lit: preheat target sent

    _set(hass, 5, p2=6, pit=104)
    await _press(hass, "meat_on")
    assert events[-1].data["key"] == "s0"
    assert events[-1].data["title"] == "Step 1/2: Smoke infusion"
    advice = hass.states.get("sensor.cook_planner_next_action")
    assert advice.attributes["step"] == 1
    assert advice.attributes["steps"] == 2

    freezer.tick(timedelta(minutes=45))
    _set(hass, 48.5, p2=46, pit=105)
    await hass.async_block_till_done()
    assert events[-1].data["key"] == "s1"
    assert [c.data["temperature"] for c in pit_calls] == [105, 190]
    assert hass.states.get("sensor.cook_planner_plan").attributes["phase_index"] == 2

    freezer.tick(timedelta(minutes=20))
    _set(hass, 71, p2=70, pit=190)
    await hass.async_block_till_done()
    assert events[-1].data["message"] == "Brush lightly with melted butter or glaze."

    freezer.tick(timedelta(minutes=12))
    _set(hass, 75, p2=81, pit=190)
    await hass.async_block_till_done()
    assert events[-1].data["kind"] == "ready"
    assert hass.states.get("sensor.cook_planner_next_action").state.startswith("Pull now (75C)")
    kinds = [e.data["kind"] for e in events]
    assert kinds == ["advice", "stage", "stage", "prompt", "ready"]  # advice = "up to temp, meat on"
    assert events[0].data["title"] == "Smoker up to temp"

    await _press(hass, "pulled")
    await _press(hass, "end_cook")
    cook = hass.states.get("sensor.cook_planner_cook_log").attributes["cooks"][-1]
    assert cook["recipe"] == "1.1"
    assert cook["step_reached"] == 2


async def test_next_step_button_and_auto_pit_off(hass: HomeAssistant) -> None:
    await _setup(hass)
    pit_calls = hass.data["pit_calls"]
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.cook_planner_auto_pit_temperature"}, blocking=True
    )
    await _select(hass, "Pork", "Spare ribs 3-2-1 · recipe")
    await _press(hass, "generate_plan")
    await _press(hass, "start_cook")
    await _press(hass, "meat_on")
    await _press(hass, "next_step")
    advice = hass.states.get("sensor.cook_planner_next_action")
    assert advice.attributes["step"] == 2
    assert "meat-side DOWN" in advice.state
    assert pit_calls == []
    with pytest.raises(Exception, match="Cannot start from stage cooking"):
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.cook_planner_start_cook"}, blocking=True
        )


async def test_legacy_cut_still_notifies(hass: HomeAssistant) -> None:
    await _setup(hass)
    events = async_capture_events(hass, "cook_planner_prompt")
    await _select(hass, "Chicken", "Thigh fillet - quick (hot & fast)")
    await _press(hass, "generate_plan")
    await _press(hass, "start_cook")
    _set(hass, 5, pit=212)
    await hass.async_block_till_done()
    assert events[-1].data["title"] == "Smoker up to temp"
    await _press(hass, "meat_on")
    _set(hass, 81, pit=215)
    await hass.async_block_till_done()
    assert events[-1].data["title"] == "Take it off"
    assert len([e for e in events if e.data["title"] == "Take it off"]) == 1


async def test_next_step_outside_a_recipe_is_refused(hass: HomeAssistant) -> None:
    await _setup(hass)
    with pytest.raises(Exception, match="recipe"):
        await hass.services.async_call("button", "press", {"entity_id": "button.cook_planner_next_step"}, blocking=True)


async def test_keep_warm_after_a_cook_rests_in_the_smoker(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    tz = dt_util.get_time_zone("Australia/Sydney")
    freezer.move_to(dt_util.as_utc(dt_util.now(tz).replace(year=2026, month=10, day=17, hour=15, minute=0)))
    await _setup(hass)
    events = async_capture_events(hass, "cook_planner_prompt")
    pit_calls = hass.data["pit_calls"]
    await _select(hass, "Chicken", "Spatchcock herb-butter · recipe")
    await _press(hass, "generate_plan")
    await _press(hass, "start_cook")
    await _press(hass, "meat_on")
    freezer.tick(timedelta(minutes=60))
    _set(hass, 75, p2=81, pit=190)
    await hass.async_block_till_done()

    await _press(hass, "keep_warm")
    assert hass.states.get("sensor.cook_planner_stage").state == "hold"
    assert pit_calls[-1].data["temperature"] == 70
    advice = hass.states.get("sensor.cook_planner_next_action")
    assert advice.state.startswith("Resting in the smoker at 70C, 12 min to go")
    assert advice.attributes["keep_warm_c"] == 70
    assert advice.attributes["rest_ends"] is not None
    assert advice.attributes["step_remaining_min"] == 12

    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.cook_planner_keep_warm", "value": 80}, blocking=True
    )
    await hass.async_block_till_done()
    assert pit_calls[-1].data["temperature"] == 80

    freezer.tick(timedelta(minutes=13))
    _set(hass, 72, p2=74, pit=80)
    await hass.async_block_till_done()
    _set(hass, 71, p2=74, pit=80)
    await hass.async_block_till_done()
    assert [e.data["title"] for e in events].count("Rest done") == 1
    assert hass.states.get("sensor.cook_planner_next_action").state == "Rest done: ready to serve. Holding 80C"

    # Meat cooling: one alert per drop, re-armed once it is back above 62C.
    _set(hass, 59, p2=74, pit=80)
    await hass.async_block_till_done()
    _set(hass, 58, p2=74, pit=80)
    await hass.async_block_till_done()
    assert [e.data["title"] for e in events].count("Meat cooling") == 1
    assert hass.states.get("sensor.cook_planner_next_action").state.startswith("Meat at 58C, below 60C: serve now")
    _set(hass, 63, p2=74, pit=80)
    await hass.async_block_till_done()
    _set(hass, 59, p2=74, pit=80)
    await hass.async_block_till_done()
    assert [e.data["title"] for e in events].count("Meat cooling") == 2

    await _press(hass, "end_cook")
    assert hass.states.get("sensor.cook_planner_stage").state == "idle"
    cook = hass.states.get("sensor.cook_planner_cook_log").attributes["cooks"][-1]
    assert cook["pulled"] is True
    assert cook["held_in_smoker_min"] == 13
    assert cook["keep_warm_c"] == 80


async def test_keep_warm_on_its_own_waits_for_ignition(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await _setup(hass)
    pit_calls = hass.data["pit_calls"]
    hass.states.async_set(PIT, "off", {"current_temperature": 105, "temperature": 115})
    await hass.async_block_till_done()
    with pytest.raises(Exception, match="outside valid range 65"):  # the GMG holds no lower than 65C
        await hass.services.async_call(
            "number", "set_value", {"entity_id": "number.cook_planner_keep_warm", "value": 60}, blocking=True
        )
    assert hass.states.get("number.cook_planner_keep_warm").state == "70.0"
    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.cook_planner_keep_warm", "value": 65}, blocking=True
    )
    await _press(hass, "keep_warm")
    assert hass.states.get("sensor.cook_planner_stage").state == "hold"
    advice = hass.states.get("sensor.cook_planner_next_action").state
    assert advice == "Grill is off: tap Ignite and it will hold 65C, meat 5C and warming"
    assert pit_calls == []  # never ignites

    hass.states.async_set(PIT, "heat", {"current_temperature": 90, "temperature": 115})
    await hass.async_block_till_done()
    assert [c.data["temperature"] for c in pit_calls] == [65]
    freezer.tick(timedelta(minutes=30))
    _set(hass, 66, p2=316.1, pit=66)
    await hass.async_block_till_done()
    advice = hass.states.get("sensor.cook_planner_next_action")
    assert advice.state == "Keeping warm at 65C for 30 min"
    _set(hass, 58, p2=316.1, pit=66)  # back below 60C after being warm: alert
    await hass.async_block_till_done()
    assert hass.states.get("sensor.cook_planner_next_action").state.startswith("Meat at 58C, below 60C")
    assert advice.attributes["rest_ends"] is None
    with pytest.raises(Exception, match="end it before planning"):
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.cook_planner_generate_plan"}, blocking=True
        )

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.cook_planner_stage").state == "hold"  # survives a restart
    await _press(hass, "end_cook")
    assert hass.states.get("sensor.cook_planner_cook_log").state == "0"  # nothing cooked, nothing logged
