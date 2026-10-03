"""Number entities: weight and thickness."""
from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfLength, UnitOfMass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CookPlannerConfigEntry
from .entity import CookPlannerEntity
from .manager import CookManager

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CookPlannerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up numbers."""
    m = entry.runtime_data
    async_add_entities([WeightNumber(m), ThicknessNumber(m)])


class WeightNumber(CookPlannerEntity, NumberEntity):
    """Raw weight of the meat."""

    _attr_device_class = NumberDeviceClass.WEIGHT
    _attr_native_unit_of_measurement = UnitOfMass.KILOGRAMS
    _attr_native_min_value = 0.2
    _attr_native_max_value = 15
    _attr_native_step = 0.1
    _attr_mode = NumberMode.BOX

    def __init__(self, manager: CookManager) -> None:
        super().__init__(manager, "weight")

    @property
    def native_value(self) -> float:
        return self.manager.weight_kg

    async def async_set_native_value(self, value: float) -> None:
        self.manager.set_input("weight_kg", round(value, 2))


class ThicknessNumber(CookPlannerEntity, NumberEntity):
    """Thickness at the thickest point; 0 = not measured."""

    _attr_device_class = NumberDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = UnitOfLength.CENTIMETERS
    _attr_native_min_value = 0
    _attr_native_max_value = 25
    _attr_native_step = 0.5
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:arrow-expand-vertical"

    def __init__(self, manager: CookManager) -> None:
        super().__init__(manager, "thickness")

    @property
    def native_value(self) -> float:
        return self.manager.thickness_cm

    async def async_set_native_value(self, value: float) -> None:
        self.manager.set_input("thickness_cm", value)
