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
    fd_map = {}
    cfg = cluster.configurationEx
    for hc in (getattr(cfg, "vsanHostConfig", None) or []):
        host = getattr(hc, "hostSystem", None)
        fd_info = getattr(hc, "faultDomainInfo", None)
        if host and fd_info and getattr(fd_info, "name", None):
            fd_map[_short_host(host.name)] = fd_info.name
    return fd_map


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


def _is_locality_capability(cap_name):
    n = (cap_name or "").lower().replace("_", "").replace(" ", "")
    return any(h in n for h in LOCALITY_HINTS)


def _locality_matches_fd(locality_value, fd_name):
    lv = (locality_value or "").lower().strip()
    fn = (fd_name or "").lower().strip()
    if not lv or not fn: return False
    if lv == fn or lv in fn or fn in lv: return True
    if "preferred" in lv and "preferred" in fn: return True
    if "secondary" in lv and "secondary" in fn: return True
    return False


def detect_issues(rules, policies, host_fd_map, vm_host_short):
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
                    if val.strip().lower() in ("none", "", "rfc-2606 hosts"): continue
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
                if not any(_locality_matches_fd(locality_val, fd) for fd in pinned_fds):
                    severity = "critical" if is_must else "warning"
                    verb = "must" if is_must else "should"
                    direction = "run on" if is_affine else "NOT run on"
                    issues.append({"severity": severity, "message": (
                        f"Site mismatch: DRS rule '{r['name']}' says VM {verb} "
                        f"{direction} hosts in {sorted(host_fds_in_rule)}, "
                        f"effectively pinning compute to {sorted(pinned_fds)}; "
                        f"but storage policy on {disk_label} requires data on "
                        f"'{locality_val}' ({cap_name}). Cross-site I/O for every R/W.")})

    if len(cluster_fds) > 1 and vm_host_short:
        host_fd = host_fd_map.get(vm_host_short)
        if host_fd:
            for label, info in policies.items():
                if not isinstance(info, dict): continue
                for cap in info.get("capabilities", []) or []:
                    if not (_is_locality_capability(cap.get("capability")) or
                            _is_locality_capability(cap.get("property"))): continue
                    val = str(cap.get("value", ""))
                    if val.strip().lower() in ("none", "", "rfc-2606 hosts"): continue
                    if not _locality_matches_fd(val, host_fd):
                        issues.append({"severity": "warning", "message": (
                            f"Runtime site mismatch: VM is currently on host "
                            f"'{vm_host_short}' (fault domain '{host_fd}'), "
                            f"but storage policy on {label} keeps data on '{val}'.")})

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
  }
  .summary-card .num { font-weight: 700; font-size: 16px; }
  .summary-card.crit .num { color: var(--crit-fg); }
  .summary-card.warn .num { color: var(--warn-fg); }
  .summary-card.info .num { color: var(--info-fg); }

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
  <a href="#rules">DRS Rules</a>
</nav>
<div class="controls">
  <input type="text" id="filter" placeholder="Filter (VM, host, rule, policy, tag, issue)…">
  <select id="clusterFilter"><option value="">All clusters</option>__CLUSTER_OPTS__</select>
  <select id="severityFilter">
    <option value="">Issues: all</option>
    <option value="any">Any issue</option>
    <option value="critical">Critical only</option>
    <option value="warning">Critical + Warning</option>
    <option value="none">No issues</option>
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
      if (sv === 'warning' && !(sev === 'critical' || sev === 'warning')) show = false;
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
  }
  [filter, clusterSel, sevSel, ruleSel].forEach(el => el.addEventListener('input', apply));

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

    out.append('</div></div>')
    return "".join(out)


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


# =====================================================================
# Main HTML render
# =====================================================================

def render_html(rows, vcenter_host, output_path):
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
        f'<div class="summary-card crit"><span class="num">{crit_count}</span> critical</div>'
        f'<div class="summary-card warn"><span class="num">{warn_count}</span> warnings</div>'
        f'<div class="summary-card info"><span class="num">{info_count}</span> info</div>'
        f'<div class="summary-card"><span class="num">{clean_count}</span> clean</div>'
    )
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
    for cluster in clusters:
        print(f"{C_CYAN}[*]{C_RESET} Cluster: {C_BRIGHT}{cluster.name}{C_RESET}")
        vm_rule_idx = build_drs_rule_index(cluster)
        host_fd_map = get_host_fault_domains(cluster)
        if host_fd_map:
            print(f"    {C_BLUE}Fault domains:{C_RESET} "
                  f"{', '.join(sorted(set(host_fd_map.values())))}")

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
                                        _short_host(host.name))

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
    render_html(rows, args.server, html_path)

    crit = sum(1 for r in rows if any(i["severity"] == "critical" for i in r["_issues"]))
    warn = sum(1 for r in rows if any(i["severity"] == "warning" for i in r["_issues"]))

    crit_str = f"{C_RED}{crit}{C_RESET}" if crit else f"{C_GREEN}{crit}{C_RESET}"
    warn_str = f"{C_YELLOW}{warn}{C_RESET}" if warn else f"{C_GREEN}{warn}{C_RESET}"
    print(f"\n{C_GREEN}[+] Done.{C_RESET} "
          f"Critical: {crit_str}, Warnings: {warn_str}.")


if __name__ == "__main__":
    main()
