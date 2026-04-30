#!/usr/bin/env python3
"""
vCenter VM Report (DRS / SPBM / Tags / Issue detection)
-------------------------------------------------------
Produces a CSV and HTML report:
  * Compact main table linking to detail catalogs
  * Storage Policy catalog (full capability listing per policy)
  * DRS Rule catalog (full configuration per rule)
  * Issue detection (e.g. stretched-cluster site mismatch)

Usage:
  python vcenter_vm_report.py -s vcenter.local -u 'user@vsphere.local' \
      -o report [--cluster CLUSTER]
"""

import argparse
import csv
import html
import re
import ssl
import sys
import warnings
from collections import defaultdict
from datetime import datetime

# --- Suppress warnings ---
warnings.filterwarnings("ignore")
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    pass

# --- Colored output (colorama is optional) ---
try:
    from colorama import init as _colorama_init, Fore, Style
    _colorama_init(autoreset=True)
    HAS_COLOR = True
    C_RED    = Fore.RED
    C_GREEN  = Fore.GREEN
    C_YELLOW = Fore.YELLOW
    C_BLUE   = Fore.BLUE
    C_CYAN   = Fore.CYAN
    C_MAGENTA = Fore.MAGENTA
    C_BRIGHT = Style.BRIGHT
    C_DIM    = Style.DIM
    C_RESET  = Style.RESET_ALL
except ImportError:
    HAS_COLOR = False
    C_RED = C_GREEN = C_YELLOW = C_BLUE = C_CYAN = C_MAGENTA = ""
    C_BRIGHT = C_DIM = C_RESET = ""

from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim, pbm, VmomiSupport, SoapStubAdapter


# =====================================================================
# Password input -- handles Alt codes and special characters on Windows
# =====================================================================

def safe_getpass(prompt: str = "") -> str:
    """
    Secure password input that correctly handles Alt codes and special characters
    on Windows (e.g. Czech/Slovak keyboards where passwords contain accented chars).

    Root cause of the original problem
    -----------------------------------
    The standard getpass on Windows reads from sys.stdin which is a text stream
    bound to the console OEM code page (cp852 for Central Europe). Windows Alt
    codes (Alt+0xxx) generate characters in the ANSI code page (cp1250). When
    the two pages differ, typed special characters are silently mis-decoded and
    the resulting password string does not match the one the user intended.

    Why msvcrt.getwch() also fails
    --------------------------------
    getwch() reads characters one at a time in unbuffered mode. Alt codes work
    by holding Alt while typing a sequence of numpad digits; Windows only resolves
    and buffers the final character when Alt is released. In unbuffered mode the
    intermediate keystrokes can interfere and the composed character is not
    reliably delivered.

    Solution: ReadConsoleW with ENABLE_LINE_INPUT
    -----------------------------------------------
    ReadConsoleW is the Windows console Unicode API. With ENABLE_LINE_INPUT the
    console buffers the entire line (including Alt code composition) and only
    returns when Enter is pressed -- the same way a normal input() call works.
    With ENABLE_ECHO_INPUT cleared the typed characters are not shown. The
    result is a proper UTF-16 Unicode string regardless of any code page settings.

    On non-Windows platforms the function falls back to the standard getpass.
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()

    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes

        kernel32    = ctypes.windll.kernel32
        STD_INPUT   = -10
        ENABLE_ECHO = 0x0004   # bit to clear (hide typing)
        ENABLE_LINE = 0x0002   # keep: buffer until Enter
        ENABLE_PROC = 0x0001   # keep: process Ctrl+C / Alt codes

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

try:
    import requests
    from com.vmware.cis_client import Session
    from com.vmware.cis.tagging_client import Tag, TagAssociation, Category
    from com.vmware.vapi.std_client import DynamicID
    from vmware.vapi.lib.connect import get_requests_connector
    from vmware.vapi.security.session import create_session_security_context
    from vmware.vapi.security.user_password import create_user_password_security_context
    from vmware.vapi.stdlib.client.factories import StubConfigurationFactory
    HAS_VAPI = True
except ImportError:
    HAS_VAPI = False


# =====================================================================
# Connections
# =====================================================================

def connect_vcenter(host, user, pwd):
    """Connect to vCenter; always accepts self-signed certs (typical for
    internal/lab vCenter deployments)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    si = SmartConnect(host=host, user=user, pwd=pwd, sslContext=ctx)
    return si, ctx


def connect_pbm(si):
    session_cookie = si._stub.cookie.split('"')[1]
    VmomiSupport.GetRequestContext()["vcSessionCookie"] = session_cookie
    hostname = si._stub.host.split(":")[0]
    pbm_stub = SoapStubAdapter(
        host=hostname, version="pbm.version.version2", path="/pbm/sdk",
        poolSize=0, sslContext=si._stub.schemeArgs.get("context"),
    )
    return pbm.ServiceInstance("ServiceInstance", pbm_stub)


def connect_tagging(host, user, pwd):
    if not HAS_VAPI:
        return None
    session = requests.Session()
    session.verify = False
    connector = get_requests_connector(session=session, url=f"https://{host}/api")
    sec_ctx = create_user_password_security_context(user, pwd)
    stub_config = StubConfigurationFactory.new_std_configuration(connector)
    stub_config.connector.set_security_context(sec_ctx)
    session_id = Session(stub_config).create()
    stub_config.connector.set_security_context(create_session_security_context(session_id))
    return {
        "tag": Tag(stub_config),
        "assoc": TagAssociation(stub_config),
        "cat": Category(stub_config),
    }


# =====================================================================
# DRS rules
# =====================================================================

def _short_host(host_name):
    return host_name.split(".")[0] if host_name else host_name


def describe_rule_kind(rule):
    if isinstance(rule, vim.cluster.AffinityRuleSpec):
        return ("Keep VMs Together (required)" if getattr(rule, "mandatory", False)
                else "Keep VMs Together (preferred)")
    if isinstance(rule, vim.cluster.AntiAffinityRuleSpec):
        return ("Separate VMs (required)" if getattr(rule, "mandatory", False)
                else "Separate VMs (preferred)")
    if isinstance(rule, vim.cluster.VmHostRuleInfo):
        affine = rule.affineHostGroupName is not None
        mandatory = getattr(rule, "mandatory", False)
        if affine and mandatory: return "VMs Must Run on Hosts"
        if affine: return "VMs Should Run on Hosts"
        if mandatory: return "VMs Must Not Run on Hosts"
        return "VMs Should Not Run on Hosts"
    if isinstance(rule, vim.cluster.DependencyRuleInfo):
        return "VM Dependency"
    return type(rule).__name__


def short_rule_kind(rule_info):
    """Short label for table display."""
    t = rule_info.get("type_raw", "")
    if "Affinity" in t and "Anti" not in t:
        return "Keep Together"
    if "AntiAffinity" in t:
        return "Separate"
    if "VmHost" in t:
        affine = rule_info.get("affinity") == "affine"
        mandatory = rule_info.get("mandatory", False)
        prefix = "Must" if mandatory else "Should"
        return f"{prefix} {'Run On' if affine else 'Avoid'}"
    return t


def build_drs_rule_index(cluster):
    vm_rules = defaultdict(list)
    rules = cluster.configurationEx.rule or []

    vm_groups_members = {}
    vm_groups_objs = {}
    host_groups_members = {}
    for grp in (cluster.configurationEx.group or []):
        if isinstance(grp, vim.cluster.VmGroup):
            vm_groups_members[grp.name] = [vm.name for vm in (grp.vm or [])]
            vm_groups_objs[grp.name] = list(grp.vm or [])
        elif isinstance(grp, vim.cluster.HostGroup):
            host_groups_members[grp.name] = [_short_host(h.name) for h in (grp.host or [])]

    for rule in rules:
        rule_info = {
            "name": rule.name,
            "kind": describe_rule_kind(rule),
            "type_raw": type(rule).__name__,
            "enabled": rule.enabled,
            "mandatory": getattr(rule, "mandatory", False),
            "in_compliance": getattr(rule, "inCompliance", None),
            "key": getattr(rule, "key", None),
        }

        if isinstance(rule, (vim.cluster.AffinityRuleSpec,
                             vim.cluster.AntiAffinityRuleSpec)):
            members = [vm.name for vm in (rule.vm or [])]
            rule_info["members"] = members
            for vm in (rule.vm or []):
                copy = dict(rule_info)
                copy["other_members"] = [m for m in members if m != vm.name]
                vm_rules[vm._moId].append(copy)

        elif isinstance(rule, vim.cluster.VmHostRuleInfo):
            host_group = rule.affineHostGroupName or rule.antiAffineHostGroupName
            rule_info["vm_group"] = rule.vmGroupName
            rule_info["host_group"] = host_group
            rule_info["vm_group_members"] = vm_groups_members.get(rule.vmGroupName, [])
            rule_info["host_group_members"] = host_groups_members.get(host_group, [])
            rule_info["affinity"] = "affine" if rule.affineHostGroupName else "anti-affine"
            for vm in vm_groups_objs.get(rule.vmGroupName, []):
                vm_rules[vm._moId].append(rule_info)

    return vm_rules


# =====================================================================
# Fault domains
# =====================================================================

def get_host_fault_domains(cluster):
    """Return short_host_name -> fault_domain_name (None if not stretched)."""
    fd_map = {}
    cfg = cluster.configurationEx
    for hc in (getattr(cfg, "vsanHostConfig", None) or []):
        host = getattr(hc, "hostSystem", None)
        fd_info = getattr(hc, "faultDomainInfo", None)
        if host and fd_info and getattr(fd_info, "name", None):
            fd_map[_short_host(host.name)] = fd_info.name
    return fd_map


def get_preferred_fault_domain(si, cluster):
    """Return the name of the *preferred* fault domain for a stretched cluster,
    or None if unknown / not a stretched cluster.

    Tries the vSAN management API first (authoritative). If that fails
    (e.g. missing privileges, OSA-only build, older vCenter), tries to read
    the value from the cluster's vsanConfigInfo property as a fallback.
    """
    # Method 1: vSAN management API
    try:
        import pyVmomi
        if hasattr(pyVmomi, "vim") and hasattr(pyVmomi.vim, "cluster"):
            from pyVmomi import VmomiSupport, SoapStubAdapter
            host = si._stub.host.split(":")[0]
            stub = SoapStubAdapter(
                host=host,
                version="vim.version.version10",
                path="/vsanHealth",
                poolSize=0,
                sslContext=si._stub.schemeArgs.get("context"),
            )
            # Carry over the auth cookie
            stub.cookie = si._stub.cookie
            vsan_sc_system = pyVmomi.vim.cluster.VsanVcStretchedClusterSystem(
                "vsan-stretched-cluster-system", stub)
            try:
                pref_fd_obj = vsan_sc_system.VSANVcGetPreferredFaultDomain(
                    cluster=cluster)
                # API returns either a string FD name directly or an object
                if pref_fd_obj is None:
                    return None
                name = getattr(pref_fd_obj, "preferredFaultDomainName", None)
                if name:
                    return name
                if isinstance(pref_fd_obj, str):
                    return pref_fd_obj
            except Exception:
                pass
    except Exception:
        pass

    # Method 2: walk vsanConfigInfo (fallback heuristic)
    try:
        cfg = cluster.configurationEx
        vsan_cfg = getattr(cfg, "vsanConfigInfo", None)
        if vsan_cfg:
            stretched = getattr(vsan_cfg, "stretchedClusterInfo", None)
            if stretched:
                pref = getattr(stretched, "preferredFdName", None) or \
                       getattr(stretched, "preferredFaultDomainName", None)
                if pref:
                    return pref
    except Exception:
        pass

    return None


# =====================================================================
# Storage policy details
# =====================================================================

def _value_to_str(v):
    if v is None: return ""
    if hasattr(v, "values"): return ", ".join(str(x) for x in v.values)
    if hasattr(v, "min") and hasattr(v, "max"): return f"{v.min}-{v.max}"
    return str(v)


def _extract_capabilities(profile):
    caps = []
    constraints = getattr(profile, "constraints", None)
    if not constraints:
        return caps
    for sub in (getattr(constraints, "subProfiles", None) or []):
        for cap in (getattr(sub, "capability", None) or []):
            cap_id = getattr(cap, "id", None)
            ns = getattr(cap_id, "namespace", "") if cap_id else ""
            cap_name = getattr(cap_id, "id", "") if cap_id else ""
            for con in (getattr(cap, "constraint", None) or []):
                for prop in (getattr(con, "propertyInstance", None) or []):
                    caps.append({
                        "namespace": ns,
                        "capability": cap_name,
                        "property": getattr(prop, "id", ""),
                        "operator": getattr(prop, "operator", "") or "=",
                        "value": _value_to_str(getattr(prop, "value", None)),
                    })
    return caps


def get_vm_storage_policies(pbm_si, vm, policy_cache):
    result = {}
    if not pbm_si:
        return result
    try:
        profile_mgr = pbm_si.RetrieveContent().profileManager
    except Exception as e:
        return {"error": {"name": "<error>", "error": f"PBM unavailable: {e}"}}

    def _resolve(profile_ids):
        if not profile_ids:
            return []
        out, missing = [], []
        for pid in profile_ids:
            key = getattr(pid, "uniqueId", None) or str(pid)
            if key in policy_cache:
                out.append(policy_cache[key])
            else:
                missing.append(pid)
        if missing:
            try:
                for p in (profile_mgr.PbmRetrieveContent(profileIds=missing) or []):
                    info = {
                        "name": p.name,
                        "description": getattr(p, "description", "") or "",
                        "capabilities": _extract_capabilities(p),
                        "profile_id_obj": p.profileId,
                    }
                    key = getattr(p.profileId, "uniqueId", None) or str(p.profileId)
                    policy_cache[key] = info
                    out.append(info)
            except Exception as e:
                out.append({"name": "<unknown>", "capabilities": [], "error": str(e)})
        return out

    try:
        pm_ref = pbm.ServerObjectRef(
            objectType=pbm.ServerObjectRef.ObjectType("virtualMachine"), key=vm._moId)
        infos = _resolve(profile_mgr.PbmQueryAssociatedProfile(pm_ref) or [])
        if infos:
            result["home"] = infos[0] if len(infos) == 1 else infos[0]
    except Exception:
        pass

    try:
        for dev in vm.config.hardware.device:
            if isinstance(dev, vim.vm.device.VirtualDisk):
                disk_ref = pbm.ServerObjectRef(
                    objectType=pbm.ServerObjectRef.ObjectType("virtualDiskId"),
                    key=f"{vm._moId}:{dev.key}")
                try:
                    infos = _resolve(profile_mgr.PbmQueryAssociatedProfile(disk_ref) or [])
                    if infos:
                        label = dev.deviceInfo.label.replace(" ", "_")
                        result[label] = infos[0]
                except Exception:
                    continue
    except Exception:
        pass
    return result


# =====================================================================
# Datastore lookup + storage compatibility (PbmQueryMatchingHub)
# =====================================================================

def _human_bytes(b):
    """Render bytes as human-readable string."""
    if b is None:
        return "?"
    try:
        b = float(b)
    except (TypeError, ValueError):
        return "?"
    if b >= 1024 ** 4:
        return f"{b / 1024 ** 4:.2f} TB"
    if b >= 1024 ** 3:
        return f"{b / 1024 ** 3:.2f} GB"
    if b >= 1024 ** 2:
        return f"{b / 1024 ** 2:.2f} MB"
    if b >= 1024:
        return f"{b / 1024:.2f} KB"
    return f"{int(b)} B"


def build_datastore_lookup(content):
    """MoID -> datastore object."""
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.Datastore], True
    )
    out = {ds._moId: ds for ds in container.view}
    container.Destroy()
    return out


def build_storage_pod_lookup(content):
    """MoID -> StoragePod (datastore cluster) object."""
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.StoragePod], True
    )
    out = {pod._moId: pod for pod in container.view}
    container.Destroy()
    return out


def _datastore_summary(ds):
    """Extract usable info from a vim.Datastore summary."""
    try:
        summary = ds.summary
        return {
            "name": ds.name,
            "type": getattr(summary, "type", "") or "?",
            "capacity_b": getattr(summary, "capacity", 0) or 0,
            "free_b": getattr(summary, "freeSpace", 0) or 0,
            "accessible": getattr(summary, "accessible", True),
            "kind": "datastore",
        }
    except Exception:
        return {"name": getattr(ds, "name", "?"), "type": "?",
                "capacity_b": 0, "free_b": 0, "accessible": False,
                "kind": "datastore"}


def _pod_summary(pod):
    try:
        summary = pod.summary
        return {
            "name": pod.name,
            "type": "Datastore Cluster",
            "capacity_b": getattr(summary, "capacity", 0) or 0,
            "free_b": getattr(summary, "freeSpace", 0) or 0,
            "accessible": True,
            "kind": "pod",
        }
    except Exception:
        return {"name": getattr(pod, "name", "?"), "type": "Datastore Cluster",
                "capacity_b": 0, "free_b": 0, "accessible": False, "kind": "pod"}


def resolve_compatible_datastores(pbm_si, policy_cache, ds_lookup, pod_lookup):
    """Populate `compatible_datastores` field on each cached policy info."""
    if not pbm_si:
        return
    try:
        # PbmQueryMatchingHub lives on the placementSolver, NOT profileManager
        placement_solver = pbm_si.RetrieveContent().placementSolver
    except Exception as e:
        print(f"{C_YELLOW}[!] Placement solver unavailable: {e}{C_RESET}",
              file=sys.stderr)
        return

    total = sum(1 for v in policy_cache.values()
                if "compatible_datastores" not in v
                and v.get("profile_id_obj") is not None)
    if total == 0:
        return

    print(f"\n{C_CYAN}[*]{C_RESET} Resolving storage compatibility for "
          f"{C_BRIGHT}{total}{C_RESET} policies ...")

    counter = 0
    for key, info in list(policy_cache.items()):
        if "compatible_datastores" in info:
            continue
        profile_id = info.get("profile_id_obj")
        if profile_id is None:
            info["compatible_datastores"] = []
            continue
        counter += 1
        print(f"    [{counter}/{total}] {info.get('name', '<unknown>')} ...",
              end="", flush=True)
        try:
            hubs = placement_solver.PbmQueryMatchingHub(
                hubsToSearch=None, profile=profile_id) or []
            ds_list = []
            for hub in hubs:
                hub_type = getattr(hub, "hubType", "")
                hub_id = getattr(hub, "hubId", None)
                if hub_type == "Datastore" and hub_id in ds_lookup:
                    ds_list.append(_datastore_summary(ds_lookup[hub_id]))
                elif hub_type == "StoragePod" and hub_id in pod_lookup:
                    ds_list.append(_pod_summary(pod_lookup[hub_id]))
            ds_list.sort(key=lambda x: x["name"].lower())
            info["compatible_datastores"] = ds_list
            print(f" {C_GREEN}{len(ds_list)} compatible{C_RESET}")
        except Exception as e:
            info["compatible_datastores"] = {"error": str(e)}
            print(f" {C_RED}error: {e}{C_RESET}")


# =====================================================================
# Tags
# =====================================================================

def get_vm_tags(tagging, vm_moid):
    if not tagging:
        return []
    try:
        dyn_id = DynamicID(type="VirtualMachine", id=vm_moid)
        tag_ids = tagging["assoc"].list_attached_tags(dyn_id)
        out = []
        for tid in tag_ids:
            t = tagging["tag"].get(tid)
            c = tagging["cat"].get(t.category_id)
            out.append(f"{c.name}:{t.name}")
        return out
    except Exception:
        return []


# =====================================================================
# Issue detection
# =====================================================================

LOCALITY_HINTS = ("locality", "siteaffinity", "sitedisaster", "datalocality")

# Locality values that mean "no site preference" - data goes everywhere
LOCALITY_NEUTRAL = (
    "", "none", "rfc-2606 hosts",
    "dual site mirroring", "dual site mirroring (stretched cluster)",
    "none - standard cluster",
)


def _is_locality_capability(cap_name):
    n = (cap_name or "").lower().replace("_", "").replace(" ", "")
    return any(h in n for h in LOCALITY_HINTS)


def _resolve_policy_locality_to_fd(locality_value, preferred_fd, all_fds):
    """Translate a storage policy locality value into the actual fault domain
    name(s) it pins data to.

    Returns a set of FD names, or empty set if locality is neutral / unknown.

    Examples (with preferred_fd='primary-az' and all_fds={'primary-az','secondary-az'}):
      'Preferred Fault Domain'           -> {'primary-az'}
      'None - Keep data on Preferred'    -> {'primary-az'}
      'Secondary Fault Domain'           -> {'secondary-az'}
      'None - Keep data on Non-preferred'-> {'secondary-az'}
      'primary-az'                       -> {'primary-az'}  (literal name)
      'None'                             -> set()           (no preference)
    """
    lv = (locality_value or "").strip().lower()
    if lv in LOCALITY_NEUTRAL:
        return set()

    # Literal FD name match (the user named the policy with the actual FD name)
    for fd in all_fds:
        if lv == fd.lower():
            return {fd}

    # Preferred-FD references
    if "preferred" in lv and ("non-preferred" not in lv and "non preferred" not in lv
                              and "nonpreferred" not in lv):
        if preferred_fd:
            return {preferred_fd}
        return set()  # we don't know which FD is preferred -> can't decide

    # Non-preferred / Secondary references
    if ("non-preferred" in lv or "non preferred" in lv or
            "nonpreferred" in lv or "secondary" in lv):
        if preferred_fd and all_fds:
            others = {fd for fd in all_fds if fd != preferred_fd}
            return others
        return set()

    return set()


def _locality_matches_fd(locality_value, fd_name, preferred_fd, all_fds):
    """Decide whether a policy's locality value applies to a given FD."""
    target_fds = _resolve_policy_locality_to_fd(locality_value, preferred_fd, all_fds)
    if not target_fds:
        # Neutral or unresolvable -> treat as compatible (don't false-positive)
        return True
    return fd_name in target_fds


def detect_issues(rules, policies, host_fd_map, vm_host_short, preferred_fd=None):
    issues = []
    cluster_fds = set(v for v in host_fd_map.values() if v)

    for r in rules:
        if not r["enabled"]:
            issues.append({"severity": "info",
                "message": f"DRS rule '{r['name']}' is disabled (no protection in effect)."})
        if r.get("in_compliance") is False:
            issues.append({"severity": "warning",
                "message": f"VM is NOT currently in compliance with DRS rule '{r['name']}'."})

    for label, info in policies.items():
        if isinstance(info, dict) and info.get("error"):
            issues.append({"severity": "warning",
                "message": f"Storage policy lookup issue for {label}: {info['error']}"})

    # Mixed storage policies across disks of the same VM
    # (typical mistake: admin added a disk and forgot to set the policy,
    #  or used a different policy by accident)
    policy_per_disk = {}
    for label, info in policies.items():
        if not isinstance(info, dict): continue
        if info.get("error"): continue
        name = info.get("name")
        if name:
            policy_per_disk[label] = name
    if len(set(policy_per_disk.values())) > 1:
        # Group disks by policy for a readable message
        by_policy = defaultdict(list)
        for disk, pol in policy_per_disk.items():
            by_policy[pol].append(disk)
        parts = [f"'{pol}' on {', '.join(sorted(disks))}"
                 for pol, disks in sorted(by_policy.items())]
        issues.append({
            "severity": "warning",
            "message": ("Mixed storage policies across disks: "
                        + " | ".join(parts)
                        + ". Verify this is intentional - inconsistent policies "
                        "across a VM's disks usually indicate a configuration mistake."),
        })

    for r in rules:
        if "host_group_members" in r and not r["host_group_members"]:
            issues.append({"severity": "warning",
                "message": f"DRS rule '{r['name']}' references host group "
                           f"'{r.get('host_group')}' that is empty or unknown."})

    for r in rules:
        if "host_group_members" not in r or not r["enabled"]: continue
        if not r["mandatory"] or r.get("affinity") != "affine": continue
        if vm_host_short and vm_host_short not in r["host_group_members"]:
            issues.append({"severity": "critical",
                "message": (f"VM runs on '{vm_host_short}', NOT in host group "
                            f"'{r.get('host_group')}' required by 'must run on' rule "
                            f"'{r['name']}'.")})

    if len(cluster_fds) > 1:
        policy_localities = []
        for label, info in policies.items():
            if not isinstance(info, dict): continue
            for cap in info.get("capabilities", []) or []:
                if _is_locality_capability(cap.get("capability")) or \
                   _is_locality_capability(cap.get("property")):
                    val = str(cap.get("value", ""))
                    if val.strip().lower() in LOCALITY_NEUTRAL: continue
                    policy_localities.append(
                        (label, cap.get("capability") or cap.get("property"), val))

        for r in rules:
            if "host_group_members" not in r or not r["enabled"]: continue
            host_fds_in_rule = set(host_fd_map.get(h) for h in r["host_group_members"])
            host_fds_in_rule.discard(None)
            if not host_fds_in_rule: continue

            is_affine = r.get("affinity") == "affine"
            is_must = r["mandatory"]
            pinned_fds = (host_fds_in_rule if is_affine
                          else cluster_fds - host_fds_in_rule)
            if not pinned_fds: continue

            for disk_label, cap_name, locality_val in policy_localities:
                # Resolve which FDs the policy actually pins data to
                policy_fds = _resolve_policy_locality_to_fd(
                    locality_val, preferred_fd, cluster_fds)
                if not policy_fds:
                    # Neutral locality (e.g. "None", dual-site mirroring) ->
                    # no site constraint -> can't be a mismatch
                    continue
                # Matching: any pinned FD overlaps with policy FDs
                if not (pinned_fds & policy_fds):
                    severity = "critical" if is_must else "warning"
                    verb = "must" if is_must else "should"
                    direction = "run on" if is_affine else "NOT run on"
                    # Resolve human-readable target for the message
                    policy_target = (locality_val
                                      if any(fd.lower() == locality_val.lower()
                                             for fd in cluster_fds)
                                      else f"{locality_val} (= {sorted(policy_fds)})")
                    issues.append({"severity": severity, "message": (
                        f"Site mismatch: DRS rule '{r['name']}' says VM {verb} "
                        f"{direction} hosts in {sorted(host_fds_in_rule)}, "
                        f"effectively pinning compute to {sorted(pinned_fds)}; "
                        f"but storage policy on {disk_label} requires data on "
                        f"{policy_target}. Cross-site I/O for every R/W.")})

    if len(cluster_fds) > 1 and vm_host_short:
        host_fd = host_fd_map.get(vm_host_short)
        if host_fd:
            for label, info in policies.items():
                if not isinstance(info, dict): continue
                for cap in info.get("capabilities", []) or []:
                    if not (_is_locality_capability(cap.get("capability")) or
                            _is_locality_capability(cap.get("property"))): continue
                    val = str(cap.get("value", ""))
                    policy_fds = _resolve_policy_locality_to_fd(
                        val, preferred_fd, cluster_fds)
                    if not policy_fds:
                        continue
                    if host_fd not in policy_fds:
                        issues.append({"severity": "warning", "message": (
                            f"Runtime site mismatch: VM is currently on host "
                            f"'{vm_host_short}' (fault domain '{host_fd}'), "
                            f"but storage policy on {label} keeps data on "
                            f"'{val}' (= {sorted(policy_fds)}).")})

    seen, unique = set(), []
    for i in issues:
        key = (i["severity"], i["message"])
        if key not in seen:
            seen.add(key)
            unique.append(i)
    return unique


# =====================================================================
# CSV formatting
# =====================================================================

def format_rules_csv(rules):
    if not rules: return ""
    parts = []
    for r in rules:
        state = "enabled" if r["enabled"] else "DISABLED"
        comp = ", NOT COMPLIANT" if r.get("in_compliance") is False else ""
        s = f"{r['kind']} \"{r['name']}\" ({state}{comp})"
        if "other_members" in r:
            s += f" | with: {', '.join(r['other_members'] or ['(only this VM)'])}"
        if "host_group" in r:
            s += f" | hosts [{r['host_group']}]: {', '.join(r['host_group_members'] or ['(empty)'])}"
        parts.append(s)
    return " ;; ".join(parts)


def format_policies_csv(pol):
    if not pol: return ""
    parts = []
    for label, info in pol.items():
        if not isinstance(info, dict): continue
        cap_parts = [f"{c['capability']}={c['value']}"
                     for c in (info.get("capabilities") or [])]
        cap_str = ("[" + "; ".join(cap_parts) + "]") if cap_parts else ""
        parts.append(f"{label}={info.get('name', '<unknown>')}{cap_str}")
    return " ;; ".join(parts)


def format_issues_csv(issues):
    if not issues: return ""
    return " ;; ".join(f"[{i['severity'].upper()}] {i['message']}" for i in issues)


# =====================================================================
# HTML helpers
# =====================================================================

def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "x"


def _esc(s):
    return html.escape(str(s)) if s is not None else ""


def _power_class(state):
    return {"poweredOn": "power-on", "poweredOff": "power-off"}.get(state, "power-suspended")


def policy_anchor(name):
    return f"policy-{_slug(name)}"


def rule_anchor(cluster, name):
    return f"rule-{_slug(cluster)}-{_slug(name)}"


# =====================================================================
# HTML template
# =====================================================================

HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>vCenter VM Report</title>
<style>
  :root {
    --bg: #f5f6f8; --fg: #1f2933; --muted: #627d98; --border: #d9e2ec;
    --accent: #1f6feb; --row-alt: #fafbfc;
    --pill-bg: #e3eefc; --pill-fg: #1f6feb;
    --pill-anti-bg: #fde2e2; --pill-anti-fg: #a81d1d;
    --pill-host-bg: #e6f4ea; --pill-host-fg: #1a7f37;
    --crit-bg: #ffe5e5; --crit-fg: #a81d1d;
    --warn-bg: #fff4d6; --warn-fg: #8a6100;
    --info-bg: #e3eefc; --info-fg: #1f6feb;
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    margin: 0; background: var(--bg); color: var(--fg); font-size: 14px;
    scroll-behavior: smooth;
  }
  header {
    background: #fff; padding: 20px 28px; border-bottom: 1px solid var(--border);
    position: sticky; top: 0; z-index: 10;
  }
  header h1 { margin: 0 0 4px; font-size: 20px; font-weight: 600; }
  header .meta { color: var(--muted); font-size: 13px; }
  .summary { display: flex; gap: 14px; margin-top: 10px; flex-wrap: wrap; }
  .summary-card {
    background: #f8fafd; border: 1px solid var(--border); border-radius: 6px;
    padding: 8px 14px; font-size: 13px;
    font-family: inherit; color: inherit; text-align: left;
    cursor: pointer; transition: all 0.15s ease;
    display: flex; align-items: baseline; gap: 6px;
  }
  .summary-card:hover {
    background: #fff; border-color: var(--accent);
    transform: translateY(-1px);
    box-shadow: 0 2px 6px rgba(31,111,235,0.12);
  }
  .summary-card:focus-visible {
    outline: 2px solid var(--accent); outline-offset: 2px;
  }
  .summary-card.active {
    background: #fff; border-color: var(--accent);
    box-shadow: 0 0 0 2px rgba(31,111,235,0.18);
  }
  .summary-card .num { font-weight: 700; font-size: 16px; }
  .summary-card.crit .num { color: var(--crit-fg); }
  .summary-card.crit.active { border-color: var(--crit-fg);
                              box-shadow: 0 0 0 2px rgba(168,29,29,0.18); }
  .summary-card.warn .num { color: var(--warn-fg); }
  .summary-card.warn.active { border-color: var(--warn-fg);
                              box-shadow: 0 0 0 2px rgba(138,97,0,0.18); }
  .summary-card.info .num { color: var(--info-fg); }
  .summary-card.clean .num { color: var(--pill-host-fg); }
  .summary-card.clean.active { border-color: var(--pill-host-fg);
                               box-shadow: 0 0 0 2px rgba(26,127,55,0.18); }

  nav.toc {
    background: #fff; padding: 10px 28px; border-bottom: 1px solid var(--border);
    display: flex; gap: 18px; font-size: 13px; flex-wrap: wrap;
  }
  nav.toc a {
    color: var(--accent); text-decoration: none; font-weight: 500;
  }
  nav.toc a:hover { text-decoration: underline; }
  nav.toc .sep { color: var(--muted); }

  .controls {
    background: #fff; padding: 14px 28px; border-bottom: 1px solid var(--border);
    display: flex; gap: 12px; align-items: center; flex-wrap: wrap;
  }
  .controls input, .controls select {
    padding: 7px 10px; border: 1px solid var(--border); border-radius: 6px;
    font-size: 13px; font-family: inherit;
  }
  .controls input { min-width: 240px; flex: 1; }
  .stats { color: var(--muted); font-size: 13px; margin-left: auto; }

  main { padding: 20px 28px; }
  section.cluster-section, section.catalog { margin-bottom: 36px; }
  section h2 {
    font-size: 16px; margin: 0 0 12px; color: var(--accent);
    display: flex; align-items: center; gap: 10px;
    padding-top: 8px;
  }
  section h2 .count {
    background: var(--pill-bg); color: var(--pill-fg);
    padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600;
  }

  /* VM table */
  table {
    width: 100%; border-collapse: collapse; background: #fff;
    border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
    table-layout: fixed;
  }
  th, td {
    padding: 10px 11px; text-align: left; border-bottom: 1px solid var(--border);
    vertical-align: top; word-wrap: break-word; overflow-wrap: anywhere;
  }
  th {
    background: #f0f4f8; font-weight: 600; font-size: 12px;
    text-transform: uppercase; letter-spacing: 0.4px; color: var(--muted);
    cursor: pointer; user-select: none;
  }
  th:hover { background: #e3eefc; }
  tr:nth-child(even) td { background: var(--row-alt); }
  tr.hidden { display: none; }
  tr.has-critical td:first-child { border-left: 4px solid var(--crit-fg); }
  tr.has-warning td:first-child { border-left: 4px solid var(--warn-fg); }

  .col-vm { width: 14%; }
  .col-host { width: 9%; }
  .col-power { width: 5%; }
  .col-issues { width: 28%; }
  .col-rules { width: 17%; }
  .col-policies { width: 17%; }
  .col-tags { width: 10%; }

  /* Pills (general) */
  .pill {
    display: inline-block; padding: 2px 7px; border-radius: 4px;
    background: var(--pill-bg); color: var(--pill-fg);
    font-size: 11px; margin: 1px 2px 1px 0; white-space: nowrap;
  }
  .pill.anti { background: var(--pill-anti-bg); color: var(--pill-anti-fg); }
  .pill.host { background: var(--pill-host-bg); color: var(--pill-host-fg); }
  .pill.muted { background: #eef2f6; color: var(--muted); }
  .empty { color: #b0bcc9; font-style: italic; }
  .power-on { color: #1a7f37; font-weight: 600; }
  .power-off { color: #b0bcc9; }
  .power-suspended { color: #b58105; }

  /* Compact rule/policy links in main table */
  .ref-row {
    display: flex; align-items: baseline; gap: 6px; margin-bottom: 4px;
    line-height: 1.35; font-size: 12.5px;
  }
  .ref-row:last-child { margin-bottom: 0; }
  .ref-kind {
    flex-shrink: 0;
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.3px;
    padding: 1px 5px; border-radius: 3px; font-weight: 600;
    background: var(--pill-bg); color: var(--pill-fg);
  }
  .ref-kind.anti { background: var(--pill-anti-bg); color: var(--pill-anti-fg); }
  .ref-kind.host { background: var(--pill-host-bg); color: var(--pill-host-fg); }
  .ref-kind.muted { background: #eef2f6; color: var(--muted); }
  .ref-link {
    color: var(--accent); text-decoration: none;
    word-break: break-all; flex: 1; min-width: 0;
  }
  .ref-link:hover { text-decoration: underline; }
  .ref-flag {
    flex-shrink: 0;
    font-size: 9px; text-transform: uppercase; letter-spacing: 0.3px;
    padding: 1px 4px; border-radius: 3px; font-weight: 700;
  }
  .ref-flag.warn { background: var(--warn-bg); color: var(--warn-fg); }
  .ref-flag.crit { background: var(--crit-bg); color: var(--crit-fg); }
  .ref-flag.muted { background: #eef2f6; color: var(--muted); }
  .ref-flag.locality { background: #fff4d6; color: #8a6100; }
  .disk-label {
    flex-shrink: 0; font-size: 10px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.3px; min-width: 32px;
  }

  /* Issues */
  .issue {
    margin-bottom: 4px; padding: 6px 8px; border-radius: 4px;
    font-size: 12px; line-height: 1.4; border-left: 3px solid;
  }
  .issue:last-child { margin-bottom: 0; }
  .issue.critical { background: var(--crit-bg); color: var(--crit-fg); border-left-color: var(--crit-fg); }
  .issue.warning  { background: var(--warn-bg); color: var(--warn-fg); border-left-color: var(--warn-fg); }
  .issue.info     { background: var(--info-bg); color: var(--info-fg); border-left-color: var(--info-fg); }
  .issue .sev {
    font-weight: 700; text-transform: uppercase; font-size: 10px;
    margin-right: 6px; letter-spacing: 0.5px;
  }

  /* Detail catalogs */
  .detail-card {
    background: #fff; border: 1px solid var(--border);
    border-radius: 8px; padding: 14px 16px; margin-bottom: 12px;
    scroll-margin-top: 90px;
  }
  .detail-card:target {
    border-color: var(--accent); box-shadow: 0 0 0 3px rgba(31,111,235,0.15);
  }
  .detail-card .title-row {
    display: flex; align-items: baseline; gap: 10px;
    margin-bottom: 8px; flex-wrap: wrap;
  }
  .detail-card .title {
    font-size: 14px; font-weight: 600;
    font-family: ui-monospace, "SF Mono", Consolas, monospace;
    word-break: break-all; flex: 1; min-width: 200px;
  }
  .detail-card .subtitle {
    color: var(--muted); font-size: 12px;
    font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    font-weight: normal;
  }
  .detail-card .badges { display: flex; gap: 4px; flex-wrap: wrap; }
  .badge {
    display: inline-block; padding: 2px 7px; border-radius: 3px;
    font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.3px;
  }
  .badge.required { background: var(--crit-bg); color: var(--crit-fg); }
  .badge.preferred { background: var(--info-bg); color: var(--info-fg); }
  .badge.disabled { background: #eef2f6; color: var(--muted); }
  .badge.compliant { background: #e6f4ea; color: var(--pill-host-fg); }
  .badge.noncompliant { background: var(--warn-bg); color: var(--warn-fg); }
  .badge.locality { background: var(--warn-bg); color: var(--warn-fg); }

  .detail-grid {
    display: grid; grid-template-columns: 140px 1fr;
    gap: 4px 14px; margin-top: 8px; font-size: 13px;
  }
  .detail-grid .lbl {
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.3px;
    color: var(--muted); padding-top: 2px;
  }
  .detail-grid .val { word-break: break-word; }
  .detail-grid .val .pill { font-size: 11px; }
  .detail-grid .val.cap-table {
    font-family: ui-monospace, "SF Mono", Consolas, monospace; font-size: 12px;
  }
  .cap-row { display: flex; gap: 8px; padding: 1px 0; }
  .cap-row .ck { color: var(--muted); min-width: 200px; }
  .cap-row .cv { font-weight: 600; }
  .cap-row.locality .ck, .cap-row.locality .cv {
    background: var(--warn-bg); padding: 0 4px; border-radius: 2px;
  }

  .vm-link {
    color: var(--accent); text-decoration: none;
    font-family: ui-monospace, "SF Mono", Consolas, monospace; font-size: 12px;
  }
  .vm-link:hover { text-decoration: underline; }

  /* Compatible datastores */
  .ds-list {
    display: flex; flex-direction: column; gap: 0;
    border: 1px solid var(--border); border-radius: 4px; overflow: hidden;
  }
  .ds-row {
    display: flex; align-items: center; gap: 10px;
    padding: 6px 10px; font-size: 12px;
    border-bottom: 1px solid var(--border); background: #fff;
  }
  .ds-row:last-child { border-bottom: none; }
  .ds-row:nth-child(even) { background: var(--row-alt); }
  .ds-name {
    font-weight: 600; min-width: 160px; flex-shrink: 0;
    word-break: break-all;
  }
  .ds-type {
    font-size: 10px; padding: 1px 6px; border-radius: 3px;
    background: var(--pill-bg); color: var(--pill-fg);
    text-transform: uppercase; letter-spacing: 0.3px;
    flex-shrink: 0; font-weight: 600;
  }
  .ds-type.vsan { background: #e6f0ff; color: #0042c4; }
  .ds-type.vmfs { background: #e6f4ea; color: var(--pill-host-fg); }
  .ds-type.nfs  { background: #fff4d6; color: var(--warn-fg); }
  .ds-type.dsc  { background: #f0e6ff; color: #5b00c4; }
  .ds-bar {
    flex: 1; height: 8px; min-width: 100px;
    background: #eef2f6; border-radius: 4px; overflow: hidden;
  }
  .ds-fill {
    height: 100%; background: var(--pill-host-fg);
  }
  .ds-fill.warn { background: #d4b46a; }
  .ds-fill.crit { background: var(--crit-fg); }
  .ds-stats {
    font-size: 11px; color: var(--muted); min-width: 150px;
    text-align: right; flex-shrink: 0;
    font-family: ui-monospace, "SF Mono", Consolas, monospace;
  }
  .ds-stats strong { color: var(--fg); }

  /* Fault domains */
  .fd-cluster-block { margin-bottom: 22px; }
  .fd-cluster-name {
    margin: 14px 0 8px; font-size: 13px; font-weight: 600;
    color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px;
  }
  .fd-grid {
    display: grid; gap: 14px;
    grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  }
  .fd-card {
    background: #fff; border: 1px solid var(--border);
    border-radius: 8px; padding: 14px 16px;
  }
  .fd-card.preferred {
    border-color: var(--pill-host-fg);
    box-shadow: 0 0 0 2px rgba(26, 127, 55, 0.08);
  }
  .fd-card.witness  { border-style: dashed; opacity: 0.85; }
  .fd-card .fd-title {
    display: flex; align-items: baseline; gap: 8px;
    margin-bottom: 8px; flex-wrap: wrap;
  }
  .fd-card .fd-title .name {
    font-weight: 600; font-size: 14px; word-break: break-all; flex: 1;
  }
  .fd-card .fd-title .badge.preferred-fd {
    background: #e6f4ea; color: var(--pill-host-fg);
  }
  .fd-card .fd-title .badge.secondary-fd {
    background: #eef2f6; color: var(--muted);
  }
  .fd-card .fd-summary {
    font-size: 11px; color: var(--muted); margin-bottom: 8px;
    display: flex; gap: 12px; flex-wrap: wrap;
  }
  .fd-card .fd-summary strong { color: var(--fg); font-weight: 600; }
  .fd-host-table {
    width: 100%; border-collapse: collapse; font-size: 12px;
  }
  .fd-host-table th, .fd-host-table td {
    padding: 5px 8px; text-align: left; border-bottom: 1px solid var(--border);
    vertical-align: middle;
  }
  .fd-host-table th {
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.3px;
    color: var(--muted); font-weight: 600; background: #f8fafd; cursor: default;
  }
  .fd-host-table th:hover { background: #f8fafd; }
  .fd-host-table tr:nth-child(even) td { background: var(--row-alt); }
  .fd-host-table tr:last-child td { border-bottom: none; }
  .fd-host-table .host-name {
    font-family: ui-monospace, "SF Mono", Consolas, monospace;
    font-size: 11px; word-break: break-all;
  }
  .fd-host-table .host-state.connected { color: var(--pill-host-fg); }
  .fd-host-table .host-state.disconnected,
  .fd-host-table .host-state.notResponding { color: var(--crit-fg); font-weight: 600; }
  .fd-host-table .host-state.maint {
    color: var(--warn-fg); font-weight: 600;
  }
  .fd-host-table .num-cell {
    text-align: right; font-variant-numeric: tabular-nums;
    font-size: 11px;
  }
  .fd-warn {
    background: var(--warn-bg); color: var(--warn-fg);
    border-left: 3px solid var(--warn-fg); padding: 8px 12px;
    border-radius: 4px; font-size: 12px; margin-top: 6px;
  }

  footer { text-align: center; color: var(--muted); padding: 24px; font-size: 12px; }
</style>
</head>
<body>
<header>
  <h1>vCenter VM Report</h1>
  <div class="meta">__META__</div>
  <div class="summary">__SUMMARY__</div>
</header>
<nav class="toc">
  <a href="#vms">VMs</a><span class="sep">·</span>
  <a href="#policies">Storage Policies</a><span class="sep">·</span>
  <a href="#rules">DRS Rules</a><span class="sep">·</span>
  <a href="#faultdomains">Fault Domains</a>
</nav>
<div class="controls">
  <input type="text" id="filter" placeholder="Filter (VM, host, rule, policy, tag, issue)…">
  <select id="clusterFilter"><option value="">All clusters</option>__CLUSTER_OPTS__</select>
  <select id="severityFilter">
    <option value="">Issues: all</option>
    <option value="any">Any issue</option>
    <option value="critical">Critical only</option>
    <option value="warning-only">Warning only</option>
    <option value="warning-plus">Critical + Warning</option>
    <option value="info-only">Info only</option>
    <option value="none">No issues (clean)</option>
  </select>
  <select id="ruleFilter">
    <option value="">DRS: all</option>
    <option value="has-rule">Has rule</option>
    <option value="no-rule">No rule</option>
  </select>
  <div class="stats" id="stats"></div>
</div>
<main>
<div id="vms">__VM_TABLES__</div>
<section class="catalog" id="policies">
  <h2>Storage Policies <span class="count">__POLICY_COUNT__</span></h2>
  __POLICY_DETAILS__
</section>
<section class="catalog" id="rules">
  <h2>DRS Rules <span class="count">__RULE_COUNT__</span></h2>
  __RULE_DETAILS__
</section>
<section class="catalog" id="faultdomains">
  <h2>Fault Domains <span class="count">__FD_COUNT__</span></h2>
  __FD_DETAILS__
</section>
</main>
<footer>Generated __TIMESTAMP__</footer>
<script>
(function(){
  const filter = document.getElementById('filter');
  const clusterSel = document.getElementById('clusterFilter');
  const sevSel = document.getElementById('severityFilter');
  const ruleSel = document.getElementById('ruleFilter');
  const stats = document.getElementById('stats');
  const rows = Array.from(document.querySelectorAll('tr.vm-row'));
  const sections = Array.from(document.querySelectorAll('.cluster-section'));
  const summaryCards = Array.from(document.querySelectorAll('.summary-card'));

  function syncSummaryCards() {
    const sv = sevSel.value;
    summaryCards.forEach(card => {
      card.classList.toggle('active', card.dataset.filter === sv);
    });
  }

  function apply() {
    const q = filter.value.toLowerCase();
    const c = clusterSel.value;
    const sv = sevSel.value;
    const r = ruleSel.value;
    let visible = 0;
    rows.forEach(row => {
      const text = row.dataset.search;
      const cluster = row.dataset.cluster;
      const sev = row.dataset.severity;
      const hasRule = row.dataset.hasrule === '1';
      let show = true;
      if (q && !text.includes(q)) show = false;
      if (c && cluster !== c) show = false;
      if (sv === 'any' && sev === 'none') show = false;
      if (sv === 'critical' && sev !== 'critical') show = false;
      if (sv === 'warning-only' && sev !== 'warning') show = false;
      if (sv === 'warning-plus' && !(sev === 'critical' || sev === 'warning')) show = false;
      if (sv === 'info-only' && sev !== 'info') show = false;
      if (sv === 'none' && sev !== 'none') show = false;
      if (r === 'has-rule' && !hasRule) show = false;
      if (r === 'no-rule' && hasRule) show = false;
      row.classList.toggle('hidden', !show);
      if (show) visible++;
    });
    sections.forEach(sec => {
      const visRows = sec.querySelectorAll('tr.vm-row:not(.hidden)').length;
      sec.style.display = visRows ? '' : 'none';
      const cnt = sec.querySelector('.count');
      if (cnt) cnt.textContent = visRows + ' VMs';
    });
    stats.textContent = visible + ' / ' + rows.length + ' VMs';
    syncSummaryCards();
  }

  [filter, clusterSel, sevSel, ruleSel].forEach(el => el.addEventListener('input', apply));

  // Clickable summary tiles - data-filter on each tile matches a value
  // in the severity dropdown. Clicking the same tile again clears the filter.
  summaryCards.forEach(card => {
    card.addEventListener('click', () => {
      const target = card.dataset.filter || '';
      sevSel.value = (sevSel.value === target) ? '' : target;
      apply();
      const vmsAnchor = document.getElementById('vms');
      if (vmsAnchor) vmsAnchor.scrollIntoView({behavior: 'smooth', block: 'start'});
    });
  });

  document.querySelectorAll('table').forEach(table => {
    table.querySelectorAll('th').forEach((th, idx) => {
      th.addEventListener('click', () => {
        const tbody = table.querySelector('tbody');
        const trs = Array.from(tbody.querySelectorAll('tr'));
        const asc = th.dataset.sort !== 'asc';
        trs.sort((a, b) => {
          const av = a.children[idx].innerText.toLowerCase();
          const bv = b.children[idx].innerText.toLowerCase();
          return asc ? av.localeCompare(bv) : bv.localeCompare(av);
        });
        trs.forEach(tr => tbody.appendChild(tr));
        table.querySelectorAll('th').forEach(x => delete x.dataset.sort);
        th.dataset.sort = asc ? 'asc' : 'desc';
      });
    });
  });

  apply();
})();
</script>
</body>
</html>
"""


# =====================================================================
# HTML rendering
# =====================================================================

def _truncate(s, n=50):
    return s if len(s) <= n else s[:n - 1] + "…"


def _rule_ref(rule, cluster):
    """Compact link to rule detail in the rules catalog."""
    is_anti = "AntiAffinity" in rule["type_raw"] or rule.get("affinity") == "anti-affine"
    is_host = "VmHost" in rule["type_raw"]
    kind_cls = "anti" if is_anti else ("host" if is_host else "")
    if not rule["enabled"]:
        kind_cls += " muted"
    flags = []
    if not rule["enabled"]:
        flags.append('<span class="ref-flag muted">DISABLED</span>')
    if rule.get("in_compliance") is False:
        flags.append('<span class="ref-flag warn">NOT COMPLIANT</span>')
    return (
        f'<div class="ref-row">'
        f'<span class="ref-kind {kind_cls}">{_esc(short_rule_kind(rule))}</span>'
        f'<a class="ref-link" href="#{rule_anchor(cluster, rule["name"])}">'
        f'{_esc(_truncate(rule["name"], 60))}</a>'
        f'{"".join(flags)}'
        f'</div>'
    )


def _rules_cell(rules, cluster):
    if not rules:
        return '<span class="empty">—</span>'
    return "".join(_rule_ref(r, cluster) for r in rules)


def _policy_ref(disk_label, info):
    """Compact link to policy detail in the policies catalog."""
    if not isinstance(info, dict):
        return ""
    name = info.get("name", "—")
    has_locality = any(_is_locality_capability(c.get("capability"))
                        for c in info.get("capabilities", []) or [])
    flag = ('<span class="ref-flag locality" title="Has site/locality rule">'
            'LOCALITY</span>' if has_locality else "")
    err = ('<span class="ref-flag crit">ERROR</span>'
           if info.get("error") else "")
    return (
        f'<div class="ref-row">'
        f'<span class="disk-label">{_esc(disk_label)}</span>'
        f'<a class="ref-link" href="#{policy_anchor(name)}">'
        f'{_esc(_truncate(name, 50))}</a>'
        f'{flag}{err}'
        f'</div>'
    )


def _policies_cell(pol):
    if not pol:
        return '<span class="empty">—</span>'
    return "".join(_policy_ref(k, v) for k, v in pol.items())


def _tag_pills(tags):
    if not tags:
        return '<span class="empty">—</span>'
    return "".join(f'<span class="pill">{_esc(t)}</span>' for t in tags)


def _issues_html(issues):
    if not issues:
        return '<span class="empty">No issues detected</span>'
    out = []
    sev_order = {"critical": 0, "warning": 1, "info": 2}
    for i in sorted(issues, key=lambda x: sev_order.get(x["severity"], 9)):
        out.append(
            f'<div class="issue {i["severity"]}">'
            f'<span class="sev">{i["severity"]}</span>{_esc(i["message"])}</div>'
        )
    return "".join(out)


def _highest_severity(issues):
    if not issues: return "none"
    if any(i["severity"] == "critical" for i in issues): return "critical"
    if any(i["severity"] == "warning" for i in issues): return "warning"
    return "info"


# ---------------------------------------------------------------------
# Catalog rendering
# ---------------------------------------------------------------------

def collect_unique_policies(rows):
    """Return dict: policy_name -> {info, usage: [(vm, cluster, disk_label)]}"""
    out = {}
    for r in rows:
        for disk, info in r["_policies"].items():
            if not isinstance(info, dict): continue
            name = info.get("name") or "<unknown>"
            if name not in out:
                out[name] = {"info": info, "usage": []}
            out[name]["usage"].append((r["vm"], r["cluster"], disk))
    return out


def collect_unique_rules(rows):
    """Return dict: (cluster, rule_name) -> {info, usage: [vm names]}"""
    out = {}
    for r in rows:
        for rule in r["_rules"]:
            key = (r["cluster"], rule["name"])
            if key not in out:
                out[key] = {"info": rule, "usage": []}
            out[key]["usage"].append(r["vm"])
    return out


def _render_policy_card(name, entry):
    info = entry["info"]
    usage = entry["usage"]
    has_locality = any(_is_locality_capability(c.get("capability"))
                        for c in info.get("capabilities", []) or [])

    badges = []
    if has_locality:
        badges.append('<span class="badge locality">site locality</span>')
    if info.get("error"):
        badges.append('<span class="badge required">error</span>')

    out = [f'<div class="detail-card" id="{policy_anchor(name)}">']
    out.append('<div class="title-row">')
    out.append(f'<span class="title">{_esc(name)}</span>')
    out.append(f'<span class="badges">{"".join(badges)}</span>')
    out.append('</div>')

    if info.get("description"):
        out.append(f'<div class="subtitle">{_esc(info["description"])}</div>')

    out.append('<div class="detail-grid">')

    if info.get("error"):
        out.append('<div class="lbl">Error</div>')
        out.append(f'<div class="val">{_esc(info["error"])}</div>')

    out.append('<div class="lbl">Capabilities</div>')
    out.append('<div class="val cap-table">')
    caps = info.get("capabilities") or []
    if not caps:
        out.append('<span class="empty">none</span>')
    for cap in caps:
        is_loc = (_is_locality_capability(cap.get("capability")) or
                  _is_locality_capability(cap.get("property")))
        cls = "cap-row locality" if is_loc else "cap-row"
        key = cap.get("capability") or cap.get("property") or "?"
        if cap.get("namespace"):
            key = f"{cap['namespace']}.{key}"
        out.append(
            f'<div class="{cls}">'
            f'<span class="ck">{_esc(key)}</span>'
            f'<span class="cv">{_esc(cap.get("value"))}</span>'
            f'</div>'
        )
    out.append('</div>')

    # Usage
    if usage:
        out.append('<div class="lbl">Used by</div>')
        out.append('<div class="val">')
        # Group by vm name
        by_vm = defaultdict(list)
        for vm, cl, disk in usage:
            by_vm[vm].append((cl, disk))
        items = []
        for vm in sorted(by_vm):
            disks = ", ".join(d for _, d in by_vm[vm])
            cluster_name = by_vm[vm][0][0]
            items.append(
                f'<span class="pill">{_esc(vm)} <span class="muted">'
                f'({_esc(disks)})</span></span>'
            )
        out.append("".join(items))
        out.append('</div>')

    # Compatible datastores (from PbmQueryMatchingHub)
    ds_data = info.get("compatible_datastores")
    if ds_data is not None:
        out.append('<div class="lbl">Compatible datastores</div>')
        out.append('<div class="val">')
        out.append(_render_compatible_datastores(ds_data))
        out.append('</div>')

    out.append('</div></div>')
    return "".join(out)


def _render_compatible_datastores(ds_data):
    if isinstance(ds_data, dict) and "error" in ds_data:
        return f'<span class="empty">error: {_esc(ds_data["error"])}</span>'
    if not ds_data:
        return '<span class="empty">no compatible datastores</span>'

    rows = ['<div class="ds-list">']
    for ds in ds_data:
        cap = ds.get("capacity_b", 0) or 0
        free = ds.get("free_b", 0) or 0
        free_pct = (free / cap * 100) if cap else 0
        used_pct = max(0, min(100, 100 - free_pct))
        bar_cls = ""
        if free_pct < 10:
            bar_cls = "crit"
        elif free_pct < 25:
            bar_cls = "warn"

        # Type pill class
        t = (ds.get("type") or "").lower()
        type_cls = ""
        if "vsan" in t: type_cls = "vsan"
        elif "vmfs" in t: type_cls = "vmfs"
        elif "nfs" in t: type_cls = "nfs"
        elif ds.get("kind") == "pod": type_cls = "dsc"

        rows.append(
            f'<div class="ds-row">'
            f'<span class="ds-name">{_esc(ds["name"])}</span>'
            f'<span class="ds-type {type_cls}">{_esc(ds.get("type", "?"))}</span>'
            f'<div class="ds-bar"><div class="ds-fill {bar_cls}" '
            f'style="width: {used_pct:.1f}%;"></div></div>'
            f'<span class="ds-stats">'
            f'<strong>{_esc(_human_bytes(free))}</strong> free '
            f'/ {_esc(_human_bytes(cap))}'
            f'</span>'
            f'</div>'
        )
    rows.append('</div>')
    return "".join(rows)


def _render_rule_card(cluster, name, entry):
    rule = entry["info"]
    usage = entry["usage"]
    is_anti = "AntiAffinity" in rule["type_raw"] or rule.get("affinity") == "anti-affine"
    is_host = "VmHost" in rule["type_raw"]

    badges = []
    badges.append(
        f'<span class="badge {"required" if rule["mandatory"] else "preferred"}">'
        f'{"Required" if rule["mandatory"] else "Preferred"}</span>'
    )
    if not rule["enabled"]:
        badges.append('<span class="badge disabled">Disabled</span>')
    if rule.get("in_compliance") is False:
        badges.append('<span class="badge noncompliant">Not Compliant</span>')
    elif rule.get("in_compliance") is True:
        badges.append('<span class="badge compliant">Compliant</span>')

    out = [f'<div class="detail-card" id="{rule_anchor(cluster, name)}">']
    out.append('<div class="title-row">')
    out.append(f'<span class="title">{_esc(name)}</span>')
    out.append(f'<span class="badges">{"".join(badges)}</span>')
    out.append('</div>')
    out.append(f'<div class="subtitle">{_esc(rule["kind"])} · cluster: {_esc(cluster)}</div>')

    out.append('<div class="detail-grid">')

    if "members" in rule:
        out.append('<div class="lbl">VM members</div>')
        pills = "".join(
            f'<span class="pill">{_esc(m)}</span>' for m in rule["members"]
        )
        out.append(f'<div class="val">{pills}</div>')

    if "vm_group" in rule:
        empty_span = '<span class="empty">empty</span>'
        out.append('<div class="lbl">VM group</div>')
        members = rule.get("vm_group_members") or []
        pills = "".join(f'<span class="pill">{_esc(m)}</span>' for m in members)
        body = pills or empty_span
        out.append(
            f'<div class="val"><strong>{_esc(rule["vm_group"])}</strong> '
            f'({len(members)} VMs)<br>{body}</div>'
        )
        out.append('<div class="lbl">Host group</div>')
        hosts = rule.get("host_group_members") or []
        pills = "".join(f'<span class="pill host">{_esc(h)}</span>' for h in hosts)
        body = pills or empty_span
        out.append(
            f'<div class="val"><strong>{_esc(rule["host_group"])}</strong> '
            f'({len(hosts)} hosts)<br>{body}</div>'
        )

    # Usage (which VMs hit this rule in our scan)
    if usage:
        out.append('<div class="lbl">Affected VMs</div>')
        unique_usage = sorted(set(usage))
        pills = "".join(f'<span class="pill">{_esc(v)}</span>' for v in unique_usage)
        out.append(f'<div class="val">{pills}</div>')

    out.append('</div></div>')
    return "".join(out)


# ---------------------------------------------------------------------
# Fault domain rendering
# ---------------------------------------------------------------------

def _host_state_class(connection, in_maintenance):
    if in_maintenance:
        return "maint"
    return (connection or "").strip()


def _host_state_label(connection, in_maintenance):
    if in_maintenance:
        return "Maintenance"
    return connection or "?"


def _render_fault_domain_card(fd_name, hosts, preferred_fd, vm_count_by_fd):
    is_preferred = (preferred_fd is not None and fd_name == preferred_fd)
    is_secondary = (preferred_fd is not None and fd_name and fd_name != preferred_fd
                    and len({h["fd"] for h in hosts if h["fd"]} | {preferred_fd}) > 1)

    # Aggregate stats
    total_cores = sum(h.get("cpu_cores") or 0 for h in hosts)
    total_mem = sum(h.get("memory_b") or 0 for h in hosts)
    total_vms = sum(h.get("vm_count", 0) for h in hosts)
    connected = sum(1 for h in hosts
                    if (h.get("connection") == "connected"
                        and not h.get("in_maintenance")))

    classes = ["fd-card"]
    if is_preferred: classes.append("preferred")

    out = [f'<div class="{" ".join(classes)}">']
    out.append('<div class="fd-title">')
    out.append(f'<span class="name">{_esc(fd_name)}</span>')
    if is_preferred:
        out.append('<span class="badge preferred-fd">Preferred</span>')
    elif preferred_fd:
        out.append('<span class="badge secondary-fd">Secondary</span>')
    out.append('</div>')

    summary_parts = []
    summary_parts.append(f'<span><strong>{len(hosts)}</strong> host(s)</span>')
    summary_parts.append(f'<span><strong>{connected}</strong> active</span>')
    summary_parts.append(f'<span><strong>{total_vms}</strong> VMs</span>')
    if total_cores:
        summary_parts.append(f'<span><strong>{total_cores}</strong> cores</span>')
    if total_mem:
        summary_parts.append(f'<span><strong>{_human_bytes(total_mem)}</strong> RAM</span>')
    out.append(f'<div class="fd-summary">{"".join(summary_parts)}</div>')

    out.append('<table class="fd-host-table"><thead><tr>')
    out.append('<th>Host</th><th>State</th>'
               '<th class="num-cell">VMs</th>'
               '<th class="num-cell">Cores</th>'
               '<th class="num-cell">RAM</th>')
    out.append('</tr></thead><tbody>')
    for h in sorted(hosts, key=lambda x: x["short"].lower()):
        state_cls = _host_state_class(h.get("connection"), h.get("in_maintenance"))
        state_lbl = _host_state_label(h.get("connection"), h.get("in_maintenance"))
        cores = h.get("cpu_cores")
        mem = h.get("memory_b")
        out.append('<tr>')
        out.append(f'<td class="host-name">{_esc(h["short"])}</td>')
        out.append(f'<td><span class="host-state {state_cls}">{_esc(state_lbl)}</span></td>')
        out.append(f'<td class="num-cell">{h.get("vm_count", 0)}</td>')
        out.append(f'<td class="num-cell">{cores if cores else "—"}</td>')
        out.append(f'<td class="num-cell">{_human_bytes(mem) if mem else "—"}</td>')
        out.append('</tr>')
    out.append('</tbody></table>')
    out.append('</div>')
    return "".join(out)


def _render_fault_domain_section(cluster_fd_info):
    """Build the body of the Fault Domains catalog section."""
    if not cluster_fd_info:
        return '<p class="empty">No cluster topology data available.</p>'

    parts = []
    total_clusters = 0
    for cluster_name in sorted(cluster_fd_info):
        info = cluster_fd_info[cluster_name]
        hosts = info.get("hosts") or []
        if not hosts:
            continue
        preferred_fd = info.get("preferred_fd")
        # Group hosts by fault domain (use a placeholder for hosts without FD)
        by_fd = defaultdict(list)
        for h in hosts:
            fd = h.get("fd") or "(no fault domain)"
            by_fd[fd].append(h)

        total_clusters += 1
        parts.append('<div class="fd-cluster-block">')
        parts.append(f'<div class="fd-cluster-name">{_esc(cluster_name)} '
                     f'· {len(hosts)} host(s) · {len(by_fd)} '
                     f'fault domain(s)</div>')

        # Warn if cluster looks stretched but preferred FD couldn't be resolved
        real_fds = [fd for fd in by_fd if fd != "(no fault domain)"]
        if len(real_fds) > 1 and not preferred_fd:
            parts.append(
                '<div class="fd-warn">Could not determine the preferred '
                'fault domain for this stretched cluster. Site-mismatch '
                'detection is limited to literal FD-name matches.</div>'
            )

        parts.append('<div class="fd-grid">')
        # Preferred first, then alphabetical
        ordered = sorted(by_fd, key=lambda x: (x != preferred_fd, x.lower()))
        for fd in ordered:
            parts.append(_render_fault_domain_card(
                fd, by_fd[fd], preferred_fd, None))
        parts.append('</div></div>')

    if total_clusters == 0:
        return '<p class="empty">No host topology data available.</p>'
    return "".join(parts)


# ---------------------------------------------------------------------
# Catalog rendering helpers (already defined above)
# ---------------------------------------------------------------------

def render_html(rows, vcenter_host, output_path, cluster_fd_info=None):
    cluster_fd_info = cluster_fd_info or {}
    by_cluster = defaultdict(list)
    for r in rows:
        by_cluster[r["cluster"]].append(r)

    # Summary
    crit_count = sum(1 for r in rows if any(i["severity"] == "critical" for i in r["_issues"]))
    warn_count = sum(1 for r in rows if any(i["severity"] == "warning" for i in r["_issues"]))
    info_count = sum(1 for r in rows if any(i["severity"] == "info" for i in r["_issues"]))
    clean_count = sum(1 for r in rows if not r["_issues"])

    # Main per-cluster tables
    table_parts = []
    for cluster in sorted(by_cluster):
        vms = sorted(by_cluster[cluster], key=lambda x: x["vm"].lower())
        table_parts.append('<section class="cluster-section">')
        table_parts.append(
            f'<h2>{_esc(cluster)} <span class="count">{len(vms)} VMs</span></h2>'
        )
        table_parts.append("<table><thead><tr>")
        for label, cls in [("VM", "col-vm"), ("Host", "col-host"),
                            ("Power", "col-power"), ("Issues", "col-issues"),
                            ("DRS rules", "col-rules"),
                            ("Storage policy", "col-policies"),
                            ("Tags", "col-tags")]:
            table_parts.append(f'<th class="{cls}">{label}</th>')
        table_parts.append("</tr></thead><tbody>")

        for r in vms:
            search = " ".join([
                r["vm"], r["host"], r["drs_rules"],
                r["storage_policies"], r["tags"], r["issues"]
            ]).lower()
            sev = _highest_severity(r["_issues"])
            tr_cls = ["vm-row"]
            if sev == "critical": tr_cls.append("has-critical")
            elif sev == "warning": tr_cls.append("has-warning")
            table_parts.append(
                f'<tr class="{" ".join(tr_cls)}" '
                f'data-search="{_esc(search)}" '
                f'data-cluster="{_esc(cluster)}" '
                f'data-severity="{sev}" '
                f'data-hasrule="{"1" if r["_rules"] else "0"}">'
            )
            table_parts.append(f'<td><strong>{_esc(r["vm"])}</strong></td>')
            table_parts.append(f'<td>{_esc(_short_host(r["host"]))}</td>')
            table_parts.append(
                f'<td class="{_power_class(r["power_state"])}">'
                f'{_esc(r["power_state"].replace("powered", ""))}</td>'
            )
            table_parts.append(f'<td>{_issues_html(r["_issues"])}</td>')
            table_parts.append(f'<td>{_rules_cell(r["_rules"], cluster)}</td>')
            table_parts.append(f'<td>{_policies_cell(r["_policies"])}</td>')
            table_parts.append(f'<td>{_tag_pills(r["_tags"])}</td>')
            table_parts.append("</tr>")
        table_parts.append("</tbody></table></section>")

    # Catalogs
    unique_policies = collect_unique_policies(rows)
    unique_rules = collect_unique_rules(rows)

    policy_details = "".join(
        _render_policy_card(name, unique_policies[name])
        for name in sorted(unique_policies, key=str.lower)
    )
    rule_details_parts = []
    by_cluster_rule = defaultdict(list)
    for (cl, name), entry in unique_rules.items():
        by_cluster_rule[cl].append((name, entry))
    for cl in sorted(by_cluster_rule):
        rule_details_parts.append(f'<h3 style="margin: 24px 0 10px; '
                                   f'color: var(--muted); font-size: 13px; '
                                   f'text-transform: uppercase; letter-spacing: 0.5px;">'
                                   f'{_esc(cl)}</h3>')
        for name, entry in sorted(by_cluster_rule[cl], key=lambda x: x[0].lower()):
            rule_details_parts.append(_render_rule_card(cl, name, entry))
    rule_details = "".join(rule_details_parts)

    cluster_opts = "".join(
        f'<option value="{_esc(c)}">{_esc(c)}</option>' for c in sorted(by_cluster)
    )
    meta = (f"vCenter: {_esc(vcenter_host)} &nbsp;·&nbsp; "
            f"{len(rows)} VMs &nbsp;·&nbsp; {len(by_cluster)} clusters")
    summary = (
        f'<button type="button" class="summary-card crit" data-filter="critical">'
        f'<span class="num">{crit_count}</span> critical</button>'
        f'<button type="button" class="summary-card warn" data-filter="warning-only">'
        f'<span class="num">{warn_count}</span> warnings</button>'
        f'<button type="button" class="summary-card info" data-filter="info-only">'
        f'<span class="num">{info_count}</span> info</button>'
        f'<button type="button" class="summary-card clean" data-filter="none">'
        f'<span class="num">{clean_count}</span> clean</button>'
    )
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Fault domain catalog
    fd_section = _render_fault_domain_section(cluster_fd_info)
    total_hosts = sum(len(info.get("hosts") or [])
                      for info in cluster_fd_info.values())
    total_fds = sum(len({h.get("fd") for h in (info.get("hosts") or [])
                        if h.get("fd")})
                    for info in cluster_fd_info.values())
    fd_count_label = (f"{total_fds} fault domain(s) across "
                       f"{total_hosts} host(s)" if total_fds
                       else f"{total_hosts} host(s)")

    html_out = (HTML_TEMPLATE
                .replace("__META__", meta)
                .replace("__SUMMARY__", summary)
                .replace("__CLUSTER_OPTS__", cluster_opts)
                .replace("__VM_TABLES__", "".join(table_parts))
                .replace("__POLICY_COUNT__", f"{len(unique_policies)} policies")
                .replace("__POLICY_DETAILS__", policy_details
                         or '<p class="empty">No policies found.</p>')
                .replace("__RULE_COUNT__", f"{len(unique_rules)} rules")
                .replace("__RULE_DETAILS__", rule_details
                         or '<p class="empty">No DRS rules found.</p>')
                .replace("__FD_COUNT__", fd_count_label)
                .replace("__FD_DETAILS__", fd_section)
                .replace("__TIMESTAMP__", timestamp))

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_out)


# =====================================================================
# Helpers / Main
# =====================================================================

def get_obj(content, vimtype, name=None):
    container = content.viewManager.CreateContainerView(content.rootFolder, vimtype, True)
    objs = list(container.view)
    container.Destroy()
    if name is None:
        return objs
    for o in objs:
        if o.name == name:
            return o
    return None


def main():
    p = argparse.ArgumentParser(description="vCenter VM Report (DRS / SPBM / Tags / Issues)")
    p.add_argument("-s", "--server", required=True, help="vCenter hostname/IP")
    p.add_argument("-u", "--user", required=True, help="Username")
    p.add_argument("-p", "--password", help="Password (prompted if omitted)")
    p.add_argument("-o", "--output", default=None,
                   help="Output filename base (.csv/.html appended). "
                        "Default: vm_report_<vcenter>_<timestamp>")
    p.add_argument("--cluster", help="Limit to a specific cluster")
    args = p.parse_args()

    pwd = args.password or safe_getpass(f"Password for {args.user}: ")

    # Build output paths
    if args.output is None:
        vc_short = re.sub(r"[^a-zA-Z0-9._-]", "_", args.server.split(".")[0])
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        base = f"vm_report_{vc_short}_{ts}"
    else:
        base = args.output
        if base.lower().endswith((".csv", ".html")):
            base = base.rsplit(".", 1)[0]
    csv_path, html_path = base + ".csv", base + ".html"

    print(f"{C_CYAN}[*]{C_RESET} Connecting to {C_BRIGHT}{args.server}{C_RESET} ...")
    try:
        si, _ = connect_vcenter(args.server, args.user, pwd)
    except Exception as e:
        print(f"{C_RED}[!] vCenter connection failed: {e}{C_RESET}", file=sys.stderr)
        sys.exit(1)
    content = si.RetrieveContent()
    about = content.about
    print(f"{C_GREEN}[OK]{C_RESET} Connected - {about.fullName} (build {about.build})")

    print(f"{C_CYAN}[*]{C_RESET} Connecting to SPBM ...")
    try:
        pbm_si = connect_pbm(si)
        print(f"{C_GREEN}[OK]{C_RESET} SPBM ready")
    except Exception as e:
        print(f"{C_YELLOW}[!] SPBM unavailable: {e}{C_RESET}", file=sys.stderr)
        pbm_si = None

    print(f"{C_CYAN}[*]{C_RESET} Connecting to tagging API ...")
    tagging = None
    if HAS_VAPI:
        try:
            tagging = connect_tagging(args.server, args.user, pwd)
            print(f"{C_GREEN}[OK]{C_RESET} Tagging API ready")
        except Exception as e:
            print(f"{C_YELLOW}[!] Tagging API failed: {e}{C_RESET}", file=sys.stderr)
    else:
        print(f"{C_YELLOW}[!] vsphere-automation-sdk not installed, "
              f"tags will be skipped.{C_RESET}", file=sys.stderr)

    clusters = get_obj(content, [vim.ClusterComputeResource])
    if args.cluster:
        clusters = [c for c in clusters if c.name == args.cluster]
        if not clusters:
            print(f"{C_RED}[!] Cluster '{args.cluster}' not found.{C_RESET}",
                  file=sys.stderr)
            sys.exit(1)

    total_vms = sum(
        1 for cluster in clusters for host in cluster.host for vm in host.vm
        if vm.config is not None and not vm.config.template
    )
    print(f"{C_CYAN}[*]{C_RESET} Total: {C_BRIGHT}{len(clusters)}{C_RESET} cluster(s), "
          f"{C_BRIGHT}{total_vms}{C_RESET} VM(s)\n")

    rows, counter, policy_cache = [], 0, {}
    cluster_fd_info = {}  # cluster_name -> {preferred_fd, hosts: [{name, fd, connection, ...}]}
    for cluster in clusters:
        print(f"{C_CYAN}[*]{C_RESET} Cluster: {C_BRIGHT}{cluster.name}{C_RESET}")
        vm_rule_idx = build_drs_rule_index(cluster)
        host_fd_map = get_host_fault_domains(cluster)
        preferred_fd = None
        if host_fd_map and len(set(host_fd_map.values())) > 1:
            preferred_fd = get_preferred_fault_domain(si, cluster)
        if host_fd_map:
            fds = sorted(set(host_fd_map.values()))
            fd_display = []
            for fd in fds:
                if fd == preferred_fd:
                    fd_display.append(f"{fd} {C_GREEN}(preferred){C_RESET}")
                else:
                    fd_display.append(fd)
            print(f"    {C_BLUE}Fault domains:{C_RESET} {', '.join(fd_display)}")
            if not preferred_fd and len(fds) > 1:
                print(f"    {C_YELLOW}[!]{C_RESET} Could not determine preferred "
                      f"fault domain - site mismatch detection will be limited "
                      f"to literal FD-name matches.")

        # Snapshot host info for the HTML report (only when cluster has FDs;
        # for non-stretched clusters we still collect basic host info because
        # the table is useful for visualizing cluster topology in general)
        host_entries = []
        for h in cluster.host:
            try:
                short = _short_host(h.name)
                rt = getattr(h, "runtime", None)
                summary = getattr(h, "summary", None)
                hardware = getattr(summary, "hardware", None) if summary else None
                quick = getattr(summary, "quickStats", None) if summary else None
                host_entries.append({
                    "name": h.name,
                    "short": short,
                    "fd": host_fd_map.get(short),
                    "connection": (getattr(rt, "connectionState", "") if rt else ""),
                    "in_maintenance": (getattr(rt, "inMaintenanceMode", False)
                                        if rt else False),
                    "power_state": (getattr(rt, "powerState", "") if rt else ""),
                    "vm_count": len([v for v in h.vm
                                     if v.config is not None and not v.config.template]),
                    "cpu_cores": (getattr(hardware, "numCpuCores", None)
                                   if hardware else None),
                    "memory_b": (getattr(hardware, "memorySize", None)
                                  if hardware else None),
                    "cpu_usage_mhz": (getattr(quick, "overallCpuUsage", None)
                                       if quick else None),
                    "mem_usage_mb": (getattr(quick, "overallMemoryUsage", None)
                                      if quick else None),
                })
            except Exception:
                continue
        cluster_fd_info[cluster.name] = {
            "preferred_fd": preferred_fd,
            "hosts": host_entries,
        }

        for host in cluster.host:
            for vm in host.vm:
                if vm.config is None or vm.config.template: continue
                counter += 1
                print(f"    [{counter}/{total_vms}] {vm.name} ...",
                      end="", flush=True)

                rules = vm_rule_idx.get(vm._moId, [])
                policies = get_vm_storage_policies(pbm_si, vm, policy_cache)
                tags = get_vm_tags(tagging, vm._moId)
                issues = detect_issues(rules, policies, host_fd_map,
                                        _short_host(host.name), preferred_fd)

                rows.append({
                    "cluster": cluster.name, "host": host.name, "vm": vm.name,
                    "power_state": vm.runtime.powerState,
                    "drs_rules": format_rules_csv(rules),
                    "storage_policies": format_policies_csv(policies),
                    "tags": " ;; ".join(tags),
                    "issues": format_issues_csv(issues),
                    "_rules": rules, "_policies": policies,
                    "_tags": tags, "_issues": issues,
                })
                crit = sum(1 for i in issues if i["severity"] == "critical")
                warn = sum(1 for i in issues if i["severity"] == "warning")
                marker = ""
                if crit:
                    marker = f" {C_RED}[!]{C_RESET} {C_RED}{crit}c{C_RESET}/{C_YELLOW}{warn}w{C_RESET}"
                elif warn:
                    marker = f" {C_YELLOW}[!]{C_RESET} {C_YELLOW}{warn}w{C_RESET}"
                print(f" rules={len(rules)}, policies={len(policies)}, "
                      f"tags={len(tags)}, issues={len(issues)}{marker}")

    # Resolve which datastores are compatible with each policy
    # (mirror of vCenter "Storage Compatibility" tab)
    if pbm_si and policy_cache:
        ds_lookup = build_datastore_lookup(content)
        pod_lookup = build_storage_pod_lookup(content)
        resolve_compatible_datastores(pbm_si, policy_cache, ds_lookup, pod_lookup)

    Disconnect(si)

    print(f"\n{C_CYAN}[*]{C_RESET} Writing CSV: {C_BRIGHT}{csv_path}{C_RESET}")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "cluster", "host", "vm", "power_state",
            "drs_rules", "storage_policies", "tags", "issues"
        ], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print(f"{C_CYAN}[*]{C_RESET} Writing HTML: {C_BRIGHT}{html_path}{C_RESET}")
    render_html(rows, args.server, html_path, cluster_fd_info)

    crit = sum(1 for r in rows if any(i["severity"] == "critical" for i in r["_issues"]))
    warn = sum(1 for r in rows if any(i["severity"] == "warning" for i in r["_issues"]))

    crit_str = f"{C_RED}{crit}{C_RESET}" if crit else f"{C_GREEN}{crit}{C_RESET}"
    warn_str = f"{C_YELLOW}{warn}{C_RESET}" if warn else f"{C_GREEN}{warn}{C_RESET}"
    print(f"\n{C_GREEN}[+] Done.{C_RESET} "
          f"Critical: {crit_str}, Warnings: {warn_str}.")


if __name__ == "__main__":
    main()
