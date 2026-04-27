#!/usr/bin/env python3
"""
vcenter_rename_local_datastores.py
===================================
Rename local VMFS datastores on ESXi hosts across one or more vCenter
instances (including Enhanced Linked Mode environments).

A datastore is treated as "local" when it is mounted by exactly ONE host
and its type is VMFS.  This matches the default 'datastore1',
'datastore1 (1)', ... naming that ESXi assigns during installation.

Compatible with: Windows 10/11, Linux, macOS  |  Python 3.8+

============================================================================
  INSTALLATION
============================================================================

    pip install pyVmomi colorama

============================================================================
  NAMING PATTERN  (--pattern)
============================================================================

  Placeholder        Description                          Example value
  ----------------------------------------------------------------
  {hostname}         Full hostname as in vCenter           esx-01a.site-a.vcf.lab
  {shortname}        First label (before first dot)        esx-01a
  {cluster}          Cluster name verbatim                 Cluster Prod A
  {cluster_slug}     Cluster name slugified (lowercase,    cluster-prod-a
                     non-alphanumeric replaced by -)
  {vcenter}          vCenter hostname/IP                   vc-mgmt-a.corp.local
  {index}            1-based 2-digit counter; empty        (empty) or -02
                     string when host has only one
                     local datastore, leading '-' included
  {index!}           Same but always shown, no leading -   01  or  02

  Default pattern:  {shortname}-local

  Examples:
    {shortname}-local             esx-01a-local  /  esx-01a-local-02
    {shortname}-ds{index!}        esx-01a-ds01   /  esx-01a-ds02
    {cluster_slug}-{shortname}    cluster-prod-a-esx-01a

  If a host has more than one local datastore and the pattern contains no
  {index} or {index!} placeholder, the script prints a warning and the
  second rename will fail with a CONFLICT error.

============================================================================
  LINKED MODE (Enhanced Linked Mode)
============================================================================

  Specify --server multiple times to process several vCenter instances in
  one run.  All vCenters must share the same SSO domain (ELM) so the same
  --user / --password works everywhere.

    python vcenter_rename_local_datastores.py \
        --server vc-site-a.corp.local \
        --server vc-site-b.corp.local \
        --user administrator@vsphere.local \
        --cluster "Cluster-Prod"

============================================================================
  USAGE EXAMPLES
============================================================================

  # List local datastores -- read-only, nothing is changed
  python vcenter_rename_local_datastores.py \
      -s vcenter.corp.local -u admin@vsphere.local --list-only

  # Dry-run -- show old and new names without making changes
  python vcenter_rename_local_datastores.py \
      -s vcenter.corp.local -u admin@vsphere.local --dry-run

  # Rename with the default pattern in all clusters
  python vcenter_rename_local_datastores.py \
      -s vcenter.corp.local -u admin@vsphere.local

  # Rename only in specific cluster(s)
  python vcenter_rename_local_datastores.py \
      -s vcenter.corp.local -u admin@vsphere.local \
      --cluster "Cluster-Prod" --cluster "Cluster-Dev"

  # Custom naming pattern
  python vcenter_rename_local_datastores.py \
      -s vcenter.corp.local -u admin@vsphere.local \
      --pattern "{shortname}-ds{index!}"

  # Multiple vCenters (ELM / linked mode)
  python vcenter_rename_local_datastores.py \
      -s vc-site-a.corp.local \
      -s vc-site-b.corp.local \
      -u administrator@vsphere.local \
      --cluster "Cluster-Prod" --dry-run

  # Skip datastores already named correctly
  python vcenter_rename_local_datastores.py \
      -s vcenter.corp.local -u admin@vsphere.local \
      --skip-already-named

  # Write a detailed log
  python vcenter_rename_local_datastores.py \
      -s vcenter.corp.local -u admin@vsphere.local \
      --dry-run --log-file C:\\Logs\\ds_rename.log
"""

import argparse
import getpass
import logging
import os
import re
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
    from colorama import init as _colorama_init, Fore, Style
    _colorama_init(autoreset=True)
    HAS_COLOR = True
except ImportError:
    HAS_COLOR = False

if MISSING:
    print(f"[ERROR] Missing packages: {', '.join(MISSING)}")
    print(f"        Install with:  pip install {' '.join(MISSING)}")
    sys.exit(1)



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
        return f"{self._C.get(record.levelno, '')}{super().format(record)}{self._R}"


def setup_logging(log_file: Optional[str], verbose: bool) -> logging.Logger:
    logger = logging.getLogger("ds_rename")
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
            fh.setFormatter(logging.Formatter(LOG_FORMAT_FILE, datefmt=DATE_FORMAT))
            logger.addHandler(fh)
        except Exception as exc:
            logger.warning(f"Could not open log file '{log_file}': {exc}")

    return logger


# ---------------------------------------------------------------------------
# vCenter connection
# ---------------------------------------------------------------------------
def connect_vcenter(host: str, user: str, password: str, port: int,
                    logger: logging.Logger):
    """Connect directly to a vCenter instance. Returns ServiceInstance or None."""
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
        logger.error(f"[FAIL] Could not connect to {host}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Inventory helpers
# ---------------------------------------------------------------------------
def _container_view(si, obj_type):
    content = si.content
    view    = content.viewManager.CreateContainerView(
        content.rootFolder, [obj_type], True
    )
    items = list(view.view)
    view.Destroy()
    return items


def get_clusters(si, cluster_filters: Optional[List[str]],
                 logger: logging.Logger) -> list:
    """Return vim.ClusterComputeResource objects, optionally filtered."""
    all_clusters = _container_view(si, vim.ClusterComputeResource)
    if not cluster_filters:
        return all_clusters
    matched = [
        c for c in all_clusters
        if any(f.lower() in c.name.lower() for f in cluster_filters)
    ]
    logger.debug(
        f"     Cluster filter matched {len(matched)} / {len(all_clusters)}"
    )
    return matched


def existing_ds_names(si) -> set:
    """Return the set of all datastore names currently in this vCenter."""
    return {ds.name for ds in _container_view(si, vim.Datastore)}


def is_local(ds, include_nfs: bool) -> bool:
    """
    Return True when the datastore is local:
      - mounted by exactly ONE host
      - type is VMFS (or NFS when include_nfs is True)
    """
    try:
        t = ds.summary.type
        if t == "VMFS" or (include_nfs and t == "NFS"):
            mounts = ds.host
            return mounts is not None and len(mounts) == 1
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------
_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")


def slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text).strip("-").lower()


def apply_pattern(pattern: str, hostname: str, cluster: str,
                  vcenter: str, idx: int, total: int) -> str:
    """
    Substitute all placeholders in pattern and return the resolved name.

    {index}  -- empty string when total==1, else  '-NN'  (with leading dash)
    {index!} -- always 'NN'  (no leading dash, always shown)
    """
    shortname    = hostname.split(".")[0]
    cluster_slug = slugify(cluster)
    idx_forced   = f"{idx:02d}"
    idx_auto     = f"-{idx:02d}" if total > 1 else ""

    result = pattern
    result = result.replace("{index!}",      idx_forced)
    result = result.replace("{index}",       idx_auto)
    result = result.replace("{hostname}",    hostname)
    result = result.replace("{shortname}",   shortname)
    result = result.replace("{cluster}",     cluster)
    result = result.replace("{cluster_slug}", cluster_slug)
    result = result.replace("{vcenter}",     vcenter)
    return result


# ---------------------------------------------------------------------------
# Rename task
# ---------------------------------------------------------------------------
def wait_task(task, timeout: int, logger: logging.Logger) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = task.info.state
        if s == vim.TaskInfo.State.success:
            return True
        if s == vim.TaskInfo.State.error:
            err = task.info.error
            msg = err.msg if hasattr(err, "msg") else str(err)
            logger.error(f"         Task error: {msg}")
            return False
        time.sleep(1)
    logger.error(f"         Task timed out after {timeout}s")
    return False


def do_rename(ds, new_name: str, timeout: int,
              logger: logging.Logger, dry_run: bool) -> bool:
    old_name = ds.name
    if dry_run:
        logger.info(
            f"   [DRY-RUN]  '{old_name}'  ->  '{new_name}'"
        )
        return True
    try:
        ok = wait_task(ds.Rename(new_name), timeout, logger)
        if ok:
            logger.info(f"   [OK]       '{old_name}'  ->  '{new_name}'")
        else:
            logger.error(f"   [FAIL]     '{old_name}'  ->  '{new_name}'")
        return ok
    except Exception as exc:
        logger.error(f"   [FAIL]     '{old_name}'  ->  '{new_name}'  ({exc})")
        return False


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
def print_summary(rows: list, logger: logging.Logger, dry_run: bool,
                  list_only: bool):
    sep = "=" * 74
    logger.info("")
    logger.info(sep)
    if list_only:
        logger.info(f"  DATASTORE INVENTORY  --  {len(rows)} local datastore(s) found")
    else:
        label = "SUMMARY [DRY-RUN]" if dry_run else "SUMMARY"
        logger.info(f"  {label}  --  {len(rows)} datastore(s) processed")
    logger.info(sep)

    counts: dict = {}
    for r in rows:
        st = r["status"]
        counts[st] = counts.get(st, 0) + 1

        vc   = r.get("vcenter", "")
        host = r.get("host", "")
        old  = r.get("old_name", "")
        new  = r.get("new_name", "")
        note = r.get("note", "")

        if list_only or st == "LIST":
            cap  = r.get("cap_gb", 0)
            free = r.get("free_gb", 0)
            logger.info(
                f"  [INFO]     vc={vc:<25}  host={host:<30}  "
                f"'{old}'  ({cap:.1f} GB / {free:.1f} GB free)"
            )
        elif new and new != old:
            tag = {
                "RENAMED":  "[OK]      ",
                "DRY-RUN":  "[DRY-RUN] ",
                "CONFLICT": "[CONFLICT]",
                "FAILED":   "[FAIL]    ",
                "SKIPPED":  "[SKIP]    ",
            }.get(st, f"[{st}]")
            logger.info(
                f"  {tag}  {host:<35}  '{old}'  ->  '{new}'"
            )
        else:
            logger.info(
                f"  [SKIP]     {host:<35}  '{old}'  ({note})"
            )

    logger.info(sep)
    if not list_only:
        verb = "Would rename" if dry_run else "Renamed"
        key  = "DRY-RUN" if dry_run else "RENAMED"
        logger.info(
            f"  {verb}: {counts.get(key, 0)}"
            f"   Skipped: {counts.get('SKIPPED', 0)}"
            f"   Conflict: {counts.get('CONFLICT', 0)}"
            f"   Failed: {counts.get('FAILED', 0)}"
        )
    logger.info(sep)


# ---------------------------------------------------------------------------
# Per-vCenter processing
# ---------------------------------------------------------------------------
def process_vcenter(
    si,
    vcenter_host: str,
    args,
    logger: logging.Logger,
) -> list:
    """
    Enumerate local datastores for one vCenter instance and either list,
    dry-run, or rename them.  Returns a list of result dicts.
    """
    rows: list = []

    clusters = get_clusters(si, args.clusters, logger)
    if not clusters:
        logger.warning(f"     No clusters matched on {vcenter_host}")
        return rows

    logger.info(
        f"     Processing {len(clusters)} cluster(s) on {vcenter_host}"
    )

    # Track names seen in this vCenter (updated as renames happen)
    taken_names = existing_ds_names(si)

    for cluster in clusters:
        logger.info("")
        logger.info(f"  CLUSTER: {cluster.name}  ({len(cluster.host)} host(s))")

        hosts = list(cluster.host)
        if args.host_name:
            hosts = [h for h in hosts
                     if args.host_name.lower() in h.name.lower()]

        if not hosts:
            logger.info("       No hosts matched the host filter")
            continue

        for host_obj in hosts:
            hostname = host_obj.name
            logger.info(f"     HOST: {hostname}")

            # Skip unhealthy hosts
            cs = str(host_obj.runtime.connectionState)
            ps = str(host_obj.runtime.powerState)
            if cs != "connected":
                logger.warning(f"       [SKIP] Host connection state: {cs}")
                continue
            if ps != "poweredOn":
                logger.warning(f"       [SKIP] Host power state: {ps}")
                continue

            # Collect local datastores visible from this host
            local_ds: list = []
            try:
                for ds in host_obj.datastore:
                    if is_local(ds, args.include_nfs):
                        local_ds.append(ds)
                        logger.debug(
                            f"       Found local ds: '{ds.name}'  "
                            f"(type={ds.summary.type})"
                        )
            except Exception as exc:
                logger.error(
                    f"       [FAIL] Cannot read datastores: {exc}"
                )
                continue

            local_ds.sort(key=lambda d: d.name)
            total = len(local_ds)

            if total == 0:
                logger.info("       No local datastores found")
                continue

            logger.info(f"       Found {total} local datastore(s)")

            # LIST-ONLY mode
            if args.list_only:
                for ds in local_ds:
                    try:
                        cap_gb  = ds.summary.capacity  / 1024 ** 3
                        free_gb = ds.summary.freeSpace / 1024 ** 3
                    except Exception:
                        cap_gb = free_gb = 0.0
                    logger.info(
                        f"         '{ds.name}'  "
                        f"{cap_gb:.1f} GB  ({free_gb:.1f} GB free)"
                    )
                    rows.append({
                        "status":   "LIST",
                        "vcenter":  vcenter_host,
                        "host":     hostname,
                        "old_name": ds.name,
                        "cap_gb":   round(cap_gb, 1),
                        "free_gb":  round(free_gb, 1),
                    })
                continue

            # RENAME mode
            has_idx = "{index}" in args.pattern or "{index!}" in args.pattern
            if total > 1 and not has_idx:
                logger.warning(
                    f"       [WARN] Host has {total} local datastores but "
                    f"pattern '{args.pattern}' contains no {{index}} or "
                    f"{{index!}} placeholder. "
                    f"All datastores will resolve to the same name -- "
                    f"the 2nd+ renames will fail with CONFLICT. "
                    f"Consider e.g. '{{shortname}}-local{{index}}'."
                )

            for idx, ds in enumerate(local_ds, start=1):
                old_name = ds.name
                new_name = apply_pattern(
                    pattern=args.pattern,
                    hostname=hostname,
                    cluster=cluster.name,
                    vcenter=vcenter_host,
                    idx=idx,
                    total=total,
                )

                # Already has the correct name
                if old_name == new_name:
                    logger.info(
                        f"       [SKIP]  '{old_name}' already has the target name"
                    )
                    rows.append({
                        "status":   "SKIPPED",
                        "vcenter":  vcenter_host,
                        "host":     hostname,
                        "old_name": old_name,
                        "new_name": new_name,
                        "note":     "already correct",
                    })
                    continue

                # Name conflict with another existing datastore
                if new_name in taken_names and new_name != old_name:
                    logger.warning(
                        f"       [CONFLICT]  Target '{new_name}' already "
                        f"exists -- skipping '{old_name}'"
                    )
                    rows.append({
                        "status":   "CONFLICT",
                        "vcenter":  vcenter_host,
                        "host":     hostname,
                        "old_name": old_name,
                        "new_name": new_name,
                        "note":     "target name already exists",
                    })
                    continue

                # Dry-run
                if args.dry_run:
                    logger.info(
                        f"       [DRY-RUN]  '{old_name}'  ->  '{new_name}'"
                    )
                    taken_names.discard(old_name)
                    taken_names.add(new_name)
                    rows.append({
                        "status":   "DRY-RUN",
                        "vcenter":  vcenter_host,
                        "host":     hostname,
                        "old_name": old_name,
                        "new_name": new_name,
                    })
                    continue

                # Live rename
                ok = do_rename(ds, new_name, args.task_timeout, logger,
                               dry_run=False)
                if ok:
                    taken_names.discard(old_name)
                    taken_names.add(new_name)
                    rows.append({
                        "status":   "RENAMED",
                        "vcenter":  vcenter_host,
                        "host":     hostname,
                        "old_name": old_name,
                        "new_name": new_name,
                    })
                else:
                    rows.append({
                        "status":   "FAILED",
                        "vcenter":  vcenter_host,
                        "host":     hostname,
                        "old_name": old_name,
                        "new_name": new_name,
                        "note":     "rename task failed",
                    })

    return rows


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Rename local VMFS datastores on ESXi hosts via vCenter. "
            "Supports multiple vCenter instances (Enhanced Linked Mode)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    vc = p.add_argument_group("vCenter connection")
    vc.add_argument(
        "-s", "--server",
        dest="servers", action="append", required=True, metavar="VCENTER",
        help=(
            "vCenter hostname or IP. Repeat for multiple vCenters: "
            "-s vc1.corp.local -s vc2.corp.local"
        ),
    )
    vc.add_argument("-u", "--user", required=True,
                    help="vCenter / SSO username")
    vc.add_argument("-p", "--password", default=None,
                    help="Password (prompted securely if omitted)")
    vc.add_argument("--port", type=int, default=443,
                    help="vCenter HTTPS port  (default: 443)")

    flt = p.add_argument_group("Filtering")
    flt.add_argument(
        "-c", "--cluster",
        dest="clusters", action="append", metavar="CLUSTER",
        help=(
            "Process only clusters whose name contains this substring "
            "(case-insensitive). Repeat for multiple clusters: "
            "--cluster Prod --cluster Dev"
        ),
    )
    flt.add_argument(
        "--host-name", default=None,
        help=(
            "Process only hosts whose name contains this substring "
            "(case-insensitive)"
        ),
    )

    naming = p.add_argument_group("Naming pattern")
    naming.add_argument(
        "--pattern", default="{shortname}-local",
        metavar="PATTERN",
        help=(
            "Naming pattern for renamed datastores. "
            "Placeholders: {hostname} {shortname} {cluster} {cluster_slug} "
            "{vcenter} {index} {index!}  "
            "(default: '{shortname}-local')"
        ),
    )

    opts = p.add_argument_group("Behaviour options")
    opts.add_argument(
        "--list-only", action="store_true",
        help=(
            "List all local datastores with their size -- "
            "do not rename anything"
        ),
    )
    opts.add_argument(
        "--skip-already-named", action="store_true",
        help="Skip datastores whose name already matches the target pattern",
    )
    opts.add_argument(
        "--include-nfs", action="store_true",
        help=(
            "Include single-host NFS datastores in addition to VMFS "
            "(default: VMFS only)"
        ),
    )
    opts.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Show what would be renamed -- print current and target names "
            "for every datastore without making any changes"
        ),
    )
    opts.add_argument(
        "--verbose", action="store_true",
        help="Print DEBUG-level output to the console",
    )
    opts.add_argument(
        "--log-file",
        default=(
            f"vcenter_rename_datastores_"
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        ),
        help=(
            "Log file path "
            "(default: vcenter_rename_datastores_YYYYMMDD_HHMMSS.log)"
        ),
    )
    opts.add_argument(
        "--no-log-file", action="store_true",
        help="Disable log file -- write to console only",
    )
    opts.add_argument(
        "--task-timeout", type=int, default=60,
        help="Seconds to wait for each vCenter rename task  (default: 60)",
    )

    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = build_parser()
    args   = parser.parse_args()

    # Prompt for password without echoing it
    if not args.password:
        args.password = safe_getpass(
            f"vCenter password for '{args.user}': "
        )

    log_file = None if args.no_log_file else args.log_file
    logger   = setup_logging(log_file, args.verbose)

    # -- Banner ------------------------------------------------------------
    SEP  = "=" * 74
    sep2 = "-" * 74
    logger.info(SEP)
    logger.info(
        f"  vCenter Local Datastore Rename  --  "
        f"started {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    if args.dry_run:
        logger.info("  *** DRY-RUN MODE -- no changes will be made ***")
    if args.list_only:
        logger.info("  *** LIST-ONLY MODE -- no changes will be made ***")
    logger.info(sep2)
    logger.info(
        f"  vCenter(s)        : {', '.join(args.servers)}"
    )
    logger.info(f"  User              : {args.user}")
    logger.info(f"  Port              : {args.port}")
    logger.info(
        f"  Cluster filter(s) : "
        f"{', '.join(args.clusters) if args.clusters else '(all clusters)'}"
    )
    logger.info(
        f"  Host filter       : {args.host_name or '(all hosts)'}"
    )
    if not args.list_only:
        logger.info(f"  Pattern           : {args.pattern}")
        logger.info(
            f"  Skip if correct   : {'YES' if args.skip_already_named else 'NO'}"
        )
        logger.info(
            f"  Include NFS       : {'YES' if args.include_nfs else 'NO (VMFS only)'}"
        )
    logger.info(
        f"  Log file          : "
        f"{os.path.abspath(log_file) if log_file else '(disabled)'}"
    )
    logger.info(SEP)

    # -- Process each vCenter ----------------------------------------------
    all_rows:     list = []
    total_failed: int  = 0

    for vc_host in args.servers:
        logger.info("")
        logger.info(f"VCENTER: {vc_host}")
        logger.info(sep2)

        si = connect_vcenter(vc_host, args.user, args.password, args.port, logger)
        if si is None:
            logger.error(f"  Skipping {vc_host} -- connection failed")
            continue

        try:
            rows = process_vcenter(si, vc_host, args, logger)
        finally:
            try:
                Disconnect(si)
                logger.debug(f"  Disconnected from {vc_host}")
            except Exception:
                pass

        all_rows.extend(rows)
        failed_here = sum(1 for r in rows if r["status"] == "FAILED")
        total_failed += failed_here

    # -- Final summary -----------------------------------------------------
    print_summary(all_rows, logger, args.dry_run, args.list_only)

    if log_file:
        logger.info(f"\n[OK] Full log written to: {os.path.abspath(log_file)}")

    sys.exit(1 if total_failed > 0 else 0)


if __name__ == "__main__":
    main()
