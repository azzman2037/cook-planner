"""Select entities: meat, cut and wrap method."""
from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CookPlannerConfigEntry
from .const import AFTER_HOLD_OPTIONS
from .entity import CookPlannerEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CookPlannerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up selects."""
    m = entry.runtime_data
    async_add_entities(
        [MeatSelect(m, "meat"), CutSelect(m, "cut"), WrapSelect(m, "wrap"), AfterHoldSelect(m, "after_keep_warm")]
    )


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

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Per-cut details so a dashboard can badge recipes and show their prep."""
        cuts = self.manager.profiles.cuts[self.manager.meat]
        return {
            "details": {
                c.name: {
                    "recipe": c.recipe if c.is_recipe else None,
                    "pellets": c.pellets or None,
                    "prep": c.prep or None,
                    "uses_probe": c.uses_probe,
                    "steps": len(c.stages) or None,
                    "pit_c": c.pit_c,
                    "pull_c": c.pull_c,
                }
                for c in cuts.values()
            }
        }


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


class AfterHoldSelect(CookPlannerEntity, SelectEntity):
    """What happens when keeping warm ends; every ending shuts down with the fan cool-down."""

    _attr_icon = "mdi:fire-off"

    @property
    def options(self) -> list[str]:
        return list(AFTER_HOLD_OPTIONS.values())

    @property
    def current_option(self) -> str | None:
        return AFTER_HOLD_OPTIONS.get(self.manager.after_hold)

    async def async_select_option(self, option: str) -> None:
        key = next(k for k, v in AFTER_HOLD_OPTIONS.items() if v == option)
        self.manager.set_input("after_hold", key)
