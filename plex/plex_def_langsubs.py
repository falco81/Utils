#!/usr/bin/env python3
"""
plex_def_langsubs.py  (Windows 10 CLI, barevné UI přes colorama)
================================================================

Hromadně nastaví VÝCHOZÍ (default) zvukovou stopu a titulky na Plex Media
Serveru - u všech dílů seriálu, nebo u filmu. Přihlásí se přes tvůj Plex
účet a přepne se na konkrétního Plex Home uživatele (s jeho PINem), pak
tě nechá procházet/hledat knihovny (seriály i filmy), načte dostupné
jazyky a nabídne výběr.

První spuštění se zeptá na:
  - FQDN (adresu) tvého Plex serveru, např. https://plex.falco81.net
  - přihlášení k Plex účtu (jméno+heslo, nebo kód na plex.tv/link)
  - kterého Plex Home uživatele použít a jeho PIN (pokud ho má)
Vše se uloží do plex_def_langsubs.config.json vedle skriptu (adresa +
získané tokeny), takže PŘI DALŠÍCH SPUŠTĚNÍCH SE UŽ NA NIC NEPTÁ.
Odhlášení / reset: --logout (smaže tokeny), --relogin (nové přihlášení),
--switch-user (znovu vybrat Home uživatele).

Jak to funguje (ověřeno proti Plex API dokumentaci)
---------------------------------------------------
- Přihlášení účtu:   POST https://plex.tv/api/v2/users/signin  (login/
                     password/verificationCode) NEBO PIN na plex.tv/link.
- Home uživatelé:    GET  https://plex.tv/api/v2/home/users     (XML)
- Přepnutí + PIN:    POST https://plex.tv/api/home/users/{id}/switch?pin=..
                     -> vrátí uživatelský token (authenticationToken),
                        ten se uloží a příště se použije rovnou.
- Nastavení stopy:   PUT  {server}/library/parts/{partId}
                        ?audioStreamID=A&subtitleStreamID=T&allParts=1
  subtitleStreamID=0 = titulky vypnout. Mění se JEN výběr výchozí stopy,
  soubory ani text titulků se nedotýká. ID stop se dohledávají za běhu
  podle jazyka (Plex je po refreshi metadat přečísluje).

Instalace
---------
1) Python 3.8+
2) (volitelně) pip install colorama   -> barvy na Windows CLI (bez ní jede
   skript stejně). Žádné jiné závislosti - vše přes standardní knihovnu.

Použití
-------
    python plex_def_langsubs.py                 # interaktivní průvodce
    python plex_def_langsubs.py --show 32800 --audio kor --subs cze --yes
    python plex_def_langsubs.py --show "Recipe for Love" --subs off --dry-run
    python plex_def_langsubs.py --logout | --relogin | --switch-user
"""

import argparse
import getpass
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# Barevný výstup (Windows CLI friendly přes colorama, bez pádu i bez něj)
# ---------------------------------------------------------------------------
try:
    import colorama
    from colorama import Fore, Style
    colorama.init(autoreset=True)
except ImportError:
    class _NoColor:
        def __getattr__(self, _name):
            return ""
    Fore = _NoColor()
    Style = _NoColor()


# truststore (volitelné): ověřuje HTTPS přes systémové úložiště certifikátů
# OS (Windows si umí dostáhnout chybějící intermediate certifikát). Když není,
# skript si u vlastního serveru poradí automatickým fallbackem (viz _connect).
try:
    import truststore
    truststore.inject_into_ssl()
    _HAS_TRUSTSTORE = True
except Exception:
    _HAS_TRUSTSTORE = False


def log_info(msg):
    print(f"{Fore.CYAN}[INFO]{Style.RESET_ALL} {msg}")


def log_warn(msg):
    print(f"{Fore.YELLOW}[VAROVÁNÍ]{Style.RESET_ALL} {msg}")


def log_done(msg):
    print(f"{Fore.GREEN}[HOTOVO]{Style.RESET_ALL} {msg}")


def die(msg, code=1):
    print(f"{Fore.RED}[CHYBA]{Style.RESET_ALL} {msg}", file=sys.stderr)
    sys.exit(code)


CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "plex_def_langsubs.config.json"
)

# streamType v Plex API
ST_VIDEO, ST_AUDIO, ST_SUBTITLE = 1, 2, 3
SECTION_TYPE_NUM = {"movie": 1, "show": 2}

PRODUCT = "plex_def_langsubs"
PLEX_TV = "https://plex.tv"
SIGNIN_URL = f"{PLEX_TV}/api/v2/users/signin"
PINS_URL = f"{PLEX_TV}/api/v2/pins"
HOMEUSERS_URL = f"{PLEX_TV}/api/v2/home/users"
SWITCH_URL = f"{PLEX_TV}/api/home/users/{{uid}}/switch"


# ---------------------------------------------------------------------------
# Interaktivní pomůcky
# ---------------------------------------------------------------------------
# --- detekce klávesnicového vstupu (šipky) napříč platformami --------------
_WINDOWS = os.name == "nt"
try:
    if _WINDOWS:
        import msvcrt
    else:
        import termios
        import tty
    _HAS_RAW = True
except Exception:
    _HAS_RAW = False

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(s):
    return _ANSI_RE.sub("", s)


def _read_key():
    """Přečte jednu klávesu. Vrátí řetězec akce nebo ('char', znak)."""
    if _WINDOWS:
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):
            c2 = msvcrt.getch()
            return {b"H": "up", b"P": "down", b"K": "left", b"M": "right",
                    b"G": "home", b"O": "end", b"I": "pgup", b"Q": "pgdn"}.get(c2, "other")
        if ch in (b"\r", b"\n"):
            return "enter"
        if ch == b"\x08":
            return "backspace"
        if ch == b"\x1b":
            return "esc"
        if ch == b"\x03":
            raise KeyboardInterrupt
        for enc in ("utf-8", "cp1250", "latin-1"):
            try:
                return ("char", ch.decode(enc))
            except Exception:
                continue
        return "other"
    else:
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            seq = sys.stdin.read(2)
            return {"[A": "up", "[B": "down", "[C": "right", "[D": "left",
                    "[H": "home", "[F": "end", "[5": "pgup", "[6": "pgdn"}.get(seq, "esc")
        if ch in ("\r", "\n"):
            return "enter"
        if ch in ("\x7f", "\x08"):
            return "backspace"
        if ch == "\x03":
            raise KeyboardInterrupt
        return ("char", ch)


class _RawMode:
    """Kontextový manažer pro raw režim terminálu (jen Unix)."""
    def __enter__(self):
        if not _WINDOWS:
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setraw(self.fd)
        return self

    def __exit__(self, *a):
        if not _WINDOWS:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)


def _tui_supported():
    return _HAS_RAW and sys.stdin.isatty() and sys.stdout.isatty()


def interactive_menu(prompt, labels, default=0, allow_cancel=False, page=None):
    """Menu ovládané šipkami + psaní pro hledání. Vrátí index, nebo None (zrušeno).
    Když terminál nepodporuje raw vstup, spadne na číselné menu."""
    if not _tui_supported():
        return _ask_choice_classic(prompt, labels, default)

    n = len(labels)
    plain = [strip_ansi(l) for l in labels]
    filt = ""
    sel_pos = default if 0 <= default < n else 0
    prev_lines = 0  # počet řádků předchozího snímku

    def visible_order():
        if not filt:
            return list(range(n))
        f = filt.lower()
        return [i for i in range(n) if f in plain[i].lower()]

    def term_size():
        try:
            sz = os.get_terminal_size()
            return sz.columns, sz.lines
        except Exception:
            return 80, 24

    def trunc(s, width):
        if len(s) > width:
            return s[:max(1, width - 1)] + "…"
        return s

    def render(order, sel_pos):
        nonlocal prev_lines
        cols, rows_total = term_size()
        maxw = max(10, cols - 2)
        # kolik položek se vejde: nech místo na prompt+hint+footer(+2 indikátory)
        page_rows = max(3, rows_total - 5)

        buf = []
        # posun kurzoru na začátek předchozího snímku a smazání dolů
        if prev_lines > 0:
            up = prev_lines - 1
            buf.append((f"\x1b[{up}F" if up > 0 else "\r") + "\x1b[J")

        vis_lines = []  # jednotlivé řádky snímku (bez \n)
        vis_lines.append(f"{Fore.YELLOW}{trunc(strip_ansi(prompt), maxw)}{Style.RESET_ALL}")
        if filt:
            hint = "↑↓ pohyb · Enter = vybrat · Esc = smazat hledání"
        elif allow_cancel:
            hint = "↑↓ pohyb · piš = hledat · Enter = vybrat · Esc = zpět"
        else:
            hint = "↑↓ pohyb · piš = hledat · Enter = vybrat"
        vis_lines.append(f"{Fore.CYAN}{trunc(hint, maxw)}{Style.RESET_ALL}")

        if not order:
            vis_lines.append(f"  {Fore.RED}(žádná shoda){Style.RESET_ALL}")
        else:
            start = max(0, min(sel_pos - page_rows // 2, len(order) - page_rows))
            window = order[start:start + page_rows]
            if start > 0:
                vis_lines.append(f"  {Fore.CYAN}▲ ({start} výše){Style.RESET_ALL}")
            for pos, i in enumerate(window, start):
                text = trunc(plain[i], maxw - 2)
                if pos == sel_pos:
                    vis_lines.append(f"{Fore.GREEN}{Style.BRIGHT}›{Style.RESET_ALL} "
                                     f"{Fore.GREEN}{Style.BRIGHT}{text}{Style.RESET_ALL}")
                else:
                    vis_lines.append(f"  {text}")
            rest = len(order) - (start + len(window))
            if rest > 0:
                vis_lines.append(f"  {Fore.CYAN}▼ ({rest} níže){Style.RESET_ALL}")

        pos_info = f" [{sel_pos + 1}/{len(order)}]" if order else ""
        if filt:
            footer = f"{Fore.MAGENTA}{trunc('Hledání: ' + filt + pos_info, maxw)}{Style.RESET_ALL}"
        else:
            footer = f"{Fore.CYAN}{trunc('(začni psát pro hledání)' + pos_info, maxw)}{Style.RESET_ALL}"
        vis_lines.append(footer)

        buf.append("\n".join(vis_lines))
        sys.stdout.write("".join(buf))
        sys.stdout.flush()
        prev_lines = len(vis_lines)
        return page_rows

    with _RawMode():
        order = visible_order()
        if sel_pos >= len(order):
            sel_pos = max(0, len(order) - 1)
        page_rows = render(order, sel_pos)
        while True:
            key = _read_key()
            if key == "up" and order:
                sel_pos = (sel_pos - 1) % len(order)
            elif key == "down" and order:
                sel_pos = (sel_pos + 1) % len(order)
            elif key == "pgup" and order:
                sel_pos = max(0, sel_pos - page_rows)
            elif key == "pgdn" and order:
                sel_pos = min(len(order) - 1, sel_pos + page_rows)
            elif key == "home":
                sel_pos = 0
            elif key == "end" and order:
                sel_pos = len(order) - 1
            elif key in ("enter", "right"):
                if order:
                    result = order[sel_pos]
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return result
            elif key in ("esc", "left"):
                if filt:
                    filt = ""
                    order = visible_order()
                    sel_pos = 0
                elif allow_cancel:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return None
            elif key == "backspace":
                if filt:
                    filt = filt[:-1]
                    order = visible_order()
                    sel_pos = 0
            elif isinstance(key, tuple) and key[0] == "char" and key[1].isprintable():
                filt += key[1]
                order = visible_order()
                sel_pos = 0
            page_rows = render(order, sel_pos)


def _ask_choice_classic(prompt, labels, default=0):
    def _show():
        print(f"{Fore.YELLOW}{prompt}{Style.RESET_ALL}")
        for i, l in enumerate(labels):
            mark = f" {Fore.CYAN}(výchozí){Style.RESET_ALL}" if i == default else ""
            print(f"  {i + 1}) {l}{mark}")

    _show()
    while True:
        raw = input(f"Volba [1-{len(labels)}, Enter = {default + 1}]: ").strip()
        if raw == "":
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(labels):
            return int(raw) - 1
        print(f"{Fore.RED}Neplatná volba, zkus to znovu.{Style.RESET_ALL}")


def ask_choice(prompt, labels, default=0):
    idx = interactive_menu(prompt, labels, default=default, allow_cancel=False)
    return default if idx is None else idx


def ask_text(prompt, default=""):
    suffix = f" [{default}]" if default else ""
    raw = input(f"{Fore.YELLOW}{prompt}{Style.RESET_ALL}{suffix}: ").strip()
    return raw or default


def ask_secret(prompt):
    try:
        return getpass.getpass(f"{prompt}: ")
    except Exception:
        return input(f"{prompt}: ")


def ask_yes(prompt, default=True):
    d = "A/n" if default else "a/N"
    raw = input(f"{Fore.YELLOW}{prompt}{Style.RESET_ALL} [{d}]: ").strip().lower()
    if raw == "":
        return default
    return raw in ("a", "ano", "y", "yes")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(CONFIG_PATH, 0o600)  # jen vlastník (kde to jde)
        except Exception:
            pass
    except Exception as ex:
        log_warn(f"Nepodařilo se uložit config: {ex}")


def get_client_id(cfg):
    cid = cfg.get("client_id")
    if not cid:
        cid = uuid.uuid4().hex
        cfg["client_id"] = cid
        save_config(cfg)
    return cid


# ---------------------------------------------------------------------------
# Nízkoúrovňové HTTP (stdlib)
# ---------------------------------------------------------------------------
def _ssl_ctx(verify):
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def http_raw(method, url, headers=None, data=None, verify=True, timeout=30):
    """Vrátí (status, text). HTTP chyby NEVYHazuje (vrátí je), síťové ano."""
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    ctx = _ssl_ctx(verify) if url.lower().startswith("https") else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as ex:
        try:
            body = ex.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return ex.code, body
    except urllib.error.URLError as ex:
        hint = ""
        if isinstance(ex.reason, ssl.SSLError) or "CERTIFICATE" in str(ex.reason).upper():
            hint = "  (zkus --insecure)"
        raise RuntimeError(f"Spojení selhalo: {ex.reason}{hint}")


def http_json(method, url, headers=None, data=None, verify=True, timeout=30):
    st, text = http_raw(method, url, headers, data, verify, timeout)
    if st >= 400:
        raise RuntimeError(f"HTTP {st} u {method} {url.split('?')[0]} - {text[:200]}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError(f"Neočekávaná odpověď (není JSON) z {url.split('?')[0]}")


# ---------------------------------------------------------------------------
# Plex účet (plex.tv): přihlášení + Home uživatelé + switch s PINem
# ---------------------------------------------------------------------------
class PlexAccount:
    def __init__(self, client_id, token=None):
        self.client_id = client_id
        self.token = token

    def _headers(self, accept="application/json", with_token=True):
        h = {
            "Accept": accept,
            "X-Plex-Product": PRODUCT,
            "X-Plex-Version": "1.0",
            "X-Plex-Client-Identifier": self.client_id,
            "X-Plex-Device-Name": "plex_def_langsubs CLI",
        }
        if with_token and self.token:
            h["X-Plex-Token"] = self.token
        return h

    # --- přihlášení jménem a heslem (+2FA) ---------------------------------
    def signin_password(self, login, password, code=None):
        body = {"login": login, "password": password, "rememberMe": "true"}
        if code:
            body["verificationCode"] = code
        data = urllib.parse.urlencode(body).encode()
        headers = {**self._headers(with_token=False),
                   "Content-Type": "application/x-www-form-urlencoded"}
        st, text = http_raw("POST", SIGNIN_URL, headers=headers, data=data)
        if st in (200, 201):
            j = json.loads(text)
            tok = j.get("authToken") or j.get("authenticationToken")
            if not tok:
                raise RuntimeError("Přihlášení proběhlo, ale token chybí.")
            self.token = tok
            return tok
        if st == 401:
            # buď špatné heslo, nebo chybí 2FA kód
            raise RuntimeError("UNAUTHORIZED")
        raise RuntimeError(f"Přihlášení selhalo (HTTP {st}): {text[:200]}")

    # --- přihlášení párovacím kódem na plex.tv/link ------------------------
    def pin_login(self, wait_timeout=300, poll=2):
        data = urllib.parse.urlencode({"strong": "false"}).encode()
        j = http_json("POST", PINS_URL,
                      headers={**self._headers(with_token=False),
                               "Content-Type": "application/x-www-form-urlencoded"},
                      data=data)
        pin_id, code = j.get("id"), j.get("code")
        if not pin_id or not code:
            raise RuntimeError("Plex nevrátil PIN.")
        print()
        print(f"{Fore.MAGENTA}==============================================={Style.RESET_ALL}")
        print(f"  1) Otevři:  {Fore.CYAN}https://plex.tv/link{Style.RESET_ALL}")
        print(f"  2) Zadej kód:  {Fore.GREEN}{Style.BRIGHT}{code}{Style.RESET_ALL}")
        print(f"{Fore.MAGENTA}==============================================={Style.RESET_ALL}")
        print("Čekám na potvrzení", end="", flush=True)
        deadline = time.time() + wait_timeout
        while time.time() < deadline:
            time.sleep(poll)
            print(".", end="", flush=True)
            try:
                r = http_json("GET", f"{PINS_URL}/{pin_id}",
                              headers=self._headers(with_token=False))
            except Exception:
                continue
            if r.get("authToken"):
                print()
                self.token = r["authToken"]
                return self.token
        print()
        raise RuntimeError("Přihlášení vypršelo (PIN nebyl potvrzen).")

    # --- Home uživatelé (XML) ---------------------------------------------
    def home_users(self):
        st, text = http_raw("GET",
                            f"{HOMEUSERS_URL}?X-Plex-Client-Identifier={self.client_id}",
                            headers=self._headers(accept="application/xml"))
        if st == 401:
            raise RuntimeError("UNAUTHORIZED")
        if st >= 400:
            raise RuntimeError(f"HTTP {st} u home/users - {text[:200]}")
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            raise RuntimeError("Neočekávaná odpověď u home/users (nešlo naparsovat XML).")
        users = []
        for u in root.iter("user"):
            a = u.attrib
            users.append({
                "id": a.get("id"),
                "uuid": a.get("uuid"),
                "title": a.get("title") or a.get("username") or a.get("friendlyName") or "?",
                "admin": a.get("admin") == "1",
                "protected": a.get("protected") == "1",
                "restricted": a.get("restricted") == "1",
            })
        return users

    # --- přepnutí na uživatele s PINem -> uživatelský token ----------------
    def switch_user(self, user_id, pin=None):
        params = {}
        if pin:
            params["pin"] = pin
        url = SWITCH_URL.format(uid=user_id)
        if params:
            url += "?" + urllib.parse.urlencode(params)
        st, text = http_raw("POST", url, headers=self._headers(accept="application/xml"))
        if st in (401, 403):
            raise RuntimeError("PIN_INVALID")
        if st >= 400:
            raise RuntimeError(f"Přepnutí uživatele selhalo (HTTP {st}): {text[:200]}")
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            raise RuntimeError("Přepnutí: neočekávaná odpověď (XML).")
        tok = root.attrib.get("authenticationToken") or root.attrib.get("authToken")
        if not tok:
            el = root.find(".//user")
            if el is not None:
                tok = el.attrib.get("authenticationToken") or el.attrib.get("authToken")
        if not tok:
            raise RuntimeError("Přepnutí: token v odpovědi chybí.")
        return tok


# ---------------------------------------------------------------------------
# Plex server klient
# ---------------------------------------------------------------------------
class PlexClient:
    def __init__(self, base_url, token, client_id, verify=True, timeout=30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.client_id = client_id
        self.verify = verify
        self.timeout = timeout

    def _headers(self):
        return {
            "X-Plex-Token": self.token,
            "X-Plex-Product": PRODUCT,
            "X-Plex-Client-Identifier": self.client_id,
            "Accept": "application/json",
        }

    def _url(self, path, params=None):
        q = urllib.parse.urlencode(params or {})
        return f"{self.base_url}{path}" + (f"?{q}" if q else "")

    def get_json(self, path, params=None):
        st, text = http_raw("GET", self._url(path, params), headers=self._headers(),
                            verify=self.verify, timeout=self.timeout)
        if st == 401:
            raise RuntimeError("UNAUTHORIZED")
        if st >= 400:
            raise RuntimeError(f"HTTP {st} u {path} - {text[:200]}")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise RuntimeError(f"Server nevrátil JSON u {path}.")

    def put(self, path, params=None):
        st, text = http_raw("PUT", self._url(path, params), headers=self._headers(),
                            verify=self.verify, timeout=self.timeout)
        if st >= 400:
            raise RuntimeError(f"HTTP {st}: {text[:120]}")
        return st

    def check(self):
        j = self.get_json("/")
        mc = j.get("MediaContainer", {})
        return mc.get("friendlyName") or mc.get("machineIdentifier") or "Plex"

    def sections(self):
        j = self.get_json("/library/sections")
        return [{"key": d.get("key"), "title": d.get("title"), "type": d.get("type")}
                for d in j.get("MediaContainer", {}).get("Directory", [])]

    def items_in_section(self, section_key, sec_type, query=None):
        params = {}
        num = SECTION_TYPE_NUM.get(sec_type)
        if num:
            params["type"] = num
        if query:
            params["title"] = query
        j = self.get_json(f"/library/sections/{section_key}/all", params)
        return [{"ratingKey": m.get("ratingKey"), "title": m.get("title"),
                 "year": m.get("year"), "type": m.get("type")}
                for m in j.get("MediaContainer", {}).get("Metadata", [])]

    def get_metadata(self, rating_key):
        j = self.get_json(f"/library/metadata/{rating_key}")
        md = j.get("MediaContainer", {}).get("Metadata", [])
        return md[0] if md else None

    def all_episodes(self, show_key):
        j = self.get_json(f"/library/metadata/{show_key}/allLeaves")
        return j.get("MediaContainer", {}).get("Metadata", [])


# ---------------------------------------------------------------------------
# Pomůcky pro adresu serveru
# ---------------------------------------------------------------------------
def normalize_base_url(s):
    s = (s or "").strip().rstrip("/")
    if not s:
        return s
    if not re.match(r"^https?://", s, re.I):
        s = "https://" + s
    return s


def verify_for(base_url, verify_pref):
    # holé https na IP nemá platný certifikát
    if verify_pref and base_url.lower().startswith("https") and \
            re.search(r"//\d+\.\d+\.\d+\.\d+(:\d+)?", base_url):
        return False
    return verify_pref


def parse_show_ref(ref):
    if ref is None:
        return None
    ref = ref.strip()
    if ref.isdigit():
        return ref
    m = re.search(r"/library/metadata/(\d+)", urllib.parse.unquote(ref))
    if m:
        return m.group(1)
    m = re.search(r"metadata%2F(\d+)", ref)
    if m:
        return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Streamy
# ---------------------------------------------------------------------------
# Čitelné názvy jazyků podle ISO kódu (aby se nezobrazovaly znaky v cizím
# písmu, které konzole neumí vykreslit - např. korejština/čínština jako ☐☐☐).
LANG_NAMES = {
    "eng": "Angličtina", "en": "Angličtina",
    "cze": "Čeština", "ces": "Čeština", "cs": "Čeština",
    "slo": "Slovenština", "slk": "Slovenština", "sk": "Slovenština",
    "ger": "Němčina", "deu": "Němčina", "de": "Němčina",
    "fre": "Francouzština", "fra": "Francouzština", "fr": "Francouzština",
    "spa": "Španělština", "es": "Španělština",
    "ita": "Italština", "it": "Italština",
    "por": "Portugalština", "pt": "Portugalština",
    "rus": "Ruština", "ru": "Ruština",
    "ukr": "Ukrajinština", "uk": "Ukrajinština",
    "pol": "Polština", "pl": "Polština",
    "kor": "Korejština", "ko": "Korejština",
    "jpn": "Japonština", "ja": "Japonština",
    "chi": "Čínština", "zho": "Čínština", "zh": "Čínština",
    "dan": "Dánština", "da": "Dánština",
    "dut": "Nizozemština", "nld": "Nizozemština", "nl": "Nizozemština",
    "swe": "Švédština", "sv": "Švédština",
    "nor": "Norština", "no": "Norština",
    "fin": "Finština", "fi": "Finština",
    "hun": "Maďarština", "hu": "Maďarština",
    "gre": "Řečtina", "ell": "Řečtina", "el": "Řečtina",
    "tur": "Turečtina", "tr": "Turečtina",
    "ara": "Arabština", "ar": "Arabština",
    "heb": "Hebrejština", "he": "Hebrejština",
    "hin": "Hindština", "hi": "Hindština",
    "tha": "Thajština", "th": "Thajština",
    "vie": "Vietnamština", "vi": "Vietnamština",
    "ron": "Rumunština", "rum": "Rumunština", "ro": "Rumunština",
    "bul": "Bulharština", "bg": "Bulharština",
    "hrv": "Chorvatština", "hr": "Chorvatština",
    "srp": "Srbština", "sr": "Srbština",
    "slv": "Slovinština", "sl": "Slovinština",
}


def _is_latin(s):
    """True, pokud text neobsahuje písmena mimo latinku (tj. konzole ho zvládne)."""
    return all((ord(ch) < 0x250) or (not ch.isalpha()) for ch in s)


def lang_name(code, plex_name=None):
    """Čitelný název jazyka: primárně podle ISO kódu, jinak Plexův název
    (jen pokud je v latince), jinak samotný kód."""
    c = (code or "").lower()
    if c in LANG_NAMES:
        return LANG_NAMES[c]
    if plex_name and _is_latin(plex_name):
        return plex_name
    return code or "?"


def stream_is_external(s):
    """Externí (sidecar) titulek pozná podle přítomnosti 'key' nebo příznaku."""
    return bool(s.get("key")) or str(s.get("external", "")).lower() in ("1", "true")


def stream_signature(s):
    """Stabilní 'podpis' stopy napříč díly (ID se u dílů liší, tohle ne).
    Rozlišuje jazyk, kodek, externí/vložené, forced, SDH, region i title."""
    code = (s.get("languageCode") or s.get("languageTag") or "").lower()
    codec = (s.get("codec") or "").lower()
    ext = stream_is_external(s)
    forced = str(s.get("forced", "")).lower() in ("1", "true")
    sdh = str(s.get("hearingImpaired", "")).lower() in ("1", "true")
    name = (s.get("language") or "").strip().lower()
    title = (s.get("title") or "").strip().lower()
    return (code, codec, ext, forced, sdh, name, title)


def variant_label(s):
    """Čitelný popis stopy. Přednostně použije Plexův displayTitle (to, co je
    vidět v Plexu), aby šly rozlišit i multiple externí titulky; jinak sestaví
    popis z komponent. Jazyk v cizím písmu nahradí českým názvem."""
    code = (s.get("languageCode") or "").lower()
    # 1) Plexův vlastní popis (např. "Czech (SRT External)", "English (SRT, SDH)")
    disp = (s.get("extendedDisplayTitle") or s.get("displayTitle") or "").strip()
    if disp and _is_latin(disp):
        label = disp
        if code and f"[{code}]" not in label and f"({code})" not in label:
            label += f" [{code}]"
        return label
    # 2) fallback: sestav z komponent
    base = lang_name(code, s.get("language"))
    raw = (s.get("language") or "").strip()
    if raw and _is_latin(raw) and raw.lower() != base.lower() and ("(" in raw or len(raw.split()) > 1):
        base = raw
    quals = []
    codec = (s.get("codec") or "").upper()
    if codec:
        quals.append(codec)
    if stream_is_external(s):
        quals.append("externí")
    if str(s.get("forced", "")).lower() in ("1", "true"):
        quals.append("forced")
    if str(s.get("hearingImpaired", "")).lower() in ("1", "true"):
        quals.append("SDH")
    title = (s.get("title") or "").strip()
    if title and _is_latin(title) and title.lower() not in base.lower():
        quals.append(title)
    label = base + (f" [{code}]" if code else "")
    if quals:
        label += f" ({', '.join(quals)})"
    return label


def variant_short(v):
    """Krátký identifikátor do souhrnu: 'ces', 'eng#2', 'ces·ext'."""
    parts = [v.get("code") or "?"]
    if v.get("ordinal", 0) > 0:
        parts[0] += f"#{v['ordinal'] + 1}"
    if v.get("external"):
        parts.append("ext")
    return "·".join(parts)


def iter_parts(md):
    for media in md.get("Media", []):
        for part in media.get("Part", []):
            yield part.get("id"), part.get("Stream", [])


def lang_key(stream):
    code = (stream.get("languageCode") or stream.get("languageTag") or "").lower()
    name = stream.get("language") or stream.get("displayTitle") or code or "?"
    return code, name


def _new_variant(s, ordinal):
    return {"key": (stream_signature(s), ordinal), "sig": stream_signature(s),
            "ordinal": ordinal, "code": (s.get("languageCode") or "").lower(),
            "label": variant_label(s), "external": stream_is_external(s), "count": 1}


def _finalize_variants(reg):
    """Z registru {key: variant} udělá seřazený seznam a odliší duplicitní popisky."""
    from collections import Counter
    vs = list(reg.values())
    lab = Counter(v["label"] for v in vs)
    for v in vs:
        if lab[v["label"]] > 1:
            v["label"] = f"{v['label']} #{v['ordinal'] + 1}"
    vs.sort(key=lambda v: (v["label"].lower(), v["ordinal"]))
    return vs


def streams_variants(streams, stream_type):
    """Varianty (konkrétní stopy) daného typu v jednom partu, s pořadím u shodných."""
    reg = {}
    seen = {}
    for s in streams:
        if s.get("streamType") != stream_type:
            continue
        sig = stream_signature(s)
        ordinal = seen.get(sig, 0)
        seen[sig] = ordinal + 1
        key = (sig, ordinal)
        if key not in reg:
            reg[key] = _new_variant(s, ordinal)
    return reg


def collect_streams(client, items, fetch_full):
    """Vrátí (items_data, audio_variants, sub_variants), kde varianty jsou
    seznamy konkrétních stop (ne jen jazyků) sloučené napříč díly podle podpisu."""
    items_data = []
    audio_reg, sub_reg = {}, {}
    total = len(items)
    for i, it in enumerate(items, 1):
        rk = it.get("ratingKey")
        md = it.get("_md") or client.get_metadata(rk)
        parts = list(iter_parts(md))
        for _pid, streams in parts:
            for stt, reg in ((ST_AUDIO, audio_reg), (ST_SUBTITLE, sub_reg)):
                for key, v in streams_variants(streams, stt).items():
                    if key in reg:
                        reg[key]["count"] += 1
                    else:
                        reg[key] = v
        items_data.append({
            "ratingKey": rk,
            "title": it.get("title") or md.get("title") or f"#{rk}",
            "s": it.get("parentIndex"),
            "e": it.get("index"),
            "parts": parts,
        })
        if total > 1:
            pct = i * 100 // total
            print(f"\r  {Fore.CYAN}skenuji streamy: {pct:3d}%{Style.RESET_ALL} "
                  f"({i}/{total})   ", end="", flush=True)
    if total > 1:
        print()
    return items_data, _finalize_variants(audio_reg), _finalize_variants(sub_reg)


def pick_variant_id(streams, stream_type, variant):
    """ID konkrétní stopy v tomto partu podle podpisu+pořadí (ID se u dílů liší)."""
    sig, ordinal = variant["key"]
    cnt = 0
    for s in streams:
        if s.get("streamType") != stream_type:
            continue
        if stream_signature(s) == sig:
            if cnt == ordinal:
                return s.get("id")
            cnt += 1
    return None


def item_has_variant(item, stream_type, variant):
    for _pid, streams in item["parts"]:
        if pick_variant_id(streams, stream_type, variant) is not None:
            return True
    return False


def item_available_variants(item, stream_type):
    reg = {}
    for _pid, streams in item["parts"]:
        for key, v in streams_variants(streams, stream_type).items():
            reg.setdefault(key, v)
    return _finalize_variants(reg)


# ---------------------------------------------------------------------------
# Výběr stopy
# ---------------------------------------------------------------------------
def choose_variant(kind, variants, allow_off=False, allow_keep=True, allow_back=False):
    labels = [v["label"] for v in variants]
    off_idx = keep_idx = None
    all_labels = list(labels)
    if allow_off:
        off_idx = len(all_labels)
        all_labels.append(f"{Fore.MAGENTA}— VYPNOUT (žádné titulky) —{Style.RESET_ALL}")
    if allow_keep:
        keep_idx = len(all_labels)
        all_labels.append(f"{Fore.MAGENTA}— nechat beze změny —{Style.RESET_ALL}")
    if not all_labels:
        log_warn(f"Žádné {kind} stopy nenalezeny.")
        return ("keep", None)
    default_idx = keep_idx if keep_idx is not None else 0
    idx = interactive_menu(f"Vyber výchozí {kind}:", all_labels,
                           default=default_idx, allow_cancel=allow_back)
    if idx is None:
        return ("back", None)
    if idx < len(variants):
        return ("var", variants[idx])
    if off_idx is not None and idx == off_idx:
        return ("off", None)
    return ("keep", None)


def resolve_variant_arg(arg, variants, kind, allow_off):
    """CLI --audio/--subs: 'keep'/'off'/'0', jazykový kód, nebo část popisku."""
    if arg is None:
        return None
    a = arg.strip().lower()
    if a in ("keep", "-", "nechat"):
        return ("keep", None)
    if allow_off and a in ("off", "none", "vypnout", "0"):
        return ("off", None)
    cands = [v for v in variants if v["code"] == a]
    if not cands:
        cands = [v for v in variants if a in v["label"].lower()]
    if cands:
        cands.sort(key=lambda v: -v.get("count", 0))
        return ("var", cands[0])
    die(f"{kind}: '{arg}' není dostupné. Kódy: "
        + (", ".join(sorted({v['code'] for v in variants})) or "(žádné)"))


# ---------------------------------------------------------------------------
# Aplikace změn
# ---------------------------------------------------------------------------
def ep_label(it):
    if it["s"] is not None and it["e"] is not None:
        try:
            return f"S{int(it['s']):02d}E{int(it['e']):02d}"
        except Exception:
            pass
    return it["title"]


def item_has_lang(item, stream_type, variant):
    return item_has_variant(item, stream_type, variant)


def resolve_coverage(kind, action, items_data, stream_type, allow_off, interactive):
    """Vrátí per-položkový plán (délka jako items_data), prvek:
       ('var', variant) | ('off', None) | ('skip', None).
    Když nějaké díly danou stopu nemají a běžíme interaktivně, zeptá se na náhradu."""
    n = len(items_data)
    if action[0] == "keep":
        return [("skip", None)] * n
    if action[0] == "off":
        return [("off", None)] * n

    variant = action[1]
    plan = [None] * n
    missing = []
    for i, it in enumerate(items_data):
        if item_has_variant(it, stream_type, variant):
            plan[i] = ("var", variant)
        else:
            plan[i] = ("skip", None)
            missing.append(i)

    if not missing:
        return plan

    log_warn(f"{len(missing)} z {n} položek nemá {kind}: {variant['label']}")
    lbls = [ep_label(items_data[i]) for i in missing]
    print("   " + ", ".join(lbls[:30]) + (" …" if len(lbls) > 30 else ""))

    if not interactive:
        log_info(f"Neinteraktivní režim: těchto {len(missing)} položek ponechávám beze změny.")
        return plan

    while missing:
        # dostupné náhradní varianty napříč chybějícími (dedup podle key)
        avail = {}
        cnt = {}
        for i in missing:
            for v in item_available_variants(items_data[i], stream_type):
                if v["key"] == variant["key"]:
                    continue
                avail.setdefault(v["key"], v)
                cnt[v["key"]] = cnt.get(v["key"], 0) + 1
        if not avail:
            log_info("Chybějící položky nemají žádnou jinou stopu — ponechávám je beze změny.")
            break
        opts = sorted(avail.values(), key=lambda v: (-cnt[v["key"]], v["label"].lower()))
        labels = [f"{v['label']}  (má {cnt[v['key']]}/{len(missing)} chybějících)" for v in opts]
        extra = []
        if allow_off:
            extra.append(f"{Fore.MAGENTA}— VYPNOUT titulky u chybějících —{Style.RESET_ALL}")
        extra.append(f"{Fore.MAGENTA}— nechat chybějící beze změny (přeskočit) —{Style.RESET_ALL}")
        idx = ask_choice(f"Čím nahradit {kind} u chybějících dílů?",
                         labels + extra, default=len(labels) + len(extra) - 1)
        if idx < len(opts):
            alt = opts[idx]
            still, applied = [], 0
            for i in missing:
                if item_has_variant(items_data[i], stream_type, alt):
                    plan[i] = ("var", alt)
                    applied += 1
                else:
                    still.append(i)
            log_done(f"Nastaveno '{alt['label']}' u {applied} dílů.")
            missing = still
            if missing:
                log_info(f"{len(missing)} dílů tuto náhradu nemá — vyber další.")
        else:
            sel = extra[idx - len(opts)]
            if allow_off and "VYPNOUT" in sel:
                for i in missing:
                    plan[i] = ("off", None)
                log_done(f"Titulky u {len(missing)} chybějících dílů vypnuty.")
            else:
                log_info(f"{len(missing)} dílů ponecháno beze změny.")
            break
    return plan


def plan_summary(plan):
    from collections import Counter
    c = Counter()
    for p in plan:
        if p[0] == "var":
            c[variant_short(p[1])] += 1
        elif p[0] == "off":
            c["VYPNUTO"] += 1
        else:
            c["beze změny"] += 1
    return ", ".join(f"{k}: {v}×" for k, v in c.items())


def apply_changes(client, items_data, audio_plan, sub_plan, dry_run):
    ok = fail = changed = 0
    for i, it in enumerate(items_data):
        se = ep_label(it)
        se = (se + " ") if se else ""
        a = audio_plan[i]
        s = sub_plan[i]
        for part_id, streams in it["parts"]:
            params = {"allParts": 1}
            notes = []
            if a[0] == "var":
                aid = pick_variant_id(streams, ST_AUDIO, a[1])  # ID se hledá PER DÍL
                if aid is not None:
                    params["audioStreamID"] = aid
                    notes.append(f"audio->{variant_short(a[1])}")
                else:
                    notes.append(f"{Fore.YELLOW}audio '{variant_short(a[1])}' chybí{Style.RESET_ALL}")
            if s[0] == "off":
                params["subtitleStreamID"] = 0
                notes.append("titulky->OFF")
            elif s[0] == "var":
                tid = pick_variant_id(streams, ST_SUBTITLE, s[1])  # ID se hledá PER DÍL
                if tid is not None:
                    params["subtitleStreamID"] = tid
                    notes.append(f"titulky->{variant_short(s[1])}")
                else:
                    notes.append(f"{Fore.YELLOW}titulky '{variant_short(s[1])}' chybí{Style.RESET_ALL}")
            if "audioStreamID" not in params and "subtitleStreamID" not in params:
                continue
            label = f"{se}{it['title']} (part {part_id}): " + ", ".join(notes)
            if dry_run:
                print(f"  {Fore.CYAN}[plán]{Style.RESET_ALL} {label}")
                changed += 1
                continue
            try:
                client.put(f"/library/parts/{part_id}", params)
                print(f"  {Fore.GREEN}+{Style.RESET_ALL} {label}")
                ok += 1
                changed += 1
            except Exception as ex:
                print(f"  {Fore.RED}x{Style.RESET_ALL} {label}  -> {ex}")
                fail += 1
    return ok, fail, changed


# ---------------------------------------------------------------------------
# Přihlášení / získání tokenu (uloží do configu)
# ---------------------------------------------------------------------------
def account_login(acct):
    """Interaktivně získá účtový token (heslo nebo plex.tv/link). Uloží do acct.token."""
    idx = ask_choice("Přihlášení k Plex účtu:", [
        "Jméno/email + heslo",
        "Kód na plex.tv/link (bez hesla)",
    ])
    if idx == 0:
        login = ask_text("Plex jméno nebo email")
        pw = ask_secret("Heslo")
        try:
            acct.signin_password(login, pw)
        except RuntimeError as ex:
            if "UNAUTHORIZED" in str(ex):
                code = ask_text("Dvoufázový (2FA) kód, pokud ho máš zapnutý (jinak Enter)")
                if code:
                    try:
                        acct.signin_password(login, pw, code=code)
                    except RuntimeError:
                        die("Přihlášení selhalo (špatné jméno/heslo nebo 2FA kód).")
                else:
                    die("Přihlášení selhalo (špatné jméno/heslo, nebo chybí 2FA kód).")
            else:
                die(str(ex))
    else:
        acct.pin_login()
    log_done("Přihlášeno k účtu.")
    return acct.token


def choose_home_user(acct, cfg):
    """Vybere Home uživatele a získá jeho token (přes switch + PIN). Uloží do cfg."""
    users = acct.home_users()
    if not users:
        die("Účet nemá žádné Home uživatele.")
    if len(users) == 1:
        chosen = users[0]
    else:
        labels = [u["title"] + (" (admin)" if u["admin"] else "")
                  + (" 🔒PIN" if u["protected"] else "") for u in users]
        chosen = users[ask_choice("Vyber Plex Home uživatele:", labels)]

    pin = None
    if chosen["protected"]:
        pin = ask_text(f"PIN uživatele '{chosen['title']}'")
    while True:
        try:
            user_token = acct.switch_user(chosen["id"], pin=pin)
            break
        except RuntimeError as ex:
            if "PIN_INVALID" in str(ex):
                log_warn("Neplatný PIN.")
                pin = ask_text(f"PIN uživatele '{chosen['title']}' (znovu)")
                continue
            die(str(ex))
    cfg["home_user"] = {"id": chosen["id"], "uuid": chosen["uuid"],
                        "title": chosen["title"], "protected": chosen["protected"]}
    cfg["user_token"] = user_token
    save_config(cfg)
    log_done(f"Přepnuto na uživatele: {chosen['title']}")
    return user_token


def full_login(cfg, client_id):
    """Kompletní přihlášení: účet -> Home uživatel + PIN. Vrátí user_token."""
    acct = PlexAccount(client_id)
    account_login(acct)
    cfg["account_token"] = acct.token
    save_config(cfg)
    return choose_home_user(acct, cfg)


def reswitch(cfg, client_id):
    """Když vyprší user_token: přepni znovu (PIN) přes uložený account_token."""
    acct = PlexAccount(client_id, cfg.get("account_token"))
    hu = cfg.get("home_user") or {}
    try:
        pin = ask_text(f"PIN uživatele '{hu.get('title','?')}'") if hu.get("protected") else None
        while True:
            try:
                tok = acct.switch_user(hu["id"], pin=pin)
                break
            except RuntimeError as ex:
                if "PIN_INVALID" in str(ex):
                    log_warn("Neplatný PIN.")
                    pin = ask_text("PIN (znovu)")
                    continue
                raise
        cfg["user_token"] = tok
        save_config(cfg)
        return tok
    except RuntimeError:
        # account token asi taky neplatný -> celé znovu
        log_warn("Uložené přihlášení vypršelo, přihlas se znovu.")
        return full_login(cfg, client_id)


def build_client(args, cfg):
    client_id = get_client_id(cfg)
    verify_pref = not args.insecure

    # adresa serveru: arg -> config -> zeptat se (a uložit)
    base_url = normalize_base_url(args.base_url) if args.base_url else cfg.get("base_url")
    if not base_url:
        base_url = normalize_base_url(
            ask_text("Zadej FQDN / adresu Plex serveru (např. https://plex.falco81.net)"))
        if not base_url:
            die("Adresa serveru je povinná.")
        cfg["base_url"] = base_url
        save_config(cfg)
    elif args.base_url:
        cfg["base_url"] = base_url
        save_config(cfg)

    def _connect(token):
        """Připojí se k serveru. Při chybě certifikátu automaticky přepne na
        nezabezpečené HTTPS (vlastní server) a zapamatuje si to. Vrací (client, name)."""
        v = False if cfg.get("insecure_server") else verify_for(base_url, verify_pref)
        client = PlexClient(base_url, token, client_id, verify=v)
        try:
            return client, client.check()
        except RuntimeError as ex:
            msg = str(ex).upper()
            if v and ("CERTIFIC" in msg or "SSL" in msg):
                log_warn("Certifikát serveru nešel ověřit (nekompletní řetěz / "
                         "chybí CA) — přepínám na nezabezpečené HTTPS pro tvůj server.")
                log_info("Tip: `pip install truststore` umožní bezpečné ověření přes "
                         "úložiště certifikátů Windows.")
                cfg["insecure_server"] = True
                save_config(cfg)
                client = PlexClient(base_url, token, client_id, verify=False)
                return client, client.check()
            raise

    # přímý token (power-user override)
    if args.token:
        client, name = _connect(args.token)
        log_done(f"Připojeno k serveru: {name}")
        return client

    # už máme uložený uživatelský token? -> zkus rovnou
    user_token = cfg.get("user_token")
    if user_token:
        try:
            client, name = _connect(user_token)
            log_done(f"Připojeno jako '{(cfg.get('home_user') or {}).get('title','?')}' k serveru: {name}")
            return client
        except RuntimeError as ex:
            if "UNAUTHORIZED" in str(ex):
                log_warn("Uložený uživatelský token vypršel - obnovuji.")
                user_token = reswitch(cfg, client_id)
                client, name = _connect(user_token)
                log_done(f"Připojeno k serveru: {name}")
                return client
            die(f"Server nedostupný: {ex}")

    # první přihlášení
    user_token = full_login(cfg, client_id)
    client, name = _connect(user_token)
    log_done(f"Připojeno k serveru: {name}")
    return client


# ---------------------------------------------------------------------------
# Výběr cíle (seriál -> epizody, film -> sám sebe)
# ---------------------------------------------------------------------------
def _target_from_item(client, item):
    if item["type"] == "show":
        eps = client.all_episodes(item["ratingKey"])
        if not eps:
            die("Seriál nemá epizody.")
        return eps, f"seriál '{item['title']}' ({len(eps)} epizod)", True
    md = client.get_metadata(item["ratingKey"])
    md["_md"] = md
    return [md], f"film '{item['title']}'", False


def _target_from_md(client, md):
    if md.get("type") == "show":
        eps = client.all_episodes(md.get("ratingKey"))
        return eps, f"seriál '{md.get('title')}' ({len(eps)} epizod)", True
    md["_md"] = md
    return [md], f"'{md.get('title')}'", False


def scan_target(client, target):
    """Načte streamy cíle a vypíše dostupné stopy. Vrátí (items_data, audio, subs)."""
    its, label, ff = target
    log_info(f"Cíl: {Fore.CYAN}{label}{Style.RESET_ALL}")
    if ff:
        log_info("Skenuji dostupné stopy napříč epizodami...")
    idata, audio, subs = collect_streams(client, its, ff)
    print()
    log_info("Dostupné ZVUKOVÉ stopy: " +
             (" | ".join(v["label"] for v in audio) or "(žádné)"))
    log_info("Dostupné TITULKY:       " +
             (" | ".join(v["label"] for v in subs) or "(žádné)"))
    print()
    return idata, audio, subs


def select_and_configure(client, args):
    """Průvodce: knihovna -> položka -> audio -> titulky, s návratem o krok zpět
    (Esc). Vrátí (items_data, audio_action, sub_action) nebo None."""
    # --show: pevně daná položka (bez kroků knihovna/položka)
    fixed = None
    if args.show:
        rk = parse_show_ref(args.show)
        if rk:
            md = client.get_metadata(rk)
            if not md:
                die(f"ratingKey {rk} nenalezen.")
            fixed = _target_from_md(client, md)
        else:
            found = []
            for sec in client.sections():
                found += client.items_in_section(sec["key"], sec["type"], query=args.show)
            if not found:
                die(f"'{args.show}' nenalezeno.")
            if len(found) == 1:
                fixed = _target_from_item(client, found[0])
            else:
                labels = [f"{f['title']} ({f['year']}) [{f['type']}]" for f in found]
                i = interactive_menu("Nalezeno více - vyber:", labels, allow_cancel=True)
                if i is None:
                    return None
                fixed = _target_from_item(client, found[i])

    secs = client.sections()
    if not secs:
        die("Server nemá žádné knihovny.")
    single_lib = len(secs) == 1
    have_item_step = fixed is None

    audio_interactive = args.audio is None
    subs_interactive = args.subs is None

    LIB, ITEM, AUDIO, SUBS = range(4)
    sec = secs[0] if single_lib else None
    scan_cache = {}
    idata = audio_vars = sub_vars = None
    audio_action = sub_action = None

    if fixed:
        idata, audio_vars, sub_vars = scan_target(client, fixed)
        step = AUDIO
    else:
        step = ITEM if single_lib else LIB

    while True:
        if step == LIB:
            labels = [f"{s['title']}  [{s['type']}]" for s in secs]
            i = interactive_menu("Vyber knihovnu:", labels, allow_cancel=True)
            if i is None:
                return None
            sec = secs[i]
            step = ITEM

        elif step == ITEM:
            items = client.items_in_section(sec["key"], sec["type"])
            if not items:
                log_warn("Knihovna je prázdná.")
                if single_lib:
                    return None
                step = LIB
                continue
            items.sort(key=lambda x: (x["title"] or "").lower())
            labels = [f"{it['title']} ({it['year']})" if it["year"] else it["title"] for it in items]
            i = interactive_menu("Vyber seriál/film:", labels, allow_cancel=True)
            if i is None:
                if single_lib:
                    return None
                step = LIB
                continue
            chosen = items[i]
            rk = chosen["ratingKey"]
            if rk not in scan_cache:
                target = _target_from_item(client, chosen)
                scan_cache[rk] = (target, scan_target(client, target))
            _t, (idata, audio_vars, sub_vars) = scan_cache[rk]
            step = AUDIO

        elif step == AUDIO:
            if not audio_interactive:
                audio_action = resolve_variant_arg(args.audio, audio_vars, "zvuk", False)
                step = SUBS
                continue
            audio_action = choose_variant("zvuk (audio)", audio_vars, allow_off=False, allow_back=True)
            if audio_action[0] == "back":
                if have_item_step:
                    step = ITEM
                    continue
                return None
            step = SUBS

        elif step == SUBS:
            if not subs_interactive:
                sub_action = resolve_variant_arg(args.subs, sub_vars, "titulky", True)
                break
            sub_action = choose_variant("titulky", sub_vars, allow_off=True, allow_back=True)
            if sub_action[0] == "back":
                if audio_interactive:
                    step = AUDIO
                elif have_item_step:
                    step = ITEM
                else:
                    return None
                continue
            break

    if audio_action[0] == "keep" and sub_action[0] == "keep":
        log_warn("Žádná změna nevybrána. Končím.")
        return None
    return idata, audio_action, sub_action


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Hromadně nastaví výchozí audio/titulky u seriálu nebo filmu na Plexu.")
    ap.add_argument("--base-url", help="Adresa/FQDN serveru (jinak se použije uložená / zeptá se)")
    ap.add_argument("--token", help="Přímo X-Plex-Token (přeskočí přihlášení)")
    ap.add_argument("--show", help="ratingKey, Plex URL, nebo název seriálu/filmu")
    ap.add_argument("--audio", help="Kód/název audio jazyka nebo 'keep'")
    ap.add_argument("--subs", help="Kód/název titulků, 'off' nebo 'keep'")
    ap.add_argument("--insecure", action="store_true", help="Nevalidovat HTTPS certifikát")
    ap.add_argument("--dry-run", action="store_true", help="Jen ukázat plán")
    ap.add_argument("--yes", action="store_true", help="Neptat se na potvrzení")
    ap.add_argument("--logout", action="store_true", help="Smazat uložené tokeny a skončit")
    ap.add_argument("--relogin", action="store_true", help="Vynutit nové přihlášení")
    ap.add_argument("--switch-user", action="store_true", help="Znovu vybrat Home uživatele")
    args = ap.parse_args()

    cfg = load_config()
    client_id = get_client_id(cfg)

    if args.logout:
        for k in ("account_token", "user_token", "home_user"):
            cfg.pop(k, None)
        save_config(cfg)
        log_done("Odhlášeno (tokeny smazány). Adresa serveru zůstala uložená.")
        return

    if args.relogin:
        for k in ("account_token", "user_token", "home_user"):
            cfg.pop(k, None)
        save_config(cfg)

    if args.switch_user and cfg.get("account_token"):
        acct = PlexAccount(client_id, cfg["account_token"])
        try:
            choose_home_user(acct, cfg)
        except RuntimeError as ex:
            if "UNAUTHORIZED" in str(ex):
                cfg.pop("account_token", None)
                save_config(cfg)
            else:
                die(str(ex))

    print(f"{Fore.MAGENTA}=== plex_def_langsubs - výchozí audio/titulky ==={Style.RESET_ALL}")

    client = build_client(args, cfg)

    result = select_and_configure(client, args)
    if result is None:
        log_info("Zrušeno.")
        return
    items_data, audio_action, sub_action = result

    # sestav per-díl plán; když nějaké díly stopu nemají, zeptej se na náhradu
    interactive = not args.yes
    print()
    audio_plan = resolve_coverage("audio", audio_action, items_data, ST_AUDIO,
                                  allow_off=False, interactive=interactive)
    sub_plan = resolve_coverage("titulky", sub_action, items_data, ST_SUBTITLE,
                                allow_off=True, interactive=interactive)

    # je vůbec co dělat?
    if all(p[0] == "skip" for p in audio_plan) and all(p[0] == "skip" for p in sub_plan):
        log_warn("Nakonec není co měnit. Končím.")
        return

    print()
    log_info(f"Nastavím u {len(items_data)} položek:")
    print(f"    audio:   {plan_summary(audio_plan)}")
    print(f"    titulky: {plan_summary(sub_plan)}")
    print()

    dry = args.dry_run
    if not dry and not args.yes:
        if not ask_yes("Provést změnu?", default=True):
            dry = ask_yes("Aspoň zobrazit plán (dry-run)?", default=True)
            if not dry:
                log_info("Zrušeno.")
                return

    print()
    ok, fail, changed = apply_changes(client, items_data, audio_plan, sub_plan, dry)
    print()
    if dry:
        log_info(f"Dry-run: naplánováno {changed} úprav (nic neuloženo).")
    else:
        color = Fore.GREEN if fail == 0 else Fore.YELLOW
        print(f"{color}Hotovo: {ok} úspěšně"
              + (f", {fail} chyb" if fail else "")
              + f" (dotčeno {changed} částí).{Style.RESET_ALL}")
        log_info("V klientu se změna projeví po obnovení / novém přehrání.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        log_warn("Přerušeno uživatelem.")
        sys.exit(130)
