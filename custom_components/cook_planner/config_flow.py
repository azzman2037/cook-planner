"""Config flow for Cook Planner."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import callback
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from .const import CONF_PIT_ENTITY, CONF_PROBE_1, CONF_PROBE_2, DOMAIN

_TEMP_SENSOR = EntitySelector(EntitySelectorConfig(domain="sensor", device_class="temperature"))
_PIT = EntitySelector(
    EntitySelectorConfig(
        filter=[{"domain": "climate"}, {"domain": "sensor", "device_class": "temperature"}]
    )
)


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_PIT_ENTITY, default=defaults.get(CONF_PIT_ENTITY, vol.UNDEFINED)): _PIT,
            vol.Required(CONF_PROBE_1, default=defaults.get(CONF_PROBE_1, vol.UNDEFINED)): _TEMP_SENSOR,
            vol.Optional(
                CONF_PROBE_2,
                description={"suggested_value": defaults.get(CONF_PROBE_2)},
            ): _TEMP_SENSOR,
        }
    )


def _validate(user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if user_input.get(CONF_PROBE_2) and user_input[CONF_PROBE_2] == user_input[CONF_PROBE_1]:
        errors[CONF_PROBE_2] = "same_probe"
    return errors


class CookPlannerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pick the smoker's pit and probe entities."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the user step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                await self.async_set_unique_id(user_input[CONF_PROBE_1])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Cook Planner", data=user_input)
        return self.async_show_form(
            step_id="user", data_schema=_schema(user_input or {}), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow."""
        return CookPlannerOptionsFlow()


class CookPlannerOptionsFlow(OptionsFlowWithReload):
    """Re-point the planner at different entities."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                return self.async_create_entry(data=user_input)
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(current), errors=errors)
