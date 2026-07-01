#!/usr/bin/env python3
"""
vcf_launcher.py
===============

Generate a self-contained offline HTML dashboard with tiles for all
components of a VMware Cloud Foundation environment - a replacement
for the deprecated Workspace ONE Access catalog.

Supports VCF 4.x / 5.x / 9.0 / 9.1.

The generated HTML is a SINGLE file with:
  - Inline SVG icons for every VCF product (no external images / CDNs)
  - Inline CSS + JS (search, category filter, dark mode, bookmarks)
  - Works fully offline once generated

Components auto-discovered from SDDC Manager API:
  - SDDC Manager itself                (via /v1/sddc-managers)
  - vCenter Servers                    (via /v1/vcenters)
  - NSX Managers                       (via /v1/nsxt-clusters)
  - NSX Advanced Load Balancer         (via /v1/nsx-alb-clusters, VCF 5+)
  - HCX Managers                       (via /v1/hcx-managers)
  - Backup server                      (via /v1/system/backup-configuration)
  - VCF Operations, Automation etc.    (via /v1/vcf-management-components/*)
  - VCFMS Platform/Instance/Fleet      (via /v1/vsp-clusters, VCF 9.1)

Custom links from JSON config file (--custom-links) can add any external
URLs like Zabbix, HPE OneView, BookStack, Grafana, etc.

Custom links config example (custom_links.json):
  {
    "links": [
      {
        "name": "HPE OneView",
        "url": "https://oneview.example.com",
        "category": "Management",
        "icon": "hpe"
      },
      {
        "name": "Zabbix",
        "url": "https://zabbix.example.com",
        "category": "Monitoring",
        "icon": "zabbix"
      }
    ]
  }

Built-in icon keys: vcenter, nsx, nsx-alb, sddc-manager, vcf-ops,
vcf-automation, aria-suite, log-insight, network-insight, hcx,
vcfms-fleet, vcfms-platform, vcfms-instance, license-server, backup,
identity-broker, hpe, zabbix, grafana, bookstack, generic.

Dependencies:
  pip install requests colorama

Usage:
  # Interactive (asks for host/username/password if not provided)
  python vcf_launcher.py

  # Full CLI
  python vcf_launcher.py --host sddcmanager.example.com ^
      --username administrator@vsphere.local --password '...' ^
      --custom-links custom_links.json --output launcher.html --insecure
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import logging
import os
import re
import sys
from getpass import getpass
from typing import Any

import requests
from requests.exceptions import HTTPError, RequestException
from urllib3.exceptions import InsecureRequestWarning


# ---------- colorama (optional) ----------

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
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stderr.isatty():
        return False
    return True


_USE_COLOR = _supports_color()


def C(text: str, color: str = "", bold: bool = False) -> str:
    if not (_USE_COLOR and _COLORAMA):
        return text
    prefix = color
    if bold:
        prefix += Style.BRIGHT
    return f"{prefix}{text}{Style.RESET_ALL}" if prefix else text


class ColorFormatter(logging.Formatter):
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


log = logging.getLogger("vcf-launcher")


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{C(default, Fore.YELLOW)}]" if default else ""
    full = f"{C('[?]', Fore.CYAN, bold=True)} {prompt}{suffix}: "
    try:
        val = input(full).strip()
    except (KeyboardInterrupt, EOFError):
        sys.stderr.write("\n")
        sys.exit(130)
    return val or (default or "")


def ask_password(prompt: str) -> str:
    full = (f"{C('[?]', Fore.CYAN, bold=True)} {prompt} "
            f"{C('(paste: right-click / Shift+Insert, hidden)', Style.DIM)}: ")
    try:
        return getpass(full)
    except (KeyboardInterrupt, EOFError):
        sys.stderr.write("\n")
        sys.exit(130)


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    full = f"{C('[?]', Fore.CYAN, bold=True)} {prompt} [{C(hint, Fore.YELLOW)}]: "
    try:
        val = input(full).strip().lower()
    except (KeyboardInterrupt, EOFError):
        sys.stderr.write("\n")
        sys.exit(130)
    if not val:
        return default
    return val in ("y", "yes", "ano", "a")


def info(msg: str) -> None:
    sys.stderr.write(f"{C('[*]', Fore.BLUE, bold=True)} {msg}\n")


def ok(msg: str) -> None:
    sys.stderr.write(f"{C('[+]', Fore.GREEN, bold=True)} {msg}\n")


# ---------- SDDC Manager client (minimal) ----------

class VCFOpsClient:
    """
    Fleet inventory client for VCF 9.x. Supports TWO different API patterns:

    1) Classic VCF Operations appliance (vROps-derived, `flt-ops01a`, etc.):
         POST /suite-api/api/auth/token/acquire  (JSON body)
             {"username":"admin","password":"...","authSource":"LOCAL"}
         -> {"token":"<uuid>::<uuid>"}
         Header: Authorization: OpsToken <token>
         Inventory: /suite-api/api/fleet-management/password-management/accounts/query

    2) VCFMS Platform / Runtime (VCF 9.1 containerized, `vcf-msr01`, etc.):
         POST /api/v1/identity/token  (application/x-www-form-urlencoded)
             grant_type=password&username=admin@system&password=...
         -> {"access_token":"<token>"}
         Header: Authorization: Bearer <token>
         Inventory: several candidate endpoints tried, no plaintext passwords

    The client auto-detects which pattern works by trying both. This handles:
      - Traditional VCF Operations appliance deployments (both 9.0 and 9.1)
      - Greenfield VCF 9.1 with containerized VCFMS (no classic vROps appliance)
    """

    STYLE_VROPS  = "vrops"    # classic VCF Operations (suite-api)
    STYLE_VCFMS  = "vcfms"    # containerized VCFMS Platform

    def __init__(self, host: str, username: str, password: str,
                 auth_source: str = "LOCAL",
                 verify_tls: bool = True, timeout: int = 60):
        self.base = f"https://{host}"
        self.username = username
        self.password = password
        self.auth_source = auth_source
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.token: str | None = None
        self.style: str | None = None    # STYLE_VROPS or STYLE_VCFMS after login
        self.session = requests.Session()

    def login(self) -> None:
        """Try each auth style in turn until one succeeds."""
        log.info("Authenticating to %s ...", self.base)

        # 1) Try vROps-style (suite-api) first
        try:
            self._login_vrops()
            self.style = self.STYLE_VROPS
            log.info("Auth OK using vROps/VCF Operations suite-api.")
            return
        except (RequestException, HTTPError, RuntimeError) as e:
            log.debug("vROps-style auth failed: %s", e)

        # 2) Try VCFMS-style (OAuth on /api/v1/identity/token).
        #    Also try the '<user>@system' variant since VCFMS commonly uses it.
        candidate_users = [self.username]
        if "@" not in self.username:
            candidate_users.append(f"{self.username}@system")
        elif not self.username.endswith("@system"):
            local = self.username.split("@", 1)[0]
            candidate_users.append(f"{local}@system")

        last_err: Exception | None = None
        for user in candidate_users:
            try:
                self._login_vcfms(user)
                self.style = self.STYLE_VCFMS
                if user != self.username:
                    log.info("Auth OK using VCFMS OAuth "
                             "(username '%s' instead of '%s').",
                             user, self.username)
                    self.username = user
                else:
                    log.info("Auth OK using VCFMS OAuth.")
                return
            except (RequestException, HTTPError, RuntimeError) as e:
                last_err = e
                log.debug("VCFMS-style auth as '%s' failed: %s", user, e)

        raise RuntimeError(
            f"Both auth styles failed for {self.base}. "
            f"Last error: {last_err}"
        )

    def _login_vrops(self) -> None:
        r = self.session.post(
            f"{self.base}/suite-api/api/auth/token/acquire",
            json={"username": self.username, "password": self.password,
                  "authSource": self.auth_source},
            headers={"Accept": "application/json",
                     "Content-Type": "application/json"},
            verify=self.verify_tls, timeout=self.timeout,
        )
        if r.status_code not in (200, 201):
            r.raise_for_status()
        body = r.json()
        token = body.get("token")
        if not token:
            raise RuntimeError(f"No 'token' in acquire response: {body}")
        self.token = token

    def _login_vcfms(self, username: str) -> None:
        r = self.session.post(
            f"{self.base}/api/v1/identity/token",
            data={"grant_type": "password",
                  "username": username,
                  "password": self.password},
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"},
            verify=self.verify_tls, timeout=self.timeout,
        )
        if r.status_code not in (200, 201):
            r.raise_for_status()
        body = r.json()
        token = body.get("access_token") or body.get("accessToken")
        if not token:
            raise RuntimeError(f"No 'access_token' in VCFMS token response: {body}")
        self.token = token

    def logout(self) -> None:
        if not self.token or not self.style:
            return
        try:
            if self.style == self.STYLE_VROPS:
                self.session.post(
                    f"{self.base}/suite-api/api/auth/token/release",
                    headers={"Authorization": f"OpsToken {self.token}"},
                    verify=self.verify_tls, timeout=self.timeout,
                )
            # VCFMS OAuth tokens usually don't have an explicit revoke;
            # they simply expire.
        except RequestException:
            pass
        finally:
            self.token = None

    def _auth_header(self) -> dict[str, str]:
        if self.style == self.STYLE_VCFMS:
            return {"Authorization": f"Bearer {self.token}"}
        return {"Authorization": f"OpsToken {self.token}"}

    def list_managed_accounts(self, page_size: int = 200) -> list[dict]:
        """
        Return account/inventory records. Format depends on style:
          vROps  -> vcfPasswordAccounts[] with applianceFqdn / appliance
          VCFMS  -> best-effort: tries a few candidate inventory endpoints
        Result is normalized to a list of dicts with applianceFqdn + appliance
        keys wherever they can be extracted.
        """
        if not self.token or not self.style:
            raise RuntimeError("Not logged in.")

        if self.style == self.STYLE_VROPS:
            return self._query_vrops_accounts(page_size)
        else:
            return self._query_vcfms_inventory()

    def _query_vrops_accounts(self, page_size: int) -> list[dict]:
        url = (f"{self.base}/suite-api/api/fleet-management/"
               f"password-management/accounts/query")
        headers = self._auth_header()
        all_accounts: list[dict] = []
        page = 0
        while page < 100:
            try:
                r = self.session.post(
                    url, headers=headers,
                    params={"page": page, "pageSize": page_size},
                    json={},
                    verify=self.verify_tls, timeout=self.timeout,
                )
            except RequestException as e:
                log.debug("Accounts query page %d failed: %s", page, e)
                break
            if r.status_code >= 400:
                log.debug("Accounts query page %d -> HTTP %d",
                          page, r.status_code)
                break
            try:
                body = r.json()
            except ValueError:
                break
            accounts = (body.get("vcfPasswordAccounts")
                        or body.get("accounts")
                        or body.get("elements") or [])
            if not accounts:
                break
            all_accounts.extend(accounts)
            log.debug("  ops page %d: %d accounts (total %d)",
                      page + 1, len(accounts), len(all_accounts))
            if len(accounts) < page_size:
                break
            page += 1
        return all_accounts

    # VCFMS inventory endpoints to try. Structure is not fully documented
    # (Broadcom hasn't published a public swagger for these yet). We probe
    # several likely paths and normalize whatever comes back.
    VCFMS_INVENTORY_PATHS = [
        "/api/v1/appliances",
        "/api/v1/inventory/appliances",
        "/api/v1/components",
        "/api/v1/inventory/components",
        "/api/v1/fleet-management/components",
        "/api/v1/fleet-management/appliances",
        "/api/v1/fleet-management/instances",
        "/api/v1/fleet-management/vcf-instances",
        "/api/v1/vcf-instances",
        "/api/v1/instances",
    ]

    def _query_vcfms_inventory(self) -> list[dict]:
        """
        VCFMS inventory endpoints are not fully documented; probe several
        likely paths. Whatever returns a list of items with FQDNs is used.
        Returns a normalized list of dicts each with keys:
          applianceFqdn, appliance (type as string)
        """
        headers = self._auth_header()
        for path in self.VCFMS_INVENTORY_PATHS:
            url = f"{self.base}{path}"
            try:
                r = self.session.get(
                    url, headers=headers,
                    verify=self.verify_tls, timeout=self.timeout,
                )
            except RequestException as e:
                log.debug("GET %s failed: %s", path, e)
                continue
            if r.status_code >= 400:
                log.debug("GET %s -> HTTP %d", path, r.status_code)
                continue
            try:
                body = r.json()
            except ValueError:
                continue
            items = (body.get("elements") if isinstance(body, dict) else body)
            if isinstance(body, dict) and "elements" not in body:
                for candidate in ("items", "components", "appliances",
                                  "instances"):
                    if candidate in body:
                        items = body[candidate]
                        break
                else:
                    items = [body]
            if not isinstance(items, list) or not items:
                continue

            normalized: list[dict] = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                fqdn = (it.get("applianceFqdn") or it.get("fqdn")
                        or it.get("hostname"))
                appl = (it.get("appliance") or it.get("type")
                        or it.get("componentType") or it.get("kind") or "")
                if fqdn:
                    normalized.append({"applianceFqdn": fqdn,
                                       "appliance": str(appl).upper()})
            if normalized:
                log.info("VCFMS inventory found at %s (%d records)",
                         path, len(normalized))
                return normalized
        log.warning("No VCFMS inventory endpoint returned usable data. "
                    "Auth succeeded but this build/deployment does not "
                    "expose a documented fleet inventory list. Nothing new "
                    "to add - VCFMS Platform/Fleet/Instance are already in "
                    "the dashboard from SDDC Manager discovery.")
        return []


class SDDCClient:
    """Minimal SDDC Manager API client for launcher discovery."""

    def __init__(self, host: str, verify_tls: bool = True, timeout: int = 60):
        self.base = f"https://{host}"
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.token: str | None = None
        self.refresh_id: str | None = None
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        self.vcf_version = "unknown"

    def login(self, username: str, password: str) -> None:
        log.info("Logging in to %s ...", self.base)
        r = self.session.post(
            f"{self.base}/v1/tokens",
            json={"username": username, "password": password},
            verify=self.verify_tls, timeout=self.timeout,
        )
        r.raise_for_status()
        body = r.json()
        self.token = body.get("accessToken")
        self.refresh_id = (body.get("refreshToken") or {}).get("id")
        if not self.token:
            raise RuntimeError(f"No accessToken in response: {body}")
        log.info("Logged in.")

    def logout(self) -> None:
        if not self.refresh_id or not self.token:
            return
        try:
            self.session.delete(
                f"{self.base}/v1/tokens/refresh-token",
                headers={"Authorization": f"Bearer {self.token}"},
                data=json.dumps(self.refresh_id),
                verify=self.verify_tls, timeout=self.timeout,
            )
        except RequestException:
            pass

    def get(self, path: str) -> Any:
        """
        GET a path, returning parsed JSON or None on any failure
        (4xx/5xx, redirects, non-JSON body, network error).

        We deliberately do NOT follow redirects: some VCF versions redirect
        missing API endpoints (e.g. /v1/hcx-managers on VCF 5.x) to the
        SDDC Manager UI login page, which returns HTML and would crash
        r.json() with "Expecting value: line 4 column 1".
        """
        if not self.token:
            raise RuntimeError("Not logged in.")
        try:
            r = self.session.get(
                f"{self.base}{path}",
                headers={"Authorization": f"Bearer {self.token}"},
                verify=self.verify_tls, timeout=self.timeout,
                allow_redirects=False,
            )
        except RequestException as e:
            log.debug("GET %s failed: %s", path, e)
            return None

        if r.status_code >= 300:
            location = r.headers.get("Location", "")
            log.debug("GET %s -> HTTP %d%s (skipped)", path, r.status_code,
                      f" -> {location}" if location else "")
            return None

        ct = (r.headers.get("Content-Type") or "").lower()
        if "json" not in ct:
            log.debug("GET %s -> Content-Type %r, not JSON (skipped)", path, ct)
            return None

        try:
            return r.json()
        except ValueError as e:
            log.debug("GET %s -> JSON parse error: %s", path, e)
            return None

    def detect_version(self) -> str:
        data = self.get("/v1/sddc-managers")
        if data:
            els = data.get("elements") or []
            if els:
                v = els[0].get("version", "unknown")
                self.vcf_version = v
                return v
        return "unknown"


# ---------- built-in SVG icons ----------
#
# Every icon is inline SVG (24x24 viewBox). No external images / CDNs.
# Colors use CSS variables so light/dark mode works.

ICONS: dict[str, str] = {
    # VMware / Broadcom products - stylized
    "sddc-manager":
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">'
        '<path d="M6 15a4 4 0 0 1 0-8 5 5 0 0 1 9.6-2 4 4 0 0 1 2.4 7.4"/>'
        '<circle cx="12" cy="18" r="2.5" fill="currentColor" stroke="none"/>'
        '</svg>',
    "vcenter":
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">'
        '<rect x="3" y="3" width="7" height="7" rx="1"/>'
        '<rect x="14" y="3" width="7" height="7" rx="1"/>'
        '<rect x="3" y="14" width="7" height="7" rx="1"/>'
        '<rect x="14" y="14" width="7" height="7" rx="1"/>'
        '<circle cx="12" cy="12" r="2" fill="currentColor" stroke="none"/>'
        '</svg>',
    "nsx":
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">'
        '<path d="M12 2 4 5v6c0 5 3.5 9.5 8 11 4.5-1.5 8-6 8-11V5l-8-3z"/>'
        '<path d="M9 12l2 2 4-4"/>'
        '</svg>',
    "nsx-alb":
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">'
        '<circle cx="12" cy="12" r="9"/>'
        '<path d="M8 12h8M12 8v8"/>'
        '<path d="M6 8l2 2M18 8l-2 2M6 16l2-2M18 16l-2-2"/>'
        '</svg>',
    "vcf-ops":
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">'
        '<path d="M4 20h16"/>'
        '<rect x="5" y="12" width="3" height="6"/>'
        '<rect x="10.5" y="8" width="3" height="10"/>'
        '<rect x="16" y="4" width="3" height="14"/>'
        '</svg>',
    "vcf-automation":
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">'
        '<circle cx="12" cy="12" r="3"/>'
        '<path d="M12 3v3M12 18v3M3 12h3M18 12h3"/>'
        '<path d="M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/>'
        '</svg>',
    "aria-suite":
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">'
        '<rect x="3" y="3" width="8" height="8" rx="1"/>'
        '<rect x="13" y="3" width="8" height="8" rx="1"/>'
        '<rect x="3" y="13" width="8" height="8" rx="1"/>'
        '<rect x="13" y="13" width="8" height="8" rx="1"/>'
        '</svg>',
    "log-insight":
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">'
        '<rect x="3" y="4" width="18" height="16" rx="2"/>'
        '<path d="M7 9h10M7 12h6M7 15h8"/>'
        '</svg>',
    "network-insight":
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">'
        '<circle cx="6" cy="6" r="2.5"/>'
        '<circle cx="18" cy="6" r="2.5"/>'
        '<circle cx="6" cy="18" r="2.5"/>'
        '<circle cx="18" cy="18" r="2.5"/>'
        '<circle cx="12" cy="12" r="2.5"/>'
        '<path d="M8 7l3 4M16 7l-3 4M8 17l3-4M16 17l-3-4"/>'
        '</svg>',
    "hcx":
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">'
        '<path d="M3 8h6l3-4M21 16h-6l-3 4"/>'
        '<circle cx="6" cy="12" r="3"/>'
        '<circle cx="18" cy="12" r="3"/>'
        '<path d="M9 12h6"/>'
        '</svg>',
    "vcfms-fleet":
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">'
        '<rect x="2" y="9" width="6" height="6" rx="1"/>'
        '<rect x="9" y="6" width="6" height="12" rx="1"/>'
        '<rect x="16" y="9" width="6" height="6" rx="1"/>'
        '</svg>',
    "vcfms-platform":
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">'
        '<polygon points="12,3 21,7.5 21,16.5 12,21 3,16.5 3,7.5"/>'
        '<path d="M12 3v18M3 7.5l18 9M21 7.5l-18 9"/>'
        '</svg>',
    "vcfms-instance":
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">'
        '<rect x="3" y="4" width="18" height="4" rx="1"/>'
        '<rect x="3" y="10" width="18" height="4" rx="1"/>'
        '<rect x="3" y="16" width="18" height="4" rx="1"/>'
        '<circle cx="6" cy="6" r="0.6" fill="currentColor"/>'
        '<circle cx="6" cy="12" r="0.6" fill="currentColor"/>'
        '<circle cx="6" cy="18" r="0.6" fill="currentColor"/>'
        '</svg>',
    "license-server":
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">'
        '<circle cx="8" cy="12" r="3"/>'
        '<path d="M11 12h10M18 12v3M15 12v2"/>'
        '</svg>',
    "identity-broker":
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">'
        '<circle cx="12" cy="8" r="3.5"/>'
        '<path d="M5 21c1-4 4-6 7-6s6 2 7 6"/>'
        '<path d="M18 5l1.5 1.5L22 4"/>'
        '</svg>',
    "backup":
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">'
        '<ellipse cx="12" cy="6" rx="8" ry="2.5"/>'
        '<path d="M4 6v6c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5V6"/>'
        '<path d="M4 12v6c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5v-6"/>'
        '</svg>',
    # Common third-party (generic style)
    "hpe":
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">'
        '<rect x="2" y="8" width="20" height="8" rx="1"/>'
        '<path d="M6 12h4M14 12h4"/>'
        '</svg>',
    "zabbix":
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">'
        '<circle cx="12" cy="12" r="9"/>'
        '<path d="M8 8l8 8M8 16l8-8"/>'
        '</svg>',
    "grafana":
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">'
        '<path d="M3 18l4-6 4 3 4-8 6 11"/>'
        '<path d="M3 21h18"/>'
        '</svg>',
    "bookstack":
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">'
        '<rect x="4" y="4" width="16" height="4" rx="1"/>'
        '<rect x="4" y="10" width="16" height="4" rx="1"/>'
        '<rect x="4" y="16" width="16" height="4" rx="1"/>'
        '</svg>',
    "esxi":
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">'
        '<rect x="3" y="6" width="18" height="4" rx="0.5"/>'
        '<rect x="3" y="14" width="18" height="4" rx="0.5"/>'
        '<circle cx="7" cy="8" r="0.7" fill="currentColor"/>'
        '<circle cx="7" cy="16" r="0.7" fill="currentColor"/>'
        '</svg>',
    "generic":
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">'
        '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7L11 7"/>'
        '<path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7L13 17"/>'
        '</svg>',
}


# Tile categories and their color hues (used for icon accent)
CATEGORY_ORDER = [
    "Virtualization",
    "Networking",
    "Management",
    "Monitoring",
    "Automation",
    "Websites",
]

CATEGORY_COLORS = {
    "Virtualization": "#0091DA",   # VMware blue
    "Networking":     "#00CBB8",   # NSX teal
    "Management":     "#8A46FF",   # purple
    "Monitoring":     "#F7A400",   # amber
    "Automation":     "#00A98F",   # green
    "Websites":       "#6B7280",   # gray
}


# ---------- discovery ----------

def _short_name(fqdn: str) -> str:
    return (fqdn or "").split(".")[0]


def _pick_vcfops_candidates(tiles: list[dict],
                            sddc_host: str) -> tuple[str | None, list[tuple[str, str]]]:
    """
    Look at discovered tiles for FQDNs that could host the VCF Operations
    suite-api endpoint (/suite-api/api/fleet-management/password-management/...).

    Returns (best_default, [(fqdn, label), ...]).

    Priority order (best first):
      1. Tiles with icon 'vcf-ops' (VCF Operations appliance / LB / nodes)
      2. Tiles with icon 'vcfms-platform' (VCFMS Runtime - hosts containerized services)
      3. Tiles with icon 'vcfms-fleet' (VCFMS Fleet container - VCF 9.1)
      4. Tiles with icon 'vcfms-instance' (VCFMS Instance container)
    """
    priority_icons = [
        ("vcf-ops",         "VCF Operations"),
        ("vcfms-platform",  "VCFMS Platform"),
        ("vcfms-fleet",     "VCFMS Fleet"),
        ("vcfms-instance",  "VCFMS Instance"),
    ]
    candidates: list[tuple[str, str]] = []
    for icon, label in priority_icons:
        for t in tiles:
            if t.get("icon") != icon:
                continue
            url = t.get("url", "")
            if not url.startswith("https://"):
                continue
            fqdn = url[len("https://"):].rstrip("/").split("/")[0]
            if fqdn and (fqdn, label) not in candidates:
                candidates.append((fqdn, label))

    if candidates:
        return candidates[0][0], candidates

    # Nothing in tiles - fall back to heuristic guess
    parts = sddc_host.split(".", 1)
    if len(parts) > 1:
        return f"vcfops-a.{parts[1]}", []
    return None, []


def discover_components(client: SDDCClient) -> list[dict]:
    """
    Query SDDC Manager API and return a list of component tiles.
    Each tile: {name, url, category, icon, description, order}

    Every discovery block is wrapped in try/except so a single unexpected
    response cannot break the rest of the discovery.
    """
    tiles: list[dict] = []

    def _safe(label: str, fn):
        try:
            fn()
        except Exception as e:
            log.warning("Discovery of %s failed: %s (skipped)", label, e)

    # --- SDDC Manager itself ---
    def _sddc():
        sddc = client.get("/v1/sddc-managers")
        if sddc:
            for el in sddc.get("elements") or []:
                fqdn = el.get("fqdn") or el.get("ipAddress")
                if fqdn:
                    tiles.append({
                        "name": "SDDC Manager",
                        "url": f"https://{fqdn}/",
                        "category": "Management",
                        "icon": "sddc-manager",
                        "description": f"v{el.get('version', '?')}",
                        "order": 1,
                    })
            log.info("  %d SDDC Manager", len(sddc.get("elements") or []))
    _safe("SDDC Manager", _sddc)

    # --- vCenter Servers ---
    def _vcenters():
        vcs = client.get("/v1/vcenters")
        if vcs:
            for el in vcs.get("elements") or []:
                fqdn = el.get("fqdn")
                if not fqdn:
                    continue
                domain_type = el.get("domainType", "")
                desc = ("Management Domain"
                        if "MANAGEMENT" in str(domain_type).upper()
                        else "Workload Domain" if domain_type
                        else "vCenter Server")
                tiles.append({
                    "name": f"vCenter Server {_short_name(fqdn).upper()}",
                    "url": f"https://{fqdn}/ui/",
                    "category": "Virtualization",
                    "icon": "vcenter",
                    "description": desc + (f" - v{el.get('version', '')}"
                                           if el.get("version") else ""),
                    "order": 10,
                })
            log.info("  %d vCenter Server(s)", len(vcs.get("elements") or []))
    _safe("vCenters", _vcenters)

    # --- NSX Managers (VIP + individual nodes) ---
    def _nsx():
        nsx = client.get("/v1/nsxt-clusters")
        if nsx:
            for el in nsx.get("elements") or []:
                vip = el.get("vipFqdn") or el.get("vip")
                if vip:
                    tiles.append({
                        "name": f"NSX Manager {_short_name(vip).upper()}",
                        "url": f"https://{vip}/",
                        "category": "Networking",
                        "icon": "nsx",
                        "description": f"Cluster VIP - v{el.get('version', '?')}",
                        "order": 20,
                    })
                for node in el.get("nodes") or []:
                    nfqdn = node.get("fqdn")
                    if nfqdn and nfqdn != vip:
                        tiles.append({
                            "name": f"NSX Node {_short_name(nfqdn).upper()}",
                            "url": f"https://{nfqdn}/",
                            "category": "Networking",
                            "icon": "nsx",
                            "description": "NSX Manager node",
                            "order": 21,
                        })
            log.info("  %d NSX cluster(s)", len(nsx.get("elements") or []))
    _safe("NSX clusters", _nsx)

    # --- NSX ALB (Avi) ---
    def _alb():
        for alb_path in ("/v1/nsx-alb-clusters", "/v1/avi-clusters"):
            alb = client.get(alb_path)
            if not alb:
                continue
            for el in alb.get("elements") or []:
                fqdn = el.get("fqdn") or el.get("clusterIp") or el.get("vip")
                if fqdn:
                    tiles.append({
                        "name": f"NSX ALB {_short_name(fqdn).upper()}",
                        "url": f"https://{fqdn}/",
                        "category": "Networking",
                        "icon": "nsx-alb",
                        "description": "NSX Advanced Load Balancer (Avi)",
                        "order": 22,
                    })
            log.info("  %d NSX ALB", len(alb.get("elements") or []))
            break
    _safe("NSX ALB", _alb)

    # --- HCX Managers ---
    def _hcx():
        hcx = client.get("/v1/hcx-managers")
        if hcx:
            for el in hcx.get("elements") or []:
                fqdn = el.get("fqdn")
                if fqdn:
                    tiles.append({
                        "name": f"HCX Manager {_short_name(fqdn).upper()}",
                        "url": f"https://{fqdn}/",
                        "category": "Management",
                        "icon": "hcx",
                        "description": "VMware HCX",
                        "order": 30,
                    })
            log.info("  %d HCX Manager(s)", len(hcx.get("elements") or []))
    _safe("HCX Managers", _hcx)

    # --- Backup config ---
    def _backup():
        backup = client.get("/v1/system/backup-configuration")
        if backup:
            for cfg in backup.get("backupLocations") or []:
                srv = cfg.get("server")
                if srv:
                    tiles.append({
                        "name": f"Backup Target {srv}",
                        "url": f"sftp://{srv}",
                        "category": "Management",
                        "icon": "backup",
                        "description": f"SFTP :{cfg.get('port', 22)}",
                        "order": 40,
                    })
    _safe("Backup config", _backup)

    # --- VCF 9.1 VCFMS containerized services ---
    def _vsp():
        vsp = client.get("/v1/vsp-clusters")
        if vsp:
            clusters = vsp.get("elements") if isinstance(vsp, dict) else vsp
            if isinstance(clusters, list):
                for c in clusters:
                    spec = c.get("spec") or c
                    for key, label, icon, desc in [
                        ("platformFqdn", "VCFMS Platform", "vcfms-platform",
                         "VCF Services Runtime"),
                        ("instanceFqdn", "VCFMS Instance", "vcfms-instance",
                         "VCFMS Instance Component"),
                        ("fleetFqdn", "VCFMS Fleet", "vcfms-fleet",
                         "VCFMS Fleet Component"),
                    ]:
                        fqdn = spec.get(key)
                        if fqdn:
                            tiles.append({
                                "name": f"{label} {_short_name(fqdn).upper()}",
                                "url": f"https://{fqdn}/",
                                "category": "Management",
                                "icon": icon,
                                "description": desc,
                                "order": 50,
                            })
    _safe("VCFMS clusters", _vsp)

    # --- VCF Management Components (from deployment task, VCF 9.x) ---
    def _mgmt():
        """
        Query GET /v1/vcf-management-components (the ROOT endpoint, without /tasks).
        Documented in VCF 9.1 SDDC Manager API to return an object with the
        current deployment state of every VCF Management Component:
          vcfOperations, vcfOperationsFleetManagement, vcfOperationsCollector,
          vcfOperationsLogs, vcfAutomation, vidb (Identity Broker),
          vspCluster, sddcLcm, fleetLcm, telemetryAcceptor, salt, saltRaas.
        Each entry has fqdn (or nodes/loadBalancerFqdn for VCF Ops) +
        deploymentStatus + deploymentType. Components that were NOT deployed
        typically have deploymentStatus == "NOT_STARTED" or are missing.

        This works for greenfield 9.x deployments where /v1/vcf-management-components/tasks
        returns an empty list. Also falls back to /tasks/{id}/spec for older builds.
        """
        body = client.get("/v1/vcf-management-components")

        # If root endpoint returns the current-state object, parse it.
        # Distinguish between "state object" (has known component keys) and
        # "task list" (has {"elements": [...]}).
        state = None
        if isinstance(body, dict):
            known_keys = {"vcfOperations", "vcfOperationsFleetManagement",
                          "vcfOperationsCollector", "vcfOperationsLogs",
                          "vcfAutomation", "vidb", "vspCluster",
                          "sddcLcm", "fleetLcm"}
            if any(k in body for k in known_keys):
                state = body

        # Fallback: legacy /tasks/{id}/spec route (returns *Spec objects with
        # hostname fields instead of fqdn)
        if not state:
            legacy_spec = _fetch_vcf_management_spec_from_tasks(client)
            if legacy_spec:
                # Normalize legacy spec to same shape as state object
                state = _normalize_legacy_spec(legacy_spec)

        if not state:
            return

        # Component map: state_key -> (label, icon, category, description, order)
        component_map: list[tuple[str, str, str, str, str, int]] = [
            ("vcfOperations",
             "VCF Operations", "vcf-ops", "Monitoring",
             "VCF Operations (formerly Aria Operations)", 60),
            ("vcfOperationsCollector",
             "Cloud Proxy", "vcf-ops", "Monitoring",
             "VCF Operations Collector / Cloud Proxy", 63),
            ("vcfOperationsFleetManagement",
             "Fleet Management", "vcfms-fleet", "Management",
             "VCF Operations Fleet Management appliance", 65),
            ("vcfOperationsLogs",
             "VCF Operations for Logs", "log-insight", "Monitoring",
             "VCF Operations for Logs (formerly vRLI)", 66),
            ("vcfOperationsForNetworks",
             "VCF Operations for Networks", "network-insight", "Monitoring",
             "VCF Operations for Networks (formerly vRNI)", 67),
            ("vcfAutomation",
             "VCF Automation", "vcf-automation", "Automation",
             "VCF Automation (formerly Aria Automation)", 70),
            ("vidb",
             "VCF Identity Broker", "identity-broker", "Management",
             "VCF Identity Broker (vIDB)", 71),
            ("vcfLicenseServer",
             "VCF License Server", "license-server", "Management",
             "VCF License Server", 72),
            ("licenseServer",
             "VCF License Server", "license-server", "Management",
             "VCF License Server", 72),
        ]

        added = 0
        for key, label, icon, category, desc, order in component_map:
            comp = state.get(key)
            if not isinstance(comp, dict):
                continue

            status = (comp.get("deploymentStatus")
                      or comp.get("status") or "").upper()
            # Skip components that were never deployed
            if status in ("NOT_STARTED", "SKIPPED", "NOT_FOUND"):
                log.debug("  %s: %s -> skipping", key, status)
                continue

            # Prefer loadBalancerFqdn for HA clusters (VCF Operations),
            # fall back to fqdn, then first node's fqdn
            primary_fqdn = (comp.get("loadBalancerFqdn")
                            or comp.get("fqdn")
                            or comp.get("hostname"))
            nodes = comp.get("nodes") or []
            if not primary_fqdn and nodes and isinstance(nodes[0], dict):
                primary_fqdn = nodes[0].get("fqdn") or nodes[0].get("hostname")

            if not primary_fqdn:
                # No FQDN - component either not deployed or infrastructure-only
                # (e.g. telemetryAcceptor, salt) - skip silently
                log.debug("  %s: no FQDN, status=%s", key, status)
                continue

            # Annotate description with status if not fully deployed
            desc_full = desc
            if status and status not in ("SUCCEEDED", "SUCCESSFUL",
                                          "COMPLETED", "ACTIVE", "OK", ""):
                desc_full = f"{desc} ({status})"

            tiles.append({
                "name": f"{label} {_short_name(primary_fqdn).upper()}",
                "url": f"https://{primary_fqdn}/",
                "category": category,
                "icon": icon,
                "description": desc_full,
                "order": order,
            })
            added += 1

            # Also emit tile per node when VCF Ops HA cluster has separate FQDNs
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                nfqdn = node.get("fqdn") or node.get("hostname")
                if nfqdn and nfqdn != primary_fqdn:
                    ntype = node.get("type", "node")
                    tiles.append({
                        "name": f"{label} node {_short_name(nfqdn).upper()}",
                        "url": f"https://{nfqdn}/",
                        "category": category,
                        "icon": icon,
                        "description": f"{label} {ntype} node",
                        "order": order + 1,
                    })
                    added += 1

        if added:
            log.info("  %d VCF Management Component tile(s)", added)
        else:
            log.info("  VCF Management Components endpoint returned state, "
                     "but no components have been deployed yet.")
    _safe("VCF Management Components", _mgmt)

    # --- VCF 9.x singleton endpoints for fleet components ---
    # In VCF 9.0/9.1 each fleet component has a singleton GET endpoint that
    # returns the current deployment info (fqdn, status, type). These work
    # even for greenfield VCF Installer deployments where the
    # /v1/vcf-management-components/tasks list is empty.
    #
    # Data structure e.g. VcfOperationsFleetManagement:
    #   { "fqdn": "vcf-operations-fleet-management.rainpole.io",
    #     "deploymentStatus": "SUCCEEDED", "deploymentType": "NEW" }
    def _vcf_singletons():
        # (endpoint_path, label, icon, category, description)
        singletons = [
            ("/v1/vcf-operations",
             "VCF Operations", "vcf-ops", "Monitoring",
             "VCF Operations (formerly Aria Operations)"),
            ("/v1/vcf-operations-fleet-management",
             "VCF Operations Fleet Management", "vcfms-fleet", "Management",
             "Fleet Management appliance"),
            ("/v1/vcf-operations-collector",
             "Cloud Proxy", "vcf-ops", "Monitoring",
             "VCF Operations Collector / Cloud Proxy"),
            ("/v1/vcf-operations-for-logs",
             "VCF Operations for Logs", "log-insight", "Monitoring",
             "VCF Operations for Logs (formerly Aria Ops for Logs / vRLI)"),
            ("/v1/vcf-operations-for-networks",
             "VCF Operations for Networks", "network-insight", "Monitoring",
             "VCF Operations for Networks (formerly Aria Ops for Networks / vRNI)"),
            ("/v1/vcf-automation",
             "VCF Automation", "vcf-automation", "Automation",
             "VCF Automation (formerly Aria Automation / vRA)"),
            ("/v1/vcf-identity-broker",
             "VCF Identity Broker", "identity-broker", "Management",
             "VCF Identity Broker (vIDB)"),
            ("/v1/vcf-license-server",
             "VCF License Server", "license-server", "Management",
             "VCF License Server"),
            # Alternative paths (some builds use different naming)
            ("/v1/license-server",
             "VCF License Server", "license-server", "Management",
             "VCF License Server"),
        ]
        found_count = 0
        for path, label, icon, category, desc in singletons:
            data = client.get(path)
            if not data:
                continue

            # Response can be:
            #   - a single object: {"fqdn": "...", "nodes": [...], "loadBalancerFqdn": "..."}
            #   - a paged list: {"elements": [{...}]}
            #   - a plain list: [{...}]
            items: list = []
            if isinstance(data, dict):
                if "elements" in data:
                    items = data.get("elements") or []
                else:
                    items = [data]
            elif isinstance(data, list):
                items = data

            for item in items:
                if not isinstance(item, dict):
                    continue
                # Try common FQDN field names, prefer LB / VIP
                fqdn = (item.get("loadBalancerFqdn")
                        or item.get("vipFqdn")
                        or item.get("fqdn")
                        or item.get("hostname")
                        or item.get("applianceFqdn"))
                if not fqdn:
                    # Try nested nodes array
                    nodes = item.get("nodes") or []
                    if nodes and isinstance(nodes[0], dict):
                        fqdn = (nodes[0].get("fqdn")
                                or nodes[0].get("hostname"))
                if not fqdn:
                    continue

                short = _short_name(fqdn).upper()
                # Add status hint to description if failed / in progress
                status = item.get("deploymentStatus") or item.get("status")
                desc_full = desc
                if status and status not in ("SUCCEEDED", "SUCCESSFUL",
                                             "COMPLETED", "ACTIVE", "OK"):
                    desc_full = f"{desc} ({status})"

                tiles.append({
                    "name": f"{label} {short}",
                    "url": f"https://{fqdn}/",
                    "category": category,
                    "icon": icon,
                    "description": desc_full,
                    "order": 35,
                })
                found_count += 1

                # Also emit tiles for each individual node when the response
                # has multiple nodes (e.g. VCF Ops HA cluster)
                nodes = item.get("nodes") or []
                for node in nodes:
                    if not isinstance(node, dict):
                        continue
                    nfqdn = node.get("fqdn") or node.get("hostname")
                    if nfqdn and nfqdn != fqdn:
                        tiles.append({
                            "name": f"{label} node {_short_name(nfqdn).upper()}",
                            "url": f"https://{nfqdn}/",
                            "category": category,
                            "icon": icon,
                            "description": f"{label} {node.get('type', 'node')}",
                            "order": 36,
                        })
        if found_count:
            log.info("  %d VCF 9.x fleet component(s) via singleton endpoints",
                     found_count)
    _safe("VCF 9.x singleton endpoints", _vcf_singletons)

    # --- Aria / vRealize products (VCF 4.x / 5.x) ---
    # Endpoints VRSLCM/WSA/VROPS/VRLI/VRA are on SDDC Manager for classic
    # Aria stack. In 9.x they were retired in favour of VCFMS + VCF Ops.
    # Falls back to /v1/credentials?resourceType=X to extract the FQDN
    # from stored credential's resourceName.
    def _aria():
        products = [
            # (resourceType, label, icon, category, description)
            ("VRSLCM", "Aria Suite Lifecycle", "aria-suite", "Management",
             "VMware Aria Suite Lifecycle (vRSLCM)"),
            ("WSA", "Workspace ONE Access", "identity-broker", "Management",
             "Workspace ONE Access (WSA)"),
            ("VROPS", "Aria Operations", "vcf-ops", "Monitoring",
             "vRealize / Aria Operations"),
            ("VRLI", "Aria Operations for Logs", "log-insight", "Monitoring",
             "vRealize Log Insight"),
            ("VRA", "Aria Automation", "vcf-automation", "Automation",
             "vRealize / Aria Automation"),
        ]
        # Dedicated endpoints (both singular and plural forms are seen)
        endpoint_map = {
            "VRSLCM": ("/v1/vrslcm", "/v1/vrslcms"),
            "WSA":    ("/v1/wsa", "/v1/wsas"),
            "VROPS":  ("/v1/vrops", "/v1/vropses"),
            "VRLI":   ("/v1/vrli", "/v1/vrlis"),
            "VRA":    ("/v1/vra", "/v1/vras"),
        }

        found: dict[str, str] = {}
        # Try dedicated endpoints first
        for rt, *_ in products:
            for ep in endpoint_map.get(rt, ()):
                data = client.get(ep)
                if not data:
                    continue
                items = (data.get("elements") if isinstance(data, dict)
                         else data if isinstance(data, list) else [data])
                if isinstance(data, dict) and "elements" not in data:
                    items = [data]
                for item in items or []:
                    if not isinstance(item, dict):
                        continue
                    fqdn = (item.get("loadBalancerFqdn")
                            or item.get("fqdn")
                            or item.get("hostname"))
                    if not fqdn:
                        nodes = item.get("nodes") or []
                        if nodes and isinstance(nodes[0], dict):
                            fqdn = nodes[0].get("fqdn") or nodes[0].get("hostname")
                    if fqdn:
                        found[rt] = fqdn
                        break
                if rt in found:
                    break

        # Fallback: query credentials, extract resourceName that looks like FQDN.
        # VCF 4.x/5.x uses old resourceType values (VRSLCM, WSA, VROPS, VRLI, VRA).
        # VCF 9.x may use renamed ones (VCF_OPERATIONS, VCF_AUTOMATION, etc.);
        # we try both since the API just ignores unknown values.
        rt_alias = {
            "VRSLCM": ["VRSLCM", "VCF_OPERATIONS_FLEET_MANAGEMENT"],
            "WSA":    ["WSA", "VCF_IDENTITY_BROKER"],
            "VROPS":  ["VROPS", "VCF_OPERATIONS"],
            "VRLI":   ["VRLI", "VCF_OPERATIONS_FOR_LOGS"],
            "VRA":    ["VRA", "VCF_AUTOMATION"],
        }
        for rt, *_ in products:
            if rt in found:
                continue
            for alias in rt_alias.get(rt, [rt]):
                creds = client.get(
                    f"/v1/credentials?resourceType={alias}&pageSize=5")
                if not creds:
                    continue
                for cred in creds.get("elements") or []:
                    res = cred.get("resource") or {}
                    name = res.get("resourceName") or res.get("resourceIp")
                    if name and "." in name and not name.endswith("."):
                        found[rt] = name
                        break
                if rt in found:
                    break

        for rt, label, icon, category, desc in products:
            fqdn = found.get(rt)
            if not fqdn:
                continue
            tiles.append({
                "name": f"{label} {_short_name(fqdn).upper()}",
                "url": f"https://{fqdn}/",
                "category": category,
                "icon": icon,
                "description": desc,
                "order": 32,
            })
        if found:
            log.info("  %d Aria / vRealize product(s): %s",
                     len(found), ", ".join(sorted(found.keys())))
    _safe("Aria / vRealize products", _aria)

    # --- Deduplicate by URL, keep first ---
    seen: set[str] = set()
    unique: list[dict] = []
    for t in tiles:
        url = t["url"]
        if url in seen:
            continue
        seen.add(url)
        unique.append(t)
    return unique


# Map VCF Operations "appliance" field to launcher tile info
# (based on managed account records from
# /suite-api/api/fleet-management/password-management/accounts/query)
_VCFOPS_APPLIANCE_MAP: dict[str, tuple[str, str, str, str]] = {
    # appliance_upper: (label, icon, category, description)
    "OPERATIONS_MANAGER":       ("VCF Operations", "vcf-ops", "Monitoring",
                                 "VCF Operations"),
    "VCF_OPERATIONS":           ("VCF Operations", "vcf-ops", "Monitoring",
                                 "VCF Operations"),
    "OPERATIONS":               ("VCF Operations", "vcf-ops", "Monitoring",
                                 "VCF Operations"),
    "OPERATIONS_FLEET_MANAGEMENT": ("Fleet Management", "vcfms-fleet",
                                     "Management", "Fleet Management appliance"),
    "FLEET_MANAGEMENT":         ("Fleet Management", "vcfms-fleet",
                                 "Management", "Fleet Management appliance"),
    "OPERATIONS_COLLECTOR":     ("Cloud Proxy", "vcf-ops", "Monitoring",
                                 "VCF Operations Collector"),
    "CLOUD_PROXY":              ("Cloud Proxy", "vcf-ops", "Monitoring",
                                 "VCF Operations Collector"),
    "OPERATIONS_FOR_LOGS":      ("VCF Operations for Logs", "log-insight",
                                 "Monitoring",
                                 "VCF Operations for Logs (formerly vRLI)"),
    "OPERATIONS_FOR_NETWORKS":  ("VCF Operations for Networks", "network-insight",
                                 "Monitoring",
                                 "VCF Operations for Networks (formerly vRNI)"),
    "AUTOMATION":               ("VCF Automation", "vcf-automation",
                                 "Automation",
                                 "VCF Automation (formerly Aria Automation)"),
    "VCF_AUTOMATION":           ("VCF Automation", "vcf-automation",
                                 "Automation", "VCF Automation"),
    "IDENTITY_BROKER":          ("VCF Identity Broker", "identity-broker",
                                 "Management", "VCF Identity Broker (vIDB)"),
    "VCF_IDENTITY_BROKER":      ("VCF Identity Broker", "identity-broker",
                                 "Management", "VCF Identity Broker"),
    "LICENSE_SERVER":           ("VCF License Server", "license-server",
                                 "Management", "VCF License Server"),
    "VCF_LICENSE_SERVER":       ("VCF License Server", "license-server",
                                 "Management", "VCF License Server"),
    "VRSLCM":                   ("Aria Suite Lifecycle", "aria-suite",
                                 "Management",
                                 "VMware Aria Suite Lifecycle (vRSLCM)"),
    "WSA":                      ("Workspace ONE Access", "identity-broker",
                                 "Management", "Workspace ONE Access (WSA)"),
    "HCX":                      ("HCX Manager", "hcx", "Management",
                                 "VMware HCX"),
}

# Appliance types that SDDC Manager already covers - skip to avoid dupes
_VCFOPS_APPLIANCE_SKIP: set[str] = {
    "ESX", "ESXI", "HOST",
    "VC", "VCENTER", "VCENTER_SERVER", "VSPHERE",
    "NSX", "NSX_T", "NSXT", "NSX_MANAGER", "NSXT_MANAGER",
    "NSX_EDGE", "NSXT_EDGE", "NSX_ALB", "AVI",
    "SDDC_MANAGER", "SDDCM",
    "BACKUP",
}


def discover_from_vcfops(host: str, username: str, password: str,
                         auth_source: str = "LOCAL",
                         verify_tls: bool = True,
                         page_size: int = 200) -> list[dict]:
    """
    Connect to VCF Operations, query the fleet-management password API,
    extract unique applianceFqdn + appliance type from all managed accounts
    and return launcher tiles for the fleet components.

    Appliance types corresponding to SDDC-Manager-discovered resources
    (ESX, vCenter, NSX, etc.) are skipped.

    Uses the response schema documented for VCF 9.1:
      { "vcfPasswordAccounts": [
          { "applianceFqdn": "...",
            "appliance": "OPERATIONS_MANAGER" | "AUTOMATION" | ... ,
            "userName": "...", "status": "ACTIVE", ... }
        ] }
    """
    tiles: list[dict] = []

    ops = VCFOpsClient(host=host, username=username, password=password,
                       auth_source=auth_source, verify_tls=verify_tls)
    try:
        ops.login()
    except (RequestException, RuntimeError, HTTPError) as e:
        log.error("VCF Operations login failed: %s", e)
        return tiles
    try:
        accounts = ops.list_managed_accounts(page_size=page_size)
    finally:
        ops.logout()

    if not accounts:
        log.warning("VCF Operations returned no managed accounts "
                    "(either empty or account query denied for this user).")
        return tiles

    log.info("  fetched %d managed account record(s) from VCF Operations",
             len(accounts))

    # Group by applianceFqdn - one tile per unique appliance
    seen: dict[str, str] = {}
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        fqdn = acc.get("applianceFqdn") or acc.get("fqdn")
        appliance = (acc.get("appliance")
                     or acc.get("applianceType")
                     or acc.get("credentialType") or "")
        appliance_up = str(appliance).upper()
        if not fqdn or not isinstance(fqdn, str):
            continue
        if fqdn in seen:
            continue
        seen[fqdn] = appliance_up

    log.info("  %d unique appliance FQDN(s) after grouping", len(seen))

    added = 0
    for fqdn, appliance_up in seen.items():
        if appliance_up in _VCFOPS_APPLIANCE_SKIP:
            continue
        target_url = f"https://{fqdn}/"

        if appliance_up in _VCFOPS_APPLIANCE_MAP:
            label, icon, category, desc = _VCFOPS_APPLIANCE_MAP[appliance_up]
        else:
            # Unknown appliance type - keep it as generic under Management
            # so it still appears in the launcher
            pretty = (appliance_up.replace("_", " ").title()
                      if appliance_up else "Fleet Appliance")
            label = pretty
            icon = "generic"
            category = "Management"
            desc = f"Fleet appliance (type: {appliance_up or 'unknown'})"

        tiles.append({
            "name": f"{label} {_short_name(fqdn).upper()}",
            "url": target_url,
            "category": category,
            "icon": icon,
            "description": desc,
            "order": 33,
        })
        added += 1

    log.info("  produced %d fleet tile(s) from VCF Operations (skipped known "
             "SDDC-covered types)", added)
    return tiles


def _fetch_vcf_management_spec_from_tasks(client: SDDCClient) -> dict | None:
    """
    Legacy path: only works when SDDC Manager has driven the deployment
    (not for greenfield 9.x via VCF Installer). Iterates through
    /v1/vcf-management-components/tasks and fetches the spec of the
    latest task. Returns the *Spec-shaped object.
    """
    body = client.get("/v1/vcf-management-components/tasks")
    if not body:
        return None
    tasks = body.get("elements") if isinstance(body, dict) else body
    if not isinstance(tasks, list) or not tasks:
        return None
    tasks_sorted = sorted(
        tasks,
        key=lambda t: t.get("creationTimestamp")
                      or t.get("modificationTimestamp") or "",
        reverse=True,
    )
    for t in tasks_sorted:
        tid = t.get("id") or t.get("taskId")
        if not tid:
            continue
        spec = client.get(f"/v1/vcf-management-components/tasks/{tid}/spec")
        if spec:
            return spec
    return None


def _normalize_legacy_spec(spec: dict) -> dict:
    """
    The legacy /tasks/{id}/spec endpoint returns *Spec objects with 'hostname'
    fields. Normalize to the same shape as GET /v1/vcf-management-components
    root endpoint response (with 'fqdn' fields) so downstream code has one path.
    """
    def _lift(src_key: str, dst_key: str) -> tuple[str, dict] | None:
        obj = spec.get(src_key)
        if not isinstance(obj, dict):
            return None
        norm: dict = {"deploymentStatus": "UNKNOWN"}
        if obj.get("hostname"):
            norm["fqdn"] = obj["hostname"]
        if obj.get("loadBalancerFqdn"):
            norm["loadBalancerFqdn"] = obj["loadBalancerFqdn"]
        nodes = obj.get("nodes") or []
        if nodes:
            norm["nodes"] = [
                {"fqdn": n.get("hostname"), "type": n.get("type")}
                for n in nodes if isinstance(n, dict) and n.get("hostname")
            ]
        return dst_key, norm

    out: dict = {}
    for src, dst in [
        ("vcfOperationsSpec",                    "vcfOperations"),
        ("vcfOperationsFleetManagementSpec",     "vcfOperationsFleetManagement"),
        ("vcfOperationsCollectorSpec",           "vcfOperationsCollector"),
        ("vcfOperationsLogsSpec",                "vcfOperationsLogs"),
        ("vcfOperationsForNetworksSpec",         "vcfOperationsForNetworks"),
        ("vcfAutomationSpec",                    "vcfAutomation"),
        ("vcfIdentityBrokerSpec",                "vidb"),
        ("vcfLicenseServerSpec",                 "vcfLicenseServer"),
        ("licenseServerSpec",                    "licenseServer"),
    ]:
        result = _lift(src, dst)
        if result:
            out[result[0]] = result[1]
    return out


def _fetch_vcf_management_spec(client: SDDCClient) -> dict | None:
    """Back-compat wrapper: retained in case other code paths call it."""
    return _fetch_vcf_management_spec_from_tasks(client)


# ---------- custom links ----------

def load_custom_links(path: str) -> list[dict]:
    if not path or not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    links = data.get("links") or data.get("custom_links") or []
    out: list[dict] = []
    for link in links:
        if not link.get("url") or not link.get("name"):
            continue
        out.append({
            "name": link["name"],
            "url": link["url"],
            "category": link.get("category") or "Websites",
            "icon": link.get("icon") or "generic",
            "description": link.get("description") or "",
            "order": 100,
        })
    return out


# ---------- HTML generation ----------

def get_icon_svg(key: str) -> str:
    return ICONS.get(key) or ICONS["generic"]


def render_html(tiles: list[dict], sddc_host: str, vcf_version: str) -> str:
    # Group by category
    cats: dict[str, list[dict]] = {c: [] for c in CATEGORY_ORDER}
    for t in tiles:
        c = t.get("category") or "Websites"
        cats.setdefault(c, []).append(t)
    # Sort tiles inside each category
    for c in cats:
        cats[c].sort(key=lambda x: (x.get("order", 99), x["name"].lower()))

    generated_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Build tile HTML
    def tile_html(t: dict) -> str:
        icon_svg = get_icon_svg(t["icon"])
        cat = t.get("category", "Websites")
        color = CATEGORY_COLORS.get(cat, "#6B7280")
        name = _html_escape(t["name"])
        desc = _html_escape(t.get("description", ""))
        url = _html_escape(t["url"])
        return (
            f'<a class="tile" href="{url}" target="_blank" rel="noopener noreferrer" '
            f'data-cat="{_html_escape(cat)}" data-name="{name.lower()}">'
            f'<div class="tile-icon" style="color:{color}">{icon_svg}</div>'
            f'<div class="tile-body">'
            f'<div class="tile-name">{name}</div>'
            f'<div class="tile-desc">{desc}</div>'
            f'</div>'
            f'<button class="star" data-url="{url}" title="Bookmark" aria-label="Bookmark">'
            f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">'
            f'<path d="M12 17.3l-6.2 3.5 1.6-7L2 9l7.1-0.6L12 2l2.9 6.4L22 9l-5.4 4.8 1.6 7z"/>'
            f'</svg>'
            f'</button>'
            f'</a>'
        )

    grid_sections = []
    for cat in CATEGORY_ORDER:
        items = cats.get(cat, [])
        if not items:
            continue
        grid_sections.append(
            f'<section class="grid-section" data-section="{_html_escape(cat)}">'
            f'<h2 class="section-title">{_html_escape(cat)}'
            f' <span class="count">{len(items)}</span></h2>'
            f'<div class="grid">{"".join(tile_html(t) for t in items)}</div>'
            f'</section>'
        )

    total = sum(len(v) for v in cats.values())

    return _HTML_TEMPLATE.format(
        title=f"VCF Launcher - {sddc_host}",
        sddc_host=_html_escape(sddc_host),
        vcf_version=_html_escape(vcf_version),
        generated_at=generated_at,
        total_tiles=total,
        grid_sections="".join(grid_sections),
    )


def _html_escape(s: str) -> str:
    return (str(s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;"))


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root {{
  --bg: #0b0d10;
  --surface: #14171c;
  --surface-2: #1a1f26;
  --border: #262d38;
  --text: #e6e9ee;
  --text-2: #9aa4b0;
  --accent: #3ea6ff;
  --shadow: 0 1px 3px rgba(0,0,0,0.4);
}}
[data-theme="light"] {{
  --bg: #f5f6f8;
  --surface: #ffffff;
  --surface-2: #f9fafb;
  --border: #e2e6ec;
  --text: #1a2029;
  --text-2: #5b6473;
  --accent: #0091DA;
  --shadow: 0 1px 3px rgba(0,0,0,0.08);
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  font-size: 14px; }}
a {{ color: inherit; text-decoration: none; }}
header.topbar {{ position: sticky; top: 0; z-index: 10; background: var(--surface);
  border-bottom: 1px solid var(--border); padding: 12px 24px;
  display: flex; align-items: center; gap: 16px; }}
.brand {{ font-weight: 500; font-size: 16px; letter-spacing: 0.2px;
  display: flex; align-items: center; gap: 10px; }}
.brand-badge {{ background: var(--accent); color: white; padding: 2px 8px;
  border-radius: 4px; font-size: 11px; font-weight: 500; }}
.env-info {{ font-size: 12px; color: var(--text-2); }}
.env-info b {{ color: var(--text); font-weight: 500; }}
.spacer {{ flex: 1; }}
input.search {{ background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 6px; padding: 7px 12px; color: var(--text);
  font-size: 13px; width: 260px; outline: none; }}
input.search:focus {{ border-color: var(--accent); }}
button.icon-btn {{ background: transparent; border: 1px solid var(--border);
  color: var(--text-2); border-radius: 6px; padding: 6px 10px; cursor: pointer;
  font-size: 12px; }}
button.icon-btn:hover {{ background: var(--surface-2); color: var(--text); }}

.layout {{ display: grid; grid-template-columns: 200px 1fr; gap: 0;
  min-height: calc(100vh - 53px); }}
nav.sidebar {{ background: var(--surface); border-right: 1px solid var(--border);
  padding: 16px 0; }}
nav.sidebar ul {{ list-style: none; margin: 0; padding: 0; }}
nav.sidebar li {{ padding: 9px 24px; cursor: pointer; font-size: 13px;
  color: var(--text-2); border-left: 3px solid transparent;
  display: flex; justify-content: space-between; align-items: center; }}
nav.sidebar li:hover {{ background: var(--surface-2); color: var(--text); }}
nav.sidebar li.active {{ color: var(--text); border-left-color: var(--accent);
  background: var(--surface-2); }}
nav.sidebar li .cat-count {{ font-size: 11px; color: var(--text-2);
  background: var(--surface-2); padding: 1px 6px; border-radius: 8px; }}
nav.sidebar li.active .cat-count {{ background: var(--bg); }}

main {{ padding: 24px 32px 48px; overflow-x: hidden; }}
.section-title {{ font-size: 12px; font-weight: 500; color: var(--text-2);
  text-transform: uppercase; letter-spacing: 1px; margin: 24px 0 12px;
  padding-bottom: 6px; border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 8px; }}
.section-title:first-child {{ margin-top: 0; }}
.section-title .count {{ background: var(--surface-2); color: var(--text-2);
  font-size: 10px; padding: 1px 6px; border-radius: 8px; letter-spacing: 0; }}

.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 14px; }}
.tile {{ background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px; display: flex; align-items: center;
  gap: 14px; transition: all 0.15s ease; cursor: pointer;
  position: relative; min-height: 76px; }}
.tile:hover {{ border-color: var(--accent); transform: translateY(-1px);
  box-shadow: var(--shadow); }}
.tile-icon {{ flex-shrink: 0; width: 44px; height: 44px;
  display: flex; align-items: center; justify-content: center;
  background: var(--surface-2); border-radius: 8px; }}
.tile-icon svg {{ width: 24px; height: 24px; }}
.tile-body {{ min-width: 0; flex: 1; }}
.tile-name {{ font-size: 13px; font-weight: 500; color: var(--text);
  line-height: 1.35;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden; word-break: break-word; }}
.tile-desc {{ font-size: 11px; color: var(--text-2); margin-top: 3px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.star {{ position: absolute; top: 8px; right: 8px; background: transparent;
  border: none; color: var(--text-2); padding: 3px; cursor: pointer;
  opacity: 0; transition: opacity 0.15s; border-radius: 4px; }}
.star:hover {{ background: var(--surface-2); color: var(--accent); }}
.star svg {{ width: 14px; height: 14px; }}
.tile:hover .star, .star.starred {{ opacity: 1; }}
.star.starred {{ color: #f7a400; }}

.empty {{ text-align: center; padding: 60px 24px; color: var(--text-2); }}
.empty h3 {{ font-weight: 500; margin: 0 0 8px; color: var(--text); }}
footer.foot {{ padding: 24px 32px; text-align: center; color: var(--text-2);
  font-size: 11px; border-top: 1px solid var(--border); margin-top: 32px; }}
</style>
</head>
<body>
<header class="topbar">
  <div class="brand">
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.6" style="color: var(--accent)">
      <rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/>
      <rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>
    </svg>
    VCF Launcher
    <span class="brand-badge">{vcf_version}</span>
  </div>
  <div class="env-info">
    <b>{sddc_host}</b> &middot; {total_tiles} tiles &middot; generated {generated_at}
  </div>
  <div class="spacer"></div>
  <input class="search" id="search" type="search" placeholder="Search tiles..." autocomplete="off">
  <button class="icon-btn" id="theme-toggle" title="Toggle theme">Theme</button>
</header>

<div class="layout">
  <nav class="sidebar">
    <ul id="cats"></ul>
  </nav>
  <main id="main">
    {grid_sections}
    <div class="empty" id="empty" style="display:none">
      <h3>No matching tiles</h3>
      <div>Try a different search or category.</div>
    </div>
  </main>
</div>

<footer class="foot">
  VCF Launcher &middot; self-contained offline dashboard &middot;
  bookmark tiles by clicking the star icon
</footer>

<script>
(function() {{
  var main = document.getElementById('main');
  var search = document.getElementById('search');
  var catsUl = document.getElementById('cats');
  var empty = document.getElementById('empty');
  var toggle = document.getElementById('theme-toggle');

  // Build category list from actual sections in DOM
  var sections = Array.from(document.querySelectorAll('.grid-section'));
  var catNames = ['All'].concat(sections.map(function(s) {{ return s.dataset.section; }}));

  function renderCats(active) {{
    catsUl.innerHTML = '';
    catNames.forEach(function(c) {{
      var li = document.createElement('li');
      li.textContent = c;
      var count = 0;
      if (c === 'All') {{
        count = document.querySelectorAll('.tile').length;
      }} else {{
        var sec = document.querySelector('.grid-section[data-section="'+c+'"]');
        if (sec) count = sec.querySelectorAll('.tile').length;
      }}
      var badge = document.createElement('span');
      badge.className = 'cat-count';
      badge.textContent = count;
      li.appendChild(badge);
      if (c === active) li.classList.add('active');
      li.addEventListener('click', function() {{ activeCat = c; apply(); }});
      catsUl.appendChild(li);
    }});
  }}

  var activeCat = 'All';
  var query = '';

  function apply() {{
    var anyVisible = false;
    sections.forEach(function(sec) {{
      var showSection = (activeCat === 'All' || activeCat === sec.dataset.section);
      var tiles = sec.querySelectorAll('.tile');
      var sectionHasMatch = false;
      tiles.forEach(function(t) {{
        var matches = !query || t.dataset.name.indexOf(query) !== -1;
        var show = showSection && matches;
        t.style.display = show ? '' : 'none';
        if (show) sectionHasMatch = true;
      }});
      sec.style.display = sectionHasMatch ? '' : 'none';
      if (sectionHasMatch) anyVisible = true;
    }});
    empty.style.display = anyVisible ? 'none' : '';
    renderCats(activeCat);
  }}

  search.addEventListener('input', function() {{
    query = search.value.trim().toLowerCase();
    apply();
  }});

  // Theme toggle - stored in localStorage if available, but harmless offline
  toggle.addEventListener('click', function() {{
    var cur = document.documentElement.dataset.theme;
    var next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try {{ localStorage.setItem('vcf-launcher-theme', next); }} catch(e) {{}}
  }});
  try {{
    var saved = localStorage.getItem('vcf-launcher-theme');
    if (saved) document.documentElement.dataset.theme = saved;
  }} catch(e) {{}}

  // Bookmarks - starred tiles float to top of "All" view
  var starred = {{}};
  try {{
    starred = JSON.parse(localStorage.getItem('vcf-launcher-stars') || '{{}}');
  }} catch(e) {{}}
  document.querySelectorAll('.star').forEach(function(btn) {{
    var url = btn.dataset.url;
    if (starred[url]) btn.classList.add('starred');
    btn.addEventListener('click', function(e) {{
      e.preventDefault();
      e.stopPropagation();
      if (starred[url]) delete starred[url];
      else starred[url] = 1;
      btn.classList.toggle('starred');
      try {{ localStorage.setItem('vcf-launcher-stars', JSON.stringify(starred)); }}
      catch(err) {{}}
    }});
  }});

  apply();
}})();
</script>
</body>
</html>
"""


# ---------- config JSON dump / load ----------

def dump_config(tiles: list[dict], sddc_host: str, vcf_version: str,
                path: str) -> None:
    """Write the discovered tiles + metadata to a JSON file for manual editing."""
    payload = {
        "_comment": (
            "VCF Launcher config. Edit freely - reorder tiles, rename them, "
            "change categories, icons, URLs. Then regenerate the HTML with "
            "'vcf_launcher.py --from-config <this-file>'. No SDDC Manager "
            "login is needed for --from-config."
        ),
        "_icon_choices": sorted(ICONS.keys()),
        "_category_choices": CATEGORY_ORDER,
        "sddc_host": sddc_host,
        "vcf_version": vcf_version,
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tiles": tiles,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_config(path: str) -> tuple[list[dict], str, str]:
    """
    Load tiles + metadata from a config JSON produced by dump_config.
    Returns (tiles, sddc_host, vcf_version). Missing fields fall back
    to sensible defaults.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    tiles = data.get("tiles") or []
    # Validate + sanitize each tile
    clean: list[dict] = []
    for t in tiles:
        if not isinstance(t, dict):
            continue
        if not t.get("url") or not t.get("name"):
            continue
        clean.append({
            "name": t["name"],
            "url": t["url"],
            "category": t.get("category") or "Websites",
            "icon": t.get("icon") or "generic",
            "description": t.get("description") or "",
            "order": t.get("order", 99),
        })
    sddc_host = data.get("sddc_host") or "manual-config"
    vcf_version = data.get("vcf_version") or "custom"
    return clean, sddc_host, vcf_version


# ---------- CLI ----------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate an offline HTML dashboard of VCF components.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Two-step workflow:\n"
               "  1) Discover from SDDC and dump the config to JSON:\n"
               "       vcf_launcher.py --host X --username Y --dump-config tiles.json\n"
               "  2) Edit tiles.json manually (add/remove/rename/reorder tiles), then:\n"
               "       vcf_launcher.py --from-config tiles.json\n"
               "  --from-config skips the SDDC Manager entirely - no login needed.",
    )
    p.add_argument("--host", help="SDDC Manager FQDN or IP (asks if omitted)")
    p.add_argument("--username", help="SSO user (e.g. administrator@vsphere.local)")
    p.add_argument("--password", help="Password (asks if omitted)")
    p.add_argument("--insecure", action="store_true",
                   help="Skip TLS verification (self-signed lab)")
    p.add_argument("--custom-links",
                   help="Path to JSON file with extra tiles (Zabbix, HPE OneView...). "
                        "Merged with discovered tiles.")

    ops = p.add_argument_group("vcf operations (VCF 9.x fleet inventory)")
    ops.add_argument("--vcfops-host",
                     help="VCF Operations FQDN. When set, script logs in there "
                          "and queries the fleet-management API for VCF Operations, "
                          "VCF Automation, VCF Identity Broker, License Server, "
                          "Cloud Proxy and other fleet components not exposed by "
                          "SDDC Manager on greenfield 9.x deployments.")
    ops.add_argument("--vcfops-username", default="admin",
                     help="VCF Operations user [admin]")
    ops.add_argument("--vcfops-password",
                     help="VCF Operations password (asks if --vcfops-host set)")
    ops.add_argument("--vcfops-auth-source", default="LOCAL",
                     help="VCF Operations authSource [LOCAL]")

    workflow = p.add_argument_group("config-based workflow")
    workflow.add_argument("--dump-config", metavar="PATH",
                          help="Discover components from SDDC Manager, save the "
                               "tile config as JSON to PATH and EXIT without "
                               "generating HTML. Edit the JSON manually, then "
                               "run again with --from-config to render.")
    workflow.add_argument("--from-config", metavar="PATH",
                          help="Skip SDDC Manager entirely and generate HTML "
                               "from a config JSON produced by --dump-config "
                               "(possibly hand-edited). No login needed.")

    p.add_argument("--output",
                   help="Output HTML file (default: vcf_launcher_<host>_<ts>.html "
                        "in current directory)")
    p.add_argument("--no-color", action="store_true", help="Disable colored logs")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return p.parse_args()


def default_output_path(host: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", host).strip("_") or "vcf"
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return os.path.join(os.getcwd(), f"vcf_launcher_{safe}_{ts}.html")


def setup_logging(verbose: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(ColorFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def main() -> int:
    args = parse_args()
    global _USE_COLOR
    if args.no_color:
        _USE_COLOR = False
    setup_logging(args.verbose)

    if args.insecure:
        requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

    # ---------- MODE: --from-config (no SDDC Manager at all) ----------
    if args.from_config:
        if not os.path.exists(args.from_config):
            log.error("Config file not found: %s", args.from_config)
            return 2
        info(f"Loading tiles from {args.from_config} ...")
        try:
            tiles, sddc_host, vcf_version = load_config(args.from_config)
        except (json.JSONDecodeError, OSError) as e:
            log.error("Failed to load config: %s", e)
            return 2

        # Optional: merge custom links even in from-config mode
        if args.custom_links:
            custom = load_custom_links(args.custom_links)
            tiles.extend(custom)
            log.info("Added %d custom tiles from %s",
                     len(custom), args.custom_links)

        log.info("Loaded %d tile(s) from config.", len(tiles))
        output_path = args.output or default_output_path(sddc_host)
        html = render_html(tiles, sddc_host, vcf_version)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)
        ok(f"Wrote {len(tiles)} tiles to {output_path}")
        info(f"Open in a browser: file:///{output_path.replace(os.sep, '/')}")
        return 0

    # ---------- MODE: normal / --dump-config (needs SDDC Manager) ----------
    # Interactive fill-in for missing SDDC credentials
    if not args.host:
        args.host = ask("SDDC Manager FQDN or IP")
        if not args.host:
            log.error("Host is required.")
            return 2
    if not args.insecure:
        if ask_yes_no("Skip TLS verification (self-signed lab)?", default=True):
            args.insecure = True
            requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
    if not args.username:
        args.username = ask("Username", default="administrator@vsphere.local")
    if not args.password:
        args.password = ask_password(f"Password for {args.username}")
    if not args.custom_links and not args.dump_config:
        # Auto-detect default custom_links.json in cwd (skip for dump-config
        # so the dumped file stays clean discovery-only)
        default_cl = "custom_links.json"
        if os.path.exists(default_cl):
            if ask_yes_no(f"Load custom links from {default_cl}?", default=True):
                args.custom_links = default_cl

    client = SDDCClient(args.host, verify_tls=not args.insecure)
    try:
        client.login(args.username, args.password)
        version = client.detect_version()
        log.info("VCF version: %s", version)

        info("Discovering components ...")
        tiles = discover_components(client)
        log.info("Discovered %d unique component tiles from SDDC Manager.",
                 len(tiles))

        # ---------- Optional: query VCF Operations for fleet inventory ----------
        # Prompt on VCF 9.x if not given via CLI (SDDC Manager's own /v1/vcf-*
        # singletons are missing on many 9.x builds, so this is the reliable
        # way to enumerate VCF Operations / Automation / Identity Broker /
        # License Server / Cloud Proxy etc.)
        want_vcfops = bool(args.vcfops_host)
        if not want_vcfops and version.startswith("9") and not args.dump_config:
            info("Some VCF 9.x fleet components (VCF Operations, VCF Automation, "
                 "Identity Broker, License Server, Cloud Proxy) are not always "
                 "exposed by SDDC Manager and must be pulled from VCF Operations.")
            if ask_yes_no("Also query VCF Operations for fleet inventory?",
                          default=True):
                # Prefer FQDNs already discovered from SDDC Manager over guessing.
                # In VCF 9.1 with VCFMS, the suite-api may live on the VCFMS
                # Fleet FQDN, on a dedicated VCF Operations appliance, or on
                # the VCFMS Platform. Show all candidates and default to the
                # best one.
                default_host, candidates = _pick_vcfops_candidates(tiles,
                                                                    args.host)
                if candidates:
                    info("Candidate FQDNs from discovery "
                         "(any of these may host the suite-api):")
                    for fqdn, label in candidates:
                        info(f"  - {fqdn}  ({label})")
                elif default_host:
                    info(f"No fleet-related FQDNs discovered; "
                         f"heuristic guess: {default_host}")

                if default_host:
                    args.vcfops_host = ask("VCF Operations FQDN or IP",
                                           default=default_host)
                else:
                    args.vcfops_host = ask("VCF Operations FQDN or IP")
                args.vcfops_username = ask("VCF Operations user",
                                           default=args.vcfops_username or "admin")
                args.vcfops_password = ask_password(
                    f"VCF Operations password for {args.vcfops_username}")
                want_vcfops = True
        # For 9.x with dump-config, also allow the query if CLI args are given
        elif not want_vcfops and version.startswith("9") and args.dump_config:
            log.info("(--dump-config: skipping VCF Operations prompt; use "
                     "--vcfops-host/--vcfops-password to include fleet inventory)")

        if want_vcfops and args.vcfops_host:
            if not args.vcfops_password:
                args.vcfops_password = ask_password(
                    f"VCF Operations password for {args.vcfops_username}"
                    f"@{args.vcfops_host}")

            # Build list of hosts to try: user's choice first, then other
            # discovered candidates as fallback. This handles the common case
            # where the default suggestion doesn't resolve or doesn't run
            # suite-api (e.g. user picks VCFMS Fleet but it's actually on
            # VCFMS Platform or vice versa).
            hosts_to_try = [args.vcfops_host]
            _, all_candidates = _pick_vcfops_candidates(tiles, args.host)
            for fqdn, _label in all_candidates:
                if fqdn not in hosts_to_try:
                    hosts_to_try.append(fqdn)

            ops_tiles: list[dict] = []
            for i, host in enumerate(hosts_to_try, 1):
                if i == 1:
                    info(f"Querying VCF Operations at {host} ...")
                else:
                    info(f"Trying next candidate ({i}/{len(hosts_to_try)}): "
                         f"{host} ...")
                ops_tiles = discover_from_vcfops(
                    host=host,
                    username=args.vcfops_username,
                    password=args.vcfops_password,
                    auth_source=args.vcfops_auth_source,
                    verify_tls=not args.insecure,
                )
                if ops_tiles:
                    args.vcfops_host = host
                    break

            # Merge, but skip if URL already in tiles
            existing = {t["url"] for t in tiles}
            added = 0
            for t in ops_tiles:
                if t["url"] in existing:
                    continue
                tiles.append(t)
                existing.add(t["url"])
                added += 1
            log.info("Merged %d new tile(s) from VCF Operations "
                     "(after dedup vs SDDC-discovered).", added)

        if args.custom_links:
            custom = load_custom_links(args.custom_links)
            tiles.extend(custom)
            log.info("Added %d custom tiles from %s",
                     len(custom), args.custom_links)

        # ---------- SUB-MODE: --dump-config ----------
        if args.dump_config:
            dump_config(tiles, args.host, version, args.dump_config)
            ok(f"Wrote {len(tiles)} tile(s) as config JSON to {args.dump_config}")
            info("Edit that file, then re-run:")
            info(f"  vcf_launcher.py --from-config {args.dump_config}")
            return 0

        if not tiles:
            log.warning("No tiles to render. Output will be an empty dashboard.")

        output_path = args.output or default_output_path(args.host)
        html = render_html(tiles, args.host, version)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)
        ok(f"Wrote {len(tiles)} tiles to {output_path}")
        info(f"Open in a browser: file:///{output_path.replace(os.sep, '/')}")
        return 0

    except HTTPError as e:
        body = ""
        try:
            body = e.response.text[:400] if e.response is not None else ""
        except Exception:
            pass
        status = e.response.status_code if e.response is not None else "?"
        log.error("HTTP %s: %s", status, body)
        return 3
    except Exception as e:
        log.error("Error: %s", e)
        return 1
    finally:
        client.logout()


if __name__ == "__main__":
    sys.exit(main())
