#!/usr/bin/env python3
"""
esxi_direct_ssh.py
==================
The vCenter-free sibling of vcenter_esxi_ssh.py.

Instead of discovering hosts via vCenter, the host list is read from a simple
JSON file (hosts.json by default).  All credentials and options are passed the
same way as in vcenter_esxi_ssh.py — only --server is replaced by --config.

Compatible with: Windows 10/11, Linux, macOS  |  Python 3.8+  |  ESXi 7.0+

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  INSTALLATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    pip install pyVmomi paramiko colorama

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  hosts.json FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  A plain JSON array of IP addresses or FQDNs — nothing else:

    [
      "192.168.10.11",
      "192.168.10.12",
      "esxi-03.corp.local",
      "esxi-04.corp.local"
    ]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  USAGE EXAMPLES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  # Dry-run (hosts.json in the current directory, prompted for passwords)
  python esxi_direct_ssh.py -u root --dry-run

  # Run commands and disable SSH afterwards
  python esxi_direct_ssh.py -u root --disable-ssh-after

  # Separate ESXi API password and SSH password
  python esxi_direct_ssh.py -u root -p ApiPass --ssh-user root --ssh-password SshPass

  # Enable SSH on all hosts (no commands run)
  python esxi_direct_ssh.py -u root --ssh-only-enable

  # Disable SSH on all hosts (no commands run)
  python esxi_direct_ssh.py -u root --ssh-only-disable

  # Use a different hosts file
  python esxi_direct_ssh.py -u root --config /etc/esxi/prod_hosts.json

  # Filter to a specific host by name substring
  python esxi_direct_ssh.py -u root --host-name "192.168.10.11" --disable-ssh-after

  # Full example with all options explicit
  python esxi_direct_ssh.py \
      --config hosts.json \
      -u root -p RootPassword \
      --ssh-user root --ssh-password RootPassword \
      --disable-ssh-after \
      --log-file /var/log/esxi_audit.log \
      --verbose
"""

import argparse
import getpass
import json
import logging
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
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
# COMMANDS TO RUN ON EVERY ESXi HOST  ← edit this list as needed
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

DEFAULT_CONFIG_FILE = "hosts.json"


# ---------------------------------------------------------------------------
# Logging  (identical to vcenter_esxi_ssh.py)
# ---------------------------------------------------------------------------
LOG_FORMAT_FILE = "%(asctime)s  [%(levelname)-8s]  %(message)s"
LOG_FORMAT_CLI  = "%(message)s"
DATE_FORMAT     = "%Y-%m-%d %H:%M:%S"


class ColorFormatter(logging.Formatter):
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
    logger = logging.getLogger("esxi_direct_ssh")
    logger.setLevel(logging.DEBUG)

    cli_handler = logging.StreamHandler(sys.stdout)
    cli_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    cli_handler.setFormatter(
        ColorFormatter(LOG_FORMAT_CLI) if HAS_COLOR else logging.Formatter(LOG_FORMAT_CLI)
    )
    logger.addHandler(cli_handler)

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
# hosts.json loader
# ---------------------------------------------------------------------------
def load_hosts(config_path: str) -> list:
    """
    Read the JSON host list file.
    Expected format: a JSON array of strings (IPs or FQDNs).
    Returns a plain Python list of strings.
    """
    path = Path(config_path)
    if not path.exists():
        print(f"[ERROR] Host list file not found: {path.resolve()}")
        print(
            "        Create a hosts.json file with a JSON array of IPs/FQDNs:\n"
            '        ["192.168.1.10", "192.168.1.11", "esxi-03.corp.local"]\n'
            "        or specify a different path with --config."
        )
        sys.exit(1)

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as e:
        print(f"[ERROR] hosts.json is not valid JSON: {e}")
        sys.exit(1)

    if not isinstance(data, list) or not all(isinstance(h, str) for h in data):
        print("[ERROR] hosts.json must be a JSON array of strings.")
        print('        Example: ["192.168.1.10", "192.168.1.11", "esxi-03.corp.local"]')
        sys.exit(1)

    hosts = [h.strip() for h in data if h.strip()]
    if not hosts:
        print("[ERROR] hosts.json contains no host entries.")
        sys.exit(1)

    return hosts


# ---------------------------------------------------------------------------
# Direct ESXi SOAP API connection  (pyVmomi — no vCenter required)
# ---------------------------------------------------------------------------
def connect_esxi(host: str, user: str, password: str, port: int,
                 logger: logging.Logger):
    """
    Connect directly to an ESXi host via its own built-in SOAP API.
    pyVmomi's SmartConnect works against individual ESXi hosts, not just vCenter.
    Returns the ServiceInstance on success, None on failure.
    """
    logger.debug(f"       SOAP connect -> {host}:{port}  (user={user})")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    try:
        si = SmartConnect(host=host, user=user, pwd=password, port=port, sslContext=ctx)
        about = si.content.about
        logger.debug(f"       ESXi SOAP API: {about.fullName}  (build {about.build})")
        return si
    except Exception as e:
        logger.error(f"       ESXi SOAP API connection failed for {host}: {e}")
        return None


# ---------------------------------------------------------------------------
# SSH service management  (identical API surface as vcenter_esxi_ssh.py)
# ---------------------------------------------------------------------------
def _svc_system(si):
    """
    Return the ServiceSystem MO from a directly-connected ESXi ServiceInstance.

    Correct object tree on a direct ESXi connection (no vCenter):
      rootFolder        (vim.Folder)
        childEntity[0]  (vim.Datacenter  – always named 'ha-datacenter')
          hostFolder    (vim.Folder)
            childEntity[0]  (vim.ComputeResource)
              host[0]   (vim.HostSystem)
                configManager.serviceSystem
    """
    datacenter       = si.content.rootFolder.childEntity[0]       # vim.Datacenter
    compute_resource = datacenter.hostFolder.childEntity[0]        # vim.ComputeResource
    host_system      = compute_resource.host[0]                    # vim.HostSystem
    return host_system.configManager.serviceSystem


def is_ssh_running(si) -> bool:
    try:
        for svc in _svc_system(si).serviceInfo.service:
            if svc.key == "TSM-SSH":
                return svc.running
    except Exception:
        pass
    return False


def enable_ssh(si, host: str, logger: logging.Logger, dry_run: bool) -> bool:
    try:
        if is_ssh_running(si):
            logger.debug(f"       SSH already running on {host}, skipping start")
            return True
        if dry_run:
            logger.info(f"   [DRY-RUN] Would start SSH service on {host}")
            return True
        _svc_system(si).StartService(id="TSM-SSH")
        for _ in range(10):
            if is_ssh_running(si):
                logger.debug(f"       SSH service started on {host}")
                return True
            time.sleep(1)
        logger.warning(f"       SSH service on {host} did not start within 10 s")
        return False
    except Exception as e:
        logger.error(f"       Failed to start SSH on {host}: {e}")
        return False


def disable_ssh(si, host: str, logger: logging.Logger, dry_run: bool):
    try:
        if not is_ssh_running(si):
            logger.debug(f"       SSH already stopped on {host}")
            return
        if dry_run:
            logger.info(f"   [DRY-RUN] Would stop SSH service on {host}")
            return
        _svc_system(si).StopService(id="TSM-SSH")
        logger.debug(f"       SSH service stopped on {host}")
    except Exception as e:
        logger.error(f"       Failed to stop SSH on {host}: {e}")


# ---------------------------------------------------------------------------
# SSH command execution  (identical to vcenter_esxi_ssh.py)
# ---------------------------------------------------------------------------
def run_ssh_commands(
    host: str,
    ssh_user: str,
    ssh_password: str,
    ssh_port: int,
    commands: list,
    ssh_timeout: int,
    logger: logging.Logger,
    dry_run: bool,
) -> dict:
    results = {}

    if dry_run:
        for cmd in commands:
            logger.info(f"   [DRY-RUN] {host}: would run: {cmd}")
            results[cmd] = {"stdout": "[DRY-RUN]", "stderr": "", "exit_code": 0}
        return results

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        logger.debug(
            f"       SSH connect -> {host}:{ssh_port}  "
            f"(user={ssh_user}, timeout={ssh_timeout}s)"
        )
        client.connect(
            hostname=host,
            port=ssh_port,
            username=ssh_user,
            password=ssh_password,
            timeout=ssh_timeout,
            banner_timeout=ssh_timeout,
            auth_timeout=ssh_timeout,
            look_for_keys=False,
            allow_agent=False,
        )
        logger.debug(f"       SSH connected to {host}")

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
        logger.error(f"       SSH authentication failed for {host}: {e}")
    except (socket.timeout, paramiko.ssh_exception.NoValidConnectionsError) as e:
        logger.error(f"       SSH connection failed for {host}: {e}")
    except Exception as e:
        logger.error(f"       Unexpected SSH error for {host}: {e}")
    finally:
        client.close()

    return results


# ---------------------------------------------------------------------------
# Summary  (identical to vcenter_esxi_ssh.py)
# ---------------------------------------------------------------------------
def print_summary(summary: list, logger: logging.Logger, dry_run: bool):
    sep = "=" * 74
    logger.info("")
    logger.info(sep)
    logger.info(
        f"  SUMMARY {'[DRY-RUN] ' if dry_run else ''}"
        f"– {len(summary)} host(s) processed"
    )
    logger.info(sep)

    counts = {"OK": 0, "SKIPPED": 0, "FAILED": 0}
    for r in summary:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        tag = {"OK": "[OK]   ", "SKIPPED": "[SKIP] ", "FAILED": "[FAIL] "}.get(
            r["status"], "[?]    "
        )
        logger.info(f"  {tag}  {r['host']:<40}  {r['status']}")
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
            "ESXi Direct SSH Automation – "
            "same as vcenter_esxi_ssh.py but reads hosts from a JSON file instead of vCenter"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # ── Host list ─────────────────────────────────────────────────────────
    vc = parser.add_argument_group("Host list")
    vc.add_argument(
        "--config", "-c",
        default=DEFAULT_CONFIG_FILE,
        help=(
            f"Path to the JSON host list file  "
            f"(default: {DEFAULT_CONFIG_FILE} in the current directory)"
        ),
    )

    # ── ESXi SOAP API credentials (used to manage the SSH service) ────────
    api = parser.add_argument_group("ESXi API credentials  (for SSH service management)")
    api.add_argument("-u", "--user",     required=True,
                     help="ESXi username  (e.g. root)")
    api.add_argument("-p", "--password", default=None,
                     help="ESXi password  (prompted interactively if omitted)")
    api.add_argument("--port", type=int, default=443,
                     help="ESXi HTTPS API port  (default: 443)")

    # ── SSH options ───────────────────────────────────────────────────────
    ssh = parser.add_argument_group("SSH options")
    ssh.add_argument("--ssh-user",     default="root",
                     help="SSH username on ESXi hosts  (default: root)")
    ssh.add_argument("--ssh-password", default=None,
                     help="SSH password on ESXi hosts  (prompted if omitted)")
    ssh.add_argument("--ssh-port",     type=int, default=22,
                     help="SSH port  (default: 22)")
    ssh.add_argument("--ssh-timeout",  type=int, default=30,
                     help="SSH connection/command timeout in seconds  (default: 30)")

    # ── Filtering ─────────────────────────────────────────────────────────
    flt = parser.add_argument_group("Filtering")
    flt.add_argument(
        "--host-name", default=None,
        help="Only process hosts whose IP/FQDN contains this string (case-insensitive)",
    )

    # ── SSH-only modes ────────────────────────────────────────────────────
    ssh_only = parser.add_argument_group(
        "SSH-only modes  (no commands executed in either mode)"
    )
    ssh_only_grp = ssh_only.add_mutually_exclusive_group()
    ssh_only_grp.add_argument(
        "--ssh-only-enable",
        action="store_true",
        help=(
            "Only ENABLE the SSH service on every host and exit.  "
            "No SSH commands are run."
        ),
    )
    ssh_only_grp.add_argument(
        "--ssh-only-disable",
        action="store_true",
        help=(
            "Only DISABLE the SSH service on every host and exit.  "
            "No SSH commands are run."
        ),
    )

    # ── Behaviour options ─────────────────────────────────────────────────
    opts = parser.add_argument_group("Behaviour options")
    opts.add_argument(
        "--disable-ssh-after", action="store_true",
        help=(
            "Disable SSH after running commands, even if it was already running "
            "before the script started.  "
            "Incompatible with --ssh-only-enable / --ssh-only-disable."
        ),
    )
    opts.add_argument("--dry-run",  action="store_true",
                      help="Simulate all actions without making any changes")
    opts.add_argument("--verbose",  action="store_true",
                      help="Print DEBUG-level output to the console (always written to log file)")
    opts.add_argument(
        "--log-file",
        default=f"esxi_direct_ssh_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
        help="Path to the log file  (default: esxi_direct_ssh_YYYYMMDD_HHMMSS.log)",
    )
    opts.add_argument("--no-log-file", action="store_true",
                      help="Disable log file creation (console output only)")

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = build_parser()
    args   = parser.parse_args()

    # Guard: incompatible flag combinations
    if args.disable_ssh_after and (args.ssh_only_enable or args.ssh_only_disable):
        parser.error(
            "--disable-ssh-after cannot be combined with "
            "--ssh-only-enable or --ssh-only-disable"
        )

    # Determine operating mode
    if args.ssh_only_enable:
        mode = "ssh-only-enable"
    elif args.ssh_only_disable:
        mode = "ssh-only-disable"
    else:
        mode = "run-commands"

    # ── Interactive credential prompts ────────────────────────────────────
    if not args.password:
        args.password = getpass.getpass(f"ESXi API password for '{args.user}': ")

    if mode == "run-commands" and not args.ssh_password:
        sys.stdout.write(
            f"SSH password for '{args.ssh_user}' on ESXi hosts\n"
            f"(leave blank to reuse the API password): "
        )
        sys.stdout.flush()
        entered = getpass.getpass("")
        if entered:
            args.ssh_password = entered
        else:
            args.ssh_password = args.password
            print("--> No SSH password entered, reusing API password for SSH.")
    elif not args.ssh_password:
        args.ssh_password = args.password

    log_file = None if args.no_log_file else args.log_file
    logger   = setup_logging(log_file, args.verbose)

    # ── Banner ────────────────────────────────────────────────────────────
    SEP  = "=" * 74
    sep2 = "-" * 74
    logger.info(SEP)
    logger.info(
        f"  ESXi Direct SSH Automation  –  "
        f"started {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    if args.dry_run:
        logger.info("  *** DRY-RUN MODE – no changes will be made ***")
    mode_label = {
        "ssh-only-enable":  "SSH-ONLY ENABLE  (no commands will be run)",
        "ssh-only-disable": "SSH-ONLY DISABLE (no commands will be run)",
        "run-commands":     "RUN COMMANDS",
    }[mode]
    logger.info(f"  *** MODE: {mode_label} ***")
    logger.info(sep2)
    logger.info(f"  Config file       : {os.path.abspath(args.config)}")
    logger.info(f"  ESXi API user     : {args.user}")
    logger.info(f"  ESXi API port     : {args.port}")
    logger.info(f"  Host name filter  : {args.host_name or '(all hosts)'}")
    if mode == "run-commands":
        logger.info(f"  SSH user          : {args.ssh_user}")
        logger.info(f"  SSH port          : {args.ssh_port}")
        logger.info(f"  SSH timeout       : {args.ssh_timeout}s")
        logger.info(
            f"  Disable SSH after : "
            f"{'YES – always' if args.disable_ssh_after else 'only if SSH was stopped before this run'}"
        )
        logger.info(f"  Commands to run   : {len(COMMANDS_TO_RUN)}")
        for cmd in COMMANDS_TO_RUN:
            logger.info(f"    >  {cmd}")
    logger.info(
        f"  Log file          : "
        f"{os.path.abspath(log_file) if log_file else '(disabled)'}"
    )
    logger.info(SEP)

    # ── Load host list ────────────────────────────────────────────────────
    hosts = load_hosts(args.config)

    # Apply --host-name filter
    if args.host_name:
        hosts = [h for h in hosts if args.host_name.lower() in h.lower()]
        logger.info(f"Host name filter '{args.host_name}': matched {len(hosts)} host(s)")
        if not hosts:
            logger.warning("No hosts matched the filter. Exiting.")
            sys.exit(0)

    logger.info(f"Hosts to process: {len(hosts)}")

    # ── Per-host processing ───────────────────────────────────────────────
    results_summary: list = []
    total = len(hosts)

    for idx, host in enumerate(hosts, start=1):
        logger.info("")
        logger.info(f"[{idx}/{total}]  HOST : {host}")

        # Connect to ESXi SOAP API
        if args.dry_run:
            logger.info(f"   [DRY-RUN] Would connect to ESXi SOAP API on {host}:{args.port}")
            si = None
            ssh_was_already_running = False
        else:
            si = connect_esxi(host, args.user, args.password, args.port, logger)
            if si is None:
                logger.error(f"   [FAIL] Could not connect to {host}, skipping")
                results_summary.append({"host": host, "status": "FAILED",
                                         "note": "ESXi SOAP API connection failed"})
                continue
            ssh_was_already_running = is_ssh_running(si)

        logger.info(
            f"   -->  SSH service: "
            f"{'RUNNING' if ssh_was_already_running else 'STOPPED'}"
        )

        # ════════════════════════════════════════════════════════════
        # MODE: --ssh-only-enable
        # ════════════════════════════════════════════════════════════
        if mode == "ssh-only-enable":
            if ssh_was_already_running:
                logger.info(f"   [OK] SSH already running on {host}, nothing to do")
                results_summary.append({"host": host, "status": "OK",
                                         "note": "SSH was already running"})
            elif enable_ssh(si, host, logger, args.dry_run):
                logger.info(f"   [OK] SSH enabled on {host}")
                results_summary.append({"host": host, "status": "OK", "note": "SSH enabled"})
            else:
                logger.error(f"   [FAIL] Could not enable SSH on {host}")
                results_summary.append({"host": host, "status": "FAILED",
                                         "note": "could not enable SSH service"})
            if si:
                Disconnect(si)
            continue

        # ════════════════════════════════════════════════════════════
        # MODE: --ssh-only-disable
        # ════════════════════════════════════════════════════════════
        if mode == "ssh-only-disable":
            if not ssh_was_already_running:
                logger.info(f"   [OK] SSH already stopped on {host}, nothing to do")
                results_summary.append({"host": host, "status": "OK",
                                         "note": "SSH was already stopped"})
            else:
                disable_ssh(si, host, logger, args.dry_run)
                logger.info(f"   [OK] SSH disabled on {host}")
                results_summary.append({"host": host, "status": "OK", "note": "SSH disabled"})
            if si:
                Disconnect(si)
            continue

        # ════════════════════════════════════════════════════════════
        # MODE: run-commands  (default)
        # ════════════════════════════════════════════════════════════
        host_failed = False

        # Step 1: Enable SSH
        logger.info(f"   -->  Enabling SSH ...")
        if not enable_ssh(si, host, logger, args.dry_run):
            logger.error(f"   [FAIL] Could not enable SSH on {host}, skipping")
            results_summary.append({"host": host, "status": "FAILED",
                                     "note": "could not enable SSH service"})
            if si:
                Disconnect(si)
            continue

        # Step 2: Run commands via SSH
        logger.info(f"   -->  Running {len(COMMANDS_TO_RUN)} command(s) via SSH ...")
        cmd_results = run_ssh_commands(
            host=host,
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

        # Step 3: Disable SSH if needed
        if args.disable_ssh_after:
            reason = "--disable-ssh-after flag"
        elif not ssh_was_already_running:
            reason = "SSH was not running before this script started"
        else:
            reason = None

        if reason:
            logger.info(f"   -->  Disabling SSH service ({reason}) ...")
            disable_ssh(si, host, logger, args.dry_run)
        else:
            logger.debug("        SSH left running (it was already enabled before this run)")

        if si:
            Disconnect(si)

        status = "FAILED" if host_failed else "OK"
        note   = f"{len(failed_cmds)} command(s) failed" if host_failed else ""
        results_summary.append({"host": host, "status": status, "note": note})
        logger.info(
            f"   [{'OK' if not host_failed else 'FAIL'}] "
            f"Host {host} finished  (status: {status})"
        )

    # ── Final summary & exit ──────────────────────────────────────────────
    print_summary(results_summary, logger, args.dry_run)

    if log_file:
        logger.info(f"\n[OK] Full log written to: {os.path.abspath(log_file)}")

    failed_count = sum(1 for r in results_summary if r["status"] == "FAILED")
    sys.exit(1 if failed_count > 0 else 0)


if __name__ == "__main__":
    main()
