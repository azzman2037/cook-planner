"""Base entity for Cook Planner."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .manager import CookManager


class CookPlannerEntity(Entity):
    """Entity bound to the entry's CookManager."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, manager: CookManager, key: str) -> None:
        """Initialise."""
        self.manager = manager
        self._attr_translation_key = key
        self._attr_unique_id = f"{manager.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, manager.entry_id)},
            name="Cook Planner",
            manufacturer="Cook Planner",
            model="Low & slow planner",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to manager updates."""
        self.async_on_remove(self.manager.async_add_listener(self.async_write_ha_state))
