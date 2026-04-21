# Utils

A collection of independent Python utilities. Each script is standalone -- no shared dependencies, no framework. Pick what you need.

Repository layout:

```
Utils/
|-- disk_scan.py               USB disk duplicate finder
|-- dircomp.py                 SHA-256 directory diff
|-- md2html.py                 Markdown to retro-terminal HTML
|-- fc2_client.py              Focusrite Control 2 WebSocket client
|-- audiobookshelf/
|   |-- abs_download_all.py        bulk-queue every episode of every podcast
|   |-- mujrozhlas_rss_gen.py      full-archive RSS generator for mujRozhlas.cz
|   |-- modrak_archive.py          offline archive of the Modrak Opinio podcast
|   +-- modrak_rss_gen.py          extended RSS feed for the Modrak Podbean podcast
+-- vsphere/
    |-- vcenter_esxi_ssh.py        run commands on all ESXi hosts via vCenter
    |-- esxi_direct_ssh.py         same, but from a JSON host list (no vCenter)
    |-- generate_hosts_config.py   generate VCF host commissioning JSON from hosts.json
    |-- hosts.json                 sample host list
    +-- requirements.txt           vSphere-specific dependencies
```

---

## Root scripts

### `disk_scan.py` -- USB disk duplicate finder

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

CLI output is Windows 10 cmd.exe compatible (ASCII-only, UTF-8 reconfigured on startup).

**Dependencies:** stdlib only (Python 3.10+)

---

### `dircomp.py` -- Directory comparison by SHA-256

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

SHA-256 is computed in 1 MB chunks (memory-friendly on large trees). ANSI colour output is enabled automatically on Windows 10 (1511+). Exit code `0` = directories are identical, `1` = differences found -- useful in CI pipelines.

**Options:**

```
--ignore PATTERN    Glob pattern to skip files/dirs (repeatable)
--show-identical    Also list files that match
--output FILE       Save plain-text report to file
```

**Dependencies:** stdlib only (Python 3.8+)

---

### `md2html.py` -- Markdown to retro terminal HTML

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
| `green` | Default -- green phosphor |
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
status: CPU . RAM
-->
```

**Dependencies:** `pip install markdown`

---

### `fc2_client.py` -- Focusrite Control 2 WebSocket client

Python client for controlling Scarlett audio interfaces via the Focusrite Control 2 (FC2) WebSocket API. Implements AES70/OCP.1 over two WebSocket channels:

- **Port 58323** -- authentication channel (pairing, `RequestApproval`)
- **Port 58322** -- control channel (gain, phantom power, notifications), encrypted with Noise_NK_25519_AESGCM_SHA256

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
discover                           scan ONo 0x1001-0x104F
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

## `audiobookshelf/` -- AudioBookshelf helpers

### `abs_download_all.py` -- Queue every episode of every podcast

Walks through every podcast library on an [AudioBookshelf](https://www.audiobookshelf.org/) server and triggers the same flow as the UI combo:

> **Look for new episodes after this date = 1970-01-01** + **Limit = 0** + **Check & Download New Episodes**

For each podcast the script performs two API calls:

1. `PATCH /api/items/{id}/media` with `{"lastEpisodeCheck": 0}` -- reset the "last seen" marker so every feed episode counts as new.
2. `GET /api/podcasts/{id}/checknew?limit=0` -- `limit=0` is the official value for *all episodes* (the default is 3). The server deduplicates against already-downloaded files by enclosure URL and starts background downloads.

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
- Re-running the script after the queue has drained is safe -- the server reports `queued 0 episodes` because enclosure URLs already exist on disk.
- Requires an admin-level API token (the `checknew` endpoint returns 403 for regular users).

**Dependencies:** `pip install requests`

---

### `mujrozhlas_rss_gen.py` -- Full-archive RSS feed for mujRozhlas.cz

Generates RSS feeds for Czech Radio (mujRozhlas.cz) shows. The official feeds are capped at the 50 most recent episodes; this script paginates through the JSON API and produces a feed containing every episode of the selected show, with XML structure that matches the official feed 1:1 (same namespaces, same resize-variant cover image URL, same GUIDs, same promo block in descriptions).

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

# Populate <enclosure length="..."> from HEAD requests (slow -- one HEAD per episode)
python mujrozhlas_rss_gen.py --with-lengths
```

Intended use: run as a cron job producing static `*.rss` files served by nginx/Apache. Consumers (Podcast Addict, AudioBookshelf, ...) point at those URLs and see the complete back catalogue. Output is written atomically (`write to temp file` + `os.replace`) so HTTP consumers never see a half-written feed.

**Dependencies:** `pip install requests`

---

### `modrak_archive.py` -- Offline archive of the Modrak Opinio podcast

Creates and maintains a full offline copy of the paid Modrak Opinio podcast. Downloads every MP3/M4A, cover image, and generates a local RSS feed in which all `<enclosure>` URLs point to local files instead of Opinio's CDN servers.

When a subscription lapses, the hosted feed stops working. This archive preserves a fully functional local copy of every episode that was ever released during the subscription.

The script is idempotent -- re-running only downloads episodes that are new or changed; everything already archived is skipped.

**Archive layout:**

```
ARCHIVE_DIR/
|-- media/
|   |-- 417-vypraveni-pribehu.m4a
|   +-- 416-arena-32-....m4a
|-- cover.jpg               channel image
|-- feed-opinio.xml         ORIGINAL Opinio feed (contains your token -- chmod 600)
+-- modrak-archive.rss      LOCAL feed with local URLs (safe to expose via HTTP)
```

**Configuration** (environment variables or constants at the top of the file):

| Variable | Default | Purpose |
|---|---|---|
| `OPINIO_URL` | Opinio RSS URL skeleton | Paid RSS URL including your `player_key` |
| `ARCHIVE_DIR` | `/var/www/rss/modrak-archive` | Local storage directory |
| `PUBLIC_BASE_URL` | `http://localhost/modrak-archive` | URL prefix used in the generated local feed |

**Usage:**

```
pip install requests
python modrak_archive.py

# Typical cron line:
# 0 6 * * * /usr/bin/python3 /opt/modrak-archive/modrak_archive.py
```

**Security note:** `feed-opinio.xml` contains your `player_key` in every enclosure URL. It is written with mode `0600` and must not be exposed via a web server. The generated `modrak-archive.rss` is clean of tokens and safe to serve publicly.

**Dependencies:** `pip install requests`

---

### `modrak_rss_gen.py` -- Extended RSS feed for the Modrak Podbean podcast

Fetches the official Podbean feed for the Modrak podcast and extends it with older episodes that have fallen off the feed (Podbean serves only the ~300 most recent episodes). The merged complete RSS feed is written to `OUTPUT_DIR`. Intended for cron.

The original XML channel metadata and all existing `<item>` elements are kept verbatim. Additional `<item>` elements for older scraped episodes are inserted before `</channel>` using the same item format.

**Configuration** (environment variables):

| Variable | Default | Purpose |
|---|---|---|
| `OUTPUT_DIR` | `/var/www/rss` | Directory where `*.rss` files are written atomically |
| `SHOWS` | `DEFAULT_SHOWS` inside the script | JSON map of `{"slug": "podbean-subdomain"}` |

**Usage:**

```
pip install requests
python modrak_rss_gen.py

# Typical cron line:
# 0 5 * * * /usr/bin/python3 /opt/modrak-rss/modrak_rss_gen.py
```

Output is written atomically (`write to temp file` + `os.replace`) so nginx never serves a partial or corrupted feed.

**Dependencies:** `pip install requests`

---

## `vsphere/` -- ESXi SSH automation

Three scripts for automating SSH operations and host commissioning across ESXi fleets. The SSH scripts use the ESXi SOAP API (pyVmomi) to manage the SSH service and Paramiko to execute commands over SSH. Every action is logged to both the console and a timestamped log file. All console output is ASCII-only and compatible with Windows 10 CMD and PowerShell without any code page changes.

**Installation:**

```
pip install -r vsphere/requirements.txt
# or
pip install pyVmomi paramiko colorama
```

`colorama` is optional -- if missing, console output still works without colours.

**`hosts.json` format** (shared by all three scripts):

```json
[
  "esx-01a.site-a.vcf.lab",
  "esx-02a.site-a.vcf.lab",
  "esxi-03.corp.local",
  "192.168.10.14"
]
```

A plain JSON array of strings. IP addresses and FQDNs can be mixed freely. When `--change-hostname` is used with `esxi_direct_ssh.py`, all entries must be valid FQDNs (at least three dot-separated labels; IP addresses and short names are rejected before any host is contacted).

---

### `vcenter_esxi_ssh.py` -- vCenter-discovered SSH automation

Connects to a vCenter instance, enumerates all registered ESXi hosts, and runs a configurable set of SSH commands on each one.

**Commands to run** are defined as a Python list near the top of the file:

```python
COMMANDS_TO_RUN = [
    "localcli --plugin-dir=/usr/lib/vmware/esxcli/int sched group getmemconfig "
        "-g host/vim/vmvisor/settingsd-task-forks",
    "localcli --plugin-dir=/usr/lib/vmware/esxcli/int sched group setmemconfig "
        "-g host/vim/vmvisor/settingsd-task-forks -m 400 -i 0 -l -1 -u mb",
    "localcli --plugin-dir=/usr/lib/vmware/esxcli/int sched group getmemconfig "
        "-g host/vim/vmvisor/settingsd-task-forks",
]
```

Commands are executed sequentially on each host. A non-zero exit code is logged as a warning but does not abort the remaining commands or skip the host.

**Per-host execution order:**

```
1. Enable SSH via the vCenter API  (if not already running)
2. Connect via SSH and run every command in COMMANDS_TO_RUN
3. Disable SSH  (if it was stopped before the run, or --disable-ssh-after is set)
```

**Usage:**

```
# Dry-run first -- preview without changing anything
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local --dry-run

# Run for real -- disable SSH afterwards
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
    --ssh-user root --disable-ssh-after --log-file audit.log

# Target one cluster only
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
    --cluster "Cluster-Prod" --disable-ssh-after --verbose

# Target a single host
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
    --host-name "esxi-prod-07" --disable-ssh-after --verbose

# Enable SSH cluster-wide without running any commands
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
    --cluster "Cluster-Prod" --ssh-only-enable

# Disable SSH cluster-wide without running any commands
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
    --cluster "Cluster-Prod" --ssh-only-disable
```

Passwords are prompted interactively when omitted from the command line. The vCenter password and the SSH password are prompted separately; pressing Enter at the SSH password prompt reuses the vCenter password.

**All options:**

| Group | Flag | Default | Description |
|---|---|---|---|
| vCenter | `-s / --server` | required | vCenter hostname or IP address |
| vCenter | `-u / --user` | required | vCenter username (e.g. `administrator@vsphere.local`) |
| vCenter | `-p / --password` | prompted | vCenter password |
| vCenter | `--port` | `443` | vCenter HTTPS port |
| SSH | `--ssh-user` | `root` | SSH username on ESXi hosts |
| SSH | `--ssh-password` | prompted | SSH password (Enter to reuse vCenter password) |
| SSH | `--ssh-port` | `22` | SSH port on ESXi hosts |
| SSH | `--ssh-timeout` | `30` | SSH connection and command timeout in seconds |
| Filtering | `--cluster` | all | Only hosts in clusters whose name contains this substring (case-insensitive) |
| Filtering | `--host-name` | all | Only hosts whose registered name contains this substring (case-insensitive) |
| Filtering | `--skip-disconnected` | on | Skip hosts in `disconnected` or `notResponding` state |
| SSH-only | `--ssh-only-enable` | off | Enable SSH on every matched host and exit (no commands run) |
| SSH-only | `--ssh-only-disable` | off | Disable SSH on every matched host and exit (no commands run) |
| Behaviour | `--disable-ssh-after` | off | Disable SSH after the run even if it was already on before |
| Behaviour | `--dry-run` | off | Simulate all actions without making any changes |
| Behaviour | `--verbose` | off | Print DEBUG-level output to the console |
| Behaviour | `--log-file` | `vcenter_esxi_ssh_YYYYMMDD_HHMMSS.log` | Log file path |
| Behaviour | `--no-log-file` | off | Disable log file, write to console only |

**Flag compatibility:**

| Combination | Result |
|---|---|
| `--ssh-only-enable` + `--ssh-only-disable` | Rejected (mutually exclusive) |
| `--ssh-only-enable` + `--disable-ssh-after` | Rejected |
| `--ssh-only-disable` + `--disable-ssh-after` | Rejected |

---

### `esxi_direct_ssh.py` -- Host-list SSH automation (no vCenter)

Identical behaviour to `vcenter_esxi_ssh.py`, but the host list comes from `hosts.json` instead of being discovered via vCenter. Each host is contacted directly via its own built-in ESXi SOAP API -- no vCenter instance is required.

The script connects to two separate endpoints on each host:

- **ESXi SOAP API** (port 443) -- used only to start and stop the SSH service.
- **SSH** (port 22) -- used to execute `COMMANDS_TO_RUN`.

These two connections use the same credentials by default. Separate usernames and passwords can be provided when they differ.

**Per-host execution order:**

```
1. Connect to ESXi SOAP API
2. Enable SSH  (if not already running)
3. [if --change-hostname]  esxcli system hostname set --fqdn=<host>
4. Run every command in COMMANDS_TO_RUN over SSH
5. Disable SSH  (if it was stopped before the run, or --disable-ssh-after is set)
6. Disconnect from SOAP API
```

**Usage:**

```
# Dry-run (hosts.json in the current directory)
python esxi_direct_ssh.py -u root --dry-run

# Run commands and disable SSH afterwards
python esxi_direct_ssh.py -u root --disable-ssh-after

# Separate ESXi API password and SSH password
python esxi_direct_ssh.py -u root -p ApiPass --ssh-user root --ssh-password SshPass

# Enable SSH on all hosts (no commands run)
python esxi_direct_ssh.py -u root --ssh-only-enable

# Disable SSH on all hosts (no commands run)
python esxi_direct_ssh.py -u root --ssh-only-disable

# Set hostname on each host then run COMMANDS_TO_RUN
# hosts.json must contain valid FQDNs -- IPs and short names are rejected
python esxi_direct_ssh.py -u root --change-hostname --disable-ssh-after

# Dry-run with --change-hostname validates FQDNs without touching any host
python esxi_direct_ssh.py -u root --change-hostname --dry-run

# Use a different hosts file
python esxi_direct_ssh.py -u root --config /etc/esxi/prod_hosts.json

# Filter by substring
python esxi_direct_ssh.py -u root --host-name "esx-01a" --disable-ssh-after
```

Passwords are prompted interactively when omitted. The ESXi API password and SSH password are prompted separately; pressing Enter at the SSH password prompt reuses the API password.

**All options:**

| Group | Flag | Default | Description |
|---|---|---|---|
| Host list | `-c / --config` | `hosts.json` | Path to the JSON host list file |
| ESXi API | `-u / --user` | required | ESXi username (typically `root`) |
| ESXi API | `-p / --password` | prompted | ESXi SOAP API password |
| ESXi API | `--port` | `443` | ESXi HTTPS API port |
| SSH | `--ssh-user` | `root` | SSH username on ESXi hosts |
| SSH | `--ssh-password` | prompted | SSH password (Enter to reuse API password) |
| SSH | `--ssh-port` | `22` | SSH port |
| SSH | `--ssh-timeout` | `30` | SSH connection and command timeout in seconds |
| Filtering | `--host-name` | all | Only hosts whose IP or FQDN contains this substring (case-insensitive) |
| SSH-only | `--ssh-only-enable` | off | Enable SSH on every matched host and exit (no commands run) |
| SSH-only | `--ssh-only-disable` | off | Disable SSH on every matched host and exit (no commands run) |
| Behaviour | `--change-hostname` | off | Set FQDN hostname before running commands. Requires all hosts.json entries to be valid FQDNs (min. 3 labels: host.domain.tld). Validated even in --dry-run. |
| Behaviour | `--disable-ssh-after` | off | Disable SSH after the run even if it was already on before |
| Behaviour | `--dry-run` | off | Simulate all actions without making any changes |
| Behaviour | `--verbose` | off | Print DEBUG-level output to the console |
| Behaviour | `--log-file` | `esxi_direct_ssh_YYYYMMDD_HHMMSS.log` | Log file path |
| Behaviour | `--no-log-file` | off | Disable log file, write to console only |

**Flag compatibility:**

| Combination | Result |
|---|---|
| `--ssh-only-enable` + `--ssh-only-disable` | Rejected (mutually exclusive) |
| `--ssh-only-enable` + `--disable-ssh-after` | Rejected |
| `--ssh-only-disable` + `--disable-ssh-after` | Rejected |
| `--change-hostname` + `--ssh-only-enable` | Rejected (no SSH session opened in ssh-only modes) |
| `--change-hostname` + `--ssh-only-disable` | Rejected |

**FQDN validation for `--change-hostname`:**

Validation runs before any host is contacted, including during `--dry-run`. The script rejects entries that are IP addresses, single-label names, or names with only two labels:

```
[X]  192.168.10.11   (IP address -- use a fully-qualified hostname)
[X]  esxi-hostname   (no domain part -- add the domain suffix)
[X]  esx-01.site-a  (only 2 labels -- need at least host.domain.tld)
[OK] esx-01.site-a.vcf.lab
[OK] esxi-03.corp.local
```

---

### `generate_hosts_config.py` -- VCF host commissioning JSON generator

Reads the same `hosts.json` file used by the SSH scripts and generates a host commissioning JSON file in the format expected by VMware Cloud Foundation (VCF). The same parameters (username, password, network pool name, storage type) are applied to every host.

VCF accepts a maximum of 50 hosts per commissioning operation. When `hosts.json` contains more than 50 entries the output is automatically split into multiple files with at most 50 hosts each. A zero-padded `_partNN` suffix is inserted before the file extension (e.g. `hosts_config.json` becomes `hosts_config_part01.json`, `hosts_config_part02.json`, ...).

**Usage:**

```
# Run with defaults (reads ./hosts.json, writes ./hosts_config.json)
python generate_hosts_config.py

# Custom input and output paths
python generate_hosts_config.py -i my_hosts.json -o my_output.json
```

The script prompts interactively for all parameters. The password is entered in hidden mode and must be confirmed. Storage type is selected from a numbered menu.

**Supported storage types:**

```
VSAN  /  VSAN_REMOTE  /  VSAN_ESA  /  VSAN_MAX  /  NFS  /  VMFS_FC  /  VVOL
```

When `VVOL` is selected an additional menu appears for the vVol storage protocol type (`VMFS_FC`, `ISCSI`, or `NFS`) and the `vvolStorageProtocolType` field is included in the output. For all other storage types the field is omitted.

**Output format (non-VVOL example):**

```json
{
    "hosts": [
        {
            "fqdn": "esx-01a.site-a.vcf.lab",
            "username": "root",
            "storageType": "VSAN",
            "password": "...",
            "networkPoolName": "sfo-m01-np01"
        }
    ]
}
```

**Options:**

```
-i / --input    Path to input hosts.json  (default: hosts.json next to the script)
-o / --output   Path to output file        (default: hosts_config.json)
```

**Exit codes:**

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Input file not found or invalid JSON |
| `2` | Invalid user input or user aborted (Ctrl+C) |

**Dependencies:** stdlib only (Python 3.6+)

---

### SSH disable logic (vcenter_esxi_ssh.py and esxi_direct_ssh.py)

The SSH service state on each host is recorded before the script makes any changes:

| SSH state before run | `--disable-ssh-after` set | SSH state after run |
|---|---|---|
| Stopped | No | Stopped -- script turns it on, turns it back off |
| Stopped | Yes | Stopped |
| Running | No | Running -- left as found |
| Running | Yes | Stopped |

This prevents the scripts from altering the security posture of hosts where SSH is intentionally left on. If the script is interrupted mid-run (`Ctrl+C`, power loss), some hosts may be left with SSH enabled. Run `--ssh-only-disable` afterwards to restore the expected state.

---

### Compatibility

- Python 3.8+ -- tested on 3.8 -- 3.12
- pyVmomi 8.0.2+, paramiko 3.4+, colorama 0.4.6+
- vCenter 7.0+ (vCenter 8 tested) -- required only for `vcenter_esxi_ssh.py`
- ESXi 7.0+ (ESXi 8 and 9 tested)
- Windows 10/11, Linux, macOS
- All console output is ASCII-only (compatible with Windows 10 CMD and PowerShell without `chcp 65001`)

---

## Requirements summary

Python 3.10 or newer is required for the `str | None` union syntax used in most scripts. `dircomp.py` and the `vsphere/` scripts work from Python 3.8. `generate_hosts_config.py` works from Python 3.6.

| Script | Extra packages |
|---|---|
| `disk_scan.py` | -- |
| `dircomp.py` | -- |
| `md2html.py` | `markdown` |
| `fc2_client.py` | `websockets`, `cryptography` |
| `audiobookshelf/abs_download_all.py` | `requests` |
| `audiobookshelf/mujrozhlas_rss_gen.py` | `requests` |
| `audiobookshelf/modrak_archive.py` | `requests` |
| `audiobookshelf/modrak_rss_gen.py` | `requests` |
| `vsphere/vcenter_esxi_ssh.py` | `pyVmomi`, `paramiko`, `colorama` (optional) |
| `vsphere/esxi_direct_ssh.py` | `pyVmomi`, `paramiko`, `colorama` (optional) |
| `vsphere/generate_hosts_config.py` | -- |

---

## License

MIT
