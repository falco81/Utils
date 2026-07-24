# Plex status page — installation (AlmaLinux 9 + nginx 1.20 + PHP-FPM)

The page will be served at `https://plex.falco81.net/smart/`.
Architecture: a **root collector** (systemd timer, every 5 min) → `data.json` → the **PHP page** only reads and renders it.
PHP has no privileges; `smartctl` runs only as root.

---

## 1. Packages

```bash
dnf install -y php-fpm php-cli php-json smartmontools hdparm policycoreutils-python-utils
```

`hdparm` is used to read each disk's power state (active / standby / sleeping)
**without** spinning it up. It is optional — without it, disks just show as "Active".

Check the versions (AlmaLinux 9 ships PHP 8.0, which is fine):

```bash
php -v
smartctl --version | head -1
```

## 2. File placement

```bash
install -d -m 755 /var/www/html/smart
install -m 644 index.php               /var/www/html/smart/index.php
install -m 700 plex-status-collect.php /usr/local/sbin/plex-status-collect.php
```

## 3. PHP-FPM: make the socket reachable by nginx

By default AlmaLinux configures FPM for Apache. Adjust the socket so `nginx` can reach it:

```bash
sed -i \
  -e 's/^listen.owner = apache/listen.owner = nginx/' \
  -e 's/^listen.group = apache/listen.group = nginx/' \
  /etc/php-fpm.d/www.conf

systemctl enable --now php-fpm
systemctl restart php-fpm
```

Verify the socket exists and has the right owner:

```bash
ls -l /run/php-fpm/www.sock      # should be  srw-rw----  nginx nginx
```

## 4. nginx — add a location to the EXISTING 443 server block

In `/etc/nginx/conf.d/plex.conf`, inside the `server { listen 443 ... }` block,
**before** `location / { proxy_pass ... }`, add:

```nginx
    # --- Status page /smart (served locally, does NOT go to Plex) ---
    location = /smart { return 301 /smart/; }

    location ^~ /smart/ {
        root  /var/www/html;          # /smart/ -> /var/www/html/smart/
        index index.php;

        # LAN only – remove these if you don't want to restrict access
        allow 192.168.0.0/16;
        allow 10.0.0.0/8;
        allow 127.0.0.1;
        deny  all;

        location ~ \.php$ {
            include        fastcgi_params;
            fastcgi_pass   unix:/run/php-fpm/www.sock;
            fastcgi_param  SCRIPT_FILENAME $document_root$fastcgi_script_name;
        }
    }
```

Adjust the `allow` ranges to match your network. Then:

```bash
nginx -t && systemctl reload nginx
```

## 5. SELinux

The nginx→php-fpm socket and reading files from `/var/www/html` are allowed by the
base policy. Just fix the file contexts:

```bash
restorecon -Rv /var/www/html/smart
```

(If PHP returns 502/permission errors, check `getsebool httpd_can_network_connect` –
but for a socket-only FPM setup you don't need it enabled.)

## 6. Collector as a systemd timer (every 5 min)

```bash
cat > /etc/systemd/system/plex-status.service <<'EOF'
[Unit]
Description=Collect Plex server status (SMART, temps, disks)
After=plexmediaserver.service

[Service]
Type=oneshot
ExecStart=/usr/bin/php /usr/local/sbin/plex-status-collect.php
Nice=10
IOSchedulingClass=idle
EOF

cat > /etc/systemd/system/plex-status.timer <<'EOF'
[Unit]
Description=Collect Plex server status every 5 minutes

[Timer]
OnBootSec=2min
OnCalendar=*:0/5
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now plex-status.timer
systemctl start plex-status.service      # first run immediately
```

Verify the JSON was created and the page responds:

```bash
systemctl status plex-status.service --no-pager
ls -l /var/www/html/smart/data.json
curl -s http://127.0.0.1/smart/ | head -5   # or open it via https in a browser
```

---

## Manual run / debugging

The collector takes optional CLI flags (they do nothing when run by the timer):

```bash
php /usr/local/sbin/plex-status-collect.php --debug     # verbose per-disk diagnostics
php /usr/local/sbin/plex-status-collect.php --dry-run    # verbose + DON'T write data.json/cache, print JSON
php /usr/local/sbin/plex-status-collect.php --wake       # read SMART even from sleeping disks (spins them up)
php /usr/local/sbin/plex-status-collect.php --help
```

Debug output goes to **stderr**, the machine summary/JSON to **stdout**, so you can
separate them: `... --debug 2>&1 >/dev/null` shows only diagnostics.

Per disk you'll see the chosen filesystem, the raw `hdparm -C` power state, the I/O
delta, and every `smartctl -d <type>` attempt with its score — handy for figuring out
which device type a USB bridge actually answers. Example:

```
[sda] /dev/sda  tran=usb  model=ST8000VN004-3CP101  serial=WWZBVAW9
    fs: mount=/data5 size=7999... used=2999... pct=38%
    hdparm -C:  drive state is:  active/idle
    power=active  io=idle (Δread=0 Δwrite=0 sectors/1s)
    smartctl -d sat       : score=1  health=y temp=- attrs=n
    smartctl -d sat,12    : score=7  health=y temp=38 attrs=y
    -> chosen: -d sat,12 (score 7)
    => temp=38 health=PASSED poh=12483 realloc=0 pending=0 uncorrect=0
```

## Troubleshooting

| problem | where to look |
|---|---|
| page shows "No data yet" | `journalctl -u plex-status.service -n 30` |
| 502 Bad Gateway | is php-fpm running? owner of `/run/php-fpm/www.sock`? |
| 403 Forbidden | your IP is not in the `allow` ranges |
| temp/SMART shows "—" on a USB disk | the enclosure doesn't pass SMART through the bridge; try manually: `smartctl -a -d sat /dev/sdX`, then `-d usbjmicron` |
| sessions empty | token is read from Preferences.xml; if you rotated it, no problem, the current one is read |

## Notes

- **Now playing.** The overview shows what each stream is doing: title, season and
  episode, who is watching, on what, direct play vs transcode per track, bandwidth,
  and which volume the file is being read from. The panel refreshes itself every few
  seconds **without any extra service** — `index.php?sessions=1` asks Plex directly
  and `index.php?art=…` proxies the poster, so the token never reaches the browser.

  For that the page needs a token it can read, which the collector mirrors for it:

  ```php
  const SESSION_TOKEN_FILE  = '/etc/plex-status/token';   // '' disables this
  const SESSION_TOKEN_OWNER = 'nginx';
  ```

  The file is written 0400 and owned by the web user. The trade-off: anything able
  to run code as that user could read the token. On a LAN-only server that is
  normally acceptable; set the constant to `''` if you would rather not, and the
  panel falls back to the snapshot the collector writes on its normal run (so it
  updates every few minutes instead of every few seconds). Everything else keeps
  working either way.

  Note that the page never reads anything off the media disks — a single read from a
  spun-down disk would wake it, and the page reloads every 60 s. All disk data comes
  from `data.json`, which the collector prepares.

- **"Wake all disks" button.** No helper service and no privileges involved: the
  page reads a small probe file off each data volume, and *that read is what spins
  the disk up*. It only ever happens on a click — never on a page load, which would
  defeat the whole point of letting the disks sleep.

  The read uses `O_DIRECT` so it bypasses the page cache; a cached read would be
  answered from memory and would leave the disk asleep.

  Verification is direct rather than inferred: `?wakecheck=1` gives each disk one
  second to answer a probe. A spinning disk answers in milliseconds, a sleeping one
  can't answer at all, so the bar fills as disks come up and only says "all awake"
  when they really are.

  The probe files (`<mount>/.wake-probe`, 1 MiB) are created by the collector during
  a `--wake` run, when the disks are spinning anyway — so just run it once:

  ```bash
  php /usr/local/sbin/plex-status-collect.php --wake
  ```

  Until they exist the button says so instead of failing silently.

  **Refreshing SMART straight away (optional).** Spinning the disks up needs no
  privileges, but reading SMART does. Grant the web user permission to run this
  one command and the button will also re-read temperatures and attributes while
  the disks are up, instead of leaving them until the next collector pass:

  ```bash
  # use the user php-fpm actually runs as — on AlmaLinux that is usually apache
  ps -o user= -C php-fpm | sort -u

  cat > /etc/sudoers.d/plex-status <<'EOF'
  apache ALL=(root) NOPASSWD: /usr/bin/php /usr/local/sbin/plex-status-collect.php --wake
  EOF
  chmod 440 /etc/sudoers.d/plex-status
  visudo -c
  ```

  The rule matches that exact command line and nothing else, and the collector
  itself is root-owned mode 700, so the web user can run it but cannot alter what
  it does. Skip this and the button still works — it simply reports "SMART
  refreshes on the next collector pass" once the disks are awake.

- **Zoom and pan.** Scroll the mouse wheel over a chart to zoom, or pinch with two
  fingers on a tablet; whatever moment sits under the pointer stays put while the
  window widens or narrows. Drag with one finger (or the mouse) to move through
  history. The range buttons are presets for the window width — after a free-form
  zoom none of them is highlighted and the current width is shown next to them
  instead. A "now" button appears while you're looking at the past, and the
  auto-refresh pauses so a reload can't yank the view back mid-read.
- **Two copies of the history.** `data.json` carries a down-sampled series
  (`HISTORY_POINTS`) so the first paint stays fast; the collector also writes
  `history-full.json` next to it with every recorded sample. The page fetches the
  full copy lazily — via `index.php?series=full`, which reuses the same PHP chart
  code — the first time you open Trends. Retention is `HISTORY_DAYS` (7 by
  default) and is enforced by age, not sample count, so changing the timer
  interval can't silently shorten it.

- **Two tabs.** *Overview* holds the live state — status, activity, performance and the
  disk cards. *Trends* holds the charts. The chosen tab is kept in the URL fragment, so
  the 60-second auto-refresh leaves you where you were.
- **Charts are interactive.** Range buttons (6 h / 24 h / 3 d / 7 d / All) zoom the time
  axis, and hovering — or tapping, on a tablet — shows a crosshair with every series'
  value at that moment. Everything is drawn client-side from data already in the page;
  no chart library and no extra requests.
- **Activity tile.** Shows what Plex is doing right now, worked out from the transcoder
  and scanner command lines: preview (BIF) generation, chapter thumbnails, loudness
  analysis, library scans and real playback transcodes — each with the file, the library
  it belongs to, how long it has been running and its CPU share. Reads `/proc` via `ps`
  only, so it touches no disks.

- **Trends.** Every run records API latency, CPU temperature, load, memory and each
  disk's temperature and fill level into `/var/lib/plex-status/history.json` (7 days by
  default, `HISTORY_KEEP`). The page charts them, down-sampled to `HISTORY_POINTS` so
  `data.json` stays around 20 KB. The capacity chart also projects when each volume
  will fill up, once there is enough of a trend to fit a line to.
- **Temperature gaps are deliberate.** A sleeping disk reports no temperature, and the
  collector will not wake it, so those periods are drawn as breaks rather than joined
  up. Points appear whenever a disk happens to be awake — the daily `--wake` timer
  guarantees at least one reading per day.
- **SMART error counters aren't charted.** A line of zeros says nothing; instead each
  card shows how long the reallocated / pending / uncorrectable counters have gone
  without moving, and the collector logs a warning the moment one changes.

- **Performance section.** Each run times the Plex API (`/identity`,
  `/library/sections`, `/hubs`) over a single reused connection — the way a browser
  sees it — and keeps a rolling history (`/var/lib/plex-status/perf-history.json`,
  48 h by default) that the page draws as a trend line. A slow climb on the
  home-screen chart is the early warning that the database wants a
  `REINDEX; VACUUM; ANALYZE;` pass. Set `PERF_ENABLED = false` to skip it.
- **Proxy comparison.** `PERF_PROXY_URL` points at your nginx vhost; the same
  endpoints are timed through it, so the difference is exactly what the proxy costs
  per request. Under ~3 ms means nginx is not your bottleneck and anything slow is
  Plex-side. Empty the constant to disable.

- **Disk cards are sorted by mountpoint** (natural order, so `disk2` comes before
  `disk10`); unmounted disks go last.
- **Power state** (top-right badge on each card): `Idle` = spinning, ready; `Reading` /
  `Writing` = live I/O during the sample window; `Standby` / `Sleeping` = spun down
  (card is dimmed). I/O is measured from `/sys/block/*/stat` over a 1 s window.
- **Power state on USB enclosures.** Many USB-SATA bridges (JMicron JMS567 and
  friends) don't pass the ATA "check power mode" command through, so `hdparm -C`
  reports `standby` for every disk regardless of what it's doing. When the collector
  detects that, it works the state out from the kernel's I/O counters instead — these
  are read from memory and never touch the disk. A drive with no I/O for
  `SPINDOWN_AFTER_S` (default 15 min) is treated as spun down; set that constant to
  roughly match your drives' own spindown timer.
- **A normal run never spins a disk up.** SMART is read only when the disk is *known*
  to be awake (`hdparm` says so, or I/O was seen during the sample). Everything else
  uses the cache. Only `--wake` / `WAKE_STANDBY` can cause a spin-up. If you'd also
  like SMART refreshed for disks that served I/O since the previous run — almost
  certainly still spinning, but not guaranteed — set `SMART_WHEN_RECENT_IO = true`.
- **Sleeping disks are never woken for SMART.** When a disk is in standby/sleeping,
  the collector skips `smartctl` and shows the last-known values from a cache
  (`/var/lib/plex-status/smart-cache.json`), labelled "cached … ago". The cache fills
  in whenever the disk is active. If you'd rather always see full SMART data and don't
  mind spinning idle disks up on every poll, set `WAKE_STANDBY = true` at the top of the
  collector, or run manually with `--wake`.
- **Keeping the cache fresh while letting disks sleep (recommended).** Keep the 5-minute
  timer as-is (no wake — it still shows live power state, I/O and usage), and add a second
  timer that wakes the disks once a day to refresh SMART. Cards then show "cached … ago"
  instead of blank:

  ```bash
  cat > /etc/systemd/system/plex-status-wake.service <<'EOF'
  [Unit]
  Description=Refresh Plex disk SMART cache (wakes sleeping disks)
  After=plexmediaserver.service
  [Service]
  Type=oneshot
  ExecStart=/usr/bin/php /usr/local/sbin/plex-status-collect.php --wake
  Nice=10
  IOSchedulingClass=idle
  EOF

  cat > /etc/systemd/system/plex-status-wake.timer <<'EOF'
  [Unit]
  Description=Daily wake + SMART refresh for Plex status
  [Timer]
  OnCalendar=*-*-* 05:00:00
  Persistent=true
  [Install]
  WantedBy=timers.target
  EOF

  systemctl daemon-reload && systemctl enable --now plex-status-wake.timer
  ```
- Some cheap USB bridges only report a SMART **health status** and no temperature or
  attribute table (you'll see `SMART OK` but a "—" temperature). Others return a bogus
  `0 °C`, which the collector ignores. Run with `--debug` to see exactly what each
  `-d <type>` returns for a given disk.
- Change the collection interval in `OnCalendar` (e.g. `*:0/2` = every 2 min). Reading
  SMART on an active disk keeps it awake, so if you rely on spindown, don't poll too often.
- `data.json` contains no token – it is safe to read.
- Temperature thresholds (45/55 °C) are constants at the top of `plex-status-collect.php`.
