#!/usr/bin/env python3
"""
vcenter_export_tpm_keys.py
===========================
Export TPM encryption state and recovery keys from ESXi hosts via vCenter.

For each host the script:
  1. Discovers hosts via vCenter API (with optional cluster filtering)
  2. Enables SSH on the host via the vCenter API (if not already on)
  3. Connects via SSH and runs the following commands:
       esxcli system settings encryption get
       esxcli system settings encryption recovery list
       esxcli hardware trustedboot get
  4. Parses and collects all output
  5. Disables SSH again if it was not running before
  6. Produces CLI / HTML / TXT output

Output modes (any combination):
  - CLI   -- always shown; colour-coded summary + per-host detail
  - HTML  -- self-contained dark-theme report  (--html [file])
  - TXT   -- plain-text report                 (--txt  [file])

If --html or --txt are given without a filename a timestamped default
is created in the current working directory.

Compatible with: Windows 10/11, Linux, macOS  |  Python 3.8+  |  vSphere 7.0+

============================================================================
  INSTALLATION
============================================================================

    pip install pyVmomi paramiko colorama

============================================================================
  USAGE EXAMPLES
============================================================================

  # CLI only -- all clusters
  python vcenter_export_tpm_keys.py -s vcenter.corp.local -u admin@vsphere.local

  # HTML and TXT with explicit filenames
  python vcenter_export_tpm_keys.py -s vcenter.corp.local -u admin@vsphere.local \
      --html C:\\Reports\\tpm.html --txt C:\\Reports\\tpm.txt

  # HTML and TXT with auto-generated filenames (timestamped, current directory)
  python vcenter_export_tpm_keys.py -s vcenter.corp.local -u admin@vsphere.local \
      --html --txt

  # Specific cluster(s)
  python vcenter_export_tpm_keys.py -s vcenter.corp.local -u admin@vsphere.local \
      --cluster "Cluster-Prod" --cluster "Cluster-Dev" --html

  # Multiple vCenters (Enhanced Linked Mode)
  python vcenter_export_tpm_keys.py \
      -s vc-site-a.corp.local -s vc-site-b.corp.local \
      -u administrator@vsphere.local --html --txt

  # Custom SSH credentials (if different from vCenter)
  python vcenter_export_tpm_keys.py -s vcenter.corp.local -u admin@vsphere.local \
      --ssh-user root --ssh-password RootPass --html

  # Keep SSH enabled after the run
  python vcenter_export_tpm_keys.py -s vcenter.corp.local -u admin@vsphere.local \
      --disable-ssh-after=no --html
"""

import argparse
import ctypes
import ctypes.wintypes
import getpass
import logging
import os
import re
import socket
import ssl
import sys
import time
from datetime import datetime
from typing import List, Optional

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------
MISSING = []
try:
    from pyVim.connect import SmartConnect, Disconnect
    from pyVmomi import vim
except ImportError:
    MISSING.append("pyVmomi")

try:
    import paramiko
except ImportError:
    MISSING.append("paramiko")

try:
    from colorama import init as _ci, Fore, Style
    _ci(autoreset=True)
    HAS_COLOR = True
except ImportError:
    HAS_COLOR = False

if MISSING:
    print(f"[ERROR] Missing packages: {', '.join(MISSING)}")
    print(f"        Install with:  pip install {' '.join(MISSING)}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Timestamp used for default filenames
# ---------------------------------------------------------------------------
_RUN_TS = datetime.now().strftime("%Y%m%d_%H%M%S")
_RUN_LABEL = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Password input -- handles Alt codes and special characters on Windows
# ---------------------------------------------------------------------------
def safe_getpass(prompt: str = "") -> str:
    """
    Secure password input that correctly handles Windows Alt codes.
    Uses ReadConsoleW (line-buffered, echo disabled) on Windows so that
    Alt code composition is handled by the Windows console before the
    string is returned to Python -- regardless of code page settings.
    Falls back to getpass on non-Windows platforms.
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()

    if sys.platform == "win32":
        kernel32    = ctypes.windll.kernel32
        STD_INPUT   = -10
        ENABLE_ECHO = 0x0004
        ENABLE_LINE = 0x0002
        ENABLE_PROC = 0x0001

        h = kernel32.GetStdHandle(STD_INPUT)
        old_mode = ctypes.wintypes.DWORD()
        kernel32.GetConsoleMode(h, ctypes.byref(old_mode))
        new_mode = (old_mode.value & ~ENABLE_ECHO) | ENABLE_LINE | ENABLE_PROC
        kernel32.SetConsoleMode(h, new_mode)

        try:
            buf        = ctypes.create_unicode_buffer(512)
            chars_read = ctypes.wintypes.DWORD()
            kernel32.ReadConsoleW(h, buf, len(buf) - 1,
                                  ctypes.byref(chars_read), None)
            password = buf.value.rstrip("\r\n")
        finally:
            kernel32.SetConsoleMode(h, old_mode.value)
            sys.stdout.write("\n")
            sys.stdout.flush()

        return password
    else:
        return getpass.getpass("")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FORMAT_FILE = "%(asctime)s  [%(levelname)-8s]  %(message)s"
LOG_FORMAT_CLI  = "%(message)s"
DATE_FORMAT     = "%Y-%m-%d %H:%M:%S"


class _ColorFmt(logging.Formatter):
    _C = {
        logging.DEBUG:    Fore.CYAN    if HAS_COLOR else "",
        logging.INFO:     Fore.GREEN   if HAS_COLOR else "",
        logging.WARNING:  Fore.YELLOW  if HAS_COLOR else "",
        logging.ERROR:    Fore.RED     if HAS_COLOR else "",
        logging.CRITICAL: Fore.MAGENTA if HAS_COLOR else "",
    }
    _R = Style.RESET_ALL if HAS_COLOR else ""

    def format(self, record):
        return f"{self._C.get(record.levelno,'')}{super().format(record)}{self._R}"


def setup_logging(log_file: Optional[str], verbose: bool) -> logging.Logger:
    logger = logging.getLogger("tpm_export")
    logger.setLevel(logging.DEBUG)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(_ColorFmt(LOG_FORMAT_CLI) if HAS_COLOR
                    else logging.Formatter(LOG_FORMAT_CLI))
    logger.addHandler(ch)

    if log_file:
        try:
            d = os.path.dirname(os.path.abspath(log_file))
            if d:
                os.makedirs(d, exist_ok=True)
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(
                logging.Formatter(LOG_FORMAT_FILE, datefmt=DATE_FORMAT))
            logger.addHandler(fh)
        except Exception as exc:
            logger.warning(f"Could not open log file '{log_file}': {exc}")

    return logger


# ---------------------------------------------------------------------------
# vCenter connection and SSH service management
# ---------------------------------------------------------------------------
def connect_vcenter(host: str, user: str, password: str, port: int,
                    logger: logging.Logger):
    logger.info(f"-->  Connecting to vCenter: {host}:{port}  as '{user}'")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    try:
        si    = SmartConnect(host=host, user=user, pwd=password,
                             port=port, sslContext=ctx)
        about = si.content.about
        logger.info(f"[OK] {host} -- {about.fullName}  (build {about.build})")
        return si
    except Exception as exc:
        logger.error(f"[FAIL] Cannot connect to {host}: {exc}")
        return None


def _container_view(si, obj_type):
    content = si.content
    view    = content.viewManager.CreateContainerView(
        content.rootFolder, [obj_type], True)
    items = list(view.view)
    view.Destroy()
    return items


def get_clusters(si, filters: Optional[List[str]]) -> list:
    all_c = _container_view(si, vim.ClusterComputeResource)
    if not filters:
        return all_c
    return [c for c in all_c
            if any(f.lower() in c.name.lower() for f in filters)]


def _svc_system(host_obj):
    return host_obj.configManager.serviceSystem


def is_ssh_running(host_obj) -> bool:
    try:
        for svc in _svc_system(host_obj).serviceInfo.service:
            if svc.key == "TSM-SSH":
                return svc.running
    except Exception:
        pass
    return False


def enable_ssh(host_obj, logger: logging.Logger) -> bool:
    try:
        if is_ssh_running(host_obj):
            return True
        _svc_system(host_obj).StartService(id="TSM-SSH")
        for _ in range(12):
            if is_ssh_running(host_obj):
                return True
            time.sleep(1)
        logger.warning(f"       SSH did not start within 12s on {host_obj.name}")
        return False
    except Exception as exc:
        logger.error(f"       Failed to enable SSH on {host_obj.name}: {exc}")
        return False


def disable_ssh(host_obj, logger: logging.Logger):
    try:
        if not is_ssh_running(host_obj):
            return
        _svc_system(host_obj).StopService(id="TSM-SSH")
    except Exception as exc:
        logger.error(f"       Failed to disable SSH on {host_obj.name}: {exc}")


def get_mgmt_ip(host_obj) -> str:
    """Return the management VMkernel IP; falls back to host_obj.name."""
    try:
        mgmt_keys = set()
        for nc in host_obj.config.virtualNicManagerInfo.netConfig:
            if nc.nicType == "management":
                for s in nc.selectedVnic:
                    mgmt_keys.add(s)
        for vnic in host_obj.config.network.vnic:
            if vnic.key in mgmt_keys and vnic.spec.ip.ipAddress:
                return vnic.spec.ip.ipAddress
    except Exception:
        pass
    return host_obj.name


# ---------------------------------------------------------------------------
# SSH command execution
# ---------------------------------------------------------------------------
def run_ssh_command(ip: str, user: str, password: str, port: int,
                    command: str, timeout: int,
                    logger: logging.Logger) -> tuple:
    """
    Run one command over SSH.
    Returns (stdout_str, stderr_str, exit_code).
    On connection failure returns ("", error_message, -1).
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=ip, port=port, username=user, password=password,
            timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
            look_for_keys=False, allow_agent=False,
        )
        _, stdout, stderr = client.exec_command(command, timeout=timeout)
        out  = stdout.read().decode("utf-8", errors="replace")
        err  = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        return out, err, code
    except paramiko.AuthenticationException as exc:
        return "", f"SSH auth failed: {exc}", -1
    except (socket.timeout, paramiko.ssh_exception.NoValidConnectionsError) as exc:
        return "", f"SSH connect failed: {exc}", -1
    except Exception as exc:
        return "", f"SSH error: {exc}", -1
    finally:
        client.close()


# ---------------------------------------------------------------------------
# ESXi output parsers
# ---------------------------------------------------------------------------
def parse_kv(text: str) -> dict:
    """
    Parse key-value output of the form:
        Key Name: value
        Other Key: other value
    Returns dict with stripped keys and values.
    """
    result = {}
    for line in text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            result[k.strip()] = v.strip()
    return result


def parse_recovery_list(text: str) -> list:
    """
    Parse output of 'esxcli system settings encryption recovery list'.

    Expected output format:
        Recovery ID                            Recovery Key
        -------------------------------------  --------------------------------------------------
        xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx   AAAAA-BBBBB-CCCCC-DDDDD-EEEEE-FFFFF-GGGGG-HHHGG

    Returns list of dicts: [{"id": str, "key": str}, ...]
    """
    records = []
    lines   = text.splitlines()

    # Find the first data line (skip header and separator lines)
    data_start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        # Skip header row (contains "Recovery" in both columns) and dash separators
        if "Recovery ID" in stripped or stripped.startswith("---"):
            data_start = i + 1
            continue

        # Each data line: two whitespace-separated columns
        # Recovery IDs look like UUID format; split on 2+ spaces
        parts = re.split(r"\s{2,}", stripped)
        if len(parts) >= 2:
            records.append({"id": parts[0].strip(), "key": parts[1].strip()})
        elif len(parts) == 1 and parts[0]:
            records.append({"id": parts[0].strip(), "key": "(key not shown)"})

    return records


def parse_trustedboot(text: str) -> dict:
    """
    Parse output of 'esxcli hardware trustedboot get'.
    Returns dict with relevant TPM fields.
    """
    raw = parse_kv(text)
    return {
        "tpm_present":  raw.get("TPM Present", "false").lower() == "true",
        "tpm_version":  raw.get("TPM Version", ""),
        "tpm_2_0":      raw.get("TPM 2.0", "false").lower() == "true",
        "secure_boot":  raw.get("Secure Boot", ""),
        "tpm_enabled":  raw.get("TPM Enabled", "false").lower() == "true",
    }


def parse_encryption_get(text: str) -> dict:
    """
    Parse output of 'esxcli system settings encryption get'.
    Returns dict with encryption configuration.
    """
    raw = parse_kv(text)
    return {
        "mode":                 raw.get("Mode", ""),
        "require_secure_boot":  raw.get("Require Secure Boot", ""),
        "require_phys_presence":raw.get("Require Phys Presence", ""),
        "required_executables": raw.get("Required Executables", ""),
    }


# ---------------------------------------------------------------------------
# Per-host data collection
# ---------------------------------------------------------------------------
# Commands to run on each ESXi host via SSH
SSH_COMMANDS = {
    "encryption_get":      "esxcli system settings encryption get",
    "recovery_list":       "esxcli system settings encryption recovery list",
    "trustedboot_get":     "esxcli hardware trustedboot get",
}


def collect_host_data(host_obj, vcenter: str, ssh_user: str, ssh_password: str,
                      ssh_port: int, ssh_timeout: int,
                      logger: logging.Logger) -> dict:
    """
    Collect all TPM / encryption data from a single ESXi host.
    Returns a structured dict; never raises.
    """
    hostname = host_obj.name
    mgmt_ip  = get_mgmt_ip(host_obj)

    info = {
        # Identity
        "vcenter":            vcenter,
        "hostname":           hostname,
        "mgmt_ip":            mgmt_ip,
        "cluster":            "(standalone)",
        "uuid":               "",
        # Encryption state
        "enc_mode":           "",
        "enc_secure_boot":    "",
        "enc_phys_presence":  "",
        # TPM hardware
        "tpm_present":        False,
        "tpm_version":        "",
        "tpm_enabled":        False,
        "secure_boot":        "",
        # Recovery keys -- list of {"id": str, "key": str}
        "recovery_keys":      [],
        # Raw command outputs (for TXT report)
        "raw": {
            "encryption_get":  "",
            "recovery_list":   "",
            "trustedboot_get": "",
        },
        "errors": [],
    }

    # Cluster
    try:
        parent = host_obj.parent
        if isinstance(parent, vim.ClusterComputeResource):
            info["cluster"] = parent.name
    except Exception:
        pass

    # Host UUID from vCenter
    try:
        info["uuid"] = host_obj.summary.hardware.uuid or ""
    except Exception:
        pass

    # -- SSH command execution ---------------------------------------------
    for cmd_key, cmd_str in SSH_COMMANDS.items():
        logger.debug(f"         $ {cmd_str}")
        out, err, code = run_ssh_command(
            ip=mgmt_ip, user=ssh_user, password=ssh_password,
            port=ssh_port, command=cmd_str, timeout=ssh_timeout,
            logger=logger,
        )

        if code == -1:
            info["errors"].append(f"{cmd_key}: {err.strip()}")
            logger.warning(f"         [FAIL] {cmd_key}: {err.strip()}")
            continue

        if code != 0 and err.strip():
            # Non-fatal -- command ran but returned error (e.g. no recovery keys)
            info["errors"].append(f"{cmd_key} (exit {code}): {err.strip()}")
            logger.debug(f"         [exit {code}] {cmd_key}: {err.strip()}")

        info["raw"][cmd_key] = out

        # Parse outputs
        if cmd_key == "trustedboot_get" and out:
            tb = parse_trustedboot(out)
            info["tpm_present"] = tb["tpm_present"]
            info["tpm_version"] = tb["tpm_version"]
            info["tpm_enabled"] = tb["tpm_enabled"]
            info["secure_boot"] = tb["secure_boot"]

        elif cmd_key == "encryption_get" and out:
            enc = parse_encryption_get(out)
            info["enc_mode"]           = enc["mode"]
            info["enc_secure_boot"]    = enc["require_secure_boot"]
            info["enc_phys_presence"]  = enc["require_phys_presence"]

        elif cmd_key == "recovery_list":
            if out.strip():
                info["recovery_keys"] = parse_recovery_list(out)
            # If output is empty, there are no recovery keys configured

    return info


# ---------------------------------------------------------------------------
# CLI output
# ---------------------------------------------------------------------------
def print_cli_report(results: list, logger: logging.Logger):
    SEP  = "=" * 80
    sep2 = "-" * 80

    # -- Summary table ----------------------------------------------------
    logger.info("")
    logger.info(SEP)
    logger.info(f"  TPM / ENCRYPTION EXPORT  --  {len(results)} host(s)")
    logger.info(SEP)

    hdr = (
        f"  {'HOST':<36}  {'CLUSTER':<20}  "
        f"{'TPM':<5}  {'MODE':<12}  {'KEYS':<5}  STATUS"
    )
    logger.info(hdr)
    logger.info(f"  {sep2}")

    for r in results:
        tpm_s  = f"v{r['tpm_version']}" if r["tpm_present"] and r["tpm_version"] else ("YES" if r["tpm_present"] else "NO")
        mode   = r["enc_mode"] or "-"
        nkeys  = str(len(r["recovery_keys"])) if r["recovery_keys"] else "0"
        status = "[ERROR]" if r["errors"] else "[OK]"
        logger.info(
            f"  {r['hostname']:<36}  {r['cluster']:<20}  "
            f"{tpm_s:<5}  {mode:<12}  {nkeys:<5}  {status}"
        )

    logger.info(SEP)

    # -- Per-host detail ---------------------------------------------------
    for r in results:
        logger.info("")
        logger.info(f"  HOST: {r['hostname']}")
        logger.info(f"  {'Cluster':<22}: {r['cluster']}")
        logger.info(f"  {'vCenter':<22}: {r['vcenter']}")
        logger.info(f"  {'Management IP':<22}: {r['mgmt_ip']}")
        logger.info(f"  {'UUID':<22}: {r['uuid'] or '(not available)'}")
        logger.info(f"  {'TPM present':<22}: {'YES' if r['tpm_present'] else 'NO'}")
        if r["tpm_version"]:
            logger.info(f"  {'TPM version':<22}: {r['tpm_version']}")
        if r["secure_boot"]:
            logger.info(f"  {'Secure Boot':<22}: {r['secure_boot']}")
        if r["enc_mode"]:
            logger.info(f"  {'Encryption mode':<22}: {r['enc_mode']}")
        if r["enc_secure_boot"]:
            logger.info(f"  {'Require Secure Boot':<22}: {r['enc_secure_boot']}")
        if r["errors"]:
            for e in r["errors"]:
                logger.warning(f"  {'Error':<22}: {e}")

        if r["recovery_keys"]:
            logger.info(f"  Recovery keys ({len(r['recovery_keys'])}):")
            for rk in r["recovery_keys"]:
                logger.info(f"    ID  : {rk['id']}")
                logger.info(f"    KEY : {rk['key']}")
        else:
            logger.info(f"  Recovery keys         : (none configured)")

        logger.info(f"  {sep2}")

    logger.info(SEP)


# ---------------------------------------------------------------------------
# TXT report
# ---------------------------------------------------------------------------
def write_txt(results: list, path: str, vcenters: List[str],
              logger: logging.Logger):
    """
    Write a compact, cluster-grouped TXT file focused on recovery keys.

    Format:
        ================================================================================
        Cluster          : CLUSTER-NAME
         HOST             : hostname.fqdn
         ID  : {recovery-id}
         KEY : 123456-789012-...
        ================================================================================
    """
    SEP = "=" * 80
    lines = []

    # Group results by cluster
    clusters: dict = {}
    for r in results:
        clusters.setdefault(r["cluster"], []).append(r)

    for cluster_name, hosts in clusters.items():
        lines.append(SEP)
        lines.append(f"Cluster          : {cluster_name}")
        for r in hosts:
            lines.append(f" HOST             : {r['hostname']}")
            if r["recovery_keys"]:
                for rk in r["recovery_keys"]:
                    lines.append(f" ID  : {rk['id']}")
                    lines.append(f" KEY : {rk['key']}")
            else:
                lines.append(" ID  : (no recovery key configured)")
                lines.append(" KEY : -")
        lines.append(SEP)

    try:
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        logger.info(f"[OK] TXT report: {os.path.abspath(path)}")
    except Exception as exc:
        logger.error(f"[FAIL] TXT report: {exc}")


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------
_CSS = """
:root{--bg:#0d1117;--bg2:#161b22;--bg3:#1c2128;--bd:#30363d;
--tx:#e6edf3;--mt:#8b949e;--gr:#3fb950;--yw:#d29922;--rd:#f85149;
--bl:#58a6ff;--pu:#bc8cff;--mn:'Courier New',monospace;--sf:'Segoe UI',system-ui,sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font-family:var(--sf);font-size:14px;
line-height:1.6;padding:32px 24px}
h1{font-size:22px;font-weight:600;color:var(--bl);margin-bottom:4px}
h2{font-size:12px;font-weight:600;color:var(--mt);margin:28px 0 12px;
letter-spacing:.1em;text-transform:uppercase}
.meta{color:var(--mt);font-size:12px;margin-bottom:24px}
.meta span{margin-right:20px}
.stats{display:flex;gap:16px;margin-bottom:28px;flex-wrap:wrap}
.stat{background:var(--bg2);border:1px solid var(--bd);border-radius:8px;
padding:12px 20px;min-width:100px}
.stat-n{font-size:28px;font-weight:700;line-height:1}
.stat-l{color:var(--mt);font-size:11px;text-transform:uppercase;
letter-spacing:.06em;margin-top:4px}
table{width:100%;border-collapse:collapse;margin-bottom:28px}
thead th{background:var(--bg3);color:var(--mt);text-align:left;
padding:8px 12px;font-size:11px;letter-spacing:.08em;
text-transform:uppercase;border-bottom:1px solid var(--bd)}
tbody tr{border-bottom:1px solid var(--bd)}
tbody tr:hover{background:var(--bg2)}
td{padding:8px 12px;vertical-align:top}
.mono{font-family:var(--mn);font-size:12px;word-break:break-all}
.badge{display:inline-block;padding:2px 8px;border-radius:12px;
font-size:11px;font-weight:600;letter-spacing:.04em}
.bg{background:rgba(63,185,80,.12);color:var(--gr)}
.by{background:rgba(210,153,34,.12);color:var(--yw)}
.br{background:rgba(248,81,73,.12);color:var(--rd)}
.bb{background:rgba(88,166,255,.12);color:var(--bl)}
.bm{background:var(--bg3);color:var(--mt)}
.card{background:var(--bg2);border:1px solid var(--bd);
border-radius:8px;margin-bottom:16px;overflow:hidden}
.ch{background:var(--bg3);padding:12px 16px;display:flex;
align-items:center;gap:12px;border-bottom:1px solid var(--bd);
cursor:pointer;user-select:none}
.ch:hover{background:#1f2630}
.ct{font-weight:600;flex:1}
.cs{color:var(--mt);font-size:12px}
.cb{padding:16px;display:none}
.cb.open{display:block}
.ti{color:var(--mt);font-size:12px;transition:transform .2s}
.ti.open{transform:rotate(90deg)}
.kv{display:grid;grid-template-columns:180px 1fr;gap:4px 12px;margin-bottom:16px}
.kk{color:var(--mt);font-size:12px}
.kv2{font-size:13px}
.rk-block{background:var(--bg3);border:1px solid var(--bd);
border-radius:6px;padding:12px 14px;margin-top:8px;font-family:var(--mn);font-size:12px}
.rk-id{color:var(--mt);margin-bottom:4px}
.rk-key{color:var(--pu);word-break:break-all;font-size:13px;font-weight:600}
.no-keys{color:var(--mt);font-style:italic;font-size:13px}
details{margin-top:12px}
summary{color:var(--mt);font-size:11px;cursor:pointer;
text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px}
summary:hover{color:var(--tx)}
pre{background:var(--bg);border:1px solid var(--bd);border-radius:4px;
padding:10px;font-size:11px;overflow-x:auto;white-space:pre-wrap;
word-break:break-all;color:var(--mt)}
.err{color:var(--rd);font-size:12px;margin-top:6px}
@media print{body{background:#fff;color:#000}.cb{display:block!important}
.rk-key{color:#000}.ch{background:#f0f0f0}}
"""

_JS = """
document.querySelectorAll('.ch').forEach(h=>{
  h.addEventListener('click',()=>{
    h.nextElementSibling.classList.toggle('open');
    h.querySelector('.ti').classList.toggle('open');
  });
});
"""


def _b(txt, cls): return f'<span class="badge {cls}">{txt}</span>'


def _tpm_badge(r):
    if not r["tpm_present"]: return _b("NO TPM", "br")
    v = r["tpm_version"]
    label = f"TPM {v}" if v else "TPM"
    return _b(label, "bg")


def _enc_badge(r):
    m = r["enc_mode"]
    if not m: return _b("Not configured", "bm")
    return _b(m, "bb")


def write_html(results: list, path: str, vcenters: List[str],
               logger: logging.Logger):
    n_tpm  = sum(1 for r in results if r["tpm_present"])
    n_keys = sum(1 for r in results if r["recovery_keys"])
    n_err  = sum(1 for r in results if r["errors"])

    # Summary table
    sum_rows = []
    for r in results:
        n_keys_r   = len(r["recovery_keys"])
        keys_bold  = f"<b>{n_keys_r}</b>" if n_keys_r else str(n_keys_r)
        err_cell   = '<span class="err">ERROR</span>' if r["errors"] else "[OK]"
        sum_rows.append(
            f"<tr>"
            f"<td>{r['hostname']}</td>"
            f"<td>{r['cluster']}</td>"
            f"<td>{_tpm_badge(r)}</td>"
            f"<td>{_enc_badge(r)}</td>"
            f"<td>{keys_bold}</td>"
            f"<td>{err_cell}</td>"
            f"</tr>"
        )
    sum_rows_html = "".join(sum_rows)

    # Detail cards
    cards = []
    for r in results:
        kv_rows = [
            ("vCenter",          r["vcenter"]),
            ("Cluster",          r["cluster"]),
            ("Management IP",    f'<span class="mono">{r["mgmt_ip"]}</span>'),
            ("UUID",             f'<span class="mono">{r["uuid"]}</span>' if r["uuid"] else "(not available)"),
            ("TPM Present",      "YES" if r["tpm_present"] else "NO"),
        ]
        if r["tpm_version"]:
            kv_rows.append(("TPM Version", r["tpm_version"]))
        if r["secure_boot"]:
            kv_rows.append(("Secure Boot", r["secure_boot"]))
        if r["enc_mode"]:
            kv_rows.append(("Encryption Mode", r["enc_mode"]))
        if r["enc_secure_boot"]:
            kv_rows.append(("Require Secure Boot", r["enc_secure_boot"]))

        kv_html = '<div class="kv">'
        for k, v in kv_rows:
            kv_html += f'<div class="kk">{k}</div><div class="kv2">{v}</div>'
        kv_html += "</div>"

        # Errors
        err_html = ""
        if r["errors"]:
            err_html = "".join(
                f'<div class="err">Error: {e}</div>' for e in r["errors"]
            )

        # Recovery keys
        if r["recovery_keys"]:
            rk_html = "".join(
                f'<div class="rk-block">'
                f'<div class="rk-id">Recovery ID: {rk["id"]}</div>'
                f'<div class="rk-key">{rk["key"]}</div>'
                f'</div>'
                for rk in r["recovery_keys"]
            )
            rk_section = f"<h2>Recovery Keys ({len(r['recovery_keys'])})</h2>{rk_html}"
        else:
            rk_section = '<p class="no-keys">No recovery keys configured on this host.</p>'

        # Raw output collapsibles
        raw_html = ""
        for cmd_key, cmd_str in SSH_COMMANDS.items():
            raw = r["raw"].get(cmd_key, "").strip() or "(no output)"
            raw_html += (
                f"<details><summary>$ {cmd_str}</summary>"
                f"<pre>{raw}</pre></details>"
            )

        body = kv_html + err_html + rk_section + raw_html

        # Card header badges
        n_keys_host = len(r["recovery_keys"])
        key_badge = _b(f"{n_keys_host} key{'s' if n_keys_host != 1 else ''}", "bm") if n_keys_host else _b("0 keys", "br")

        cards.append(
            f'<div class="card">'
            f'<div class="ch">'
            f'<span class="ti">&#9654;</span>'
            f'<span class="ct">{r["hostname"]}</span>'
            f'<span class="cs">{r["cluster"]}</span>'
            f'{_tpm_badge(r)} {_enc_badge(r)} {key_badge}'
            f'</div>'
            f'<div class="cb">{body}</div>'
            f'</div>'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TPM Key Export -- {_RUN_LABEL}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>vSphere ESXi TPM / Encryption Recovery Key Export</h1>
<div class="meta">
  <span>Generated: {_RUN_LABEL}</span>
  <span>vCenter(s): {', '.join(vcenters)}</span>
</div>
<div class="stats">
  <div class="stat"><div class="stat-n">{len(results)}</div><div class="stat-l">Hosts</div></div>
  <div class="stat"><div class="stat-n" style="color:var(--gr)">{n_tpm}</div><div class="stat-l">TPM Present</div></div>
  <div class="stat"><div class="stat-n" style="color:var(--pu)">{n_keys}</div><div class="stat-l">Have Recovery Keys</div></div>
  <div class="stat"><div class="stat-n" style="color:var(--rd)">{n_err}</div><div class="stat-l">With Errors</div></div>
</div>
<h2>Summary</h2>
<table>
<thead><tr><th>Host</th><th>Cluster</th><th>TPM</th><th>Encryption</th><th>Keys</th><th>Status</th></tr></thead>
<tbody>{sum_rows_html}</tbody>
</table>
<h2>Host Details <span style="font-size:11px;font-weight:400">(click to expand)</span></h2>
{''.join(cards)}
<script>{_JS}</script>
</body>
</html>"""

    try:
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        logger.info(f"[OK] HTML report: {os.path.abspath(path)}")
    except Exception as exc:
        logger.error(f"[FAIL] HTML report: {exc}")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------
_DEFAULT_SENTINEL = "__DEFAULT__"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Export TPM encryption state and recovery keys from ESXi hosts via vCenter."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    vc = p.add_argument_group("vCenter connection")
    vc.add_argument("-s", "--server", dest="servers", action="append",
                    required=True, metavar="VCENTER",
                    help="vCenter hostname or IP. Repeat for ELM: -s vc1 -s vc2")
    vc.add_argument("-u", "--user", required=True,
                    help="vCenter / SSO username")
    vc.add_argument("-p", "--password", default=None,
                    help="vCenter password (prompted securely if omitted)")
    vc.add_argument("--port", type=int, default=443,
                    help="vCenter HTTPS port  (default: 443)")

    ssh = p.add_argument_group("SSH credentials")
    ssh.add_argument("--ssh-user", default="root",
                     help="SSH username on ESXi hosts  (default: root)")
    ssh.add_argument("--ssh-password", default=None,
                     help="SSH password (prompted separately if omitted; "
                          "press Enter to reuse vCenter password)")
    ssh.add_argument("--ssh-port", type=int, default=22,
                     help="SSH port on ESXi hosts  (default: 22)")
    ssh.add_argument("--ssh-timeout", type=int, default=30,
                     help="SSH connection/command timeout in seconds  (default: 30)")

    flt = p.add_argument_group("Filtering")
    flt.add_argument("-c", "--cluster", dest="clusters", action="append",
                     metavar="CLUSTER",
                     help=("Only process clusters whose name contains this "
                           "substring (case-insensitive, repeatable)"))
    flt.add_argument("--host-name", default=None,
                     help="Only process hosts whose name contains this substring")

    out = p.add_argument_group("Output")
    out.add_argument(
        "--html", nargs="?", const=_DEFAULT_SENTINEL, default=None,
        metavar="FILE",
        help=(
            "Write self-contained HTML report. "
            "Omit FILE to auto-generate a timestamped filename in the "
            "current directory."
        ),
    )
    out.add_argument(
        "--txt", nargs="?", const=_DEFAULT_SENTINEL, default=None,
        metavar="FILE",
        help=(
            "Write plain-text report. "
            "Omit FILE to auto-generate a timestamped filename in the "
            "current directory."
        ),
    )
    out.add_argument("--log-file", default=None,
                     help="Also write the console log to this file")
    out.add_argument("--verbose", action="store_true",
                     help="Print DEBUG-level output to the console")

    beh = p.add_argument_group("Behaviour")
    beh.add_argument(
        "--disable-ssh-after", default="auto",
        choices=["auto", "yes", "no"],
        help=(
            "When to disable SSH after collecting data on each host.  "
            "auto = disable only if it was stopped before the script started (default).  "
            "yes  = always disable.  "
            "no   = never disable (leave SSH running)."
        ),
    )

    return p


def resolve_output_path(arg_value: str, ext: str) -> Optional[str]:
    """Return the resolved file path, or None if output disabled."""
    if arg_value is None:
        return None
    if arg_value == _DEFAULT_SENTINEL:
        return os.path.join(os.getcwd(), f"tpm_export_{_RUN_TS}.{ext}")
    return arg_value


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = build_parser()
    args   = parser.parse_args()

    # Resolve output paths (handle --html / --txt without filename)
    html_path = resolve_output_path(args.html, "html")
    txt_path  = resolve_output_path(args.txt,  "txt")

    # Credential prompts
    if not args.password:
        args.password = safe_getpass(f"vCenter password for '{args.user}': ")

    if not args.ssh_password:
        sys.stdout.write(
            f"SSH password for '{args.ssh_user}' on ESXi hosts\n"
            f"(leave blank to reuse the vCenter password): "
        )
        sys.stdout.flush()
        entered = safe_getpass("")
        args.ssh_password = entered if entered else args.password
        if not entered:
            print("--> Reusing vCenter password for SSH.")

    logger = setup_logging(args.log_file, args.verbose)

    SEP  = "=" * 78
    sep2 = "-" * 78
    logger.info(SEP)
    logger.info(f"  vCenter ESXi TPM Key Export  --  {_RUN_LABEL}")
    logger.info(sep2)
    logger.info(f"  vCenter(s)   : {', '.join(args.servers)}")
    logger.info(f"  User         : {args.user}")
    logger.info(f"  SSH user     : {args.ssh_user}")
    logger.info(f"  Clusters     : {', '.join(args.clusters) if args.clusters else '(all)'}")
    logger.info(f"  Host filter  : {args.host_name or '(all)'}")
    logger.info(f"  Disable SSH  : {args.disable_ssh_after}")
    logger.info(f"  HTML report  : {os.path.abspath(html_path) if html_path else '(disabled)'}")
    logger.info(f"  TXT report   : {os.path.abspath(txt_path) if txt_path else '(disabled)'}")
    logger.info(f"  Commands     :")
    for cmd_str in SSH_COMMANDS.values():
        logger.info(f"    >  {cmd_str}")
    logger.info(SEP)

    all_results: list = []

    for vc_host in args.servers:
        logger.info("")
        logger.info(f"VCENTER: {vc_host}")
        logger.info(sep2)

        si = connect_vcenter(vc_host, args.user, args.password, args.port, logger)
        if si is None:
            logger.error(f"  Skipping {vc_host}")
            continue

        try:
            clusters = get_clusters(si, args.clusters)
            if not clusters:
                logger.warning(f"  No clusters matched on {vc_host}")
                continue

            logger.info(f"  Processing {len(clusters)} cluster(s)")

            for cluster in clusters:
                logger.info(
                    f"  CLUSTER: {cluster.name}  ({len(cluster.host)} host(s))")

                hosts = list(cluster.host)
                if args.host_name:
                    hosts = [h for h in hosts
                             if args.host_name.lower() in h.name.lower()]

                for host_obj in hosts:
                    hostname = host_obj.name
                    cs = str(host_obj.runtime.connectionState)
                    ps = str(host_obj.runtime.powerState)

                    if cs != "connected":
                        logger.warning(f"    [SKIP] {hostname} -- {cs}")
                        continue
                    if ps != "poweredOn":
                        logger.warning(f"    [SKIP] {hostname} -- powerState={ps}")
                        continue

                    logger.info(f"    HOST: {hostname}")

                    # Enable SSH
                    ssh_was_running = is_ssh_running(host_obj)
                    logger.debug(
                        f"       SSH: {'RUNNING' if ssh_was_running else 'STOPPED'}")
                    if not enable_ssh(host_obj, logger):
                        logger.error(
                            f"       [FAIL] Cannot enable SSH -- skipping {hostname}")
                        continue

                    # Collect data via SSH
                    logger.info(f"       Collecting TPM data via SSH ...")
                    info = collect_host_data(
                        host_obj    = host_obj,
                        vcenter     = vc_host,
                        ssh_user    = args.ssh_user,
                        ssh_password= args.ssh_password,
                        ssh_port    = args.ssh_port,
                        ssh_timeout = args.ssh_timeout,
                        logger      = logger,
                    )
                    info["cluster"] = cluster.name

                    tpm_s  = f"TPM v{info['tpm_version']}" if info["tpm_version"] else ("TPM YES" if info["tpm_present"] else "NO TPM")
                    n_keys = len(info["recovery_keys"])
                    logger.info(
                        f"       {tpm_s}  |  mode: {info['enc_mode'] or '-'}  |  "
                        f"recovery keys: {n_keys}"
                    )

                    all_results.append(info)

                    # Disable SSH according to policy
                    should_disable = (
                        args.disable_ssh_after == "yes"
                        or (args.disable_ssh_after == "auto" and not ssh_was_running)
                    )
                    if should_disable:
                        disable_ssh(host_obj, logger)
                        logger.debug(f"       SSH disabled on {hostname}")

        finally:
            try:
                Disconnect(si)
                logger.debug(f"  Disconnected from {vc_host}")
            except Exception:
                pass

    if not all_results:
        logger.warning("No host data collected.")
        sys.exit(0)

    # Outputs
    print_cli_report(all_results, logger)

    if html_path:
        write_html(all_results, html_path, args.servers, logger)

    if txt_path:
        write_txt(all_results, txt_path, args.servers, logger)

    n_keys = sum(1 for r in all_results if r["recovery_keys"])
    logger.info(
        f"\n[OK] Done -- {len(all_results)} host(s), "
        f"{n_keys} with recovery keys"
    )


if __name__ == "__main__":
    main()
