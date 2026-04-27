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
    |-- vcenter_esxi_ssh.py                run commands on all ESXi hosts via vCenter
    |-- esxi_direct_ssh.py                 same, but from a JSON host list (no vCenter)
    |-- vcenter_rename_local_datastores.py rename local datastores across clusters
    |-- vcenter_export_tpm_keys.py         export TPM encryption recovery keys
    |-- generate_hosts_config.py           generate VCF host commissioning JSON
    |-- hosts.json                         sample host list
    +-- requirements.txt                   vSphere-specific dependencies
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

Intended use: run as a cron job producing static `*.rss` files served by nginx/Apache. Output is written atomically (`write to temp file` + `os.replace`) so HTTP consumers never see a half-written feed.

**Dependencies:** `pip install requests`

---

### `modrak_archive.py` -- Offline archive of the Modrak Opinio podcast

Creates and maintains a full offline copy of the paid Modrak Opinio podcast. Downloads every MP3/M4A, cover image, and generates a local RSS feed in which all `<enclosure>` URLs point to local files instead of Opinio's CDN servers.

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
```

**Security note:** `feed-opinio.xml` contains your `player_key` in every enclosure URL. It is written with mode `0600` and must not be exposed via a web server.

**Dependencies:** `pip install requests`

---

### `modrak_rss_gen.py` -- Extended RSS feed for the Modrak Podbean podcast

Fetches the official Podbean feed for the Modrak podcast and extends it with older episodes that have fallen off the feed (Podbean serves only the ~300 most recent episodes). The merged complete RSS feed is written to `OUTPUT_DIR`. Intended for cron.

**Configuration** (environment variables):

| Variable | Default | Purpose |
|---|---|---|
| `OUTPUT_DIR` | `/var/www/rss` | Directory where `*.rss` files are written atomically |
| `SHOWS` | `DEFAULT_SHOWS` inside the script | JSON map of `{"slug": "podbean-subdomain"}` |

**Usage:**

```
pip install requests
python modrak_rss_gen.py
```

**Dependencies:** `pip install requests`

---

## `vsphere/` -- ESXi automation scripts

Five scripts for automating SSH operations, host commissioning, datastore management, and TPM recovery key export across ESXi fleets. All console output is ASCII-only and compatible with Windows 10 CMD and PowerShell without any code page changes. Passwords are never echoed to the terminal.

**Installation:**

```
pip install -r vsphere/requirements.txt
# or
pip install pyVmomi paramiko colorama
```

`colorama` is optional -- if missing, console output still works without colours.

**`hosts.json` format** (shared by the direct-SSH and generate scripts):

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

**Per-host execution order:**

```
1. Enable SSH via the vCenter API  (if not already running)
2. Connect via SSH and run every command in COMMANDS_TO_RUN
3. Disable SSH  (if it was stopped before the run, or --disable-ssh-after is set)
```

**Usage:**

```
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local --dry-run
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
    --ssh-user root --disable-ssh-after --log-file audit.log
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
    --cluster "Cluster-Prod" --ssh-only-enable
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
    --cluster "Cluster-Prod" --ssh-only-disable
```

**All options:**

| Group | Flag | Default | Description |
|---|---|---|---|
| vCenter | `-s / --server` | required | vCenter hostname or IP address |
| vCenter | `-u / --user` | required | vCenter username (e.g. `administrator@vsphere.local`) |
| vCenter | `-p / --password` | prompted | vCenter password. Never echoed to the terminal. |
| vCenter | `--port` | `443` | vCenter HTTPS port |
| SSH | `--ssh-user` | `root` | SSH username on ESXi hosts |
| SSH | `--ssh-password` | prompted | SSH password. Press Enter to reuse the vCenter password. |
| SSH | `--ssh-port` | `22` | SSH port on ESXi hosts |
| SSH | `--ssh-timeout` | `30` | SSH connection and command timeout in seconds |
| Filtering | `--cluster` | all | Only hosts in clusters whose name contains this substring (case-insensitive) |
| Filtering | `--host-name` | all | Only hosts whose registered name contains this substring (case-insensitive) |
| Filtering | `--skip-disconnected` | on | Skip hosts in `disconnected` or `notResponding` state |
| SSH-only | `--ssh-only-enable` | off | Enable SSH on every matched host and exit. No commands are run. |
| SSH-only | `--ssh-only-disable` | off | Disable SSH on every matched host and exit. No commands are run. |
| Behaviour | `--disable-ssh-after` | off | Disable SSH after the run even if SSH was already on before the script started |
| Behaviour | `--dry-run` | off | Simulate all actions without making any changes |
| Behaviour | `--verbose` | off | Print DEBUG-level output to the console |
| Behaviour | `--log-file` | `vcenter_esxi_ssh_YYYYMMDD_HHMMSS.log` | Log file path. Directory is created automatically. |
| Behaviour | `--no-log-file` | off | Disable log file, write to console only |

---

### `esxi_direct_ssh.py` -- Host-list SSH automation (no vCenter)

Identical behaviour to `vcenter_esxi_ssh.py` but reads hosts from `hosts.json` instead of vCenter. Each host is contacted directly via its built-in ESXi SOAP API.

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
python esxi_direct_ssh.py -u root --dry-run
python esxi_direct_ssh.py -u root --disable-ssh-after
python esxi_direct_ssh.py -u root --ssh-only-enable
python esxi_direct_ssh.py -u root --ssh-only-disable
python esxi_direct_ssh.py -u root --change-hostname --disable-ssh-after
python esxi_direct_ssh.py -u root --config /etc/esxi/prod_hosts.json
python esxi_direct_ssh.py -u root --host-name "esx-01a" --disable-ssh-after
```

**All options:**

| Group | Flag | Default | Description |
|---|---|---|---|
| Host list | `-c / --config` | `hosts.json` | Path to the JSON host list file |
| ESXi API | `-u / --user` | required | ESXi username (typically `root`) |
| ESXi API | `-p / --password` | prompted | ESXi SOAP API password. Never echoed to the terminal. |
| ESXi API | `--port` | `443` | ESXi HTTPS API port |
| SSH | `--ssh-user` | `root` | SSH username on ESXi hosts |
| SSH | `--ssh-password` | prompted | SSH password. Press Enter to reuse the API password. |
| SSH | `--ssh-port` | `22` | SSH port |
| SSH | `--ssh-timeout` | `30` | SSH connection and command timeout in seconds |
| Filtering | `--host-name` | all | Only hosts whose IP or FQDN contains this substring (case-insensitive) |
| SSH-only | `--ssh-only-enable` | off | Enable SSH on every matched host and exit. No commands are run. |
| SSH-only | `--ssh-only-disable` | off | Disable SSH on every matched host and exit. No commands are run. |
| Behaviour | `--change-hostname` | off | Set FQDN hostname before running commands. All hosts.json entries must be valid FQDNs (min. 3 labels: host.domain.tld). Validated even in dry-run. |
| Behaviour | `--disable-ssh-after` | off | Disable SSH after the run even if SSH was already on before the script started |
| Behaviour | `--dry-run` | off | Simulate all actions without making any changes |
| Behaviour | `--verbose` | off | Print DEBUG-level output to the console |
| Behaviour | `--log-file` | `esxi_direct_ssh_YYYYMMDD_HHMMSS.log` | Log file path. Directory is created automatically. |
| Behaviour | `--no-log-file` | off | Disable log file, write to console only |

FQDN validation for `--change-hostname`:

```
[X]  192.168.10.11   IP address
[X]  esxi-hostname   no domain suffix
[X]  esx-01.site-a   only 2 labels -- need at least host.domain.tld
[OK] esx-01.site-a.vcf.lab
[OK] esxi-03.corp.local
```

---

### `vcenter_rename_local_datastores.py` -- Local datastore rename

Renames local VMFS datastores on ESXi hosts across one or more vCenter instances. Supports Enhanced Linked Mode (ELM) environments by accepting multiple `--server` flags and processing each vCenter in sequence with a single set of SSO credentials.

A datastore is treated as **local** when it is mounted by exactly one host and its type is VMFS. This matches the default `datastore1`, `datastore1 (1)`, `datastore1 (2)`, ... naming that ESXi assigns during installation.

**Recommended workflow:**

```
# Step 1 -- inventory: see what is there without changing anything
python vcenter_rename_local_datastores.py \
    -s vcenter.corp.local -u administrator@vsphere.local --list-only

# Step 2 -- dry-run: confirm old and new names side by side
python vcenter_rename_local_datastores.py \
    -s vcenter.corp.local -u administrator@vsphere.local \
    --cluster "Cluster-Prod" --dry-run

# Step 3 -- rename for real
python vcenter_rename_local_datastores.py \
    -s vcenter.corp.local -u administrator@vsphere.local \
    --cluster "Cluster-Prod" --log-file C:\Logs\ds_rename.log
```

**Naming pattern** (`--pattern`, default: `{shortname}-local`):

| Host registered in vCenter | Resolved datastore name |
|---|---|
| `esx01-15.domain.local` | `esx01-15-local` |
| `esx15.domain.local` | `esx15-local` |
| `testesx.domain2.local` | `testesx-local` |

Available placeholders:

| Placeholder | Resolved to | Example |
|---|---|---|
| `{hostname}` | Full hostname as registered in vCenter | `esx-01a.site-a.vcf.lab` |
| `{shortname}` | First label before the first dot | `esx-01a` |
| `{cluster}` | Cluster name verbatim | `Cluster Prod A` |
| `{cluster_slug}` | Cluster name lowercased, non-alphanumeric chars replaced by `-` | `cluster-prod-a` |
| `{vcenter}` | vCenter hostname used for this connection | `vc-mgmt.corp.local` |
| `{index}` | 1-based 2-digit counter; empty string when only one local datastore exists on the host, prepended with `-` otherwise | (empty) or `-02` |
| `{index!}` | Same counter, always shown, no leading dash | `01` or `02` |

Pattern examples:

```
{shortname}-local             ->  esx-01a-local       /  esx-01a-local-02
{shortname}-ds{index!}        ->  esx-01a-ds01        /  esx-01a-ds02
{cluster_slug}-{shortname}    ->  cluster-prod-a-esx-01a
```

**Usage:**

```
# Inventory -- read-only, nothing changed
python vcenter_rename_local_datastores.py \
    -s vcenter.corp.local -u administrator@vsphere.local --list-only

# Dry-run -- show current and target names
python vcenter_rename_local_datastores.py \
    -s vcenter.corp.local -u administrator@vsphere.local --dry-run

# Rename all local datastores with default pattern
python vcenter_rename_local_datastores.py \
    -s vcenter.corp.local -u administrator@vsphere.local

# Only specific clusters (--cluster is repeatable)
python vcenter_rename_local_datastores.py \
    -s vcenter.corp.local -u administrator@vsphere.local \
    --cluster "Cluster-Prod" --cluster "Cluster-Dev"

# Custom naming pattern
python vcenter_rename_local_datastores.py \
    -s vcenter.corp.local -u administrator@vsphere.local \
    --pattern "{shortname}-ds{index!}"

# Multiple vCenters -- Enhanced Linked Mode (one SSO password for all)
python vcenter_rename_local_datastores.py \
    -s vc-site-a.corp.local \
    -s vc-site-b.corp.local \
    -u administrator@vsphere.local \
    --cluster "Cluster-Prod" --dry-run

# Skip datastores that already have the correct name
python vcenter_rename_local_datastores.py \
    -s vcenter.corp.local -u administrator@vsphere.local \
    --skip-already-named
```

**All options:**

| Group | Flag | Default | Description |
|---|---|---|---|
| vCenter | `-s / --server` | required (repeatable) | vCenter hostname or IP. Repeat for multiple vCenters: `-s vc1 -s vc2`. In ELM environments all vCenters share one SSO domain so the same credentials apply everywhere. |
| vCenter | `-u / --user` | required | vCenter / SSO username |
| vCenter | `-p / --password` | prompted | Password. Never echoed to the terminal. Used for all specified vCenters. |
| vCenter | `--port` | `443` | vCenter HTTPS port |
| Filtering | `-c / --cluster` | all (repeatable) | Only process clusters whose name contains this substring (case-insensitive). Repeat for multiple clusters: `--cluster Prod --cluster Dev`. |
| Filtering | `--host-name` | all | Only process hosts whose name contains this substring (case-insensitive) |
| Naming | `--pattern` | `{shortname}-local` | Naming pattern with placeholders. See table above. |
| Behaviour | `--list-only` | off | Print all local datastores with capacity and free space. No renames are performed. |
| Behaviour | `--skip-already-named` | off | Skip datastores whose current name already matches the resolved target name |
| Behaviour | `--include-nfs` | off | Include single-host NFS datastores in addition to VMFS (default: VMFS only) |
| Behaviour | `--dry-run` | off | Show what would be renamed without making any changes |
| Behaviour | `--verbose` | off | Print DEBUG-level output to the console |
| Behaviour | `--log-file` | `vcenter_rename_datastores_YYYYMMDD_HHMMSS.log` | Log file path. Directory is created automatically. |
| Behaviour | `--no-log-file` | off | Disable log file, write to console only |
| Behaviour | `--task-timeout` | `60` | Seconds to wait for each vCenter rename task before treating it as failed |

**Conflict detection:** before each rename the script checks whether the target name already exists anywhere in the same vCenter. If it does, the rename is skipped with a `[CONFLICT]` status. Names created during the current run are tracked in memory so back-to-back renames are also covered.

**Exit codes:**

| Code | Meaning |
|---|---|
| `0` | All processed datastores completed successfully (skipped and conflict entries do not count as failures) |
| `1` | One or more rename tasks failed |

**Dependencies:** `pip install pyVmomi colorama`

---


### `vcenter_export_tpm_keys.py` -- TPM encryption recovery key export

Connects to one or more vCenter instances, discovers ESXi hosts (with optional
cluster filtering), enables SSH on each host via the vCenter API, runs the
relevant `esxcli` commands to collect TPM state and encryption recovery keys,
then disables SSH again. Supports Enhanced Linked Mode (ELM) by accepting
multiple `--server` flags.

**Commands run on each host via SSH:**

| Command | Data collected |
|---|---|
| `esxcli system settings encryption get` | Encryption mode (TPM / None), Secure Boot requirement |
| `esxcli system settings encryption recovery list` | Recovery ID and full recovery key string |
| `esxcli hardware trustedboot get` | TPM presence, version (1.2 / 2.0), Secure Boot state |

**Output formats** (any combination, active simultaneously):

- **CLI** -- always printed; colour-coded summary table and per-host detail
- **HTML** (`--html`) -- self-contained dark-theme report with expandable cards and raw command output
- **TXT** (`--txt`) -- compact cluster-grouped plain-text file focused on the recovery keys

**TXT file format:**

```
================================================================================
Cluster          : CLUSTER-MGMT
 HOST             : nsx01n.corp.local
 ID  : {AB3F3271-05E6-4A7E-A91F-527E49F6DEF3}
 KEY : 672595-512392-589338-241376-619117-509184-009686-393576-...
 HOST             : nsx02n.corp.local
 ID  : {CD4A1382-16F7-5B8F-B02G-638F60G7EFG4}
 KEY : 112233-445566-778899-001122-334455-667788-990011-223344-...
================================================================================
```

**Usage:**

```
# CLI output only -- all clusters
python vcenter_export_tpm_keys.py -s vcenter.corp.local -u admin@vsphere.local

# HTML and TXT with auto-generated timestamped filenames in current directory
python vcenter_export_tpm_keys.py -s vcenter.corp.local -u admin@vsphere.local     --html --txt

# HTML and TXT with explicit paths
python vcenter_export_tpm_keys.py -s vcenter.corp.local -u admin@vsphere.local     --html C:\Reports\tpm.html --txt C:\Reports\tpm.txt

# Specific cluster(s)
python vcenter_export_tpm_keys.py -s vcenter.corp.local -u admin@vsphere.local     --cluster "Cluster-Prod" --cluster "Cluster-Dev" --html

# Multiple vCenters (Enhanced Linked Mode)
python vcenter_export_tpm_keys.py     -s vc-site-a.corp.local -s vc-site-b.corp.local     -u administrator@vsphere.local --html --txt
```

**All options:**

| Group | Flag | Default | Description |
|---|---|---|---|
| vCenter | `-s / --server` | required (repeatable) | vCenter hostname or IP. Repeat for ELM: `-s vc1 -s vc2` |
| vCenter | `-u / --user` | required | vCenter / SSO username |
| vCenter | `-p / --password` | prompted | vCenter password. Never echoed. |
| vCenter | `--port` | `443` | vCenter HTTPS port |
| SSH | `--ssh-user` | `root` | SSH username on ESXi hosts |
| SSH | `--ssh-password` | prompted | SSH password. Press Enter to reuse vCenter password. |
| SSH | `--ssh-port` | `22` | SSH port |
| SSH | `--ssh-timeout` | `30` | SSH connection and command timeout in seconds |
| Filtering | `-c / --cluster` | all (repeatable) | Only process clusters whose name contains this substring (case-insensitive) |
| Filtering | `--host-name` | all | Only process hosts whose name contains this substring |
| Output | `--html [FILE]` | off | Write HTML report. Omit FILE for an auto-generated timestamped name. |
| Output | `--txt [FILE]` | off | Write TXT report. Omit FILE for an auto-generated timestamped name. |
| Output | `--log-file` | off | Also write the console log to a file |
| Output | `--verbose` | off | Print DEBUG-level output to the console |
| Behaviour | `--disable-ssh-after` | `auto` | `auto` = disable SSH only if the script turned it on. `yes` = always disable. `no` = leave SSH running. |

**Exit codes:** `0` success, `1` if host data collection fails for one or more hosts.

**Dependencies:** `pip install pyVmomi paramiko colorama`

---

### `generate_hosts_config.py` -- VCF host commissioning JSON generator

Reads `hosts.json` and generates a host commissioning JSON file in the format expected by VMware Cloud Foundation (VCF). VCF accepts a maximum of 50 hosts per operation; when `hosts.json` contains more the output is split into `_part01`, `_part02`, ... files automatically.

**Usage:**

```
python generate_hosts_config.py
python generate_hosts_config.py -i my_hosts.json -o my_output.json
```

**Options:**

```
-i / --input    Path to input hosts.json  (default: hosts.json next to the script)
-o / --output   Path to output file        (default: hosts_config.json)
```

**Supported storage types:** VSAN / VSAN_REMOTE / VSAN_ESA / VSAN_MAX / NFS / VMFS_FC / VVOL

When VVOL is selected a second menu appears for the vVol protocol type (VMFS_FC / ISCSI / NFS).

**Exit codes:** `0` success, `1` file/JSON error, `2` invalid input or Ctrl+C

**Dependencies:** stdlib only (Python 3.6+)

---

### SSH disable logic (vcenter_esxi_ssh.py and esxi_direct_ssh.py)

| SSH state before run | `--disable-ssh-after` set | SSH state after run |
|---|---|---|
| Stopped | No | Stopped -- script turns it on, turns it back off |
| Stopped | Yes | Stopped |
| Running | No | Running -- left as found |
| Running | Yes | Stopped |

If the script is interrupted mid-run, some hosts may be left with SSH enabled. Run `--ssh-only-disable` afterwards to restore the expected state.

---

### Compatibility

- Python 3.8+ -- tested on 3.8 -- 3.12
- pyVmomi 8.0.2+, paramiko 3.4+, colorama 0.4.6+
- vCenter 7.0+ (vCenter 8 and 9 tested)
- ESXi 7.0+ (ESXi 8 and 9 tested)
- Windows 10/11, Linux, macOS
- All console output is ASCII-only (no `chcp 65001` required)
- Passwords prompted via `getpass`, never echoed or written to log files

---

## Requirements summary

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
| `vsphere/vcenter_rename_local_datastores.py` | `pyVmomi`, `colorama` (optional) |
| `vsphere/vcenter_export_tpm_keys.py` | `pyVmomi`, `paramiko`, `colorama` (optional) |
| `vsphere/generate_hosts_config.py` | -- |

---

## License

MIT
