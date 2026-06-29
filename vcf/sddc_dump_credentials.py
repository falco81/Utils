#!/usr/bin/env python3
"""
Stáhne všechny credentials z VMware Cloud Foundation SDDC Manageru.

Použití:
    python sddc_dump_credentials.py \
        --host sddc-manager.example.local \
        --username administrator@vsphere.local \
        --password 'TajneHeslo!' \
        --output credentials.json

Pozn.: Skript je určen pro autorizovaného administrátora (rotace hesel,
audit, záloha). API endpoint /v1/credentials vrací heslo v plain textu,
takže výstup zacházejte odpovídajícím způsobem (šifrované úložiště,
mazání po použití, žádné commitnutí do gitu).
"""

import argparse
import csv
import json
import sys
from getpass import getpass

import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning


def get_token(host: str, username: str, password: str, verify_tls: bool = True) -> str:
    """Získá access token z SDDC Manageru."""
    url = f"https://{host}/v1/tokens"
    payload = {"username": username, "password": password}
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    r = requests.post(url, json=payload, headers=headers, verify=verify_tls, timeout=30)
    r.raise_for_status()
    data = r.json()
    token = data.get("accessToken")
    if not token:
        raise RuntimeError(f"V odpovědi není accessToken: {data}")
    return token


def get_all_credentials(
    host: str,
    token: str,
    verify_tls: bool = True,
    resource_type: str | None = None,
    page_size: int = 100,
) -> list[dict]:
    """Stáhne všechny credentials s podporou stránkování."""
    url = f"https://{host}/v1/credentials"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    all_creds: list[dict] = []
    page = 0
    while True:
        params = {"pageNumber": page, "pageSize": page_size}
        if resource_type:
            params["resourceType"] = resource_type

        r = requests.get(url, headers=headers, params=params, verify=verify_tls, timeout=60)
        r.raise_for_status()
        body = r.json()

        elements = body.get("elements", []) or []
        all_creds.extend(elements)

        meta = body.get("pageMetadata", {}) or {}
        total_pages = meta.get("totalPages", 1)
        # pageSize=0 by mělo vrátit vše naráz; když ne, jdeme po stránkách
        if not elements or page + 1 >= total_pages:
            break
        page += 1

    return all_creds


def save_json(creds: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(creds, f, ensure_ascii=False, indent=2)


def save_csv(creds: list[dict], path: str) -> None:
    """Zploští důležitá pole do CSV."""
    fieldnames = [
        "resourceType",
        "resourceName",
        "resourceIp",
        "domainName",
        "credentialType",
        "accountType",
        "username",
        "password",
        "expiryStatus",
        "expiryDate",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for c in creds:
            res = c.get("resource") or {}
            exp = c.get("expiry") or {}
            w.writerow({
                "resourceType": res.get("resourceType", ""),
                "resourceName": res.get("resourceName", ""),
                "resourceIp": res.get("resourceIp", ""),
                "domainName": res.get("domainName", ""),
                "credentialType": c.get("credentialType", ""),
                "accountType": c.get("accountType", ""),
                "username": c.get("username", ""),
                "password": c.get("password", ""),
                "expiryStatus": exp.get("status", ""),
                "expiryDate": exp.get("expiryDate", ""),
            })


def main() -> int:
    p = argparse.ArgumentParser(description="Dump SDDC Manager credentials.")
    p.add_argument("--host", required=True, help="FQDN/IP SDDC Manageru (bez https://)")
    p.add_argument("--username", required=True, help="SSO uživatel, např. administrator@vsphere.local")
    p.add_argument("--password", help="Heslo (když nezadáš, zeptá se interaktivně)")
    p.add_argument("--output", default="credentials.json", help="Výstupní soubor (.json nebo .csv)")
    p.add_argument("--resource-type", choices=[
        "ESXI", "VCENTER", "PSC", "NSXT_MANAGER", "NSXT_EDGE",
        "NSX_ALB", "BACKUP", "HCX_MANAGER", "VSP",
    ], help="Filtrovat jen tento typ zdroje")
    p.add_argument("--insecure", action="store_true", help="Vypnout ověření TLS certifikátu (self-signed lab)")
    args = p.parse_args()

    if args.insecure:
        requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
    verify_tls = not args.insecure

    password = args.password or getpass("Heslo: ")

    try:
        print(f"[*] Přihlašuji se na {args.host} ...", file=sys.stderr)
        token = get_token(args.host, args.username, password, verify_tls=verify_tls)

        print("[*] Stahuji credentials ...", file=sys.stderr)
        creds = get_all_credentials(
            args.host, token, verify_tls=verify_tls, resource_type=args.resource_type
        )
        print(f"[+] Načteno {len(creds)} záznamů.", file=sys.stderr)

        if args.output.lower().endswith(".csv"):
            save_csv(creds, args.output)
        else:
            save_json(creds, args.output)
        print(f"[+] Uloženo do {args.output}", file=sys.stderr)
        return 0

    except requests.HTTPError as e:
        print(f"[!] HTTP chyba: {e.response.status_code} {e.response.text}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"[!] Chyba: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
