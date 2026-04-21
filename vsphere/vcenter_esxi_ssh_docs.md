# vsphere/ -- Documentation

Three scripts for automating SSH operations and host commissioning across ESXi fleets.

| Script | Purpose |
|---|---|
| `vcenter_esxi_ssh.py` | Run SSH commands on every ESXi host registered in vCenter |
| `esxi_direct_ssh.py` | Same as above but reads hosts from `hosts.json` -- no vCenter required |
| `generate_hosts_config.py` | Generate a VCF host commissioning JSON file from `hosts.json` |

`vcenter_esxi_ssh.py` and `esxi_direct_ssh.py` share the same operating modes, logging behaviour, SSH disable logic, and `COMMANDS_TO_RUN` list. The only difference is how they discover hosts and how credentials are structured. All console output is ASCII-only and compatible with Windows 10 CMD and PowerShell without any code page changes.

---

## Table of Contents

1. [Requirements](#1-requirements)
2. [Installation](#2-installation)
3. [hosts.json Format](#3-hostsjson-format)
4. [Configuring Commands](#4-configuring-commands)
5. [Operating Modes](#5-operating-modes)
6. [Command Reference -- vcenter_esxi_ssh.py](#6-command-reference----vcenter_esxi_sshpy)
7. [Command Reference -- esxi_direct_ssh.py](#7-command-reference----esxi_direct_sshpy)
8. [Command Reference -- generate_hosts_config.py](#8-command-reference----generate_hosts_configpy)
9. [SSH Disable Logic](#9-ssh-disable-logic)
10. [Credential Prompts](#10-credential-prompts)
11. [Logging](#11-logging)
12. [Host Filtering](#12-host-filtering)
13. [Exit Codes](#13-exit-codes)
14. [Workflow Examples](#14-workflow-examples)
15. [Scheduled Execution on Windows](#15-scheduled-execution-on-windows)
16. [Troubleshooting](#16-troubleshooting)
17. [Security Considerations](#17-security-considerations)
18. [Architecture Overview](#18-architecture-overview)

---

## 1. Requirements

| Requirement | Minimum version | Notes |
|---|---|---|
| Python | 3.8 | Tested on 3.8 -- 3.12 |
| pyVmomi | 8.0.2.0 | vSphere Python SDK -- works against both vCenter and standalone ESXi |
| paramiko | 3.4.0 | SSH client |
| colorama | 0.4.6 | Coloured console output (Windows CMD / PowerShell compatible) |
| vCenter | 7.0+ | Required only for `vcenter_esxi_ssh.py`. Tested on vCenter 8 |
| ESXi | 7.0+ | Tested on ESXi 8 and 9. Management network must be reachable from the machine running the script |

`colorama` is optional. If not installed the scripts still run correctly -- console output is displayed without colours.

`generate_hosts_config.py` requires only the Python standard library (3.6+).

---

## 2. Installation

```
pip install pyVmomi paramiko colorama
```

Or using the requirements file:

```
pip install -r requirements.txt
```

Verify:

```
python vcenter_esxi_ssh.py --help
python esxi_direct_ssh.py --help
python generate_hosts_config.py --help
```

If any required package is missing the SSH scripts exit immediately with a clear message:

```
[ERROR] Missing Python packages: pyVmomi, paramiko
        Install them with:  pip install pyVmomi paramiko
```

---

## 3. hosts.json Format

All three scripts read from the same `hosts.json` file -- a plain JSON array of strings. Each entry is an IP address or FQDN.

```json
[
  "192.168.10.11",
  "192.168.10.12",
  "esx-03a.site-a.vcf.lab",
  "esx-04a.site-a.vcf.lab"
]
```

Rules:

- The file must be a JSON array of strings. No objects, no nested structure.
- Each entry must be a non-empty string.
- IP addresses and FQDNs can be mixed freely in normal operation.
- When `--change-hostname` is used with `esxi_direct_ssh.py`, every entry must be a valid FQDN with at least three dot-separated labels (e.g. `host.domain.tld`). IP addresses and short names are rejected before any host is contacted. See [Section 7](#7-command-reference----esxi_direct_sshpy) for details.

---

## 4. Configuring Commands

Both SSH scripts contain a `COMMANDS_TO_RUN` list near the top of the file. Edit this list to define what runs on every host.

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

When a command contains single quotes (e.g. `awk '{print $NF}'`), wrap the Python string in double quotes. When a command contains double quotes (e.g. `esxcfg-vswitch -p "VM Network" ...`), wrap the Python string in single quotes:

```python
COMMANDS_TO_RUN = [
    "vdq -qH | egrep -o -B5 'Disk in use by disk group' | grep 'Name:' | awk '{print $NF}'",
    'esxcfg-vswitch -p "VM Network" -v 3010 vSwitch0',
]
```

### Useful ESXi command reference

**Inventory & version**
```
esxcli system version get
esxcli hardware cpu global get
esxcli hardware memory get
```

**Networking**
```
esxcli network ip interface list
esxcli network ip route ipv4 list
esxcli network nic list
esxcfg-vswitch -l
```

**Storage**
```
esxcli storage core device list
esxcli storage core adapter list
vdq -qH
```

**NTP & time**
```
esxcli system ntp get
esxcli system ntp set -e=no
esxcli system ntp set -s=10.0.0.1 -s=10.0.0.2
esxcli system ntp set -e=yes
/etc/init.d/ntpd status
```

**Security & compliance**
```
esxcli system settings advanced list -o /UserVars/ESXiShellInteractiveTimeOut
esxcli system settings advanced list -o /UserVars/SuppressShellWarning
cat /etc/vmware/config
```

**Software & patching**
```
esxcli software vib list
esxcli software profile get
```

**Certificates**
```
/sbin/generate-certificates
```

**Syslog**
```
esxcli system syslog config get
```

**VM inventory**
```
vim-cmd vmsvc/getallvms
```

---

## 5. Operating Modes

Both SSH scripts support the same three mutually exclusive modes.

### Mode 1 -- Run Commands (default)

Enables SSH on each host, opens an SSH session, runs every command, then optionally disables SSH.

**Per-host steps for vcenter_esxi_ssh.py:**
```
1. Record current SSH service state
2. Enable SSH via the vCenter API  (if not already running)
3. Connect via SSH and run every command in COMMANDS_TO_RUN
4. Disconnect SSH
5. Disable SSH if appropriate  (see SSH Disable Logic)
```

**Per-host steps for esxi_direct_ssh.py:**
```
1. Connect to ESXi SOAP API directly
2. Record current SSH service state
3. Enable SSH  (if not already running)
4. [if --change-hostname]  esxcli system hostname set --fqdn=<host>
5. Connect via SSH and run every command in COMMANDS_TO_RUN
6. Disconnect SSH
7. Disable SSH if appropriate  (see SSH Disable Logic)
8. Disconnect SOAP API
```

The hostname set command runs before `COMMANDS_TO_RUN` so that any subsequent commands in the list operate on the host under its new identity.

### Mode 2 -- SSH Only Enable (--ssh-only-enable)

Enables the SSH service on every matched host via the SOAP API. No SSH session is opened and no commands are run. If SSH is already running the host is recorded as `OK` with a note -- not an error.

### Mode 3 -- SSH Only Disable (--ssh-only-disable)

Disables the SSH service on every matched host via the SOAP API. No SSH session is opened and no commands are run. If SSH is already stopped the host is recorded as `OK` with a note.

### Mode compatibility

| Flag combination | Result |
|---|---|
| (no mode flag) | Run Commands mode |
| `--ssh-only-enable` | SSH Only Enable mode |
| `--ssh-only-disable` | SSH Only Disable mode |
| `--ssh-only-enable --ssh-only-disable` | Rejected -- mutually exclusive |
| `--ssh-only-enable --disable-ssh-after` | Rejected |
| `--ssh-only-disable --disable-ssh-after` | Rejected |
| `--change-hostname --ssh-only-enable` | Rejected -- no SSH session in ssh-only modes |
| `--change-hostname --ssh-only-disable` | Rejected -- no SSH session in ssh-only modes |

---

## 6. Command Reference -- vcenter_esxi_ssh.py

### vCenter Connection

| Parameter | Short | Required | Default | Description |
|---|---|---|---|---|
| `--server` | `-s` | Yes | -- | vCenter hostname or IP address |
| `--user` | `-u` | Yes | -- | vCenter login (e.g. `administrator@vsphere.local`) |
| `--password` | `-p` | No | prompted | vCenter password |
| `--port` | | No | `443` | vCenter HTTPS port |

### ESXi SSH Options

Only apply in Run Commands mode. Ignored in `--ssh-only-*` modes.

| Parameter | Default | Description |
|---|---|---|
| `--ssh-user` | `root` | SSH username on ESXi hosts |
| `--ssh-password` | prompted | SSH password. Prompted separately from the vCenter password. Press Enter to reuse the vCenter password. |
| `--ssh-port` | `22` | TCP port for SSH on ESXi hosts |
| `--ssh-timeout` | `30` | Timeout in seconds for the SSH connection and each individual command |

### Filtering

All filters apply in every mode. Hosts that do not match are listed in the summary as `SKIPPED` and do not affect the exit code.

| Parameter | Default | Description |
|---|---|---|
| `--cluster` | all | Case-insensitive substring match against the cluster name. Standalone hosts not in any cluster are excluded when this filter is active. |
| `--host-name` | all | Case-insensitive substring match against the host's registered name |
| `--skip-disconnected` | on | Skip hosts in `disconnected` or `notResponding` connection state. Powered-off hosts are always skipped regardless of this flag. |

> Substring matching: `--cluster "Prod"` matches `Cluster-Prod-A`, `Prod-Cluster`, `Cluster-Prod-B`, etc.

### SSH-Only Modes

| Parameter | Description |
|---|---|
| `--ssh-only-enable` | Enable SSH on every matched host and exit. No SSH commands are run. Incompatible with `--disable-ssh-after`. |
| `--ssh-only-disable` | Disable SSH on every matched host and exit. No SSH commands are run. Incompatible with `--disable-ssh-after`. |

### Behaviour Options

| Parameter | Default | Description |
|---|---|---|
| `--disable-ssh-after` | off | Disable SSH after running commands, even if SSH was already running before the script started. |
| `--dry-run` | off | Simulate all actions without making any changes. Prints `[DRY-RUN]` prefixed lines. |
| `--verbose` | off | Print DEBUG-level output to the console. Debug output is always written to the log file regardless of this flag. |
| `--log-file` | `vcenter_esxi_ssh_YYYYMMDD_HHMMSS.log` | Log file path. Directory is created automatically if it does not exist. |
| `--no-log-file` | off | Disable log file creation. Write to console only. |

---

## 7. Command Reference -- esxi_direct_ssh.py

### Host List

| Parameter | Short | Default | Description |
|---|---|---|---|
| `--config` | `-c` | `hosts.json` | Path to the JSON host list file |

### ESXi API Credentials

Used to connect to the ESXi built-in SOAP API on each host to start and stop the SSH service.

| Parameter | Short | Required | Default | Description |
|---|---|---|---|---|
| `--user` | `-u` | Yes | -- | ESXi username (typically `root`) |
| `--password` | `-p` | No | prompted | ESXi SOAP API password |
| `--port` | | No | `443` | ESXi HTTPS API port |

### SSH Options

Only apply in Run Commands mode. Ignored in `--ssh-only-*` modes.

| Parameter | Default | Description |
|---|---|---|
| `--ssh-user` | `root` | SSH username on ESXi hosts |
| `--ssh-password` | prompted | SSH password. Prompted separately from the API password. Press Enter to reuse the API password. |
| `--ssh-port` | `22` | TCP port for SSH |
| `--ssh-timeout` | `30` | Timeout in seconds for the SSH connection and each individual command |

### Filtering

| Parameter | Default | Description |
|---|---|---|
| `--host-name` | all | Case-insensitive substring match against the IP or FQDN string from `hosts.json` |

### SSH-Only Modes

| Parameter | Description |
|---|---|
| `--ssh-only-enable` | Enable SSH on every matched host and exit. No SSH commands are run. |
| `--ssh-only-disable` | Disable SSH on every matched host and exit. No SSH commands are run. |

### Behaviour Options

| Parameter | Default | Description |
|---|---|---|
| `--change-hostname` | off | Before running `COMMANDS_TO_RUN`, execute `esxcli system hostname set --fqdn=<host>` on each host via SSH. The FQDN value is taken directly from `hosts.json`. Requires all entries to be valid FQDNs. Validated even in `--dry-run`. Incompatible with `--ssh-only-*` modes. |
| `--disable-ssh-after` | off | Disable SSH after running commands, even if SSH was already running before the script started. |
| `--dry-run` | off | Simulate all actions without making any changes. FQDN validation (for `--change-hostname`) still runs and will abort on invalid entries. |
| `--verbose` | off | Print DEBUG-level output to the console. |
| `--log-file` | `esxi_direct_ssh_YYYYMMDD_HHMMSS.log` | Log file path. Directory is created automatically. |
| `--no-log-file` | off | Disable log file creation. Write to console only. |

### FQDN Validation for --change-hostname

When `--change-hostname` is active the script validates every entry in `hosts.json` before contacting any host. An entry is accepted as a valid FQDN when:

- It contains at least three dot-separated labels (minimum: `host.domain.tld`)
- It is not an IPv4 address
- Each label contains only `a-z`, `A-Z`, `0-9`, and hyphens, and does not start or end with a hyphen
- The rightmost label (TLD) is at least two characters long
- Total length does not exceed 253 characters

Examples:

```
[X]  192.168.10.11           IP address -- use a fully-qualified hostname
[X]  esxi-hostname           no domain part -- add the domain suffix
[X]  esx-01.site-a           only 2 labels -- need at least host.domain.tld
[OK] esx-01.site-a.vcf.lab
[OK] x00-w01-esx01.infra.pcr.cz
[OK] esxi-03.corp.local
```

If any entry fails validation the script prints a clear error for each invalid entry and exits before connecting to any host.

---

## 8. Command Reference -- generate_hosts_config.py

Reads `hosts.json` and generates a host commissioning JSON file in the format expected by VMware Cloud Foundation (VCF). All parameters (username, password, network pool, storage type) are entered interactively and applied identically to every host.

VCF accepts a maximum of 50 hosts per commissioning operation. When `hosts.json` contains more than 50 entries the output is automatically split into multiple files (e.g. `hosts_config_part01.json`, `hosts_config_part02.json`, ...).

**Usage:**

```
# Defaults: reads ./hosts.json, writes ./hosts_config.json
python generate_hosts_config.py

# Custom paths
python generate_hosts_config.py -i my_hosts.json -o my_output.json
```

**Options:**

| Parameter | Short | Default | Description |
|---|---|---|---|
| `--input` | `-i` | `hosts.json` | Input file path. When the default is used the file is located relative to the script directory, not the current working directory. |
| `--output` | `-o` | `hosts_config.json` | Output file path |

**Interactive prompts:**

```
Username:              (applied to every host)
Password:              (hidden input, confirmed)
Confirm password:
Network Pool Name:
Select storage type:
  1) VSAN
  2) VSAN_REMOTE
  3) VSAN_ESA
  4) VSAN_MAX
  5) NFS
  6) VMFS_FC
  7) VVOL
Select vVol storage protocol type:   (shown only when VVOL is selected)
  1) VMFS_FC
  2) ISCSI
  3) NFS
```

**Output format -- non-VVOL:**

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

**Output format -- VVOL:**

```json
{
    "hosts": [
        {
            "fqdn": "esx-01a.site-a.vcf.lab",
            "username": "root",
            "storageType": "VVOL",
            "password": "...",
            "networkPoolName": "sfo-m01-np01",
            "vvolStorageProtocolType": "VMFS_FC"
        }
    ]
}
```

**Exit codes:**

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Input file not found or invalid JSON |
| `2` | Invalid user input or user aborted (Ctrl+C) |

**Dependencies:** stdlib only (Python 3.6+)

---

## 9. SSH Disable Logic

The SSH service state on each host is recorded before the script makes any changes. The same logic applies to both SSH scripts.

| SSH state before run | `--disable-ssh-after` set | SSH state after run |
|---|---|---|
| Stopped | No | Stopped -- script turns it on for the session then turns it back off |
| Stopped | Yes | Stopped |
| Running | No | Running -- left as found |
| Running | Yes | Stopped |

The key principle: if the script turned SSH on, it turns it back off. If SSH was already on and `--disable-ssh-after` is not set, it is left running.

If the script is interrupted mid-run (`Ctrl+C`, power loss), some hosts may be left with SSH enabled. Run `--ssh-only-disable` afterwards to restore the expected state across the entire fleet.

### SSH-only modes

`--ssh-only-enable` and `--ssh-only-disable` make a single explicit change and do not apply restore logic. The state after the script finishes is exactly what the mode name says.

---

## 10. Credential Prompts

Both scripts prompt interactively for any password not provided on the command line. Prompts appear before any host is contacted.

### vcenter_esxi_ssh.py

```
vCenter password for 'administrator@vsphere.local': ****
SSH password for 'root' on ESXi hosts
(leave blank to reuse the vCenter password): ****
```

Pressing Enter at the SSH password prompt reuses the vCenter password for SSH. This is shown explicitly in the output:

```
--> No SSH password entered, reusing vCenter password for SSH.
```

In `--ssh-only-*` modes, no SSH session is opened, so the SSH password is not prompted.

### esxi_direct_ssh.py

```
ESXi API password for 'root': ****
SSH password for 'root' on ESXi hosts
(leave blank to reuse the API password): ****
```

Pressing Enter at the SSH password prompt reuses the API password for SSH.

In `--ssh-only-*` modes, no SSH session is opened, so the SSH password is not prompted.

---

## 11. Logging

Both scripts write two independent log streams simultaneously.

### Console output

Colour-coded by severity when `colorama` is installed:

| Level | Colour | When visible |
|---|---|---|
| DEBUG (cyan) | Only with `--verbose` | SOAP connection details, per-service state checks |
| INFO (green) | Always | Normal progress -- host headers, command output, state changes |
| WARNING (yellow) | Always | Non-fatal issues -- host skipped, SSH did not start within 10s, non-zero exit code |
| ERROR (red) | Always | Recoverable failures -- SSH auth failed, command exception |
| CRITICAL (magenta) | Always | Fatal errors -- API connection refused; script exits immediately |

Console output is ASCII-only and works in Windows 10 CMD and PowerShell without any code page changes.

### Log file

The log file always receives all levels including DEBUG, regardless of `--verbose`.

```
2026-04-16 15:22:56  [INFO    ]  [1/5]  HOST : esx-01a.site-a.vcf.lab
2026-04-16 15:22:57  [DEBUG   ]         SOAP connect -> esx-01a.site-a.vcf.lab:443
2026-04-16 15:22:57  [DEBUG   ]         ESXi SOAP API: VMware ESXi 9.0.2.0  (build 25148076)
2026-04-16 15:22:57  [INFO    ]    -->  SSH service: STOPPED
2026-04-16 15:22:58  [DEBUG   ]         SSH service started on esx-01a.site-a.vcf.lab
2026-04-16 15:22:59  [INFO    ]    -->  Running 3 command(s) via SSH ...
2026-04-16 15:23:00  [INFO    ]         [OK]   (exit 0) $ localcli ... getmemconfig ...
```

Default filenames:
- `vcenter_esxi_ssh_YYYYMMDD_HHMMSS.log`
- `esxi_direct_ssh_YYYYMMDD_HHMMSS.log`

Custom path: `--log-file C:\Logs\esxi_audit.log`

Disable log file: `--no-log-file`

---

## 12. Host Filtering

### vcenter_esxi_ssh.py

Filters are applied in this order. A host is skipped as soon as it fails any check.

```
1. --host-name  substring match
2. connection_state: disconnected / notResponding
3. power_state: not poweredOn
```

Skipped hosts appear in the summary as `[SKIP]` and do not affect the exit code.

Examples:

```
--cluster "Prod"                               all clusters containing "Prod"
--host-name "esx-01a"                          single host by name fragment
--cluster "Prod" --host-name "esx-01"          both filters must match
```

### esxi_direct_ssh.py

The only filter is `--host-name`, matched case-insensitively against the IP or FQDN string in `hosts.json`.

```
--host-name "192.168.10.11"    single host by IP
--host-name "site-a"           all hosts whose address contains "site-a"
```

---

## 13. Exit Codes

Both SSH scripts return the same exit codes.

| Code | Meaning |
|---|---|
| `0` | All processed hosts completed successfully. Skipped hosts do not count as failures. |
| `1` | One or more hosts reported `FAILED` status. |

`generate_hosts_config.py` uses codes `0`, `1`, and `2` (see Section 8).

Using the exit code in a batch script:

```bat
python esxi_direct_ssh.py -u root -p %ESXI_PASS% --disable-ssh-after
if %errorlevel% neq 0 (
    echo Script failed -- check the log file
    exit /b 1
)
```

---

## 14. Workflow Examples

### Workflow 1 -- Audit (read-only, all hosts)

Always dry-run first to confirm scope, then run for real.

```
# vcenter version
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local --dry-run
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --log-file C:\Logs\audit.log

# direct version
python esxi_direct_ssh.py -u root --dry-run
python esxi_direct_ssh.py -u root --log-file C:\Logs\audit.log
```

### Workflow 2 -- Apply a Configuration Change

The current `COMMANDS_TO_RUN` reads the memory config, applies the change, then reads again to confirm. Disable SSH afterwards.

```
# vcenter version
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --ssh-user root --disable-ssh-after --log-file C:\Logs\memconfig.log

# direct version
python esxi_direct_ssh.py -u root --disable-ssh-after --log-file C:\Logs\memconfig.log
```

### Workflow 3 -- Target a Single Host

```
# vcenter version
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --host-name "esx-01a" --disable-ssh-after --verbose

# direct version
python esxi_direct_ssh.py -u root --host-name "esx-01a" --disable-ssh-after --verbose
```

### Workflow 4 -- Set Hostname and Run Commands

hosts.json must contain valid FQDNs. Dry-run first to validate without touching any host.

```
# Validate FQDNs without connecting to anything
python esxi_direct_ssh.py -u root --change-hostname --dry-run

# Run for real: set hostname first, then COMMANDS_TO_RUN
python esxi_direct_ssh.py -u root --change-hostname --disable-ssh-after \
    --log-file C:\Logs\hostname_change.log
```

### Workflow 5 -- Maintenance Window SSH Toggle

Open SSH before the window, close it cleanly afterwards. No commands are run.

```
# Open SSH
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --cluster "Cluster-Prod" --ssh-only-enable --log-file C:\Logs\ssh_open.log

python esxi_direct_ssh.py -u root --ssh-only-enable --log-file C:\Logs\ssh_open.log

# ... perform maintenance ...

# Close SSH
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --cluster "Cluster-Prod" --ssh-only-disable --log-file C:\Logs\ssh_close.log

python esxi_direct_ssh.py -u root --ssh-only-disable --log-file C:\Logs\ssh_close.log
```

Both support `--dry-run`:

```
python esxi_direct_ssh.py -u root --ssh-only-disable --dry-run
```

### Workflow 6 -- Multi-Cluster Sweep (vcenter_esxi_ssh.py)

Run against each environment with its own credentials and log file.

```
python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --cluster "Cluster-Prod" --ssh-password ProdRootPass \
    --disable-ssh-after --log-file C:\Logs\sweep_prod.log

python vcenter_esxi_ssh.py -s vcenter.corp.local -u administrator@vsphere.local \
    --cluster "Cluster-Dev" --ssh-password DevRootPass \
    --disable-ssh-after --log-file C:\Logs\sweep_dev.log
```

### Workflow 7 -- Generate VCF Commissioning File

```
# Edit hosts.json with the target FQDNs, then:
python generate_hosts_config.py

# Custom paths
python generate_hosts_config.py -i site-a-hosts.json -o site-a-commissioning.json
```

---

## 15. Scheduled Execution on Windows

### Store credentials in environment variables

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
set LOG_FILE=C:\Logs\vcenter_%date:~-4,4%%date:~-7,2%%date:~-10,2%.log

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

### esxi_direct_ssh.py with --change-hostname batch wrapper

```bat
@echo off
setlocal

set LOG_FILE=C:\Logs\hostname_set_%date:~-4,4%%date:~-7,2%%date:~-10,2%.log

python C:\Scripts\esxi_direct_ssh.py ^
    -u root ^
    -p %ESXI_PASS% ^
    --ssh-password %ESXI_PASS% ^
    --change-hostname ^
    --disable-ssh-after ^
    --log-file "%LOG_FILE%"

if %errorlevel% neq 0 (
    echo [FAIL] Check: %LOG_FILE%
    exit /b 1
)
echo [OK] Log: %LOG_FILE%
```

---

## 16. Troubleshooting

### `Failed to start SSH on <host>: host`

Occurs with `esxi_direct_ssh.py` when connecting directly to an ESXi host (no vCenter). The pyVmomi object tree on a direct ESXi connection includes a `vim.Datacenter` layer named `ha-datacenter` that is absent when connecting through vCenter. The correct navigation path is:

```
rootFolder
  -> childEntity[0]  (vim.Datacenter "ha-datacenter")
       -> hostFolder
            -> childEntity[0]  (vim.ComputeResource)
                 -> host[0]  (vim.HostSystem)
                      -> configManager.serviceSystem
```

This is handled correctly in the current version of the script.

---

### `--change-hostname` rejected entries that look correct

The FQDN validator requires at least three dot-separated labels. A name like `esx-01.site-a` has only two labels and is rejected even though it appears valid. Add the full domain suffix:

```
[X]  esx-01.site-a          2 labels -- rejected
[OK] esx-01.site-a.vcf.lab  4 labels -- accepted
```

---

### API connection refused / SSL error

Both scripts disable certificate verification by default to support self-signed certificates on ESXi and vCenter.

- Confirm the host is reachable: `ping <host>`
- Confirm port 443 is open: `Test-NetConnection <host> -Port 443` (PowerShell)
- If using a non-standard port: `--port <port>`

---

### SSH connection fails

| Symptom | Likely cause | Fix |
|---|---|---|
| `SSH authentication failed` | Wrong `--ssh-password` or `--ssh-user` | Verify root credentials |
| `SSH connection failed` / timeout | Management IP not reachable from this machine | Check routing and firewall |
| `SSH service did not start within 10s` | Host under load or SSH service locked | Check host health in vCenter; retry |
| Command exit code `-1` | SSH session dropped mid-command | Increase `--ssh-timeout` |

---

### A host always shows as FAILED in esxi_direct_ssh.py

- Confirm the IP or FQDN in `hosts.json` is correct and reachable on port 443
- Confirm the ESXi API user and password are correct
- Run with `--verbose` to see the full SOAP connection error in the console

---

### A host always shows as SKIPPED in vcenter_esxi_ssh.py

Check the host's connection and power state in vCenter. The script skips hosts that are `disconnected`, `notResponding`, or not `poweredOn`, and hosts excluded by `--cluster` or `--host-name` filters.

---

### Commands produce no output

Some ESXi commands return output only when there is something to report. An empty stdout with exit code `0` is not an error.

---

### `reboot` in COMMANDS_TO_RUN shows exit code -1 in the log

The reboot command terminates the SSH connection before Paramiko can read the exit code. The host still reboots correctly. To suppress the error in the log, call reboot as a background process so the SSH session closes cleanly first:

```python
"nohup reboot &"
```

---

## 17. Security Considerations

**Credentials**

Never pass passwords on the command line in shared or logged environments. Use the interactive prompt (omit `-p` / `--ssh-password`) or environment variables set at the user level. Neither script persists credentials -- they exist in memory only for the duration of the run.

**SSL / TLS**

Both scripts disable certificate verification by default to support self-signed certificates. To enable verification for a known CA, modify the `ssl.SSLContext` block in the connect function:

```python
ctx.check_hostname = True
ctx.verify_mode    = ssl.CERT_REQUIRED
ctx.load_verify_locations("/path/to/ca-bundle.crt")
```

**SSH host key verification**

Both scripts use `paramiko.AutoAddPolicy()`, which accepts any SSH host key without verification. This is acceptable for controlled internal environments. For stricter environments replace `AutoAddPolicy` with `paramiko.RejectPolicy` and pre-populate a `known_hosts` file.

**SSH service state after interruption**

If the script is interrupted (`Ctrl+C`, power loss) mid-run, some hosts may be left with SSH enabled. Run `--ssh-only-disable` afterwards to restore the expected state across the entire fleet.

**generate_hosts_config.py password handling**

The script writes the password in plaintext into the output JSON file. This is required by the VCF commissioning API. Apply appropriate filesystem permissions to the output file and delete it once the commissioning operation is complete.

---

## 18. Architecture Overview

### vcenter_esxi_ssh.py

```
vcenter_esxi_ssh.py
|
+-- COMMANDS_TO_RUN              edit to customise what runs on each host
|
+-- Logging layer
|   +-- Console handler          ASCII, coloured when colorama installed
|   +-- File handler             always DEBUG level, timestamped lines
|
+-- vCenter layer  (pyVmomi)
|   +-- connect_vcenter()        SSL connection to vCenter
|   +-- get_all_hosts()          ContainerView traversal of rootFolder
|   +-- get_host_info()          name, cluster, management IP, state
|   +-- is_ssh_running()         reads vim.host.ServiceSystem.serviceInfo
|   +-- enable_ssh()             StartService("TSM-SSH") + 10s polling
|   +-- disable_ssh()            StopService("TSM-SSH")
|
+-- SSH layer  (paramiko)
|   +-- run_ssh_commands()       connect -> exec_command loop -> close
|
+-- Argument parser
|   +-- vCenter connection group
|   +-- ESXi SSH options group
|   +-- Filtering group          --cluster, --host-name, --skip-disconnected
|   +-- SSH-only modes group     --ssh-only-enable / --ssh-only-disable
|   +-- Behaviour options group  --disable-ssh-after, --dry-run, --verbose, --log-file
|
+-- main()
    +-- Credential prompts       vCenter password then SSH password (separately)
    +-- Mode detection           run-commands / ssh-only-enable / ssh-only-disable
    +-- Banner + config summary
    +-- vCenter connect
    +-- Host enumeration + filtering
    +-- Per-host loop
    |   +-- [ssh-only-enable]    enable SSH -> continue
    |   +-- [ssh-only-disable]   disable SSH -> continue
    |   +-- [run-commands]       enable SSH -> COMMANDS_TO_RUN -> conditional disable
    +-- Summary report + exit code
```

### esxi_direct_ssh.py

```
esxi_direct_ssh.py
|
+-- COMMANDS_TO_RUN              same list, edit in the same way
|
+-- Logging layer                identical to vcenter_esxi_ssh.py
|
+-- FQDN validator
|   +-- is_valid_fqdn()          regex-based: 3+ labels, no IPs, valid chars
|   +-- validate_fqdn_hosts()    checks all hosts, reports each invalid entry
|
+-- hosts.json loader
|   +-- load_hosts()             reads a plain JSON array of IP/FQDN strings
|
+-- Direct ESXi SOAP layer  (pyVmomi -- no vCenter)
|   +-- connect_esxi()           SmartConnect directly to ESXi host
|   +-- _svc_system()            rootFolder -> Datacenter (ha-datacenter)
|   |                              -> hostFolder -> ComputeResource
|   |                                -> host[0] -> configManager.serviceSystem
|   +-- is_ssh_running()         reads vim.host.ServiceSystem.serviceInfo
|   +-- enable_ssh()             StartService("TSM-SSH") + 10s polling
|   +-- disable_ssh()            StopService("TSM-SSH")
|
+-- SSH layer  (paramiko)        identical to vcenter_esxi_ssh.py
|
+-- Argument parser
|   +-- Host list group          --config
|   +-- ESXi API credentials     -u, -p, --port
|   +-- SSH options group
|   +-- Filtering group          --host-name
|   +-- SSH-only modes group     --ssh-only-enable / --ssh-only-disable
|   +-- Behaviour options group  --change-hostname, --disable-ssh-after,
|                                  --dry-run, --verbose, --log-file
|
+-- main()
    +-- Flag compatibility check  rejects invalid combinations
    +-- Credential prompts        API password then SSH password (separately)
    +-- Mode detection
    +-- Banner + config summary
    +-- Load hosts.json + apply --host-name filter
    +-- FQDN validation           (when --change-hostname; runs even in --dry-run)
    +-- Per-host loop
    |   +-- connect_esxi()        per-host SOAP connection (not a shared session)
    |   +-- [ssh-only-enable]     enable SSH -> disconnect SOAP -> continue
    |   +-- [ssh-only-disable]    disable SSH -> disconnect SOAP -> continue
    |   +-- [run-commands]        enable SSH
    |                              -> [--change-hostname] hostname set command
    |                              -> COMMANDS_TO_RUN
    |                              -> conditional disable
    |                              -> disconnect SOAP
    +-- Summary report + exit code
```

### generate_hosts_config.py

```
generate_hosts_config.py
|
+-- load_fqdns()              reads hosts.json, validates array of strings
+-- prompt_non_empty()        interactive text input with empty-check loop
+-- prompt_password()         getpass with confirmation, works on Windows CMD
+-- prompt_choice()           numbered menu for enumerated options
+-- build_host_entries()      applies shared params to each FQDN
+-- chunk_list()              splits list into chunks of max 50
+-- build_chunked_output_paths()  inserts _partNN suffix when needed
+-- write_output()            JSON dump with trailing newline
+-- main()
    +-- Argument parsing      --input / --output
    +-- Load FQDNs
    +-- Interactive prompts   username, password, network pool, storage type,
    |                          vvol protocol (only when VVOL selected)
    +-- Build host entries
    +-- Split into chunks of max 50
    +-- Write output file(s)
    +-- Print success summary
```

---

*Tested on: vCenter 8.0, ESXi 8.0 / 9.0, Python 3.11, Windows 10/11, Ubuntu 22.04.*
