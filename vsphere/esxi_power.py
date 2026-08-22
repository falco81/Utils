#!/usr/bin/env python3
"""
esxi_power.py - Bulk power management of VMs on a standalone ESXi 8.x host.

Usage:
    python esxi_power.py --scan     Detect powered-on VMs, their IPs and ICMP
                                    reachability, save everything to config
    python esxi_power.py --status   Show power state of all VMs + live ICMP test
    python esxi_power.py --off      Gracefully shut down ALL running VMs (via VMware
                                    Tools) and verify with ICMP that they went down
    python esxi_power.py --on       Power on VMs from last --scan and verify
                                    boot (Tools) + ICMP ping

Requirements (Windows 10 CLI friendly):
    pip install pyvmomi colorama

Config file:
    Same directory and same base name as this script, with .json extension
    (e.g. esxi_power.json). If it does not exist, you will be asked for the
    connection parameters interactively and the file will be created.

ICMP logic:
    --scan pings every powered-on VM and stores an "icmp" flag per VM:
        true  = the VM normally answers ping -> ping checks are used on --on/--off
        false = the VM does NOT answer ping even when running (firewall etc.)
                -> ping checks are SKIPPED for this VM on --on/--off
    VMs with unknown ICMP state (never scanned / no IP) are also skipped.

Progress display:
    Every VM has its own line with a single multi-stage progress bar.
    On --on the stages are:  ON | Tools | ICMP   (segments of one bar)
    On --off the stages are: Shutdown | ICMP-down
    Completed stages are green, the running stage fills up towards its
    timeout, a failed stage turns red, a warned stage yellow.

Notes:
    --off performs a GRACEFUL guest shutdown only (requires VMware Tools).
    VMs without running Tools are never powered off hard - they are reported
    and left untouched.
"""

import argparse
import atexit
import base64
import getpass
import json
import os
import re
import ssl
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

try:
    from colorama import Fore, Style, init as colorama_init
except ImportError:
    print("Missing dependency 'colorama'. Install it with: pip install colorama")
    sys.exit(1)

try:
    from pyVim.connect import SmartConnect, Disconnect
    from pyVmomi import vim
except ImportError:
    print("Missing dependency 'pyvmomi'. Install it with: pip install pyvmomi")
    sys.exit(1)

colorama_init(autoreset=True)  # enables ANSI colors + cursor codes on Windows 10

# Config file path: same folder + same name as the script, with .json extension
SCRIPT_PATH = os.path.abspath(sys.argv[0])
CONFIG_PATH = os.path.splitext(SCRIPT_PATH)[0] + ".json"

SHUTDOWN_TIMEOUT = 300    # seconds to wait for graceful guest shutdown
POWERON_TIMEOUT = 120     # seconds to wait for VMs to reach 'poweredOn'
BOOT_TIMEOUT = 300        # seconds to wait for VMware Tools after power on
PING_UP_TIMEOUT = 300     # seconds to wait for guests to START answering ping (--on)
PING_DOWN_TIMEOUT = 60    # seconds to wait for guests to STOP answering ping (--off)
POLL_INTERVAL = 2         # seconds between power/tools state checks
SCAN_PING_TRIES = 2       # ping attempts per VM during --scan

BAR_WIDTH = 30
SPINNER = "|/-\\"

RESET = Style.RESET_ALL


# ---------------------------------------------------------------------------
# Helpers for colored output
# ---------------------------------------------------------------------------

def info(msg):
    print(Fore.CYAN + "[*] " + RESET + msg)


def ok(msg):
    print(Fore.GREEN + "[+] " + RESET + msg)


def warn(msg):
    print(Fore.YELLOW + "[!] " + RESET + msg)


def error(msg):
    print(Fore.RED + "[-] " + RESET + msg)


def len_visible(s):
    """Length of a string without ANSI escape sequences."""
    return len(re.sub(r"\x1b\[[0-9;]*m", "", s))


# ---------------------------------------------------------------------------
# ICMP ping (Windows / Linux)
# ---------------------------------------------------------------------------

def ping(ip):
    """Send a single ICMP echo request. Returns True when the host replied."""
    if os.name == "nt":
        cmd = ["ping", "-n", "1", "-w", "1000", ip]
    else:
        cmd = ["ping", "-c", "1", "-W", "1", ip]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=5)
        if os.name == "nt":
            # On Windows, returncode 0 can also mean "Destination host
            # unreachable", so require a TTL= in the reply as well.
            out = res.stdout.decode(errors="ignore").lower()
            return res.returncode == 0 and "ttl=" in out
        return res.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Multi-stage, per-VM progress display
# ---------------------------------------------------------------------------

class StageBars:
    """Live multi-line display: one line per VM, one bar per VM. The bar is
    split into segments - one segment per stage (e.g. ON | Tools | ICMP).
    The VM progresses through the stages inside its single bar:
      - finished stages are solid green (yellow when they ended with a warning)
      - the running stage fills up as elapsed time approaches its timeout
        (or bounces when the stage has no timeout)
      - on failure the current stage turns solid red
    Lines are redrawn in place using ANSI cursor movement (translated by
    colorama on Windows).

    Final states: ok | warn | fail | skip  ('pending' while still running)
    """

    def __init__(self, stages_by_name):
        """stages_by_name: {vm_name: [(stage_label, timeout_or_None), ...]}"""
        self.names = list(stages_by_name.keys())
        self.stages = {n: list(v) for n, v in stages_by_name.items()}
        self.idx = {n: 0 for n in self.names}
        self.state = {n: "pending" for n in self.names}
        self.msg = {n: "" for n in self.names}
        self.warned = {n: set() for n in self.names}   # stage indices that warned
        now = time.time()
        self.start = now
        self.stage_start = {n: now for n in self.names}
        self.done_at = {}
        self.tick = 0
        self._drawn = False
        self.name_width = min(max((len(n) for n in self.names), default=10), 32)

    # ---- state control -------------------------------------------------

    def pending(self):
        return [n for n in self.names if self.state[n] == "pending"]

    def current_stage(self, name):
        i = self.idx[name]
        st = self.stages[name]
        return st[i][0] if i < len(st) else None

    def stage_timed_out(self, name):
        i = self.idx[name]
        timeout = self.stages[name][i][1]
        if timeout is None:
            return False
        return (time.time() - self.stage_start[name]) > timeout

    def advance(self, name, msg="", warned=False):
        """Complete the current stage and move to the next one."""
        if warned:
            self.warned[name].add(self.idx[name])
        self.idx[name] += 1
        self.stage_start[name] = time.time()
        self.msg[name] = msg

    def complete(self, name, state, msg=""):
        """Set the final state of a VM (ok / warn / fail / skip)."""
        if self.state.get(name) == "pending":
            self.done_at[name] = time.time()
        if state == "ok":
            self.idx[name] = len(self.stages[name])
        self.state[name] = state
        self.msg[name] = msg

    def set_msg(self, name, msg):
        self.msg[name] = msg

    # ---- rendering -----------------------------------------------------

    def _bar(self, name):
        stages = self.stages[name]
        n = max(len(stages), 1)
        widths = [BAR_WIDTH // n] * n
        for i in range(BAR_WIDTH % n):
            widths[i] += 1

        st = self.state[name]
        idx = self.idx[name]
        parts = []
        for i, (label, timeout) in enumerate(stages):
            w = widths[i]
            if st == "skip":
                parts.append(Style.DIM + "-" * w + RESET)
            elif i < idx or st == "ok":
                color = Fore.YELLOW if i in self.warned[name] else Fore.GREEN
                parts.append(color + "#" * w + RESET)
            elif i == idx and st == "fail":
                parts.append(Fore.RED + "#" * w + RESET)
            elif i == idx and st == "warn":
                parts.append(Fore.YELLOW + "#" * w + RESET)
            elif i == idx:  # running
                if timeout:
                    frac = min((time.time() - self.stage_start[name]) / timeout, 1.0)
                    f = int(w * frac)
                    parts.append(Fore.CYAN + "#" * f + RESET
                                 + Style.DIM + "-" * (w - f) + RESET)
                else:
                    # indeterminate: single moving marker inside the segment
                    pos = self.tick % max(w, 1)
                    parts.append(Style.DIM + "-" * pos + RESET
                                 + Fore.CYAN + "#" + RESET
                                 + Style.DIM + "-" * max(w - pos - 1, 0) + RESET)
            else:  # not reached yet
                parts.append(Style.DIM + "-" * w + RESET)
        sep = Style.DIM + "|" + RESET
        return sep.join(parts)

    def render(self):
        self.tick += 1
        if self._drawn:
            sys.stdout.write("\x1b[{}A".format(len(self.names)))
        for n in self.names:
            st = self.state[n]
            elapsed = int((self.done_at.get(n) or time.time()) - self.start)
            t = "{:02d}:{:02d}".format(elapsed // 60, elapsed % 60)
            if st == "pending":
                lead = Fore.CYAN + SPINNER[self.tick % len(SPINNER)] + RESET
                stage = self.current_stage(n)
                prefix = (Style.BRIGHT + "{}: ".format(stage) + RESET) if stage else ""
                text = prefix + Style.DIM + (self.msg[n] or "waiting ...") + RESET
            elif st == "ok":
                lead = Fore.GREEN + "+" + RESET
                text = Fore.GREEN + (self.msg[n] or "OK") + RESET
            elif st == "warn":
                lead = Fore.YELLOW + "!" + RESET
                text = Fore.YELLOW + (self.msg[n] or "warning") + RESET
            elif st == "fail":
                lead = Fore.RED + "x" + RESET
                stage = self.current_stage(n)
                prefix = (Fore.RED + "{}: ".format(stage) + RESET) if stage else ""
                text = prefix + Fore.RED + (self.msg[n] or "FAILED") + RESET
            else:  # skip
                lead = Style.DIM + "." + RESET
                text = Style.DIM + (self.msg[n] or "skipped") + RESET

            disp = n if len(n) <= self.name_width else n[:self.name_width - 3] + "..."
            line = " {} {:<{nw}} [{}] {}  {}".format(
                lead, disp, self._bar(n), t, text, nw=self.name_width)
            sys.stdout.write("\r" + line + "\x1b[K\n")
        sys.stdout.flush()
        self._drawn = True

    def finish(self):
        """Final redraw so completed states are shown."""
        self.render()


# ---------------------------------------------------------------------------
# Config handling
# ---------------------------------------------------------------------------

def encode_password(pwd):
    """Trivial obfuscation only - NOT encryption. Keeps the password from
    being read at a glance; anyone with the file can decode it."""
    return base64.b64encode(pwd.encode("utf-8")).decode("ascii")


def decode_password(enc):
    return base64.b64decode(enc.encode("ascii")).decode("utf-8")


def load_config():
    if not os.path.isfile(CONFIG_PATH):
        return None
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        error("Failed to read config file '{}': {}".format(CONFIG_PATH, e))
        sys.exit(1)


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4)
        ok("Config saved to: {}".format(CONFIG_PATH))
    except OSError as e:
        error("Failed to write config file: {}".format(e))
        sys.exit(1)


def create_config_interactively():
    info("Config file not found: {}".format(CONFIG_PATH))
    info("Please enter the ESXi connection parameters.")

    host = ""
    while not host:
        host = input("  ESXi host (IP or FQDN): ").strip()

    port_raw = input("  Port [443]: ").strip()
    port = int(port_raw) if port_raw.isdigit() else 443

    user = ""
    while not user:
        user = input("  Username [root]: ").strip() or "root"

    pwd = ""
    while not pwd:
        pwd = getpass.getpass("  Password: ")

    verify_raw = input("  Verify SSL certificate? (y/N): ").strip().lower()
    verify_ssl = verify_raw == "y"

    cfg = {
        "host": host,
        "port": port,
        "user": user,
        "password_b64": encode_password(pwd),
        "verify_ssl": verify_ssl,
        "powered_on_vms": [],
        "last_scan": None,
    }
    warn("Note: the password is stored base64-encoded (obfuscated, NOT encrypted).")
    save_config(cfg)
    return cfg


def get_config():
    cfg = load_config()
    if cfg is None:
        cfg = create_config_interactively()
    return cfg


def normalize_saved_vms(cfg):
    """Return the saved VM list as [{'name', 'ip', 'icmp'}, ...].
    'icmp': True = normally answers ping, False = normally does NOT answer,
    None = unknown. Also accepts older config formats."""
    result = []
    for entry in cfg.get("powered_on_vms") or []:
        if isinstance(entry, str):
            result.append({"name": entry, "ip": None, "icmp": None})
        elif isinstance(entry, dict) and entry.get("name"):
            result.append({
                "name": entry["name"],
                "ip": entry.get("ip") or None,
                "icmp": entry.get("icmp", None),
            })
    return result


# ---------------------------------------------------------------------------
# ESXi connection and VM enumeration
# ---------------------------------------------------------------------------

def connect_esxi(cfg):
    if cfg.get("verify_ssl", False):
        ssl_ctx = ssl.create_default_context()
    else:
        ssl_ctx = ssl._create_unverified_context()

    info("Connecting to {}:{} as {} ...".format(cfg["host"], cfg["port"], cfg["user"]))
    try:
        si = SmartConnect(
            host=cfg["host"],
            user=cfg["user"],
            pwd=decode_password(cfg["password_b64"]),
            port=cfg["port"],
            sslContext=ssl_ctx,
        )
    except vim.fault.InvalidLogin:
        error("Invalid credentials.")
        sys.exit(1)
    except Exception as e:
        error("Connection failed: {}".format(e))
        sys.exit(1)

    atexit.register(Disconnect, si)
    ok("Connected.")
    return si


def get_all_vms(si):
    content = si.RetrieveContent()
    view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    vms = list(view.view)
    view.Destroy()
    return vms


def is_powered_on(vm):
    return vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn


def tools_running(vm):
    return vm.guest is not None and vm.guest.toolsRunningStatus == "guestToolsRunning"


def guest_ip(vm):
    """Primary guest IPv4/IPv6 address as reported by VMware Tools, or None."""
    if vm.guest is not None and vm.guest.ipAddress:
        return vm.guest.ipAddress
    return None


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def action_scan(si, cfg):
    info("Scanning VMs ...")
    vms = get_all_vms(si)
    running = sorted((v for v in vms if is_powered_on(v)), key=lambda v: v.name.lower())

    entries = []
    for vm in running:
        entries.append({"name": vm.name, "ip": guest_ip(vm), "icmp": None})

    # ICMP baseline test - one bar per VM. The result decides whether ping
    # checks are used for the VM during --on / --off.
    to_ping = [e for e in entries if e["ip"]]
    if to_ping:
        info("ICMP baseline test:")
        bars = StageBars({e["name"]: [("ICMP", None)] for e in to_ping})
        for e in to_ping:
            bars.set_msg(e["name"], "queued ({})".format(e["ip"]))
        bars.render()
        for e in to_ping:
            bars.set_msg(e["name"], "pinging {} ...".format(e["ip"]))
            bars.render()
            alive = False
            for _ in range(SCAN_PING_TRIES):
                if ping(e["ip"]):
                    alive = True
                    break
            e["icmp"] = alive
            if alive:
                bars.complete(e["name"], "ok", "ICMP OK ({})".format(e["ip"]))
            else:
                bars.complete(e["name"], "warn", "ICMP blocked ({})".format(e["ip"]))
            bars.render()
        bars.finish()

    cfg["powered_on_vms"] = entries
    cfg["last_scan"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_config(cfg)

    print()
    ok("Scan complete. {} VM(s) total, {} powered on.".format(len(vms), len(entries)))
    for e in entries:
        if e["ip"] is None:
            ip_str = "-"
            icmp_str = Fore.YELLOW + "ICMP: unknown (no IP / Tools?)" + RESET
        elif e["icmp"]:
            ip_str = e["ip"]
            icmp_str = Fore.GREEN + "ICMP: OK" + RESET
        else:
            ip_str = e["ip"]
            icmp_str = Fore.YELLOW + "ICMP: blocked" + RESET
        print("    " + Fore.GREEN + "ON " + RESET
              + "{:<40} {:<18} {}".format(e["name"], ip_str, icmp_str))
    if any(e["icmp"] is not True for e in entries):
        warn("VMs marked 'ICMP: blocked/unknown' will have ping checks SKIPPED "
             "during --on and --off.")


def action_status(si, cfg):
    vms = get_all_vms(si)
    if not vms:
        warn("No virtual machines found on this host.")
        return

    saved = {e["name"]: e for e in normalize_saved_vms(cfg)}

    def _state_rank(vm):
        s = vm.runtime.powerState
        if s == vim.VirtualMachinePowerState.poweredOn:
            return 0
        if s == vim.VirtualMachinePowerState.suspended:
            return 1
        return 2  # poweredOff

    # primary sort: state (ON, SUSPENDED, OFF), secondary: VM name
    vms_sorted = sorted(vms, key=lambda v: (_state_rank(v), v.name.lower()))

    # Live ICMP test of all powered-on VMs with a known IP - one bar per VM,
    # pinged in parallel.
    ping_results = {}   # vm.name -> True/False
    targets = [(vm.name, guest_ip(vm)) for vm in vms_sorted
               if is_powered_on(vm) and guest_ip(vm)]
    if targets:
        info("Live ICMP test:")
        bars = StageBars({name: [("ICMP", None)] for name, _ in targets})
        for name, ip in targets:
            bars.set_msg(name, "pinging {} ...".format(ip))
        bars.render()

        def _ping_one(item):
            name, ip = item
            return name, ip, ping(ip)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(_ping_one, t) for t in targets]
            while True:
                all_done = True
                for f in futures:
                    if f.done():
                        name, ip, res = f.result()
                        if name not in ping_results:
                            ping_results[name] = res
                            if res:
                                bars.complete(name, "ok", "reply from {}".format(ip))
                            else:
                                e = saved.get(name)
                                if e and e["icmp"] is False:
                                    bars.complete(name, "warn", "no reply (expected)")
                                else:
                                    bars.complete(name, "fail", "no reply")
                    else:
                        all_done = False
                bars.render()
                if all_done:
                    break
                time.sleep(0.3)
        bars.finish()
        print()

    print(Style.BRIGHT + "{:<40} {:<12} {:<18} {:<14} {}".format(
        "VM NAME", "STATE", "IP ADDRESS", "VMWARE TOOLS", "PING"))
    print("-" * 100)
    for vm in vms_sorted:
        state = vm.runtime.powerState
        if state == vim.VirtualMachinePowerState.poweredOn:
            state_str = Fore.GREEN + "ON" + RESET
        elif state == vim.VirtualMachinePowerState.poweredOff:
            state_str = Fore.RED + "OFF" + RESET
        else:
            state_str = Fore.YELLOW + "SUSPENDED" + RESET

        ip_str = guest_ip(vm) or "-"
        tools_str = "running" if tools_running(vm) else "not running"

        if vm.name in ping_results:
            if ping_results[vm.name]:
                ping_str = Fore.GREEN + "OK" + RESET
            else:
                e = saved.get(vm.name)
                if e and e["icmp"] is False:
                    ping_str = Fore.YELLOW + "no reply (expected)" + RESET
                else:
                    ping_str = Fore.RED + "no reply" + RESET
        else:
            ping_str = "-"

        # width fix: colored strings contain invisible ANSI codes
        pad = 12 + (len(state_str) - len_visible(state_str))
        print("{:<40} {:<{pad}} {:<18} {:<14} {}".format(
            vm.name, state_str, ip_str, tools_str, ping_str, pad=pad))

    on_count = sum(1 for vm in vms_sorted if is_powered_on(vm))
    print("-" * 100)
    info("{} of {} VM(s) powered on. Last scan: {}".format(
        on_count, len(vms), cfg.get("last_scan") or "never"))


def action_off(si, cfg):
    vms = get_all_vms(si)
    running = [vm for vm in vms if is_powered_on(vm)]

    if not running:
        ok("No VMs are powered on. Nothing to do.")
        return

    saved = {e["name"]: e for e in normalize_saved_vms(cfg)}

    no_tools = [vm for vm in running if not tools_running(vm)]
    shut_list = [vm for vm in running if tools_running(vm)]

    warn("The following {} VM(s) will be shut down gracefully (via VMware Tools):".format(
        len(shut_list)))
    for vm in shut_list:
        print("    " + Fore.GREEN + vm.name + RESET)
    if no_tools:
        error("These VM(s) have no running VMware Tools and will be SKIPPED "
              "(no hard power off is ever performed):")
        for vm in no_tools:
            print("    " + Fore.RED + vm.name + RESET)

    if not shut_list:
        error("Nothing can be shut down gracefully. Aborting.")
        return

    confirm = input("Proceed? (y/N): ").strip().lower()
    if confirm != "y":
        info("Aborted by user.")
        return

    # IP + ICMP expectation for the ping-down stage (from the last --scan).
    # VMs with icmp != True get no ICMP-down stage in their bar.
    ping_ip = {}   # vm.name -> ip
    for vm in shut_list:
        e = saved.get(vm.name)
        if e and e["icmp"] is True:
            ip = guest_ip(vm) or e["ip"]
            if ip:
                ping_ip[vm.name] = ip

    # Send graceful shutdown requests
    active = []
    for vm in shut_list:
        try:
            vm.ShutdownGuest()
            active.append(vm)
        except Exception as e:
            error("Failed to request shutdown of '{}': {}".format(vm.name, e))

    if not active:
        error("No shutdown requests were accepted.")
        return

    # One bar per VM with stages: Shutdown [| ICMP-down]
    info("Shutting down (graceful via VMware Tools; stages: Shutdown | ICMP-down):")
    stages = {}
    for vm in active:
        s = [("Shutdown", SHUTDOWN_TIMEOUT)]
        if vm.name in ping_ip:
            s.append(("ICMP-down", PING_DOWN_TIMEOUT))
        stages[vm.name] = s
    bars = StageBars(stages)
    for vm in active:
        bars.set_msg(vm.name, "guest shutting down ...")
    bars.render()

    while bars.pending():
        for vm in active:
            if bars.state[vm.name] != "pending":
                continue
            stage = bars.current_stage(vm.name)

            if stage == "Shutdown":
                if not is_powered_on(vm):
                    if vm.name in ping_ip:
                        bars.advance(vm.name, "waiting for {} to stop replying ...".format(
                            ping_ip[vm.name]))
                    else:
                        bars.complete(vm.name, "ok",
                                      "powered off (ICMP check skipped)")
                elif bars.stage_timed_out(vm.name):
                    bars.complete(vm.name, "fail",
                                  "timeout - still running (no hard off)")

            elif stage == "ICMP-down":
                ip = ping_ip[vm.name]
                if not ping(ip):
                    bars.complete(vm.name, "ok",
                                  "powered off, {} confirmed silent".format(ip))
                elif bars.stage_timed_out(vm.name):
                    bars.complete(vm.name, "fail",
                                  "off but {} STILL replies - IP conflict/ARP?".format(ip))
        bars.render()
        if bars.pending():
            time.sleep(POLL_INTERVAL)
    bars.finish()

    for vm in no_tools:
        warn("'{}' was skipped (VMware Tools not running).".format(vm.name))

    n_ok = sum(1 for n in bars.names if bars.state[n] == "ok")
    n_fail = sum(1 for n in bars.names if bars.state[n] == "fail")
    print()
    if n_fail or no_tools:
        warn("Shutdown finished with warnings: {} ok, {} failed, {} skipped (no Tools).".format(
            n_ok, n_fail, len(no_tools)))
    else:
        ok("All {} VM(s) shut down gracefully and verified.".format(n_ok))


def action_on(si, cfg):
    saved = normalize_saved_vms(cfg)
    if not saved:
        warn("No VM list found in config. Run --scan first (while VMs are running).")
        return

    saved_by_name = {e["name"]: e for e in saved}
    info("VMs recorded by last scan ({}): {}".format(
        cfg.get("last_scan") or "unknown time",
        ", ".join(e["name"] for e in saved)))

    vms = get_all_vms(si)
    vms_by_name = {vm.name: vm for vm in vms}

    targets = []        # VMs to bring up (to start now or already running)
    for entry in saved:
        vm = vms_by_name.get(entry["name"])
        if vm is None:
            error("VM '{}' from config was not found on the host - skipping.".format(
                entry["name"]))
        else:
            targets.append(vm)

    if not targets:
        ok("Nothing to power on.")
        return

    # Send power-on tasks for VMs that are off
    for vm in targets:
        if not is_powered_on(vm):
            try:
                vm.PowerOnVM_Task()
            except Exception as e:
                error("Failed to power on '{}': {}".format(vm.name, e))

    # One bar per VM with stages: ON | Tools [| ICMP]
    # The ICMP stage is only present for VMs that normally answer ping
    # (icmp == True from the last --scan).
    info("Powering on (stages: ON | Tools | ICMP):")
    stages = {}
    do_ping = {}
    for vm in targets:
        e = saved_by_name.get(vm.name)
        do_ping[vm.name] = bool(e and e["icmp"] is True)
        s = [("ON", POWERON_TIMEOUT), ("Tools", BOOT_TIMEOUT)]
        if do_ping[vm.name]:
            s.append(("ICMP", PING_UP_TIMEOUT))
        stages[vm.name] = s
    bars = StageBars(stages)
    ping_ip = {}   # resolved when the VM reaches the ICMP stage

    for vm in targets:
        bars.set_msg(vm.name, "powering on ...")
    bars.render()

    def _enter_icmp_stage(vm, warned=False):
        """Move a VM from the Tools stage onward (to ICMP or final state)."""
        e = saved_by_name.get(vm.name)
        note = " (no Tools report)" if warned else ""
        if not do_ping[vm.name]:
            state = "warn" if warned else "ok"
            bars.complete(vm.name, state, "up{} - ICMP check skipped".format(note))
            return
        ip = guest_ip(vm) or (e["ip"] if e else None)
        if not ip:
            bars.complete(vm.name, "warn", "up{} - no IP, ping skipped".format(note))
            return
        ping_ip[vm.name] = ip
        bars.advance(vm.name, "waiting for {} to reply ...".format(ip), warned=warned)

    while bars.pending():
        for vm in targets:
            if bars.state[vm.name] != "pending":
                continue
            stage = bars.current_stage(vm.name)

            if stage == "ON":
                if is_powered_on(vm):
                    bars.advance(vm.name, "guest booting, waiting for Tools ...")
                elif bars.stage_timed_out(vm.name):
                    bars.complete(vm.name, "fail", "did not power on in time")

            elif stage == "Tools":
                if tools_running(vm):
                    _enter_icmp_stage(vm, warned=False)
                elif bars.stage_timed_out(vm.name):
                    # No Tools heartbeat - still try ICMP if configured
                    _enter_icmp_stage(vm, warned=True)

            elif stage == "ICMP":
                ip = ping_ip[vm.name]
                warned = len(bars.warned[vm.name]) > 0
                note = " (no Tools report)" if warned else ""
                if ping(ip):
                    state = "warn" if warned else "ok"
                    bars.complete(vm.name, state, "up, ping {} OK{}".format(ip, note))
                elif bars.stage_timed_out(vm.name):
                    bars.complete(vm.name, "fail", "no reply from {}{}".format(ip, note))
        bars.render()
        if bars.pending():
            time.sleep(POLL_INTERVAL)
    bars.finish()

    n_ok = sum(1 for n in bars.names if bars.state[n] == "ok")
    n_warn = sum(1 for n in bars.names if bars.state[n] == "warn")
    n_fail = sum(1 for n in bars.names if bars.state[n] == "fail")
    print()
    if n_fail == 0 and n_warn == 0:
        ok("Power on sequence finished - all {} VM(s) verified up.".format(n_ok))
    else:
        warn("Power on sequence finished: {} ok, {} with warnings, {} failed.".format(
            n_ok, n_warn, n_fail))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Bulk power management of VMs on a standalone ESXi 8.x host.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scan", action="store_true",
                       help="detect powered-on VMs, IPs and ICMP reachability, save to config")
    group.add_argument("--status", action="store_true",
                       help="show power state of all VMs + live ICMP test")
    group.add_argument("--off", action="store_true",
                       help="gracefully shut down all running VMs and verify via ping")
    group.add_argument("--on", action="store_true",
                       help="power on VMs from the last --scan and verify boot + ping")
    args = parser.parse_args()

    cfg = get_config()
    si = connect_esxi(cfg)

    if args.scan:
        action_scan(si, cfg)
    elif args.status:
        action_status(si, cfg)
    elif args.off:
        action_off(si, cfg)
    elif args.on:
        action_on(si, cfg)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        warn("Interrupted by user.")
        sys.exit(130)
