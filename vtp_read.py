#!/usr/bin/env python3
"""
vtp_read.py - Read VTP info and the VLAN database out of a Cisco vlan.dat file.

Offline equivalent of "show vtp status" / "show vtp password" / "show vlan brief":
it parses a vlan.dat backup and prints the VTP domain, mode-independent header
fields (revision, updater, timestamp, MD5 digest, password) and a colored table
of every VLAN in the database.

Colors use colorama, so they render correctly in the Windows 10 console / cmd /
PowerShell as well as on Linux and macOS. If colorama is not installed the
script still runs, just without color.

Usage:
    python vtp_read.py vlan.dat
    python vtp_read.py vlan.dat --no-color
    python vtp_read.py vlan.dat --sort id|name
"""

import argparse
import re
import struct
import sys

MAGIC = 0xBADB100D

# ---- colorama / color handling (Windows 10 friendly) -----------------------
try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init(autoreset=True)          # translates ANSI for the Win10 console
    _HAVE_COLORAMA = True
except ImportError:                        # graceful fallback, no hard dependency
    _HAVE_COLORAMA = False

    class _Dummy:
        def __getattr__(self, _):
            return ""
    Fore = Style = _Dummy()


def paint(text, color, enabled=True, bright=False):
    if not enabled or not _HAVE_COLORAMA:
        return text
    pre = (Style.BRIGHT if bright else "") + color
    return f"{pre}{text}{Style.RESET_ALL}"


# ---- file parsing ----------------------------------------------------------
def read_file(path):
    with open(path, "rb") as f:
        return f.read()


def parse_header(data):
    """Parse the fixed VTP header of a vlan.dat file (big-endian).

        0x00  u32   magic = 0xBADB100D
        0x04  u32   format version
        0x08  u8    tag, u8 len, <VTP domain name>        (e.g. "DPMO_VTP")
        0x2C  u32   VTP configuration revision number
        0x30  4B    local updater ID (IPv4 address)
        0x38  12B   last-modified timestamp, ASCII "YYMMDDHHMMSS"
        0x44  16B   MD5 digest of the VTP password
        0x54  u8    VTP password length, then the password (plaintext when the
                    password was set WITHOUT the 'hidden' keyword, as in VTPv1/v2)
    """
    if len(data) < 0x60:
        raise ValueError("File too short to be a vlan.dat")

    magic = struct.unpack_from(">I", data, 0x00)[0]
    if magic != MAGIC:
        raise ValueError(
            f"Bad magic 0x{magic:08X} (expected 0x{MAGIC:08X}); not a vlan.dat file"
        )

    version = struct.unpack_from(">I", data, 0x04)[0]

    dom_len = data[0x09]
    domain = data[0x0A:0x0A + dom_len].decode("ascii", "replace").strip("\x00")
    if not domain:
        domain = "<null / not configured>"

    revision = struct.unpack_from(">I", data, 0x2C)[0]
    updater = ".".join(str(b) for b in data[0x30:0x34])

    raw_ts = data[0x38:0x44].decode("ascii", "replace")
    timestamp = pretty_timestamp(raw_ts)

    md5 = data[0x44:0x54]
    md5_hex = " ".join(f"0x{b:02X}" for b in md5)
    md5_is_zero = all(b == 0 for b in md5)

    pw_len = data[0x54]
    password = None
    if 1 <= pw_len <= 32:
        cand = data[0x55:0x55 + pw_len]
        if all(0x20 <= b < 0x7F for b in cand):
            password = cand.decode("ascii")

    return {
        "magic": magic,
        "version": version,
        "domain": domain,
        "revision": revision,
        "updater": updater,
        "timestamp": timestamp,
        "md5_hex": md5_hex,
        "md5_is_zero": md5_is_zero,
        "password": password,
    }


def pretty_timestamp(raw):
    """ "YYMMDDHHMMSS" -> "20YY-MM-DD HH:MM:SS" when it looks numeric."""
    if len(raw) == 12 and raw.isdigit():
        return (f"20{raw[0:2]}-{raw[2:4]}-{raw[4:6]} "
                f"{raw[6:8]}:{raw[8:10]}:{raw[10:12]}")
    return raw


def parse_vlans(data):
    """Extract (vlan_id, name) for every VLAN record.

    Each VLAN record ends with  <01 01> <MTU:2> <VLAN id:2> <SAID:4>  and is
    preceded by a length-prefixed ASCII name. We locate the type/MTU/id marker,
    read the id, then scan backwards over the padding to recover the name."""
    vlans = []
    # marker: type 0x01, 0x01, then a 2-byte MTU, 2-byte vlan id, 4-byte SAID
    for m in re.finditer(rb"\x01\x01(..)(..)\x00\x01[\x86-\x9f].", data):
        mtu = struct.unpack(">H", m.group(1))[0]
        vid = struct.unpack(">H", m.group(2))[0]
        if not (1 <= vid <= 4094):
            continue
        name = _name_before(data, m.start())
        vlans.append((vid, name, mtu))

    # de-dup on id, keep first occurrence
    seen, uniq = set(), []
    for vid, name, mtu in vlans:
        if vid not in seen:
            seen.add(vid)
            uniq.append((vid, name, mtu))
    return uniq


def _name_before(data, marker_pos):
    """Walk back from a record marker over zero padding, then read the
    length-prefixed ASCII VLAN name that precedes it."""
    p = marker_pos - 1
    while p >= 0 and data[p] == 0x00:          # skip padding
        p -= 1
    end = p + 1
    while p >= 0 and 0x20 <= data[p] < 0x7F:   # read the printable name
        p -= 1
    name = data[p + 1:end].decode("ascii", "replace")
    return name or "<unnamed>"


# ---- output ----------------------------------------------------------------
def print_report(info, vlans, color, sort):
    C = color
    line = paint("=" * 60, Fore.CYAN, C)
    print(line)
    print(paint("  VTP configuration  (from vlan.dat)", Fore.CYAN, C, bright=True))
    print(line)

    def row(label, value, value_color=Fore.WHITE, bright=False):
        print(f"  {paint(label.ljust(22), Fore.YELLOW, C)}: "
              f"{paint(str(value), value_color, C, bright=bright)}")

    row("VTP domain name", info["domain"], Fore.GREEN, bright=True)
    row("File format version", info["version"])
    row("Configuration revision", info["revision"], Fore.MAGENTA, bright=True)
    row("Last modified", info["timestamp"])
    row("Updater ID", info["updater"])
    row("MD5 digest", info["md5_hex"])

    print(paint("-" * 60, Fore.CYAN, C))
    if info["password"] is not None:
        row("VTP password", info["password"], Fore.RED, bright=True)
        print("  " + paint("(stored as plaintext - password set without 'hidden')",
                            Fore.WHITE, C))
    elif info["md5_is_zero"]:
        row("VTP password", "not configured", Fore.WHITE)
    else:
        row("VTP password", "hidden (only MD5 digest stored)", Fore.WHITE)
        print("  " + paint("plaintext not recoverable; use the digest with "
                           "'vtp password <digest> secret'", Fore.WHITE, C))
    print(line)

    # VLAN table
    if sort == "name":
        vlans = sorted(vlans, key=lambda v: v[1].lower())
    else:
        vlans = sorted(vlans, key=lambda v: v[0])

    print()
    print(paint(f"  VLAN database  -  {len(vlans)} VLANs", Fore.CYAN, C, bright=True))
    header = f"  {'VLAN':>5}  {'MTU':>5}  Name"
    print(paint(header, Fore.YELLOW, C, bright=True))
    print(paint("  " + "-" * 56, Fore.CYAN, C))
    for vid, name, mtu in vlans:
        vid_txt = paint(f"{vid:>5}", Fore.GREEN, C, bright=True)
        name_txt = paint(name, Fore.WHITE, C)
        print(f"  {vid_txt}  {mtu:>5}  {name_txt}")
    print()


# VLANs that exist by default on every switch; skip them when generating config.
_DEFAULT_VLANS = {1, 1002, 1003, 1004, 1005}


def generate_cli(info):
    """Produce IOS config commands that rebuild this VTP/VLAN database by hand,
    so the switch can be populated without copying vlan.dat onto it."""
    lines = []
    lines.append("! --- VTP / VLAN configuration generated from vlan.dat ---")
    lines.append(f"! domain {info['domain']}  revision {info['revision']}"
                 f"  ({len(info['vlans'])} VLANs)")
    lines.append("configure terminal")
    lines.append("vtp version 1")
    # Set the mode to transparent first so the box does not adopt/propagate a
    # stale revision while VLANs are being entered, then switch to server.
    lines.append("vtp mode transparent")
    lines.append(f"vtp domain {info['domain']}")
    if info["password"] is not None:
        lines.append(f"vtp password {info['password']}")
    elif not info["md5_is_zero"]:
        digest = info["md5_hex"].replace("0x", "").replace(" ", "").lower()
        lines.append(f"vtp password {digest} secret   ! original plaintext unknown (hidden)")
    lines.append("!")
    for vid, name, _mtu in sorted(info["vlans"], key=lambda v: v[0]):
        if vid in _DEFAULT_VLANS:
            continue
        lines.append(f"vlan {vid}")
        lines.append(f" name {name}")
    lines.append("!")
    lines.append("vtp mode server")
    lines.append("end")
    lines.append("! review, then: write memory")
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Read VTP domain/password and the VLAN database from a Cisco vlan.dat file")
    ap.add_argument("file", help="path to vlan.dat")
    ap.add_argument("--no-color", action="store_true", help="disable colored output")
    ap.add_argument("--sort", choices=["id", "name"], default="id",
                    help="sort the VLAN list by id (default) or name")
    ap.add_argument("--gen-cli", action="store_true",
                    help="output IOS commands to rebuild the DB manually (no file upload needed)")
    args = ap.parse_args(argv)

    color = not args.no_color
    if color and not _HAVE_COLORAMA:
        print("note: colorama not installed - run 'pip install colorama' for color"
              " output on Windows\n", file=sys.stderr)

    try:
        data = read_file(args.file)
        info = parse_header(data)
        info["vlans"] = parse_vlans(data)
    except (OSError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if args.gen_cli:
        # Plain, paste-ready output: no color, no decoration.
        print("\n".join(generate_cli(info)))
    else:
        print_report(info, info["vlans"], color, args.sort)
    return 0


if __name__ == "__main__":
    sys.exit(main())
