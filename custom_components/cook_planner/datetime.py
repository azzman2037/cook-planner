"""Datetime entity: when you want to eat."""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import CookPlannerConfigEntry
from .entity import CookPlannerEntity
from .manager import CookManager

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CookPlannerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the serve-by entity."""
    async_add_entities([ServeByDateTime(entry.runtime_data)])


class ServeByDateTime(CookPlannerEntity, DateTimeEntity):
    """Target serve time; the plan works backwards from it."""

    _attr_icon = "mdi:silverware-fork-knife"

    def __init__(self, manager: CookManager) -> None:
        super().__init__(manager, "serve_by")

    @property
    def native_value(self) -> datetime:
        return self.manager.serve_by

    async def async_set_value(self, value: datetime) -> None:
        self.manager.set_input("serve_by", dt_util.as_local(value))
