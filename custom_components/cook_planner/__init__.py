"""Cook Planner: plan, forecast and track low-and-slow cooks in Home Assistant."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .manager import CookManager

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.DATETIME,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
]

type CookPlannerConfigEntry = ConfigEntry[CookManager]


async def async_setup_entry(hass: HomeAssistant, entry: CookPlannerConfigEntry) -> bool:
    """Set up a Cook Planner entry."""
    manager = CookManager(hass, entry.entry_id, {**entry.data, **entry.options})
    await manager.async_setup()
    entry.runtime_data = manager
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: CookPlannerConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: CookPlannerConfigEntry) -> bool:
    """Unload a Cook Planner entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_unload()
    return unloaded
