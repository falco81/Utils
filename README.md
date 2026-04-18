# Utils

A collection of independent Python utilities. Each script is standalone — no shared dependencies, no framework. Pick what you need.

Repository layout:

```
Utils/
├── disk_scan.py          USB disk duplicate finder
├── dircomp.py            SHA-256 directory diff
├── md2html.py            Markdown to retro-terminal HTML
├── fc2_client.py         Focusrite Control 2 WebSocket client
├── audiobookshelf/
│   ├── abs_download_all.py     bulk-queue every episode of every podcast
│   └── mujrozhlas_rss_gen.py   full-archive RSS generator for mujRozhlas.cz
└── vsphere/
    ├── vcenter_esxi_ssh.py     run commands on all ESXi hosts in a vCenter
    ├── esxi_direct_ssh.py      same as above, but from a JSON host list
    ├── hosts.json              sample host list
    └── requirements.txt        vSphere-specific dependencies
```

---

## Root scripts

### `disk_scan.py` — USB disk duplicate finder

Scans USB drives for duplicate files and generates a deletion plan that maximises free space on one disk.

**Workflow:**

```
# Scan each disk (plug in, run, unplug, repeat)
python disk_scan.py scan J: --label red_flash
python disk_scan.py scan J: --label blue_flash

# Compare all scan results
python disk_scan.py compare
```

**How it works:**

- `scan` walks the disk, computes MD5 fingerprints and records disk capacity. For large files (> 512 KB) only three 256 KB chunks are read (start / middle / end) to keep scanning fast on 2 TB drives connected over USB. Results are saved to `disk_scan_<label>.json` in the current directory.
- `compare` loads all `disk_scan_*.json` files, finds duplicates by hash, and decides which copy to keep. Files inside folders whose name starts with `_` are always preferred for survival. Among the remaining candidates the algorithm picks the copy on the disk with the least free space, then maximises the space freed on one target disk.
- Outputs a colour-coded CLI report, an HTML report (`duplicates_report.html`), and one `disk_scan_<label>.cmd` per disk containing ready-to-run `DEL` commands.

**CLI output is Windows 10 cmd.exe compatible** (ASCII-only, UTF-8 reconfigure on startup).

**Dependencies:** stdlib only (Python 3.10+)

---

### `dircomp.py` — Directory comparison by SHA-256

Compares the full contents of two directories and classifies every file.

```
python dircomp.py <dir_A> <dir_B>
python dircomp.py .\src .\dst --ignore "*.pyc" --ignore "__pycache__"
python dircomp.py D:\old D:\new --output diff_report.txt
```

**Categories reported:**

| Category | Meaning |
|---|---|
| Identical | Same path, same hash |
| Content differs | Same path, different hash (size delta shown) |
| Only in A | Missing from B |
| Only in B | Missing from A |
| Moved / renamed | Same hash, different path |

SHA-256 is computed in 1 MB chunks (memory-friendly on large trees). ANSI colour output is enabled automatically on Windows 10 (1511+). Exit code `0` = directories are identical, `1` = differences found — useful in CI pipelines.

**Options:**

```
--ignore PATTERN    Glob pattern to skip files/dirs (repeatable)
--show-identical    Also list files that match
--output FILE       Save plain-text report to file
```

**Dependencies:** stdlib only (Python 3.8+)

---

### `md2html.py` — Markdown to retro terminal HTML

Converts a Markdown file into a self-contained HTML page with a retro terminal aesthetic: fixed sidebar table of contents, CRT scanlines, blinking cursor, and a statusbar.

```
python md2html.py input.md
python md2html.py input.md -o output.html --theme amber
python md2html.py input.md --title "My Doc" --no-scanlines --verbose
python md2html.py --list-themes
```

**Themes:**

| Theme | Description |
|---|---|
| `green` | Default — green phosphor |
| `amber` | Classic amber phosphor monitor |
| `blue` | Cold cyberpunk neon |
| `red` | Alert / danger terminal |
| `purple` | Synthwave / vaporwave |
| `c64` | Commodore 64 blue |
| `dos` | Classic DOS blue editor |
| `nord` | Cool nordic blues and teals |
| `paper` | Light mode, clean technical document |
| `hacker` | Hacker movie aesthetic |
| `retro` | Warm sepia vintage computer |

Title, subtitle, sidebar label, and statusbar text are auto-detected from the document structure. They can be overridden via CLI flags or embedded in the Markdown file:

```markdown
<!-- md2html
title: Custom Title
subtitle: Custom subtitle
label: SYSTEM // NAME
status: CPU · RAM
-->
```

**Dependencies:** `pip install markdown`

---

### `fc2_client.py` — Focusrite Control 2 WebSocket client

Python client for controlling Scarlett audio interfaces via the Focusrite Control 2 (FC2) WebSocket API. Implements AES70/OCP.1 over two WebSocket channels:

- **Port 58323** — authentication channel (pairing, `RequestApproval`)
- **Port 58322** — control channel (gain, phantom power, notifications), encrypted with Noise\_NK\_25519\_AESGCM\_SHA256

```
python fc2_client.py pair          # First-time pairing (generates X25519 keypair)
python fc2_client.py status        # Connection and auth status
python fc2_client.py discover      # Scan ONo object tree
python fc2_client.py monitor       # Live notification stream
python fc2_client.py interactive   # REPL (default)
```

**Interactive REPL commands:**

```
gain <ch> [dB]                     get or set input gain
phantom <ch> [0/1]                 get or set phantom power (+48 V)
air <ch> [0/1]                     get or set Air mode
mute <ch> [0/1]                    get or set input mute
raw <ono> <dl> <mi> [params_hex]   send raw AES70 command
monitor                            live event stream
discover                           scan ONo 0x1001–0x104F
quit
```

**Options:**

```
--name <n>              Device name shown in FC2 (default: iPad)
--auth <file>           Auth file path (default: fc2_auth.json)
--fc2-pubkey <64hex>    FC2 static Noise public key (32 bytes, hex)
```

Auth credentials (X25519 keypair + session token) are saved to `fc2_auth.json` after the first successful pairing. Subsequent runs reconnect automatically.

**Dependencies:** `pip install websockets cryptography`

---

## `audiobookshelf/` — AudioBookshelf helpers

### `abs_download_all.py` — Queue every episode of every podcast

Walks through every podcast library on an [AudioBookshelf](https://www.audiobookshelf.org/) server and triggers the same flow as the UI combo:

> **Look for new episodes after this date = 1970-01-01** + **Limit = 0** + **Check & Download New Episodes**

For each podcast the script performs two API calls:

1. `PATCH /api/items/{id}/media` with `{"lastEpisodeCheck": 0}` — reset the "last seen" marker so every feed episode counts as new.
2. `GET /api/podcasts/{id}/checknew?limit=0` — `limit=0` is the official value for *all episodes* (the default is 3). The server deduplicates against already-downloaded files by enclosure URL and starts background downloads.

**Configuration** (edit the constants at the top of the file):

```python
ABS_URL    = "http://localhost:13378"
API_TOKEN  = "PUT_YOUR_API_TOKEN_HERE"   # Settings -> Users -> click user -> API Token
VERIFY_SSL = True
```

**Usage:**

```
pip install requests
python abs_download_all.py
```

**Behaviour:**

- Podcasts without an RSS feed URL (e.g. local-only libraries) are skipped with a log line.
- Re-running the script after the queue has drained is safe — the server reports `queued 0 episodes` because enclosure URLs already exist on disk.
- Requires an admin-level API token (the `checknew` endpoint returns 403 for regular users).

**Dependencies:** `pip install requests`

---

### `mujrozhlas_rss_gen.py` — Full-archive RSS feed for mujRozhlas.cz

Generates RSS feeds for Czech Radio (mujRozhlas.cz) shows. The official feeds are capped at the 50 most recent episodes; this script paginates through the JSON API and produces a feed containing **every** episode of the selected show, with XML structure that matches the official feed 1:1 (same namespaces, same resize-variant cover image URL, same GUIDs, same promo block in descriptions).

**Configuration** (all via environment variables):

| Variable | Default | Purpose |
|---|---|---|
| `OUTPUT_DIR` | `/var/www/rss` | Directory where `<slug>.rss` files are written atomically |
| `SHOWS` | `DEFAULT_SHOWS` inside the script | JSON map of `{"slug": "show_uuid", ...}` |

**Usage:**

```
pip install requests

# Single show (the default bundled mapping)
python mujrozhlas_rss_gen.py

# Multiple shows, custom output directory
export OUTPUT_DIR=/srv/rss
export SHOWS='{"quest":"9f19fbeb-a3d2-3cfb-b04e-3e0a253b639a","vortex":"..."}'
python mujrozhlas_rss_gen.py

# Populate <enclosure length="..."> from HEAD requests (slow — one HEAD per episode)
python mujrozhlas_rss_gen.py --with-lengths
```

**Intended use:** run as a cron job producing static `*.rss` files served by nginx/Apache; consumers (Podcast Addict, AudioBookshelf, …) point at those URLs and see the complete back catalogue.

**Notes:**

- Output is written atomically (write to temp file, `os.replace`) so HTTP consumers never see a half-written feed.
- The script writes stderr logs listing each show processed, episode count, output path, and size.
- The `PROMO_SHOW` footer and `<copyright>Český rozhlas …</copyright>` line are kept in Czech on purpose — the generated feed is intended for Czech-speaking listeners and has to match the reference feed verbatim.

**Dependencies:** `pip install requests`

---

## `vsphere/` — ESXi SSH automation

Two siblings that automate SSH operations across a fleet of ESXi hosts. Both use the ESXi SOAP API (pyVmomi) to manage the SSH service and Paramiko to execute commands. Every action is logged to the console and a timestamped log file, and both scripts share identical CLI flags apart from how they discover hosts.

| Script | Host source |
|---|---|
| `vcenter_esxi_ssh.py` | Queries vCenter and discovers every registered host automatically |
| `esxi_direct_ssh.py` | Reads a flat list of IPs / FQDNs from `hosts.json` (no vCenter required) |

**Installation:**

```
pip install -r vsphere/requirements.txt
# or
pip install pyVmomi paramiko colorama
```

`colorama` is optional — if missing, console output still works, just without colours.

**Commands to run** are configured inside each script as a `COMMANDS_TO_RUN` Python list. Edit the list, save, run the script; the commands are executed sequentially on every host.

### `vcenter_esxi_ssh.py` — vCenter-discovered SSH automation

```
# Audit (dry-run first, always)
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local --dry-run
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
    --log-file audit.log

# Compliance check — guarantee SSH is disabled afterwards
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
    --ssh-user root --ssh-password RootP@ss --disable-ssh-after \
    --log-file compliance.log

# Target one cluster only
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
    --cluster "Cluster-Prod" --disable-ssh-after --verbose

# Emergency patch to a single host
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
    --host-name "esxi-prod-07" --disable-ssh-after --verbose

# Open/close SSH cluster-wide without running any commands
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local --ssh-only-enable
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local --ssh-only-disable
```

**Key options:**

| Group | Flag | Description |
|---|---|---|
| vCenter | `-s / --server` | vCenter hostname or IP (required) |
| vCenter | `-u / --user` | vCenter username (required) |
| vCenter | `-p / --password` | vCenter password (interactive prompt if omitted) |
| vCenter | `--port` | vCenter HTTPS port (default 443) |
| SSH | `--ssh-user` | SSH username on ESXi hosts (default: root) |
| SSH | `--ssh-password` | SSH password (defaults to `--password` if omitted) |
| SSH | `--ssh-port` | SSH port (default 22) |
| SSH | `--ssh-timeout` | SSH connection / command timeout in seconds (default 30) |
| Filtering | `--cluster` | Only hosts in clusters matching this substring |
| Filtering | `--host-name` | Only hosts whose name matches this substring |
| Filtering | `--skip-disconnected` | Skip disconnected / not-responding hosts (on by default) |
| SSH-only | `--ssh-only-enable` | Enable SSH everywhere and exit (no commands run) |
| SSH-only | `--ssh-only-disable` | Disable SSH everywhere and exit (no commands run) |
| Behaviour | `--disable-ssh-after` | Force SSH off after the run, even on hosts where it was already on |
| Behaviour | `--dry-run` | Simulate everything without changing state |
| Behaviour | `--verbose` | Print DEBUG-level output to console (always written to log file) |
| Behaviour | `--log-file` | Log file path (default `vcenter_esxi_ssh_YYYYMMDD_HHMMSS.log`) |
| Behaviour | `--no-log-file` | Disable the log file entirely |

### `esxi_direct_ssh.py` — Host-list SSH automation (no vCenter)

Identical behaviour, but the host list comes from a JSON file:

```json
[
  "192.168.10.11",
  "192.168.10.12",
  "esxi-03.corp.local",
  "esxi-04.corp.local"
]
```

```
# hosts.json in current directory, password prompted
python esxi_direct_ssh.py -u root --dry-run
python esxi_direct_ssh.py -u root --disable-ssh-after

# Custom host list location
python esxi_direct_ssh.py -u root --config /etc/esxi/prod_hosts.json

# Separate API vs SSH credentials
python esxi_direct_ssh.py -u root -p ApiPass --ssh-user root --ssh-password SshPass

# Filter by substring, open or close SSH fleet-wide
python esxi_direct_ssh.py -u root --host-name "192.168.10.11" --disable-ssh-after
python esxi_direct_ssh.py -u root --ssh-only-enable
python esxi_direct_ssh.py -u root --ssh-only-disable
```

All other flags (`--ssh-user`, `--ssh-password`, `--ssh-port`, `--ssh-timeout`, `--host-name`, `--dry-run`, `--verbose`, `--log-file`, `--no-log-file`) behave identically to `vcenter_esxi_ssh.py`. The only replacement is `--config / -c` instead of `-s / --server`.

### SSH disable logic (both scripts)

The SSH service state on each host is recorded before the script makes any changes:

- If SSH was **running** before: left running (unless `--disable-ssh-after` is set).
- If SSH was **stopped** before: restored to stopped after the run.
- If `--disable-ssh-after` is set: SSH is stopped afterwards unconditionally.

This prevents the script from changing the security posture of a cluster where some hosts intentionally keep SSH open.

### Compatibility

- Python 3.8+ — tested on 3.8 – 3.12
- pyVmomi 8.0.2+, paramiko 3.4+, colorama 0.4.6+
- vCenter 7.0+ (vCenter 8 tested) — only for `vcenter_esxi_ssh.py`
- ESXi 7.0+ (ESXi 8 and 9 tested)
- Windows 10/11, Linux, macOS

---

## Requirements summary

Python 3.10 or newer is required for the `str | None` union syntax used in most scripts. `dircomp.py` and the `vsphere/` scripts work from Python 3.8.

| Script | Extra packages |
|---|---|
| `disk_scan.py` | — |
| `dircomp.py` | — |
| `md2html.py` | `markdown` |
| `fc2_client.py` | `websockets`, `cryptography` |
| `audiobookshelf/abs_download_all.py` | `requests` |
| `audiobookshelf/mujrozhlas_rss_gen.py` | `requests` |
| `vsphere/vcenter_esxi_ssh.py` | `pyVmomi`, `paramiko`, `colorama` *(optional)* |
| `vsphere/esxi_direct_ssh.py` | `pyVmomi`, `paramiko`, `colorama` *(optional)* |

---

## License

MIT
