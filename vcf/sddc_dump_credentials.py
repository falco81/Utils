#!/usr/bin/env python3
"""
sddc_dump_credentials.py
========================

Retrieve all credentials from a VMware Cloud Foundation SDDC Manager via the
official REST API. Supports VCF 4.x, 5.x, 9.0 and 9.1.

Endpoints (stable across VCF 4.0 - 9.1):
  POST   /v1/tokens                          - Create Token Pair
  PATCH  /v1/tokens/access-token/refresh     - Refresh Access Token
  DELETE /v1/tokens/refresh-token            - Invalidate Refresh Token
  GET    /v1/credentials                     - Get Credentials
  GET    /v1/sddc-managers                   - SDDC Manager info (used for
                                               version detection)

Verified from official Broadcom documentation:
  - VCF 9.0 / 9.1: full schema on developer.broadcom.com
  - VCF 4.0 / 5.1 / 5.2: endpoints and request/response bodies from VMware
    blogs and official examples (PowerVCF, lookup_passwords articles)

Features that appeared gradually (see VCF_CAPS in code):
  - apiKey auth:            VCF 4.4+
  - idToken (SSO) auth:     VCF 9.0+
  - autoRotatePolicy field: VCF 5.0+
  - accountType filter:     VCF 5.0+
  - connectivityStatus:     VCF 5.0+

Examples:
  # Auto-detect version, username/password, auto-generated output path
  # (creates ./credentials_sddc.example.local_2026-06-29_14-30-25.json)
  python sddc_dump_credentials.py --host sddc.example.local ^
      --username administrator@vsphere.local --insecure

  # Explicit output file, VCF 4.x (no apiKey/idToken, no accountType filter)
  python sddc_dump_credentials.py --host sddc.example.local --vcf 4 ^
      --username administrator@vsphere.local --output creds.csv --insecure

  # VCF 9.1 with API key (service account)
  python sddc_dump_credentials.py --host sddc.example.local --vcf 9.1 ^
      --api-key %VCF_API_KEY% --resource-type ESXI --output esxi.json

  # VCF 5.2 with domain filter, masked passwords for audit, auto-named CSV
  python sddc_dump_credentials.py --host sddc.example.local --vcf 5.2 ^
      --username admin@local --domain-name mgmt-domain --mask --format csv

Notes on the interactive password prompt:
  - If --password is not provided and --username is set, the script will prompt
    for the password interactively.
  - On Windows 10/11 (cmd.exe, PowerShell, Windows Terminal): paste works via
    right-click. In Windows Terminal you can also use Ctrl+Shift+V or
    Shift+Insert.
  - On Linux/macOS: paste works via Ctrl+Shift+V or middle-click (depending
    on terminal).
  - The prompt uses getpass(), so the password is NOT echoed. Just paste
    and press Enter - the input is read silently.

Security warning:
  The /v1/credentials endpoint returns passwords in plain text. Protect the
  output file appropriately (chmod 600, encrypted storage, never commit to git).

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


def prompt_password(username: str) -> str:
    """
    Prompt for password using getpass(). Paste-friendly:
      - Windows cmd.exe / PowerShell: right-click pastes
      - Windows Terminal: Ctrl+Shift+V or Shift+Insert
      - Linux/macOS terminals: Ctrl+Shift+V or middle-click

    getpass() reads silently (no echo), so the pasted value is hidden.
    """
    prompt = f"Password for {username} (paste with right-click / Shift+Insert): "
    if _USE_COLOR and _COLORAMA:
        prompt = f"{Fore.CYAN}{prompt}{Style.RESET_ALL}"
    try:
        return getpass(prompt)
    except (KeyboardInterrupt, EOFError):
        sys.stderr.write("\n")
        log.error("Password prompt cancelled.")
        sys.exit(130)


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
    out = []
    for c in creds:
        c2 = dict(c)
        if c2.get("password"):
            pwd = c2["password"]
            c2["password"] = (
                pwd[:2] + "*" * (len(pwd) - 2) if len(pwd) > 2 else "***"
            )
        out.append(c2)
    return out


def save_json(creds: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(creds, f, ensure_ascii=False, indent=2)


def save_csv(creds: list[dict], path: str, caps: dict[str, bool]) -> None:
    """Flat CSV. Columns specific to newer VCF stay in the header but are
    empty for older versions, so the header is consistent."""
    fieldnames = [
        "resourceType", "resourceName", "resourceIp", "domainName",
        "credentialType", "accountType", "username", "password",
        "expiryStatus", "expiryDate",
        "connectivityStatus",
        "creationTimestamp", "modificationTimestamp",
        "autoRotateFrequencyDays", "autoRotateNextSchedule",
        "id",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for c in creds:
            res = c.get("resource") or {}
            exp = c.get("expiry") or {}
            rot = c.get("autoRotatePolicy") or {}
            w.writerow({
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
        description="Dump credentials from a VCF SDDC Manager (4.x / 5.x / 9.x).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples:")[1] if "Examples:" in __doc__ else None,
    )
    p.add_argument("--host", required=True,
                   help="FQDN/IP of the SDDC Manager (no https://)")
    p.add_argument("--vcf", default="auto",
                   choices=["auto", "4", "5", "9.0", "9.1"],
                   help="Target VCF version [auto]. 'auto' = detect via "
                        "/v1/sddc-managers after login")

    auth = p.add_argument_group("authentication (provide at least one method)")
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


def main() -> int:
    args = parse_args()

    global _USE_COLOR
    if args.no_color:
        _USE_COLOR = False

    setup_logging(args.verbose)

    if not _COLORAMA and sys.platform == "win32":
        log.debug("colorama not installed - colors may not render correctly "
                  "in cmd.exe (install with: pip install colorama)")

    if args.insecure:
        requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

    if not (args.username or args.api_key or args.id_token):
        log.error("Provide at least one auth method: "
                  "--username + --password, --api-key, or --id-token")
        return 2

    password = args.password
    if args.username and not password and not (args.api_key or args.id_token):
        password = prompt_password(args.username)

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

        log.info("Fetching credentials (VCF %s) ...", client.vcf_version)
        creds = client.get_credentials(
            resource_type=args.resource_type,
            resource_name=args.resource_name,
            domain_name=args.domain_name,
            account_type=args.account_type,
            page_size=args.page_size,
        )
        log.info("Fetched %d records.", len(creds))

        if args.mask:
            creds = mask_passwords(creds)

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
            save_csv(creds, output_path, client.caps)
        else:
            save_json(creds, output_path)
        log.info("Saved %d records to %s", len(creds), output_path)
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
