"""Setup: pick a printer from ha-bambulab. Options: alert and display settings."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)

from .const import (
    BAMBU_DOMAIN,
    CONF_ALERT_ON_EVENT,
    CONF_BAMBU_ENTRY_ID,
    CONF_CUSTOM_GCODE_ALERTS,
    CONF_DEFAULT_RECIPIENTS,
    CONF_EVENT_SLOTS,
    CONF_LEAD_TIME,
    CONF_MARGIN_MIN,
    CONF_MARGIN_PCT,
    CONF_MODEL,
    CONF_SERIAL,
    CONF_SHOW_FINISHED,
    DEFAULT_OPTIONS,
    DOMAIN,
)
from .notifier import notify_targets


class BambuTimelineConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        printers = [
            entry for entry in self.hass.config_entries.async_entries(BAMBU_DOMAIN) if entry.data.get("serial")
        ]
        if not printers:
            return self.async_abort(reason="no_printers")

        if user_input is not None:
            bambu_entry = self.hass.config_entries.async_get_entry(user_input[CONF_BAMBU_ENTRY_ID])
            if bambu_entry is None:
                return self.async_abort(reason="no_printers")
            serial = bambu_entry.data["serial"]
            await self.async_set_unique_id(serial)
            self._abort_if_unique_id_configured()
            model = bambu_entry.data.get("device_type") or "Printer"
            return self.async_create_entry(
                title=f"{model} timeline",
                data={CONF_BAMBU_ENTRY_ID: bambu_entry.entry_id, CONF_SERIAL: serial, CONF_MODEL: model},
                options={CONF_DEFAULT_RECIPIENTS: user_input.get(CONF_DEFAULT_RECIPIENTS, [])},
            )

        options = [
            SelectOptionDict(
                value=entry.entry_id,
                label=f"{entry.data.get('device_type', 'Printer')} {entry.data['serial']} ({entry.title})",
            )
            for entry in printers
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_BAMBU_ENTRY_ID, default=options[0]["value"]): SelectSelector(
                    SelectSelectorConfig(options=options)
                ),
                vol.Optional(CONF_DEFAULT_RECIPIENTS, default=[]): _recipient_selector(self.hass),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> BambuTimelineOptionsFlow:
        return BambuTimelineOptionsFlow()


def _recipient_selector(hass: HomeAssistant) -> SelectSelector:
    options = [
        SelectOptionDict(value=target.entry_id, label=target.name) for target in notify_targets(hass).values()
    ]
    return SelectSelector(SelectSelectorConfig(options=options, multiple=True))


def _number(minimum: float, maximum: float, step: float, unit: str | None = None) -> NumberSelector:
    config = NumberSelectorConfig(min=minimum, max=maximum, step=step, mode=NumberSelectorMode.BOX)
    if unit:
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


class BambuTimelineOptionsFlow(OptionsFlowWithReload):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={**self.config_entry.options, **user_input})

        current = {**DEFAULT_OPTIONS, **self.config_entry.options}
        known = notify_targets(self.hass)
        recipients = [entry_id for entry_id in current[CONF_DEFAULT_RECIPIENTS] if entry_id in known]
        schema = vol.Schema(
            {
                vol.Optional(CONF_DEFAULT_RECIPIENTS, default=recipients): _recipient_selector(self.hass),
                vol.Required(CONF_LEAD_TIME, default=current[CONF_LEAD_TIME]): _number(0, 120, 1, "min"),
                vol.Required(CONF_ALERT_ON_EVENT, default=current[CONF_ALERT_ON_EVENT]): BooleanSelector(),
                vol.Required(
                    CONF_CUSTOM_GCODE_ALERTS, default=current[CONF_CUSTOM_GCODE_ALERTS]
                ): BooleanSelector(),
                vol.Required(CONF_SHOW_FINISHED, default=current[CONF_SHOW_FINISHED]): BooleanSelector(),
                vol.Required(CONF_MARGIN_MIN, default=current[CONF_MARGIN_MIN]): _number(0, 15, 0.5, "min"),
                vol.Required(CONF_MARGIN_PCT, default=current[CONF_MARGIN_PCT]): _number(0, 25, 0.5, "%"),
                vol.Required(CONF_EVENT_SLOTS, default=current[CONF_EVENT_SLOTS]): _number(1, 30, 1),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
