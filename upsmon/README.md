# upsmon

UPS monitoring for a UPS published over the network by a Synology NAS, or any
other host running Network UPS Tools.

* `upsmon.py` — the daemon and the command-line tool, one file, standard library only
* `web/index.php`, `web/upsmon-api.php` — the dashboard and its thin API client
* `deploy/` — systemd unit, sample configuration, nginx server block
* `INSTALL.md` — the full AlmaLinux 9 walkthrough

It also watches a Shelly smart plug and any sensor that pushes readings to it,
so the power actually being drawn is measured rather than estimated.

## How the pieces fit

```
Synology NAS                       AlmaLinux 9
┌──────────────┐                 ┌────────────────────────────────────────┐
│ UPS over USB │                 │ upsmon.service   (user "upsmon")       │
│      ↓       │    TCP 3493     │   polls every 10 s                     │
│ upsd (NUT)   │ ───────────────►│   writes /var/lib/upsmon/history.db    │
└──────────────┘                 │   serves 127.0.0.1:9848, token-guarded │
                                 │        ↑                               │
                                 │ php-fpm ← nginx :443 ← browser         │
                                 └────────────────────────────────────────┘
```

The daemon is the only thing that talks to the UPS. PHP relays JSON and holds no
credentials; the two endpoints that change something send a token read from a
file the browser never sees. Nothing in the chain needs root.

## Why it is built this way

**SQLite in three layers.** Full-resolution samples for a fortnight, hourly
averages for two years, and the complete variable set stored only when it
changes. A year of history costs single-digit megabytes, and a year-long chart
costs a few hundred rows rather than half a million. The roll-up runs inside the
daemon; nothing external needs scheduling.

**Faster sampling on battery.** A mains failure is the one event worth having in
detail, so the interval drops from 60 s to 10 s while the UPS is discharging.

**An outage means `OB`, not `DISCHRG`.** A self test drains the battery while
still reporting `OL` — on line. Treating any discharge as a power cut would file
every test as an outage and make the failure log useless.

**Charging state is not an event.** `CHRG` and `DISCHRG` flip constantly, several
times during a single test. They are visible in every chart; they do not each
deserve a log entry.

**Writes are read back on the driver's schedule.** `usbhid-ups` refreshes its
cache every few seconds, so a value read straight after a write is still the old
one. The daemon polls until the UPS confirms, then reports what was actually
accepted — including when the UPS clamped the value to something else.

**Tests are followed to their verdict.** Starting a self test from the dashboard
returns immediately, but the daemon keeps watching and records how long the test
ran, how long the load was genuinely on battery, and how far the battery voltage
sagged. That last figure says more about battery health than pass/fail does,
because a failing battery still passes a short test.

**Unknown variables are still explained.** NUT names are structured, so
`input.L2-N.voltage` and `ambient.1.temperature.high` get sensible descriptions
even though no table lists them. What one UPS reports is not what the next one
does; nothing here is specific to a model.

## The dashboard

Six tabs: **Overview** (status, charge, runtime, load, voltages, 24-hour chart),
**History** (six charts from 6 hours to a year, plus every power failure),
**Tests** (self-test verdicts with a battery-voltage trend across tests),
**Events**, **Control** (commands, writable settings, and clearing recorded
data — all PIN-protected), and **All variables**.

It refreshes every five seconds, pauses when the browser tab is hidden, and can
be paused by hand from the indicator in the corner. The tab icon is a battery
coloured by the verdict, so a UPS in trouble shows up without switching to it.

Command buttons carry a plain label and an icon rather than the raw NUT name;
resting the pointer on one for five seconds reveals the exact command and what
it does.

## Command line

Everything the web interface does is available without it:

```bash
upsmon --status                       # full report straight from the UPS
upsmon --list-rw                      # what this UPS lets you change
upsmon --exec test.battery.start.quick
upsmon --set battery.charge.low=20
upsmon --history 7d                   # recorded samples as a table
upsmon --events                       # the event log
upsmon --tests                        # the self-test history
upsmon --plug                         # everything the smart plug reports
upsmon --plug-on / --plug-off         # switch the socket
upsmon --sensors                      # readings pushed by battery sensors
upsmon shelly plug dump               # the full Shelly toolkit, by role
upsmon --reset-data [what]            # erase recorded data
upsmon --check                        # is the whole system healthy
upsmon --diag                         # settings, API latency, database size
upsmon --oneshot                      # one collection, then exit
```

`--set` and `--exec` use the running daemon when there is one, so they get the
same permission handling and the same event log as the dashboard.

## API

Read endpoints are open on the loopback; the two that change something require
`X-Upsmon-Token`.

| endpoint | returns |
|---|---|
| `GET /api/status` | current snapshot: every variable, descriptions, verdict, thresholds |
| `GET /api/health` | daemon uptime, last contact, error count, database statistics |
| `GET /api/history?range=24h&points=600` | samples, thinned, mixing full and hourly data |
| `GET /api/events?limit=100` | the event log |
| `GET /api/outages` | power failures with duration and how far the battery fell |
| `GET /api/tests` | self-test history: duration, time on battery, lowest voltage |
| `GET /api/capabilities` | writable variables and supported commands |
| `POST /api/command` | `{"command": "beeper.disable"}` |
| `POST /api/set` | `{"var": "battery.charge.low", "value": "20"}` |
| `GET /api/plug` | the plug's current state, every field it reports |
| `GET /api/plug-history` | plug samples: power, energy, voltage, current, counters |
| `GET /api/sensors`, `/api/sensor-history` | what sensors have pushed |
| `POST /api/plug/switch` | `{"on": false}` |
| `POST /api/plug/reset` | `{"types": ["aenergy"]}`, or `[]` for every counter |
| `POST /api/reset` | `{"scope": "all / history / events / tests / outages"}` |

## What was verified

Against a mock upsd replaying a real APC Back-UPS BX1200MI: every endpoint, token
authentication, refusal of power-cutting commands, control actions through both
the daemon and a direct connection, self-test following, a staged mains failure
with correct outage recording, hourly roll-up and year-long charts, 60 concurrent
dashboard polls (median 27 ms), and recovery after the UPS disappeared mid-flight
— which logs one pair of events rather than one per failed poll.

The PHP was checked structurally rather than executed; no interpreter was
available in the build environment. Run `php -l` on both files after copying them
across.

Current version: **3.4.4**
