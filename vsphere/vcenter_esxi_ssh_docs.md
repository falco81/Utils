# vcenter_esxi_ssh.py — Documentation

Automate SSH operations across all ESXi hosts registered in a vCenter 8 instance.  
The script uses the **vCenter API** (pyVmomi) to manage the SSH service and **Paramiko** to execute commands over SSH. Every action is logged to both the console and a timestamped log file.

---

## Table of Contents

1. [Requirements](#1-requirements)
2. [Installation](#2-installation)
3. [Quick Start](#3-quick-start)
4. [Operating Modes](#4-operating-modes)
5. [Command Reference](#5-command-reference)
6. [Customising Commands](#6-customising-commands)
7. [SSH Disable Logic](#7-ssh-disable-logic)
8. [Logging](#8-logging)
9. [Host Filtering](#9-host-filtering)
10. [Exit Codes](#10-exit-codes)
11. [Workflow Examples](#11-workflow-examples)
12. [Scheduled Execution on Windows](#12-scheduled-execution-on-windows)
13. [Troubleshooting](#13-troubleshooting)
14. [Security Considerations](#14-security-considerations)
15. [Architecture Overview](#15-architecture-overview)

---

## 1. Requirements

| Requirement | Minimum version | Notes |
|---|---|---|
| Python | 3.8 | Tested on 3.8 – 3.12 |
| pyVmomi | 8.0.2.0 | vSphere / vCenter Python SDK |
| paramiko | 3.4.0 | SSH client |
| colorama | 0.4.6 | Coloured console output (Windows CMD/PowerShell compatible) |
| vCenter | 7.0+ | Tested on vCenter 8 |
| ESXi | 7.0+ | Requires management network reachability from the machine running the script |

> **Note:** `colorama` is optional. If it is not installed, the script still runs correctly — console output is simply displayed without colours.

---

## 2. Installation

### 2.1 Install Python packages

```bash
pip install -r requirements.txt
```

Or install packages individually:

```bash
pip install pyVmomi paramiko colorama
```

### 2.2 Verify installation

```bash
python vcenter_esxi_ssh.py --help
```

If any required package is missing, the script prints a clear error message and exits before doing anything else:

```
[ERROR] Missing Python packages: pyVmomi, paramiko
        Install them with:  pip install pyVmomi paramiko
```

---

## 3. Quick Start

```bash
# Dry-run first — see what would happen without making any changes
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local --dry-run

# Run for real — enable SSH, execute commands, disable SSH again
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --disable-ssh-after
```

The password is prompted interactively if `-p` is omitted. The SSH password defaults to the vCenter password unless `--ssh-password` is provided separately.

---

## 4. Operating Modes

The script has three mutually exclusive operating modes, selected by the flags you pass.

### Mode 1 — Run Commands *(default)*

Enables SSH on each host, opens an SSH session, runs every command listed in `COMMANDS_TO_RUN`, then optionally disables SSH again.

```
python vcenter_esxi_ssh.py -s vc.corp.local -u admin@vsphere.local
```

**Per-host steps:**
1. Check current SSH service state (remember it for later)
2. Enable SSH via the vCenter API (if not already running)
3. Connect via SSH (Paramiko)
4. Execute each command in `COMMANDS_TO_RUN` sequentially
5. Log stdout, stderr, and the exit code for every command
6. Disconnect SSH
7. Disable SSH if appropriate (see [SSH Disable Logic](#7-ssh-disable-logic))

---

### Mode 2 — SSH Only Enable (`--ssh-only-enable`)

Enables the SSH service on every matched host via the vCenter API. **No SSH session is opened and no commands are run.** Use this to prepare hosts for a maintenance window.

```
python vcenter_esxi_ssh.py -s vc.corp.local -u admin@vsphere.local --ssh-only-enable
```

If SSH is already running on a host, the script logs that fact and moves on — it does **not** treat this as an error.

---

### Mode 3 — SSH Only Disable (`--ssh-only-disable`)

Disables the SSH service on every matched host via the vCenter API. **No SSH session is opened and no commands are run.** Use this to clean up after a maintenance window.

```
python vcenter_esxi_ssh.py -s vc.corp.local -u admin@vsphere.local --ssh-only-disable
```

If SSH is already stopped on a host, the script logs that fact and moves on.

---

### Mode compatibility matrix

| Flag combination | Allowed? |
|---|---|
| *(no mode flag)* | ✓ Run Commands mode |
| `--ssh-only-enable` | ✓ |
| `--ssh-only-disable` | ✓ |
| `--ssh-only-enable --ssh-only-disable` | ✗ Rejected (mutually exclusive) |
| `--ssh-only-enable --disable-ssh-after` | ✗ Rejected |
| `--ssh-only-disable --disable-ssh-after` | ✗ Rejected |

---

## 5. Command Reference

All parameters are grouped by function below.

### 5.1 vCenter Connection

| Parameter | Short | Required | Default | Description |
|---|---|---|---|---|
| `--server` | `-s` | **Yes** | — | vCenter hostname or IP address |
| `--user` | `-u` | **Yes** | — | vCenter login (e.g. `administrator@vsphere.local`) |
| `--password` | `-p` | No | *(prompted)* | vCenter password. If omitted the script prompts interactively — recommended for interactive use to avoid passwords in shell history |
| `--port` | | No | `443` | vCenter HTTPS port |

### 5.2 ESXi SSH Options

These parameters only apply to **Run Commands mode**. They are ignored in `--ssh-only-*` modes.

| Parameter | Required | Default | Description |
|---|---|---|---|
| `--ssh-user` | No | `root` | Username for the SSH connection on each ESXi host |
| `--ssh-password` | No | same as `--password` | SSH password. Defaults to the vCenter password when omitted |
| `--ssh-port` | No | `22` | TCP port for SSH on ESXi hosts |
| `--ssh-timeout` | No | `30` | Timeout in seconds for both the SSH connection and individual command execution |

### 5.3 Filtering

All filters apply to every operating mode. Hosts that do not match are listed in the summary as `SKIPPED`.

| Parameter | Default | Description |
|---|---|---|
| `--cluster` | *(all)* | Case-insensitive substring match against the cluster name. Standalone hosts (not in any cluster) are excluded when this filter is active |
| `--host-name` | *(all)* | Case-insensitive substring match against the host's registered name. Useful for targeting a single host or a naming-pattern group |
| `--skip-disconnected` | on | Skip hosts whose connection state is `disconnected` or `notResponding`. Enabled by default. Powered-off hosts are always skipped regardless of this flag |

> **Substring matching:** `--cluster "Prod"` matches `Cluster-Prod-A`, `Cluster-Prod-B`, `Prod-Cluster`, etc.

### 5.4 SSH-Only Modes

| Parameter | Description |
|---|---|
| `--ssh-only-enable` | Enable SSH on matched hosts only; skip all command execution. Mutually exclusive with `--ssh-only-disable` and `--disable-ssh-after` |
| `--ssh-only-disable` | Disable SSH on matched hosts only; skip all command execution. Mutually exclusive with `--ssh-only-enable` and `--disable-ssh-after` |

### 5.5 Behaviour Options

| Parameter | Default | Description |
|---|---|---|
| `--disable-ssh-after` | off | Always disable SSH after command execution, even if SSH was already running when the script started. See [SSH Disable Logic](#7-ssh-disable-logic) |
| `--dry-run` | off | Simulate every action without making any changes. Prints `[DRY-RUN]` prefixed lines showing what would happen |
| `--verbose` | off | Print `DEBUG`-level messages to the console. Debug output is always written to the log file regardless of this flag |
| `--log-file` | `vcenter_esxi_ssh_YYYYMMDD_HHMMSS.log` | Path to the log file. The directory is created automatically if it does not exist |
| `--no-log-file` | off | Disable log file creation; write to the console only |

---

## 6. Customising Commands

Edit the `COMMANDS_TO_RUN` list near the top of the script (around line 200):

```python
COMMANDS_TO_RUN = [
    "esxcli system version get",
    "esxcli network ip interface list",
    "esxcli storage core device list | head -40",
    "vim-cmd vmsvc/getallvms",
]
```

Commands are executed **sequentially** on each host in the order they appear in the list. If one command fails (non-zero exit code), the script logs the failure and continues with the next command — it does **not** abort the host.

### Useful command examples by use case

**Inventory & version info**
```bash
esxcli system version get
esxcli hardware cpu global get
esxcli hardware memory get
esxcli storage core device list
```

**Networking**
```bash
esxcli network ip interface list
esxcli network ip route ipv4 list
esxcli network nic list
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
esxcli system version get
```

**VM inventory**
```bash
vim-cmd vmsvc/getallvms
vim-cmd vmsvc/get.summary <vmid>
```

**Syslog**
```bash
esxcli system syslog config get
```

---

## 7. SSH Disable Logic

The script is careful not to alter the SSH state of a host unnecessarily.

### Run Commands mode

| SSH state before script ran | `--disable-ssh-after` set? | SSH state after script finishes |
|---|---|---|
| Stopped | No | Stopped — the script turns it on for the session and turns it back off |
| Stopped | Yes | Stopped |
| Running | No | **Running** — the script leaves it as it found it |
| Running | Yes | Stopped — the flag overrides the original state |

> The key principle: **if you didn't open it, the script closes it behind itself; if it was already open and you haven't asked otherwise, the script leaves it alone.**

### SSH-only modes

`--ssh-only-enable` and `--ssh-only-disable` make a single, explicit change and do not apply any restore logic. The state after the script finishes is exactly what the mode name says.

---

## 8. Logging

The script writes two independent log streams simultaneously.

### Console output

Colour-coded by severity (requires `colorama`):

| Level | Colour | When used |
|---|---|---|
| DEBUG (cyan) | Only with `--verbose` | SSH connection details, per-service state checks |
| INFO (green) | Always | Normal progress — host headers, command output, SSH state changes |
| WARNING (yellow) | Always | Non-fatal issues — host skipped, SSH did not start within 10 s, command non-zero exit |
| ERROR (red) | Always | Recoverable failures — SSH auth failed, command exception |
| CRITICAL (magenta) | Always | Fatal errors — vCenter connection refused; script exits immediately |

### Log file

The file always receives **all** levels including DEBUG, regardless of `--verbose`.  
Default filename: `vcenter_esxi_ssh_YYYYMMDD_HHMMSS.log` in the current working directory.

Log file line format:
```
2025-03-15 14:22:01  [INFO    ]  [1/12]  HOST : esxi-prod-01.corp.local
2025-03-15 14:22:01  [INFO    ]           Cluster : Cluster-Prod
2025-03-15 14:22:02  [INFO    ]     -->  SSH service: STOPPED  -> enabling ...
2025-03-15 14:22:04  [DEBUG   ]           SSH service started on esxi-prod-01.corp.local
2025-03-15 14:22:04  [INFO    ]     -->  Running 4 command(s) via SSH ...
2025-03-15 14:22:05  [INFO    ]           [OK]   (exit 0) $ esxcli system version get
2025-03-15 14:22:05  [INFO    ]                  VMware ESXi 8.0.2 build-22380479
```

To specify a custom path:
```bash
--log-file C:\Logs\vcenter_audit.log
# or on Linux/macOS:
--log-file /var/log/vcenter_audit.log
```

To suppress the log file entirely:
```bash
--no-log-file
```

---

## 9. Host Filtering

Filters are applied in this order. A host is skipped as soon as it fails any check.

```
1. --host-name substring match
2. connection_state: disconnected / notResponding  (when --skip-disconnected)
3. power_state: not poweredOn
```

Skipped hosts appear in the final summary as `[SKIP]` and do **not** affect the exit code.

### Examples

Process only hosts in clusters containing "Prod":
```bash
--cluster "Prod"
```

Process only a specific host:
```bash
--host-name "esxi-prod-07"
```

Process all hosts in a cluster whose name starts with "DMZ":
```bash
--cluster "DMZ"
```

Combine cluster and host name filters (both must match):
```bash
--cluster "Prod" --host-name "esxi-prod-0"
```

---

## 10. Exit Codes

| Code | Meaning |
|---|---|
| `0` | All processed hosts completed successfully (or were skipped by filter) |
| `1` | One or more hosts reported `FAILED` status |

This makes the script safe to use in pipelines, CI systems, or Windows Task Scheduler conditions.

```bat
REM Example: abort a batch if the script fails
python vcenter_esxi_ssh.py -s vc.corp.local -u admin@vsphere.local --disable-ssh-after
if %errorlevel% neq 0 (
    echo Script failed — check the log file
    exit /b 1
)
```

---

## 11. Workflow Examples

### Workflow 1 — Infrastructure Audit (read-only)

Collect version, hardware, and network info from every host. No changes are made to the environment. Always dry-run first.

```bash
# Step 1: preview scope
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local --dry-run

# Step 2: run and save the output
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --log-file C:\Logs\audit.log
```

`COMMANDS_TO_RUN` suggestion:
```python
COMMANDS_TO_RUN = [
    "esxcli system version get",
    "esxcli hardware cpu global get",
    "esxcli hardware memory get",
    "esxcli storage core device list",
    "esxcli network ip interface list",
]
```

---

### Workflow 2 — Security Compliance Check

Verify NTP, syslog, and shell timeout settings across all hosts. Use `--disable-ssh-after` to guarantee SSH is closed when the run finishes.

```bash
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --ssh-user root --ssh-password RootP@ss \
    --disable-ssh-after \
    --log-file C:\Logs\compliance.log
```

`COMMANDS_TO_RUN` suggestion:
```python
COMMANDS_TO_RUN = [
    "esxcli system ntp get",
    "esxcli system syslog config get",
    "esxcli system settings advanced list -o /UserVars/ESXiShellInteractiveTimeOut",
    "esxcli system settings advanced list -o /UserVars/SuppressShellWarning",
]
```

---

### Workflow 3 — Pre-Patch VIB Inventory

Snapshot installed VIBs across the production cluster before a maintenance window. `--verbose` ensures every VIB line appears in the log file.

```bash
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --cluster "Cluster-Prod" \
    --disable-ssh-after \
    --verbose \
    --log-file C:\Logs\patch_prep.log
```

`COMMANDS_TO_RUN` suggestion:
```python
COMMANDS_TO_RUN = [
    "esxcli software vib list",
    "esxcli software profile get",
    "esxcli system version get",
]
```

---

### Workflow 4 — Emergency Remediation on a Single Host

Target a specific host, push a config fix, and verify it took effect.

```bash
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --host-name "esxi-prod-07" \
    --disable-ssh-after \
    --verbose
```

`COMMANDS_TO_RUN` suggestion:
```python
COMMANDS_TO_RUN = [
    "esxcli system settings advanced set -o /UserVars/SuppressShellWarning -i 1",
    "/etc/init.d/ntpd restart",
    "esxcli system ntp get",
]
```

---

### Workflow 5 — Multi-Cluster Sweep

Run against each environment separately with its own credentials and log file. The exit code makes it easy to detect failures.

```bash
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --cluster "Cluster-Prod" --ssh-password ProdRootPass \
    --disable-ssh-after --log-file C:\Logs\sweep_prod.log

python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --cluster "Cluster-Dev" --ssh-password DevRootPass \
    --disable-ssh-after --log-file C:\Logs\sweep_dev.log
```

---

### Workflow 6 — Maintenance Window SSH Toggle

Open SSH across the cluster before the window, then close it cleanly afterwards. No commands are run — just the service state changes.

```bash
# Before the maintenance window — open SSH on all production hosts
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --cluster "Cluster-Prod" \
    --ssh-only-enable \
    --log-file C:\Logs\maint_ssh_open.log

# ... perform your maintenance work manually or with other tools ...

# After the maintenance window — close SSH on all production hosts
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --cluster "Cluster-Prod" \
    --ssh-only-disable \
    --log-file C:\Logs\maint_ssh_close.log
```

Both steps support `--dry-run`:
```bash
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --cluster "Cluster-Prod" --ssh-only-disable --dry-run
```

---

## 12. Scheduled Execution on Windows

### Using environment variables for credentials

Avoid storing plain-text passwords in Task Scheduler XML by setting user-level environment variables:

```bat
setx VC_PASS "MyVCenterPassword"
setx SSH_PASS "MyRootPassword"
```

Then reference them in the scheduled task action:

```
Program/script:   python
Add arguments:    C:\Scripts\vcenter_esxi_ssh.py
                  -s vcenter.corp.local
                  -u administrator@vsphere.local
                  -p %VC_PASS%
                  --ssh-password %SSH_PASS%
                  --disable-ssh-after
                  --log-file C:\Logs\weekly_audit.log
```

### Batch file wrapper (recommended)

Create a `run_audit.bat` wrapper to handle date-stamped log filenames and exit code checking:

```bat
@echo off
setlocal

set VC_SERVER=vcenter.corp.local
set VC_USER=administrator@vsphere.local
set VC_PASS=MyVCenterPassword
set SSH_PASS=MyRootPassword
set LOG_DIR=C:\Logs
set LOG_FILE=%LOG_DIR%\vcenter_audit_%date:~-4,4%%date:~-7,2%%date:~-10,2%.log

python C:\Scripts\vcenter_esxi_ssh.py ^
    -s %VC_SERVER% ^
    -u %VC_USER% ^
    -p %VC_PASS% ^
    --ssh-password %SSH_PASS% ^
    --disable-ssh-after ^
    --log-file "%LOG_FILE%"

if %errorlevel% neq 0 (
    echo [FAIL] Script reported one or more host failures. Check: %LOG_FILE%
    exit /b 1
)

echo [OK] Audit completed successfully. Log: %LOG_FILE%
```

---

## 13. Troubleshooting

### `NameError: name 'log_file' is not defined`

This occurred in an early version of the script. Ensure you are using the latest version — the fix assigns `log_file` before `setup_logging()` is called.

---

### vCenter connection refused / SSL error

The script disables certificate verification by default to handle self-signed vCenter certificates. If you see a connection error:

- Confirm the hostname/IP is reachable: `ping vcenter.corp.local`
- Confirm the HTTPS port is open: `Test-NetConnection vcenter.corp.local -Port 443` (PowerShell)
- If using a non-standard port, pass `--port <port>`

---

### SSH connection fails on a host

Possible causes:

| Symptom | Likely cause | Fix |
|---|---|---|
| `SSH authentication failed` | Wrong `--ssh-password` or `--ssh-user` | Verify root credentials on the host |
| `SSH connection failed` / timeout | Host management IP not reachable from the machine running the script | Check network routing and firewall rules |
| `SSH service on X did not start within 10s` | Host is under load or the service is locked | Check host events in vCenter; retry |
| Command exit code `-1` | SSH session dropped mid-command | Increase `--ssh-timeout`; check host health |

---

### A host always shows as SKIPPED

Check the connection and power state of the host in vCenter. The script skips hosts that are:

- In `disconnected` or `notResponding` connection state
- Not in `poweredOn` power state
- Not matching the `--cluster` or `--host-name` filters

---

### Commands produce no output

Some ESXi commands return output only when there is something to report (e.g. `esxcli software vib list` on a minimal install). An empty stdout with exit code `0` is not an error.

---

## 14. Security Considerations

**Credentials**

- Never pass passwords on the command line in shared or logged environments — use the interactive prompt (omit `-p`) or environment variables.
- The script does **not** persist credentials anywhere. They exist only in memory for the duration of the run.

**SSL / TLS**

- The script disables certificate verification to support self-signed vCenter certificates. If your vCenter has a valid certificate from an internal CA, you can harden the connection by modifying `connect_vcenter()` in the script to enable certificate verification.

**SSH host key verification**

- The script uses `paramiko.AutoAddPolicy()`, which accepts any SSH host key without verification. This is acceptable for controlled internal environments. If you require strict host key verification, replace `AutoAddPolicy` with `paramiko.RejectPolicy` and pre-populate a `known_hosts` file.

**SSH service state**

- The script is designed to leave the SSH service in the state it found it (unless `--disable-ssh-after` is set). However, if the script is interrupted (e.g. `Ctrl+C`) mid-run, a host's SSH service may remain enabled. Use `--ssh-only-disable` after any interrupted run to clean up.

---

## 15. Architecture Overview

```
vcenter_esxi_ssh.py
│
├── Dependency check (pyVmomi, paramiko, colorama)
│
├── COMMANDS_TO_RUN  ← edit here to customise what runs on each host
│
├── Logging layer
│   ├── Console handler  (coloured, INFO by default / DEBUG with --verbose)
│   └── File handler     (always DEBUG level, timestamped lines)
│
├── vCenter layer  (pyVmomi)
│   ├── connect_vcenter()       SSL connection, accepts self-signed certs
│   ├── get_all_hosts()         ContainerView traversal of rootFolder
│   ├── get_host_info()         name, cluster, management IP, connection/power state
│   ├── is_ssh_running()        reads vim.host.ServiceSystem.serviceInfo
│   ├── enable_ssh()            StartService("TSM-SSH") + 10s polling
│   └── disable_ssh()           StopService("TSM-SSH")
│
├── SSH execution layer  (paramiko)
│   └── run_ssh_commands()      connect → exec_command loop → close
│
├── Argument parser
│   ├── vCenter connection group
│   ├── ESXi SSH options group
│   ├── Filtering group
│   ├── SSH-only modes group    (--ssh-only-enable / --ssh-only-disable)
│   └── Behaviour options group (--disable-ssh-after, --dry-run, --verbose, --log-file)
│
└── main()
    ├── Credential resolution
    ├── Mode detection  (run-commands / ssh-only-enable / ssh-only-disable)
    ├── Banner + configuration summary
    ├── vCenter connect
    ├── Host enumeration + filtering
    ├── Per-host loop
    │   ├── [ssh-only-enable]   enable SSH → continue
    │   ├── [ssh-only-disable]  disable SSH → continue
    │   └── [run-commands]      enable SSH → run commands → conditional disable
    ├── vCenter disconnect
    └── Summary report + exit code
```

---

*Documentation version: corresponds to `vcenter_esxi_ssh.py` as of the last update.*  
*Tested on: vCenter 8.0, ESXi 8.0, Python 3.11, Windows 10/11, Ubuntu 22.04.*
