# upsmon

Monitoring, history and control for a UPS published over the network by a
Synology NAS or any other host running Network UPS Tools — and, optionally, for
a Shelly smart plug and the sensors that report to it, so the power being drawn
is measured rather than estimated and the socket can be switched from the same
dashboard.

* `upsmon.py` — the daemon and the command-line tool, one file, standard library only
* `web/index.php`, `web/upsmon-api.php` — the dashboard and its thin API client
* `deploy/` — systemd unit, logrotate rules, sample configuration, nginx server block
* `test/` — the checks used during development; nothing here ships to the server
* `INSTALL.md` — the full AlmaLinux 9 walkthrough, twenty sections

## How the pieces fit

```
Synology NAS                       AlmaLinux 9
┌──────────────┐                 ┌────────────────────────────────────────┐
│ UPS over USB │                 │ upsmon.service   (user "upsmon")       │
│      ↓       │    TCP 3493     │   polls the UPS every 10 s             │
│ upsd (NUT)   │ ───────────────►│   polls the plug every 15 s            │
└──────────────┘                 │   writes /var/lib/upsmon/history.db    │
                                 │   serves 127.0.0.1:9848, token-guarded │
Shelly plug     ◄── JSON-RPC ────┤        ↑                               │
Shelly H&T      ─── webhook ────►│ php-fpm ← nginx :443 ← browser         │
                                 └────────────────────────────────────────┘
```

The daemon is the only thing that talks to any of the devices. PHP relays JSON
and holds no credentials; the endpoints that change something send a token read
from a file the browser never sees. Nothing in the chain needs root, and the
plug is reached over plain local RPC with no cloud account involved.

## What it watches

**The UPS**, over the network from a Synology NAS or any host running Network
UPS Tools: status, charge, remaining runtime, load, voltages, every variable the
driver publishes, self tests and power failures.

**A Shelly smart plug**, optionally: the watts actually being drawn, voltage,
current, power factor, frequency, its own temperature, the energy total and the
runtime and switching counters. The socket can be switched and its counters
reset from the dashboard.

**Sensors**, optionally: anything that pushes a reading to the listener, or a
Shelly sensor addressed directly. Temperature, humidity, battery and signal.

Each part is independent. A plug that stops answering never disturbs UPS
monitoring, and none of it is needed for the UPS side to work.

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

**A reset is verified, not assumed.** The plug answers `Switch.ResetCounters`
with success whether or not it understood the counter name, so the values are
read back and compared. Firmware that quietly ignores a counter is reported as
exactly that rather than as a reset that worked.

**Requests to the plug are paced.** Gen3 firmware with authentication answers
429 when they arrive closer together than about a second, and the digest
challenge is reused rather than renegotiated on every call. Together those take
a poll from six HTTP requests to two.

**A missed reading does not blank the panel.** The last values stay on screen,
greyed out and dated, and after a restart they come from the recorded history
until a fresh reading lands.

**An error inside an endpoint answers rather than raises.** A dashboard polling
every five seconds would otherwise bury the journal in identical stack traces
and push the real cause out of view; instead the caller gets a short message and
the log gets one line.

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

Six tabs. **Overview**: status, charge, runtime, load and voltages, the plug's
live figures with a switch for the socket, two 24-hour charts, and whatever the
sensors last reported. **History**: six charts for the UPS from 6 hours to a
year, fourteen more from the plug, five from the sensors, and every power
failure.
**Tests**: self-test verdicts with a battery-voltage trend. **Events**.
**Control**: UPS commands, writable settings, the socket, a reset button for
each plug counter, and clearing recorded data — all behind the same PIN.
**All variables**: everything the UPS, the plug and the sensors report.

It refreshes every five seconds, pauses when the browser tab is hidden, and can
be paused by hand from the indicator in the corner. The tab icon is a battery
coloured by the verdict, so a UPS in trouble shows up without switching to it.

Command buttons carry a plain label and an icon rather than the raw NUT name;
resting the pointer on one for five seconds reveals the exact command and what
it does.

## Command line

Everything the web interface does is available without it, and a good deal more.

### The UPS

```bash
upsmon --status                       # full report straight from the UPS
upsmon --list-rw                      # what this UPS lets you change
upsmon --set battery.charge.low=20    # change a writable setting
upsmon --exec test.battery.start.quick
upsmon --tests                        # the self-test history
upsmon --history 7d                   # recorded samples as a table
upsmon --events                       # the event log
```

`--set` and `--exec` use the running daemon when there is one, so they get the
same permission handling and land in the same event log as the dashboard.

### The smart plug

Short forms for the everyday things, which go through the daemon:

```bash
upsmon --plug                         # the full report
upsmon --plug-on                      # switch the socket on
upsmon --plug-off                     # and off
upsmon --plug-reset                   # reset every counter
upsmon --plug-reset aenergy           # or just one
upsmon --sensors                      # what sensors have pushed
```

### The full Shelly toolkit

Everything the standalone Shelly reader could do, addressed by role rather than
by IP — the address and password come from the configuration. Each command has
its own `--help`.

```
upsmon shelly [plug|sensor] <command> [options]
```

| reading | |
|---|---|
| `discover` | find Shelly devices on the LAN over multicast DNS |
| `info` | identity, firmware, auth and Matter state |
| `dump` | every value the device exposes |
| `methods [COMPONENT] [--examples]` | every RPC method it supports, with a note on what each is for |
| `poll` | a live table. `--interval`, `--count`, `--csv FILE` |
| `watch` | the same, pushed over the device's websocket |
| `listen`, `serve` | receive webhooks, or an outbound websocket |

| controlling | |
|---|---|
| `on`, `off`, `toggle` | switch the relay |
| `reset-counters [type…]` | no type means every counter |
| `reboot [--wait]` | restart, optionally waiting for it to come back |

| configuring | |
|---|---|
| `config [COMPONENT] ['{JSON}']` | read or write a component's settings |
| `matter status\|on\|off\|code` | Matter, and the pairing code |
| `ble status\|on\|off\|rpc-on\|rpc-off` | radio and RPC over Bluetooth |
| `call METHOD ['{JSON}']` | anything the above does not cover |

`--host` and `--password` address a device that is not in the configuration at
all; `--json` and `--no-color` work anywhere, before the command or after it.

```bash
upsmon shelly plug methods Switch --examples
upsmon shelly plug config switch:0 '{"auto_off": true, "auto_off_delay": 300}'
upsmon shelly plug poll --interval 5 --csv plug.csv
upsmon shelly plug call Switch.SetConfig '{"id":0,"config":{"power_limit":2900}}'
upsmon shelly --host 10.0.0.5 --password secret info
```

A `config` write is read back afterwards, because firmware accepts a whole
object and silently drops keys it does not know — the ones that took are listed
separately from the ones that did not. `watch` needs a device without a
password, the websocket carrying no authentication. `discover` does not cross
subnets. `ble` and `matter` take effect after a reboot, which `--reboot` will do.

### Housekeeping

```bash
upsmon --check                        # is the whole system healthy
upsmon --diag                         # settings, API latency, database size
upsmon --oneshot                      # one collection, then exit
upsmon --reset-data [what]            # erase history: all, charts, events,
                                      # tests, outages, plug or sensor
upsmon --aggregate                    # roll old samples up now, not on the hour
upsmon                                # run the daemon in the foreground
```

## API

Read endpoints are open on the loopback; the two that change something require
`X-Upsmon-Token`.

The UPS:

| endpoint | returns |
|---|---|
| `GET /api/status` | current snapshot: every variable, descriptions, verdict, thresholds, and the plug and sensors alongside |
| `GET /api/health` | daemon uptime, last contact, error count, database statistics |
| `GET /api/history?range=24h&points=600` | samples, thinned, mixing full and hourly data |
| `GET /api/events?limit=100` | the event log |
| `GET /api/outages` | power failures with duration and how far the battery fell |
| `GET /api/tests` | self-test history: duration, time on battery, lowest voltage |
| `GET /api/capabilities` | writable variables and supported commands |
| `POST /api/command` | `{"command": "beeper.disable"}` |
| `POST /api/set` | `{"var": "battery.charge.low", "value": "20"}` |
| `POST /api/poll` | read the UPS now rather than waiting for the next tick |

The smart plug and the sensors:

| endpoint | returns |
|---|---|
| `GET /api/plug` | current state, every field the plug reports, and each counter with its value and the date it started from |
| `GET /api/plug-history?range=24h` | power, energy, voltage, current, power factor, frequency, temperature, counters, signal, uptime |
| `GET /api/sensors` | the latest push from each device, and where the listener is |
| `GET /api/sensor-history?range=24h` | temperature, humidity, battery, signal |
| `POST /api/plug/switch` | `{"on": false}` — needs the PIN |
| `POST /api/plug/reset` | `{"types": ["aenergy"]}`, or `[]` for every counter — needs the PIN |

Housekeeping:

| endpoint | returns |
|---|---|
| `POST /api/reset` | `{"scope": "…"}` — `all`, `history`, `events`, `tests`, `outages`, `plug` or `sensor` |

## What was verified

Against mocks that replay real devices — an APC Back-UPS BX1200MI over NUT and a
Shelly Plug M Gen3 over RPC, including one that insists on SHA-256 digest
authentication and refuses requests less than a second apart:

* every API endpoint, token authentication, and refusal of power-cutting commands
* control actions through the daemon and through a direct connection
* self-test following, and a staged mains failure with correct outage recording
* hourly roll-up, year-long charts, and migrating a database made by an older version
* the plug: switching, per-counter resets including firmware that only pretends
  to clear them, rate-limit backoff, and recovery after the plug disappears
* 60 concurrent dashboard polls, median 27 ms

The PHP is checked structurally rather than executed; no interpreter was
available in the build environment. Run `php -l` on both files after copying
them across, and see `test/README.md` for the checks used during development.

Current version: **3.6.5**
