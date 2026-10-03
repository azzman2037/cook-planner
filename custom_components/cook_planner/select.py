"""Select entities: meat, cut and wrap method."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
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
    """Set up selects."""
    m = entry.runtime_data
    async_add_entities([MeatSelect(m, "meat"), CutSelect(m, "cut"), WrapSelect(m, "wrap")])


class MeatSelect(CookPlannerEntity, SelectEntity):
    """Which meat. Options are display names; changing it resets the cut."""

    _attr_icon = "mdi:food-steak"

    @property
    def options(self) -> list[str]:
        return list(self.manager.profiles.meats.values())

    @property
    def current_option(self) -> str | None:
        return self.manager.profiles.meats.get(self.manager.meat)

    async def async_select_option(self, option: str) -> None:
        key = next(k for k, v in self.manager.profiles.meats.items() if v == option)
        self.manager.set_meat(key)


class CutSelect(CookPlannerEntity, SelectEntity):
    """Which cut of the selected meat."""

    _attr_icon = "mdi:knife"

    @property
    def options(self) -> list[str]:
        return list(self.manager.cut_options().values())

    @property
    def current_option(self) -> str | None:
        return self.manager.cut_options().get(self.manager.cut)

    async def async_select_option(self, option: str) -> None:
        key = next(k for k, v in self.manager.cut_options().items() if v == option)
        self.manager.set_cut(key)


class WrapSelect(CookPlannerEntity, SelectEntity):
    """Wrap method (ignored for cuts that are never wrapped)."""

    _attr_icon = "mdi:package-variant"

    @property
    def options(self) -> list[str]:
        return [w.name for w in self.manager.profiles.wrap_methods.values()]

    @property
    def current_option(self) -> str | None:
        w = self.manager.profiles.wrap_methods.get(self.manager.wrap)
        return w.name if w else None

    async def async_select_option(self, option: str) -> None:
        key = next(k for k, w in self.manager.profiles.wrap_methods.items() if w.name == option)
        self.manager.set_input("wrap", key)
