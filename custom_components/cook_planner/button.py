"""Buttons that drive the cook through its stages."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CookPlannerConfigEntry
from .entity import CookPlannerEntity
from .manager import CookManager

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class CookButtonDescription(ButtonEntityDescription):
    """Button bound to a manager action."""

    press_fn: Callable[[CookManager], None]


BUTTONS: tuple[CookButtonDescription, ...] = (
    CookButtonDescription(key="generate_plan", icon="mdi:calendar-clock", press_fn=lambda m: m.generate_plan()),
    CookButtonDescription(key="start", icon="mdi:fire", press_fn=lambda m: m.start()),
    CookButtonDescription(key="meat_on", icon="mdi:food-drumstick", press_fn=lambda m: m.mark_meat_on()),
    CookButtonDescription(key="wrapped", icon="mdi:package-variant-closed", press_fn=lambda m: m.mark_wrapped()),
    CookButtonDescription(key="pulled", icon="mdi:timer-sand", press_fn=lambda m: m.mark_pulled()),
    CookButtonDescription(key="end", icon="mdi:flag-checkered", press_fn=lambda m: m.end()),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CookPlannerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up buttons."""
    async_add_entities(CookButton(entry.runtime_data, d) for d in BUTTONS)


class CookButton(CookPlannerEntity, ButtonEntity):
    """A stage-advancing button."""

    entity_description: CookButtonDescription

    def __init__(self, manager: CookManager, description: CookButtonDescription) -> None:
        super().__init__(manager, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        self.entity_description.press_fn(self.manager)
