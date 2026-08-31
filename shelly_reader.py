#!/usr/bin/env python3
"""
shelly_reader.py - read every value a Shelly Gen2+ device exposes on the LAN.

The plug speaks the Shelly Gen2+ RPC API over plain HTTP and WebSocket. There is
no pairing and no cloud involved: the local API stays fully available while the
plug is signed in to the Shelly Smart Control app, connected to Shelly Cloud and
commissioned into Apple Home over Matter. Nothing here interferes with any of it.

Written for the Plug M Gen3 and works unchanged on the Plug S Gen3, Plug PM Gen3
and Outdoor Plug S Gen3. Battery sensors such as the H&T Gen3 speak the same RPC
API, so "dump", "info" and "call" work there too - but they sleep between
measurements, so use "listen" instead of "poll" unless they run on USB power.

Requirements:
    pip install requests zeroconf websocket-client colorama

Usage:
    python shelly_reader.py discover                    # find plugs via mDNS
    python shelly_reader.py --host 192.168.1.50 info    # identity + firmware
    python shelly_reader.py --host 192.168.1.50 dump    # absolutely everything
    python shelly_reader.py --host 192.168.1.50 dump --json
    python shelly_reader.py --host 192.168.1.50 poll --interval 5 --csv log.csv
    python shelly_reader.py --host 192.168.1.50 watch   # push updates, no polling
    python shelly_reader.py --host 192.168.1.50 on
    python shelly_reader.py listen --port 8088          # for sleeping sensors
    python shelly_reader.py --host 192.168.1.50 ble status
    python shelly_reader.py --host 192.168.1.50 matter off --reboot
    python shelly_reader.py --host 192.168.1.50 reboot --wait
    python shelly_reader.py --host 192.168.1.50 call Switch.ResetCounters '{"id":0,"type":["aenergy"]}'

Instead of --host every time, set the SHELLY_HOST environment variable.

If you enabled a password in the plug's web UI, pass --password. The username is
always "admin". Note that the WebSocket channel used by "watch" does not support
that authentication here - with a password set, use "poll" instead.

Windows 10/11: ANSI colours via colorama, UTF-8 console output, working Ctrl+C.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import textwrap
import time
from datetime import datetime
from typing import Any

import requests
from requests.auth import HTTPDigestAuth

IS_WINDOWS = sys.platform == "win32"
DEFAULT_TIMEOUT = 5.0
RPC_USER = "admin"


# ------------------------------------------------------------------- terminal

class _NoColour:
    def __getattr__(self, _name: str) -> str:
        return ""


try:
    from colorama import Fore, Style, just_fix_windows_console

    just_fix_windows_console()  # enables ANSI on legacy Windows 10 consoles
    HAVE_COLOUR = True
except ImportError:  # pragma: no cover - colorama is optional
    Fore = Style = _NoColour()  # type: ignore[assignment]
    HAVE_COLOUR = False


def _disable_colour() -> None:
    global Fore, Style, HAVE_COLOUR
    Fore = Style = _NoColour()  # type: ignore[assignment]
    HAVE_COLOUR = False


def setup_console() -> None:
    """UTF-8 output and sane colours, especially on the Windows console."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        _disable_colour()


def paint(text: str, colour: str, bright: bool = False) -> str:
    if not HAVE_COLOUR:
        return text
    return f"{Style.BRIGHT if bright else ''}{colour}{text}{Style.RESET_ALL}"


def info(text: str) -> None:
    print(paint(text, Fore.CYAN))


def ok(text: str) -> None:
    print(paint(text, Fore.GREEN, bright=True))


def warn(text: str) -> None:
    print(paint(text, Fore.YELLOW))


def die(text: str) -> None:
    print(paint(text, Fore.RED, bright=True), file=sys.stderr)
    sys.exit(1)


# -------------------------------------------------------------- field catalog

# Units and friendly labels for the values the plug reports. Anything not listed
# still gets printed - this only makes the output readable.
FIELD_META: dict[str, tuple[str, str]] = {
    "output": ("Relay output", ""),
    "apower": ("Active power", "W"),
    "voltage": ("Voltage", "V"),
    "current": ("Current", "A"),
    "pf": ("Power factor", ""),
    "freq": ("Line frequency", "Hz"),
    "aenergy.total": ("Energy consumed", "Wh"),
    "aenergy.by_minute": ("Energy last 3 min", "mWh"),
    "aenergy.minute_ts": ("Energy timestamp", "unix"),
    "ret_aenergy.total": ("Energy returned", "Wh"),
    "ret_aenergy.by_minute": ("Returned last 3 min", "mWh"),
    "ret_aenergy.minute_ts": ("Returned timestamp", "unix"),
    "temperature.tC": ("Device temperature", "degC"),
    "temperature.tF": ("Device temperature", "degF"),
    "counts.on_time": ("Total on-time", "s"),
    "counts.switch_on": ("Switch-on count", ""),
    "counts.on_above_thr": ("Time above threshold", "s"),
    "source": ("Last switched by", ""),
    "timer_started_at": ("Timer started", "unix"),
    "timer_duration": ("Timer duration", "s"),
    "sys.uptime": ("Uptime", "s"),
    "sys.ram_free": ("Free RAM", "B"),
    "sys.ram_size": ("Total RAM", "B"),
    "sys.fs_free": ("Free filesystem", "B"),
    "sys.fs_size": ("Filesystem size", "B"),
    "sys.mac": ("MAC address", ""),
    "sys.restart_required": ("Restart required", ""),
    "wifi.rssi": ("Wi-Fi signal", "dBm"),
    "wifi.sta_ip": ("IP address", ""),
    "wifi.ssid": ("Wi-Fi SSID", ""),
    "wifi.status": ("Wi-Fi status", ""),
    "cloud.connected": ("Shelly Cloud", ""),
    "mqtt.connected": ("MQTT broker", ""),
    # Sensor devices such as the H&T Gen3
    "tC": ("Temperature", "degC"),
    "tF": ("Temperature", "degF"),
    "rh": ("Relative humidity", "%"),
    "battery.V": ("Battery voltage", "V"),
    "battery.percent": ("Battery level", "%"),
    "external.present": ("USB power", ""),
}

# Timestamps worth rendering as local time rather than a raw epoch integer.
TIMESTAMP_FIELDS = {
    "aenergy.minute_ts", "ret_aenergy.minute_ts", "sys.unixtime",
    "counts.on_time_rst_ts", "counts.switch_on_rst_ts", "counts.on_above_thr_rst_ts",
    "timer_started_at",
}


def meta_key(path: str) -> str:
    """Strip the component prefix so 'switch:0.apower' finds 'apower'."""
    if path in FIELD_META:
        return path
    stripped = re.sub(r"^[a-z_0-9]+:\d+\.", "", path)
    return stripped if stripped in FIELD_META else path


def label_for(path: str) -> str:
    key = meta_key(path)
    if key in FIELD_META:
        return FIELD_META[key][0]
    return path.rsplit(".", 1)[-1].replace("_", " ")


def unit_for(path: str) -> str:
    return FIELD_META.get(meta_key(path), ("", ""))[1]


def flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    """Turn the nested RPC response into dotted paths: aenergy.total, sys.uptime..."""
    flat: dict[str, Any] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            flat.update(flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(obj, list):
        if obj and all(isinstance(item, (int, float)) for item in obj):
            flat[prefix] = obj  # keep by_minute[] together, it reads better
        else:
            for index, item in enumerate(obj):
                flat.update(flatten(item, f"{prefix}.{index}"))
    else:
        flat[prefix] = obj
    return flat


def fmt_value(path: str, value: Any) -> str:
    unit = unit_for(path)
    if value is None:
        return "-"
    if isinstance(value, bool):
        text = "ON" if value else "OFF"
    elif isinstance(value, list):
        text = ", ".join(f"{v:g}" if isinstance(v, float) else str(v) for v in value)
    elif meta_key(path) in TIMESTAMP_FIELDS and isinstance(value, (int, float)) and value > 1_000_000_000:
        text = datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
        unit = ""
    elif isinstance(value, float):
        text = f"{value:g}"
    else:
        text = str(value)
    return f"{text} {unit}".strip()


def colour_value(path: str, value: Any, width: int = 0) -> str:
    # Pad before colouring, otherwise invisible ANSI codes break the columns.
    text = fmt_value(path, value).ljust(width)
    if value is None or value == "":
        return paint(text, Fore.BLUE)
    if isinstance(value, bool):
        return paint(text, Fore.GREEN if value else Fore.RED, bright=True)
    if isinstance(value, (int, float, list)):
        return paint(text, Fore.YELLOW, bright=True)
    return paint(text, Fore.WHITE)


# -------------------------------------------------------------------- rpc i/o

class ShellyRPC:
    """Minimal client for the Shelly Gen2+ JSON-RPC API over HTTP."""

    def __init__(self, host: str, password: str | None = None, timeout: float = DEFAULT_TIMEOUT):
        self.host = host
        self.timeout = timeout
        self.session = requests.Session()
        if password:
            # Gen2+ devices use HTTP Digest with SHA-256; the user is always "admin".
            self.session.auth = HTTPDigestAuth(RPC_USER, password)

    @property
    def url(self) -> str:
        return f"http://{self.host}/rpc"

    def call(self, method: str, params: dict | None = None, soft: bool = False) -> Any:
        """Call an RPC method. With soft=True a network failure raises instead of
        exiting - used by the poll loop, where a battery device is simply asleep."""
        payload = {"id": 1, "method": method}
        if params:
            payload["params"] = params
        try:
            response = self.session.post(self.url, json=payload, timeout=self.timeout)
        except requests.exceptions.RequestException as error:
            if soft:
                raise ConnectionError(str(error) or type(error).__name__) from error
            if isinstance(error, requests.exceptions.Timeout):
                die(f"{self.host} did not answer within {self.timeout:g} s.")
            if isinstance(error, requests.exceptions.ConnectionError):
                die(f"Cannot reach {self.host}. Check the IP address "
                    f"and that you are on the same LAN.")
            die(f"Request to {self.host} failed: {error}")

        if response.status_code == 401:
            if self.session.auth:
                die("Authentication rejected. Check the password; the username is always 'admin'.")
            die("The plug requires authentication. Pass --password (username is always 'admin').")
        if response.status_code != 200:
            die(f"{self.host} answered HTTP {response.status_code}. "
                f"Is this really a Shelly Gen2+ device?")

        try:
            body = response.json()
        except ValueError:
            die(f"{self.host} did not return JSON. Is this really a Shelly Gen2+ device?")
        if "error" in body:
            error = body["error"]
            message = f"RPC error {error.get('code')}: {error.get('message')}"
            if soft:
                # e.g. asking about BLE pairings while the radio is switched off
                raise RuntimeError(message)
            die(message)
        return body.get("result")


# ------------------------------------------------------------------ discovery

def cmd_discover(args: argparse.Namespace) -> None:
    """Shelly devices announce themselves over mDNS as _shelly._tcp."""
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except ImportError:
        die("The 'discover' command needs the zeroconf package: pip install zeroconf\n"
            "Everything else works without it - just pass --host with the IP address.")

    found: dict[str, dict] = {}

    class Listener(ServiceListener):
        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            service = zc.get_service_info(type_, name, timeout=3000)
            if not service:
                return
            addresses = [addr for addr in service.parsed_addresses() if ":" not in addr]
            props = {
                key.decode(): (value.decode() if isinstance(value, bytes) else value)
                for key, value in (service.properties or {}).items()
                if key
            }
            found[name] = {
                "name": name.split(".")[0],
                "address": addresses[0] if addresses else "?",
                "port": service.port,
                "gen": props.get("gen", "?"),
                "app": props.get("app", "?"),
                "version": props.get("ver", "?"),
            }

        def update_service(self, *_args) -> None:
            pass

        def remove_service(self, *_args) -> None:
            pass

    zeroconf = Zeroconf()
    browser = ServiceBrowser(zeroconf, "_shelly._tcp.local.", Listener())
    info(f"Scanning for Shelly devices ({args.timeout:g} s)...")
    try:
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            time.sleep(0.25)
    finally:
        browser.cancel()
        zeroconf.close()

    if not found:
        warn("Nothing found. Make sure you are on the same L2 network as the plug "
             "(mDNS does not cross VLANs or VPNs) and that multicast is not blocked. "
             "On Windows, allow Python through the firewall on private networks.")
        return

    for entry in found.values():
        print()
        print(paint(f"  {entry['name']}", Fore.GREEN, bright=True))
        print(f"    Address  : {paint(entry['address'], Fore.WHITE, bright=True)}:{entry['port']}")
        print(f"    Model    : {entry['app']}  (generation {entry['gen']})")
        print(f"    Firmware : {entry['version']}")


# ----------------------------------------------------------------------- info

def cmd_info(args: argparse.Namespace) -> None:
    rpc = ShellyRPC(args.host, args.password)
    device = rpc.call("Shelly.GetDeviceInfo", {"ident": True})
    print()
    for path, value in flatten(device).items():
        print(f"  {label_for(path):<26} {colour_value(path, value)}")


# ----------------------------------------------------------------------- dump

def fetch_components(rpc: "ShellyRPC") -> dict[str, dict]:
    """Shelly.GetComponents also returns virtual and BTHome components, which
    Shelly.GetStatus does not. Paginated, so keep asking until we have them all."""
    found: dict[str, dict] = {}
    offset = 0
    while True:
        try:
            page = rpc.call("Shelly.GetComponents",
                            {"offset": offset, "include": ["status"]}, soft=True)
        except (ConnectionError, RuntimeError):
            break
        for entry in page.get("components", []):
            found[entry.get("key", "?")] = entry
        offset += len(page.get("components", []))
        if offset >= page.get("total", 0) or not page.get("components"):
            break
    return found


def cmd_dump(args: argparse.Namespace) -> None:
    """Everything the plug is willing to tell us, in one go."""
    rpc = ShellyRPC(args.host, args.password)
    device = rpc.call("Shelly.GetDeviceInfo", {"ident": True})
    status = rpc.call("Shelly.GetStatus")
    config = rpc.call("Shelly.GetConfig")
    methods = rpc.call("Shelly.ListMethods")

    if args.json:
        print(json.dumps(
            {"device_info": device, "status": status, "config": config, "methods": methods},
            indent=2, ensure_ascii=False))
        return

    def section(title: str) -> None:
        print()
        print(paint(f"=== {title} " + "=" * max(0, 56 - len(title)), Fore.MAGENTA, bright=True))

    section("DEVICE")
    for path, value in flatten(device).items():
        print(f"  {label_for(path):<26} {colour_value(path, value)}")

    section("STATUS")
    for component, payload in status.items():
        print()
        print("  " + paint(component, Fore.CYAN, bright=True))
        flat = flatten(payload)
        if not flat:
            print(paint("      (component present, no status properties)", Fore.BLUE))
            continue
        for path, value in flat.items():
            full = f"{component}.{path}"
            print(f"      {label_for(full):<26} = "
                  f"{colour_value(full, value, width=24)} "
                  f"{paint(path, Fore.BLUE)}")

    advertised = set(methods.get("methods", []))

    if "Shelly.GetComponents" in advertised:
        extra = fetch_components(rpc)
        unseen = {key: payload for key, payload in extra.items() if key not in status}
        if unseen:
            section("DYNAMIC COMPONENTS")
            for key, payload in unseen.items():
                print()
                print("  " + paint(key, Fore.CYAN, bright=True))
                for path, value in flatten(payload.get("status", {})).items():
                    print(f"      {label_for(f'{key}.{path}'):<26} = "
                          f"{colour_value(f'{key}.{path}', value, width=24)} "
                          f"{paint(path, Fore.BLUE)}")

    listings = [name for name in
                ("Script.List", "Schedule.List", "Webhook.List", "KVS.List",
                 "BLE.ListPairedDevices", "BTHome.GetStatus")
                if name in advertised]
    if listings:
        section("STORED ITEMS")
        for name in listings:
            try:
                result = rpc.call(name, soft=True)
            except (ConnectionError, RuntimeError) as error:
                # a disabled subsystem answers with an error; that is information too
                print(f"  {paint(name, Fore.CYAN):<32} {paint(str(error), Fore.BLUE)}")
                continue
            summary = json.dumps(result, ensure_ascii=False)
            if len(summary) > 400:
                summary = summary[:400] + " ..."
            print(f"  {paint(name, Fore.CYAN):<32} {summary}")

    if args.config:
        section("CONFIG")
        print(json.dumps(config, indent=2, ensure_ascii=False))

    section(f"AVAILABLE RPC METHODS ({len(methods.get('methods', []))})")
    for method in methods.get("methods", []):
        print(f"  {method}")


# ----------------------------------------------------------------------- poll

def numeric_paths(flat: dict[str, Any]) -> list[str]:
    return [path for path, value in flat.items()
            if isinstance(value, (int, float, bool)) and not isinstance(value, str)]


PREFERRED = [
    # power devices
    "switch:0.output", "switch:0.apower", "switch:0.voltage", "switch:0.current",
    "switch:0.pf", "switch:0.freq", "switch:0.aenergy.total", "switch:0.temperature.tC",
    "pm1:0.apower", "pm1:0.voltage", "pm1:0.current", "pm1:0.aenergy.total",
    # sensor devices such as the H&T Gen3
    "temperature:0.tC", "humidity:0.rh",
    "devicepower:0.battery.percent", "devicepower:0.battery.V",
    "devicepower:0.external.present",
]


def read_flat_status(rpc: "ShellyRPC", soft: bool = False) -> dict[str, Any]:
    """Whole-device status, flattened to component-qualified paths."""
    status = rpc.call("Shelly.GetStatus", soft=soft)
    flat: dict[str, Any] = {}
    for component, payload in status.items():
        for path, value in flatten(payload).items():
            flat[f"{component}.{path}"] = value
    return flat


def cmd_poll(args: argparse.Namespace) -> None:
    """Repeatedly read the whole device, optionally into a CSV file.

    Uses Shelly.GetStatus rather than a single component, so the same command
    works for a plug (power, energy) and for a sensor (temperature, humidity,
    battery) without any switches.
    """
    rpc = ShellyRPC(args.host, args.password)

    first = read_flat_status(rpc)
    columns = [path for path in numeric_paths(first) if not path.endswith(".id")]

    writer = None
    csv_file = None
    if args.csv:
        csv_file = open(args.csv, "a", newline="", encoding="utf-8")
        writer = csv.writer(csv_file)
        if csv_file.tell() == 0:
            header = [f"{path} [{unit_for(path)}]".replace(" []", "") for path in columns]
            writer.writerow(["timestamp"] + header)

    highlight = [path for path in PREFERRED if path in first]
    if not highlight:  # unknown device: show whatever numbers it has
        highlight = [path for path in columns if not path.startswith("sys.")][:8]

    info(f"Polling {args.host} every {args.interval:g} s. Press Ctrl+C to stop.")
    try:
        while True:
            try:
                flat = read_flat_status(rpc, soft=True)
            except (ConnectionError, RuntimeError):  # battery device back asleep
                warn(f"{datetime.now():%H:%M:%S}  no answer, device asleep or offline")
                flat = {}

            now = datetime.now()
            if flat:
                line = "  ".join(
                    f"{paint(label_for(path), Fore.CYAN)}={colour_value(path, flat[path])}"
                    for path in highlight if path in flat
                )
                print(f"{paint(now.strftime('%H:%M:%S'), Fore.WHITE)}  {line}")

                if writer:
                    writer.writerow([now.isoformat(timespec="seconds")] +
                                    [flat.get(path) for path in columns])
                    csv_file.flush()

            # Sleep in slices so Ctrl+C is picked up promptly on Windows.
            remaining = args.interval
            while remaining > 0:
                time.sleep(min(0.25, remaining))
                remaining -= 0.25
    except KeyboardInterrupt:
        print()
        info("Stopped.")
    finally:
        if csv_file:
            csv_file.close()


# ---------------------------------------------------------------------- watch

def cmd_watch(args: argparse.Namespace) -> None:
    """Subscribe to push notifications - the plug reports changes on its own."""
    import websocket

    if args.password:
        warn("A password is set; the WebSocket channel is not authenticated here. "
             "Use 'poll' instead if this fails.")

    url = f"ws://{args.host}/rpc"
    info(f"Connecting to {url} ...")
    try:
        socket = websocket.create_connection(url, timeout=DEFAULT_TIMEOUT)
    except Exception as error:
        die(f"Cannot open the WebSocket: {error}")

    socket.settimeout(1.0)
    # Sending one request registers our "src", which is what makes the device
    # start pushing NotifyStatus and NotifyEvent frames back to us.
    socket.send(json.dumps({"id": 1, "src": "pyreader", "method": "Shelly.GetStatus"}))

    ok("Connected. Waiting for updates - press Ctrl+C to stop.")
    print(paint("Note: the plug pushes a change only when a value moves enough to "
                "matter (roughly 5% for voltage), plus an energy tick every minute.",
                Fore.BLUE))
    try:
        while True:
            try:
                frame = json.loads(socket.recv())
            except websocket.WebSocketTimeoutException:
                continue
            except (websocket.WebSocketConnectionClosedException, ValueError):
                warn("Connection closed by the device.")
                break

            stamp = paint(datetime.now().strftime("%H:%M:%S"), Fore.WHITE)
            method = frame.get("method")

            if method in ("NotifyStatus", "NotifyFullStatus"):
                params = frame.get("params", {})
                for component, payload in params.items():
                    if component == "ts":
                        continue
                    for path, value in flatten(payload).items():
                        if path == "id":
                            continue
                        print(f"{stamp}  {paint(component, Fore.MAGENTA)} "
                              f"{paint(label_for(path), Fore.CYAN)} -> "
                              f"{colour_value(path, value)}")
            elif method == "NotifyEvent":
                for event in frame.get("params", {}).get("events", []):
                    print(f"{stamp}  {paint('event', Fore.YELLOW)} "
                          f"{event.get('component')}: {event.get('event')}")
            elif "error" in frame:
                error = frame["error"]
                if error.get("code") == 401:
                    die("The plug has authentication enabled and the WebSocket channel "
                        "is not authenticated here. Use 'poll --password ...' instead.")
                warn(f"RPC error {error.get('code')}: {error.get('message')}")
            elif "result" in frame:
                ok("Initial state received:")
                for component, payload in frame["result"].items():
                    for path, value in flatten(payload).items():
                        full = f"{component}.{path}" if component in ("sys", "wifi") else path
                        print(f"    {component}.{path:<24} {colour_value(full, value)}")
    except KeyboardInterrupt:
        print()
        info("Stopped.")
    finally:
        socket.close()




# ------------------------------------------------------------- radio switches

def set_config_verified(rpc: "ShellyRPC", component: str, config: dict) -> bool:
    """Apply a SetConfig and check it actually stuck.

    Shelly silently drops configuration keys it does not know: SetConfig answers
    restart_required=false and nothing changes. So always read the config back.
    """
    before = rpc.call(f"{component}.GetConfig")
    result = rpc.call(f"{component}.SetConfig", {"config": config})
    after = rpc.call(f"{component}.GetConfig")

    if after != before:
        ok(f"{component} configuration updated.")
        applied = True
    elif result and result.get("restart_required"):
        ok(f"{component} accepted the change; a restart is needed to apply it.")
        applied = True
    else:
        warn(f"{component} did not change. This firmware most likely ignores the key "
             f"{', '.join(config)} for this component.")
        applied = False

    print(f"  now: {json.dumps(after, ensure_ascii=False)}")
    return applied and bool(result and result.get("restart_required"))


def reboot_and_wait(rpc: "ShellyRPC", seconds: float = 12.0) -> None:
    info("Rebooting...")
    rpc.call("Shelly.Reboot")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(1.0)
        try:
            rpc.call("Shelly.GetDeviceInfo", soft=True)
            ok("Back online.")
            return
        except (ConnectionError, RuntimeError):
            continue
    warn("The device has not answered yet; give it a few more seconds.")


def cmd_reboot(args: argparse.Namespace) -> None:
    rpc = ShellyRPC(args.host, args.password)
    if args.wait:
        reboot_and_wait(rpc)
    else:
        rpc.call("Shelly.Reboot")
        ok("Reboot command sent.")


def cmd_matter(args: argparse.Namespace) -> None:
    rpc = ShellyRPC(args.host, args.password)

    if args.action == "status":
        status = rpc.call("Matter.GetStatus")
        config = rpc.call("Matter.GetConfig")
        for path, value in flatten({**config, **status}).items():
            print(f"  {label_for(path):<26} {colour_value(path, value)}")
        if status.get("commissionable") and not status.get("num_fabrics"):
            print(paint("  Waiting to be commissioned - this is what keeps the "
                        "Bluetooth beacon advertising.", Fore.BLUE))
        return

    if args.action == "code":
        result = rpc.call("Matter.GetSetupCode")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print(paint("  Type this into Apple Home instead of scanning the QR code.", Fore.BLUE))
        return

    enable = args.action == "on"
    needs_restart = set_config_verified(rpc, "Matter", {"enable": enable})
    if needs_restart:
        reboot_and_wait(rpc) if args.reboot else warn("Restart required: run 'reboot'.")


def cmd_ble(args: argparse.Namespace) -> None:
    rpc = ShellyRPC(args.host, args.password)

    if args.action == "status":
        status = rpc.call("BLE.GetStatus")
        config = rpc.call("BLE.GetConfig")
        print(f"  status: {json.dumps(status, ensure_ascii=False)}")
        print(f"  config: {json.dumps(config, ensure_ascii=False)}")
        if not status:
            ok("  The radio is off - no address, no advertising.")
        elif "advertising" in status.get("flags", []):
            warn("  The radio is advertising. If BLE RPC is already disabled, the "
                 "beacon belongs to Matter waiting to be commissioned.")
        return

    enable = args.action in ("on", "rpc-on")
    if args.action in ("rpc-on", "rpc-off"):
        config = {"rpc": {"enable": enable}}
    else:
        config = {"enable": enable}

    needs_restart = set_config_verified(rpc, "BLE", config)
    if args.action in ("on", "off"):
        print(paint("  Note: some firmware builds have no master switch for the BLE "
                    "radio. If nothing changed, the radio follows Matter - turn that "
                    "off with 'matter off' instead.", Fore.BLUE))
    if needs_restart:
        reboot_and_wait(rpc) if args.reboot else warn("Restart required: run 'reboot'.")


# --------------------------------------------------------------------- listen

def cmd_listen(args: argparse.Namespace) -> None:
    """Receive pushes from a device that sleeps, such as the H&T Gen3.

    A battery-powered sensor is unreachable most of the time, so nothing can
    poll it. Instead it wakes up, pushes its readings and sleeps again. Point an
    outbound webhook at this listener and you get the data without the device
    ever having to answer an incoming request.

    Create the webhook once (replace the IP with the machine running this):

      python shelly_reader.py --host <sensor-ip> call Webhook.Create \
        '{"cid":0,"enable":true,"event":"temperature.change",
          "urls":["http://192.168.1.10:8088/?t=${ev.tC}"]}'

      python shelly_reader.py --host <sensor-ip> call Webhook.Create \
        '{"cid":0,"enable":true,"event":"humidity.change",
          "urls":["http://192.168.1.10:8088/?rh=${ev.rh}"]}'

    Webhook.ListSupported on the device lists every event you can hook into.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    csv_file = open(args.csv, "a", newline="", encoding="utf-8") if args.csv else None
    writer = csv.writer(csv_file) if csv_file else None
    if writer and csv_file.tell() == 0:
        writer.writerow(["timestamp", "source", "payload"])

    class Handler(BaseHTTPRequestHandler):
        def _record(self, payload: str) -> None:
            stamp = datetime.now()
            print(f"{paint(stamp.strftime('%H:%M:%S'), Fore.WHITE)}  "
                  f"{paint(self.client_address[0], Fore.MAGENTA)}  "
                  f"{paint(payload, Fore.YELLOW, bright=True)}")
            if writer:
                writer.writerow([stamp.isoformat(timespec="seconds"),
                                 self.client_address[0], payload])
                csv_file.flush()
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            query = parse_qs(urlparse(self.path).query)
            flat = {key: values[0] if len(values) == 1 else values
                    for key, values in query.items()}
            self._record(json.dumps(flat, ensure_ascii=False) if flat else self.path)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            self._record(self.rfile.read(length).decode("utf-8", "replace"))

        def log_message(self, *_args) -> None:
            pass  # our own output is enough

    server = HTTPServer(("0.0.0.0", args.port), Handler)
    server.timeout = 0.25
    info(f"Listening on port {args.port}. Point the device's webhooks at "
         f"http://<this-machine-ip>:{args.port}/ . Press Ctrl+C to stop.")
    try:
        while True:
            server.handle_request()  # short timeout keeps Ctrl+C responsive
    except KeyboardInterrupt:
        print()
        info("Stopped.")
    finally:
        server.server_close()
        if csv_file:
            csv_file.close()



# ---------------------------------------------------------------------- serve

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_frames(connection):
    """Yield text payloads from a WebSocket connection (client frames are masked)."""
    import struct

    buffer = b""
    message = ""

    def read_exactly(count: int) -> bytes:
        nonlocal buffer
        while len(buffer) < count:
            chunk = connection.recv(4096)
            if not chunk:
                raise ConnectionError("closed")
            buffer += chunk
        head, buffer = buffer[:count], buffer[count:]
        return head

    while True:
        first, second = read_exactly(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", read_exactly(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", read_exactly(8))[0]
        mask = read_exactly(4) if masked else b""
        payload = read_exactly(length)
        if masked:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))

        if opcode == 0x8:  # close
            return
        if opcode == 0x9:  # ping -> pong
            connection.sendall(b"\x8a" + bytes([len(payload)]) + payload)
            continue
        if opcode in (0x0, 0x1):
            message += payload.decode("utf-8", "replace")
            if fin:
                yield message
                message = ""


def _ws_handshake(connection) -> bool:
    import base64
    import hashlib

    request = b""
    while b"\r\n\r\n" not in request:
        chunk = connection.recv(1024)
        if not chunk:
            return False
        request += chunk

    key = ""
    for line in request.decode("utf-8", "replace").split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
    if not key:
        connection.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        return False

    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    connection.sendall(
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        + f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode())
    return True


def cmd_serve(args: argparse.Namespace) -> None:
    """Accept outbound WebSocket connections from Shelly devices.

    Configured with WS.SetConfig, a device dials out to this server and pushes
    NotifyFullStatus on connect plus NotifyStatus on every change. That works
    through NAT, needs no polling, and - crucially for battery sensors such as
    the H&T Gen3 - happens while the device is briefly awake.
    """
    import socket
    import threading

    csv_file = open(args.csv, "a", newline="", encoding="utf-8") if args.csv else None
    writer = csv.writer(csv_file) if csv_file else None
    columns: list[str] = []
    lock = threading.Lock()

    def show(source: str, frame: dict) -> None:
        method = frame.get("method", "")
        params = frame.get("params", {})
        stamp = datetime.now()
        if method not in ("NotifyStatus", "NotifyFullStatus", "NotifyEvent"):
            return

        flat: dict[str, Any] = {}
        for component, payload in params.items():
            if component == "ts" or not isinstance(payload, dict):
                continue
            for path, value in flatten(payload).items():
                flat[f"{component}.{path}"] = value

        interesting = [path for path in PREFERRED if path in flat] or \
                      [path for path in flat if not path.endswith(".id")][:8]
        line = "  ".join(f"{paint(label_for(path), Fore.CYAN)}={colour_value(path, flat[path])}"
                         for path in interesting)
        print(f"{paint(stamp.strftime('%H:%M:%S'), Fore.WHITE)}  "
              f"{paint(source, Fore.MAGENTA)}  {line or method}")

        if writer and flat:
            with lock:
                nonlocal columns
                if not columns:
                    columns = [path for path, value in flat.items()
                               if isinstance(value, (int, float, bool))]
                    writer.writerow(["timestamp", "device"] +
                                    [f"{c} [{unit_for(c)}]".replace(" []", "") for c in columns])
                writer.writerow([stamp.isoformat(timespec="seconds"), source] +
                                [flat.get(c) for c in columns])
                csv_file.flush()

    def handle(connection, address) -> None:
        source = address[0]
        try:
            if not _ws_handshake(connection):
                return
            ok(f"Device connected from {source}")
            for message in _ws_frames(connection):
                try:
                    frame = json.loads(message)
                except ValueError:
                    continue
                show(frame.get("src", source), frame)
        except (ConnectionError, OSError):
            pass
        finally:
            warn(f"Device {source} disconnected")
            connection.close()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", args.port))
    server.listen(8)
    server.settimeout(0.25)

    info(f"WebSocket server on port {args.port}. Configure the device with:\n"
         f'  call WS.SetConfig "{{\\"config\\":{{\\"enable\\":true,'
         f'\\"server\\":\\"ws://<this-machine-ip>:{args.port}/rpc\\"}}}}"\n'
         f"Then reboot it. Press Ctrl+C to stop.")
    try:
        while True:
            try:
                connection, address = server.accept()
            except socket.timeout:
                continue
            threading.Thread(target=handle, args=(connection, address), daemon=True).start()
    except KeyboardInterrupt:
        print()
        info("Stopped.")
    finally:
        server.close()
        if csv_file:
            csv_file.close()


# -------------------------------------------------------------------- control

def cmd_switch(args: argparse.Namespace) -> None:
    rpc = ShellyRPC(args.host, args.password)
    status = rpc.call("Shelly.GetStatus")
    if not any(key.startswith("switch:") for key in status):
        die("This device has no relay to switch. Components it does have: "
            + ", ".join(sorted(status)))
    if args.command == "toggle":
        result = rpc.call("Switch.Toggle", {"id": args.id})
        ok(f"Toggled; it was {'ON' if result.get('was_on') else 'OFF'} before.")
        return
    turn_on = args.command == "on"
    rpc.call("Switch.Set", {"id": args.id, "on": turn_on})
    ok(f"Relay switched {'ON' if turn_on else 'OFF'}.")


def cmd_call(args: argparse.Namespace) -> None:
    """Escape hatch: invoke any RPC method the plug advertises."""
    params = None
    if args.params:
        raw = args.params.strip()
        # cmd.exe does not strip single quotes the way a POSIX shell does, so a
        # copy-pasted '{"a":1}' arrives with the quotes still attached.
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\u2018\u2019":
            raw = raw[1:-1]
        try:
            params = json.loads(raw)
        except json.JSONDecodeError as error:
            die(f"The params argument is not valid JSON: {error}\n"
                f"On the Windows command prompt, quote it like this:\n"
                f'  call {args.method} "{{\\"config\\":{{\\"enable\\":false}}}}"')

    rpc = ShellyRPC(args.host, args.password)
    print(json.dumps(rpc.call(args.method, params), indent=2, ensure_ascii=False))


# ----------------------------------------------------------------------- main

RAW = argparse.RawDescriptionHelpFormatter

MAIN_EPILOG = """
commands at a glance
  reading        discover, info, dump, poll, watch, listen
  controlling    on, off, toggle, reboot
  radios         matter, ble
  anything else  call

typical first run
  pip install requests zeroconf websocket-client colorama
  shelly_reader.py discover
  shelly_reader.py --host 192.168.1.50 info
  shelly_reader.py --host 192.168.1.50 dump

saving the address and password
  Windows:  set SHELLY_HOST=192.168.1.50
            set SHELLY_PASSWORD=secret
  Linux:    export SHELLY_HOST=192.168.1.50
  Then --host and --password can be left out entirely.

quoting JSON on Windows
  cmd.exe does not understand single quotes. Write:
    call Switch.SetConfig "{\\"id\\":0,\\"config\\":{\\"power_limit\\":2900}}"
  PowerShell and Linux shells accept the simpler form:
    call Switch.SetConfig '{"id":0,"config":{"power_limit":2900}}'

Run 'shelly_reader.py COMMAND --help' for the details of one command.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shelly_reader.py",
        formatter_class=RAW,
        description=("Read all local data from a Shelly Gen2+ device over its local RPC API.\n"
                     "No pairing, no cloud: the plug answers on plain HTTP while it stays\n"
                     "connected to the Shelly app, the cloud and Apple Home over Matter."),
        epilog=MAIN_EPILOG)
    parser.add_argument("--host", metavar="ADDR", default=os.environ.get("SHELLY_HOST"),
                        help="device IP address or hostname; defaults to $SHELLY_HOST. "
                             "Not needed for 'discover' and 'listen'")
    parser.add_argument("--password", metavar="PASS", default=os.environ.get("SHELLY_PASSWORD"),
                        help="web UI password if authentication is enabled; defaults to "
                             "$SHELLY_PASSWORD. The username is always 'admin'")
    parser.add_argument("--no-color", action="store_true",
                        help="plain output with no ANSI colours (also honours $NO_COLOR)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND",
                                title="commands")

    def add(name: str, summary: str, detail: str, examples: str):
        return sub.add_parser(name, help=summary, formatter_class=RAW,
                              description=textwrap.dedent(detail).strip(),
                              epilog="examples:\n" + examples.rstrip())

    # --------------------------------------------------------------- reading
    p = add("discover", "find Shelly devices on the network",
            """
            Browses mDNS for _shelly._tcp and lists every Shelly that answers,
            with its address, model, generation and firmware version. Needs the
            zeroconf package and a network without VLAN or VPN boundaries in the
            way, because multicast does not cross them. --host is ignored here.
            """,
            "  shelly_reader.py discover\n"
            "  shelly_reader.py discover --timeout 15\n")
    p.add_argument("--timeout", type=float, default=6.0, metavar="SEC",
                   help="how long to collect mDNS answers (default: 6)")
    p.set_defaults(func=cmd_discover, needs_host=False)

    p = add("info", "identity, firmware, Matter and auth state",
            """
            Prints Shelly.GetDeviceInfo: name, device id, MAC, model code,
            generation, firmware version, whether authentication is on and
            whether Matter is enabled. The quickest way to check you are talking
            to the right box.
            """,
            "  shelly_reader.py --host 192.168.1.50 info\n")
    p.set_defaults(func=cmd_info)

    p = add("dump", "everything the device exposes, in one go",
            """
            The full picture: device info, the status of every component, any
            dynamic and virtual components, stored scripts, schedules, webhooks
            and KVS entries, and the complete list of RPC methods this firmware
            supports. That method list tells you what else you can reach with
            the 'call' command.
            """,
            "  shelly_reader.py --host 192.168.1.50 dump\n"
            "  shelly_reader.py --host 192.168.1.50 dump --config\n"
            "  shelly_reader.py --host 192.168.1.50 dump --json > device.json\n")
    p.add_argument("--json", action="store_true",
                   help="raw JSON instead of the formatted report, for piping to a file")
    p.add_argument("--config", action="store_true",
                   help="also print the full configuration tree")
    p.set_defaults(func=cmd_dump)

    p = add("poll", "read the whole device on a timer, optionally to CSV",
            """
            Calls Shelly.GetStatus every few seconds and prints the interesting
            values. Works on a plug (power, voltage, current, energy, chip
            temperature) and on a sensor (temperature, humidity, battery) alike,
            because it reads the whole device rather than one component. With
            --csv every numeric field is appended to a file, columns and all.
            A device that stops answering is reported and retried, not fatal.
            """,
            "  shelly_reader.py --host 192.168.1.50 poll\n"
            "  shelly_reader.py --host 192.168.1.50 poll --interval 5\n"
            "  shelly_reader.py --host 192.168.1.50 poll --interval 60 --csv power.csv\n")
    p.add_argument("--interval", type=float, default=10.0, metavar="SEC",
                   help="seconds between reads (default: 10)")
    p.add_argument("--csv", metavar="FILE",
                   help="append every reading to this CSV file, header written once")
    p.set_defaults(func=cmd_poll)

    p = add("watch", "live push updates over WebSocket",
            """
            Opens ws://<host>/rpc and prints NotifyStatus and NotifyEvent frames
            as the device sends them, so there is no polling at all. The device
            pushes when a value moves enough to matter, plus an energy tick every
            minute. Does not work when the device has a password set - the
            WebSocket channel authenticates differently; use 'poll' instead.
            """,
            "  shelly_reader.py --host 192.168.1.50 watch\n")
    p.set_defaults(func=cmd_watch)

    p = add("listen", "receive webhooks from a sleeping battery device",
            """
            Starts a small HTTP server on this machine and prints whatever the
            device posts to it. Meant for battery sensors such as the H&T Gen3,
            which sleep between measurements and cannot be polled: they wake up,
            push, and sleep again. Create the webhook once on the device with
            'call Webhook.Create', pointing at this machine's address, and use
            'call Webhook.ListSupported' to see which events exist. --host is
            ignored here; the traffic comes to you.
            """,
            "  shelly_reader.py listen --port 8088\n"
            "  shelly_reader.py listen --port 8088 --csv sensor.csv\n"
            "  shelly_reader.py --host 192.168.1.77 call Webhook.ListSupported\n")
    p.add_argument("--port", type=int, default=8088, metavar="PORT",
                   help="TCP port to listen on (default: 8088)")
    p.add_argument("--csv", metavar="FILE", help="append every received push to this CSV file")
    p.set_defaults(func=cmd_listen, needs_host=False)

    p = add("serve", "accept outbound WebSocket pushes from devices",
            """
            Runs a WebSocket server that Shelly devices dial out to, configured
            with WS.SetConfig. On connect a device sends its complete status, and
            after that every change - so you get temperature, humidity, battery
            voltage and signal strength in one payload, without templating each
            value into a URL the way webhooks require.

            This is the best fit for a battery sensor such as the H&T Gen3: it
            pushes while briefly awake, and the connection is outbound, so no
            port forwarding and no waiting for the device to answer. --host is
            ignored; the devices come to you.
            """,
            "  shelly_reader.py serve --port 8090\n"
            "  shelly_reader.py serve --port 8090 --csv sensor.csv\n"
            "  shelly_reader.py --host 192.168.1.77 call WS.SetConfig "
            "\"{\\\"config\\\":{\\\"enable\\\":true,"
            "\\\"server\\\":\\\"ws://192.168.1.10:8090/rpc\\\"}}\"\n")
    p.add_argument("--port", type=int, default=8090, metavar="PORT",
                   help="TCP port to listen on (default: 8090)")
    p.add_argument("--csv", metavar="FILE", help="append every push to this CSV file")
    p.set_defaults(func=cmd_serve, needs_host=False)

    # ----------------------------------------------------------- controlling
    for name, summary, verb in (("on", "switch the relay on", "on"),
                                ("off", "switch the relay off", "off"),
                                ("toggle", "flip the relay to the other state", "toggle")):
        p = add(name, summary,
                f"""
                Switches the relay {verb}. Refuses with a clear message on devices
                that have no relay, such as sensors. Use --id only on multi-channel
                devices; a single plug is always channel 0.
                """,
                f"  shelly_reader.py --host 192.168.1.50 {name}\n"
                f"  shelly_reader.py --host 192.168.1.50 {name} --id 1\n")
        p.add_argument("--id", type=int, default=0, metavar="N",
                       help="switch channel id (default: 0)")
        p.set_defaults(func=cmd_switch)

    p = add("reboot", "restart the device",
            """
            Sends Shelly.Reboot. With --wait the script keeps pinging the device
            afterwards and tells you when it is back, instead of you guessing how
            long to wait. Needed after some configuration changes, which report
            restart_required.
            """,
            "  shelly_reader.py --host 192.168.1.50 reboot\n"
            "  shelly_reader.py --host 192.168.1.50 reboot --wait\n")
    p.add_argument("--wait", action="store_true",
                   help="poll the device until it answers again")
    p.set_defaults(func=cmd_reboot)

    # ---------------------------------------------------------------- radios
    p = add("matter", "Matter on/off, status, or the pairing code",
            """
            status  show whether Matter is enabled, how many fabrics it has joined
                    and whether it is still waiting to be commissioned
            on/off  enable or disable Matter, verifying the change actually stuck
            code    print the manual setup code to type into Apple Home instead
                    of hunting for the QR sticker

            An uncommissioned device with Matter enabled keeps a Bluetooth beacon
            advertising, which is how phones find it. Turning Matter off silences
            that beacon - but then it cannot be added to Apple Home.
            """,
            "  shelly_reader.py --host 192.168.1.50 matter status\n"
            "  shelly_reader.py --host 192.168.1.50 matter code\n"
            "  shelly_reader.py --host 192.168.1.50 matter off --reboot\n")
    p.add_argument("action", choices=["status", "on", "off", "code"], metavar="ACTION",
                   help="status | on | off | code")
    p.add_argument("--reboot", action="store_true",
                   help="reboot immediately if the change needs it")
    p.set_defaults(func=cmd_matter)

    p = add("ble", "Bluetooth radio and BLE RPC on/off, or status",
            """
            status            print BLE status and config, and say what an
                              advertising radio most likely belongs to
            on / off          try the master switch for the radio
            rpc-on / rpc-off  enable or disable RPC over Bluetooth, which is the
                              channel the phone app uses for setup

            Some firmware builds have no master switch: 'off' then reports that
            nothing changed, and the radio is in fact driven by Matter. Every
            change is read back, because Shelly silently ignores configuration
            keys it does not recognise.
            """,
            "  shelly_reader.py --host 192.168.1.50 ble status\n"
            "  shelly_reader.py --host 192.168.1.50 ble rpc-off --reboot\n"
            "  shelly_reader.py --host 192.168.1.50 ble off\n")
    p.add_argument("action", choices=["status", "on", "off", "rpc-on", "rpc-off"],
                   metavar="ACTION", help="status | on | off | rpc-on | rpc-off")
    p.add_argument("--reboot", action="store_true",
                   help="reboot immediately if the change needs it")
    p.set_defaults(func=cmd_ble)

    # ----------------------------------------------------------------- call
    p = add("call", "invoke any RPC method directly",
            """
            The escape hatch. METHOD is any method the firmware advertises - run
            'dump' to see the whole list - and PARAMS is an optional JSON object.
            Everything the web UI can do is reachable this way: configuration,
            schedules, webhooks, scripts, firmware updates, factory reset.

            After any SetConfig, read the configuration back with the matching
            GetConfig. Shelly accepts unknown keys without complaint and simply
            ignores them, so a successful-looking answer proves nothing.
            """,
            "  shelly_reader.py --host 192.168.1.50 call Switch.GetStatus\n"
            "  shelly_reader.py --host 192.168.1.50 call Wifi.GetConfig\n"
            "  shelly_reader.py --host 192.168.1.50 call Shelly.ListMethods\n"
            "  shelly_reader.py --host 192.168.1.50 call Matter.GetSetupCode\n"
            "  shelly_reader.py --host 192.168.1.50 call Switch.ResetCounters "
            "\"{\\\"id\\\":0}\"\n")
    p.add_argument("method", metavar="METHOD",
                   help="RPC method name, e.g. Switch.GetStatus or Sys.GetConfig")
    p.add_argument("params", nargs="?", metavar="PARAMS",
                   help="optional JSON object with the method parameters")
    p.set_defaults(func=cmd_call)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    setup_console()
    if args.no_color:
        _disable_colour()

    if getattr(args, "needs_host", True) and not args.host:
        die("No plug address. Pass --host 192.168.x.x, set SHELLY_HOST, "
            "or run the 'discover' command first.")

    try:
        args.func(args)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
