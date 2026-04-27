#!/usr/bin/env python3
"""
vcenter_esxi_ssh.py
===================
Automate SSH command execution across all ESXi hosts registered in a vCenter instance.
Enables SSH via the vCenter API, connects via Paramiko, runs commands, then optionally
disables SSH again. Supports dry-run mode and detailed logging to both CLI and a log file.

Compatible with: Windows 10/11, Linux, macOS  |  Python 3.8+

============================================================================
  INSTALLATION
============================================================================

    pip install -r requirements.txt
        - or -
    pip install pyVmomi paramiko colorama

Required packages:
    pyVmomi   - vCenter / vSphere Python SDK
    paramiko  - SSH client
    colorama  - Coloured terminal output (Windows-compatible)

============================================================================
  WORKFLOW EXAMPLES
============================================================================

  1) AUDIT - Collect version & hardware info from every host (read-only, safe)
     -------------------------------------------------------------------------
     Always start with a dry-run to confirm the scope before touching anything.

       # Step 1: preview what the script would do
       python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local --dry-run

       # Step 2: run for real, write a timestamped log
       python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
           --log-file C:\\Logs\\audit.log

     Suggested COMMANDS_TO_RUN for this workflow:
       esxcli system version get
       esxcli hardware cpu global get
       esxcli storage core device list
       esxcli network ip interface list

  -----------------------------------------------------------------------------

  2) COMPLIANCE CHECK - Verify security settings across all hosts
     -------------------------------------------------------------
     Use --disable-ssh-after to guarantee SSH is closed when the script finishes,
     regardless of whether it was open before the run.

       python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
           --ssh-user root --ssh-password RootP@ss \
           --disable-ssh-after \
           --log-file C:\\Logs\\compliance.log

     Suggested COMMANDS_TO_RUN for this workflow:
       esxcli system ntp get
       esxcli system syslog config get
       esxcli system settings advanced list -o /UserVars/ESXiShellInteractiveTimeOut
       cat /etc/vmware/config

  -----------------------------------------------------------------------------

  3) PATCH PREP - Inventory installed VIBs before a maintenance window
     ------------------------------------------------------------------
     Target a single production cluster; enable verbose output so every detail
     appears in the log file for later comparison.

       python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
           --cluster "Cluster-Prod" \
           --disable-ssh-after \
           --verbose \
           --log-file C:\\Logs\\patch_prep.log

     Suggested COMMANDS_TO_RUN for this workflow:
       esxcli software vib list
       esxcli software profile get
       esxcli system version get

  -----------------------------------------------------------------------------

  4) EMERGENCY REMEDIATION - Push a config fix to one specific host
     ---------------------------------------------------------------
     Use --host-name to pin execution to a single host; --verbose gives a
     line-by-line trace in both the terminal and the log file.

       python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
           --host-name "esxi-prod-07" \
           --disable-ssh-after \
           --verbose

     Suggested COMMANDS_TO_RUN for this workflow:
       esxcli system settings advanced set -o /UserVars/SuppressShellWarning -i 1
       /etc/init.d/ntpd restart
       esxcli system ntp get

  -----------------------------------------------------------------------------

  5) MULTI-CLUSTER SWEEP - Separate credentials & logs per environment
     -------------------------------------------------------------------
     Run the script once per cluster with its own credentials and log file.
     Exit code 1 is returned on any failure, making this easy to chain in a
     CI pipeline or scheduled task.

       python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
           --cluster "Cluster-Prod" --ssh-password ProdRootPass \
           --disable-ssh-after --log-file C:\\Logs\\sweep_prod.log

       python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
           --cluster "Cluster-Dev"  --ssh-password DevRootPass \
           --disable-ssh-after --log-file C:\\Logs\\sweep_dev.log

  -----------------------------------------------------------------------------

  6) SSH TOGGLE - Enable or disable SSH across hosts without running any commands
     ----------------------------------------------------------------------------
     Use --ssh-only-enable to turn SSH on across every host (e.g. before a
     maintenance window) and --ssh-only-disable to close it again afterwards.
     Both modes support the same --cluster / --host-name filters and --dry-run.

       # Open SSH on all hosts in a cluster before a maintenance window
       python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
           --cluster "Cluster-Prod" --ssh-only-enable

       # Close SSH on all hosts once the work is done
       python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
           --cluster "Cluster-Prod" --ssh-only-disable

       # Dry-run first to confirm scope
       python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local \
           --ssh-only-disable --dry-run

     NOTE: --ssh-only-enable and --ssh-only-disable are mutually exclusive
           with each other and with --disable-ssh-after.

  -----------------------------------------------------------------------------

  7) SCHEDULED TASK - Windows Task Scheduler with env-variable credentials
     ----------------------------------------------------------------------
     Avoid plain-text passwords in scheduled task XML by reading them from
     environment variables set as user-level secrets.

       # In Task Scheduler "Action" -> Program/script:
       #   python
       # Add arguments:
       #   vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local
       #   -p %VC_PASS% --ssh-password %SSH_PASS% --disable-ssh-after
       #   --log-file "C:\\Logs\\weekly_%DATE:~-4,4%%DATE:~-7,2%%DATE:~-10,2%.log"

       set VC_PASS=MyVCenterPassword
       set SSH_PASS=MyRootPassword
       python vcenter_esxi_ssh.py -s vcenter.corp.local -u admin@vsphere.local ^
           -p %VC_PASS% --ssh-password %SSH_PASS% ^
           --disable-ssh-after ^
           --log-file "C:\\Logs\\weekly_audit.log"
"""

import argparse
import getpass
import logging
import os
import socket
import sys
import time
from datetime import datetime
from typing import Optional

# ---------------------------------------------------------------------------
# Dependency check & imports
# ---------------------------------------------------------------------------
MISSING_PACKAGES = []

try:
    from pyVim.connect import SmartConnect, Disconnect
    from pyVmomi import vim
    import ssl
except ImportError:
    MISSING_PACKAGES.append("pyVmomi")

try:
    import paramiko
except ImportError:
    MISSING_PACKAGES.append("paramiko")

try:
    from colorama import init as colorama_init, Fore, Style
    colorama_init(autoreset=True)
    HAS_COLOR = True
except ImportError:
    HAS_COLOR = False

if MISSING_PACKAGES:
    print(f"[ERROR] Missing Python packages: {', '.join(MISSING_PACKAGES)}")
    print(f"        Install them with:  pip install {' '.join(MISSING_PACKAGES)}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# COMMANDS TO RUN ON EVERY ESXi HOST  <- edit this list as needed
# ---------------------------------------------------------------------------
COMMANDS_TO_RUN = [
    "localcli --plugin-dir=/usr/lib/vmware/esxcli/int sched group getmemconfig -g host/vim/vmvisor/settingsd-task-forks",
    "localcli --plugin-dir=/usr/lib/vmware/esxcli/int sched group setmemconfig -g host/vim/vmvisor/settingsd-task-forks -m 400 -i 0 -l -1 -u mb",
    "localcli --plugin-dir=/usr/lib/vmware/esxcli/int sched group getmemconfig -g host/vim/vmvisor/settingsd-task-forks",
    # Add more commands below, for example:
    # "esxcli hardware cpu global get",
    # "esxcli system ntp get",
    # "esxcli software vib list",
    # "esxcli system settings advanced list -o /UserVars/SuppressShellWarning",
]



# ---------------------------------------------------------------------------
# Password input -- handles Alt codes and special characters on Windows
# ---------------------------------------------------------------------------
def safe_getpass(prompt: str = "") -> str:
    """
    Secure password input that correctly handles Alt codes and special characters
    on Windows (e.g. Czech/Slovak keyboards where passwords contain accented chars).

    Root cause of the original problem
    -----------------------------------
    The standard getpass on Windows reads from sys.stdin which is a text stream
    bound to the console OEM code page (cp852 for Central Europe).  Windows Alt
    codes (Alt+0xxx) generate characters in the ANSI code page (cp1250).  When
    the two pages differ, typed special characters are silently mis-decoded and
    the resulting password string does not match the one the user intended.

    Why msvcrt.getwch() also fails
    --------------------------------
    getwch() reads characters one at a time in unbuffered mode.  Alt codes work
    by holding Alt while typing a sequence of numpad digits; Windows only resolves
    and buffers the final character when Alt is released.  In unbuffered mode the
    intermediate keystrokes can interfere and the composed character is not
    reliably delivered.

    Solution: ReadConsoleW with ENABLE_LINE_INPUT
    -----------------------------------------------
    ReadConsoleW is the Windows console Unicode API.  With ENABLE_LINE_INPUT the
    console buffers the entire line (including Alt code composition) and only
    returns when Enter is pressed -- the same way a normal input() call works.
    With ENABLE_ECHO_INPUT cleared the typed characters are not shown.  The
    result is a proper UTF-16 Unicode string regardless of any code page settings.

    On non-Windows platforms the function falls back to the standard getpass.
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()

    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes

        kernel32        = ctypes.windll.kernel32
        STD_INPUT       = -10
        ENABLE_ECHO     = 0x0004   # bit to clear (hide typing)
        ENABLE_LINE     = 0x0002   # keep: buffer until Enter
        ENABLE_PROC     = 0x0001   # keep: process Ctrl+C / Alt codes

        h = kernel32.GetStdHandle(STD_INPUT)

        old_mode = ctypes.wintypes.DWORD()
        kernel32.GetConsoleMode(h, ctypes.byref(old_mode))

        # Disable echo; keep line-buffering and processed input so that Alt
        # code composition is handled by the Windows console subsystem.
        new_mode = (old_mode.value & ~ENABLE_ECHO) | ENABLE_LINE | ENABLE_PROC
        kernel32.SetConsoleMode(h, new_mode)

        try:
            buf        = ctypes.create_unicode_buffer(512)
            chars_read = ctypes.wintypes.DWORD()
            kernel32.ReadConsoleW(
                h,
                buf,
                len(buf) - 1,
                ctypes.byref(chars_read),
                None,
            )
            password = buf.value.rstrip("\r\n")
        finally:
            # Always restore original console mode
            kernel32.SetConsoleMode(h, old_mode.value)
            sys.stdout.write("\n")
            sys.stdout.flush()

        return password
    else:
        import getpass as _gp
        return _gp.getpass("")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FORMAT_FILE = "%(asctime)s  [%(levelname)-8s]  %(message)s"
LOG_FORMAT_CLI  = "%(message)s"
DATE_FORMAT     = "%Y-%m-%d %H:%M:%S"


class ColorFormatter(logging.Formatter):
    """ANSI colour formatter for console output (via colorama on Windows)."""

    COLORS = {
        logging.DEBUG:    Fore.CYAN    if HAS_COLOR else "",
        logging.INFO:     Fore.GREEN   if HAS_COLOR else "",
        logging.WARNING:  Fore.YELLOW  if HAS_COLOR else "",
        logging.ERROR:    Fore.RED     if HAS_COLOR else "",
        logging.CRITICAL: Fore.MAGENTA if HAS_COLOR else "",
    }
    RESET = Style.RESET_ALL if HAS_COLOR else ""

    def format(self, record):
        color = self.COLORS.get(record.levelno, "")
        return f"{color}{super().format(record)}{self.RESET}"


def setup_logging(log_file: Optional[str], verbose: bool) -> logging.Logger:
    logger = logging.getLogger("vcenter_esxi_ssh")
    logger.setLevel(logging.DEBUG)

    # Console handler
    cli_handler = logging.StreamHandler(sys.stdout)
    cli_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    cli_handler.setFormatter(
        ColorFormatter(LOG_FORMAT_CLI) if HAS_COLOR else logging.Formatter(LOG_FORMAT_CLI)
    )
    logger.addHandler(cli_handler)

    # File handler
    if log_file:
        try:
            log_dir = os.path.dirname(os.path.abspath(log_file))
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter(LOG_FORMAT_FILE, datefmt=DATE_FORMAT))
            logger.addHandler(fh)
        except Exception as e:
            logger.warning(f"Could not open log file '{log_file}': {e}")

    return logger


# ---------------------------------------------------------------------------
# vCenter helpers
# ---------------------------------------------------------------------------
def connect_vcenter(host: str, user: str, password: str, port: int,
                    logger: logging.Logger):
    """Connect to vCenter via pyVmomi (accepts self-signed certificates)."""
    logger.info(f"-->  Connecting to vCenter: {host}:{port}  as '{user}'")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    try:
        si = SmartConnect(host=host, user=user, pwd=password, port=port, sslContext=ctx)
        about = si.content.about
        logger.info(f"[OK] Connected - {about.fullName}  (build {about.build})")
        return si
    except Exception as e:
        logger.critical(f"[FAIL] vCenter connection failed: {e}")
        sys.exit(1)


def get_all_hosts(si, cluster_filter: Optional[str],
                  logger: logging.Logger) -> list:
    """Return all vim.HostSystem objects from vCenter, optionally filtered by cluster name."""
    logger.info("-->  Fetching ESXi host list from vCenter ...")
    content   = si.content
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.HostSystem], True
    )
    hosts = list(container.view)
    container.Destroy()

    if cluster_filter:
        filtered = [
            h for h in hosts
            if cluster_filter.lower() in (
                h.parent.name if isinstance(h.parent, vim.ClusterComputeResource) else ""
            ).lower()
        ]
        logger.info(
            f"     Cluster filter '{cluster_filter}': "
            f"matched {len(filtered)} / {len(hosts)} host(s)"
        )
        return filtered

    logger.info(f"     Total ESXi hosts found: {len(hosts)}")
    return hosts


def get_host_info(host_obj) -> dict:
    """Extract essential runtime info from a vim.HostSystem object."""
    name    = host_obj.name
    cluster = (
        host_obj.parent.name
        if isinstance(host_obj.parent, vim.ClusterComputeResource)
        else "(standalone)"
    )

    # Resolve the management IP using the VirtualNicManager, which tracks
    # which VMkernel adapters are tagged for each traffic type.
    # Fallback chain:
    #   1. VMkernel port explicitly tagged as "management" traffic type
    #   2. host_obj.name as registered in vCenter  (always the management address)
    ip = name  # safest fallback -- this is how vCenter knows the host
    try:
        nic_mgr = host_obj.config.virtualNicManagerInfo
        # Find VMkernel device keys selected for "management" traffic
        mgmt_keys = set()
        for net_cfg in nic_mgr.netConfig:
            if net_cfg.nicType == "management":
                for selected in net_cfg.selectedVnic:
                    mgmt_keys.add(selected)

        if mgmt_keys:
            # Match selected keys to actual vnic entries to get the IP
            for vnic in host_obj.config.network.vnic:
                if vnic.key in mgmt_keys and vnic.spec.ip.ipAddress:
                    ip = vnic.spec.ip.ipAddress
                    break
    except Exception:
        pass  # leave ip = host_obj.name

    return {
        "name":             name,
        "ip":               ip,
        "cluster":          cluster,
        "connection_state": str(host_obj.runtime.connectionState),
        "power_state":      str(host_obj.runtime.powerState),
    }


# ---------------------------------------------------------------------------
# SSH service management via vCenter API  (vim.host.ServiceSystem)
# ---------------------------------------------------------------------------
def _svc_system(host_obj):
    return host_obj.configManager.serviceSystem


def is_ssh_running(host_obj) -> bool:
    """Return True if the TSM-SSH service is currently active on the host."""
    try:
        for svc in _svc_system(host_obj).serviceInfo.service:
            if svc.key == "TSM-SSH":
                return svc.running
    except Exception:
        pass
    return False


def enable_ssh(host_obj, logger: logging.Logger, dry_run: bool) -> bool:
    """Start the SSH service via the vCenter API. Returns True on success."""
    name = host_obj.name
    try:
        if is_ssh_running(host_obj):
            logger.debug(f"       SSH already running on {name}, skipping start")
            return True
        if dry_run:
            logger.info(f"   [DRY-RUN] Would start SSH service on {name}")
            return True
        _svc_system(host_obj).StartService(id="TSM-SSH")
        for _ in range(10):          # wait up to 10 s for the service to come up
            if is_ssh_running(host_obj):
                logger.debug(f"       SSH service started on {name}")
                return True
            time.sleep(1)
        logger.warning(f"       SSH service on {name} did not start within 10 s")
        return False
    except Exception as e:
        logger.error(f"       Failed to start SSH on {name}: {e}")
        return False


def disable_ssh(host_obj, logger: logging.Logger, dry_run: bool):
    """Stop the SSH service via the vCenter API."""
    name = host_obj.name
    try:
        if not is_ssh_running(host_obj):
            logger.debug(f"       SSH already stopped on {name}")
            return
        if dry_run:
            logger.info(f"   [DRY-RUN] Would stop SSH service on {name}")
            return
        _svc_system(host_obj).StopService(id="TSM-SSH")
        logger.debug(f"       SSH service stopped on {name}")
    except Exception as e:
        logger.error(f"       Failed to stop SSH on {name}: {e}")


# ---------------------------------------------------------------------------
# SSH connection & command execution  (paramiko)
# ---------------------------------------------------------------------------
def run_ssh_commands(
    ip: str,
    hostname: str,
    ssh_user: str,
    ssh_password: str,
    ssh_port: int,
    commands: list,
    ssh_timeout: int,
    logger: logging.Logger,
    dry_run: bool,
) -> dict:
    """
    Open an SSH session to the host, run every command, and return results.

    Return format:
        { "<command>": {"stdout": str, "stderr": str, "exit_code": int} }
    """
    results = {}

    if dry_run:
        for cmd in commands:
            logger.info(f"   [DRY-RUN] {hostname}: would run: {cmd}")
            results[cmd] = {"stdout": "[DRY-RUN]", "stderr": "", "exit_code": 0}
        return results

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        logger.debug(
            f"       SSH connect -> {ip}:{ssh_port}  "
            f"(user={ssh_user}, timeout={ssh_timeout}s)"
        )
        client.connect(
            hostname=ip,
            port=ssh_port,
            username=ssh_user,
            password=ssh_password,
            timeout=ssh_timeout,
            banner_timeout=ssh_timeout,
            auth_timeout=ssh_timeout,
            look_for_keys=False,
            allow_agent=False,
        )
        logger.debug(f"       SSH connected to {hostname} ({ip})")

        for cmd in commands:
            logger.debug(f"       Running: {cmd}")
            try:
                _, stdout, stderr = client.exec_command(cmd, timeout=ssh_timeout)
                out       = stdout.read().decode("utf-8", errors="replace").strip()
                err       = stderr.read().decode("utf-8", errors="replace").strip()
                exit_code = stdout.channel.recv_exit_status()
                results[cmd] = {"stdout": out, "stderr": err, "exit_code": exit_code}

                tag = "[OK]  " if exit_code == 0 else "[FAIL]"
                logger.info(f"       {tag} (exit {exit_code}) $ {cmd}")
                for line in out.splitlines():
                    logger.info(f"              {line}")
                if err and exit_code != 0:
                    for line in err.splitlines():
                        logger.warning(f"           stderr: {line}")

            except Exception as cmd_err:
                logger.error(f"       Error running '{cmd}': {cmd_err}")
                results[cmd] = {"stdout": "", "stderr": str(cmd_err), "exit_code": -1}

    except paramiko.AuthenticationException as e:
        logger.error(f"       SSH authentication failed for {hostname} ({ip}): {e}")
    except (socket.timeout, paramiko.ssh_exception.NoValidConnectionsError) as e:
        logger.error(f"       SSH connection failed for {hostname} ({ip}): {e}")
    except Exception as e:
        logger.error(f"       Unexpected SSH error for {hostname} ({ip}): {e}")
    finally:
        client.close()

    return results


# ---------------------------------------------------------------------------
# Summary report
# ---------------------------------------------------------------------------
def print_summary(summary: list, logger: logging.Logger, dry_run: bool):
    sep = "=" * 74
    logger.info("")
    logger.info(sep)
    logger.info(
        f"  SUMMARY {'[DRY-RUN] ' if dry_run else ''}"
        f"- {len(summary)} host(s) processed"
    )
    logger.info(sep)

    counts = {"OK": 0, "SKIPPED": 0, "FAILED": 0}
    for r in summary:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        tag = {"OK": "[OK]   ", "SKIPPED": "[SKIP] ", "FAILED": "[FAIL] "}.get(
            r["status"], "[?]    "
        )
        logger.info(
            f"  {tag}  {r['name']:<30}  cluster: {r['cluster']:<22}  {r['status']}"
        )
        if r.get("note"):
            logger.info(f"           -> {r['note']}")

    logger.info(sep)
    logger.info(
        f"  OK: {counts['OK']}   "
        f"SKIPPED: {counts['SKIPPED']}   "
        f"FAILED: {counts['FAILED']}"
    )
    logger.info(sep)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "vCenter ESXi SSH Automation - "
            "run commands on all registered ESXi hosts via the vCenter API + SSH"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # -- vCenter connection ------------------------------------------------
    vc = parser.add_argument_group("vCenter connection")
    vc.add_argument("-s", "--server",   required=True,
                    help="vCenter hostname or IP address")
    vc.add_argument("-u", "--user",     required=True,
                    help="vCenter username  (e.g. administrator@vsphere.local)")
    vc.add_argument("-p", "--password", default=None,
                    help="vCenter password  (prompted interactively if omitted)")
    vc.add_argument("--port", type=int, default=443,
                    help="vCenter HTTPS port  (default: 443)")

    # -- ESXi SSH options --------------------------------------------------
    ssh = parser.add_argument_group("ESXi SSH options")
    ssh.add_argument("--ssh-user",     default="root",
                     help="SSH username on ESXi hosts  (default: root)")
    ssh.add_argument("--ssh-password", default=None,
                     help="SSH password on ESXi hosts  (default: same as --password)")
    ssh.add_argument("--ssh-port",     type=int, default=22,
                     help="SSH port on ESXi hosts  (default: 22)")
    ssh.add_argument("--ssh-timeout",  type=int, default=30,
                     help="SSH connection/command timeout in seconds  (default: 30)")

    # -- Filtering ---------------------------------------------------------
    flt = parser.add_argument_group("Filtering")
    flt.add_argument("--cluster",   default=None,
                     help=(
                         "Only process hosts in clusters whose name contains this "
                         "string (case-insensitive substring match)"
                     ))
    flt.add_argument("--host-name", default=None,
                     help=(
                         "Only process hosts whose registered name contains this "
                         "string (case-insensitive substring match)"
                     ))
    flt.add_argument("--skip-disconnected", action="store_true", default=True,
                     help="Skip hosts in disconnected / notResponding state  (default: on)")

    # -- SSH-only modes (mutually exclusive with each other & --disable-ssh-after) --
    ssh_only = parser.add_argument_group(
        "SSH-only modes  (no commands are executed in either mode)"
    )
    ssh_only_grp = ssh_only.add_mutually_exclusive_group()
    ssh_only_grp.add_argument(
        "--ssh-only-enable",
        action="store_true",
        help=(
            "Only ENABLE the SSH service on every matched host and exit.  "
            "No SSH commands are run.  "
            "Useful for opening SSH across a cluster before a maintenance window."
        ),
    )
    ssh_only_grp.add_argument(
        "--ssh-only-disable",
        action="store_true",
        help=(
            "Only DISABLE the SSH service on every matched host and exit.  "
            "No SSH commands are run.  "
            "Useful for closing SSH across a cluster after a maintenance window."
        ),
    )

    # -- Behaviour options -------------------------------------------------
    opts = parser.add_argument_group("Behaviour options")
    opts.add_argument("--disable-ssh-after", action="store_true",
                      help=(
                          "Disable SSH service on each host after running commands.  "
                          "If this flag is NOT set, SSH is only disabled on hosts where "
                          "it was stopped BEFORE this script ran.  "
                          "Incompatible with --ssh-only-enable / --ssh-only-disable."
                      ))
    opts.add_argument("--dry-run",  action="store_true",
                      help="Simulate all actions without making any changes")
    opts.add_argument("--verbose",  action="store_true",
                      help="Print DEBUG-level output to the console (always written to log file)")
    opts.add_argument(
        "--log-file",
        default=f"vcenter_esxi_ssh_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
        help=(
            "Path to the log file.  "
            "Default: vcenter_esxi_ssh_YYYYMMDD_HHMMSS.log in the current directory."
        ),
    )
    opts.add_argument("--no-log-file", action="store_true",
                      help="Do not write a log file (console output only)")

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = build_parser()
    args   = parser.parse_args()

    # Guard: --disable-ssh-after is incompatible with the ssh-only modes
    if args.disable_ssh_after and (args.ssh_only_enable or args.ssh_only_disable):
        parser.error(
            "--disable-ssh-after cannot be combined with "
            "--ssh-only-enable or --ssh-only-disable"
        )

    # Determine the active operating mode
    if args.ssh_only_enable:
        mode = "ssh-only-enable"
    elif args.ssh_only_disable:
        mode = "ssh-only-disable"
    else:
        mode = "run-commands"

    # -- Interactive credential prompts -----------------------------------
    # vCenter password - always required
    if not args.password:
        args.password = safe_getpass(f"vCenter password for '{args.user}': ")

    # SSH password - only needed when we actually open SSH sessions (run-commands mode).
    # Never silently reuse the vCenter password; always ask explicitly.
    if mode == "run-commands" and not args.ssh_password:
        sys.stdout.write(
            f"SSH password for '{args.ssh_user}' on ESXi hosts\n"
            f"(leave blank to reuse the vCenter password): "
        )
        sys.stdout.flush()
        entered = safe_getpass("")
        if entered:
            args.ssh_password = entered
        else:
            args.ssh_password = args.password
            print("--> No SSH password entered, reusing vCenter password for SSH.")
    elif not args.ssh_password:
        args.ssh_password = args.password


    log_file = None if args.no_log_file else args.log_file
    logger   = setup_logging(log_file, args.verbose)

    # -- Banner ------------------------------------------------------------
    SEP  = "=" * 74
    sep2 = "-" * 74
    logger.info(SEP)
    logger.info(
        f"  vCenter ESXi SSH Automation  -  "
        f"started {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    if args.dry_run:
        logger.info("  *** DRY-RUN MODE - no changes will be made ***")
    mode_label = {
        "ssh-only-enable":  "SSH-ONLY ENABLE  (no commands will be run)",
        "ssh-only-disable": "SSH-ONLY DISABLE (no commands will be run)",
        "run-commands":     "RUN COMMANDS",
    }[mode]
    logger.info(f"  *** MODE: {mode_label} ***")
    logger.info(sep2)
    logger.info(f"  vCenter server    : {args.server}:{args.port}")
    logger.info(f"  vCenter user      : {args.user}")
    logger.info(f"  Cluster filter    : {args.cluster   or '(all clusters)'}")
    logger.info(f"  Host name filter  : {args.host_name or '(all hosts)'}")
    if mode == "run-commands":
        logger.info(f"  SSH user          : {args.ssh_user}")
        logger.info(f"  SSH port          : {args.ssh_port}")
        logger.info(f"  SSH timeout       : {args.ssh_timeout}s")
        logger.info(
            f"  Disable SSH after : "
            f"{'YES - always' if args.disable_ssh_after else 'only if SSH was stopped before this run'}"
        )
        logger.info(f"  Commands to run   : {len(COMMANDS_TO_RUN)}")
        for cmd in COMMANDS_TO_RUN:
            logger.info(f"    >  {cmd}")
    logger.info(
        f"  Log file          : "
        f"{os.path.abspath(log_file) if log_file else '(disabled)'}"
    )
    logger.info(SEP)

    # -- Connect to vCenter -----------------------------------------------
    si = connect_vcenter(args.server, args.user, args.password, args.port, logger)

    # -- Retrieve hosts ---------------------------------------------------
    hosts = get_all_hosts(si, args.cluster, logger)

    if not hosts:
        logger.warning("No ESXi hosts matched the current filters. Exiting.")
        Disconnect(si)
        sys.exit(0)

    results_summary: list = []
    total = len(hosts)

    # -- Per-host processing ----------------------------------------------
    for idx, host_obj in enumerate(hosts, start=1):
        info = get_host_info(host_obj)
        logger.info("")
        logger.info(f"[{idx}/{total}]  HOST : {info['name']}")
        logger.info(f"         Cluster : {info['cluster']}")
        logger.info(f"         IP      : {info['ip']}")
        logger.info(
            f"         State   : connection={info['connection_state']}  "
            f"power={info['power_state']}"
        )

        # Host name filter
        if args.host_name and args.host_name.lower() not in info["name"].lower():
            logger.info(f"   [SKIP] Host name does not match filter '{args.host_name}'")
            results_summary.append(
                {**info, "status": "SKIPPED", "note": f"name filter '{args.host_name}'"}
            )
            continue

        # Skip disconnected / powered-off hosts
        if args.skip_disconnected and info["connection_state"] in (
            "disconnected", "notResponding"
        ):
            logger.warning(f"   [SKIP] Host is {info['connection_state']}")
            results_summary.append(
                {**info, "status": "SKIPPED", "note": info["connection_state"]}
            )
            continue

        if info["power_state"] != "poweredOn":
            logger.warning(
                f"   [SKIP] Host is not powered on  (powerState={info['power_state']})"
            )
            results_summary.append(
                {**info, "status": "SKIPPED", "note": f"powerState={info['power_state']}"}
            )
            continue

        host_failed             = False
        ssh_was_already_running = is_ssh_running(host_obj)

        # ================================================================
        # MODE: --ssh-only-enable  ->  just turn SSH on, nothing else
        # ================================================================
        if mode == "ssh-only-enable":
            logger.info(
                f"   -->  SSH service: "
                f"{'RUNNING (already on)' if ssh_was_already_running else 'STOPPED -> enabling ...'}"
            )
            if ssh_was_already_running:
                logger.info(f"   [OK] SSH already running on {info['name']}, nothing to do")
                results_summary.append({**info, "status": "OK", "note": "SSH was already running"})
            elif enable_ssh(host_obj, logger, args.dry_run):
                logger.info(f"   [OK] SSH enabled on {info['name']}")
                results_summary.append({**info, "status": "OK", "note": "SSH enabled"})
            else:
                logger.error(f"   [FAIL] Could not enable SSH on {info['name']}")
                results_summary.append(
                    {**info, "status": "FAILED", "note": "could not enable SSH service"}
                )
            continue

        # ================================================================
        # MODE: --ssh-only-disable  ->  just turn SSH off, nothing else
        # ================================================================
        if mode == "ssh-only-disable":
            logger.info(
                f"   -->  SSH service: "
                f"{'RUNNING -> disabling ...' if ssh_was_already_running else 'STOPPED (already off)'}"
            )
            if not ssh_was_already_running:
                logger.info(f"   [OK] SSH already stopped on {info['name']}, nothing to do")
                results_summary.append({**info, "status": "OK", "note": "SSH was already stopped"})
            else:
                disable_ssh(host_obj, logger, args.dry_run)
                logger.info(f"   [OK] SSH disabled on {info['name']}")
                results_summary.append({**info, "status": "OK", "note": "SSH disabled"})
            continue

        # ================================================================
        # MODE: run-commands  (default)
        # ================================================================

        # -- Step 1: Enable SSH -------------------------------------------
        logger.info(
            f"   -->  SSH service: "
            f"{'RUNNING' if ssh_was_already_running else 'STOPPED'}  -> enabling ..."
        )
        if not enable_ssh(host_obj, logger, args.dry_run):
            logger.error(f"   [FAIL] Could not enable SSH on {info['name']}, skipping host")
            results_summary.append(
                {**info, "status": "FAILED", "note": "could not enable SSH service"}
            )
            continue

        # -- Step 2: Run commands via SSH ---------------------------------
        logger.info(f"   -->  Running {len(COMMANDS_TO_RUN)} command(s) via SSH ...")
        cmd_results = run_ssh_commands(
            ip=info["ip"],
            hostname=info["name"],
            ssh_user=args.ssh_user,
            ssh_password=args.ssh_password,
            ssh_port=args.ssh_port,
            commands=COMMANDS_TO_RUN,
            ssh_timeout=args.ssh_timeout,
            logger=logger,
            dry_run=args.dry_run,
        )

        failed_cmds = [c for c, r in cmd_results.items() if r["exit_code"] not in (0,)]
        if failed_cmds:
            logger.warning(f"   [WARN] {len(failed_cmds)} command(s) returned a non-zero exit code")
            host_failed = True

        # -- Step 3: Disable SSH if needed -------------------------------
        # Always disable  ->  --disable-ssh-after was set
        # Disable anyway  ->  SSH was not running before we started (we turned it on)
        # Leave running   ->  SSH was already on AND --disable-ssh-after not set
        if args.disable_ssh_after:
            reason = "--disable-ssh-after flag"
        elif not ssh_was_already_running:
            reason = "SSH was not running before this script started"
        else:
            reason = None

        if reason:
            logger.info(f"   -->  Disabling SSH service ({reason}) ...")
            disable_ssh(host_obj, logger, args.dry_run)
        else:
            logger.debug("        SSH left running (it was already enabled before this run)")

        status = "FAILED" if host_failed else "OK"
        note   = f"{len(failed_cmds)} command(s) failed" if host_failed else ""
        results_summary.append({**info, "status": status, "note": note})
        logger.info(
            f"   [{'OK' if not host_failed else 'FAIL'}] "
            f"Host {info['name']} finished  (status: {status})"
        )

    # -- Disconnect from vCenter ------------------------------------------
    logger.info("")
    logger.info("-->  Disconnecting from vCenter ...")
    try:
        Disconnect(si)
        logger.info("[OK] Disconnected from vCenter")
    except Exception as e:
        logger.warning(f"Warning during vCenter disconnect: {e}")

    # -- Final summary & exit ---------------------------------------------
    print_summary(results_summary, logger, args.dry_run)

    if log_file:
        logger.info(f"\n[OK] Full log written to: {os.path.abspath(log_file)}")

    failed_count = sum(1 for r in results_summary if r["status"] == "FAILED")
    sys.exit(1 if failed_count > 0 else 0)


if __name__ == "__main__":
    main()
