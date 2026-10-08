"""End-to-end tests against Home Assistant's test harness, with a fake ha-bambulab printer."""

import shutil
from pathlib import Path

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.config_entries import SOURCE_USER  # noqa: E402
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.bambu_timeline import coordinator as coordinator_module  # noqa: E402
from custom_components.bambu_timeline.const import (  # noqa: E402
    BAMBU_KEYS,
    CONF_BAMBU_ENTRY_ID,
    CONF_MODEL,
    CONF_SERIAL,
    CONF_SHOW_FINISHED,
    DOMAIN,
)
from custom_components.bambu_timeline.diagnostics import async_get_config_entry_diagnostics  # noqa: E402

from .conftest import FIXTURES  # noqa: E402

SERIAL = "00M00A000000000"  # Made up; real serials are personal data.
PRINT_NAME = "Test_two_pauses_and_message"
EVENT_1 = "switch.a1_timeline_event_1"
EVENT_3 = "switch.a1_timeline_event_3"
EVENT_4 = "switch.a1_timeline_event_4"
NEXT_EVENT = "sensor.a1_timeline_next_event"
GCODE_STATUS = "sensor.a1_timeline_g_code_status"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def fast_file_search(monkeypatch):
    monkeypatch.setattr(coordinator_module, "FILE_SEARCH_INTERVAL_S", 0)


@pytest.fixture(autouse=True)
def clean_cache(hass: HomeAssistant):
    """The harness reuses one config folder for all tests, so remove cached prints around each test."""
    folder = hass.config.path("www/media/ha-bambulab")
    shutil.rmtree(folder, ignore_errors=True)
    yield
    shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
def bambu_entry(hass: HomeAssistant) -> MockConfigEntry:
    """A fake ha-bambulab printer: a config entry plus the sensors this integration reads."""
    entry = MockConfigEntry(domain="bambu_lab", data={"serial": SERIAL, "device_type": "A1"}, title=SERIAL)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for key in BAMBU_KEYS:
        registry.async_get_or_create(
            "sensor", "bambu_lab", f"{SERIAL}_{key}", suggested_object_id=f"a1_{key}", config_entry=entry
        )
    printer(hass, "idle")
    return entry


def printer(hass: HomeAssistant, status: str, layer: int = 0, remaining: float = 0, progress: int = 0) -> None:
    hass.states.async_set("sensor.a1_print_status", status)
    hass.states.async_set("sensor.a1_current_layer", str(layer))
    hass.states.async_set("sensor.a1_total_layers", "24")
    hass.states.async_set("sensor.a1_remaining_time", str(remaining), {"unit_of_measurement": "min"})
    hass.states.async_set("sensor.a1_print_progress", str(progress), {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.a1_speed_profile", "standard")
    hass.states.async_set("sensor.a1_subtask_name", PRINT_NAME if status != "idle" else "")
    hass.states.async_set("sensor.a1_gcode_file", f"/cache/{PRINT_NAME}.gcode.3mf" if status != "idle" else "")


def cache_gcode(hass: HomeAssistant) -> None:
    folder = Path(hass.config.path("www/media/ha-bambulab", SERIAL, "prints", "cache"))
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "orca_a1_pauses_and_message.gcode", folder / f"6556-{PRINT_NAME}.gcode.gcode")


async def setup_timeline(hass: HomeAssistant, bambu_entry: MockConfigEntry, **options) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_BAMBU_ENTRY_ID: bambu_entry.entry_id, CONF_SERIAL: SERIAL, CONF_MODEL: "A1"},
        options=options,
        unique_id=SERIAL,
        title="A1 timeline",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_config_flow_creates_entry(hass: HomeAssistant, bambu_entry):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BAMBU_ENTRY_ID: bambu_entry.entry_id}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "A1 timeline"
    assert result["data"] == {CONF_BAMBU_ENTRY_ID: bambu_entry.entry_id, CONF_SERIAL: SERIAL, CONF_MODEL: "A1"}


async def test_config_flow_without_printer(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_printers"


async def test_print_lifecycle(hass: HomeAssistant, bambu_entry):
    await setup_timeline(hass, bambu_entry)
    assert hass.states.get(EVENT_1).state == STATE_UNAVAILABLE
    assert hass.states.get(GCODE_STATUS).state == "idle"

    cache_gcode(hass)
    printer(hass, "running", layer=1, remaining=215, progress=6)
    await settle(hass)

    assert hass.states.get(GCODE_STATUS).state == "ok"
    first = hass.states.get(EVENT_1)
    assert first.state == STATE_ON
    assert first.attributes["label"] == "Pause · layer 5/24"
    assert first.attributes["status"] == "upcoming"
    assert 50 <= first.attributes["minutes_until"] <= 57
    assert first.attributes["countdown"].startswith(f"in {first.attributes['minutes_until']} min · ")
    custom = hass.states.get(EVENT_3)
    assert custom.state == STATE_OFF
    assert custom.attributes["label"] == "Message: Hello Claude · layer 14/24"
    assert hass.states.get(EVENT_4).state == STATE_UNAVAILABLE
    assert hass.states.get(NEXT_EVENT).attributes["slot"] == 1

    # The printer reaches the first pause.
    printer(hass, "pause", layer=5, remaining=158, progress=30)
    await settle(hass)
    first = hass.states.get(EVENT_1)
    assert first.attributes["status"] == "active"
    assert first.attributes["countdown"] == "Paused, waiting for you"

    # Resumed: the first pause is done and drops off the list; the next one is up.
    printer(hass, "running", layer=5, remaining=157, progress=31)
    await settle(hass)
    assert hass.states.get(EVENT_1).state == STATE_UNAVAILABLE
    assert hass.states.get(NEXT_EVENT).attributes["slot"] == 2

    printer(hass, "finish", layer=24, remaining=0, progress=100)
    await settle(hass)
    assert hass.states.get(EVENT_1).state == STATE_UNAVAILABLE
    assert hass.states.get(GCODE_STATUS).state == "idle"


async def test_alert_toggle_survives_reload(hass: HomeAssistant, bambu_entry):
    entry = await setup_timeline(hass, bambu_entry)
    cache_gcode(hass)
    printer(hass, "running", layer=1, remaining=215, progress=6)
    await settle(hass)

    await hass.services.async_call("switch", "turn_off", {"entity_id": EVENT_1}, blocking=True)
    await hass.services.async_call("switch", "turn_on", {"entity_id": EVENT_3}, blocking=True)
    assert hass.states.get(EVENT_1).state == STATE_OFF

    assert await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass)
    assert hass.states.get(EVENT_1).state == STATE_OFF
    assert hass.states.get(EVENT_3).state == STATE_ON


async def test_show_finished_option(hass: HomeAssistant, bambu_entry):
    await setup_timeline(hass, bambu_entry, **{CONF_SHOW_FINISHED: True})
    cache_gcode(hass)
    printer(hass, "running", layer=6, remaining=145, progress=37)
    await settle(hass)
    first = hass.states.get(EVENT_1)
    assert first.state == STATE_ON
    assert first.attributes["status"] == "done"
    assert first.attributes["countdown"].startswith("Done")


async def test_missing_file_is_reported(hass: HomeAssistant, bambu_entry, monkeypatch):
    monkeypatch.setattr(coordinator_module, "FILE_SEARCH_TIMEOUT_S", 0)
    await setup_timeline(hass, bambu_entry)
    printer(hass, "running", layer=1, remaining=215, progress=6)
    await settle(hass)
    assert hass.states.get(GCODE_STATUS).state == "file_not_found"
    assert hass.states.get(EVENT_1).state == STATE_UNAVAILABLE


async def test_diagnostics(hass: HomeAssistant, bambu_entry):
    entry = await setup_timeline(hass, bambu_entry)
    cache_gcode(hass)
    printer(hass, "running", layer=1, remaining=215, progress=6)
    await settle(hass)
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["gcode_status"] == "ok"
    assert len(data["print"]["events"]) == 3
    assert data["print"]["log"][0]["printer_remaining"] == 215


async def test_options_flow(hass: HomeAssistant, bambu_entry):
    entry = await setup_timeline(hass, bambu_entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "custom_gcode_alerts": True,
            "show_finished": True,
            "early_margin_minutes": 2,
            "early_margin_percent": 5,
            "event_slots": 3,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await settle(hass)
    assert entry.options["event_slots"] == 3
    assert hass.states.get("switch.a1_timeline_event_3") is not None
    registry = er.async_get(hass)
    assert registry.async_get(EVENT_4) is None


# ----- notifications ---------------------------------------------------------------------

PHONE = "switch.a1_timeline_notify_alex_phone"
TABLET = "switch.a1_timeline_notify_family_tablet"


def printer_at(hass: HomeAssistant, slicer_left: float, status: str = "running") -> None:
    """Report the printer at a given point of the fixture print, running exactly as planned."""
    from custom_components.bambu_timeline.gcode_parser import parse_gcode_file

    parsed = parse_gcode_file(FIXTURES / "orca_a1_pauses_and_message.gcode")
    progress = max(p for p, r in parsed.progress if r >= int(slicer_left))
    layer = max((n for n, start in parsed.layer_start_remaining.items() if start >= slicer_left), default=1)
    printer(hass, status, layer=layer, remaining=int(slicer_left), progress=progress)


@pytest.fixture
def phones(hass: HomeAssistant):
    """Two Companion-app devices, with their notify services recorded instead of sent."""
    from pytest_homeassistant_custom_component.common import async_mock_service

    phone = MockConfigEntry(domain="mobile_app", data={"device_name": "Alex Phone"}, title="Alex Phone")
    tablet = MockConfigEntry(domain="mobile_app", data={"device_name": "Family Tablet"}, title="Family Tablet")
    phone.add_to_hass(hass)
    tablet.add_to_hass(hass)
    return {
        "phone": phone,
        "phone_calls": async_mock_service(hass, "notify", "mobile_app_alex_phone"),
        "tablet_calls": async_mock_service(hass, "notify", "mobile_app_family_tablet"),
    }


async def test_notifications_follow_the_print(hass: HomeAssistant, bambu_entry, phones):
    await setup_timeline(hass, bambu_entry, default_recipients=[phones["phone"].entry_id])
    phone_calls, tablet_calls = phones["phone_calls"], phones["tablet_calls"]
    assert hass.states.get(PHONE).state == STATE_ON
    assert hass.states.get(TABLET).state == STATE_OFF

    cache_gcode(hass)
    printer_at(hass, 200)
    await settle(hass)
    printer_at(hass, 170)
    await settle(hass)
    assert phone_calls == []

    # Inside the 5-minute window before the first pause: one heads-up, to the phone only.
    printer_at(hass, 162)
    await settle(hass)
    printer_at(hass, 161)
    await settle(hass)
    assert len(phone_calls) == 1
    assert tablet_calls == []
    heads_up = phone_calls[0].data
    assert heads_up["title"].startswith("A1: pause in ")
    assert heads_up["message"].startswith("Pause · layer 5/24 (about ")
    tag = heads_up["data"]["tag"]

    # The printer pauses: the "it happened" alert replaces the heads-up.
    printer_at(hass, 158.4, status="pause")
    await settle(hass)
    assert len(phone_calls) == 2
    assert phone_calls[1].data["title"] == "A1 paused"
    assert phone_calls[1].data["data"]["tag"] == tag

    # Resumed: the "waiting for you" notification is cleared.
    printer_at(hass, 158)
    await settle(hass)
    assert phone_calls[2].data == {"message": "clear_notification", "data": {"tag": tag}}

    # Someone else opts in for this print.
    await hass.services.async_call("switch", "turn_on", {"entity_id": TABLET}, blocking=True)
    printer_at(hass, 115)
    await settle(hass)
    assert len(tablet_calls) == 1
    assert tablet_calls[0].data["title"].startswith("A1: pause in ")
    # Printing exactly as planned, so the learned rate factor must stay at about 1.
    assert hass.states.get(GCODE_STATUS).attributes["rate_factor"] == pytest.approx(1.0, abs=0.03)

    # The print ends: back to the defaults for the next print.
    printer(hass, "finish", layer=24, remaining=0, progress=100)
    await settle(hass)
    assert hass.states.get(TABLET).state == STATE_OFF
    assert hass.states.get(PHONE).state == STATE_ON


async def test_alert_switch_controls_notifications(hass: HomeAssistant, bambu_entry, phones):
    await setup_timeline(hass, bambu_entry, default_recipients=[phones["phone"].entry_id])
    cache_gcode(hass)
    printer_at(hass, 200)
    await settle(hass)
    await hass.services.async_call("switch", "turn_off", {"entity_id": EVENT_1}, blocking=True)
    await hass.services.async_call("switch", "turn_on", {"entity_id": EVENT_3}, blocking=True)

    printer_at(hass, 162)
    await settle(hass)
    printer_at(hass, 158.4, status="pause")
    await settle(hass)
    assert phones["phone_calls"] == []

    # Custom G-code with its alert turned on: heads-up, then a note when the layer is reached.
    printer_at(hass, 86)
    await settle(hass)
    printer_at(hass, 82.4)
    await settle(hass)
    titles = [call.data["title"] for call in phones["phone_calls"]]
    # The second pause was skipped over entirely by this jump, so it never got a heads-up.
    assert len(titles) == 2
    assert titles[0].startswith("A1: Message: Hello Claude in ")
    assert titles[1] == "A1 reached layer 14/24"


async def test_no_duplicate_notifications_after_restart(hass: HomeAssistant, bambu_entry, phones):
    entry = await setup_timeline(hass, bambu_entry, default_recipients=[phones["phone"].entry_id])
    cache_gcode(hass)
    printer_at(hass, 200)
    await settle(hass)
    printer_at(hass, 162)
    await settle(hass)
    assert len(phones["phone_calls"]) == 1

    assert await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass)
    printer_at(hass, 161)
    await settle(hass)
    assert len(phones["phone_calls"]) == 1


async def test_no_notifications_for_events_already_past(hass: HomeAssistant, bambu_entry, phones):
    await setup_timeline(hass, bambu_entry, default_recipients=[phones["phone"].entry_id])
    cache_gcode(hass)
    # Integration installed mid-print, past the first pause and inside the second one's window.
    printer_at(hass, 115)
    await settle(hass)
    printer_at(hass, 114)
    await settle(hass)
    titles = [call.data["title"] for call in phones["phone_calls"]]
    assert len(titles) == 1 and titles[0].startswith("A1: pause in ")


async def test_test_notification_button(hass: HomeAssistant, bambu_entry, phones):
    from homeassistant.exceptions import HomeAssistantError

    await setup_timeline(hass, bambu_entry)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.a1_timeline_send_test_notification"}, blocking=True
        )
    await hass.services.async_call("switch", "turn_on", {"entity_id": PHONE}, blocking=True)
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.a1_timeline_send_test_notification"}, blocking=True
    )
    assert phones["phone_calls"][0].data["title"] == "A1 timeline"


async def test_name_sensor_blip_keeps_the_print(hass: HomeAssistant, bambu_entry):
    await setup_timeline(hass, bambu_entry)
    cache_gcode(hass)
    printer(hass, "running", layer=1, remaining=215, progress=6)
    await settle(hass)
    await hass.services.async_call("switch", "turn_off", {"entity_id": EVENT_1}, blocking=True)

    hass.states.async_set("sensor.a1_subtask_name", "unavailable")
    await settle(hass)
    hass.states.async_set("sensor.a1_subtask_name", PRINT_NAME)
    await settle(hass)
    assert hass.states.get(EVENT_1).state == STATE_OFF


async def test_changing_defaults_applies_right_away(hass: HomeAssistant, bambu_entry, phones):
    """Picking a default phone while idle must switch it on, not only after the next print."""
    entry = await setup_timeline(hass, bambu_entry)
    assert hass.states.get(PHONE).state == STATE_OFF
    await hass.services.async_call("switch", "turn_on", {"entity_id": TABLET}, blocking=True)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {**entry.options, "default_recipients": [phones["phone"].entry_id]}
    )
    await settle(hass)
    assert hass.states.get(PHONE).state == STATE_ON
    assert hass.states.get(TABLET).state == STATE_ON  # Not a default, but switched on by hand: unchanged.

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {**entry.options, "default_recipients": []}
    )
    await settle(hass)
    assert hass.states.get(PHONE).state == STATE_OFF


async def test_alert_recipients_are_listed_by_person(hass: HomeAssistant, bambu_entry):
    from pytest_homeassistant_custom_component.common import async_mock_service

    sam = await hass.auth.async_create_user("Sam")
    hass.states.async_set("person.alex", "home", {"user_id": "alex-user", "friendly_name": "Alex"})
    devices = {}
    for name, user_id in (("Pixel 9", "alex-user"), ("Galaxy Tab", "alex-user"), ("Sam Phone", sam.id)):
        entry = MockConfigEntry(domain="mobile_app", data={"device_name": name, "user_id": user_id}, title=name)
        entry.add_to_hass(hass)
        async_mock_service(hass, "notify", f"mobile_app_{name.lower().replace(' ', '_')}")
        devices[name] = entry

    await setup_timeline(
        hass, bambu_entry, default_recipients=[devices["Pixel 9"].entry_id, devices["Galaxy Tab"].entry_id]
    )
    summary = hass.states.get("sensor.a1_timeline_alert_recipients")
    assert summary.state == "Alex"
    assert summary.attributes["device_count"] == 2

    sam_switch = hass.states.get("switch.a1_timeline_notify_sam_phone")
    assert sam_switch.attributes == {**sam_switch.attributes, "role": "recipient", "person": "Sam"}
    await hass.services.async_call("switch", "turn_on", {"entity_id": sam_switch.entity_id}, blocking=True)
    assert hass.states.get("sensor.a1_timeline_alert_recipients").state == "Alex, Sam"

    for entity_id in ("switch.a1_timeline_notify_pixel_9", "switch.a1_timeline_notify_galaxy_tab", sam_switch.entity_id):
        await hass.services.async_call("switch", "turn_off", {"entity_id": entity_id}, blocking=True)
    assert hass.states.get("sensor.a1_timeline_alert_recipients").state == "no one"


async def test_seconds_in_the_last_minute(hass: HomeAssistant, bambu_entry, freezer):
    import re
    from datetime import timedelta

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    await setup_timeline(hass, bambu_entry, show_seconds=True)
    cache_gcode(hass)
    printer_at(hass, 200)
    await settle(hass)
    printer_at(hass, 160)
    await settle(hass)
    printer_at(hass, 159)  # The printer's minute just ticked down: the clock within the minute starts.
    await settle(hass)

    freezer.tick(timedelta(seconds=20))
    async_fire_time_changed(hass)
    await settle(hass)
    first = hass.states.get(EVENT_1).attributes["countdown"]
    assert re.fullmatch(r"in ~\d+ s", first), first

    # The 5-second refresh keeps it moving without any printer update.
    freezer.tick(timedelta(seconds=15))
    async_fire_time_changed(hass)
    await settle(hass)
    second = hass.states.get(EVENT_1).attributes["countdown"]
    assert int(second[4:-2]) < int(first[4:-2]), (first, second)


async def test_calibration_record_kept_after_print(hass: HomeAssistant, bambu_entry):
    entry = await setup_timeline(hass, bambu_entry)
    cache_gcode(hass)
    for slicer_left in (200, 170, 163, 159):
        printer_at(hass, slicer_left)
        await settle(hass)
    printer_at(hass, 158.4, status="pause")
    await settle(hass)
    printer(hass, "finish", layer=24, remaining=0, progress=100)
    await settle(hass)

    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["print"] is None
    record = data["last_finished_print"]["calibration"][0]
    assert record["event"] == "Pause" and record["layer"] == 5
    assert {"predicted_5_min_ahead", "predicted_1_min_ahead", "reached", "paused"} <= record.keys()
    assert "error_seconds_1_min_ahead" in record
    assert data["last_finished_print"]["log"]

    # Kept across a restart, too.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass)
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["last_finished_print"]["calibration"][0]["layer"] == 5
