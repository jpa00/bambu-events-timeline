"""Phone notifications through the Home Assistant Companion app.

Every phone or tablet with the Companion app has its own notify service,
``notify.mobile_app_<device name>``. Recipients are kept as the Companion
app's config entry IDs, which survive renaming the device.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.core import HomeAssistant
from homeassistant.util import slugify

from .const import LOGGER, MOBILE_APP_DOMAIN
from .estimator import EventState
from .gcode_parser import KIND_PAUSE, ParsedPrint, TimelineEvent
from .presentation import clock, duration_text, event_label, layer_text, whole_minutes

NOTIFY_DOMAIN = "notify"
CLEAR_MESSAGE = "clear_notification"


@dataclass(frozen=True)
class NotifyTarget:
    entry_id: str
    name: str
    user_id: str | None = None
    """The Home Assistant user who set up the Companion app on this device."""

    @property
    def service(self) -> str:
        return f"mobile_app_{slugify(self.name)}"


def notify_targets(hass: HomeAssistant) -> dict[str, NotifyTarget]:
    """Every device with the Companion app, by config entry ID."""
    targets = {}
    for entry in hass.config_entries.async_entries(MOBILE_APP_DOMAIN):
        name = entry.data.get("device_name") or entry.title
        if name:
            targets[entry.entry_id] = NotifyTarget(entry.entry_id, name, entry.data.get("user_id"))
    return targets


def owner_name(hass: HomeAssistant, target: NotifyTarget, user_names: dict[str, str]) -> str:
    """The person a device belongs to: their person entity's name, else their user name, else the device name."""
    if target.user_id:
        for person in hass.states.async_all("person"):
            if person.attributes.get("user_id") == target.user_id:
                return person.name
        if name := user_names.get(target.user_id):
            return name
    return target.name


@dataclass(frozen=True)
class Notification:
    title: str
    message: str
    tag: str


def lead_notification(
    model: str, tag: str, event: TimelineEvent, state: EventState, parsed: ParsedPrint | None
) -> Notification:
    minutes = whole_minutes(state.minutes_until) or 0
    when = f" (about {clock(state.eta)})" if state.eta else ""
    what = "pause" if event.kind == KIND_PAUSE else event.description
    return Notification(
        title=f"{model}: {what} in {duration_text(minutes)}",
        message=f"{event_label(event, parsed)}{when}.",
        tag=tag,
    )


def event_notification(model: str, tag: str, event: TimelineEvent, parsed: ParsedPrint | None) -> Notification:
    if event.kind == KIND_PAUSE:
        detail = f" {event.message}." if event.message else ""
        return Notification(
            title=f"{model} paused",
            message=f"Planned pause at {layer_text(event, parsed)}.{detail} Waiting for you.",
            tag=tag,
        )
    return Notification(
        title=f"{model} reached {layer_text(event, parsed)}",
        message=f"{event.description}.",
        tag=tag,
    )


def _payload(notification: Notification) -> dict:
    if notification.message == CLEAR_MESSAGE:
        return {"message": CLEAR_MESSAGE, "data": {"tag": notification.tag}}
    return {
        "title": notification.title,
        "message": notification.message,
        "data": {
            # Same tag for an event's heads-up and its "it happened" alert, so the second replaces the first.
            "tag": notification.tag,
            "group": "bambu_timeline",
            # Android: a separate notification channel the user can tune, shown with high priority.
            "channel": "Printer events",
            "importance": "high",
            # iOS: allowed through Focus modes.
            "push": {"interruption-level": "time-sensitive"},
        },
    }


async def async_send(hass: HomeAssistant, targets: list[NotifyTarget], notification: Notification) -> int:
    """Send to each target. Returns how many sends succeeded. Failures are logged, never raised."""
    sent = 0
    payload = _payload(notification)
    for target in targets:
        if not hass.services.has_service(NOTIFY_DOMAIN, target.service):
            LOGGER.warning("Can't notify %s: there is no notify.%s service", target.name, target.service)
            continue
        try:
            await hass.services.async_call(NOTIFY_DOMAIN, target.service, payload, blocking=True)
            sent += 1
        except Exception:  # noqa: BLE001 - one failing phone must not stop the others
            LOGGER.exception("Sending a notification to %s failed", target.name)
    return sent


def clear_notification(tag: str) -> Notification:
    return Notification(title="", message=CLEAR_MESSAGE, tag=tag)
