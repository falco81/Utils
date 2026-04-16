# ESXi SSH Automation — Documentation

Two scripts for automating SSH operations across ESXi hosts.  
Both use the **ESXi SOAP API** (pyVmomi) to manage the SSH service and **Paramiko** to execute commands. Every action is logged to both the console and a timestamped log file.

| Script | Host source | Use when |
|---|---|---|
| `vcenter_esxi_ssh.py` | vCenter — discovers all registered hosts automatically | You have a vCenter instance |
| `esxi_direct_ssh.py` | `hosts.json` — a plain list of IPs / FQDNs | No vCenter, or you want to target a fixed list |

Both scripts share identical operating modes, CLI flags, logging behaviour, SSH disable logic, and `COMMANDS_TO_RUN`.

---

## Table of Contents

1. [Requirements](#1-requirements)
2. [Installation](#2-installation)
3. [Quick Start](#3-quick-start)
4. [Configuring Commands](#4-configuring-commands)
5. [Operating Modes](#5-operating-modes)
6. [Command Reference — vcenter_esxi_ssh.py](#6-command-reference--vcenter_esxi_sshpy)
7. [Command Reference — esxi_direct_ssh.py](#7-command-reference--esxi_direct_sshpy)
8. [hosts.json Format](#8-hostsjson-format)
9. [SSH Disable Logic](#9-ssh-disable-logic)
10. [Logging](#10-logging)
11. [Host Filtering](#11-host-filtering)
12. [Exit Codes](#12-exit-codes)
13. [Workflow Examples](#13-workflow-examples)
14. [Scheduled Execution on Windows](#14-scheduled-execution-on-windows)
15. [Troubleshooting](#15-troubleshooting)
16. [Security Considerations](#16-security-considerations)
17. [Architecture Overview](#17-architecture-overview)

---

## 1. Requirements

| Requirement | Minimum version | Notes |
|---|---|---|
| Python | 3.8 | Tested on 3.8 – 3.12 |
| pyVmomi | 8.0.2.0 | vSphere / vCenter Python SDK — works against both vCenter and standalone ESXi |
| paramiko | 3.4.0 | SSH client |
| colorama | 0.4.6 | Coloured console output (Windows CMD / PowerShell compatible) |
| vCenter | 7.0+ | Required only for `vcenter_esxi_ssh.py`. Tested on vCenter 8 |
| ESXi | 7.0+ | Tested on ESXi 8 and 9. Management network must be reachable from the machine running the script |

> `colorama` is optional. If it is not installed the script still runs correctly — console output is displayed without colours.

---

## 2. Installation

```bash
pip install pyVmomi paramiko colorama
```

Or using the requirements file:

```bash
pip install -r requirements.txt
```

Verify the installation:

```bash
python vcenter_esxi_ssh.py --help
python esxi_direct_ssh.py --help
```

If any required package is missing the script exits immediately with a clear message:

```
[ERROR] Missing Python packages: pyVmomi, paramiko
        Install them with:  pip install pyVmomi paramiko
```

---

## 3. Quick Start

### vcenter_esxi_ssh.py

```bash
# Dry-run — preview without making any changes
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local --dry-run

# Run commands and disable SSH afterwards
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --disable-ssh-after
```

Passwords are prompted interactively when omitted from the command line.

### esxi_direct_ssh.py

```bash
# Create hosts.json in the current directory first (see Section 8)

# Dry-run
python esxi_direct_ssh.py -u root --dry-run

# Run commands and disable SSH afterwards
python esxi_direct_ssh.py -u root --disable-ssh-after
```

---

## 4. Configuring Commands

Both scripts share a single `COMMANDS_TO_RUN` list near the top of each file. Edit this list to define what runs on every host.

```python
COMMANDS_TO_RUN = [
    "localcli --plugin-dir=/usr/lib/vmware/esxcli/int sched group getmemconfig -g host/vim/vmvisor/settingsd-task-forks",
    "localcli --plugin-dir=/usr/lib/vmware/esxcli/int sched group setmemconfig -g host/vim/vmvisor/settingsd-task-forks -m 400 -i 0 -l -1 -u mb",
    "localcli --plugin-dir=/usr/lib/vmware/esxcli/int sched group getmemconfig -g host/vim/vmvisor/settingsd-task-forks",
]
```

Commands are executed **sequentially** on each host. A non-zero exit code is logged as a warning but does **not** abort the remaining commands or skip the host.

### Useful ESXi command reference

**Inventory & version**
```bash
esxcli system version get
esxcli hardware cpu global get
esxcli hardware memory get
```

**Networking**
```bash
esxcli network ip interface list
esxcli network ip route ipv4 list
esxcli network nic list
```

**Storage**
```bash
esxcli storage core device list
esxcli storage core adapter list
```

**NTP & time**
```bash
esxcli system ntp get
/etc/init.d/ntpd status
```

**Security & compliance**
```bash
esxcli system settings advanced list -o /UserVars/ESXiShellInteractiveTimeOut
esxcli system settings advanced list -o /UserVars/SuppressShellWarning
cat /etc/vmware/config
```

**Software & patching**
```bash
esxcli software vib list
esxcli software profile get
```

**Syslog**
```bash
esxcli system syslog config get
```

**VM inventory**
```bash
vim-cmd vmsvc/getallvms
```

---

## 5. Operating Modes

Both scripts support the same three mutually exclusive modes.

### Mode 1 — Run Commands *(default)*

Enables SSH on each host, opens an SSH session, runs every command in `COMMANDS_TO_RUN`, then optionally disables SSH.

**Per-host steps:**
1. Record current SSH service state
2. Enable SSH via the ESXi SOAP API (if not already running)
3. Connect via SSH (Paramiko)
4. Execute each command sequentially; log stdout, stderr, exit code
5. Disconnect SSH
6. Disable SSH if appropriate (see [SSH Disable Logic](#9-ssh-disable-logic))

### Mode 2 — SSH Only Enable (`--ssh-only-enable`)

Enables the SSH service on every matched host. No SSH session is opened, no commands are run. If SSH is already running the host is recorded as `OK` with a note — not an error.

### Mode 3 — SSH Only Disable (`--ssh-only-disable`)

Disables the SSH service on every matched host. No SSH session is opened, no commands are run. If SSH is already stopped the host is recorded as `OK` with a note.

### Mode compatibility

| Flag combination | Result |
|---|---|
| *(no mode flag)* | Run Commands mode |
| `--ssh-only-enable` | SSH Only Enable mode |
| `--ssh-only-disable` | SSH Only Disable mode |
| `--ssh-only-enable --ssh-only-disable` | Rejected — mutually exclusive |
| `--ssh-only-enable --disable-ssh-after` | Rejected |
| `--ssh-only-disable --disable-ssh-after` | Rejected |

---

## 6. Command Reference — vcenter_esxi_ssh.py

### vCenter Connection

| Parameter | Short | Required | Default | Description |
|---|---|---|---|---|
| `--server` | `-s` | **Yes** | — | vCenter hostname or IP address |
| `--user` | `-u` | **Yes** | — | vCenter login (e.g. `administrator@vsphere.local`) |
| `--password` | `-p` | No | *(prompted)* | vCenter password |
| `--port` | | No | `443` | vCenter HTTPS port |

### ESXi SSH Options

Only apply in **Run Commands** mode.

| Parameter | Default | Description |
|---|---|---|
| `--ssh-user` | `root` | SSH username on ESXi hosts |
| `--ssh-password` | *(prompted)* | SSH password. Prompted separately from the vCenter password |
| `--ssh-port` | `22` | TCP port for SSH |
| `--ssh-timeout` | `30` | Timeout in seconds for connection and each command |

### Filtering

| Parameter | Default | Description |
|---|---|---|
| `--cluster` | *(all)* | Case-insensitive substring match against the cluster name |
| `--host-name` | *(all)* | Case-insensitive substring match against the host's registered name |
| `--skip-disconnected` | on | Skip hosts in `disconnected` or `notResponding` state. Powered-off hosts are always skipped |

### SSH-Only Modes

| Parameter | Description |
|---|---|
| `--ssh-only-enable` | Enable SSH only; no commands |
| `--ssh-only-disable` | Disable SSH only; no commands |

### Behaviour Options

| Parameter | Default | Description |
|---|---|---|
| `--disable-ssh-after` | off | Always disable SSH after commands, even if SSH was already running |
| `--dry-run` | off | Simulate all actions without making changes |
| `--verbose` | off | Print DEBUG-level output to the console |
| `--log-file` | `vcenter_esxi_ssh_YYYYMMDD_HHMMSS.log` | Log file path. Directory is created automatically |
| `--no-log-file` | off | Console output only; no log file |

---

## 7. Command Reference — esxi_direct_ssh.py

### Host List

| Parameter | Short | Default | Description |
|---|---|---|---|
| `--config` | `-c` | `hosts.json` | Path to the JSON host list file |

### ESXi API Credentials

Used to manage the SSH service via the ESXi SOAP API.

| Parameter | Short | Required | Default | Description |
|---|---|---|---|---|
| `--user` | `-u` | **Yes** | — | ESXi username (typically `root`) |
| `--password` | `-p` | No | *(prompted)* | ESXi API password |
| `--port` | | No | `443` | ESXi HTTPS API port |

### SSH Options

Only apply in **Run Commands** mode.

| Parameter | Default | Description |
|---|---|---|
| `--ssh-user` | `root` | SSH username on ESXi hosts |
| `--ssh-password` | *(prompted)* | SSH password. Prompted separately; press Enter to reuse the API password |
| `--ssh-port` | `22` | TCP port for SSH |
| `--ssh-timeout` | `30` | Timeout in seconds for connection and each command |

### Filtering

| Parameter | Default | Description |
|---|---|---|
| `--host-name` | *(all)* | Case-insensitive substring match against the IP or FQDN from hosts.json |

### SSH-Only Modes

| Parameter | Description |
|---|---|
| `--ssh-only-enable` | Enable SSH only; no commands |
| `--ssh-only-disable` | Disable SSH only; no commands |

### Behaviour Options

| Parameter | Default | Description |
|---|---|---|
| `--disable-ssh-after` | off | Always disable SSH after commands, even if SSH was already running |
| `--dry-run` | off | Simulate all actions without making changes |
| `--verbose` | off | Print DEBUG-level output to the console |
| `--log-file` | `esxi_direct_ssh_YYYYMMDD_HHMMSS.log` | Log file path. Directory is created automatically |
| `--no-log-file` | off | Console output only; no log file |

---

## 8. hosts.json Format

`esxi_direct_ssh.py` reads its host list from a plain JSON array of strings. Each entry is an IP address or FQDN.

```json
[
  "192.168.10.11",
  "192.168.10.12",
  "192.168.10.13",
  "esxi-lab-01.corp.local"
]
```

Rules:
- The file must be a JSON array — no objects, no nested structure, no comments.
- Each element must be a non-empty string.
- The default filename is `hosts.json` in the current working directory. Use `--config` to specify a different path.

---

## 9. SSH Disable Logic

Both scripts apply the same logic.

### Run Commands mode

| SSH state before script ran | `--disable-ssh-after` set? | SSH state after script finishes |
|---|---|---|
| Stopped | No | Stopped — script turns it on, turns it back off |
| Stopped | Yes | Stopped |
| Running | No | **Running** — left as found |
| Running | Yes | Stopped |

The key principle: **if the script turned SSH on, it turns it back off; if SSH was already on and `--disable-ssh-after` is not set, it is left running.**

If the script is interrupted mid-run (e.g. `Ctrl+C`), some hosts may be left with SSH enabled. Run `--ssh-only-disable` afterwards to clean up.

### SSH-only modes

`--ssh-only-enable` and `--ssh-only-disable` make a single explicit change and do not apply restore logic. The state after the script finishes is exactly what the mode name says.

---

## 10. Logging

Both scripts write two independent log streams simultaneously.

### Console output

Colour-coded by severity (requires `colorama`):

| Level | Colour | When visible |
|---|---|---|
| DEBUG (cyan) | Only with `--verbose` | SSH connection details, service state checks |
| INFO (green) | Always | Normal progress — host headers, command output, state changes |
| WARNING (yellow) | Always | Non-fatal issues — host skipped, SSH did not start, non-zero exit code |
| ERROR (red) | Always | Recoverable failures — SSH auth failed, command exception |
| CRITICAL (magenta) | Always | Fatal errors — API connection refused; script exits immediately |

### Log file

Always receives **all** levels including DEBUG, regardless of `--verbose`.

Log line format:
```
2026-04-16 15:22:56  [INFO    ]  [1/5]  HOST : esx-01a.site-a.vcf.lab
2026-04-16 15:22:57  [DEBUG   ]         SOAP connect -> esx-01a.site-a.vcf.lab:443
2026-04-16 15:22:57  [DEBUG   ]         ESXi SOAP API: VMware ESXi 9.0.2.0  (build 25148076)
2026-04-16 15:22:57  [INFO    ]    -->  SSH service: STOPPED
2026-04-16 15:22:58  [DEBUG   ]         SSH service started on esx-01a.site-a.vcf.lab
2026-04-16 15:22:59  [INFO    ]    -->  Running 3 command(s) via SSH ...
2026-04-16 15:23:00  [INFO    ]         [OK]   (exit 0) $ localcli ... getmemconfig ...
```

Custom log file path:
```bash
# vcenter version
--log-file C:\Logs\vcenter_audit.log

# direct version
--log-file C:\Logs\esxi_direct_audit.log
```

Suppress log file:
```bash
--no-log-file
```

---

## 11. Host Filtering

### vcenter_esxi_ssh.py

Filters are applied in this order. A host is skipped as soon as it fails any check.

```
1. --host-name  substring match
2. connection_state: disconnected / notResponding
3. power_state: not poweredOn
```

Examples:
```bash
--cluster "Prod"                          # all clusters containing "Prod"
--host-name "esxi-prod-07"               # exact single host
--cluster "Prod" --host-name "esxi-prod-0"  # both must match
```

### esxi_direct_ssh.py

The only filter is `--host-name`, matched against the IP or FQDN string from `hosts.json`.

```bash
--host-name "192.168.10.11"    # single host by IP
--host-name "site-a"           # all hosts whose address contains "site-a"
```

Hosts that do not match appear in the summary as `SKIPPED` and do not affect the exit code.

---

## 12. Exit Codes

Both scripts return the same exit codes.

| Code | Meaning |
|---|---|
| `0` | All processed hosts completed successfully (skipped hosts do not count as failures) |
| `1` | One or more hosts reported `FAILED` status |

```bat
REM Example: abort a batch if the script fails
python esxi_direct_ssh.py -u root --disable-ssh-after
if %errorlevel% neq 0 (
    echo Script failed — check the log file
    exit /b 1
)
```

---

## 13. Workflow Examples

### Workflow 1 — Audit (read-only)

Collect info from every host. Dry-run first, then run for real.

```bash
# vcenter version
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local --dry-run
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --log-file C:\Logs\audit.log

# direct version
python esxi_direct_ssh.py -u root --dry-run
python esxi_direct_ssh.py -u root --log-file C:\Logs\audit.log
```

---

### Workflow 2 — Apply a Configuration Change

The current `COMMANDS_TO_RUN` reads the current memory config, applies the change, then reads again to confirm:

```bash
# vcenter version — all hosts, disable SSH afterwards
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --ssh-user root \
    --disable-ssh-after \
    --log-file C:\Logs\memconfig_change.log

# direct version
python esxi_direct_ssh.py -u root \
    --disable-ssh-after \
    --log-file C:\Logs\memconfig_change.log
```

---

### Workflow 3 — Target a Single Host

```bash
# vcenter version
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --host-name "esx-01a" \
    --disable-ssh-after \
    --verbose

# direct version
python esxi_direct_ssh.py -u root \
    --host-name "esx-01a" \
    --disable-ssh-after \
    --verbose
```

---

### Workflow 4 — Maintenance Window SSH Toggle

Open SSH before the window, close it cleanly afterwards.

```bash
# vcenter version
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --cluster "Cluster-Prod" --ssh-only-enable --log-file C:\Logs\ssh_open.log

# ... perform maintenance ...

python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --cluster "Cluster-Prod" --ssh-only-disable --log-file C:\Logs\ssh_close.log

# direct version
python esxi_direct_ssh.py -u root --ssh-only-enable --log-file C:\Logs\ssh_open.log

# ... perform maintenance ...

python esxi_direct_ssh.py -u root --ssh-only-disable --log-file C:\Logs\ssh_close.log
```

Both support `--dry-run`:
```bash
python esxi_direct_ssh.py -u root --ssh-only-disable --dry-run
```

---

### Workflow 5 — Multi-Cluster Sweep (vcenter_esxi_ssh.py)

```bash
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --cluster "Cluster-Prod" --ssh-password ProdRootPass \
    --disable-ssh-after --log-file C:\Logs\sweep_prod.log

python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --cluster "Cluster-Dev" --ssh-password DevRootPass \
    --disable-ssh-after --log-file C:\Logs\sweep_dev.log
```

---

## 14. Scheduled Execution on Windows

### Using environment variables for credentials

```bat
setx VC_PASS "MyVCenterPassword"
setx SSH_PASS "MyRootPassword"
setx ESXI_PASS "MyRootPassword"
```

### vcenter_esxi_ssh.py batch wrapper

```bat
@echo off
setlocal

set VC_SERVER=vcenter.corp.local
set VC_USER=administrator@vsphere.local
set LOG_FILE=C:\Logs\vcenter_audit_%date:~-4,4%%date:~-7,2%%date:~-10,2%.log

python C:\Scripts\vcenter_esxi_ssh.py ^
    -s %VC_SERVER% ^
    -u %VC_USER% ^
    -p %VC_PASS% ^
    --ssh-password %SSH_PASS% ^
    --disable-ssh-after ^
    --log-file "%LOG_FILE%"

if %errorlevel% neq 0 (
    echo [FAIL] Check: %LOG_FILE%
    exit /b 1
)
echo [OK] Log: %LOG_FILE%
```

### esxi_direct_ssh.py batch wrapper

```bat
@echo off
setlocal

set LOG_FILE=C:\Logs\esxi_direct_%date:~-4,4%%date:~-7,2%%date:~-10,2%.log

python C:\Scripts\esxi_direct_ssh.py ^
    -u root ^
    -p %ESXI_PASS% ^
    --ssh-password %ESXI_PASS% ^
    --disable-ssh-after ^
    --log-file "%LOG_FILE%"

if %errorlevel% neq 0 (
    echo [FAIL] Check: %LOG_FILE%
    exit /b 1
)
echo [OK] Log: %LOG_FILE%
```

---

## 15. Troubleshooting

### `Failed to start SSH on <host>: host`

Occurs with `esxi_direct_ssh.py`. The pyVmomi object tree on a direct ESXi connection goes through a `vim.Datacenter` layer (`ha-datacenter`) that does not exist on vCenter. Fixed in the current version — the path is:

```
rootFolder → Datacenter (ha-datacenter) → hostFolder → ComputeResource → host[0] → configManager.serviceSystem
```

Ensure you are using the latest version of the script.

---

### API connection refused / SSL error

Both scripts disable certificate verification by default to support self-signed ESXi and vCenter certificates.

- Confirm the host is reachable: `ping <esxi-or-vcenter-host>`
- Confirm port 443 is open: `Test-NetConnection <host> -Port 443` (PowerShell)
- If using a non-standard port, pass `--port <port>`

---

### SSH connection fails

| Symptom | Likely cause | Fix |
|---|---|---|
| `SSH authentication failed` | Wrong `--ssh-password` or `--ssh-user` | Verify root credentials |
| `SSH connection failed` / timeout | Management IP not reachable | Check routing and firewall |
| `SSH service did not start within 10s` | Host under load or service locked | Check host health; retry |
| Command exit code `-1` | SSH session dropped mid-command | Increase `--ssh-timeout` |

---

### A host always shows as FAILED in esxi_direct_ssh.py

- Confirm the IP / FQDN in `hosts.json` is correct and reachable on port 443
- Confirm the ESXi API user and password are correct (`-u root -p <password>`)
- Run with `--verbose` to see the full SOAP connection error

---

### A host always shows as SKIPPED in vcenter_esxi_ssh.py

Check the host's connection and power state in vCenter. The script skips hosts that are `disconnected`, `notResponding`, or not `poweredOn`, and hosts excluded by `--cluster` or `--host-name` filters.

---

### Commands produce no output

Some ESXi commands return output only when there is something to report. An empty stdout with exit code `0` is not an error.

---

## 16. Security Considerations

**Credentials**

- Never pass passwords on the command line in shared or logged environments — use the interactive prompt (omit `-p` / `--ssh-password`) or environment variables.
- Neither script persists credentials. They exist in memory only for the duration of the run.

**SSL / TLS**

- Both scripts disable certificate verification to support self-signed certificates. To enable verification modify the `ssl.SSLContext` block in the connect function and set `ctx.check_hostname = True` and `ctx.verify_mode = ssl.CERT_REQUIRED`.

**SSH host key verification**

- Both scripts use `paramiko.AutoAddPolicy()`, which accepts any SSH host key without verification. This is acceptable for controlled internal environments. For stricter environments replace `AutoAddPolicy` with `paramiko.RejectPolicy` and pre-populate a `known_hosts` file.

**SSH service state after interruption**

- If the script is interrupted (`Ctrl+C`, power loss) mid-run, some hosts may be left with SSH enabled. Run `--ssh-only-disable` afterwards to close SSH on all hosts.

---

## 17. Architecture Overview

### vcenter_esxi_ssh.py

```
vcenter_esxi_ssh.py
│
├── COMMANDS_TO_RUN              ← edit to customise what runs on each host
│
├── Logging layer
│   ├── Console handler          coloured, INFO by default / DEBUG with --verbose
│   └── File handler             always DEBUG, timestamped lines
│
├── vCenter layer  (pyVmomi)
│   ├── connect_vcenter()        SSL connection to vCenter, accepts self-signed certs
│   ├── get_all_hosts()          ContainerView traversal of rootFolder
│   ├── get_host_info()          name, cluster, management IP, connection/power state
│   ├── is_ssh_running()         reads vim.host.ServiceSystem.serviceInfo
│   ├── enable_ssh()             StartService("TSM-SSH") + 10s polling
│   └── disable_ssh()            StopService("TSM-SSH")
│
├── SSH layer  (paramiko)
│   └── run_ssh_commands()       connect → exec_command loop → close
│
├── Argument parser
│   ├── vCenter connection group
│   ├── ESXi SSH options group
│   ├── Filtering group          --cluster, --host-name, --skip-disconnected
│   ├── SSH-only modes group     --ssh-only-enable / --ssh-only-disable
│   └── Behaviour options group  --disable-ssh-after, --dry-run, --verbose, --log-file
│
└── main()
    ├── Credential prompts
    ├── Mode detection            run-commands / ssh-only-enable / ssh-only-disable
    ├── Banner + config summary
    ├── vCenter connect
    ├── Host enumeration + filtering
    ├── Per-host loop
    │   ├── [ssh-only-enable]    enable SSH → disconnect SOAP → continue
    │   ├── [ssh-only-disable]   disable SSH → disconnect SOAP → continue
    │   └── [run-commands]       enable SSH → run commands → conditional disable → disconnect SOAP
    └── Summary report + exit code
```

### esxi_direct_ssh.py

```
esxi_direct_ssh.py
│
├── COMMANDS_TO_RUN              ← same list, edit in the same way
│
├── Logging layer                identical to vcenter_esxi_ssh.py
│
├── hosts.json loader
│   └── load_hosts()             reads a plain JSON array of IP/FQDN strings
│
├── Direct ESXi SOAP layer  (pyVmomi — no vCenter)
│   ├── connect_esxi()           SmartConnect directly to ESXi host, self-signed certs
│   ├── _svc_system()            rootFolder → Datacenter (ha-datacenter)
│   │                              → hostFolder → ComputeResource → host[0]
│   │                                → configManager.serviceSystem
│   ├── is_ssh_running()         reads vim.host.ServiceSystem.serviceInfo
│   ├── enable_ssh()             StartService("TSM-SSH") + 10s polling
│   └── disable_ssh()            StopService("TSM-SSH")
│
├── SSH layer  (paramiko)        identical to vcenter_esxi_ssh.py
│
├── Argument parser
│   ├── Host list group          --config
│   ├── ESXi API credentials     -u, -p, --port
│   ├── SSH options group
│   ├── Filtering group          --host-name
│   ├── SSH-only modes group     --ssh-only-enable / --ssh-only-disable
│   └── Behaviour options group  --disable-ssh-after, --dry-run, --verbose, --log-file
│
└── main()
    ├── Credential prompts
    ├── Mode detection
    ├── Banner + config summary
    ├── Load hosts.json + apply --host-name filter
    ├── Per-host loop
    │   ├── connect_esxi()       per-host SOAP connection (not a shared session)
    │   ├── [ssh-only-enable]    enable SSH → disconnect SOAP → continue
    │   ├── [ssh-only-disable]   disable SSH → disconnect SOAP → continue
    │   └── [run-commands]       enable SSH → run commands → conditional disable → disconnect SOAP
    └── Summary report + exit code
```

---

*Tested on: vCenter 8.0, ESXi 8.0 / 9.0, Python 3.11, Windows 10/11, Ubuntu 22.04.*
