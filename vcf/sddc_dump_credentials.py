#!/usr/bin/env python3
"""
sddc_dump_credentials.py
========================

Retrieve all credentials from a VMware Cloud Foundation environment via the
official REST APIs. Supports VCF 4.x, 5.x, 9.0 and 9.1.

In VCF 9.x the architecture is split:
  - SDDC Manager /v1/credentials   - classic infra: ESXi, vCenter, NSX, etc.
  - VCF Fleet Manager Locker API   - fleet components (formerly Aria):
                                       VCF Operations, VCF Automation,
                                       Identity Broker, Cloud Proxy,
                                       License Server, Operations for Logs

This script can query both. Pass --fleet-host to also pull fleet passwords.

Endpoints used (verified from Broadcom docs / KB articles):
  SDDC Manager (4.x - 9.1):
    POST   /v1/tokens                          - Create Token Pair
    PATCH  /v1/tokens/access-token/refresh     - Refresh Access Token
    DELETE /v1/tokens/refresh-token            - Invalidate Refresh Token
    GET    /v1/credentials                     - Get Credentials
    GET    /v1/sddc-managers                   - SDDC Manager info

  Fleet Manager (9.x only):
    GET    /lcm/locker/api/v3/passwords        - List passwords (v3, preferred)
    GET    /lcm/locker/api/v2/passwords        - List passwords (v2, fallback)
    POST   /lcm/locker/api/v2/passwords/{vmid}/decrypted - Decrypt one password
    (Basic Auth with admin@local + Fleet Manager root password for decryption)

Examples:
  # SDDC Manager only - 16 records typical for a small lab
  python sddc_dump_credentials.py --host sddcmanager-a.site-a.vcf.lab ^
      --username administrator@vsphere.local --insecure

  # SDDC Manager + Fleet Manager (fleet aliases only, no plaintext passwords)
  python sddc_dump_credentials.py --host sddcmanager-a.site-a.vcf.lab ^
      --username administrator@vsphere.local ^
      --fleet-host vcf-fleet.site-a.vcf.lab ^
      --insecure

  # SDDC Manager + Fleet Manager with decryption (full plaintext)
  python sddc_dump_credentials.py --host sddcmanager-a.site-a.vcf.lab ^
      --username administrator@vsphere.local ^
      --fleet-host vcf-fleet.site-a.vcf.lab ^
      --fleet-root-password "FleetMgrRootPwd!" ^
      --insecure

  # Fleet Manager only
  python sddc_dump_credentials.py --host sddcmanager-a.site-a.vcf.lab ^
      --username administrator@vsphere.local --fleet-only ^
      --fleet-host vcf-fleet.site-a.vcf.lab ^
      --fleet-root-password "FleetMgrRootPwd!" --insecure

Notes on the interactive password prompts:
  - If --password / --fleet-password / --fleet-root-password is not provided,
    the script prompts for it (when needed) using getpass().
  - On Windows 10/11 (cmd.exe, PowerShell, Windows Terminal): paste works via
    right-click. In Windows Terminal also Ctrl+Shift+V or Shift+Insert.
  - The prompt uses getpass(), so input is NOT echoed.

Security warning:
  Both APIs return passwords in plain text (Fleet only after decryption).
  Protect the output file (chmod 600, encrypted storage, never commit to git).

Dependencies:
  pip install requests colorama
"""

from __future__ import annotations

import argparse
import atexit
import csv
import datetime
import json
import logging
import os
import re
import signal
import sys
from getpass import getpass
from typing import Any

import requests
from requests.exceptions import HTTPError, RequestException
from urllib3.exceptions import InsecureRequestWarning


# ---------- colorama (optional) ----------
#
# colorama enables ANSI color codes on Windows cmd.exe and PowerShell.
# On Linux/macOS it's a no-op (ANSI works natively).
# If colorama isn't installed, we fall back to no colors.

try:
    import colorama
    from colorama import Fore, Style
    colorama.init(autoreset=True)
    _COLORAMA = True
except ImportError:
    _COLORAMA = False

    class _Dummy:
        def __getattr__(self, _name: str) -> str:
            return ""

    Fore = _Dummy()    # type: ignore[assignment]
    Style = _Dummy()   # type: ignore[assignment]


def _supports_color() -> bool:
    """Disable colors when output is piped to a file or NO_COLOR is set."""
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stderr.isatty():
        return False
    return True


_USE_COLOR = _supports_color()


# Convenience palette - all checks for _USE_COLOR happen at use site
def C(text: str, color: str = "", bold: bool = False) -> str:
    """Wrap text in ANSI color codes if colors are enabled."""
    if not (_USE_COLOR and _COLORAMA):
        return text
    prefix = color
    if bold:
        prefix += Style.BRIGHT
    return f"{prefix}{text}{Style.RESET_ALL}" if prefix else text


def print_banner() -> None:
    """Show a colored banner at script start (only in interactive mode)."""
    lines = [
        "+============================================================+",
        "|                                                            |",
        "|   VCF Credentials Dump  -  SDDC Manager + Fleet Manager    |",
        "|   Supports VCF 4.x / 5.x / 9.0 / 9.1                       |",
        "|                                                            |",
        "+============================================================+",
    ]
    for line in lines:
        sys.stderr.write(C(line, Fore.CYAN, bold=True) + "\n")
    sys.stderr.write("\n")


# ---------- interactive prompts ----------

def ask(prompt: str, default: str | None = None,
        allow_empty: bool = False) -> str:
    """Colored text prompt with optional default value."""
    suffix = f" [{C(default, Fore.YELLOW)}]" if default else ""
    full = f"{C('[?]', Fore.CYAN, bold=True)} {prompt}{suffix}: "
    while True:
        try:
            val = input(full).strip()
        except (KeyboardInterrupt, EOFError):
            sys.stderr.write("\n")
            sys.exit(130)
        if val:
            return val
        if default is not None:
            return default
        if allow_empty:
            return ""
        sys.stderr.write(C("  (value required)\n", Fore.RED))


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    """Colored yes/no prompt."""
    hint = "Y/n" if default else "y/N"
    full = f"{C('[?]', Fore.CYAN, bold=True)} {prompt} [{C(hint, Fore.YELLOW)}]: "
    while True:
        try:
            val = input(full).strip().lower()
        except (KeyboardInterrupt, EOFError):
            sys.stderr.write("\n")
            sys.exit(130)
        if not val:
            return default
        if val in ("y", "yes", "ano", "a"):
            return True
        if val in ("n", "no", "ne"):
            return False
        sys.stderr.write(C("  (please answer y or n)\n", Fore.RED))


def ask_choice(prompt: str, choices: list[tuple[str, str]],
               default: int = 0) -> str:
    """
    Colored numbered-choice prompt.
    choices: list of (key, description) tuples.
    Returns the chosen key.
    """
    sys.stderr.write(f"{C('[?]', Fore.CYAN, bold=True)} {prompt}\n")
    for i, (_key, desc) in enumerate(choices, 1):
        marker = C(f"  {i})", Fore.YELLOW)
        sys.stderr.write(f"{marker} {desc}\n")
    while True:
        try:
            val = input(f"    Choose [{C(str(default + 1), Fore.YELLOW)}]: ").strip()
        except (KeyboardInterrupt, EOFError):
            sys.stderr.write("\n")
            sys.exit(130)
        if not val:
            return choices[default][0]
        if val.isdigit() and 1 <= int(val) <= len(choices):
            return choices[int(val) - 1][0]
        sys.stderr.write(C(f"  (enter 1-{len(choices)})\n", Fore.RED))


def ask_password(prompt: str) -> str:
    """Hidden password prompt (paste works via right-click / Shift+Insert)."""
    full = (f"{C('[?]', Fore.CYAN, bold=True)} {prompt} "
            f"{C('(paste: right-click / Shift+Insert, hidden input)', Style.DIM)}: ")
    try:
        return getpass(full)
    except (KeyboardInterrupt, EOFError):
        sys.stderr.write("\n")
        sys.exit(130)


def info(msg: str) -> None:
    """Print an informational status line (not via logger)."""
    sys.stderr.write(f"{C('[*]', Fore.BLUE, bold=True)} {msg}\n")


def ok(msg: str) -> None:
    """Print a success status line."""
    sys.stderr.write(f"{C('[+]', Fore.GREEN, bold=True)} {msg}\n")


def warn(msg: str) -> None:
    """Print a warning status line."""
    sys.stderr.write(f"{C('[!]', Fore.YELLOW, bold=True)} {msg}\n")


# ---------- colored help formatter ----------

class ColoredHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """argparse formatter that colorizes section headers, flags, and metavars."""

    def start_section(self, heading: str | None) -> None:  # type: ignore[override]
        if heading:
            heading = C(heading.upper(), Fore.CYAN, bold=True)
        super().start_section(heading)

    def _format_action_invocation(self, action: argparse.Action) -> str:
        text = super()._format_action_invocation(action)
        if not (_USE_COLOR and _COLORAMA):
            return text
        # Colorize -x/--xxx flags
        parts = []
        for token in text.split(", "):
            if token.startswith("-"):
                # Separate flag from its METAVAR
                head, sep, tail = token.partition(" ")
                colored = C(head, Fore.GREEN, bold=True)
                if sep:
                    colored += " " + C(tail, Fore.MAGENTA)
                parts.append(colored)
            else:
                parts.append(token)
        return ", ".join(parts)


class ColorFormatter(logging.Formatter):
    """Color-coded log formatter for the terminal."""

    LEVEL_COLORS = {
        logging.DEBUG:    Fore.CYAN if _COLORAMA else "",
        logging.INFO:     Fore.GREEN if _COLORAMA else "",
        logging.WARNING:  Fore.YELLOW if _COLORAMA else "",
        logging.ERROR:    Fore.RED if _COLORAMA else "",
        logging.CRITICAL: (Fore.RED + Style.BRIGHT) if _COLORAMA else "",
    }


    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%H:%M:%S")
        level = record.levelname
        msg = record.getMessage()
        if _USE_COLOR and _COLORAMA:
            color = self.LEVEL_COLORS.get(record.levelno, "")
            reset = Style.RESET_ALL
            dim = Style.DIM
            return f"{dim}{ts}{reset} {color}[{level:<7}]{reset} {msg}"
        return f"{ts} [{level:<7}] {msg}"


log = logging.getLogger("sddc")


# ---------- VCF capability matrix ----------
#
# Keys: "4", "5", "9.0", "9.1"
# Auto-detection returns one of these based on /v1/sddc-managers.

VCF_CAPS: dict[str, dict[str, bool]] = {
    "4": {
        "api_key_auth":        False,  # apiKey support was added in 4.4 - conservative default
        "id_token_auth":       False,
        "account_type_filter": False,
        "auto_rotate_policy":  False,
        "connectivity_status": False,
        "resource_name_filter": True,
        "domain_name_filter":  True,
        "resource_type_filter": True,
    },
    "5": {
        "api_key_auth":        True,
        "id_token_auth":       False,
        "account_type_filter": True,
        "auto_rotate_policy":  True,
        "connectivity_status": True,
        "resource_name_filter": True,
        "domain_name_filter":  True,
        "resource_type_filter": True,
    },
    "9.0": {
        "api_key_auth":        True,
        "id_token_auth":       True,
        "account_type_filter": True,
        "auto_rotate_policy":  True,
        "connectivity_status": True,
        "resource_name_filter": True,
        "domain_name_filter":  True,
        "resource_type_filter": True,
    },
    "9.1": {
        "api_key_auth":        True,
        "id_token_auth":       True,
        "account_type_filter": True,
        "auto_rotate_policy":  True,
        "connectivity_status": True,
        "resource_name_filter": True,
        "domain_name_filter":  True,
        "resource_type_filter": True,
    },
}


def normalize_vcf(version: str) -> str:
    """'5.2.1' -> '5', '9.1.0' -> '9.1', '4.5' -> '4'. Returns VCF_CAPS key."""
    v = version.strip().lower().lstrip("v")
    if v.startswith("9.1"):
        return "9.1"
    if v.startswith("9.0") or v == "9":
        return "9.0"
    if v.startswith("9"):
        return "9.1"  # unknown 9.x defaults to 9.1
    if v.startswith("5"):
        return "5"
    if v.startswith("4"):
        return "4"
    raise ValueError(f"Unsupported VCF version: {version}")


class FleetClient:
    """
    VCF Fleet Manager Locker API client (VCF 9.x only).

    Auth: HTTP Basic Auth with admin@local (or another fleet admin user).
    Decryption: Each plaintext password requires a POST with the Fleet Manager
    VM's root password in the JSON body. Without root password, only the
    encrypted aliases / metadata can be retrieved.

    The Locker API path moved between VCF Operations Fleet Manager versions:
      - VCF 9.0+ (current):  /lcm/locker/api/passwords            (unversioned)
                             /lcm/locker/api/passwords/view/{vmid}
      - VCF 9.0.1+:          /lcm/locker/api/v3/passwords
                             /lcm/locker/api/v3/passwords/{vmid}/decrypted
      - VCF 9.0 / vRSLCM:    /lcm/locker/api/v2/passwords
                             /lcm/locker/api/v2/passwords/{vmid}/decrypted

    We probe each prefix in order ("", "v3", "v2") and use whichever returns
    a 200. Decryption follows the matching version style.
    """

    # (path_prefix, list_url_template, decrypt_url_template) tuples to try,
    # in order of preference. {base} is filled in by _list_url().
    #
    # IMPORTANT - VCF 9.1 architectural change:
    #   The Fleet Management appliance (which hosted the Locker) is DEPRECATED.
    #   Per Broadcom docs: "VCF Operations does not store a copy of an account
    #   password in a local vault" (9.1+). The Locker API below works only on
    #   VCF 9.0 deployments that still have the Fleet Management appliance.
    #
    #   In VCF 9.1, fleet component passwords (VCF Operations, VCF Automation,
    #   Identity Broker, etc.) are NOT centrally stored in plaintext anywhere.
    #   They were either captured at deployment time from the VCF Installer UI,
    #   or must be reset via the Remediate Password workflow.
    #
    #   We still try the VCF Operations password management endpoint as a
    #   metadata-only fallback - it lists managed accounts but does not return
    #   plaintext passwords.

    API_VARIANTS = [
        # VCF 9.0 Fleet Management Locker (the appliance is decommissioned in 9.1)
        ("locker-new",   "/lcm/locker/api/passwords",        "/lcm/locker/api/passwords/view/{vmid}"),
        ("locker-v3",    "/lcm/locker/api/v3/passwords",     "/lcm/locker/api/v3/passwords/{vmid}/decrypted"),
        ("locker-v2",    "/lcm/locker/api/v2/passwords",     "/lcm/locker/api/v2/passwords/{vmid}/decrypted"),
        ("locker-alt",   "/locker/api/passwords",            "/locker/api/passwords/view/{vmid}"),
        ("locker-v1",    "/lcm/locker/api/v1/passwords",     "/lcm/locker/api/v1/passwords/{vmid}/decrypted"),
        # VCF 9.1 VCF Operations password management API (METADATA ONLY - no plaintext)
        ("vcfops-9.1",   "/suite-api/api/fleet-management/password-management/accounts/query", ""),
    ]

    # Identity probes - hit these to figure out what appliance answered.
    # (path, expected_substring_in_body_or_header, friendly_name)
    APPLIANCE_PROBES = [
        ("/api/swagger-ui/index.html",      "swagger", "VCF Ops Fleet Manager (Swagger UI)"),
        ("/lcm/",                            "lcm",     "vRSLCM / Fleet Locker root (VCF 9.0)"),
        ("/suite-api/api/versions",          "version", "VCF Operations (suite-api) - VCF 9.1+"),
        ("/ui/",                             "vco",     "VCF Operations UI"),
        ("/csp/gateway/am/api/auth/api-tokens/details", "csp", "Identity Broker / CSP"),
        ("/automation-ui/",                  "automation", "VCF Automation"),
    ]

    def __init__(self, host: str, username: str, password: str,
                 root_password: str | None = None,
                 verify_tls: bool = True, timeout: int = 60,
                 list_path_override: str | None = None,
                 decrypt_path_override: str | None = None):
        self.base = f"https://{host}"
        self.auth = (username, password)
        self.root_password = root_password
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.list_url = list_path_override or ""
        self.decrypt_url = decrypt_path_override or ""
        self.variant_label = "override" if list_path_override else ""
        self.is_metadata_only = False  # True for VCF 9.1 vcfops endpoint
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def _probe_appliance_type(self) -> None:
        """Best-effort identification of what kind of appliance we're talking to."""
        log.info("Probing %s to identify appliance type ...", self.base)
        found_any = False
        for path, marker, name in self.APPLIANCE_PROBES:
            try:
                r = self.session.get(
                    f"{self.base}{path}",
                    auth=self.auth,
                    verify=self.verify_tls,
                    timeout=10,
                    allow_redirects=False,
                )
                if r.status_code in (200, 301, 302):
                    log.info("  [%s] %s -> HTTP %s : likely %s",
                             "+", path, r.status_code, name)
                    found_any = True
                else:
                    log.debug("  [-] %s -> HTTP %s", path, r.status_code)
            except RequestException as e:
                log.debug("  [-] %s -> %s", path, e)
        if not found_any:
            log.warning("  No known appliance signatures matched. Check the host "
                        "manually in a browser: https://%s",
                        self.base.replace("https://", ""))

    def _detect_api_version(self) -> bool:
        """
        Probe known list endpoints to find the one this Fleet Manager exposes.
        If overrides were supplied at construction time, use them directly.
        Returns True on success, False if all variants 404'd.
        """
        if self.list_url and self.decrypt_url:
            log.info("Using override Fleet API paths: list=%s, decrypt=%s",
                     self.list_url, self.decrypt_url)
            return True

        for prefix, list_tpl, decrypt_tpl in self.API_VARIANTS:
            url = f"{self.base}{list_tpl}"
            label = prefix or "<unversioned>"
            try:
                r = self.session.get(
                    url,
                    auth=self.auth,
                    params={"limit": 1},
                    verify=self.verify_tls,
                    timeout=self.timeout,
                )
                if r.status_code == 401:
                    raise RuntimeError(
                        "Fleet Manager authentication failed (401). "
                        "Check --fleet-username / --fleet-password.")
                if r.status_code in (200, 204):
                    self.list_url = list_tpl
                    self.decrypt_url = decrypt_tpl
                    self.variant_label = prefix
                    self.is_metadata_only = (prefix == "vcfops-9.1")
                    log.info("Fleet API path detected: %s (variant: %s)",
                             url, prefix)
                    if self.is_metadata_only:
                        log.warning(
                            "Detected VCF Operations password management API "
                            "(VCF 9.1+). This endpoint returns ONLY metadata "
                            "(account names, FQDNs, status) - plaintext "
                            "passwords are NOT available because VCF 9.1 by "
                            "design does not store them centrally.")
                    return True
                log.debug("Fleet API %s -> %s, trying next variant", label,
                          r.status_code)
            except RequestException as e:
                log.debug("Fleet API %s probe failed: %s", label, e)

        # Probe appliance type to give the user a hint
        log.error("None of the known Fleet/Locker API paths responded. "
                  "Tried %d variants.", len(self.API_VARIANTS))
        self._probe_appliance_type()
        log.error("VCF 9.1 architectural note:")
        log.error("  Starting with VCF 9.1, the standalone Fleet Management "
                  "appliance is DEPRECATED and powered down after upgrade.")
        log.error("  Per Broadcom docs, VCF 9.1 by design does NOT store "
                  "fleet component passwords (VCF Operations, VCF Automation, "
                  "Identity Broker, etc.) in any central vault.")
        log.error("  Plaintext fleet passwords are only available:")
        log.error("    - Downloaded ONCE from the VCF Installer UI right "
                  "after deployment.")
        log.error("    - From CyberArk vault (if integration configured).")
        log.error("    - Via reset/remediate workflow (sets a new password).")
        log.error("  For VCF 9.0 environments only, look for the Fleet "
                  "Management appliance (often named 'flt-fm*' or 'vcf-fleet'). "
                  "If your VCF was upgraded to 9.1, that VM is now powered off.")
        log.error("If you find the right appliance and a Swagger UI at "
                  "https://<host>/api/swagger-ui/index.html shows a Locker "
                  "Password Controller, use --fleet-list-path / "
                  "--fleet-decrypt-path to override.")
        return False

    def list_passwords(self, page_size: int = 100,
                       alias_query: str | None = None) -> list[dict]:
        """GET passwords with pagination, using the detected API path."""
        if not self.list_url:
            if not self._detect_api_version():
                return []

        url = f"{self.base}{self.list_url}"
        all_pwds: list[dict] = []
        page = 0
        while True:
            params: dict[str, Any] = {
                "limit": page_size,
                "from": page * page_size,
            }
            if alias_query:
                params["aliasQuery"] = alias_query

            r = self.session.get(
                url,
                auth=self.auth,
                params=params,
                verify=self.verify_tls,
                timeout=self.timeout,
            )
            r.raise_for_status()
            body = r.json()

            # Response shape varies slightly between API versions:
            #   - {"passwords": [...], "total": N}
            #   - {"content": [...], "totalElements": N}
            #   - just a JSON array
            if isinstance(body, dict):
                pwds = body.get("passwords") or body.get("content") or []
                total = (body.get("total")
                         or body.get("totalElements")
                         or len(all_pwds) + len(pwds))
            elif isinstance(body, list):
                pwds = body
                total = len(pwds)
            else:
                pwds, total = [], 0

            all_pwds.extend(pwds)
            log.info("  fleet page %d, items: %d (total so far: %d / %d)",
                     page + 1, len(pwds), len(all_pwds), total)

            if not pwds or len(all_pwds) >= total:
                break
            page += 1
        return all_pwds

    def decrypt_password(self, vmid: str) -> str | None:
        """Decrypt a single password using the detected POST endpoint."""
        if not self.root_password or not self.decrypt_url:
            return None
        url = self.base + self.decrypt_url.format(vmid=vmid)
        try:
            r = self.session.post(
                url,
                auth=self.auth,
                json={"rootPassword": self.root_password},
                verify=self.verify_tls,
                timeout=self.timeout,
            )
            r.raise_for_status()
            body = r.json()
            return (body.get("password")
                    or body.get("decryptedPassword")
                    or body.get("value"))
        except RequestException as e:
            log.warning("Decryption failed for vmid=%s: %s", vmid[:8], e)
            return None

    def get_all_credentials(self, page_size: int = 100,
                            decrypt: bool = True) -> list[dict]:
        """Fetch all fleet passwords and optionally decrypt each one."""
        pwds = self.list_passwords(page_size=page_size)
        if not pwds:
            return []

        if self.is_metadata_only:
            log.info("Metadata-only API (VCF 9.1) - no plaintext passwords "
                     "available, marking all entries as such.")
            for p in pwds:
                p["plaintextPassword"] = None
                p["_vcf_note"] = ("VCF 9.1 does not store plaintext fleet "
                                  "passwords centrally; this is metadata only.")
            return pwds

        if not decrypt or not self.root_password:
            if not self.root_password:
                log.warning("No --fleet-root-password given; fleet plaintext "
                            "passwords will NOT be decrypted, only metadata "
                            "(aliases, vmid, references) will be saved.")
            for p in pwds:
                p["plaintextPassword"] = None
            return pwds

        log.info("Decrypting %d fleet passwords ...", len(pwds))
        for i, p in enumerate(pwds, 1):
            vmid = p.get("vmid") or p.get("id")
            if not vmid:
                p["plaintextPassword"] = None
                continue
            p["plaintextPassword"] = self.decrypt_password(vmid)
            if i % 10 == 0 or i == len(pwds):
                log.info("  decrypted %d/%d", i, len(pwds))
        return pwds


class SDDCClient:
    """SDDC Manager REST API client, VCF-version aware."""

    def __init__(self, host: str, vcf_version: str = "9.1",
                 verify_tls: bool = True, timeout: int = 60):
        self.base = f"https://{host}"
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.vcf_version = vcf_version
        self.caps = VCF_CAPS[vcf_version]

        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        self.access_token: str | None = None
        self.refresh_token_id: str | None = None
        self._auth_spec: dict[str, str] | None = None

    # ---------- version auto-detection ----------

    @staticmethod
    def detect_version(host: str, verify_tls: bool = True, timeout: int = 30) -> str:
        """
        Try to detect the VCF version via /v1/sddc-managers (unauthenticated
        access is usually denied, so this may not always succeed). Falls back
        to "9.1" if detection fails.

        Returns a VCF_CAPS key: "4", "5", "9.0" or "9.1".
        """
        url = f"https://{host}/v1/sddc-managers"
        try:
            r = requests.get(url, verify=verify_tls, timeout=timeout)
            if r.status_code == 200:
                data = r.json()
                els = data.get("elements") or []
                if els and "version" in els[0]:
                    return normalize_vcf(els[0]["version"])
        except RequestException as e:
            log.debug("Pre-login detection via /v1/sddc-managers failed: %s", e)

        # Fallback: server header sometimes contains a version banner
        try:
            r = requests.get(f"https://{host}/v1/tokens", verify=verify_tls,
                             timeout=timeout)
            server = r.headers.get("X-VCF-Version") or r.headers.get("Server", "")
            m = re.search(r"\b(\d+\.\d+)", server)
            if m:
                return normalize_vcf(m.group(1))
        except RequestException:
            pass

        log.warning("Could not detect SDDC Manager version, defaulting to 9.1.")
        return "9.1"

    def detect_version_post_login(self) -> str | None:
        """After login we can reliably call /v1/sddc-managers."""
        try:
            r = self._authed_request("GET", "/v1/sddc-managers", _retry=True)
            data = r.json()
            els = data.get("elements") or []
            if els:
                version = els[0].get("version")
                if version:
                    return version
        except (RequestException, RuntimeError) as e:
            log.debug("Post-login version detection failed: %s", e)
        return None

    def discover_fleet_fqdns(self) -> dict[str, list[str] | str | None]:
        """
        Query SDDC Manager for FQDNs of the deployed fleet/VCF Management
        components.

        Sources tried, in order:
          1) /v1/vsp-clusters (VCF 9.1: VCF Services Platform / VCFMS)
             - returns platformFqdn / instanceFqdn / fleetFqdn of the new
               containerized Management Services Runtime
          2) /v1/vcf-management-components/tasks/<id>/spec (VCF 9.0)
             - returns hostnames of standalone appliances (Fleet Management,
               VCF Operations, VCF Automation, etc.)

        Both sources are tried and merged.

        Returns a dict with potentially these keys (any may be None / []):
            vcfms_platform_fqdn:   VCFMS Platform (VCF 9.1)
            vcfms_instance_fqdn:   VCFMS Instance (VCF 9.1)
            vcfms_fleet_fqdn:      VCFMS Fleet (VCF 9.1) - container, no Locker
            vcf_operations:        list of FQDNs (master/replica/data nodes)
            vcf_operations_lb:     load balancer FQDN (if HA)
            fleet_management:      Fleet Management appliance FQDN (9.0 only)
            vcf_operations_collector: Cloud Proxy FQDN
            vcf_automation:        VCF Automation FQDN
            vcf_identity_broker:   Identity Broker FQDN (9.0)
            license_server:        License Server FQDN
            best_fleet_guess:      our single best guess for --fleet-host
        """
        result: dict[str, Any] = {
            "vcfms_platform_fqdn": None,
            "vcfms_instance_fqdn": None,
            "vcfms_fleet_fqdn": None,
            "vcf_operations": [],
            "vcf_operations_lb": None,
            "fleet_management": None,
            "vcf_operations_collector": None,
            "vcf_automation": None,
            "vcf_identity_broker": None,
            "license_server": None,
            "best_fleet_guess": None,
        }

        # --- Source 1: VCF 9.1 VSP clusters (containerized VCFMS) ---
        vsp = self._fetch_vsp_clusters()
        if vsp:
            # The result can be either a list of cluster objects or a paged
            # {"elements": [...]} - handle both
            clusters = vsp.get("elements") if isinstance(vsp, dict) else vsp
            if isinstance(clusters, list) and clusters:
                # Take the first cluster (most environments have only one)
                c = clusters[0]
                # In some builds the spec is nested under "spec" or inline
                spec = c.get("spec") or c
                result["vcfms_platform_fqdn"] = spec.get("platformFqdn")
                result["vcfms_instance_fqdn"] = spec.get("instanceFqdn")
                result["vcfms_fleet_fqdn"] = spec.get("fleetFqdn")
                log.debug("VSP cluster discovered: platform=%s instance=%s fleet=%s",
                          result["vcfms_platform_fqdn"],
                          result["vcfms_instance_fqdn"],
                          result["vcfms_fleet_fqdn"])

        # --- Source 2: VCF 9.0 vcf-management-components/tasks ---
        spec = self._fetch_vcf_management_spec()
        if spec:
            ops_spec = spec.get("vcfOperationsSpec") or {}
            nodes = ops_spec.get("nodes") or []
            result["vcf_operations"] = [n.get("hostname") for n in nodes
                                        if n.get("hostname")]
            result["vcf_operations_lb"] = ops_spec.get("loadBalancerFqdn")

            flt_spec = spec.get("vcfOperationsFleetManagementSpec") or {}
            result["fleet_management"] = flt_spec.get("hostname")

            col_spec = spec.get("vcfOperationsCollectorSpec") or {}
            result["vcf_operations_collector"] = col_spec.get("hostname")

            auto_spec = spec.get("vcfAutomationSpec") or {}
            result["vcf_automation"] = auto_spec.get("hostname")

            idb_spec = spec.get("vcfIdentityBrokerSpec") or {}
            result["vcf_identity_broker"] = idb_spec.get("hostname")

            ls_spec = (spec.get("vcfLicenseServerSpec")
                       or spec.get("licenseServerSpec") or {})
            result["license_server"] = ls_spec.get("hostname")

        # Best guess for --fleet-host (the script will try Locker on it):
        #   1) VCF 9.0 Fleet Management appliance (has Locker)
        #   2) VCF 9.1 VCFMS fleet container (no Locker but at least matches
        #      what the user expects to see)
        #   3) VCF Operations LB (for 9.1 metadata-only fallback)
        #   4) VCF Operations master node
        if result["fleet_management"]:
            result["best_fleet_guess"] = result["fleet_management"]
        elif result["vcfms_fleet_fqdn"]:
            result["best_fleet_guess"] = result["vcfms_fleet_fqdn"]
        elif result["vcf_operations_lb"]:
            result["best_fleet_guess"] = result["vcf_operations_lb"]
        elif result["vcf_operations"]:
            result["best_fleet_guess"] = result["vcf_operations"][0]

        return result

    def _fetch_vsp_clusters(self) -> Any:
        """GET /v1/vsp-clusters - VCF 9.1 VCFMS containerized cluster info."""
        try:
            r = self._authed_request("GET", "/v1/vsp-clusters")
            return r.json()
        except (RequestException, RuntimeError) as e:
            log.debug("Fetching /v1/vsp-clusters failed: %s", e)
            return None

    def _fetch_vcf_management_spec(self) -> dict | None:
        """
        Find a /v1/vcf-management-components task and fetch its 'spec',
        which contains FQDNs for all fleet components.

        Strategy: pick the latest task we have access to, GET its /spec.
        Returns the spec dict, or None if nothing usable found.
        """
        # Try a few candidate endpoints - the tasks list path varies slightly
        # between SDDC Manager builds.
        task_list_paths = [
            "/v1/vcf-management-components/tasks",
            "/v1/vcf-management-components",
        ]

        latest_task_id: str | None = None
        for path in task_list_paths:
            try:
                r = self._authed_request("GET", path)
                body = r.json()
                if isinstance(body, list):
                    tasks = body
                else:
                    tasks = body.get("elements") or []
                if not tasks:
                    continue
                # Pick the most recent task by sort or creation timestamp
                tasks_sorted = sorted(
                    tasks,
                    key=lambda t: t.get("creationTimestamp")
                                  or t.get("modificationTimestamp")
                                  or "",
                    reverse=True,
                )
                for t in tasks_sorted:
                    tid = t.get("id") or t.get("taskId")
                    if tid:
                        latest_task_id = tid
                        log.debug("Found vcf-management-components task: %s "
                                  "(name=%s, status=%s)", tid,
                                  t.get("name"), t.get("status"))
                        break
                if latest_task_id:
                    break
            except (RequestException, RuntimeError) as e:
                log.debug("Listing %s failed: %s", path, e)

        if not latest_task_id:
            log.debug("No vcf-management-components task found.")
            return None

        spec_path = f"/v1/vcf-management-components/tasks/{latest_task_id}/spec"
        try:
            r = self._authed_request("GET", spec_path)
            return r.json()
        except (RequestException, RuntimeError) as e:
            log.debug("Fetching %s failed: %s", spec_path, e)
            return None

    # ---------- authentication ----------

    def login(
        self,
        username: str | None = None,
        password: str | None = None,
        api_key: str | None = None,
        id_token: str | None = None,
    ) -> None:
        """POST /v1/tokens. Builds payload according to capability matrix."""
        spec: dict[str, str] = {}
        if username and password:
            spec["username"] = username
            spec["password"] = password
        if api_key:
            if not self.caps["api_key_auth"]:
                log.warning("VCF %s may not support apiKey auth (added in 4.4+). "
                            "Sending anyway, but it may fail.", self.vcf_version)
            spec["apiKey"] = api_key
        if id_token:
            if not self.caps["id_token_auth"]:
                log.warning("VCF %s does not support idToken auth (9.x only). "
                            "Sending anyway, but expect failure.", self.vcf_version)
            spec["idToken"] = id_token

        if not spec:
            raise ValueError("Provide username+password, apiKey, or idToken")

        log.info("Logging in to %s (VCF %s) ...", self.base, self.vcf_version)
        self._auth_spec = spec

        r = self.session.post(
            f"{self.base}/v1/tokens",
            json=spec,
            verify=self.verify_tls,
            timeout=self.timeout,
        )
        if r.status_code not in (200, 201):
            r.raise_for_status()

        body = r.json()
        self.access_token = body.get("accessToken")
        rt = body.get("refreshToken") or {}
        self.refresh_token_id = rt.get("id")

        if not self.access_token:
            raise RuntimeError(f"Response from /v1/tokens has no accessToken: {body}")

        rt_short = (self.refresh_token_id or "<none>")[:8]
        log.info("Logged in. Refresh token id: %s...", rt_short)

    def refresh_access_token(self) -> bool:
        """PATCH /v1/tokens/access-token/refresh"""
        if not self.refresh_token_id:
            return False
        try:
            log.info("Refreshing access token ...")
            r = self.session.patch(
                f"{self.base}/v1/tokens/access-token/refresh",
                data=json.dumps(self.refresh_token_id),
                verify=self.verify_tls,
                timeout=self.timeout,
            )
            r.raise_for_status()
            try:
                new_token = r.json()
            except ValueError:
                new_token = r.text.strip().strip('"')
            if isinstance(new_token, dict):
                new_token = new_token.get("accessToken") or new_token.get("token")
            if not isinstance(new_token, str) or not new_token:
                log.warning("Unexpected response from refresh endpoint: %r",
                            r.text[:200])
                return False
            self.access_token = new_token
            log.info("Access token refreshed.")
            return True
        except RequestException as e:
            log.warning("Access token refresh failed: %s", e)
            return False

    def _reauth_with_credentials(self) -> bool:
        if not self._auth_spec:
            return False
        try:
            log.info("Refresh failed, attempting re-login ...")
            self.login(
                username=self._auth_spec.get("username"),
                password=self._auth_spec.get("password"),
                api_key=self._auth_spec.get("apiKey"),
                id_token=self._auth_spec.get("idToken"),
            )
            return True
        except (RequestException, RuntimeError) as e:
            log.error("Re-login failed: %s", e)
            return False

    def logout(self) -> None:
        """DELETE /v1/tokens/refresh-token (cleanup, never raises)."""
        if not self.refresh_token_id or not self.access_token:
            return
        try:
            log.info("Invalidating refresh token ...")
            r = self.session.delete(
                f"{self.base}/v1/tokens/refresh-token",
                headers={"Authorization": f"Bearer {self.access_token}"},
                data=json.dumps(self.refresh_token_id),
                verify=self.verify_tls,
                timeout=self.timeout,
            )
            if r.status_code == 204:
                log.info("Refresh token invalidated.")
            else:
                log.warning("Invalidation returned %s: %s",
                            r.status_code, r.text[:200])
        except RequestException as e:
            log.warning("Refresh token invalidation failed: %s", e)
        finally:
            self.refresh_token_id = None
            self.access_token = None

    # ---------- HTTP helper ----------

    def _authed_request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: Any = None,
        _retry: bool = True,
    ) -> requests.Response:
        if not self.access_token:
            raise RuntimeError("Not logged in - call login() first.")
        headers = {"Authorization": f"Bearer {self.access_token}"}
        r = self.session.request(
            method,
            f"{self.base}{path}",
            headers=headers,
            params=params,
            json=json_body,
            verify=self.verify_tls,
            timeout=self.timeout,
        )
        if r.status_code == 401 and _retry:
            log.info("Got HTTP 401, attempting token refresh / re-login ...")
            if self.refresh_access_token() or self._reauth_with_credentials():
                return self._authed_request(
                    method, path, params=params, json_body=json_body, _retry=False
                )
        r.raise_for_status()
        return r

    # ---------- credentials ----------

    def get_credentials(
        self,
        resource_type: str | None = None,
        resource_name: str | None = None,
        domain_name: str | None = None,
        account_type: str | None = None,
        page_size: int = 200,
    ) -> list[dict]:
        """GET /v1/credentials with pagination and version-aware filters."""
        # Drop filters not supported by this VCF version (would cause HTTP 400)
        if account_type and not self.caps["account_type_filter"]:
            log.warning("VCF %s does not support accountType filter, ignoring.",
                        self.vcf_version)
            account_type = None
        if resource_name and not self.caps["resource_name_filter"]:
            log.warning("VCF %s does not support resourceName filter, ignoring.",
                        self.vcf_version)
            resource_name = None
        if domain_name and not self.caps["domain_name_filter"]:
            log.warning("VCF %s does not support domainName filter, ignoring.",
                        self.vcf_version)
            domain_name = None
        if resource_type and not self.caps["resource_type_filter"]:
            log.warning("VCF %s does not support resourceType filter, ignoring.",
                        self.vcf_version)
            resource_type = None

        all_creds: list[dict] = []
        page = 0
        while True:
            params: dict[str, Any] = {"pageNumber": page, "pageSize": page_size}
            if resource_type:
                params["resourceType"] = resource_type
            if resource_name:
                params["resourceName"] = resource_name
            if domain_name:
                params["domainName"] = domain_name
            if account_type:
                params["accountType"] = account_type

            r = self._authed_request("GET", "/v1/credentials", params=params)
            body = r.json()
            elements = body.get("elements") or []
            all_creds.extend(elements)

            meta = body.get("pageMetadata") or {}
            total_pages = meta.get("totalPages", 1)
            log.info("  page %d/%d, items: %d (total so far: %d)",
                     page + 1, total_pages, len(elements), len(all_creds))

            if not elements or page + 1 >= total_pages:
                break
            page += 1
        return all_creds

    def get_roles(self) -> dict[str, dict]:
        """GET /v1/roles - returns {roleId: roleObject} for name lookup."""
        try:
            r = self._authed_request("GET", "/v1/roles")
            elements = r.json().get("elements") or []
            return {role.get("id"): role for role in elements if role.get("id")}
        except Exception as e:
            log.warning("Could not fetch /v1/roles: %s", e)
            return {}

    def get_users(self) -> list[dict]:
        """
        GET /v1/users - list all users/groups/service accounts and their roles.

        This is what you need to troubleshoot PERMISSION_NOT_FOUND / 401 on
        /v1/tokens: an account can authenticate but still be rejected if it has
        no role mapping in SDDC Manager. Each returned record is enriched with
        the resolved role name (ADMIN / OPERATOR / VIEWER) so you can
        immediately spot who is missing a role.
        """
        roles = self.get_roles()
        if roles:
            log.info("Fetched %d role definition(s): %s", len(roles),
                     ", ".join(sorted(r.get("name", "?")
                                      for r in roles.values())))

        all_users: list[dict] = []
        page = 0
        while True:
            params: dict[str, Any] = {"pageNumber": page, "pageSize": 200}
            r = self._authed_request("GET", "/v1/users", params=params)
            body = r.json()
            elements = body.get("elements") or []

            # Enrich each record with a resolved, human-readable role name.
            for u in elements:
                role_ref = u.get("role") or {}
                role_id = role_ref.get("id")
                # Some versions already inline the role name; keep it if present
                resolved = role_ref.get("name")
                if not resolved and role_id and role_id in roles:
                    resolved = roles[role_id].get("name")
                u["resolvedRoleName"] = resolved or "(no role)"

            all_users.extend(elements)

            meta = body.get("pageMetadata") or {}
            total_pages = meta.get("totalPages", 1)
            log.info("  page %d/%d, users: %d (total so far: %d)",
                     page + 1, total_pages, len(elements), len(all_users))

            if not elements or page + 1 >= total_pages:
                break
            page += 1
        return all_users


def print_users_table(users: list[dict]) -> None:
    """Pretty-print users to stderr so it's visible even when JSON is piped."""
    if not users:
        sys.stderr.write(C("No users returned.\n", Fore.YELLOW))
        return

    rows = []
    for u in users:
        name = u.get("name", "?")
        domain = u.get("domain", "")
        utype = u.get("type", "")          # USER / GROUP / SERVICE
        role = u.get("resolvedRoleName", "(no role)")
        rows.append((name, domain, utype, role))

    w_name = max(len("NAME"), *(len(r[0]) for r in rows))
    w_dom = max(len("DOMAIN"), *(len(r[1]) for r in rows))
    w_type = max(len("TYPE"), *(len(r[2]) for r in rows))

    header = (f"{'NAME':<{w_name}}  {'DOMAIN':<{w_dom}}  "
              f"{'TYPE':<{w_type}}  ROLE")
    sys.stderr.write(C(header + "\n", Fore.CYAN, bold=True))
    sys.stderr.write("-" * len(header) + "\n")
    for name, domain, utype, role in rows:
        line = (f"{name:<{w_name}}  {domain:<{w_dom}}  "
                f"{utype:<{w_type}}  {role}")
        # Highlight accounts with no role - these cause PERMISSION_NOT_FOUND
        if role == "(no role)":
            sys.stderr.write(C(line + "   <-- NO ROLE\n", Fore.RED, bold=True))
        else:
            sys.stderr.write(line + "\n")
    sys.stderr.write("\n")


# ---------- output helpers ----------

def default_output_path(host: str, fmt: str = "json") -> str:
    """
    Build a default output filename in the current working directory
    (the directory from which the script was launched), in the form:
        credentials_<host>_<YYYY-MM-DD_HH-MM-SS>.<ext>

    The host part is sanitized so it never contains characters that are
    illegal on Windows (\\ / : * ? " < > |) or unfriendly on Unix.
    """
    safe_host = re.sub(r"[^A-Za-z0-9._-]+", "_", host).strip("_") or "sddc"
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    ext = fmt.lstrip(".").lower() or "json"
    filename = f"credentials_{safe_host}_{ts}.{ext}"
    return os.path.join(os.getcwd(), filename)


def mask_passwords(creds: list[dict]) -> list[dict]:
    """Mask both SDDC ('password') and Fleet ('plaintextPassword') fields."""
    out = []
    for c in creds:
        c2 = dict(c)
        for field in ("password", "plaintextPassword"):
            if c2.get(field):
                pwd = c2[field]
                c2[field] = (
                    pwd[:2] + "*" * (len(pwd) - 2) if len(pwd) > 2 else "***"
                )
        out.append(c2)
    return out


def save_json(data: dict | list, path: str) -> None:
    """Save either a list (SDDC-only) or a dict with sddc/fleet keys."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_csv(data: dict | list, path: str, caps: dict[str, bool]) -> None:
    """
    Flat CSV. Adds a 'source' column to distinguish SDDC vs Fleet entries.
    Columns specific to one source stay in the header but are empty for the
    other, so the header is consistent.
    """
    fieldnames = [
        "source",  # 'sddc' or 'fleet'
        # SDDC-specific
        "resourceType", "resourceName", "resourceIp", "domainName",
        "credentialType", "accountType", "username", "password",
        "expiryStatus", "expiryDate",
        "connectivityStatus",
        "creationTimestamp", "modificationTimestamp",
        "autoRotateFrequencyDays", "autoRotateNextSchedule",
        "id",
        # Fleet-specific
        "fleetAlias", "fleetUserName", "fleetVmid", "fleetStatus",
        "fleetProductId", "fleetHostName", "fleetIp", "fleetNodeType",
        "fleetPlaintextPassword",
    ]

    sddc_creds: list[dict] = []
    fleet_creds: list[dict] = []
    if isinstance(data, dict):
        sddc_creds = data.get("sddc_credentials") or []
        fleet_creds = data.get("fleet_credentials") or []
    else:
        sddc_creds = data

    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()

        for c in sddc_creds:
            res = c.get("resource") or {}
            exp = c.get("expiry") or {}
            rot = c.get("autoRotatePolicy") or {}
            w.writerow({
                "source": "sddc",
                "resourceType": res.get("resourceType", ""),
                "resourceName": res.get("resourceName", ""),
                "resourceIp": res.get("resourceIp", ""),
                "domainName": res.get("domainName") or "",
                "credentialType": c.get("credentialType", ""),
                "accountType": c.get("accountType", ""),
                "username": c.get("username", ""),
                "password": c.get("password", ""),
                "expiryStatus": exp.get("status", ""),
                "expiryDate": exp.get("expiryDate", ""),
                "connectivityStatus": exp.get("connectivityStatus", "") if caps["connectivity_status"] else "",
                "creationTimestamp": c.get("creationTimestamp", ""),
                "modificationTimestamp": c.get("modificationTimestamp", ""),
                "autoRotateFrequencyDays": rot.get("frequencyInDays", "") if caps["auto_rotate_policy"] else "",
                "autoRotateNextSchedule": rot.get("nextSchedule", "") if caps["auto_rotate_policy"] else "",
                "id": c.get("id", ""),
                "fleetAlias": "",
                "fleetUserName": "",
                "fleetVmid": "",
                "fleetStatus": "",
                "fleetProductId": "",
                "fleetHostName": "",
                "fleetIp": "",
                "fleetNodeType": "",
                "fleetPlaintextPassword": "",
            })

        for p in fleet_creds:
            ref = p.get("reference") or {}
            w.writerow({
                "source": "fleet",
                "resourceType": "", "resourceName": "", "resourceIp": "",
                "domainName": "", "credentialType": "", "accountType": "",
                "username": "", "password": "",
                "expiryStatus": "", "expiryDate": "", "connectivityStatus": "",
                "creationTimestamp": p.get("createdOn", ""),
                "modificationTimestamp": p.get("lastUpdatedOn", ""),
                "autoRotateFrequencyDays": "", "autoRotateNextSchedule": "",
                "id": "",
                "fleetAlias": p.get("alias", ""),
                "fleetUserName": p.get("userName", ""),
                "fleetVmid": p.get("vmid", ""),
                "fleetStatus": p.get("status", ""),
                "fleetProductId": ref.get("productId", ""),
                "fleetHostName": ref.get("hostName", ""),
                "fleetIp": ref.get("ip", ""),
                "fleetNodeType": ref.get("nodeType", ""),
                "fleetPlaintextPassword": p.get("plaintextPassword") or "",
            })


# ---------- CLI ----------

def setup_logging(verbose: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(ColorFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=C("Dump credentials from a VCF SDDC Manager and Fleet Manager "
                      "(VCF 4.x / 5.x / 9.x).", Fore.WHITE, bold=True),
        formatter_class=ColoredHelpFormatter,
        epilog=__doc__.split("Examples:")[1] if "Examples:" in __doc__ else None,
    )
    p.add_argument("--host",
                   help="FQDN/IP of the SDDC Manager (no https://). "
                        "If omitted, the script asks interactively.")
    p.add_argument("--vcf", default="auto",
                   choices=["auto", "4", "5", "9.0", "9.1"],
                   help="Target VCF version [auto]. 'auto' = detect via "
                        "/v1/sddc-managers after login")

    auth = p.add_argument_group("authentication (any method - prompts if omitted)")
    auth.add_argument("--username",
                      help="SSO user (e.g. administrator@vsphere.local)")
    auth.add_argument("--password",
                      help="Password. If omitted and --username is set, prompts "
                           "interactively (paste-friendly, hidden input).")
    auth.add_argument("--api-key", help="API key (VCF 4.4+)")
    auth.add_argument("--id-token", help="SSO ID token (VCF 9.x)")

    flt = p.add_argument_group("filters")
    flt.add_argument("--resource-type", choices=[
        "ESXI", "VCENTER", "PSC", "NSXT_MANAGER", "NSXT_EDGE",
        "NSX_ALB", "BACKUP", "HCX_MANAGER", "VSP",
    ], help="Filter by resource type")
    flt.add_argument("--resource-name", help="Filter by resource name")
    flt.add_argument("--domain-name", help="Filter by workload domain name")
    flt.add_argument("--account-type",
                     help="USER/SYSTEM/SERVICE (VCF 5.0+)")

    fm = p.add_argument_group("fleet manager (VCF 9.x, formerly Aria components)")
    fm.add_argument("--fleet-host",
                    help="FQDN/IP of the VCF Fleet Manager appliance. Required "
                         "to pull fleet component passwords (VCF Operations, "
                         "VCF Automation, Identity Broker, Cloud Proxy, etc.)")
    fm.add_argument("--fleet-username", default="admin@local",
                    help="Fleet Manager API user (Basic Auth) [admin@local]")
    fm.add_argument("--fleet-password",
                    help="Fleet Manager API user password. If omitted, will "
                         "prompt interactively when --fleet-host is set.")
    fm.add_argument("--fleet-root-password",
                    help="Fleet Manager VM root password. Required to decrypt "
                         "fleet passwords to plaintext. If omitted, only "
                         "metadata/aliases are saved.")
    fm.add_argument("--fleet-only", action="store_true",
                    help="Skip SDDC Manager /v1/credentials, only fetch fleet "
                         "passwords (still needs --host for version detection "
                         "or use --vcf to skip that)")
    fm.add_argument("--fleet-page-size", type=int, default=100,
                    help="Fleet API page size [100]")
    fm.add_argument("--fleet-list-path",
                    help="Override the Fleet Locker LIST endpoint path "
                         "(e.g. '/lcm/locker/api/passwords'). Use when "
                         "auto-detection fails - find it in Swagger UI at "
                         "https://<fleet>/api/swagger-ui/index.html.")
    fm.add_argument("--fleet-decrypt-path",
                    help="Override the Fleet Locker DECRYPT endpoint path "
                         "(e.g. '/lcm/locker/api/passwords/view/{vmid}'). "
                         "Must contain the literal '{vmid}' placeholder.")

    out = p.add_argument_group("output")
    out.add_argument("--output", default=None,
                     help="Output file (.json or .csv). If omitted, an auto-named "
                          "file 'credentials_<host>_<timestamp>.json' is created "
                          "in the current working directory.")
    out.add_argument("--format", choices=["json", "csv"], default=None,
                     help="Force output format when --output is omitted. "
                          "Otherwise inferred from the --output extension. [json]")
    out.add_argument("--mask", action="store_true",
                     help="Mask passwords in output (audit mode)")
    out.add_argument("--page-size", type=int, default=200,
                     help="API page size [200]")
    out.add_argument("--list-users", action="store_true",
                     help="List SDDC Manager users/groups/service accounts and "
                          "their roles (GET /v1/users + /v1/roles) instead of "
                          "credentials. Useful for diagnosing PERMISSION_NOT_"
                          "FOUND / 401 on /v1/tokens. Prints a table and writes "
                          "full JSON to --output.")

    misc = p.add_argument_group("misc")
    misc.add_argument("--insecure", action="store_true",
                      help="Disable TLS verification (self-signed lab)")
    misc.add_argument("--keep-token", action="store_true",
                      help="Do not invalidate refresh token on exit (debug)")
    misc.add_argument("--no-color", action="store_true",
                      help="Disable colored output")
    misc.add_argument("-v", "--verbose", action="store_true",
                      help="Verbose logging")

    return p.parse_args()


def run_wizard(args: argparse.Namespace) -> argparse.Namespace:
    """
    Fill in missing arguments by asking the user interactively.

    Triggered:
      - Unconditionally when no CLI args were given
      - Per-field when CLI args were partial (e.g. --host but no --username)

    Modifies and returns the same Namespace object.
    """
    if not args.host:
        args.host = ask("SDDC Manager FQDN or IP")

    if args.vcf == "auto":
        info(f"Will auto-detect VCF version of {args.host} after login.")

    # TLS verification
    if not args.insecure:
        if ask_yes_no("Skip TLS verification? (lab / self-signed certs)",
                      default=True):
            args.insecure = True

    # Auth method
    if not (args.username or args.api_key or args.id_token):
        method = ask_choice(
            "Authentication method",
            choices=[
                ("user", "Username + password (most common)"),
                ("api", "API key (VCF 4.4+ service accounts)"),
                ("sso", "SSO ID token (VCF 9.x federated identity)"),
            ],
            default=0,
        )
        if method == "user":
            args.username = ask("Username",
                                default="administrator@vsphere.local")
            args.password = ask_password(f"Password for {args.username}")
        elif method == "api":
            args.api_key = ask_password("API key")
        else:
            args.id_token = ask_password("SSO ID token")
    else:
        # Partial args - just prompt for missing password if needed
        if args.username and not args.password and not (args.api_key or args.id_token):
            args.password = ask_password(f"Password for {args.username}")

    return args


def maybe_offer_fleet(args: argparse.Namespace, vcf_version: str,
                      sddc_client: Any = None) -> None:
    """
    Called AFTER successful SDDC login + version detection.
    If VCF 9.x and no --fleet-host was given, offer to also pull fleet
    credentials. Tries to discover real FQDNs from SDDC Manager API first.
    Mutates args in place.
    """
    if args.fleet_host:
        # User already specified fleet - just prompt for missing credentials
        if not args.fleet_password:
            args.fleet_password = ask_password(
                f"Fleet API password for {args.fleet_username}")
        if not args.fleet_root_password:
            warn("Without --fleet-root-password, fleet entries will not be "
                 "decrypted to plaintext.")
            if ask_yes_no("Provide Fleet Manager root password for decryption?",
                          default=True):
                args.fleet_root_password = ask_password(
                    "Fleet Manager VM root password")
        return

    # No fleet host given - decide whether to offer
    if not vcf_version.startswith("9"):
        info(f"VCF {vcf_version} detected - no separate Fleet Manager "
             f"(fleet/Aria passwords are in SDDC Manager creds for this version).")
        return

    info("VCF 9.x detected. Fleet Manager (formerly Aria) holds additional "
         "passwords for VCF Operations, VCF Automation, Identity Broker, "
         "Cloud Proxy, License Server, etc.")
    if vcf_version == "9.1":
        warn("Architectural note for VCF 9.1: the standalone Fleet Management "
             "appliance is deprecated and powered down after upgrade. VCF 9.1 "
             "by design does NOT store fleet component passwords centrally. "
             "The best you can get from a running 9.1 fleet API is account "
             "metadata (names, FQDNs, status). Plaintext fleet passwords had "
             "to be saved at deployment time from the VCF Installer UI.")
        if not ask_yes_no("Try to query Fleet API anyway (for metadata)?",
                          default=True):
            return
    else:
        if not ask_yes_no("Pull Fleet Manager credentials too?", default=True):
            return

    # Discover real FQDNs from SDDC Manager
    discovered: dict[str, Any] = {}
    suggestion: str | None = None
    if sddc_client is not None:
        info("Asking SDDC Manager for the actual fleet component FQDNs ...")
        try:
            discovered = sddc_client.discover_fleet_fqdns()
        except Exception as e:
            log.debug("Fleet FQDN discovery failed: %s", e)

        # Show what we found
        found_any = False
        for key, label in [
            ("vcfms_platform_fqdn",     "VCFMS Platform (9.1)"),
            ("vcfms_instance_fqdn",     "VCFMS Instance (9.1)"),
            ("vcfms_fleet_fqdn",        "VCFMS Fleet (9.1)"),
            ("fleet_management",        "Fleet Management appliance (9.0)"),
            ("vcf_operations_lb",       "VCF Operations (LB)"),
            ("vcf_operations",          "VCF Operations nodes"),
            ("vcf_operations_collector", "Cloud Proxy / Collector"),
            ("vcf_automation",          "VCF Automation"),
            ("vcf_identity_broker",     "Identity Broker"),
            ("license_server",          "License Server"),
        ]:
            val = discovered.get(key)
            if val:
                if isinstance(val, list):
                    if not val:
                        continue
                    info(f"  {label:<33} : {', '.join(val)}")
                else:
                    info(f"  {label:<33} : {val}")
                found_any = True
        if found_any:
            suggestion = discovered.get("best_fleet_guess")
        else:
            warn("SDDC Manager did not return any fleet component FQDNs "
                 "(deployment task may not be accessible).")

    # Fallback heuristic if discovery failed
    if not suggestion:
        suggestion = suggest_fleet_fqdn(args.host)

    if suggestion:
        args.fleet_host = ask("Fleet Manager FQDN or IP", default=suggestion)
    else:
        args.fleet_host = ask("Fleet Manager FQDN or IP")

    args.fleet_username = ask("Fleet API user",
                              default=args.fleet_username or "admin@local")
    args.fleet_password = ask_password(
        f"Fleet API password for {args.fleet_username}")

    if ask_yes_no("Decrypt passwords to plaintext "
                  "(needs Fleet Manager VM root password)?", default=True):
        args.fleet_root_password = ask_password(
            "Fleet Manager VM root password")
    else:
        warn("Skipping decryption - fleet entries will be saved with metadata "
             "only (alias, vmid, references), no plaintext passwords.")


def suggest_fleet_fqdn(sddc_host: str) -> str | None:
    """
    Heuristically guess the Fleet Manager FQDN from the SDDC Manager FQDN.
    Common naming patterns seen in VCF 9.x labs and Broadcom docs:
      sddcmanager-a.site-a.vcf.lab -> vcf-fleet.site-a.vcf.lab
      sfo-vcf01.sfo.rainpole.io    -> flt-fm01.sfo.rainpole.io
    Returns None if hostname looks like an IP address.
    """
    if not sddc_host or sddc_host.replace(".", "").isdigit():
        return None  # IP address, no FQDN structure to leverage
    parts = sddc_host.split(".", 1)
    if len(parts) < 2:
        return None
    domain = parts[1]
    return f"vcf-fleet.{domain}"


def main() -> int:
    args = parse_args()

    global _USE_COLOR
    if args.no_color:
        _USE_COLOR = False

    setup_logging(args.verbose)

    # Detect "no args" mode (only --no-color / -v / --verbose count as
    # cosmetic): if --host wasn't supplied, run the full wizard.
    no_essential_args = not args.host
    if no_essential_args:
        print_banner()
        args = run_wizard(args)

    if not _COLORAMA and sys.platform == "win32":
        log.debug("colorama not installed - colors may not render correctly "
                  "in cmd.exe (install with: pip install colorama)")

    if args.insecure:
        requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

    # Final validation - if we still don't have a host or any auth, abort
    if not args.host:
        log.error("No --host given (and wizard was skipped).")
        return 2
    if not (args.username or args.api_key or args.id_token):
        log.error("No authentication method provided "
                  "(--username/--password, --api-key, or --id-token).")
        return 2

    # If --password is missing for username auth, prompt now (CLI-mode users
    # also benefit from the paste-friendly prompt)
    password = args.password
    if args.username and not password and not (args.api_key or args.id_token):
        password = ask_password(f"Password for {args.username}")

    # Pre-login: heuristic version detection. Post-login: reliable.
    if args.vcf == "auto":
        log.info("Detecting VCF version ...")
        vcf_version = SDDCClient.detect_version(args.host,
                                                verify_tls=not args.insecure)
        log.info("Heuristic detection: VCF %s (will be confirmed after login)",
                 vcf_version)
    else:
        vcf_version = args.vcf

    client = SDDCClient(args.host, vcf_version=vcf_version,
                        verify_tls=not args.insecure)

    def _cleanup(*_a):
        if not args.keep_token:
            client.logout()

    atexit.register(_cleanup)
    signal.signal(signal.SIGINT, lambda *_: (_cleanup(), sys.exit(130)))
    signal.signal(signal.SIGTERM, lambda *_: (_cleanup(), sys.exit(143)))

    try:
        client.login(
            username=args.username,
            password=password,
            api_key=args.api_key,
            id_token=args.id_token,
        )

        # Post-login: refine version detection
        if args.vcf == "auto":
            real = client.detect_version_post_login()
            if real:
                norm = normalize_vcf(real)
                if norm != vcf_version:
                    log.info("Confirmed SDDC Manager version: %s (key %s) - "
                             "updating capability matrix.", real, norm)
                    client.vcf_version = norm
                    client.caps = VCF_CAPS[norm]
                else:
                    log.info("SDDC Manager version confirmed: %s", real)
                vcf_version = client.vcf_version

        # Now that we know the real version, offer/configure Fleet Manager
        # (interactive part - only meaningful for VCF 9.x). Pass the live
        # client so we can ask SDDC Manager for the real fleet FQDNs.
        if no_essential_args or (args.fleet_host and
                                 not (args.fleet_password and
                                      args.fleet_root_password)):
            maybe_offer_fleet(args, vcf_version, sddc_client=client)

        # --list-users: dump user/role mappings instead of credentials.
        if args.list_users:
            log.info("Fetching SDDC Manager users and roles (VCF %s) ...",
                     client.vcf_version)
            users = client.get_users()
            log.info("Fetched %d user/group/service account record(s).",
                     len(users))
            print_users_table(users)

            output_path = args.output
            if output_path:
                fmt = (args.format or
                       ("csv" if output_path.lower().endswith(".csv")
                        else "json"))
            else:
                fmt = args.format or "json"
                output_path = default_output_path(args.host + "_users", fmt)
                log.info("No --output given, using auto-generated path: %s",
                         output_path)

            if fmt == "csv":
                # Flat, CSV-friendly subset of the user records
                flat = [{
                    "name": u.get("name", ""),
                    "domain": u.get("domain", ""),
                    "type": u.get("type", ""),
                    "role": u.get("resolvedRoleName", ""),
                    "id": u.get("id", ""),
                } for u in users]
                with open(output_path, "w", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(
                        fh, fieldnames=["name", "domain", "type", "role", "id"])
                    writer.writeheader()
                    writer.writerows(flat)
            else:
                save_json(users, output_path)
            log.info("Saved %d user record(s) to %s", len(users), output_path)
            return 0

        sddc_creds: list[dict] = []
        if not args.fleet_only:
            log.info("Fetching SDDC Manager credentials (VCF %s) ...",
                     client.vcf_version)
            sddc_creds = client.get_credentials(
                resource_type=args.resource_type,
                resource_name=args.resource_name,
                domain_name=args.domain_name,
                account_type=args.account_type,
                page_size=args.page_size,
            )
            log.info("Fetched %d SDDC records.", len(sddc_creds))
        else:
            log.info("--fleet-only set, skipping SDDC Manager credentials.")

        fleet_creds: list[dict] = []
        if args.fleet_host:
            fleet_password = args.fleet_password
            if not fleet_password:
                fleet_password = ask_password(
                    f"Fleet API password for {args.fleet_username}@{args.fleet_host}")

            fleet_root_pwd = args.fleet_root_password
            if not fleet_root_pwd:
                log.warning("No --fleet-root-password provided. Fleet entries "
                            "will be saved without decrypted plaintext "
                            "passwords (only aliases & metadata).")

            log.info("Connecting to Fleet Manager at %s ...", args.fleet_host)
            fleet = FleetClient(
                host=args.fleet_host,
                username=args.fleet_username,
                password=fleet_password,
                root_password=fleet_root_pwd,
                verify_tls=not args.insecure,
                list_path_override=args.fleet_list_path,
                decrypt_path_override=args.fleet_decrypt_path,
            )
            fleet_creds = fleet.get_all_credentials(
                page_size=args.fleet_page_size,
                decrypt=bool(fleet_root_pwd),
            )
            log.info("Fetched %d fleet records (%d decrypted).",
                     len(fleet_creds),
                     sum(1 for p in fleet_creds if p.get("plaintextPassword")))

        if args.mask:
            sddc_creds = mask_passwords(sddc_creds)
            fleet_creds = mask_passwords(fleet_creds)

        # Build output: keep classic flat list when only SDDC was queried
        # (backward compatible), else use {sddc_credentials, fleet_credentials}
        if fleet_creds or args.fleet_only:
            output_data: dict | list = {
                "sddc_credentials": sddc_creds,
                "fleet_credentials": fleet_creds,
            }
            total = len(sddc_creds) + len(fleet_creds)
        else:
            output_data = sddc_creds
            total = len(sddc_creds)

        # Resolve output path and format
        output_path = args.output
        if output_path:
            fmt = (args.format or
                   ("csv" if output_path.lower().endswith(".csv") else "json"))
        else:
            fmt = args.format or "json"
            output_path = default_output_path(args.host, fmt)
            log.info("No --output given, using auto-generated path: %s",
                     output_path)

        if fmt == "csv":
            save_csv(output_data, output_path, client.caps)
        else:
            save_json(output_data, output_path)
        log.info("Saved %d records (%d SDDC + %d fleet) to %s",
                 total, len(sddc_creds), len(fleet_creds), output_path)
        return 0

    except HTTPError as e:
        body = ""
        try:
            body = e.response.text[:500] if e.response is not None else ""
        except Exception:
            pass
        status = e.response.status_code if e.response is not None else "?"
        log.error("HTTP %s: %s", status, body)
        return 3
    except Exception as e:
        log.error("Error: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
