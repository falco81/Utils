#!/usr/bin/env python3
"""
nsx_dhcp_leases.py - vypise a pripadne smaze DHCP leasy konkretniho segmentu v NSX 4.x

Pouziva Policy API:
  cteni:  GET  /policy/api/v1/infra/dhcp-server-configs/{config-id}/leases
               ?connectivity_path=<tier-1 nebo segment path>&segment_path=<segment path>
  mazani: POST /policy/api/v1/infra/segments/{segment-id}?action=delete_dhcp_leases
          POST /policy/api/v1/infra/tier-1s/{t1-id}/segments/{segment-id}?action=delete_dhcp_leases
          telo: {"leases": [{"ip": "...", "mac": "..."}]}

NSX pri mazani vyzaduje PRESNOU dvojici IP + MAC. Skript si proto leasy vzdy
nejdriv nacte a dvojici doplni sam - staci zadat IP, nebo MAC, nebo obojí.

Segment lze zadat jako ID nebo display name.

Priklady:
    export NSX_PASSWORD='tajneheslo'

    # vypis
    ./nsx_dhcp_leases.py -m nsx.firma.cz -u admin -s ls-prod-web --insecure
    ./nsx_dhcp_leases.py -m nsx.firma.cz -u admin -s ls-prod-web --json

    # smazani konkretnich leasu (lze opakovat, lze mixovat IP a MAC)
    ./nsx_dhcp_leases.py -m nsx.firma.cz -s ls-prod-web --delete 10.20.30.105
    ./nsx_dhcp_leases.py -m nsx.firma.cz -s ls-prod-web --delete 00:50:56:ae:6b:01
    ./nsx_dhcp_leases.py -m nsx.firma.cz -s ls-prod-web \
        --delete 10.20.30.105/00:50:56:ae:6b:01 --delete 10.20.30.9

    # smazani vsech leasu na segmentu
    ./nsx_dhcp_leases.py -m nsx.firma.cz -s ls-prod-web --delete-all --yes

    # smazani vsech leasu s konkretnim lease timem
    ./nsx_dhcp_leases.py -m nsx.firma.cz -s ls-prod-web --lease-time 86400 --delete-all
    ./nsx_dhcp_leases.py -m nsx.firma.cz -s ls-prod-web --lease-time '<600' --delete-all
    ./nsx_dhcp_leases.py -m nsx.firma.cz -s ls-prod-web --lease-time 3600-7200 --delete-all

    # obecny filtr na libovolne pole lease
    ./nsx_dhcp_leases.py -m nsx.firma.cz -s ls-prod-web --filter 'mac=00:50:56:*' --delete-all
    ./nsx_dhcp_leases.py -m nsx.firma.cz -s ls-prod-web --filter 'ip=10.20.30.1*'

    # nanecisto, nic se nemaze
    ./nsx_dhcp_leases.py -m nsx.firma.cz -s ls-prod-web --delete-all --dry-run

Zavislosti: requests  (pip install requests)
"""

import argparse
import fnmatch
import getpass
import json
import os
import sys

try:
    import requests
except ImportError:
    sys.exit("Chybi modul 'requests'. Nainstaluj: pip install requests")


API = "/policy/api/v1"
DELETE_CHUNK = 100  # kolik leasu poslat v jednom POST requestu


class NsxError(Exception):
    pass


class NotFound(NsxError):
    pass


class NsxClient:
    def __init__(self, manager, user, password, verify=True, timeout=60):
        self.base = f"https://{manager.rstrip('/')}"
        self.timeout = timeout
        self.s = requests.Session()
        self.s.auth = (user, password)
        self.s.verify = verify
        self.s.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def _request(self, method, path, params=None, body=None):
        url = self.base + path
        try:
            r = self.s.request(method, url, params=params, json=body,
                               timeout=self.timeout)
        except requests.exceptions.SSLError as e:
            raise NsxError(
                f"Chyba TLS pri spojeni na {self.base}: {e}\n"
                "Pouzij --ca-bundle s CA certifikatem, nebo --insecure (jen v labu)."
            )
        except requests.exceptions.RequestException as e:
            raise NsxError(f"Spojeni na {url} selhalo: {e}")

        if r.status_code == 404:
            raise NotFound(f"404 Not Found: {path}")
        if r.status_code in (401, 403):
            raise NsxError(
                f"HTTP {r.status_code} - autentizace/opravneni selhalo. "
                "Pro cteni staci read-only role, pro mazani je potreba zapis."
            )
        if r.status_code >= 400:
            detail = ""
            try:
                b = r.json()
                detail = b.get("error_message") or json.dumps(b)[:500]
            except ValueError:
                detail = r.text[:500]
            raise NsxError(f"HTTP {r.status_code} na {method} {path}: {detail}")

        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {}

    def get(self, path, params=None):
        return self._request("GET", path, params=params)

    def post(self, path, params=None, body=None):
        return self._request("POST", path, params=params, body=body)


# ---------------------------------------------------------------- segment

def resolve_segment(client, ident):
    """Najde segment podle ID nebo display name. Vraci kompletni objekt segmentu."""
    try:
        return client.get(f"{API}/infra/segments/{ident}")
    except NotFound:
        pass

    # search API - najde i fixed segmenty pod /infra/tier-1s/<t1>/segments/<id>
    query = f'resource_type:Segment AND (display_name:"{ident}" OR id:"{ident}")'
    try:
        res = client.get(f"{API}/search/query", params={"query": query, "page_size": 50})
    except NsxError:
        res = {"results": []}

    results = res.get("results") or []
    if not results:
        raise NsxError(f"Segment '{ident}' nenalezen (ani jako ID, ani jako display name).")
    if len(results) > 1:
        names = ", ".join(f"{x.get('display_name')} (id={x.get('id')})" for x in results[:10])
        raise NsxError(
            f"Vice segmentu odpovida '{ident}': {names}\n"
            "Zadej presne ID segmentu pomoci -s."
        )

    hit = results[0]
    path = hit.get("path")
    return client.get(f"{API}{path}") if path else hit


def resolve_dhcp(client, segment, override_config=None, override_connectivity=None):
    """
    Zjisti, ktery DHCP server config segment pouziva a jaky connectivity_path
    se ma poslat do lease dotazu.

    Pravidla dle NSX API:
      - lokalni DHCP server primo na segmentu  -> connectivity_path = path segmentu
      - DHCP server na Tier-0/Tier-1 gateway   -> connectivity_path = path gateway
    """
    seg_path = segment.get("path") or f"/infra/segments/{segment.get('id')}"

    if override_config:
        return override_config, (override_connectivity or seg_path), "rucne zadano"

    dhcp_cfg = segment.get("dhcp_config_path")
    if dhcp_cfg:
        if "/dhcp-relay-configs/" in dhcp_cfg:
            raise NsxError(
                f"Segment '{segment.get('display_name')}' pouziva DHCP relay "
                f"({dhcp_cfg}), ne DHCP server. Leasy drzi externi DHCP server, "
                "NSX je nema a nemuze je ani smazat."
            )
        return dhcp_cfg, (override_connectivity or seg_path), "lokalni DHCP na segmentu"

    conn_path = segment.get("connectivity_path")
    if not conn_path:
        raise NsxError(
            f"Segment '{segment.get('display_name')}' nema dhcp_config_path ani "
            "connectivity_path - neni na nem nakonfigurovany zadny DHCP server."
        )

    try:
        gw = client.get(f"{API}{conn_path}")
    except NsxError as e:
        raise NsxError(f"Nepodarilo se nacist gateway {conn_path}: {e}")

    cfg_paths = gw.get("dhcp_config_paths") or []
    if not cfg_paths:
        raise NsxError(
            f"Segment '{segment.get('display_name')}' nema vlastni DHCP config a "
            f"pripojena gateway '{gw.get('display_name')}' ({conn_path}) take ne. "
            "Na segmentu zrejme zadny NSX DHCP server nebezi."
        )

    cfg = cfg_paths[0]
    if "/dhcp-relay-configs/" in cfg:
        raise NsxError(
            f"Gateway '{gw.get('display_name')}' pouziva DHCP relay ({cfg}), "
            "ne DHCP server. NSX zadne leasy nedrzi."
        )
    return cfg, (override_connectivity or conn_path), f"DHCP server na gateway '{gw.get('display_name')}'"


# ---------------------------------------------------------------- cteni leasu

def fetch_leases(client, config_path, connectivity_path, segment_path,
                 enforcement_point=None, max_pages=200):
    """Stahne vsechny leasy, vcetne strankovani pres cursor."""
    config_id = config_path.rstrip("/").split("/")[-1]
    url = f"{API}/infra/dhcp-server-configs/{config_id}/leases"

    params = {"connectivity_path": connectivity_path, "segment_path": segment_path}
    if enforcement_point:
        params["enforcement_point_path"] = enforcement_point

    leases, meta = [], {}
    cursor, seen_cursors = None, set()

    for _ in range(max_pages):
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        data = client.get(url, params=p)

        if not meta:
            meta = {
                "dhcp_server_id": data.get("dhcp_server_id"),
                "timestamp": data.get("timestamp"),
            }

        page = data.get("leases") or []
        leases.extend(page)

        cursor = data.get("cursor")
        if not cursor or not page or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)

    return leases, meta


# ---------------------------------------------------------------- filtrovani

# aliasy, aby slo psat --filter ip=... misto ip_address=...
FIELD_ALIASES = {
    "ip": "ip_address",
    "address": "ip_address",
    "mac": "mac_address",
    "lease-time": "lease_time",
    "leasetime": "lease_time",
    "expire": "expire_time",
    "start": "start_time",
    "host": "hostname",
}


def _num(v):
    """Pokusi se prevest hodnotu na cislo; vraci None, pokud to nejde."""
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def parse_match_spec(spec):
    """
    Z textove specifikace udela predikat nad hodnotou pole.

    Podporuje:
        86400          presna shoda (u cisel numericky, jinak textove)
        >3600 >=3600   numericke porovnani (take <, <=, !=)
        3600-7200      numericky rozsah (vcetne obou mezi)
        00:50:56:*     glob (wildcard * a ?)
    """
    s = str(spec).strip()
    if not s:
        raise NsxError("Prazdna specifikace filtru.")

    for op in (">=", "<=", "!=", ">", "<"):
        if s.startswith(op):
            rhs = s[len(op):].strip()
            rnum = _num(rhs)
            if rnum is None:
                raise NsxError(f"Operator '{op}' vyzaduje cislo, dostal '{rhs}'.")

            def pred(value, op=op, rnum=rnum):
                vnum = _num(value)
                if vnum is None:
                    return False
                return {
                    ">=": vnum >= rnum, "<=": vnum <= rnum,
                    ">": vnum > rnum, "<": vnum < rnum,
                    "!=": vnum != rnum,
                }[op]
            return pred

    # rozsah 3600-7200 (jen kdyz jsou obe strany cisla, aby to nerozbilo MAC/datum)
    if "-" in s[1:]:
        lo_s, _, hi_s = s.partition("-")
        lo, hi = _num(lo_s), _num(hi_s)
        if lo is not None and hi is not None:
            def pred(value, lo=lo, hi=hi):
                vnum = _num(value)
                return vnum is not None and lo <= vnum <= hi
            return pred

    # glob
    if any(ch in s for ch in "*?["):
        def pred(value, pat=s.lower()):
            return fnmatch.fnmatch(str(value or "").strip().lower(), pat)
        return pred

    # presna shoda - numericky, pokud to jde (aby '3600' == '3600.0')
    snum = _num(s)

    def pred(value, s=s.lower(), snum=snum):
        if value is None:
            return False
        if snum is not None:
            vnum = _num(value)
            if vnum is not None:
                return vnum == snum
        return str(value).strip().lower() == s
    return pred


def build_filters(filter_args, lease_time_arg):
    """Z --filter a --lease-time udela seznam dvojic (nazev_pole, predikat)."""
    filters = []

    if lease_time_arg:
        filters.append(("lease_time", parse_match_spec(lease_time_arg), lease_time_arg))

    for raw in (filter_args or []):
        if "=" not in raw:
            raise NsxError(
                f"Spatny format --filter '{raw}'. Ocekavam FIELD=HODNOTA, "
                "napr. --filter lease_time=86400"
            )
        field, _, spec = raw.partition("=")
        field = field.strip().lower()
        field = FIELD_ALIASES.get(field, field)
        filters.append((field, parse_match_spec(spec), raw))

    return filters


def apply_filters(leases, filters):
    """Vrati jen leasy, ktere vyhovuji VSEM filtrum (AND)."""
    out = leases
    for field, pred, _raw in filters:
        out = [l for l in out if pred(l.get(field))]
    return out


# ---------------------------------------------------------------- mazani leasu

def _norm_mac(v):
    if not v:
        return None
    return str(v).strip().lower().replace("-", ":")


def _norm_ip(v):
    if not v:
        return None
    return str(v).strip().split("/")[0]


def _looks_like_mac(v):
    return v.count(":") >= 2 or (v.count("-") >= 2 and "." not in v)


def parse_delete_spec(spec):
    """
    Rozparsuje jeden --delete argument na dvojici (ip, mac); chybejici cast je None.
    Prijima:  '10.20.30.5'
              '00:50:56:ae:6b:01'
              '10.20.30.5/00:50:56:ae:6b:01'   (take ',' nebo '|' jako oddelovac)
    """
    s = str(spec).strip()
    if not s:
        raise NsxError("Prazdny --delete argument.")

    for sep in ("/", ",", "|"):
        if sep in s:
            a, b = s.split(sep, 1)
            a, b = a.strip(), b.strip()
            if _looks_like_mac(a) and not _looks_like_mac(b):
                a, b = b, a  # uzivatel to zadal obracene
            return (_norm_ip(a) or None), (_norm_mac(b) or None)

    if _looks_like_mac(s):
        return None, _norm_mac(s)
    return _norm_ip(s), None


def select_leases_for_delete(leases, specs):
    """
    Vybere z nactenych leasu ty, ktere odpovidaji zadanym --delete specifikacim.
    Vraci (vybrane_leasy, nenalezene_specifikace).
    """
    chosen, seen, missing = [], set(), []

    for spec in specs:
        ip, mac = parse_delete_spec(spec)
        hits = [
            l for l in leases
            if (ip is None or _norm_ip(l.get("ip_address")) == ip)
            and (mac is None or _norm_mac(l.get("mac_address")) == mac)
        ]
        if not hits:
            missing.append(spec)
            continue
        for l in hits:
            key = (_norm_ip(l.get("ip_address")), _norm_mac(l.get("mac_address")))
            if key not in seen:
                seen.add(key)
                chosen.append(l)

    return chosen, missing


def delete_leases(client, segment_path, leases, enforcement_point=None):
    """
    Smaze zadane leasy. NSX vyzaduje presnou dvojici ip + mac.
    POST na cestu segmentu funguje pro /infra/segments/<id> i pro
    /infra/tier-1s/<t1>/segments/<id>.
    Vraci (pocet_odeslanych, seznam_preskocenych).
    """
    entries, skipped = [], []
    for l in leases:
        ip = _norm_ip(l.get("ip_address"))
        mac = _norm_mac(l.get("mac_address"))
        if not ip or not mac:
            skipped.append(l)
            continue
        entries.append({"ip": ip, "mac": mac})

    if not entries:
        return 0, skipped

    params = {"action": "delete_dhcp_leases"}
    if enforcement_point:
        params["enforcement_point_path"] = enforcement_point

    sent = 0
    for i in range(0, len(entries), DELETE_CHUNK):
        chunk = entries[i:i + DELETE_CHUNK]
        client.post(f"{API}{segment_path}", params=params, body={"leases": chunk})
        sent += len(chunk)

    return sent, skipped


# ---------------------------------------------------------------- vystup

def print_table(leases):
    cols = [
        ("ip_address", "IP ADRESA"),
        ("mac_address", "MAC"),
        ("hostname", "HOSTNAME"),
        ("subnet", "SUBNET"),
        ("lease_time", "LEASE(s)"),
        ("start_time", "START"),
        ("expire_time", "EXPIRE"),
    ]
    # vyhodime sloupce, ktere jsou vsude prazdne (napr. hostname NSX casto nevraci)
    cols = [c for c in cols if any(str(l.get(c[0], "")).strip() for l in leases)]

    widths = []
    for key, head in cols:
        widths.append(max([len(head)] + [len(str(l.get(key, "") or "-")) for l in leases]))

    print("  ".join(h.ljust(w) for (_, h), w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for l in sorted(leases, key=lambda x: _ip_key(x.get("ip_address", ""))):
        row = "  ".join(str(l.get(key, "") or "-").ljust(w) for (key, _), w in zip(cols, widths))
        print(row.rstrip())


def _ip_key(ip):
    try:
        return tuple(int(x) for x in str(ip).split("/")[0].split("."))
    except (ValueError, AttributeError):
        return (999, 999, 999, 999)


def confirm(prompt):
    if not sys.stdin.isatty():
        raise NsxError(
            "Potvrzeni neni mozne (stdin neni terminal). "
            "Pro neinteraktivni beh pridej --yes."
        )
    try:
        ans = input(f"{prompt} [ano/NE]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("ano", "a", "yes", "y")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="Vypise (a volitelne smaze) DHCP leasy konkretniho segmentu v NSX 4.x.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Mazani: --delete lze opakovat; prijima IP, MAC, nebo 'IP/MAC'.",
    )
    ap.add_argument("-m", "--manager", required=True,
                    help="FQDN nebo IP NSX Manageru (nebo VIP)")
    ap.add_argument("-u", "--user", default=os.environ.get("NSX_USER", "admin"),
                    help="uzivatel (default: admin nebo $NSX_USER)")
    ap.add_argument("-p", "--password", default=os.environ.get("NSX_PASSWORD"),
                    help="heslo (lepe pres $NSX_PASSWORD; jinak se zepta)")
    ap.add_argument("-s", "--segment", required=True,
                    help="ID nebo display name segmentu")

    g = ap.add_mutually_exclusive_group()
    g.add_argument("--delete", action="append", metavar="IP|MAC|IP/MAC",
                   help="smaze konkretni lease; lze zadat vicekrat")
    g.add_argument("--delete-all", action="store_true",
                   help="smaze VSECHNY leasy na danem segmentu")

    ap.add_argument("--lease-time", metavar="SPEC",
                    help="filtr na lease time: '86400', '>3600', '<=600', '3600-7200'")
    ap.add_argument("--filter", action="append", metavar="FIELD=SPEC",
                    help="obecny filtr na pole lease (ip, mac, lease_time, subnet, "
                         "hostname, start_time, expire_time); podporuje >, <, >=, <=, "
                         "!=, rozsah a wildcard *; lze zadat vicekrat (AND)")

    ap.add_argument("--yes", "-y", action="store_true",
                    help="nepotvrzovat mazani (pro skripty/cron)")
    ap.add_argument("--dry-run", action="store_true",
                    help="jen ukaze, co by se smazalo; nic nemaze")

    ap.add_argument("--dhcp-config-path",
                    help="rucne zadana cesta k DHCP server configu "
                         "(/infra/dhcp-server-configs/xxx), obchazi autodetekci")
    ap.add_argument("--connectivity-path",
                    help="rucne zadana connectivity_path (tier-0/tier-1/segment)")
    ap.add_argument("--enforcement-point",
                    help="enforcement_point_path, napr. "
                         "/infra/sites/default/enforcement-points/default")
    ap.add_argument("--json", action="store_true",
                    help="vystup jako JSON misto tabulky")
    ap.add_argument("--insecure", action="store_true",
                    help="nekontrolovat TLS certifikat (jen lab)")
    ap.add_argument("--ca-bundle", help="cesta k CA certifikatu pro overeni TLS")
    ap.add_argument("--timeout", type=int, default=60, help="timeout HTTP (s)")
    args = ap.parse_args()

    deleting = bool(args.delete or args.delete_all)

    password = args.password or getpass.getpass(f"Heslo pro {args.user}@{args.manager}: ")

    verify = args.ca_bundle if args.ca_bundle else True
    if args.insecure:
        verify = False
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except ImportError:
            pass

    client = NsxClient(args.manager, args.user, password, verify=verify, timeout=args.timeout)

    result = {}
    try:
        segment = resolve_segment(client, args.segment)
        seg_path = segment.get("path") or f"/infra/segments/{segment.get('id')}"

        cfg_path, conn_path, kind = resolve_dhcp(
            client, segment,
            override_config=args.dhcp_config_path,
            override_connectivity=args.connectivity_path,
        )

        all_leases, meta = fetch_leases(
            client, cfg_path, conn_path, seg_path,
            enforcement_point=args.enforcement_point,
        )

        filters = build_filters(args.filter, args.lease_time)
        leases = apply_filters(all_leases, filters)
        filter_desc = ", ".join(raw for _f, _p, raw in filters)

        # ------------------------------------------------ rezim vypisu
        if not deleting:
            if args.json:
                print(json.dumps({
                    "segment": {"id": segment.get("id"),
                                "display_name": segment.get("display_name"),
                                "path": seg_path},
                    "dhcp_config_path": cfg_path,
                    "connectivity_path": conn_path,
                    "dhcp_server_id": meta.get("dhcp_server_id"),
                    "timestamp": meta.get("timestamp"),
                    "filters": filter_desc or None,
                    "total_on_segment": len(all_leases),
                    "lease_count": len(leases),
                    "leases": leases,
                }, indent=2, ensure_ascii=False))
                return 0

            print(f"Segment:     {segment.get('display_name')} (id={segment.get('id')})")
            print(f"DHCP:        {kind}")
            print(f"  config:    {cfg_path}")
            print(f"  conn path: {conn_path}")
            if meta.get("dhcp_server_id"):
                print(f"  server id: {meta['dhcp_server_id']}")
            if filters:
                print(f"Filtr:       {filter_desc}")
                print(f"Pocet leasu: {len(leases)} (z {len(all_leases)} na segmentu)\n")
            else:
                print(f"Pocet leasu: {len(leases)}\n")
            if not leases:
                print("Zadne leasy neodpovidaji filtru." if filters
                      else "Zadne aktivni leasy na tomto segmentu.")
                return 0
            print_table(leases)
            return 0

        # ------------------------------------------------ rezim mazani
        if not leases:
            msg = ("Zadne leasy neodpovidaji filtru, neni co mazat." if filters
                   else "Na segmentu nejsou zadne leasy, neni co mazat.")
            print(msg) if not args.json else print(json.dumps(
                {"deleted": 0, "message": msg}, indent=2, ensure_ascii=False))
            return 0

        if args.delete_all:
            targets, missing = list(leases), []
        else:
            targets, missing = select_leases_for_delete(leases, args.delete)

        if missing:
            for spec in missing:
                print(f"VAROVANI: '{spec}' neodpovida zadnemu aktivnimu lease na segmentu.",
                      file=sys.stderr)

        if not targets:
            print("Nic k smazani - zadna specifikace neodpovidala aktivnimu lease.",
                  file=sys.stderr)
            return 1

        if not args.json:
            print(f"Segment: {segment.get('display_name')} (id={segment.get('id')})")
            if filters:
                print(f"Filtr:   {filter_desc}")
            print(f"K smazani: {len(targets)} z {len(all_leases)} leasu na segmentu\n")
            print_table(targets)
            print()

        if args.dry_run:
            if args.json:
                print(json.dumps({"dry_run": True, "would_delete": len(targets),
                                  "leases": targets}, indent=2, ensure_ascii=False))
            else:
                print("DRY-RUN: nic nebylo smazano.")
            return 0

        if not args.yes:
            if args.delete_all and not filters:
                what = f"VSECHNY leasy ({len(targets)})"
            else:
                what = f"{len(targets)} leasu"
            if not confirm(f"Opravdu smazat {what} na segmentu "
                           f"'{segment.get('display_name')}'?"):
                print("Zruseno, nic se nesmazalo.")
                return 0

        sent, skipped = delete_leases(client, seg_path, targets,
                                      enforcement_point=args.enforcement_point)

        for l in skipped:
            print(f"VAROVANI: preskocen lease bez IP nebo MAC: {l}", file=sys.stderr)

        # overeni - znovu nacteme leasy
        remaining, _ = fetch_leases(client, cfg_path, conn_path, seg_path,
                                    enforcement_point=args.enforcement_point)
        result = {
            "segment": segment.get("display_name"),
            "filters": filter_desc or None,
            "requested": len(targets),
            "sent": sent,
            "skipped": len(skipped),
            "remaining_on_segment": len(remaining),
        }

    except NsxError as e:
        print(f"CHYBA: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Smazano: {result['sent']} leasu.")
        print(f"Na segmentu zbyva: {result['remaining_on_segment']} leasu.")
        print("\nPozn.: smazani lease neodebere IP uz bezicimu klientovi - "
              "ten ji pouziva do dalsiho DHCP renew/reboot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
