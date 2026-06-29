#!/usr/bin/env python3
"""
sddc_dump_credentials.py
========================

Stáhne všechny credentials z VMware Cloud Foundation SDDC Manageru přes
oficiální REST API. Podporuje VCF 4.x, 5.x, 9.0 a 9.1.

Endpointy (stabilní napříč VCF 4.0 - 9.1):
  POST   /v1/tokens                          - Create Token Pair
  PATCH  /v1/tokens/access-token/refresh     - Refresh Access Token
  DELETE /v1/tokens/refresh-token            - Invalidate Refresh Token
  GET    /v1/credentials                     - Get Credentials
  GET    /v1/sddc-managers                   - SDDC Manager info (pro detekci verze)

Co jsem ověřil z oficiální dokumentace Broadcom:
  - VCF 9.0 a 9.1: kompletní schéma na developer.broadcom.com
  - VCF 4.0, 5.1, 5.2: endpointy a request/response body z VMware blogů
    a oficiálních příkladů (PowerVCF, lookup_passwords articles)

Co se postupně přidávalo (capability matrix v kódu níže):
  - apiKey auth:           VCF 4.4+
  - idToken (SSO) auth:    VCF 9.0+
  - autoRotatePolicy pole: VCF 5.0+
  - accountType filter:    VCF 5.0+
  - connectivityStatus:    VCF 5.0+

Příklady použití:
  # auto-detekce verze, username/password, JSON výstup
  python sddc_dump_credentials.py --host sddc.example.local \\
      --username administrator@vsphere.local --output creds.json --insecure

  # explicitně VCF 4.x (žádný apiKey/idToken support, žádný accountType filter)
  python sddc_dump_credentials.py --host sddc.example.local --vcf 4 \\
      --username administrator@vsphere.local --output creds.csv --insecure

  # VCF 9.1 s API klíčem service účtu
  python sddc_dump_credentials.py --host sddc.example.local --vcf 9.1 \\
      --api-key "$VCF_API_KEY" --resource-type ESXI --output esxi.json

  # VCF 5.2 s filtrem podle domény, maskovaná hesla pro audit
  python sddc_dump_credentials.py --host sddc.example.local --vcf 5.2 \\
      --username admin@local --domain-name mgmt-domain --mask --output audit.json

Pozn.: API endpoint /v1/credentials vrací heslo v plain textu. Výstupní soubor
chraň odpovídajícím způsobem (chmod 600, šifrované úložiště, žádný git).
"""

from __future__ import annotations

import argparse
import atexit
import csv
import json
import logging
import re
import signal
import sys
from getpass import getpass
from typing import Any

import requests
from requests.exceptions import HTTPError, RequestException
from urllib3.exceptions import InsecureRequestWarning

log = logging.getLogger("sddc")


# ---------- VCF capability matrix ----------
#
# Klíče verzí: "4", "5", "9.0", "9.1"
# Auto detekce vrací jednu z nich na základě /v1/sddc-managers.

VCF_CAPS: dict[str, dict[str, bool]] = {
    "4": {
        "api_key_auth":       False,  # API key support přibyl až ve 4.4 - bezpečnější default False
        "id_token_auth":      False,
        "account_type_filter": False,
        "auto_rotate_policy":  False,
        "connectivity_status": False,
        "resource_name_filter": True,
        "domain_name_filter":   True,
        "resource_type_filter": True,
    },
    "5": {
        "api_key_auth":       True,
        "id_token_auth":      False,
        "account_type_filter": True,
        "auto_rotate_policy":  True,
        "connectivity_status": True,
        "resource_name_filter": True,
        "domain_name_filter":   True,
        "resource_type_filter": True,
    },
    "9.0": {
        "api_key_auth":       True,
        "id_token_auth":      True,
        "account_type_filter": True,
        "auto_rotate_policy":  True,
        "connectivity_status": True,
        "resource_name_filter": True,
        "domain_name_filter":   True,
        "resource_type_filter": True,
    },
    "9.1": {
        "api_key_auth":       True,
        "id_token_auth":      True,
        "account_type_filter": True,
        "auto_rotate_policy":  True,
        "connectivity_status": True,
        "resource_name_filter": True,
        "domain_name_filter":   True,
        "resource_type_filter": True,
    },
}


def normalize_vcf(version: str) -> str:
    """'5.2.1' -> '5', '9.1.0' -> '9.1', '4.5' -> '4'. Vrací klíč VCF_CAPS."""
    v = version.strip().lower().lstrip("v")
    if v.startswith("9.1"):
        return "9.1"
    if v.startswith("9.0") or v == "9":
        return "9.0"
    if v.startswith("9"):
        return "9.1"  # neznámé 9.x defaultně na 9.1
    if v.startswith("5"):
        return "5"
    if v.startswith("4"):
        return "4"
    raise ValueError(f"Nepodporovaná VCF verze: {version}")


class SDDCClient:
    """Klient pro SDDC Manager REST API se znalostí VCF verze."""

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

    # ---------- autodetekce verze ----------

    @staticmethod
    def detect_version(host: str, verify_tls: bool = True, timeout: int = 30) -> str:
        """
        Pokusí se zjistit VCF verzi přes /v1/sddc-managers (neautentizovaný
        access je obvykle odmítnut, takže to nemusí vždy vyjít). Pokud selže,
        vrátí default "9.1".

        Vrací klíč VCF_CAPS: "4", "5", "9.0" nebo "9.1".
        """
        url = f"https://{host}/v1/sddc-managers"
        try:
            r = requests.get(url, verify=verify_tls, timeout=timeout)
            # I 401 obvykle vrátí JSON s headery, ale není v něm verze.
            # Některé verze SDDC vystavují i veřejnější /v1/about.
            if r.status_code == 200:
                data = r.json()
                els = data.get("elements") or []
                if els and "version" in els[0]:
                    return normalize_vcf(els[0]["version"])
        except RequestException as e:
            log.debug("Detekce přes /v1/sddc-managers selhala: %s", e)

        # Fallback: hlavičky odpovědi občas obsahují version banner
        try:
            r = requests.get(f"https://{host}/v1/tokens", verify=verify_tls,
                             timeout=timeout)
            server = r.headers.get("X-VCF-Version") or r.headers.get("Server", "")
            m = re.search(r"\b(\d+\.\d+)", server)
            if m:
                return normalize_vcf(m.group(1))
        except RequestException:
            pass

        log.warning("Verzi SDDC Manageru se nepodařilo detekovat, používám 9.1.")
        return "9.1"

    def detect_version_post_login(self) -> str | None:
        """
        Po přihlášení můžeme spolehlivě zavolat /v1/sddc-managers
        a získat skutečnou verzi.
        """
        try:
            r = self._authed_request("GET", "/v1/sddc-managers", _retry=True)
            data = r.json()
            els = data.get("elements") or []
            if els:
                version = els[0].get("version")
                if version:
                    return version
        except (RequestException, RuntimeError) as e:
            log.debug("Detekce verze po loginu selhala: %s", e)
        return None

    # ---------- autentizace ----------

    def login(
        self,
        username: str | None = None,
        password: str | None = None,
        api_key: str | None = None,
        id_token: str | None = None,
    ) -> None:
        """POST /v1/tokens. Skládá payload podle capability matrix verze."""
        spec: dict[str, str] = {}
        if username and password:
            spec["username"] = username
            spec["password"] = password
        if api_key:
            if not self.caps["api_key_auth"]:
                log.warning("VCF %s nemusí podporovat apiKey auth (testováno od 4.4+). "
                            "Posílám stejně, ale může selhat.", self.vcf_version)
            spec["apiKey"] = api_key
        if id_token:
            if not self.caps["id_token_auth"]:
                log.warning("VCF %s nepodporuje idToken auth (jen 9.x). "
                            "Posílám stejně, ale očekávej selhání.", self.vcf_version)
            spec["idToken"] = id_token

        if not spec:
            raise ValueError("Musíš zadat username+password, apiKey, nebo idToken")

        self._auth_spec = spec
        log.info("Přihlašuji se na %s (VCF %s) ...", self.base, self.vcf_version)
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
            raise RuntimeError(f"V odpovědi /v1/tokens chybí accessToken: {body}")

        rt_short = (self.refresh_token_id or "<none>")[:8]
        log.info("Přihlášen. Refresh token id: %s...", rt_short)

    def refresh_access_token(self) -> bool:
        """PATCH /v1/tokens/access-token/refresh"""
        if not self.refresh_token_id:
            return False
        try:
            log.info("Obnovuji access token přes refresh token ...")
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
                log.warning("Neočekávaná odpověď refresh endpointu: %r", r.text[:200])
                return False
            self.access_token = new_token
            log.info("Access token obnoven.")
            return True
        except RequestException as e:
            log.warning("Refresh access tokenu selhal: %s", e)
            return False

    def _reauth_with_credentials(self) -> bool:
        if not self._auth_spec:
            return False
        try:
            log.info("Refresh selhal, zkouším re-login ...")
            self.login(
                username=self._auth_spec.get("username"),
                password=self._auth_spec.get("password"),
                api_key=self._auth_spec.get("apiKey"),
                id_token=self._auth_spec.get("idToken"),
            )
            return True
        except (RequestException, RuntimeError) as e:
            log.error("Re-login selhal: %s", e)
            return False

    def logout(self) -> None:
        """DELETE /v1/tokens/refresh-token (cleanup, nikdy nevyhazuje výjimku)."""
        if not self.refresh_token_id or not self.access_token:
            return
        try:
            log.info("Invaliduji refresh token ...")
            r = self.session.delete(
                f"{self.base}/v1/tokens/refresh-token",
                headers={"Authorization": f"Bearer {self.access_token}"},
                data=json.dumps(self.refresh_token_id),
                verify=self.verify_tls,
                timeout=self.timeout,
            )
            if r.status_code == 204:
                log.info("Refresh token invalidován.")
            else:
                log.warning("Invalidace vrátila %s: %s", r.status_code, r.text[:200])
        except RequestException as e:
            log.warning("Invalidace refresh tokenu selhala: %s", e)
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
            raise RuntimeError("Nejsi přihlášen, zavolej login() první.")
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
            log.info("HTTP 401, zkouším refresh tokenu / re-login ...")
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
        """GET /v1/credentials se stránkováním a verzově-aware filtry."""
        # Odfiltrujeme parametry, které daná verze nepodporuje, abychom dostali 400
        if account_type and not self.caps["account_type_filter"]:
            log.warning("VCF %s nepodporuje accountType filter, ignoruji.",
                        self.vcf_version)
            account_type = None
        if resource_name and not self.caps["resource_name_filter"]:
            log.warning("VCF %s nepodporuje resourceName filter, ignoruji.",
                        self.vcf_version)
            resource_name = None
        if domain_name and not self.caps["domain_name_filter"]:
            log.warning("VCF %s nepodporuje domainName filter, ignoruji.",
                        self.vcf_version)
            domain_name = None
        if resource_type and not self.caps["resource_type_filter"]:
            log.warning("VCF %s nepodporuje resourceType filter, ignoruji.",
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
            log.info("  stránka %d/%d, prvků: %d (celkem: %d)",
                     page + 1, total_pages, len(elements), len(all_creds))

            if not elements or page + 1 >= total_pages:
                break
            page += 1
        return all_creds


# ---------- output helpers ----------

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
    """CSV s plochou strukturou; pole specifická pro novější VCF necháme vždy
    ve sloupcích (prázdné u starších)."""
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

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Dump credentials z VCF SDDC Manageru (4.x / 5.x / 9.x).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(__doc__.split("Příklady použití:")[1]
                if "Příklady použití:" in __doc__ else None),
    )
    p.add_argument("--host", required=True,
                   help="FQDN/IP SDDC Manageru (bez https://)")
    p.add_argument("--vcf", default="auto",
                   choices=["auto", "4", "5", "9.0", "9.1"],
                   help="Cílová VCF verze [auto]. 'auto' = detekce přes "
                        "/v1/sddc-managers po přihlášení")

    auth = p.add_argument_group("autentizace (alespoň jedna metoda)")
    auth.add_argument("--username", help="SSO uživatel (administrator@vsphere.local)")
    auth.add_argument("--password",
                      help="Heslo. Nezadáno + je-li username => interaktivní prompt.")
    auth.add_argument("--api-key", help="API klíč (VCF 4.4+)")
    auth.add_argument("--id-token", help="SSO ID token (VCF 9.x)")

    filt = p.add_argument_group("filtrování")
    filt.add_argument("--resource-type", choices=[
        "ESXI", "VCENTER", "PSC", "NSXT_MANAGER", "NSXT_EDGE",
        "NSX_ALB", "BACKUP", "HCX_MANAGER", "VSP",
    ], help="Filtrovat jen tento typ zdroje")
    filt.add_argument("--resource-name", help="Filtrovat podle názvu zdroje")
    filt.add_argument("--domain-name", help="Filtrovat podle workload domény")
    filt.add_argument("--account-type", help="USER/SYSTEM/SERVICE (VCF 5.0+)")

    out = p.add_argument_group("výstup")
    out.add_argument("--output", default="credentials.json",
                     help="Cílový soubor (.json nebo .csv) [credentials.json]")
    out.add_argument("--mask", action="store_true",
                     help="Zamaskovat hesla ve výstupu (pro audit)")
    out.add_argument("--page-size", type=int, default=200,
                     help="Velikost stránky API [200]")

    misc = p.add_argument_group("ostatní")
    misc.add_argument("--insecure", action="store_true",
                      help="Vypnout ověření TLS (self-signed lab)")
    misc.add_argument("--keep-token", action="store_true",
                      help="Neinvalidovat refresh token na konci (debug)")
    misc.add_argument("-v", "--verbose", action="store_true",
                      help="Detailní logování")

    return p.parse_args()


def main() -> int:
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.insecure:
        requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

    if not (args.username or args.api_key or args.id_token):
        log.error("Zadej alespoň jednu metodu autentizace: "
                  "--username + --password, --api-key, nebo --id-token")
        return 2

    password = args.password
    if args.username and not password and not (args.api_key or args.id_token):
        password = getpass(f"Heslo pro {args.username}: ")

    # Detekce verze - před loginem jen heuristicky, po loginu spolehlivě
    if args.vcf == "auto":
        log.info("Detekuji VCF verzi ...")
        vcf_version = SDDCClient.detect_version(args.host, verify_tls=not args.insecure)
        log.info("Heuristicky detekováno: VCF %s (upřesní se po přihlášení)", vcf_version)
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

        # Post-login auto-detekce: pokud uživatel zadal "auto", upřesni
        if args.vcf == "auto":
            real = client.detect_version_post_login()
            if real:
                norm = normalize_vcf(real)
                if norm != vcf_version:
                    log.info("Upřesněná verze SDDC Manageru: %s (klíč %s) - "
                             "aktualizuji capability matrix.", real, norm)
                    client.vcf_version = norm
                    client.caps = VCF_CAPS[norm]
                else:
                    log.info("Verze SDDC Manageru potvrzena: %s", real)

        log.info("Stahuji credentials (VCF %s) ...", client.vcf_version)
        creds = client.get_credentials(
            resource_type=args.resource_type,
            resource_name=args.resource_name,
            domain_name=args.domain_name,
            account_type=args.account_type,
            page_size=args.page_size,
        )
        log.info("Načteno %d záznamů.", len(creds))

        if args.mask:
            creds = mask_passwords(creds)

        if args.output.lower().endswith(".csv"):
            save_csv(creds, args.output, client.caps)
        else:
            save_json(creds, args.output)
        log.info("Uloženo do %s", args.output)
        return 0

    except HTTPError as e:
        body = ""
        try:
            body = e.response.text[:500] if e.response is not None else ""
        except Exception:
            pass
        log.error("HTTP %s: %s",
                  e.response.status_code if e.response is not None else "?", body)
        return 3
    except Exception as e:
        log.error("Chyba: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
