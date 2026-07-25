# plexmon (single file) — installation and migration

Same daemon as the package version, merged into one file: `plexmon.py`. Pure
Python standard library, nothing to `pip install`. It replaces the old PHP
collector and all its systemd timers; `index.php` is a thin client that reads
the daemon's localhost API.

Everything the old dashboard did is preserved, and the rule still holds: **a
normal run never wakes a sleeping disk.**

---

## 1. Remove the old setup

```bash
systemctl disable --now plex-status.timer 2>/dev/null
systemctl disable --now plex-status.service 2>/dev/null
systemctl disable --now plex-status-sessions.timer 2>/dev/null
systemctl disable --now plex-status-wake.path 2>/dev/null
systemctl disable --now plex-status-wake.service 2>/dev/null

rm -f /etc/systemd/system/plex-status*.{timer,service,path}
rm -f /etc/sudoers.d/plex-status
rm -f /usr/local/sbin/plex-status-collect.php /usr/local/sbin/plex-lib.php
rm -rf /var/www/html/smart/req
systemctl daemon-reload
```

Leave `/var/lib/plex-status/` — the daemon reuses the SMART cache and history
there, so charts keep their past across the switch.

---

## 2. Install the daemon

```bash
install -d /usr/local/lib/plexmon
install -m 755 plexmon.py /usr/local/lib/plexmon/plexmon.py

# optional convenience wrapper
cat > /usr/local/bin/plexmon <<'EOF'
#!/bin/bash
exec /usr/bin/python3 /usr/local/lib/plexmon/plexmon.py "$@"
EOF
chmod +x /usr/local/bin/plexmon

install -d -m 755 /var/lib/plex-status
```

## 3. Install the page

```bash
install -m 644 index.php       /var/www/html/smart/index.php
install -m 644 plexmon-api.php /var/www/html/smart/plexmon-api.php
```

## 4. The service

```bash
cp plex-status.service /etc/systemd/system/plex-status.service
systemctl daemon-reload
systemctl enable --now plex-status.service
systemctl status plex-status.service
```

The unit's `RuntimeDirectory=plex-status` recreates `/run/plex-status` on every
boot (it's cleared on reboot), so the API token always has somewhere to live.

## 5. Verify

```bash
plexmon --check      # every line should read "ok"
plexmon --diag       # API latency + each disk's inferred power state
```

---

## Running it by hand

```bash
python3 plexmon.py            # the daemon (what systemd runs)
python3 plexmon.py --check    # self-check against a running daemon
python3 plexmon.py --diag     # probe the API + show disk state
python3 plexmon.py --wake     # tell the RUNNING daemon to wake the disks + refresh SMART
python3 plexmon.py --refresh  # re-read SMART from disks already spinning (wakes nothing)

# Apple TV remote (needs atv_enable in config.json)
plexmon --atv                 # which televisions are known, and which are paired
plexmon --atv-scan            # search the network again
plexmon --atv-pair "Ložnice"  # pair — asks for the PIN the television shows
plexmon --atv-unpair "Ložnice"
plexmon --atv-unpair-all
python3 plexmon.py --oneshot  # one collection without the service, print a summary, exit
python3 plexmon.py --oneshot --wake   # …forcing a SMART read
python3 plexmon.py -v         # add debug logging
```

## Configuration

Everything lives in one JSON object in `/etc/plex-status/config.json`. Anything
you leave out keeps its default, so the file only needs the settings you change.
Every option can also be set from the environment with a `PLEXMON_` prefix
(`PLEXMON_TEMP_WARN=55`), which takes precedence over the file.

**`CONFIG.md` is the full reference** — every option, what it does, and ready-made
templates. The essentials:

```json
{
  "spindown_after_s": 900,
  "bridge_wakes_siblings": true,
  "temp_warn": 60,
  "temp_crit": 70,
  "temp_warn_nvme": 70,
  "temp_crit_nvme": 80,
  "smart_min_interval": 300,
  "perf_proxy_url": "https://plex.falco81.net"
}
```

An unknown key is reported rather than silently ignored, and so is a malformed
file — check `plexmon --diag`, which prints the settings actually in force.

`spindown_after_s` is how long a disk must be idle before it's reported as
standby. It's an assumption, not a measurement — the daemon never asks a drive
its power state. Use `spindown-info.sh` to find the real figure for your drives.
The daemon sanity-checks it passively: if a drive's Start_Stop_Count grows while
we never once reported it as standby, it spun down behind our back and
`plexmon --check` says so.

`bridge_wakes_siblings` reflects how multi-bay USB-SATA enclosures behave: the
bridge powers all its bays together, so touching one disk spins up every disk in
that enclosure. Disks sharing a bridge then share a power state, and SMART may be
read from them at no extra cost. Disks on a *different* bridge are unaffected.
Set `false` for enclosures that genuinely power each bay independently.

The temperature limits are picked per disk: what the drive itself publishes wins
(an NVMe typically warns at 77 °C), then the `*_nvme` values for flash that
publishes nothing, then the global pair meant for spinning disks.

`smart_min_interval` throttles routine SMART reads — values move over minutes,
not seconds, and each read costs a `smartctl` process. Wake requests and the
daily sweep ignore it.

## Diagnostic tools

All of these read only `/sys` and the daemon's API unless stated otherwise, so
none of them wakes a disk by accident.

| script | what it answers |
|---|---|
| `check-idle.sh` | Is anything touching the disks? Watches the kernel I/O counters and reports how long each disk has been quiet. Run it for 20 minutes with nothing playing. |
| `spindown-info.sh` | After how long do the disks *really* sleep? Prints the configured assumption and what the system reports; `--measure` times a probe read at rising intervals to find the true figure (this one does wake the disks). |
| `bridge-test.sh` | Which disks share a USB bridge, and does waking one wake the rest? `--test` runs the experiment with a control measurement. |
| `nvme-check.sh` | Dumps every SMART field the NVMe exposes and compares it against what the daemon parsed, flagging anything missed. |
| `diag-art.sh` | Traces the now-playing poster from the Plex API to the file the page serves. |
| `hist-check.py` | Why a chart looks wrong: reports the keys the history is stored under, how much of each series is actually filled, and any gaps in the recording. |

## Television remote

Optional playback control for Apple TV, LG and Samsung sets, off by default.
Plex cannot do this itself — its client apps advertise no remote-control
capability — so the daemon speaks each maker's own protocol, over a connection
it keeps open so a press lands in milliseconds rather than seconds.

Apple TV and LG get real playback control. Samsung offers only remote-key
presses, which the foreground app interprets, so it works but is less certain.

Each make needs its library available to the interpreter running the daemon,
which in turn needs Python 3.10 or newer, plus a more generous memory limit than
the monitoring alone requires. **`CONFIG.md` has the full setup**; the short
version:

```bash
dnf install -y python3.12
python3.12 -m pip install pyatv aiowebostv samsungtvws   # only what you own
# ExecStart=/usr/bin/python3.12 /usr/local/lib/plexmon/plexmon.py
echo '{"atv_enable": true}' >> /etc/plex-status/config.json   # merge, one object
plexmon --atv-probe 192.168.1.50    # LG and Samsung are named by address
plexmon --atv-pair 192.168.1.50
```

A missing or broken library disables that make only; disk monitoring carries on
untouched.

## The no-wake guarantee

No `hdparm` (the JMS567 bridge mistranslates its power-mode query and wakes the
disk), and no `smartctl` on a disk without recent I/O. Power state comes purely
from `/sys/block/*/stat`, which is a memory read. `plexmon --diag` shows each
disk's inferred state and the reason, so you can confirm nothing reaches the
platters on the routine path.
