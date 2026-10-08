"""Switches: one per event slot (that event's alerts on/off), and one per phone (notify it during this print)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_EVENT_SLOTS, CONF_SHOW_FINISHED, CONF_SHOW_SECONDS, DOMAIN
from .coordinator import TimelineCoordinator
from .entity import TimelineEntity
from .estimator import STATUS_DONE
from .notifier import NotifyTarget, notify_targets
from .presentation import event_attributes


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator: TimelineCoordinator = entry.runtime_data
    slots = int(coordinator.options[CONF_EVENT_SLOTS])
    targets = notify_targets(hass)
    async_add_entities(
        [
            *(EventAlertSwitch(coordinator, index) for index in range(slots)),
            *(RecipientSwitch(coordinator, target) for target in targets.values()),
        ]
    )

    # Remove event slots left over from a larger "maximum number of events" setting,
    # and phones that no longer have the Companion app set up.
    registry = er.async_get(hass)
    wanted = {f"{coordinator.serial}_event_{index + 1}" for index in range(slots)}
    wanted |= {f"{coordinator.serial}_notify_{entry_id}" for entry_id in targets}
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            registry_entry.domain == "switch"
            and registry_entry.unique_id.startswith((f"{coordinator.serial}_event_", f"{coordinator.serial}_notify_"))
            and registry_entry.unique_id not in wanted
        ):
            registry.async_remove(registry_entry.entity_id)


class EventAlertSwitch(TimelineEntity, SwitchEntity):
    _attr_translation_key = "event"
    # The event list is shown live; there is no value in recording it in history.
    _unrecorded_attributes = frozenset(
        {"label", "countdown", "description", "commands", "eta", "minutes_until"}
    )

    def __init__(self, coordinator: TimelineCoordinator, index: int) -> None:
        super().__init__(coordinator, f"event_{index + 1}")
        self.index = index
        self._attr_translation_placeholders = {"slot": str(index + 1)}

    @property
    def available(self) -> bool:
        """Unused slots, and finished events unless they are kept on the list, are unavailable.

        That keeps the dashboard filter trivial: show every available event switch.
        """
        session = self.coordinator.session
        if session is None or self.index >= len(session.events):
            return False
        return session.states[self.index].status != STATUS_DONE or bool(self.coordinator.options[CONF_SHOW_FINISHED])

    @property
    def is_on(self) -> bool | None:
        session = self.coordinator.session
        if session is None or self.index >= len(session.alerts):
            return None
        return session.alerts[self.index]

    @property
    def icon(self) -> str:
        if self._finished():
            return "mdi:check-circle-outline"
        return "mdi:bell-ring" if self.is_on else "mdi:bell-off-outline"

    def _finished(self) -> bool:
        session = self.coordinator.session
        return (
            session is not None
            and self.index < len(session.states)
            and session.states[self.index].status == STATUS_DONE
        )

    def _refuse_if_finished(self) -> None:
        # An alert for something that has already happened can't go out any more.
        if self._finished():
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="event_finished")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        session = self.coordinator.session
        if session is None or self.index >= len(session.events):
            return None
        return {
            "role": "event",
            **event_attributes(
                session.events[self.index],
                session.states[self.index],
                session.parsed,
                show_seconds=bool(self.coordinator.options[CONF_SHOW_SECONDS]),
            ),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._refuse_if_finished()
        self.coordinator.set_alert(self.index, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._refuse_if_finished()
        self.coordinator.set_alert(self.index, False)


class RecipientSwitch(TimelineEntity, SwitchEntity):
    """Whether this phone gets notifications for the current print.

    Opt-in is per print: when a print ends, every phone goes back to the default
    set in the options. Changing it between prints applies to the next print.
    """

    _attr_translation_key = "notify"

    def __init__(self, coordinator: TimelineCoordinator, target: NotifyTarget) -> None:
        super().__init__(coordinator, f"notify_{target.entry_id}")
        self.target = target
        self._attr_translation_placeholders = {"device": target.name}

    @property
    def is_on(self) -> bool:
        return self.target.entry_id in self.coordinator.recipients

    @property
    def icon(self) -> str:
        return "mdi:cellphone-message" if self.is_on else "mdi:cellphone-off"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # "role" lets dashboard filters find these switches whatever their entity IDs are.
        return {"role": "recipient", "device": self.target.name, "person": self.coordinator.owner(self.target)}

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.set_recipient(self.target.entry_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.set_recipient(self.target.entry_id, False)
