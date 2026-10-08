"""Next-event countdown and the G-code status sensor."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_SHOW_SECONDS, PARSE_STATES
from .coordinator import TimelineCoordinator
from .entity import TimelineEntity
from .estimator import STATUS_DONE
from .presentation import event_attributes, to_minute


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator: TimelineCoordinator = entry.runtime_data
    async_add_entities(
        [NextEventSensor(coordinator), GcodeStatusSensor(coordinator), AlertRecipientsSensor(coordinator)]
    )


class NextEventSensor(TimelineEntity, SensorEntity):
    """When the next event happens. Shown by the frontend as "in 23 minutes"."""

    _attr_translation_key = "next_event"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _unrecorded_attributes = frozenset({"label", "countdown", "description", "commands", "eta", "minutes_until"})

    def __init__(self, coordinator: TimelineCoordinator) -> None:
        super().__init__(coordinator, "next_event")

    def _next(self) -> int | None:
        session = self.coordinator.session
        if session is None:
            return None
        return next((i for i, state in enumerate(session.states) if state.status != STATUS_DONE), None)

    @property
    def native_value(self) -> datetime | None:
        index = self._next()
        return to_minute(self.coordinator.session.states[index].eta) if index is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        index = self._next()
        if index is None:
            return None
        session = self.coordinator.session
        attributes = event_attributes(
            session.events[index],
            session.states[index],
            session.parsed,
            show_seconds=bool(self.coordinator.options[CONF_SHOW_SECONDS]),
        )
        attributes["alert"] = session.alerts[index]
        attributes["slot"] = index + 1
        return attributes


class GcodeStatusSensor(TimelineEntity, SensorEntity):
    """Whether the current print's G-code was found and read."""

    _attr_translation_key = "gcode_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = PARSE_STATES
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: TimelineCoordinator) -> None:
        super().__init__(coordinator, "gcode_status")

    @property
    def native_value(self) -> str:
        return self.coordinator.parse_status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        session = self.coordinator.session
        parsed = session.parsed if session else None
        estimator = session.estimator if session else None
        return {
            "print": session.key if session else None,
            "file": session.file if session else None,
            "slicer": parsed.slicer if parsed else None,
            "slicer_total_minutes": round(parsed.total_min, 1) if parsed and parsed.total_min else None,
            "events": len(parsed.events) if parsed else None,
            "rate_factor": round(estimator.k, 3) if estimator else None,
            "pace": round(estimator.pace, 3) if estimator else None,
            "error": self.coordinator.parse_error,
        }


class AlertRecipientsSensor(TimelineEntity, SensorEntity):
    """Who gets alerts right now, as names: "Alex", "Alex, Sam" or "no one"."""

    _attr_translation_key = "alert_recipients"
    _attr_icon = "mdi:message-badge-outline"

    def __init__(self, coordinator: TimelineCoordinator) -> None:
        super().__init__(coordinator, "alert_recipients")

    @property
    def native_value(self) -> str:
        people = self.coordinator.recipient_people()
        return ", ".join(people)[:255] if people else "no one"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        targets = self.coordinator.recipient_targets()
        return {
            "people": self.coordinator.recipient_people(),
            "devices": [target.name for target in targets],
            "device_count": len(targets),
        }
