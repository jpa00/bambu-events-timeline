# Bambu Events Timeline

A Home Assistant integration that tells you how long it is until your Bambu Lab printer next needs you.

If you add a pause or some custom G-code at a layer in the slicer, the printer will stop or do something at that point in the print. Neither the printer nor the Bambu Lab integration tells you when that will be. This integration reads the sliced G-code of the current print, finds those events, keeps a running countdown to each one, and can send a heads-up to your phone a few minutes before.

> **Status: early.** It works on my prints, but the countdown hasn't been tuned against much real data yet.
>
> **The countdown is an estimate, and can't be exact.** Expect it to be about right close to an event and rougher further out. It can also be caught out, for example by an event that comes right after a filament change. [Why it can't be exact](#how-accurate-is-it) explains the reasons before you rely on it.

## Before you use this

I built this heavily AI-assisted with Claude Opus 5.5 for my own setup: a Bambu Lab A1 with an AMS lite, sliced in OrcaSlicer, connected to Home Assistant through [ha-bambulab](https://github.com/greghesp/ha-bambulab). It's public so it's easy to install with HACS, and in case it's useful to someone else as-is.

That also means:

- It's provided as is, with no warranty or promise of support (see [LICENSE](LICENSE)).
- I'm unlikely to add features I don't need myself. Bug reports with clear steps to reproduce are welcome. Feature requests may just sit there.
- Other printers and Bambu Studio will probably work, since they write the same markers into the G-code. I just haven't tested them.

## What it detects

| In the slicer | Counted as | Alert by default |
|---|---|---|
| Add pause (layer slider) | Pause | On |
| Add custom G-code (layer slider) | Custom G-code. If the G-code contains a pause command (`M400 U1`, `M600`, `M601`), it counts as a pause instead. | Off |
| Filament changes, AMS swaps | Ignored, unless the change includes a pause (see below) | – |
| Filament change with a pause in the printer's change-filament G-code | One "Filament change" event | On |

Plain filament changes are ignored on purpose. With an AMS, the slicer turns every filament change into an automatic swap, and the G-code doesn't say whether a swap actually needs a person. Without an AMS, the usual way to change filament by hand is to add a pause (`M400 U1`) to the printer's change-filament G-code in the slicer's machine settings. The integration spots that pause inside the change and shows the change as a single "Filament change" event.

## What you need

- Home Assistant 2026.1 or newer.
- [ha-bambulab](https://github.com/greghesp/ha-bambulab) set up for your printer, **with the printer's IP address and access code filled in**. That's what lets ha-bambulab download the print file from the printer. This integration reads the G-code from ha-bambulab's download folder (`/config/www/media/ha-bambulab/<serial>/prints/`) and never talks to the printer itself.
- An SD card in the printer. Without one, there's no file for ha-bambulab to download.

## Installing

1. In HACS, open the menu (⋮) → **Custom repositories**. Add `https://github.com/jpa00/bambu-events-timeline` with the type **Integration**.
2. Find **Bambu Events Timeline** in HACS, download it, and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration**, search for **Bambu Events Timeline**, and pick your printer.

You'll get a new device called "A1 Timeline" (or whatever your printer model is).

## What it gives you

| Entity | What it is |
|---|---|
| `sensor.a1_timeline_next_event` | When the next event happens. The dashboard shows it as "in 23 minutes". The attributes say what it is (`label`), how long until it (`countdown`, `minutes_until`), and its `status`. |
| `switch.a1_timeline_event_1` … `_10` | One per event in the current print, in order. The switch turns that event's alerts on or off. Its attributes are the same as above. Unused slots, and finished events, are unavailable. |
| `sensor.a1_timeline_g_code_status` | Whether the G-code for the current print was found and read. Check this first if the list stays empty. |
| `switch.a1_timeline_notify_<phone>` | One per phone or tablet with the Home Assistant app. On means that phone gets alerts for this print. |
| `sensor.a1_timeline_alert_recipients` | Who gets alerts right now, by name: "Alex", "Alex, Sam" or "no one". Each person is listed once, however many of their devices are on. |
| `button.a1_timeline_send_test_notification` | Sends a test notification to the phones that are switched on. |

An event's `status` goes from `upcoming` to `due` (it's at that point in the print, so the printer should stop any second) to `active` (the printer is paused at it) to `done`. Custom G-code that doesn't pause goes straight from `upcoming` to `done`.

## Notifications

For each event with its alert on, the phones that are switched on get:

1. **A heads-up** a few minutes before the event (5 by default), e.g. *"A1: pause in 5 min — Pause · layer 5/24 (about 14:35)."*
2. **A second alert when it happens**, e.g. *"A1 paused — Planned pause at layer 5/24. Waiting for you."* This replaces the heads-up on the phone, and it's removed again once the print resumes. You can turn this second alert off in the options.

Who gets alerts is decided **per print**. In the options you choose which phones get them by default. During a print, anyone can switch their phone on or off on the dashboard. When the print ends, everything goes back to the defaults. Switching a phone on between prints applies to the next print.

Names come from the Home Assistant user who set up the Companion app on each device (their person's name if they have one).

Notifications go through the Home Assistant Companion app (`notify.mobile_app_…`), so every phone needs the app logged in to your Home Assistant. On Android they arrive in their own "Printer events" notification channel, which you can adjust in the phone's settings. On iOS they're marked time-sensitive, so they get through Focus modes.

A phone that gets the Companion app after this integration was set up shows up once you reload the integration (**Settings → Devices & services → Bambu Events Timeline → ⋮ → Reload**).

## Dashboard

[`dashboard/timeline-cards.yaml`](dashboard/timeline-cards.yaml) is a ready-made card. It lists the print's upcoming events with a countdown for each, and marks the next one with a small clock badge. Tapping a row turns that event's alert on or off. When there's nothing to list, a status line says why (still looking for the G-code, no pauses in this print, and so on). Below that is a collapsed "Sending alerts to Alex" row; open it to switch devices on or off for this print or to send a test. It needs the [Mushroom](https://github.com/piitaya/lovelace-mushroom), [auto-entities](https://github.com/thomasloven/lovelace-auto-entities) and [collapsable-cards](https://github.com/RossMcMillan92/lovelace-collapsable-cards) cards from HACS. Open your dashboard in edit mode, add a **Manual** card, and paste the file's contents. The card writes out three entity IDs (`sensor.a1_timeline_g_code_status`, `sensor.a1_timeline_next_event` and `sensor.a1_timeline_alert_recipients`). If Home Assistant named yours differently, for example with the area in front (`sensor.workshop_a1_timeline_next_event`), change them in the YAML or rename the entities. The entity IDs in the tables above have the same caveat. The event list only shows while something is printing.

## How the countdown works

The slicer writes its own time estimate into the G-code every minute or so. From that, the integration knows how many slicer-minutes before the end of the print each event is. While printing, it follows the printer's remaining time, and measures how fast the print is really going compared to that: real minutes per printer-minute, over the last 20 minutes of printing. The start sequence and the first layer are left out of that, as they run at their own speed (on one of my prints the first layer took 12 % longer than estimated, and the rest ran on time). In practice:

- **Pauses** (planned or not) stop the countdown. It continues when the print resumes, and paused time doesn't count towards the measured pace.
- **A print running slower or faster than the slicer expected** is corrected for once there are 10 minutes of data from the second layer on. Until then, the countdown follows the slicer's estimate as is.
- **Speed changes** (silent, sport, ludicrous) are picked up by the same measurement over the following minutes. I haven't tested this on a real print yet.
- **It runs early on purpose.** Countdowns are shortened by a safety margin of half a minute, and they round down to whole minutes, so they reach zero up to a minute and a half before the event. The margin is adjustable in the options.

## How accurate is it?

Close to an event, usually within a minute, and on the early side. Further out, expect it to be a few minutes off. That's about as good as it gets with the information available, for these reasons:

- **There is no real timeline, only the slicer's estimate.** The printer's own "remaining time" is just the slicer's estimate passed through, in whole minutes. It doesn't learn from how the print is actually going.
- **Real prints drift from the estimate.** On a long print of mine, printing ran about 3 % slower than the slicer thought. Over a 3-hour wait, that's 5 minutes. The integration measures the actual pace and corrects for it, but it needs 10 minutes of printing after the first layer, and the pace changes through a print as the layers change. In replays of two real prints, the corrected countdown was at most about a minute late, and usually a little early.
- **Filament changes can jump the clock.** The slicer budgets a few minutes for each filament change. If a change is quicker than that, for example a different filament profile on the same spool, the printer's remaining time drops by several minutes at once. An event after such a change then comes **earlier** than the countdown said, by up to the time the slicer budgeted. To avoid that, while the printer is feeding from the external spool, the integration assumes a filament change without a pause is only a profile change, which takes no time, and leaves its budget out of the countdown. (The printer can't swap filament on the external spool by itself, so a real change there needs a pause, and then it shows up as an event.) With an AMS, it assumes a swap takes as long as the slicer budgeted. The slicer's budgets are only known to about a minute, and the countdown corrects itself as soon as a jump happens.
- **Whole minutes.** The printer reports remaining time rounded down to whole minutes, so anything finer is an informed estimate. That's what the "~" in "in ~45 s" means.
- **Things nobody can know in advance:** how long you take at a pause, objects skipped mid-print, a speed change, a filament runout.

The countdown keeps correcting itself as the print goes on, so it gets more accurate as the event gets closer. The heads-up notification goes out a few minutes early on purpose, to leave room for all of the above.

**Please don't open issues about it being a minute or two off.** That's expected. If it's consistently off by more, or late rather than early, an issue with the diagnostics attached (see below) is welcome.

The times it finds should match what OrcaSlicer shows when you hover an event's icon on the preview's layer slider, to within a few seconds. Without hovering, Orca shows the layer's *end* time instead, which can be quite a bit later.

## Settings

**Settings → Devices & services → Bambu Events Timeline → Configure:**

- **Notify by default.** The phones that get alerts unless switched off for a print.
- **Advance warning.** How many minutes before an event the heads-up goes out. 0 turns heads-ups off.
- **Also alert when the event happens.** On by default.
- **Alerts for custom G-code on by default.** Pauses always start with alerts on.
- **Keep finished events on the list.** Off by default. Finished events are shown with a check, and their alerts can't be changed any more.
- **Show seconds in the last minute.** Off by default. In an event's final minute the countdown reads "in ~45 s". The printer only reports whole minutes, so the seconds are an informed estimate, worked out from when the printer's minute last ticked down.
- **Safety margin (minutes).** 0.5 by default.
- **Extra safety margin for far-off events (percent).** Off by default. A percentage of the time left until each event, used instead of the minute margin when it's larger. Useful if your countdowns tend to run late on long prints.
- **Maximum number of events.** 10 by default.

## When something doesn't work

- **"File not found":** ha-bambulab didn't download the print file. Check that it has the printer's IP address and access code, and that the printer has an SD card. Look in `/config/www/media/ha-bambulab/<serial>/prints/` for a `.gcode` file with your print's name.
- **No notifications:** press **Send test notification**. If that doesn't arrive either, check that the phone's switch is on and that `notify.mobile_app_<your phone>` exists under **Developer tools → Actions**.
- **The countdown is off:** download the diagnostics (**Settings → Devices & services → Bambu Events Timeline → ⋮ → Download diagnostics**) during a print or after it, until the next print finishes. They contain a minute-by-minute log of the printer's numbers next to the slicer's, and a calibration report for each event: what was predicted 5 and 1 minutes ahead, when it actually happened, and the difference in seconds (positive means the prediction was early). Attach them to a bug report.
- **Updates don't show up:** Home Assistant's update list works from GitHub releases. A release is published automatically whenever the version number changes, so HACS may show a newer commit before Home Assistant lists it as an update.
- **Check a G-code file without Home Assistant:**

  ```
  python custom_components/bambu_timeline/gcode_parser.py my_print.gcode
  ```

  This prints what the integration will find:

  ```
  Slicer: OrcaSlicer 2.4.2
  Estimated total: 3:48:43, layers: 24
    1:10:13  layer    5  Pause  (line 109797)
    1:57:13  layer   10  Pause  (line 244045)
    2:26:13  layer   14  Message: Hello Claude  (line 331214)
  ```

## Development

The parser, estimator and file finder are plain Python and test without Home Assistant:

```
python -m venv .venv
.venv/Scripts/python -m pip install pytest      # on Linux/macOS: .venv/bin/python
.venv/Scripts/python -m pytest
```

The integration tests need Home Assistant's test harness, which needs Python 3.14 and Linux (WSL works):

```
pip install pytest-homeassistant-custom-component
pytest
```

The test fixtures in `tests/fixtures/` are real OrcaSlicer output, trimmed down with `scripts/trim_gcode.py` so that only the lines the parser looks at are left.
