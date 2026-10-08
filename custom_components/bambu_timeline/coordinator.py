"""Follows the printer, finds and parses the G-code, and keeps the event states up to date."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    BAMBU_CACHE_DIR,
    BAMBU_DOMAIN,
    BAMBU_KEYS,
    CALIBRATION_MAX_SAMPLES,
    CALIBRATION_SAMPLE_INTERVAL_S,
    CONF_ALERT_ON_EVENT,
    CONF_BAMBU_ENTRY_ID,
    CONF_CUSTOM_GCODE_ALERTS,
    CONF_DEFAULT_RECIPIENTS,
    CONF_LEAD_TIME,
    CONF_MARGIN_MIN,
    CONF_MARGIN_PCT,
    CONF_MODEL,
    CONF_SERIAL,
    CONF_SHOW_SECONDS,
    DEFAULT_OPTIONS,
    DOMAIN,
    EXTERNAL_SPOOL_ACTIVE_SUFFIX,
    EXTERNAL_SPOOL_AMS_INDEXES,
    EXTERNAL_SPOOL_MARKER,
    EXTERNAL_SPOOL_SENSOR_SUFFIX,
    FAST_UPDATE_INTERVAL_S,
    FILE_SEARCH_INTERVAL_S,
    FILE_SEARCH_TIMEOUT_S,
    LOGGER,
    OPTIONAL_BAMBU_KEYS,
    PARSE_ERROR,
    PARSE_IDLE,
    PARSE_NO_EVENTS,
    PARSE_NOT_FOUND,
    PARSE_OK,
    PARSE_PARSING,
    PARSE_WAITING,
    PRINT_ACTIVE_STATES,
    PRINT_ENDED_STATES,
    STALE_FILE_GRACE_S,
    STORAGE_VERSION,
    UPDATE_INTERVAL_S,
)
from .estimator import (
    STATUS_ACTIVE,
    STATUS_DONE,
    STATUS_UPCOMING,
    Estimator,
    EventState,
    MinuteInterpolator,
    PrinterSnapshot,
)
from .file_finder import find_gcode
from .gcode_parser import KIND_PAUSE, ParsedPrint, TimelineEvent, parse_gcode_file
from .notifier import (
    Notification,
    NotifyTarget,
    async_send,
    clear_notification,
    event_notification,
    lead_notification,
    notify_targets,
    owner_name,
)
from .presentation import event_attributes

_UNIT_TO_MINUTES = {"min": 1.0, "h": 60.0, "s": 1 / 60, "d": 1440.0, "ms": 1 / 60000}


@dataclass
class PrintSession:
    """One print, from the moment it is noticed until it ends."""

    key: str
    started: datetime
    file: str | None = None
    parsed: ParsedPrint | None = None
    estimator: Estimator | None = None
    states: list[EventState] = field(default_factory=list)
    alerts: list[bool] = field(default_factory=list)
    notified_lead: list[bool] = field(default_factory=list)
    notified_event: list[bool] = field(default_factory=list)
    primed: bool = False
    """False until the first update after parsing. Notifications only go out for changes seen live,
    not for events that were already past when the print was first seen."""
    samples: list[dict[str, Any]] = field(default_factory=list)
    last_sample: datetime | None = None
    calibration: list[dict[str, Any]] = field(default_factory=list)
    """Per event: what was predicted 5 and 1 minutes ahead, and when it actually happened."""
    search_task: asyncio.Task | None = None

    @property
    def events(self) -> list[TimelineEvent]:
        return self.parsed.events if self.parsed else []


class TimelineCoordinator:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.serial: str = entry.data[CONF_SERIAL]
        self.model: str = entry.data.get(CONF_MODEL) or "Printer"
        self.entity_ids: dict[str, str] = {}
        self.spool_entity_ids: list[str] = []
        self.session: PrintSession | None = None
        self.parse_status = PARSE_IDLE
        self.parse_error: str | None = None
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._tracking_unsub: CALLBACK_TYPE | None = None
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._stored: dict[str, Any] = {}
        self._evaluation_pending = False
        self._stopped = False
        self._user_names: dict[str, str] = {}
        self._minute_clock = MinuteInterpolator()
        self._fast_refresh_unsub: CALLBACK_TYPE | None = None
        self.last_print: dict[str, Any] | None = None
        """Summary and logs of the last finished print, kept for diagnostics until the next one ends."""
        self.recipients: set[str] = self._default_recipients()
        # The defaults that `recipients` was last reconciled with. Saved, so a change to the
        # defaults in the options can be applied when the integration reloads.
        self._applied_defaults: set[str] = set(self.recipients)

    # ----- setup -------------------------------------------------------------------------

    @property
    def options(self) -> dict[str, Any]:
        return {**DEFAULT_OPTIONS, **self.entry.options}

    async def async_start(self) -> None:
        self._stored = await self._store.async_load() or {}
        self.last_print = self._stored.get("last_print")
        for target in notify_targets(self.hass).values():
            if target.user_id and (user := await self.hass.auth.async_get_user(target.user_id)):
                self._user_names[target.user_id] = user.name or ""
        self._restore_recipients()
        self._ensure_tracking()
        self._unsubs.append(
            async_track_time_interval(self.hass, self._on_tick, timedelta(seconds=UPDATE_INTERVAL_S))
        )
        self._evaluate(printer_changed=True)

    async def async_stop(self) -> None:
        self._stopped = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._set_fast_refresh(False)
        if self._tracking_unsub:
            self._tracking_unsub()
            self._tracking_unsub = None
        if self.session and self.session.search_task:
            self.session.search_task.cancel()
        await self._store.async_save(self._storage_data())

    def _ensure_tracking(self) -> None:
        """Look up the printer's entities and listen to them.

        Done at setup and when a print starts (the spool setup can't change in a way that matters
        during a print), and retried on every tick while required sensors are still missing.
        """
        registry = er.async_get(self.hass)
        found = {}
        printer_entities: list[er.RegistryEntry] | None = None
        for key in BAMBU_KEYS:
            entity_id = registry.async_get_entity_id("sensor", BAMBU_DOMAIN, f"{self.serial}_{key}")
            if entity_id is None:
                # Not under the usual ID (older ha-bambulab installs can differ): look for the sensor
                # of that kind on the printer's own device instead.
                if printer_entities is None:
                    printer_entities = self._printer_device_entities(registry)
                entity_id = next(
                    (
                        entry.entity_id
                        for entry in printer_entities
                        if entry.domain == "sensor"
                        and (entry.translation_key == key or entry.unique_id.endswith(f"_{key}"))
                    ),
                    None,
                )
            if entity_id:
                found[key] = entity_id
        spools = self._external_spool_entities(registry)
        if found == self.entity_ids and spools == self.spool_entity_ids and self._tracking_unsub:
            return
        if self.missing_printer_sensors:
            LOGGER.debug(
                "Printer %s: sensors not found yet: %s", self.serial, ", ".join(self.missing_printer_sensors)
            )
        self.entity_ids = found
        self.spool_entity_ids = spools
        if self._tracking_unsub:
            self._tracking_unsub()
            self._tracking_unsub = None
        tracked = [*found.values(), *spools]
        if tracked:
            self._tracking_unsub = async_track_state_change_event(self.hass, tracked, self._on_printer_change)

    def _external_spool_entities(self, registry: er.EntityRegistry) -> list[str]:
        """The external spool device's filament sensor and in-use binary sensor (ha-bambulab 2.2+)."""
        bambu_entry_id = self.entry.data.get(CONF_BAMBU_ENTRY_ID)
        if not bambu_entry_id:
            return []
        marker = f"{self.serial}{EXTERNAL_SPOOL_MARKER}"
        return sorted(
            entry.entity_id
            for entry in er.async_entries_for_config_entry(registry, bambu_entry_id)
            if marker in entry.unique_id
            and (
                (entry.domain == "sensor" and entry.unique_id.endswith(EXTERNAL_SPOOL_SENSOR_SUFFIX))
                or (entry.domain == "binary_sensor" and entry.unique_id.endswith(EXTERNAL_SPOOL_ACTIVE_SUFFIX))
            )
        )

    def _printer_device_entities(self, registry: er.EntityRegistry) -> list[er.RegistryEntry]:
        devices = dr.async_get(self.hass)
        for device in devices.devices.values():
            if (BAMBU_DOMAIN, self.serial) in device.identifiers:
                return er.async_entries_for_device(registry, device.id)
        return []

    @property
    def missing_printer_sensors(self) -> list[str]:
        """Required printer sensors that weren't found. (The active tray sensor only exists with an AMS.)"""
        return sorted(set(BAMBU_KEYS) - set(OPTIONAL_BAMBU_KEYS) - set(self.entity_ids))

    # ----- listeners ---------------------------------------------------------------------

    @callback
    def async_add_listener(self, update_callback: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(update_callback)

        @callback
        def remove() -> None:
            self._listeners.remove(update_callback)

        return remove

    def _notify_listeners(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    @callback
    def _on_printer_change(self, event: Event[EventStateChangedData]) -> None:
        # ha-bambulab writes its sensors one after another for each printer report. Wait until
        # they are all written, so layer, progress and remaining time are read as one consistent set.
        if not self._evaluation_pending:
            self._evaluation_pending = True
            self.hass.loop.call_soon(self._run_pending_evaluation)

    @callback
    def _run_pending_evaluation(self) -> None:
        self._evaluation_pending = False
        if not self._stopped:
            self._evaluate(printer_changed=True)

    @callback
    def _on_tick(self, _now: datetime) -> None:
        if self.missing_printer_sensors:
            # E.g. ha-bambulab hasn't finished setting up after a restart.
            self._ensure_tracking()
        self._evaluate(printer_changed=False)

    # ----- reading the printer -----------------------------------------------------------

    def _state(self, key: str) -> str | None:
        entity_id = self.entity_ids.get(key)
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
            return None
        return state.state

    def _number(self, key: str) -> float | None:
        value = self._state(key)
        try:
            return float(value) if value is not None else None
        except ValueError:
            return None

    def _minutes(self, key: str) -> float | None:
        value = self._number(key)
        if value is None:
            return None
        state = self.hass.states.get(self.entity_ids[key])
        unit = state.attributes.get("unit_of_measurement", "min") if state else "min"
        return value * _UNIT_TO_MINUTES.get(unit, 1.0)

    def snapshot(self) -> PrinterSnapshot:
        layer = self._number("current_layer")
        return PrinterSnapshot(
            status=self._state("print_status"),
            remaining_min=self._minutes("remaining_time"),
            progress_pct=self._number("print_progress"),
            layer=int(layer) if layer is not None else None,
            speed=self._state("speed_profile"),
            on_external_spool=self._on_external_spool(),
        )

    def _on_external_spool(self) -> bool:
        """Whether the printer is feeding from the external spool rather than an AMS."""
        for entity_id in self.spool_entity_ids:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            if state.domain == "binary_sensor" and state.state == "on":
                return True
            if state.domain == "sensor" and state.attributes.get("active") is True:
                return True
        # With an AMS connected, the printer's active tray sensor says it too.
        if self._state("active_tray") in (None, "none"):
            return False
        state = self.hass.states.get(self.entity_ids["active_tray"])
        try:
            return int(state.attributes.get("ams_index")) in EXTERNAL_SPOOL_AMS_INDEXES
        except (TypeError, ValueError):
            return False

    def _print_key(self) -> str:
        """The print's name, or "" while it isn't known (e.g. the sensor is briefly unavailable).

        Only the task name is used: falling back to the file name would make a blip in the
        task name sensor look like a different print.
        """
        return self._state("subtask_name") or ""

    # ----- print lifecycle ---------------------------------------------------------------

    @callback
    def _evaluate(self, printer_changed: bool) -> None:
        snap = self.snapshot()
        now = dt_util.utcnow()
        if printer_changed:
            self._minute_clock.observe(snap.remaining_min, snap.status, now)
        snap.remaining_precise = self._minute_clock.refine(snap.remaining_min, now)

        if snap.status in PRINT_ACTIVE_STATES:
            key = self._print_key()
            if self.session is None:
                self._start_session(key, now)
            elif key and self.session.key and key != self.session.key:
                LOGGER.debug("Print changed from %s to %s", self.session.key, key)
                self._end_session()
                self._start_session(key, now)
            elif key and not self.session.key:
                self.session.key = key
        elif snap.status in PRINT_ENDED_STATES and self.session is not None:
            self._end_session()

        session = self.session
        if session and session.estimator:
            if printer_changed:
                session.estimator.observe(snap, now)
            before = [(s.status, s.status_since) for s in session.states]
            before_raw = [s.raw_minutes_until for s in session.states]
            session.estimator.update(snap, session.states, now)
            if session.primed:
                self._check_notifications(session, [status for status, _ in before])
                self._record_calibration(session, [status for status, _ in before], before_raw, now)
            else:
                session.primed = True
            if before != [(s.status, s.status_since) for s in session.states]:
                self._save()
            self._sample(session, snap, now)

        self._set_fast_refresh(self._needs_fast_refresh())
        self._notify_listeners()

    def _start_session(self, key: str, now: datetime) -> None:
        started = now
        if self._stored.get("key") and self._stored.get("key") == key and self._stored.get("started"):
            started = dt_util.parse_datetime(self._stored["started"]) or now
        LOGGER.debug("Print started: %s", key or "(name not known yet)")
        # Pick up AMS or spool devices ha-bambulab added or removed since the last print.
        self._ensure_tracking()
        self.session = PrintSession(key=key, started=started)
        self._minute_clock = MinuteInterpolator()
        self.parse_status = PARSE_WAITING
        self.parse_error = None
        self.session.search_task = self.entry.async_create_background_task(
            self.hass, self._find_and_parse(self.session), f"{DOMAIN} find G-code"
        )

    def _end_session(self) -> None:
        session = self.session
        if session and session.search_task:
            session.search_task.cancel()
        LOGGER.debug("Print ended: %s", session.key if session else None)
        if session:
            # Don't leave "paused, waiting for you" on anyone's phone after the print is over.
            for index, state in enumerate(session.states):
                if session.notified_event[index] and state.status == STATUS_ACTIVE:
                    self._send(clear_notification(self._tag(index)))
            if session.parsed:
                self.last_print = self.print_summary(session, ended=dt_util.utcnow())
        self.session = None
        self.parse_status = PARSE_IDLE
        self.parse_error = None
        # Recipients are opted in per print: back to the defaults for the next one.
        self.recipients = self._default_recipients()
        self._applied_defaults = set(self.recipients)
        self._stored = {}
        self._save()

    def cache_roots(self) -> list[Path]:
        roots = [Path(self.hass.config.path(BAMBU_CACHE_DIR, self.serial))]
        local_media = self.hass.config.media_dirs.get("local")
        if local_media:
            roots.append(Path(local_media) / "ha-bambulab" / self.serial)
        return roots

    async def _find_and_parse(self, session: PrintSession) -> None:
        deadline = time.monotonic() + FILE_SEARCH_TIMEOUT_S
        previous: tuple[Path, int] | None = None
        path: Path | None = None
        while session is self.session:
            names =[session.key, self._state("subtask_name") or "", self._state("gcode_file") or ""]
            found = await self.hass.async_add_executor_job(
                find_gcode,
                self.cache_roots(),
                names,
                session.started.timestamp(),
                time.time(),
                STALE_FILE_GRACE_S,
            )
            if found is not None:
                size = await self.hass.async_add_executor_job(_file_size, found)
                # Only parse once the file has stopped growing.
                if previous == (found, size):
                    path = found
                    break
                previous = (found, size)
            elif time.monotonic() > deadline:
                LOGGER.warning(
                    "No cached G-code found for print '%s' in %s. Is the printer's IP address set in ha-bambulab?",
                    session.key,
                    ", ".join(str(root) for root in self.cache_roots()),
                )
                self._set_parse_status(session, PARSE_NOT_FOUND)
                return
            await asyncio.sleep(FILE_SEARCH_INTERVAL_S)
        if path is None:
            return

        self._set_parse_status(session, PARSE_PARSING)
        try:
            parsed = await self.hass.async_add_executor_job(parse_gcode_file, path)
        except Exception as err:  # noqa: BLE001 - any parse failure is reported, never raised
            LOGGER.exception("Could not parse %s", path)
            session.file = str(path)
            self.parse_error = str(err)
            self._set_parse_status(session, PARSE_ERROR)
            return

        if session is not self.session:
            return
        session.file = str(path)
        session.parsed = parsed
        session.states = [EventState() for _ in parsed.events]
        session.alerts = [self._default_alert(event) for event in parsed.events]
        session.notified_lead = [False] * len(parsed.events)
        session.notified_event = [False] * len(parsed.events)
        session.calibration = [{} for _ in parsed.events]
        k = 1.0
        stored = self._stored
        if stored.get("key") == session.key and len(stored.get("events", [])) == len(parsed.events):
            k = float(stored.get("k", 1.0))
            for index, (state, saved) in enumerate(zip(session.states, stored["events"])):
                session.alerts[index] = bool(saved.get("alert", session.alerts[index]))
                session.notified_lead[index] = bool(saved.get("notified_lead", False))
                session.notified_event[index] = bool(saved.get("notified_event", False))
                session.calibration[index] = dict(saved.get("calibration", {}))
                state.status = saved.get("status", state.status)
                if saved.get("status_since"):
                    state.status_since = dt_util.parse_datetime(saved["status_since"])
            LOGGER.debug("Restored saved state for %s", session.key)
        options = self.options
        session.estimator = Estimator(
            parsed,
            margin_min=float(options[CONF_MARGIN_MIN]),
            margin_pct=float(options[CONF_MARGIN_PCT]) / 100,
            k=k,
        )
        LOGGER.debug("Parsed %s: %d events", path, len(parsed.events))
        self._set_parse_status(session, PARSE_OK if parsed.events else PARSE_NO_EVENTS)
        self._save()
        self._evaluate(printer_changed=False)

    def _set_parse_status(self, session: PrintSession, status: str) -> None:
        if session is self.session:
            self.parse_status = status
            self._notify_listeners()

    def _default_alert(self, event: TimelineEvent) -> bool:
        return event.kind == KIND_PAUSE or bool(self.options[CONF_CUSTOM_GCODE_ALERTS])

    # ----- user actions ------------------------------------------------------------------

    def set_alert(self, index: int, enabled: bool) -> None:
        if self.session and index < len(self.session.alerts):
            self.session.alerts[index] = enabled
            self._save()
            self._notify_listeners()

    def set_recipient(self, entry_id: str, enabled: bool) -> None:
        if enabled:
            self.recipients.add(entry_id)
        else:
            self.recipients.discard(entry_id)
        self._save()
        self._notify_listeners()

    def _default_recipients(self) -> set[str]:
        return set(self.options[CONF_DEFAULT_RECIPIENTS])

    def _restore_recipients(self) -> None:
        """Pick up the saved recipients, and apply any change to the defaults made since.

        Adding a phone to the defaults switches it on right away, and removing one switches it
        off, also in the middle of a print. Other phones keep their per-print setting.
        """
        defaults = self._default_recipients()
        self._applied_defaults = defaults
        stored = self._stored
        if stored.get("recipients") is None:
            self.recipients = set(defaults)
            return
        recipients = set(stored["recipients"])
        previous_defaults = set(stored.get("defaults", []))
        recipients |= defaults - previous_defaults
        recipients -= previous_defaults - defaults
        self.recipients = recipients
        if defaults != previous_defaults:
            self._save()

    # ----- notifications -----------------------------------------------------------------

    def _tag(self, index: int) -> str:
        return f"{DOMAIN}_{self.serial}_{index + 1}"

    def _check_notifications(self, session: PrintSession, before: list[str]) -> None:
        options = self.options
        lead_time = float(options[CONF_LEAD_TIME])
        for index, (event, state) in enumerate(zip(session.events, session.states)):
            previous = before[index]
            if previous == STATUS_ACTIVE and state.status != STATUS_ACTIVE and session.notified_event[index]:
                # Resumed: the "waiting for you" notification is no longer true.
                self._send(clear_notification(self._tag(index)))
            if not session.alerts[index]:
                continue
            if (
                lead_time > 0
                and not session.notified_lead[index]
                and state.status == STATUS_UPCOMING
                and state.minutes_until is not None
                and state.minutes_until <= lead_time
            ):
                session.notified_lead[index] = True
                self._save()
                self._send(lead_notification(self.model, self._tag(index), event, state, session.parsed))
            happened = (event.kind == KIND_PAUSE and state.status == STATUS_ACTIVE) or (
                event.kind != KIND_PAUSE and state.status == STATUS_DONE
            )
            if happened and previous != state.status and not session.notified_event[index]:
                session.notified_event[index] = True
                self._save()
                if options[CONF_ALERT_ON_EVENT]:
                    self._send(event_notification(self.model, self._tag(index), event, session.parsed))

    def owner(self, target: NotifyTarget) -> str:
        return owner_name(self.hass, target, self._user_names)

    def recipient_people(self) -> list[str]:
        """Everyone with at least one device switched on, each name once, in alphabetical order."""
        return sorted({self.owner(target) for target in self.recipient_targets()}, key=str.casefold)

    def recipient_targets(self) -> list[NotifyTarget]:
        targets = notify_targets(self.hass)
        return [targets[entry_id] for entry_id in sorted(self.recipients) if entry_id in targets]

    def _send(self, notification: Notification) -> None:
        targets = self.recipient_targets()
        if not targets:
            return
        self.entry.async_create_background_task(
            self.hass, async_send(self.hass, targets, notification), f"{DOMAIN} notify"
        )

    async def async_send_test(self) -> int:
        """Send a test notification to everyone currently opted in. Returns the number sent."""
        notification = Notification(
            title=f"{self.model} timeline",
            message="Test notification. Alerts for this printer will look like this.",
            tag=f"{DOMAIN}_{self.serial}_test",
        )
        return await async_send(self.hass, self.recipient_targets(), notification)

    # ----- calibration log and storage ---------------------------------------------------

    def _record_calibration(
        self, session: PrintSession, before: list[str], before_raw: list[float | None], now: datetime
    ) -> None:
        """Note what was predicted 5 and 1 minutes ahead of each event, and when it really happened.

        Predictions are the raw estimate, without the safety margin, so they show the estimator's
        own accuracy. Only changes seen live are recorded.
        """
        changed = False
        for index, state in enumerate(session.states):
            record = session.calibration[index]
            raw, previous_raw = state.raw_minutes_until, before_raw[index]
            if raw is not None and previous_raw is not None:
                for name, threshold in (("predicted_5_min_ahead", 5.0), ("predicted_1_min_ahead", 1.0)):
                    if name not in record and previous_raw > threshold >= raw:
                        record[name] = (now + timedelta(minutes=raw)).isoformat(timespec="seconds")
                        changed = True
            if before[index] == STATUS_UPCOMING and state.status != STATUS_UPCOMING and "reached" not in record:
                # "due" means the printer reached the event's layer but hasn't paused yet.
                record["reached"] = now.isoformat(timespec="seconds")
                changed = True
            if state.status == STATUS_ACTIVE and "paused" not in record:
                record["paused"] = now.isoformat(timespec="seconds")
                changed = True
        if changed:
            self._save()

    def _needs_fast_refresh(self) -> bool:
        """Seconds are shown in the last minute, so refresh more often then."""
        session = self.session
        if session is None or not self.options[CONF_SHOW_SECONDS]:
            return False
        return any(
            state.status == STATUS_UPCOMING and state.minutes_until is not None and state.minutes_until < 1.5
            for state in session.states
        )

    def _set_fast_refresh(self, enabled: bool) -> None:
        if enabled and self._fast_refresh_unsub is None and not self._stopped:
            self._fast_refresh_unsub = async_track_time_interval(
                self.hass, self._on_fast_tick, timedelta(seconds=FAST_UPDATE_INTERVAL_S)
            )
        elif not enabled and self._fast_refresh_unsub is not None:
            self._fast_refresh_unsub()
            self._fast_refresh_unsub = None

    @callback
    def _on_fast_tick(self, _now: datetime) -> None:
        self._evaluate(printer_changed=False)

    def calibration_report(self, session: PrintSession) -> list[dict[str, Any]]:
        """Each event's predictions next to what happened, with the errors in seconds (positive = early)."""
        report = []
        for event, record in zip(session.events, session.calibration):
            actual = record.get("paused") if event.kind == KIND_PAUSE else record.get("reached")
            entry: dict[str, Any] = {"event": event.description, "layer": event.layer, **record}
            for name in ("predicted_5_min_ahead", "predicted_1_min_ahead"):
                if actual and record.get(name):
                    predicted = dt_util.parse_datetime(record[name])
                    happened = dt_util.parse_datetime(actual)
                    if predicted and happened:
                        entry[name.replace("predicted", "error_seconds")] = round(
                            (happened - predicted).total_seconds()
                        )
            report.append(entry)
        return report

    def print_summary(self, session: PrintSession, ended: datetime | None = None) -> dict[str, Any]:
        parsed = session.parsed
        return {
            "key": session.key,
            "started": session.started.isoformat(),
            "ended": ended.isoformat() if ended else None,
            "file": session.file,
            "slicer": parsed.slicer if parsed else None,
            "slicer_total_minutes": parsed.total_min if parsed else None,
            "total_layers": parsed.total_layers if parsed else None,
            "rate_factor": session.estimator.k if session.estimator else None,
            "pace": session.estimator.pace if session.estimator else None,
            "events": [
                {
                    **event_attributes(event, state, parsed),
                    "alert": alert,
                    "remaining_at_event": event.remaining_min,
                }
                for event, state, alert in zip(session.events, session.states, session.alerts)
            ],
            "filament_changes": [
                {"layer": c.layer, "filament": c.filament, "slicer_budget_min": c.budget_min, "is_event": c.event is not None}
                for c in (parsed.filament_changes if parsed else [])
                if c.layer > 0
            ],
            "calibration": self.calibration_report(session),
            "log": list(session.samples),
        }

    def _sample(self, session: PrintSession, snap: PrinterSnapshot, now: datetime) -> None:
        if session.last_sample and (now - session.last_sample).total_seconds() < CALIBRATION_SAMPLE_INTERVAL_S:
            return
        session.last_sample = now
        estimator = session.estimator
        session.samples.append(
            {
                "time": now.isoformat(timespec="seconds"),
                "status": snap.status,
                "printer_remaining": snap.remaining_min,
                "printer_remaining_precise": (
                    round(snap.remaining_precise, 2) if snap.remaining_precise is not None else None
                ),
                "progress": snap.progress_pct,
                "layer": snap.layer,
                "speed": snap.speed,
                "external_spool": snap.on_external_spool,
                "slicer_remaining": estimator.slicer_remaining(snap) if estimator else None,
                "k": round(estimator.k, 4) if estimator else None,
                "pace": round(estimator.pace, 4) if estimator else None,
                "next_minutes": next(
                    (s.minutes_until for s in session.states if s.minutes_until is not None), None
                ),
            }
        )
        del session.samples[:-CALIBRATION_MAX_SAMPLES]

    def _storage_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "recipients": sorted(self.recipients),
            "defaults": sorted(self._applied_defaults),
            "last_print": self.last_print,
        }
        session = self.session
        if session is None or session.parsed is None:
            return data
        return {
            **data,
            "key": session.key,
            "started": session.started.isoformat(),
            "file": session.file,
            "k": session.estimator.k if session.estimator else 1.0,
            "events": [
                {
                    "alert": session.alerts[index],
                    "notified_lead": session.notified_lead[index],
                    "notified_event": session.notified_event[index],
                    "status": state.status,
                    "status_since": state.status_since.isoformat() if state.status_since else None,
                    "calibration": session.calibration[index],
                }
                for index, state in enumerate(session.states)
            ],
        }

    def _save(self) -> None:
        data = self._storage_data()
        if "key" in data:
            self._stored = data
        self._store.async_delay_save(self._storage_data, 5)


def _file_size(path: Path) -> int:
    return path.stat().st_size
