# vsphere/ -- Documentation

Five scripts for automating SSH operations, local datastore management, TPM recovery key export, and host commissioning across ESXi fleets.

| Script | Purpose |
|---|---|
| `vcenter_esxi_ssh.py` | Run SSH commands on every ESXi host registered in vCenter |
| `esxi_direct_ssh.py` | Same as above but reads hosts from `hosts.json` -- no vCenter required |
| `vcenter_rename_local_datastores.py` | Rename local VMFS datastores across clusters and vCenter instances |
| `vcenter_export_tpm_keys.py` | Export TPM encryption recovery keys via SSH |
| `generate_hosts_config.py` | Generate a VCF host commissioning JSON file from `hosts.json` |

`vcenter_esxi_ssh.py` and `esxi_direct_ssh.py` share the same operating modes, logging behaviour, SSH disable logic, and `COMMANDS_TO_RUN` list. All console output is ASCII-only and compatible with Windows 10 CMD and PowerShell without any code page changes. Passwords are never echoed to the terminal.

---

## Table of Contents

1. [Requirements](#1-requirements)
2. [Installation](#2-installation)
3. [hosts.json Format](#3-hostsjson-format)
4. [Configuring Commands](#4-configuring-commands)
5. [Operating Modes -- SSH Scripts](#5-operating-modes----ssh-scripts)
6. [Command Reference -- vcenter_esxi_ssh.py](#6-command-reference----vcenter_esxi_sshpy)
7. [Command Reference -- esxi_direct_ssh.py](#7-command-reference----esxi_direct_sshpy)
8. [Command Reference -- vcenter_rename_local_datastores.py](#8-command-reference----vcenter_rename_local_datastorespy)
9. [Command Reference -- vcenter_export_tpm_keys.py](#9-command-reference----vcenter_export_tpm_keyspy)
10. [Command Reference -- generate_hosts_config.py](#10-command-reference----generate_hosts_configpy)
11. [SSH Disable Logic](#11-ssh-disable-logic)
12. [Credential Prompts](#12-credential-prompts)
13. [Logging](#13-logging)
14. [Host Filtering](#14-host-filtering)
15. [Exit Codes](#15-exit-codes)
16. [Workflow Examples](#16-workflow-examples)
17. [Scheduled Execution on Windows](#17-scheduled-execution-on-windows)
18. [Troubleshooting](#18-troubleshooting)
19. [Security Considerations](#19-security-considerations)
20. [Architecture Overview](#20-architecture-overview)

---

## 1. Requirements

| Requirement | Minimum version | Notes |
|---|---|---|
| Python | 3.8 | Tested on 3.8 -- 3.12 |
| pyVmomi | 8.0.2.0 | vSphere Python SDK -- works against both vCenter and standalone ESXi |
| paramiko | 3.4.0 | SSH client (SSH scripts only) |
| colorama | 0.4.6 | Coloured console output (Windows CMD / PowerShell compatible, optional) |
| vCenter | 7.0+ | Required for `vcenter_esxi_ssh.py` and `vcenter_rename_local_datastores.py`. Tested on vCenter 8 and 9. |
| ESXi | 7.0+ | Tested on ESXi 8 and 9 |

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
python vcenter_rename_local_datastores.py --help
python generate_hosts_config.py --help
```

If any required package is missing the scripts exit immediately with a clear message:

```
[ERROR] Missing packages: pyVmomi
        Install with:  pip install pyVmomi
```

---

## 3. hosts.json Format

`esxi_direct_ssh.py` and `generate_hosts_config.py` read their host list from a plain JSON array of strings. Each entry is an IP address or FQDN.

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
- IP addresses and FQDNs can be mixed freely in normal operation.
- When `--change-hostname` is used with `esxi_direct_ssh.py`, every entry must be a valid FQDN with at least three dot-separated labels. IP addresses and short names without a domain suffix are rejected before any host is contacted.

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

When a command contains single quotes wrap the Python string in double quotes. When a command contains double quotes wrap the Python string in single quotes:

```python
COMMANDS_TO_RUN = [
    "vdq -qH | egrep -o -B5 'Disk in use by disk group' | grep 'Name:' | awk '{print $NF}'",
    'esxcfg-vswitch -p "VM Network" -v 3010 vSwitch0',
]
```

Note on `reboot`: Paramiko waits for the exit code after each command. A `reboot` terminates the SSH session before the code can be read, causing a logged error. The host still reboots. To avoid the error in the log call reboot as a background job:

```python
"nohup reboot &"
```

---

## 5. Operating Modes -- SSH Scripts

Both SSH scripts support the same three mutually exclusive modes.

### Mode 1 -- Run Commands (default)

Enables SSH on each host via the SOAP API, opens an SSH session, runs every command in `COMMANDS_TO_RUN`, then optionally disables SSH.

**Per-host steps for vcenter_esxi_ssh.py:**

```
1. Record current SSH service state
2. Enable SSH via the vCenter API  (if not already running)
3. Connect via SSH and run every command in COMMANDS_TO_RUN
4. Disconnect SSH
5. Disable SSH if appropriate
```

**Per-host steps for esxi_direct_ssh.py:**

```
1. Connect to ESXi SOAP API directly (per-host connection)
2. Record current SSH service state
3. Enable SSH  (if not already running)
4. [if --change-hostname]  esxcli system hostname set --fqdn=<host>
5. Connect via SSH and run every command in COMMANDS_TO_RUN
6. Disconnect SSH
7. Disable SSH if appropriate
8. Disconnect SOAP API
```

The hostname set command runs before `COMMANDS_TO_RUN` so that subsequent commands operate on the host under its new identity.

### Mode 2 -- SSH Only Enable (--ssh-only-enable)

Enables the SSH service on every matched host via the SOAP API. No SSH session is opened. If SSH is already running the host is recorded as OK -- not an error.

### Mode 3 -- SSH Only Disable (--ssh-only-disable)

Disables the SSH service on every matched host via the SOAP API. No SSH session is opened. If SSH is already stopped the host is recorded as OK.

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
| `--change-hostname --ssh-only-disable` | Rejected |

---

## 6. Command Reference -- vcenter_esxi_ssh.py

### vCenter Connection

| Parameter | Short | Required | Default | Description |
|---|---|---|---|---|
| `--server` | `-s` | Yes | -- | vCenter hostname or IP address |
| `--user` | `-u` | Yes | -- | vCenter login (e.g. `administrator@vsphere.local`) |
| `--password` | `-p` | No | prompted | vCenter password. Never echoed to the terminal. |
| `--port` | | No | `443` | vCenter HTTPS port |

### ESXi SSH Options

Only apply in Run Commands mode.

| Parameter | Default | Description |
|---|---|---|
| `--ssh-user` | `root` | SSH username on ESXi hosts |
| `--ssh-password` | prompted | SSH password. Prompted separately. Press Enter to reuse the vCenter password. |
| `--ssh-port` | `22` | TCP port for SSH on ESXi hosts |
| `--ssh-timeout` | `30` | Timeout in seconds for the SSH connection and each individual command |

### Filtering

Hosts that do not match are listed in the summary as SKIPPED and do not affect the exit code.

| Parameter | Default | Description |
|---|---|---|
| `--cluster` | all | Case-insensitive substring match against the cluster name. `--cluster "Prod"` matches `Cluster-Prod-A`, `Prod-Cluster`, etc. |
| `--host-name` | all | Case-insensitive substring match against the host's registered name |
| `--skip-disconnected` | on | Skip hosts in `disconnected` or `notResponding` connection state. Powered-off hosts are always skipped. |

### SSH-Only Modes

| Parameter | Description |
|---|---|
| `--ssh-only-enable` | Enable SSH on every matched host and exit. No commands are run. Incompatible with `--disable-ssh-after`. |
| `--ssh-only-disable` | Disable SSH on every matched host and exit. No commands are run. Incompatible with `--disable-ssh-after`. |

### Behaviour Options

| Parameter | Default | Description |
|---|---|---|
| `--disable-ssh-after` | off | Disable SSH after running commands, even if SSH was already running before the script started. |
| `--dry-run` | off | Simulate all actions without making any changes. Prints `[DRY-RUN]` prefixed lines. |
| `--verbose` | off | Print DEBUG-level output to the console. Debug output is always written to the log file. |
| `--log-file` | `vcenter_esxi_ssh_YYYYMMDD_HHMMSS.log` | Log file path. Directory is created automatically if it does not exist. |
| `--no-log-file` | off | Disable log file creation. Write to console only. |

### Example invocations

```
# Dry-run across all hosts
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local --dry-run

# Run in one cluster, disable SSH afterwards, save log
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
    --cluster "Cluster-Prod" --disable-ssh-after \
    --log-file C:\Logs\esxi_cmd.log

# Target a single host by name
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
    --host-name "esx-prod-07" --disable-ssh-after --verbose

# Open SSH across a cluster (no commands run)
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
    --cluster "Cluster-Prod" --ssh-only-enable

# Close SSH across a cluster (no commands run)
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
    --cluster "Cluster-Prod" --ssh-only-disable
```

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
| `--password` | `-p` | No | prompted | ESXi SOAP API password. Never echoed to the terminal. |
| `--port` | | No | `443` | ESXi HTTPS API port |

### SSH Options

Only apply in Run Commands mode.

| Parameter | Default | Description |
|---|---|---|
| `--ssh-user` | `root` | SSH username on ESXi hosts |
| `--ssh-password` | prompted | SSH password. Prompted separately. Press Enter to reuse the API password. |
| `--ssh-port` | `22` | TCP port for SSH |
| `--ssh-timeout` | `30` | Timeout in seconds for the SSH connection and each individual command |

### Filtering

| Parameter | Default | Description |
|---|---|---|
| `--host-name` | all | Case-insensitive substring match against the IP or FQDN string from `hosts.json` |

### SSH-Only Modes

| Parameter | Description |
|---|---|
| `--ssh-only-enable` | Enable SSH on every matched host and exit. No commands are run. |
| `--ssh-only-disable` | Disable SSH on every matched host and exit. No commands are run. |

### Behaviour Options

| Parameter | Default | Description |
|---|---|---|
| `--change-hostname` | off | Before running `COMMANDS_TO_RUN`, execute `esxcli system hostname set --fqdn=<host>` on each host. The FQDN value is the host's entry in `hosts.json`. Requires all entries to be valid FQDNs (min. 3 labels). FQDN validation runs even in `--dry-run` and exits before connecting to any host if any entry is invalid. Incompatible with `--ssh-only-*` modes. |
| `--disable-ssh-after` | off | Disable SSH after running commands, even if SSH was already running before the script started. |
| `--dry-run` | off | Simulate all actions without making any changes. FQDN validation still runs. |
| `--verbose` | off | Print DEBUG-level output to the console. |
| `--log-file` | `esxi_direct_ssh_YYYYMMDD_HHMMSS.log` | Log file path. Directory is created automatically. |
| `--no-log-file` | off | Disable log file creation. Write to console only. |

### FQDN validation for --change-hostname

Validation runs before any host is contacted. An entry is accepted when it has at least three dot-separated labels, is not an IPv4 address, and uses only valid label characters.

```
[X]  192.168.10.11           IP address
[X]  esxi-hostname           no domain suffix (single label)
[X]  esx-01.site-a           only 2 labels -- need at least host.domain.tld
[OK] esx-01.site-a.vcf.lab   4 labels
[OK] esxi-03.corp.local      3 labels (minimum)
```

### Example invocations

```
# Dry-run with hosts.json in the current directory
python esxi_direct_ssh.py -u root --dry-run

# Run commands and disable SSH afterwards
python esxi_direct_ssh.py -u root --disable-ssh-after

# Separate API and SSH passwords
python esxi_direct_ssh.py -u root -p ApiPass --ssh-password SshPass

# Enable SSH on all hosts (no commands run)
python esxi_direct_ssh.py -u root --ssh-only-enable

# Disable SSH on all hosts (no commands run)
python esxi_direct_ssh.py -u root --ssh-only-disable

# Set hostname then run COMMANDS_TO_RUN
python esxi_direct_ssh.py -u root --change-hostname --disable-ssh-after

# Validate FQDNs without touching any host
python esxi_direct_ssh.py -u root --change-hostname --dry-run

# Use a different hosts file
python esxi_direct_ssh.py -u root --config /etc/esxi/prod_hosts.json

# Target a specific host by substring
python esxi_direct_ssh.py -u root --host-name "esx-01a" --disable-ssh-after --verbose
```

---

## 8. Command Reference -- vcenter_rename_local_datastores.py

Renames local VMFS datastores on ESXi hosts across one or more vCenter instances.

A datastore is treated as **local** when it is mounted by exactly one host and its type is VMFS. This matches the default `datastore1`, `datastore1 (1)`, `datastore1 (2)`, ... naming that ESXi assigns during installation.

### vCenter Connection

| Parameter | Short | Required | Default | Description |
|---|---|---|---|---|
| `--server` | `-s` | Yes (repeatable) | -- | vCenter hostname or IP. Repeat for multiple vCenters: `-s vc1 -s vc2`. In Enhanced Linked Mode (ELM) all vCenters share one SSO domain so the same credentials apply everywhere. Each vCenter gets its own independent connection. Results from all vCenters are aggregated into a single summary. |
| `--user` | `-u` | Yes | -- | vCenter / SSO username |
| `--password` | `-p` | No | prompted | Password. Never echoed to the terminal. Applied to all specified vCenters. |
| `--port` | | No | `443` | vCenter HTTPS port |

### Filtering

Hosts and datastores that do not match are silently skipped and do not affect the exit code.

| Parameter | Short | Default | Description |
|---|---|---|---|
| `--cluster` | `-c` | all (repeatable) | Only process clusters whose name contains this substring (case-insensitive). Repeat for multiple values: `--cluster Prod --cluster Dev`. |
| `--host-name` | | all | Only process hosts whose registered name contains this substring (case-insensitive). |

### Naming Pattern

| Parameter | Default | Description |
|---|---|---|
| `--pattern` | `{shortname}-local` | Naming pattern with placeholders substituted per datastore. See placeholder table below. |

Available placeholders:

| Placeholder | Resolved to | Example |
|---|---|---|
| `{hostname}` | Full hostname as registered in vCenter | `esx-01a.site-a.vcf.lab` |
| `{shortname}` | First label before the first dot | `esx-01a` |
| `{cluster}` | Cluster name verbatim | `Cluster Prod A` |
| `{cluster_slug}` | Cluster name lowercased with non-alphanumeric chars replaced by `-` | `cluster-prod-a` |
| `{vcenter}` | vCenter hostname used for this connection | `vc-mgmt.corp.local` |
| `{index}` | 1-based 2-digit counter; resolves to an empty string when the host has exactly one local datastore, or to `-NN` (with leading dash) when there are multiple | (empty) or `-02` |
| `{index!}` | Same counter but always shown and without a leading dash | `01` or `02` |

Pattern examples and expected output:

| Pattern | Single DS on host | Multiple DS on host |
|---|---|---|
| `{shortname}-local` | `esx-01a-local` | `esx-01a-local` + CONFLICT warning |
| `{shortname}-local{index}` | `esx-01a-local` | `esx-01a-local-01`, `esx-01a-local-02` |
| `{shortname}-ds{index!}` | `esx-01a-ds01` | `esx-01a-ds01`, `esx-01a-ds02` |
| `{cluster_slug}-{shortname}` | `cluster-prod-a-esx-01a` | CONFLICT warning |
| `{hostname}-datastore` | `esx-01a.site-a.vcf.lab-datastore` | CONFLICT warning |

Default pattern `{shortname}-local` applied to real hostnames:

| Host registered in vCenter | Resolved datastore name |
|---|---|
| `esx01-15.domain.local` | `esx01-15-local` |
| `esx15.domain.local` | `esx15-local` |
| `testesx.domain2.local` | `testesx-local` |

If a host has more than one local datastore and the pattern contains neither `{index}` nor `{index!}`, the script prints a warning before processing starts. All datastores on that host would resolve to the same name; the second and subsequent renames will fail with CONFLICT.

### Behaviour Options

| Parameter | Default | Description |
|---|---|---|
| `--list-only` | off | Print all local datastores with their capacity and free space. No renames are performed. Use this first to inventory what exists before deciding on a naming pattern. |
| `--skip-already-named` | off | Skip datastores whose current name already equals the resolved target name. Useful when re-running after a partial run. |
| `--include-nfs` | off | Include single-host NFS datastores in addition to VMFS. Default is VMFS-only. |
| `--dry-run` | off | Show what would be renamed -- print current and target names for every datastore -- without making any changes. Conflict detection and naming pattern resolution run exactly as in a live run so the output accurately predicts what would happen. |
| `--verbose` | off | Print DEBUG-level output to the console. Always written to the log file regardless of this flag. |
| `--log-file` | `vcenter_rename_datastores_YYYYMMDD_HHMMSS.log` | Log file path. Directory is created automatically if it does not exist. |
| `--no-log-file` | off | Disable log file creation. Write to console only. |
| `--task-timeout` | `60` | Seconds to wait for each vCenter rename task before treating it as failed. |

### Conflict detection

Before each rename the script checks whether the target name already exists anywhere in the same vCenter. If it does, the rename is skipped and logged as CONFLICT. Names created during the current run are tracked in memory so back-to-back renames in a single session are also covered correctly.

### Example invocations

```
# Step 1 -- inventory: read-only list of all local datastores
python vcenter_rename_local_datastores.py \
    -s vcenter.corp.local -u administrator@vsphere.local --list-only

# Step 2 -- dry-run: confirm old and new names before touching anything
python vcenter_rename_local_datastores.py \
    -s vcenter.corp.local -u administrator@vsphere.local \
    --cluster "Cluster-Prod" --dry-run

# Step 3 -- rename for real, write a log
python vcenter_rename_local_datastores.py \
    -s vcenter.corp.local -u administrator@vsphere.local \
    --cluster "Cluster-Prod" \
    --log-file C:\Logs\ds_rename.log

# Rename only in specific clusters (--cluster is repeatable)
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

# Rename only hosts matching a substring
python vcenter_rename_local_datastores.py \
    -s vcenter.corp.local -u administrator@vsphere.local \
    --host-name "esx-prod"
```

---

## 9. Command Reference -- vcenter_export_tpm_keys.py

Discovers ESXi hosts via vCenter, enables SSH on each host via the vCenter
API, runs three `esxcli` commands to collect TPM state and recovery keys,
then disables SSH. Supports multiple vCenters (ELM) and three simultaneous
output modes.

**Commands run on each ESXi host via SSH:**

| Command | Data returned |
|---|---|
| `esxcli system settings encryption get` | Encryption mode, Secure Boot requirement, Physical Presence |
| `esxcli system settings encryption recovery list` | Recovery ID and full recovery key string |
| `esxcli hardware trustedboot get` | TPM presence, version (1.2 / 2.0), Secure Boot state |

**TXT output format** (cluster-grouped, keys only):

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

If a host has no recovery key configured the entry shows:
```
 ID  : (no recovery key configured)
 KEY : -
```

### vCenter Connection

| Parameter | Short | Required | Default | Description |
|---|---|---|---|---|
| `--server` | `-s` | Yes (repeatable) | -- | vCenter hostname or IP. Repeat for ELM environments: `-s vc-a -s vc-b`. Each vCenter is processed independently with the same SSO credentials. |
| `--user` | `-u` | Yes | -- | vCenter / SSO username |
| `--password` | `-p` | No | prompted | vCenter password. Never echoed to the terminal. |
| `--port` | | No | `443` | vCenter HTTPS port |

### SSH Credentials

| Parameter | Default | Description |
|---|---|---|
| `--ssh-user` | `root` | SSH username on ESXi hosts |
| `--ssh-password` | prompted | SSH password. Prompted separately from the vCenter password. Press Enter to reuse the vCenter password. |
| `--ssh-port` | `22` | SSH port on ESXi hosts |
| `--ssh-timeout` | `30` | Timeout in seconds for SSH connection and each individual command |

### Filtering

| Parameter | Short | Default | Description |
|---|---|---|---|
| `--cluster` | `-c` | all (repeatable) | Only process clusters whose name contains this substring (case-insensitive). Repeat for multiple clusters: `--cluster Prod --cluster Dev`. |
| `--host-name` | | all | Only process hosts whose registered name contains this substring (case-insensitive) |

### Output Options

| Parameter | Default | Description |
|---|---|---|
| `--html [FILE]` | off | Write a self-contained HTML report. If FILE is omitted a timestamped filename (`tpm_export_YYYYMMDD_HHMMSS.html`) is created in the current working directory. |
| `--txt [FILE]` | off | Write a plain-text cluster-grouped report. If FILE is omitted a timestamped filename (`tpm_export_YYYYMMDD_HHMMSS.txt`) is created in the current working directory. |
| `--log-file` | off | Also write the console log to a file |
| `--verbose` | off | Print DEBUG-level output to the console |

### Behaviour Options

| Parameter | Default | Description |
|---|---|---|
| `--disable-ssh-after` | `auto` | Controls SSH state after each host is processed. `auto` = disable SSH only if the script turned it on (same logic as the other SSH scripts). `yes` = always disable. `no` = leave SSH in whatever state it is after collection. |

### Example invocations

```
# CLI output only -- all clusters
python vcenter_export_tpm_keys.py -s vcenter.corp.local -u admin@vsphere.local

# HTML and TXT with auto-generated filenames (current directory)
python vcenter_export_tpm_keys.py -s vcenter.corp.local -u admin@vsphere.local     --html --txt

# HTML and TXT with explicit filenames
python vcenter_export_tpm_keys.py -s vcenter.corp.local -u admin@vsphere.local     --html C:\Reports\tpm.html --txt C:\Reports\tpm.txt

# Specific cluster(s) only
python vcenter_export_tpm_keys.py -s vcenter.corp.local -u admin@vsphere.local     --cluster "Cluster-Prod" --cluster "Cluster-Dev" --html

# Different SSH credentials
python vcenter_export_tpm_keys.py -s vcenter.corp.local -u admin@vsphere.local     --ssh-user root --ssh-password RootPass --html --txt

# Multiple vCenters (ELM -- one SSO password for all)
python vcenter_export_tpm_keys.py     -s vc-site-a.corp.local     -s vc-site-b.corp.local     -u administrator@vsphere.local     --html --txt

# Leave SSH running after collection
python vcenter_export_tpm_keys.py -s vcenter.corp.local -u admin@vsphere.local     --disable-ssh-after no --html
```

---

## 10. Command Reference -- generate_hosts_config.py

Reads `hosts.json` and generates a host commissioning JSON file in the format expected by VMware Cloud Foundation (VCF). All parameters are entered interactively with hidden password input.

VCF accepts a maximum of 50 hosts per commissioning operation. When `hosts.json` contains more than 50 entries the output is automatically split: `hosts_config_part01.json`, `hosts_config_part02.json`, ...

### Options

| Parameter | Short | Default | Description |
|---|---|---|---|
| `--input` | `-i` | `hosts.json` next to the script | Input file path |
| `--output` | `-o` | `hosts_config.json` | Output file path |

### Interactive prompts

```
Username:                   (applied to every host)
Password:                   (hidden input)
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
Select vVol storage protocol type:    (shown only when VVOL is selected)
  1) VMFS_FC
  2) ISCSI
  3) NFS
```

### Output format

Non-VVOL:

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

VVOL (adds `vvolStorageProtocolType`):

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

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Input file not found or invalid JSON |
| `2` | Invalid user input or user aborted (Ctrl+C) |

### Example invocations

```
# Use defaults
python generate_hosts_config.py

# Custom paths
python generate_hosts_config.py -i site-a-hosts.json -o site-a-commissioning.json
```

---

## 11. SSH Disable Logic

The SSH service state on each host is recorded before the script makes any changes. The same logic applies to both SSH scripts.

| SSH state before run | `--disable-ssh-after` set | SSH state after run |
|---|---|---|
| Stopped | No | Stopped -- script turns it on for the session then turns it back off |
| Stopped | Yes | Stopped |
| Running | No | Running -- left as found |
| Running | Yes | Stopped |

The key principle: if the script turned SSH on, it turns it back off. If SSH was already on and `--disable-ssh-after` is not set, it is left running.

If the script is interrupted mid-run (`Ctrl+C`, power loss), some hosts may be left with SSH enabled. Run `--ssh-only-disable` afterwards to restore the expected state across the entire fleet.

SSH-only modes make a single explicit change and do not apply restore logic. The state after the script finishes is exactly what the mode name says.

---

## 12. Credential Prompts

All scripts prompt interactively for any password not provided on the command line. Prompts appear before any host is contacted. Passwords are never echoed to the terminal.

### vcenter_esxi_ssh.py

```
vCenter password for 'administrator@vsphere.local': ****
SSH password for 'root' on ESXi hosts
(leave blank to reuse the vCenter password): ****
```

Pressing Enter at the SSH password prompt reuses the vCenter password. This is logged:

```
--> No SSH password entered, reusing vCenter password for SSH.
```

In `--ssh-only-*` modes the SSH password is not prompted because no SSH session is opened.

### esxi_direct_ssh.py

```
ESXi API password for 'root': ****
SSH password for 'root' on ESXi hosts
(leave blank to reuse the API password): ****
```

In `--ssh-only-*` modes the SSH password is not prompted.

### vcenter_rename_local_datastores.py

```
vCenter password for 'administrator@vsphere.local': ****
```

A single prompt is shown even when multiple vCenters are specified, because in ELM they share the same SSO password.

---

## 13. Logging

All scripts write two independent log streams simultaneously.

### Console output

Colour-coded by severity when `colorama` is installed. All output is ASCII-only.

| Level | Colour | When visible |
|---|---|---|
| DEBUG (cyan) | Only with `--verbose` | SOAP connection details, per-service state checks |
| INFO (green) | Always | Normal progress -- host headers, command output, state changes |
| WARNING (yellow) | Always | Non-fatal issues -- host skipped, SSH startup timeout, non-zero command exit code |
| ERROR (red) | Always | Recoverable failures -- SSH auth failed, task error |
| CRITICAL (magenta) | Always | Fatal errors -- API connection refused; script exits immediately |

### Log file

The log file always receives all levels including DEBUG, regardless of `--verbose`. Log lines include a timestamp and level tag:

```
2026-04-16 15:22:56  [INFO    ]  [1/5]  HOST : esx-01a.site-a.vcf.lab
2026-04-16 15:22:57  [DEBUG   ]         SOAP connect -> esx-01a.site-a.vcf.lab:443
2026-04-16 15:22:58  [INFO    ]    -->  SSH service: STOPPED -> enabling ...
2026-04-16 15:22:59  [INFO    ]    -->  Running 3 command(s) via SSH ...
2026-04-16 15:23:00  [INFO    ]         [OK]   (exit 0) $ localcli ... getmemconfig ...
```

Default filenames:

| Script | Default log filename |
|---|---|
| `vcenter_esxi_ssh.py` | `vcenter_esxi_ssh_YYYYMMDD_HHMMSS.log` |
| `esxi_direct_ssh.py` | `esxi_direct_ssh_YYYYMMDD_HHMMSS.log` |
| `vcenter_rename_local_datastores.py` | `vcenter_rename_datastores_YYYYMMDD_HHMMSS.log` |

---

## 14. Host Filtering

### vcenter_esxi_ssh.py

Filters are applied in this order. A host is skipped as soon as it fails any check.

```
1. --host-name  substring match
2. connection_state: disconnected / notResponding
3. power_state: not poweredOn
```

Skipped hosts appear in the summary as SKIPPED and do not affect the exit code.

### esxi_direct_ssh.py

The only filter is `--host-name`, matched case-insensitively against the IP or FQDN string in `hosts.json`.

### vcenter_rename_local_datastores.py

Two independent filters:

- `--cluster` -- substring match against cluster name. Repeatable.
- `--host-name` -- substring match against host's registered name.

Hosts in clusters that do not match, hosts that are not powered on or not connected, and datastores that are not local are all silently skipped without affecting the exit code.

---

## 15. Exit Codes

| Script | Code | Meaning |
|---|---|---|
| vcenter_esxi_ssh.py | `0` | All processed hosts completed successfully |
| vcenter_esxi_ssh.py | `1` | One or more hosts reported FAILED status |
| esxi_direct_ssh.py | `0` | All processed hosts completed successfully |
| esxi_direct_ssh.py | `1` | One or more hosts reported FAILED status |
| vcenter_rename_local_datastores.py | `0` | All processed datastores completed successfully |
| vcenter_rename_local_datastores.py | `1` | One or more rename tasks failed |
| vcenter_export_tpm_keys.py | `0` | All hosts processed successfully |
| vcenter_export_tpm_keys.py | `1` | One or more hosts failed data collection |
| generate_hosts_config.py | `0` | Success |
| generate_hosts_config.py | `1` | Input file not found or invalid JSON |
| generate_hosts_config.py | `2` | Invalid user input or user aborted |

Skipped, conflict, and already-correctly-named entries do not count as failures.

---

## 16. Workflow Examples

### Workflow 1 -- Apply a configuration change to all hosts via vCenter

```
# Dry-run first
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local --dry-run

# Run for real
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
    --disable-ssh-after --log-file C:\Logs\config_change.log
```

### Workflow 2 -- Apply a change to hosts without vCenter

```
# Dry-run
python esxi_direct_ssh.py -u root --dry-run

# Run
python esxi_direct_ssh.py -u root --disable-ssh-after
```

### Workflow 3 -- Set hostnames and run configuration commands

```
# Validate FQDNs without touching any host
python esxi_direct_ssh.py -u root --change-hostname --dry-run

# Run for real
python esxi_direct_ssh.py -u root --change-hostname --disable-ssh-after \
    --log-file C:\Logs\hostname_and_config.log
```

### Workflow 4 -- Maintenance window SSH toggle

```
# Open SSH before the window
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
    --cluster "Cluster-Prod" --ssh-only-enable

# ... perform maintenance ...

# Close SSH after the window
python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
    --cluster "Cluster-Prod" --ssh-only-disable
```

### Workflow 5 -- Rename local datastores

```
# Step 1: inventory
python vcenter_rename_local_datastores.py \
    -s vcenter.corp.local -u admin@vsphere.local --list-only

# Step 2: dry-run in target cluster
python vcenter_rename_local_datastores.py \
    -s vcenter.corp.local -u admin@vsphere.local \
    --cluster "Cluster-Prod" --dry-run

# Step 3: rename
python vcenter_rename_local_datastores.py \
    -s vcenter.corp.local -u admin@vsphere.local \
    --cluster "Cluster-Prod" --log-file C:\Logs\ds_rename.log
```

### Workflow 6 -- Rename datastores across multiple vCenters (ELM)

```
# Dry-run against both vCenters at once
python vcenter_rename_local_datastores.py \
    -s vc-site-a.corp.local \
    -s vc-site-b.corp.local \
    -u administrator@vsphere.local \
    --dry-run

# Rename for real
python vcenter_rename_local_datastores.py \
    -s vc-site-a.corp.local \
    -s vc-site-b.corp.local \
    -u administrator@vsphere.local \
    --log-file C:\Logs\ds_rename_all.log
```

### Workflow 7 -- Export TPM recovery keys

```
# Step 1: CLI only to verify connectivity and see what's there
python vcenter_export_tpm_keys.py -s vcenter.corp.local -u admin@vsphere.local

# Step 2: Export to both HTML and TXT
python vcenter_export_tpm_keys.py -s vcenter.corp.local -u admin@vsphere.local \
    --cluster "Cluster-Prod" --html --txt

# ELM: both vCenters, one command
python vcenter_export_tpm_keys.py \
    -s vc-site-a.corp.local -s vc-site-b.corp.local \
    -u administrator@vsphere.local --html --txt
```

### Workflow 8 -- Generate VCF commissioning file

```
# Edit hosts.json with the target FQDNs, then run
python generate_hosts_config.py

# Custom paths
python generate_hosts_config.py -i site-a-hosts.json -o site-a-commission.json
```

---

## 17. Scheduled Execution on Windows

Store credentials in user-level environment variables to avoid plain-text passwords in scheduled task XML:

```bat
setx VC_PASS "MyVCenterPassword"
setx SSH_PASS "MyRootPassword"
setx ESXI_PASS "MyRootPassword"
```

### vcenter_esxi_ssh.py batch wrapper

```bat
@echo off
setlocal
set LOG=C:\Logs\vcenter_%date:~-4,4%%date:~-7,2%%date:~-10,2%.log
python C:\Scripts\vcenter_esxi_ssh.py ^
    -s vcenter.corp.local ^
    -u administrator@vsphere.local ^
    -p %VC_PASS% ^
    --ssh-password %SSH_PASS% ^
    --disable-ssh-after ^
    --log-file "%LOG%"
if %errorlevel% neq 0 ( echo [FAIL] Check: %LOG% & exit /b 1 )
echo [OK] Log: %LOG%
```

### esxi_direct_ssh.py batch wrapper

```bat
@echo off
setlocal
set LOG=C:\Logs\esxi_direct_%date:~-4,4%%date:~-7,2%%date:~-10,2%.log
python C:\Scripts\esxi_direct_ssh.py ^
    -u root ^
    -p %ESXI_PASS% ^
    --ssh-password %ESXI_PASS% ^
    --disable-ssh-after ^
    --log-file "%LOG%"
if %errorlevel% neq 0 ( echo [FAIL] Check: %LOG% & exit /b 1 )
echo [OK] Log: %LOG%
```

### vcenter_rename_local_datastores.py batch wrapper

```bat
@echo off
setlocal
set LOG=C:\Logs\ds_rename_%date:~-4,4%%date:~-7,2%%date:~-10,2%.log
python C:\Scripts\vcenter_rename_local_datastores.py ^
    -s vcenter.corp.local ^
    -u administrator@vsphere.local ^
    -p %VC_PASS% ^
    --log-file "%LOG%"
if %errorlevel% neq 0 ( echo [FAIL] Check: %LOG% & exit /b 1 )
echo [OK] Log: %LOG%
```

---

## 18. Troubleshooting

### `Failed to start SSH on <host>: host`

Occurs with `esxi_direct_ssh.py` on a direct ESXi connection. The pyVmomi object tree on a direct ESXi connection includes a `vim.Datacenter` layer named `ha-datacenter` that is absent when connecting through vCenter. The correct navigation path is:

```
rootFolder
  -> childEntity[0]  (vim.Datacenter "ha-datacenter")
       -> hostFolder
            -> childEntity[0]  (vim.ComputeResource)
                 -> host[0]  (vim.HostSystem)
                      -> configManager.serviceSystem
```

This is handled correctly in the current version.

### --change-hostname rejects entries that appear valid

The FQDN validator requires at least three dot-separated labels:

```
[X]  esx-01.site-a         2 labels -- rejected
[OK] esx-01.site-a.vcf.lab 4 labels -- accepted
```

Add the full domain suffix (e.g. `.vcf.lab` or `.corp.local`).

### API connection refused / SSL error

Both scripts disable certificate verification by default to support self-signed certificates.

- Check reachability: `ping <host>`
- Check port: `Test-NetConnection <host> -Port 443` (PowerShell)
- Non-standard port: add `--port <port>`

### SSH connection fails

| Symptom | Likely cause | Fix |
|---|---|---|
| `SSH authentication failed` | Wrong `--ssh-password` or `--ssh-user` | Verify root credentials on the host |
| `SSH connection failed` / timeout | Management IP not reachable | Check routing and firewall |
| `SSH service did not start within 10s` | Host under load or service locked | Check host health; retry |
| Command exit code `-1` | SSH session dropped mid-command | Increase `--ssh-timeout` |

### A host always shows as FAILED in esxi_direct_ssh.py

- Confirm the IP or FQDN in `hosts.json` is correct and reachable on port 443
- Confirm API credentials: `-u root -p <password>`
- Run `--verbose` to see the full SOAP connection error

### A host always shows as SKIPPED in vcenter_esxi_ssh.py

Check the host's connection state and power state in vCenter. The script skips hosts that are `disconnected`, `notResponding`, or not `poweredOn`, and hosts excluded by `--cluster` or `--host-name` filters.

### A datastore shows as CONFLICT in vcenter_rename_local_datastores.py

The target name already exists in this vCenter. Either another datastore was already renamed to that name, or the pattern resolves to a name that happens to be in use. Options:

- Use a more specific pattern (e.g. add `{index!}`)
- Manually rename the conflicting datastore first
- Use `--list-only` to see the current state before re-running

### Commands produce no output

Some ESXi commands return output only when there is something to report. An empty stdout with exit code `0` is not an error.

### `reboot` in COMMANDS_TO_RUN shows exit code -1 in the log

The reboot command terminates the SSH connection before Paramiko can read the exit code. The host still reboots. To suppress the log error use:

```python
"nohup reboot &"
```

---

## 19. Security Considerations

**Credentials**

Never pass passwords on the command line in shared or logged environments. Use the interactive prompt (omit `-p` / `--ssh-password`) or user-level environment variables. No script persists credentials -- they exist in memory only for the duration of the run. Passwords are never written to log files.

**SSL / TLS**

All scripts disable certificate verification by default to support self-signed certificates. To enable verification modify the `ssl.SSLContext` block in the connect function:

```python
ctx.check_hostname = True
ctx.verify_mode    = ssl.CERT_REQUIRED
ctx.load_verify_locations("/path/to/ca-bundle.crt")
```

**SSH host key verification**

Both SSH scripts use `paramiko.AutoAddPolicy()`, which accepts any SSH host key without verification. This is acceptable for controlled internal environments. For stricter environments replace `AutoAddPolicy` with `paramiko.RejectPolicy` and pre-populate a `known_hosts` file.

**SSH service state after interruption**

If the script is interrupted (`Ctrl+C`, power loss) mid-run, some hosts may be left with SSH enabled. Run `--ssh-only-disable` afterwards to close SSH on all hosts.

**generate_hosts_config.py password handling**

The script writes the password in plaintext into the output JSON file. This is required by the VCF commissioning API. Apply appropriate filesystem permissions to the output file and delete it once the commissioning operation is complete.

---

## 20. Architecture Overview

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
|   +-- get_host_info()          name, cluster, management IP via VirtualNicManager
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
    +-- Credential prompts       vCenter password then SSH password
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
|   +-- validate_fqdn_hosts()    checks all entries, reports each invalid one
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
    +-- Flag compatibility check
    +-- Credential prompts        API password then SSH password
    +-- Mode detection
    +-- Banner + config summary
    +-- Load hosts.json + apply --host-name filter
    +-- FQDN validation           (when --change-hostname; runs even in --dry-run)
    +-- Per-host loop
    |   +-- connect_esxi()        per-host SOAP connection
    |   +-- [ssh-only-enable]     enable SSH -> disconnect SOAP -> continue
    |   +-- [ssh-only-disable]    disable SSH -> disconnect SOAP -> continue
    |   +-- [run-commands]        enable SSH
    |                              -> [--change-hostname] hostname set command
    |                              -> COMMANDS_TO_RUN
    |                              -> conditional disable
    |                              -> disconnect SOAP
    +-- Summary report + exit code
```

### vcenter_rename_local_datastores.py

```
vcenter_rename_local_datastores.py
|
+-- Logging layer                identical structure to SSH scripts
|
+-- vCenter layer  (pyVmomi)
|   +-- connect_vcenter()        SSL connection, accepts self-signed certs
|   +-- get_clusters()           ContainerView for vim.ClusterComputeResource
|   +-- existing_ds_names()      builds set of all current datastore names
|   +-- is_local()               host count == 1 AND type == VMFS (or NFS)
|
+-- Naming layer
|   +-- slugify()                lowercases and replaces non-alphanumeric with -
|   +-- apply_pattern()          substitutes all placeholders in the pattern string
|
+-- Rename layer
|   +-- wait_task()              polls vim.Task until success / error / timeout
|   +-- do_rename()              calls ds.Rename(newName) and waits for the task
|
+-- Argument parser
|   +-- vCenter connection group  --server (repeatable), -u, -p, --port
|   +-- Filtering group           --cluster (repeatable), --host-name
|   +-- Naming group              --pattern
|   +-- Behaviour options group   --list-only, --skip-already-named, --include-nfs,
|                                   --dry-run, --verbose, --log-file, --task-timeout
|
+-- process_vcenter()            per-vCenter logic; returns list of result dicts
|   +-- get_clusters() + filter
|   +-- Per-cluster loop
|       +-- Per-host loop
|           +-- collect local datastores
|           +-- sort by name for deterministic index assignment
|           +-- [--list-only]     log capacity + free space -> continue
|           +-- warn if multi-DS and no {index} in pattern
|           +-- Per-datastore loop
|               +-- apply_pattern()
|               +-- skip if already correct
|               +-- skip if CONFLICT
|               +-- [--dry-run]   log -> update in-memory name set -> continue
|               +-- do_rename()   live rename via vCenter task
|               +-- update in-memory name set
|
+-- main()
    +-- Credential prompt
    +-- Banner + config summary
    +-- Per-vCenter loop
    |   +-- connect_vcenter()
    |   +-- process_vcenter()
    |   +-- Disconnect(si)
    +-- Aggregate summary report + exit code
```

### vcenter_export_tpm_keys.py

```
vcenter_export_tpm_keys.py
|
+-- safe_getpass()               ReadConsoleW on Windows, getpass elsewhere
|
+-- Logging layer                identical structure to SSH scripts
|
+-- vCenter layer  (pyVmomi)
|   +-- connect_vcenter()        SSL connection, accepts self-signed certs
|   +-- get_clusters()           ContainerView for vim.ClusterComputeResource
|   +-- get_mgmt_ip()            VirtualNicManager management-tagged vnic
|   +-- is_ssh_running()         reads vim.host.ServiceSystem.serviceInfo
|   +-- enable_ssh()             StartService("TSM-SSH") + 12s polling
|   +-- disable_ssh()            StopService("TSM-SSH")
|
+-- SSH layer  (paramiko)
|   +-- run_ssh_command()        single-command SSH execute; returns (out,err,code)
|
+-- Parsers
|   +-- parse_kv()               key: value line format
|   +-- parse_recovery_list()    parses "esxcli ... recovery list" tabular output
|   +-- parse_trustedboot()      parses "esxcli hardware trustedboot get"
|   +-- parse_encryption_get()   parses "esxcli system settings encryption get"
|
+-- Output layer
|   +-- print_cli_report()       summary table + per-host detail
|   +-- write_txt()              cluster-grouped keys-only plain text
|   +-- write_html()             self-contained dark-theme HTML with cards
|
+-- Argument parser
|   +-- vCenter connection group  --server (repeatable), -u, -p, --port
|   +-- SSH credentials group     --ssh-user, --ssh-password, --ssh-port, --ssh-timeout
|   +-- Filtering group           --cluster (repeatable), --host-name
|   +-- Output group              --html, --txt, --log-file, --verbose
|   +-- Behaviour group           --disable-ssh-after (auto/yes/no)
|
+-- main()
    +-- Credential prompts        vCenter password then SSH password
    +-- Banner + config summary
    +-- Per-vCenter loop
    |   +-- connect_vcenter()
    |   +-- get_clusters() + filter
    |   +-- Per-cluster loop
    |       +-- Per-host loop
    |           +-- skip if not connected / not poweredOn
    |           +-- enable_ssh() via vCenter API
    |           +-- collect_host_data()
    |           |   +-- run_ssh_command() x3
    |           |   +-- parse all outputs
    |           +-- disable_ssh() according to --disable-ssh-after policy
    |   +-- Disconnect(si)
    +-- print_cli_report()
    +-- write_html() if --html
    +-- write_txt()  if --txt
```

---

### generate_hosts_config.py

```
generate_hosts_config.py
|
+-- load_fqdns()              reads hosts.json, validates array of strings
+-- prompt_non_empty()        interactive text input with empty-check loop
+-- prompt_password()         getpass with confirmation
+-- prompt_choice()           numbered menu for enumerated options
+-- build_host_entries()      applies shared params to each FQDN
+-- chunk_list()              splits list into chunks of max 50
+-- build_chunked_output_paths()  inserts _partNN suffix when needed
+-- write_output()            JSON dump with trailing newline
+-- main()
    +-- Argument parsing      --input / --output
    +-- Load FQDNs
    +-- Interactive prompts
    +-- Build host entries
    +-- Split into chunks of max 50
    +-- Write output file(s)
    +-- Print success summary
```

---

*Tested on: vCenter 8.0 / 9.0, ESXi 8.0 / 9.0, Python 3.11, Windows 10/11, Ubuntu 22.04.  
Dependencies: pyVmomi 8.0.2+, paramiko 3.4+, colorama 0.4.6+ (optional).*
