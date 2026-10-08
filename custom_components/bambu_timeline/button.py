"""A button that sends a test notification to the phones currently opted in."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TimelineCoordinator
from .entity import TimelineEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([TestNotificationButton(entry.runtime_data)])


class TestNotificationButton(TimelineEntity, ButtonEntity):
    _attr_translation_key = "test_notification"
    _attr_icon = "mdi:message-badge-outline"

    def __init__(self, coordinator: TimelineCoordinator) -> None:
        super().__init__(coordinator, "test_notification")

    async def async_press(self) -> None:
        if not self.coordinator.recipient_targets():
            raise HomeAssistantError("No phone is set to get notifications. Turn one on first.")
        if await self.coordinator.async_send_test() == 0:
            raise HomeAssistantError("The test notification could not be sent. See the Home Assistant log.")
