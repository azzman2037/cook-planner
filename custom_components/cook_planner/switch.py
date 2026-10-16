"""Switch: let the planner set the pit temperature at each planned step."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CookPlannerConfigEntry
from .entity import CookPlannerEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CookPlannerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the switch."""
    async_add_entities([AutoPitSwitch(entry.runtime_data, "auto_pit")])


class AutoPitSwitch(CookPlannerEntity, SwitchEntity):
    """On: each step's pit temperature is sent to the grill. Ignition stays manual."""

    _attr_icon = "mdi:thermometer-auto"

    @property
    def is_on(self) -> bool:
        return self.manager.auto_pit

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.manager.set_auto_pit(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.manager.set_auto_pit(False)
