# Utils

A collection of independent Python utilities. Each script is standalone — no shared dependencies, no framework. Pick what you need.

---

## Scripts

### `disk_scan.py` — USB Disk Duplicate Finder

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
- `compare` loads all `disk_scan_*.json` files, finds duplicates by hash, and decides which copy to keep. Files inside folders whose name starts with `_` are always preferred for survival. Among the remaining candidates the algorithm picks the copy on the disk with least free space, then maximises the space freed on one target disk.
- Outputs a colour-coded CLI report, an HTML report (`duplicates_report.html`), and one `disk_scan_<label>.cmd` per disk containing ready-to-run `DEL` commands.

**CLI output is Windows 10 cmd.exe compatible** (ASCII-only, UTF-8 reconfigure on startup).

**Dependencies:** stdlib only (Python 3.10+)

---

### `dircomp.py` — Directory Comparison by SHA-256

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

### `md2html.py` — Markdown to Retro Terminal HTML

Converts a Markdown file into a self-contained HTML page with a retro terminal aesthetic: fixed sidebar table of contents, CRT scanlines, blinking cursor, and a statusbar.

```
python md2html.py input.md
python md2html.py input.md -o output.html --theme amber
python md2html.py input.md --title "My Doc" --no-scanlines --verbose
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

### `fc2_client.py` — Focusrite Control 2 WebSocket Client

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
gain <ch> [dB]         get or set input gain
phantom <ch> [0/1]     get or set phantom power (+48 V)
air <ch> [0/1]         get or set Air mode
mute <ch> [0/1]        get or set input mute
raw <ono> <dl> <mi> [params_hex]   send raw AES70 command
monitor                live event stream
discover               scan ONo 0x1001–0x104F
quit
```

**Options:**

```
--name <name>           Device name shown in FC2 (default: iPad)
--auth <file>           Auth file path (default: fc2_auth.json)
--fc2-pubkey <64hex>    FC2 static Noise public key (32 bytes, hex)
```

Auth credentials (X25519 keypair + session token) are saved to `fc2_auth.json` after the first successful pairing. Subsequent runs reconnect automatically.

**Dependencies:** `pip install websockets cryptography`

---

## Requirements

Python 3.10 or newer is required for `str | None` union syntax used in type hints. `dircomp.py` works from Python 3.8.

| Script | Extra packages |
|---|---|
| `disk_scan.py` | — |
| `dircomp.py` | — |
| `md2html.py` | `markdown` |
| `fc2_client.py` | `websockets`, `cryptography` |

---

## License

MIT
