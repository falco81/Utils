#!/usr/bin/env python3
"""
plex-diag.py — responsiveness / speed diagnostics for a self-hosted Plex server.

Measures where latency actually comes from: the reverse proxy, TLS, or the Plex
database. Read-only: it never modifies Plex, the database or any config.

Usage:
    python3 plex-diag.py                          # local checks + direct API timings
    python3 plex-diag.py --proxy https://plex.example.net
    python3 plex-diag.py --runs 15 --json report.json

Only the Python standard library is used, so it runs on a stock AlmaLinux box.
"""

import argparse
import http.client
import json
import os
import re
import shutil
import socket
import ssl
import statistics
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PLEX_HOST = "127.0.0.1"
PLEX_PORT = 32400
PREFS = Path("/var/lib/plexmediaserver/Library/Application Support/"
             "Plex Media Server/Preferences.xml")
DB_DIR = Path("/var/lib/plexmediaserver/Library/Application Support/"
              "Plex Media Server/Plug-in Support/Databases")
LIB_DB = DB_DIR / "com.plexapp.plugins.library.db"

# Reverse proxy in front of Plex. Set it here to always run the full
# through-the-proxy comparison without passing --proxy every time.
#   e.g. PROXY_URL = "https://plex.example.net"
# Leave it empty ("") to auto-detect the vhost from the nginx config instead.
# Priority:  --proxy argument  >  this constant  >  auto-detection
#            (--no-proxy overrides everything and skips the proxy layer)
PROXY_URL = "https://plex.falco81.net"

# thresholds (ms) for judging an endpoint
GOOD, SLOW = 100.0, 500.0


# --------------------------------------------------------------------------- #
# output helpers
# --------------------------------------------------------------------------- #
class C:
    """ANSI colours, disabled when not writing to a terminal."""
    on = sys.stdout.isatty()
    R = "\033[31m" if on else ""
    G = "\033[32m" if on else ""
    Y = "\033[33m" if on else ""
    # cyan (36), deliberately not blue (34): dark blue is barely legible on
    # PuTTY's default dark background
    B = "\033[36m" if on else ""
    DIM = "\033[2m" if on else ""
    BOLD = "\033[1m" if on else ""
    OFF = "\033[0m" if on else ""


def head(title):
    print(f"\n{C.BOLD}{title}{C.OFF}")
    print("-" * len(title))


def verdict(ms):
    if ms is None:
        return f"{C.DIM}n/a{C.OFF}"
    if ms < GOOD:
        return f"{C.G}good{C.OFF}"
    if ms < SLOW:
        return f"{C.Y}slow{C.OFF}"
    return f"{C.R}BAD{C.OFF}"


def human_bytes(n):
    v = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024:
            return f"{v:.1f} {u}"
        v /= 1024
    return f"{v:.1f} PB"


def run(cmd):
    """Run a shell command, return stdout or '' — never raises."""
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=15).stdout.strip()
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# token
# --------------------------------------------------------------------------- #
def find_token(cli_token):
    """Returns (token, source). Auto-detects from Preferences.xml by default."""
    if cli_token:
        return cli_token, "--token argument"
    env = os.environ.get("PLEX_TOKEN")
    if env:
        return env, "PLEX_TOKEN environment variable"
    try:
        text = PREFS.read_text(errors="ignore")
        m = re.search(r'PlexOnlineToken="([^"]+)"', text)
        if m:
            return m.group(1), "Preferences.xml"
    except PermissionError:
        return None, "Preferences.xml unreadable (run as root)"
    except Exception:
        pass
    return None, None


def mask(token):
    if not token:
        return "-"
    return token[:4] + "…" + token[-3:] if len(token) > 8 else "…"


def fetch(path, token, host=PLEX_HOST, port=PLEX_PORT, timeout=15):
    """
    GET a Plex endpoint and return the body as text.
    Uses a native socket rather than shelling out to curl, so the token never
    appears in the process list.
    """
    url = path + (("&" if "?" in path else "?") + "X-Plex-Token=" +
                  urllib.parse.quote(token) if token else "")
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request("GET", url)
        body = conn.getresponse().read().decode("utf-8", "replace")
        conn.close()
        return body
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# timing core
# --------------------------------------------------------------------------- #
def time_direct(path, token, runs, host=PLEX_HOST, port=PLEX_PORT):
    """
    Time an endpoint over ONE reused HTTP connection (like a browser does).
    Returns (list_of_ms, status, bytes, error).
    """
    url = path + (("&" if "?" in path else "?") + "X-Plex-Token=" +
                  urllib.parse.quote(token) if token else "")
    times, status, size, err = [], None, 0, None
    conn = None
    try:
        conn = http.client.HTTPConnection(host, port, timeout=20)
        conn.connect()
        # without this, Nagle + delayed ACK can add a bogus ~40 ms to small requests
        conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        for _ in range(runs):
            t0 = time.perf_counter()
            conn.request("GET", url, headers={"Accept": "application/json"})
            resp = conn.getresponse()
            body = resp.read()
            times.append((time.perf_counter() - t0) * 1000)
            status, size = resp.status, len(body)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return times, status, size, err


def time_proxy(base, path, token, runs, insecure=True):
    """Same, but through the reverse proxy over HTTPS on one reused connection."""
    u = urllib.parse.urlparse(base)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    url = path + (("&" if "?" in path else "?") + "X-Plex-Token=" +
                  urllib.parse.quote(token) if token else "")
    times, status, err = [], None, None
    conn = None
    try:
        if u.scheme == "https":
            ctx = ssl.create_default_context()
            if insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection(host, port, timeout=20, context=ctx)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=20)
        conn.connect()
        try:
            conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        for _ in range(runs):
            t0 = time.perf_counter()
            conn.request("GET", url, headers={"Accept": "application/json"})
            resp = conn.getresponse()
            resp.read()
            times.append((time.perf_counter() - t0) * 1000)
            status = resp.status
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return times, status, err


def handshake_breakdown(base, insecure=True):
    """Cost of establishing a fresh connection: TCP vs TLS (paid once per connection)."""
    u = urllib.parse.urlparse(base)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    out = {"tcp_ms": None, "tls_ms": None, "error": None}
    try:
        t0 = time.perf_counter()
        sock = socket.create_connection((host, port), timeout=10)
        out["tcp_ms"] = (time.perf_counter() - t0) * 1000
        if u.scheme == "https":
            ctx = ssl.create_default_context()
            if insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            t1 = time.perf_counter()
            ssock = ctx.wrap_socket(sock, server_hostname=host)
            out["tls_ms"] = (time.perf_counter() - t1) * 1000
            out["tls_version"] = ssock.version()
            out["cipher"] = ssock.cipher()[0] if ssock.cipher() else None
            ssock.close()
        else:
            sock.close()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def stats(times):
    """Ignore the first sample (connection warm-up) when we have enough runs."""
    if not times:
        return None
    warm = times[1:] if len(times) > 2 else times
    return {
        "first_ms": times[0],
        "median_ms": statistics.median(warm),
        "min_ms": min(warm),
        "max_ms": max(warm),
        "n": len(times),
    }


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
def check_service():
    head("Plex service")
    active = run("systemctl is-active plexmediaserver") == "active"
    print(f"  systemd unit      : {C.G + 'active' + C.OFF if active else C.R + 'NOT active' + C.OFF}")
    ident, _, _, err = time_direct("/identity", None, 1)
    if err:
        print(f"  API reachable     : {C.R}no{C.OFF} ({err})")
        return False
    print(f"  API reachable     : {C.G}yes{C.OFF} on {PLEX_HOST}:{PLEX_PORT}")
    ver = fetch("/identity", None)
    m = re.search(r'<MediaContainer\b[^>]*\bversion="([^"]+)"', ver or "")
    if m:
        print(f"  server version    : {m.group(1)}")
    return True


def check_endpoints(token, runs):
    head(f"API response times (one reused connection, {runs} requests each)")

    endpoints = [
        ("/identity", "trivial, no DB access — baseline"),
        ("/library/sections", "library list, light DB query"),
        ("/status/sessions", "active playback sessions"),
        ("/hubs", "HOME SCREEN — heaviest, this is what makes the UI feel slow"),
        ("/library/recentlyAdded?X-Plex-Container-Start=0&X-Plex-Container-Size=20",
         "recently added, paged"),
    ]

    results = {}
    print(f"  {'endpoint':<34}{'median':>10}{'min':>9}{'max':>9}{'first':>9}   verdict")
    for path, desc in endpoints:
        need_token = path != "/identity"
        if need_token and not token:
            continue
        times, status, size, err = time_direct(path, token, runs)
        name = path.split("?")[0]
        if err:
            print(f"  {name:<34}{C.R}{err[:40]}{C.OFF}")
            results[name] = {"error": err}
            continue
        s = stats(times)
        results[name] = {**s, "status": status, "bytes": size, "desc": desc}
        flag = ""
        if status and status >= 400:
            flag = f" {C.Y}HTTP {status}{C.OFF}"
        print(f"  {name:<34}{s['median_ms']:>9.1f}ms{s['min_ms']:>8.1f}ms"
              f"{s['max_ms']:>8.1f}ms{s['first_ms']:>8.1f}ms   {verdict(s['median_ms'])}{flag}")
        print(f"    {C.DIM}{desc}{C.OFF}")
    return results


def check_libraries(token, runs):
    """Time each library separately — finds the one library that's dragging things down."""
    if not token:
        return {}
    head("Per-library query times")
    raw = fetch("/library/sections", token)
    sections = re.findall(r'<Directory\b[^>]*\bkey="(\d+)"[^>]*\btitle="([^"]*)"', raw or "")
    if not sections:
        sections = [(k, t) for t, k in
                    re.findall(r'<Directory\b[^>]*\btitle="([^"]*)"[^>]*\bkey="(\d+)"', raw or "")]
    if not sections:
        print(f"  {C.DIM}could not list libraries{C.OFF}")
        return {}

    out = {}
    for key, title in sections:
        path = f"/library/sections/{key}/all?X-Plex-Container-Start=0&X-Plex-Container-Size=20"
        times, status, size, err = time_direct(path, token, max(3, runs // 3))
        cnt = fetch(f"/library/sections/{key}/all"
                    f"?X-Plex-Container-Start=0&X-Plex-Container-Size=0", token)
        m = re.search(r'totalSize="(\d+)"', cnt or "") or re.search(r'\bsize="(\d+)"', cnt or "")
        total = int(m.group(1)) if m else None
        if err:
            print(f"  {title[:28]:<30}{C.R}error{C.OFF}")
            continue
        s = stats(times)
        out[title] = {**s, "items": total}
        items = f"{total:,} items" if total is not None else ""
        print(f"  {title[:28]:<30}{s['median_ms']:>9.1f}ms   {verdict(s['median_ms'])}  {C.DIM}{items}{C.OFF}")
    return out


def detect_proxy():
    """
    Find the nginx vhost that proxies to Plex, so the proxy layer can be tested
    without the user having to pass --proxy. Returns a URL or None.
    """
    cfg = run("nginx -T 2>/dev/null")
    if not cfg:
        for p in ("/etc/nginx/nginx.conf", "/etc/nginx/conf.d"):
            path = Path(p)
            if path.is_dir():
                for f in path.glob("*.conf"):
                    try:
                        cfg += f.read_text(errors="ignore") + "\n"
                    except Exception:
                        pass
            elif path.is_file():
                try:
                    cfg += path.read_text(errors="ignore") + "\n"
                except Exception:
                    pass
    if not cfg:
        return None

    # upstream blocks pointing at Plex (configs usually use a named upstream)
    upstreams = set()
    for m in re.finditer(r"upstream\s+(\S+)\s*\{(.*?)\}", cfg, re.S):
        if f":{PLEX_PORT}" in m.group(2):
            upstreams.add(m.group(1))

    best = None
    for m in re.finditer(r"\bserver\s*\{", cfg):
        start = m.end()
        depth, i = 1, start
        while i < len(cfg) and depth:                     # naive brace matching
            if cfg[i] == "{":
                depth += 1
            elif cfg[i] == "}":
                depth -= 1
            i += 1
        block = cfg[start:i]

        targets = re.findall(r"proxy_pass\s+https?://([^;\s/]+)", block)
        if not [t for t in targets
                if f":{PLEX_PORT}" in t or t.split(":")[0] in upstreams]:
            continue

        names = re.findall(r"server_name\s+([^;]+);", block)
        listens = re.findall(r"listen\s+([^;]+);", block)
        host = None
        for n in names:
            for cand in n.split():
                if cand not in ("_", "localhost") and not cand.startswith("$"):
                    host = cand
                    break
            if host:
                break
        if not host:
            continue

        is_ssl = any("ssl" in l for l in listens)
        port = None
        for l in listens:
            pm = re.match(r"(?:[\d.]+:)?(\d+)", l.strip())
            if pm:
                port = int(pm.group(1))
                break
        scheme = "https" if is_ssl else "http"
        default_port = 443 if is_ssl else 80
        url = f"{scheme}://{host}" + (f":{port}" if port and port != default_port else "")
        if is_ssl or best is None:      # prefer the TLS vhost
            best = url
        if is_ssl:
            break
    return best


def proxy_probe(base, path, token, insecure=True):
    """One request through the proxy, returning (ms, status, bytes, headers)."""
    u = urllib.parse.urlparse(base)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    url = path + (("&" if "?" in path else "?") + "X-Plex-Token=" +
                  urllib.parse.quote(token) if token else "")
    try:
        if u.scheme == "https":
            ctx = ssl.create_default_context()
            if insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection(host, port, timeout=20, context=ctx)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=20)
        conn.request("GET", url, headers={"Accept-Encoding": "gzip, deflate"})
        resp = conn.getresponse()
        body = resp.read()
        hdrs = dict(resp.getheaders())
        conn.close()
        return resp.status, len(body), hdrs
    except Exception:
        return None, 0, {}


def check_proxy(base, token, runs):
    """
    Compare the two layers side by side:
      - "behind nginx"  = straight to Plex on 127.0.0.1:32400
      - "through nginx" = what a browser actually gets
    The difference is, by definition, what nginx costs you.
    """
    head(f"Layered comparison — Plex direct vs through the proxy")
    print(f"  {C.DIM}behind nginx  = http://{PLEX_HOST}:{PLEX_PORT} (Plex itself)")
    print(f"  through nginx = {base} (what your browser sees){C.OFF}")

    hs = handshake_breakdown(base)
    if hs["error"]:
        print(f"\n  {C.R}Cannot reach the proxy: {hs['error']}{C.OFF}")
        print(f"  {C.DIM}If Plex itself answered above, the proxy layer is the problem.{C.OFF}")
        return {"error": hs["error"]}

    print(f"\n  {C.BOLD}Connection setup{C.OFF} {C.DIM}(paid once per connection, NOT per request){C.OFF}")
    print(f"    TCP connect     : {hs['tcp_ms']:.1f} ms")
    if hs.get("tls_ms") is not None:
        print(f"    TLS handshake   : {hs['tls_ms']:.1f} ms  "
              f"{C.DIM}({hs.get('tls_version')}, {hs.get('cipher')}){C.OFF}")

    # same endpoints both ways
    paths = ["/identity", "/library/sections", "/hubs",
             "/library/recentlyAdded?X-Plex-Container-Start=0&X-Plex-Container-Size=20"]
    rows, out = [], {}
    print(f"\n  {C.BOLD}Per-request timings{C.OFF} "
          f"{C.DIM}(one reused connection each, median of {runs}){C.OFF}")
    print(f"    {'endpoint':<28}{'behind':>10}{'through':>10}{'nginx adds':>13}   size")
    for p in paths:
        if p != "/identity" and not token:
            continue
        dt, _, dsize, derr = time_direct(p, token, runs)
        pt, pstatus, perr = time_proxy(base, p, token, runs)
        if derr or perr:
            continue
        ds, ps = stats(dt), stats(pt)
        delta = ps["median_ms"] - ds["median_ms"]
        name = p.split("?")[0]
        col = C.G if delta < 3 else (C.Y if delta < 15 else C.R)
        print(f"    {name:<28}{ds['median_ms']:>8.1f}ms{ps['median_ms']:>8.1f}ms"
              f"{col}{delta:>+11.1f}ms{C.OFF}   {human_bytes(dsize)}")
        rows.append((name, ds["median_ms"], ps["median_ms"], delta, dsize))
        out[name] = {"direct_ms": ds["median_ms"], "proxy_ms": ps["median_ms"],
                     "overhead_ms": delta, "bytes": dsize}

    if not rows:
        return out

    # Is the proxy actually pointing at THIS Plex?
    direct_id = fetch("/identity", None)
    m1 = re.search(r'machineIdentifier="([^"]+)"', direct_id or "")
    status, _, hdrs = proxy_probe(base, "/identity", None)
    proxied_id = ""
    try:
        u = urllib.parse.urlparse(base)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        c = (http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=10, context=ctx)
             if u.scheme == "https" else
             http.client.HTTPConnection(u.hostname, u.port or 80, timeout=10))
        c.request("GET", "/identity")
        proxied_id = c.getresponse().read().decode("utf-8", "replace")
        c.close()
    except Exception:
        pass
    m2 = re.search(r'machineIdentifier="([^"]+)"', proxied_id or "")

    print(f"\n  {C.BOLD}Sanity checks{C.OFF}")
    if m1 and m2:
        same = m1.group(1) == m2.group(1)
        print(f"    backend identity: "
              f"{C.G + 'same Plex server' + C.OFF if same else C.R + 'DIFFERENT server behind the proxy!' + C.OFF}")
        out["same_backend"] = same
    srv = hdrs.get("Server") or hdrs.get("server") or "?"
    enc = hdrs.get("Content-Encoding") or hdrs.get("content-encoding") or "none"
    print(f"    proxy software  : {srv}")
    print(f"    compression     : {enc}")
    out["server"], out["encoding"] = srv, enc

    # interpretation: constant overhead vs size-dependent overhead
    small = min(rows, key=lambda r: r[4])
    large = max(rows, key=lambda r: r[4])
    avg = sum(r[3] for r in rows) / len(rows)
    out["avg_overhead_ms"] = avg
    out["overhead_ms"] = avg          # kept for the verdict

    print(f"\n  {C.BOLD}Reading this{C.OFF}")
    if avg < 0:
        print(f"    {C.G}nginx costs nothing measurable ({avg:+.1f} ms).{C.OFF}")
        if out.get("encoding", "none") != "none":
            print(f"    {C.DIM}It is actually faster than talking to Plex directly, because"
                  f"\n    {out['encoding']} compression makes the responses smaller over the wire.{C.OFF}")
        print(f"    {C.DIM}If the UI feels slow, the cause is behind nginx (Plex/database).{C.OFF}")
    elif avg < 3:
        print(f"    {C.G}nginx adds {avg:.1f} ms on average — it is NOT your bottleneck.{C.OFF}")
        print(f"    {C.DIM}If the UI feels slow, the cause is behind nginx (Plex/database).{C.OFF}")
    elif avg < 15:
        print(f"    {C.Y}nginx adds {avg:.1f} ms on average — noticeable but minor.{C.OFF}")
    else:
        print(f"    {C.R}nginx adds {avg:.1f} ms on average — the proxy IS a problem.{C.OFF}")
        print(f"    {C.DIM}Check upstream keepalive: 'Connection' must be set from a"
              f"\n    map $http_upgrade, not hardcoded to \"Upgrade\", and the upstream"
              f"\n    block needs a 'keepalive' directive.{C.OFF}")

    if large[4] > small[4] * 4 and large[3] > small[3] + 10:
        print(f"    {C.Y}Overhead grows with response size ({small[3]:+.1f} ms at "
              f"{human_bytes(small[4])} vs {large[3]:+.1f} ms at {human_bytes(large[4])}).{C.OFF}")
        print(f"    {C.DIM}That points at buffering or compression: check proxy_buffering"
              f"\n    and gzip settings for these locations.{C.OFF}")
    return out


def check_keepalive():
    head("nginx → Plex connection reuse (keepalive)")
    if not shutil.which("ss"):
        print(f"  {C.DIM}ss not available, skipping{C.OFF}")
        return {}
    est = run(f"ss -tn state established '( dport = :{PLEX_PORT} )' | wc -l")
    tw = run(f"ss -tn state time-wait '( dport = :{PLEX_PORT} )' | wc -l")
    try:
        est_n, tw_n = int(est or 0), int(tw or 0)
    except ValueError:
        return {}
    print(f"  established to :{PLEX_PORT} : {est_n}")
    print(f"  TIME-WAIT to :{PLEX_PORT}   : {tw_n}")
    if tw_n > 100:
        print(f"  {C.R}High TIME-WAIT count — connections are not being reused.{C.OFF}")
        print(f"  {C.DIM}In nginx: use 'map $http_upgrade $connection_upgrade' instead of"
              f" Connection \"Upgrade\", and keep 'keepalive' in the upstream block.{C.OFF}")
    else:
        print(f"  {C.G}Normal — no sign of connection churn.{C.OFF}")
    return {"established": est_n, "time_wait": tw_n}


def check_database():
    head("Database")
    out = {}
    if not LIB_DB.exists():
        print(f"  {C.DIM}library database not found at {LIB_DB}{C.OFF}")
        return out
    size = LIB_DB.stat().st_size
    wal = LIB_DB.with_name(LIB_DB.name + "-wal")
    wal_size = wal.stat().st_size if wal.exists() else 0
    out["db_bytes"], out["wal_bytes"] = size, wal_size
    print(f"  library.db        : {human_bytes(size)}")
    print(f"  WAL               : {human_bytes(wal_size)}")
    if wal_size > 200 * 1024**2:
        print(f"  {C.Y}Large WAL — checkpoints may not be running.{C.OFF}")

    # is the DB on an SSD?
    dev = run(f"df --output=source '{DB_DIR}' | tail -1")
    base = re.sub(r"p?\d+$", "", os.path.basename(dev)) if dev else ""
    rota = run(f"lsblk -dno ROTA /dev/{base}") if base else ""
    if rota:
        spinning = rota.strip().startswith("1")
        out["rotational"] = spinning
        if spinning:
            print(f"  storage           : {C.R}rotational disk ({dev}){C.OFF}")
            print(f"  {C.R}This alone can make the UI slow. Move the DB to an SSD/NVMe.{C.OFF}")
        else:
            print(f"  storage           : {C.G}SSD/NVMe ({dev}){C.OFF}")

    # stale backup copies Plex leaves behind
    backups = sorted(DB_DIR.glob("com.plexapp.plugins.library.db-20*"))
    if backups:
        total = sum(b.stat().st_size for b in backups)
        out["backup_bytes"] = total
        print(f"  old backup copies : {len(backups)} files, {human_bytes(total)}")
    out["backup_count"] = len(backups)

    # integrity + fragmentation, using Plex's own SQLite build (read-only queries)
    plexsql = "/usr/lib/plexmediaserver/Plex SQLite"
    if os.path.exists(plexsql):
        free = run(f'"{plexsql}" "{LIB_DB}" "PRAGMA freelist_count;"')
        page = run(f'"{plexsql}" "{LIB_DB}" "PRAGMA page_count;"')
        try:
            frag = int(free) / int(page) * 100 if int(page) else 0
            out["fragmentation_pct"] = frag
            colour = C.G if frag < 10 else (C.Y if frag < 25 else C.R)
            print(f"  free pages        : {colour}{frag:.1f}%{C.OFF} of the file")
            if frag >= 10:
                print(f"  {C.DIM}Consider: REINDEX; VACUUM; ANALYZE; (stop Plex first){C.OFF}")
        except (ValueError, ZeroDivisionError):
            pass
    return out


def classify_job(args):
    """
    Work out what a Plex Transcoder invocation is actually doing, from its
    command line. Returns (kind, label, media_path_or_None).
    """
    a = args
    media = None
    m = re.search(r"\s-i\s+(.+?)(?=\s+-[a-zA-Z])", a)   # filenames contain spaces
    if m:
        media = m.group(1).strip()

    if "/bif/" in a or "Indexes/tmp" in a or "img-%06d" in a:
        return "bif", "video preview thumbnails (BIF)", media
    if "thumb-%05d" in a or ("-f image2" in a and "-ss " in a):
        return "chapter", "chapter thumbnail", media
    if "ebur128" in a or "loudness" in a.lower():
        return "loudness", "loudness analysis", media
    if "-f null" in a or "showinfo" in a and "image2" not in a:
        return "analysis", "media analysis", media
    if "transcode/session" in a:
        return "playback", "playback transcode", media
    return "other", "transcoder", media


def fmt_secs(s):
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def check_processes(libraries_by_path=None):
    head("Plex background activity")
    raw = run("ps -eo etimes,pcpu,args --sort=-etimes")
    if not raw:
        print(f"  {C.DIM}cannot read process list{C.OFF}")
        return {}

    jobs, counts = [], {}
    for line in raw.splitlines():
        if "Plex Transcoder" not in line and "Plex Media Scanner" not in line:
            continue
        if "grep" in line:
            continue
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        etimes, pcpu, args = parts
        try:
            etimes, pcpu = int(etimes), float(pcpu)
        except ValueError:
            continue

        if "Plex Media Scanner" in args:
            kind, label, media = "scan", "library scan", None
        else:
            kind, label, media = classify_job(args)
        counts[kind] = counts.get(kind, 0) + 1
        jobs.append({"kind": kind, "label": label, "media": media,
                     "runtime_s": etimes, "cpu": pcpu})

    if not jobs:
        print(f"  {C.G}Idle{C.OFF} — no transcoding or scanning running right now.")
        return {"jobs": [], "counts": {}}

    explain = {
        "bif": "generating scrubbing previews for a recently added video",
        "chapter": "generating chapter thumbnails for a recently added video",
        "loudness": "analysing audio loudness of a recently added track",
        "analysis": "analysing media (codecs, duration) of a recently added file",
        "playback": "someone is watching — real playback transcode",
        "scan": "scanning a library for new files",
        "other": "transcoder task",
    }

    for j in jobs:
        colour = C.Y if j["kind"] == "playback" else C.B
        print(f"  {colour}{j['label']}{C.OFF}  "
              f"{C.DIM}running {fmt_secs(j['runtime_s'])}, {j['cpu']:.0f}% CPU{C.OFF}")
        print(f"    {C.DIM}{explain.get(j['kind'], '')}{C.OFF}")
        if j["media"]:
            p = Path(j["media"])
            lib = ""
            if libraries_by_path:
                for root, name in libraries_by_path.items():
                    if j["media"].startswith(root):
                        lib = f"  {C.DIM}[library: {name}]{C.OFF}"
                        break
            print(f"    file: {p.name}{lib}")
            print(f"    {C.DIM}{p.parent}{C.OFF}")

    total_cpu = sum(j["cpu"] for j in jobs)
    bg = sum(1 for j in jobs if j["kind"] in ("bif", "chapter", "loudness", "analysis", "scan"))
    print()
    print(f"  {len(jobs)} process(es), {total_cpu:.0f}% CPU total")
    if bg:
        print(f"  {C.DIM}{bg} background job(s) — these are triggered by your 'asap' settings"
              f"\n  whenever new media is added, and they compete for disk I/O.{C.OFF}")
    return {"jobs": jobs, "counts": counts}


def library_roots(token):
    """Map library folder -> library name, so running jobs can be attributed."""
    if not token:
        return {}
    raw = fetch("/library/sections", token)
    roots = {}
    for sec in re.findall(r"<Directory\b.*?</Directory>", raw or "", re.S):
        t = re.search(r'\btitle="([^"]*)"', sec)
        if not t:
            continue
        for loc in re.findall(r'<Location\b[^>]*\bpath="([^"]*)"', sec):
            roots[loc] = t.group(1)
    return roots


def check_settings():
    head("Background task settings")
    try:
        prefs = PREFS.read_text(errors="ignore")
    except Exception as e:
        print(f"  {C.DIM}cannot read Preferences.xml ({e}){C.OFF}")
        return {}

    def pref(name, default=None):
        m = re.search(rf'{name}="([^"]*)"', prefs)
        return m.group(1) if m else default

    # These are choices, not faults: 'asap' trades background CPU/IO for having
    # previews ready immediately after new media is added.
    items = [
        ("GenerateBIFBehavior", "video preview thumbnails"),
        ("GenerateChapterThumbBehavior", "chapter thumbnails"),
        ("LoudnessAnalysisBehavior", "loudness analysis"),
        ("ScheduledLibraryUpdatesEnabled", "scheduled library updates"),
        ("ButlerTaskDeepMediaAnalysis", "deep media analysis"),
    ]
    out = {}
    for key, label in items:
        val = pref(key)
        if val is None:
            continue
        out[key] = val
        if val.lower() == "asap":
            print(f"  {label:<28}: {C.B}{val}{C.OFF} "
                  f"{C.DIM}(runs as soon as media is added, by design){C.OFF}")
        else:
            print(f"  {label:<28}: {val}")

    start, end = pref("ButlerStartHour"), pref("ButlerEndHour")
    if start and end:
        print(f"  maintenance window          : {start}:00–{end}:00")
        out["butler_window"] = f"{start}-{end}"
    return out


def summarise(endpoints, proxy, db, processes):
    head("Verdict")
    ident = endpoints.get("/identity", {}).get("median_ms")
    hubs = endpoints.get("/hubs", {}).get("median_ms")
    problems, notes = [], []

    counts = (processes or {}).get("counts", {})
    bg = sum(counts.get(k, 0) for k in ("bif", "chapter", "loudness", "analysis", "scan"))
    under_load = bg > 0
    if under_load:
        notes.append(
            f"{bg} background job(s) were running during this test (preview/thumbnail\n"
            "     generation for newly added media), so these numbers are a worst case.\n"
            "     Re-run when idle for a clean baseline.")

    if ident is not None and hubs is not None:
        print(f"  /identity {ident:.1f} ms  vs  /hubs {hubs:.1f} ms")
        if hubs > SLOW:
            problems.append(
                "The home-screen query (/hubs) is slow while the trivial endpoint is fast.\n"
                "     This points at the DATABASE, not the network or the proxy.\n"
                "     Fix: stop Plex, then run  REINDEX; VACUUM; ANALYZE;  on library.db\n"
                "     (ANALYZE matters most — stale statistics cause bad query plans).")
        elif hubs > GOOD and not under_load:
            problems.append(
                "The home-screen query is a bit sluggish. A REINDEX/VACUUM/ANALYZE pass\n"
                "     on library.db will usually bring it back under 100 ms.")

    slow_eps = [k for k, v in endpoints.items()
                if isinstance(v, dict) and (v.get("median_ms") or 0) > SLOW and k != "/hubs"]
    if slow_eps and not under_load:
        problems.append(f"Slow endpoint(s): {', '.join(slow_eps)}.")

    if proxy and proxy.get("avg_overhead_ms") is not None:
        ov = proxy["avg_overhead_ms"]
        if ov > 15:
            problems.append(
                f"LAYER: the proxy. nginx adds {ov:.1f} ms per request on average.\n"
                "     Check upstream keepalive (map $http_upgrade, plus 'keepalive' in the\n"
                "     upstream block) — without it every request opens a new TCP connection.")
        else:
            desc = (f"costs nothing measurable ({ov:+.1f} ms)" if ov < 0
                    else f"adds only {ov:.1f} ms per request")
            notes.append(
                f"LAYER: nginx {desc} — the proxy is clean.\n"
                "     Anything slow above is therefore Plex-side (database or background load).")
    elif proxy and proxy.get("error"):
        problems.append(
            "LAYER: the proxy could not be reached at all, while Plex itself answered.\n"
            "     The fault is in nginx/TLS/DNS, not in Plex.")

    if proxy.get("same_backend") is False:
        problems.append(
            "The proxy is serving a DIFFERENT Plex server than the one measured directly.\n"
            "     Your proxy_pass target is wrong.")

    if db.get("rotational"):
        problems.append(
            "The Plex database lives on a rotational disk. Moving it to an SSD/NVMe is the\n"
            "     single biggest improvement available.")

    if db.get("fragmentation_pct", 0) >= 25:
        problems.append(
            f"The database is {db['fragmentation_pct']:.0f}% free pages — a VACUUM will shrink it\n"
            "     and speed up queries.")

    if db.get("backup_bytes", 0) > 500 * 1024**2:
        notes.append(
            f"Plex's own database backups take {human_bytes(db['backup_bytes'])}. Safe to prune:\n"
            "     find <Databases> -name '*.db-20??-??-??' -mtime +7 -delete")

    if not problems:
        print(f"\n  {C.G}Nothing obviously wrong — response times look healthy.{C.OFF}")
    else:
        for i, p in enumerate(problems, 1):
            print(f"\n  {C.Y}{i}.{C.OFF} {p}")
    for n in notes:
        print(f"\n  {C.DIM}Note:{C.OFF} {n}")
    print()


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description="Diagnose Plex server responsiveness (read-only).")
    ap.add_argument("--proxy", metavar="URL",
                    help="reverse proxy URL to compare against (overrides the "
                         "PROXY_URL constant; falls back to nginx auto-detection)")
    ap.add_argument("--no-proxy", action="store_true",
                    help="skip the proxy layer entirely (test Plex only)")
    ap.add_argument("--token", help="Plex token (default: read from Preferences.xml)")
    ap.add_argument("--runs", type=int, default=10,
                    help="requests per endpoint (default: 10)")
    ap.add_argument("--json", metavar="FILE", help="also write results as JSON")
    ap.add_argument("--no-libraries", action="store_true",
                    help="skip the per-library timings (they can be slow)")
    ap.add_argument("--print-token", action="store_true",
                    help="print an 'export TOKEN=...' line for use in your own commands")
    args = ap.parse_args()

    token, source = find_token(args.token)

    if args.print_token:
        if token:
            print(f"export TOKEN={token}")
            sys.exit(0)
        print("no token found (run as root so Preferences.xml is readable)", file=sys.stderr)
        sys.exit(1)

    print(f"{C.BOLD}Plex responsiveness diagnostics{C.OFF}  "
          f"{C.DIM}{time.strftime('%Y-%m-%d %H:%M:%S')}{C.OFF}")

    if not check_service():
        print(f"\n{C.R}Plex is not answering on {PLEX_HOST}:{PLEX_PORT} — nothing else to measure.{C.OFF}")
        sys.exit(1)

    if token:
        print(f"  token             : {C.G}auto-detected{C.OFF} from {source} "
              f"{C.DIM}({mask(token)}){C.OFF}")
    else:
        print(f"  token             : {C.Y}not found{C.OFF}"
              f"{f' — {source}' if source else ''}")
        print(f"  {C.DIM}Only /identity can be measured. Run as root, or pass --token.{C.OFF}")

    report = {"generated": int(time.time())}

    # Which proxy to test: --proxy > PROXY_URL constant > auto-detection
    proxy_url, proxy_src = None, None
    if args.no_proxy:
        pass
    elif args.proxy:
        proxy_url, proxy_src = args.proxy, "--proxy argument"
    elif PROXY_URL.strip():
        proxy_url, proxy_src = PROXY_URL.strip(), "PROXY_URL in this script"
    else:
        proxy_url = detect_proxy()
        proxy_src = "auto-detected from nginx config" if proxy_url else None

    if proxy_url:
        print(f"  proxy             : {C.G}{proxy_url}{C.OFF} {C.DIM}({proxy_src}){C.OFF}")
    elif args.no_proxy:
        print(f"  proxy             : {C.DIM}skipped (--no-proxy){C.OFF}")
    else:
        print(f"  proxy             : {C.Y}not detected{C.OFF} "
              f"{C.DIM}— testing Plex only; set PROXY_URL or pass --proxy{C.OFF}")

    report["endpoints"] = check_endpoints(token, args.runs)
    if not args.no_libraries:
        report["libraries"] = check_libraries(token, args.runs)
    report["proxy"] = check_proxy(proxy_url, token, args.runs) if proxy_url else {}
    report["keepalive"] = check_keepalive()
    report["database"] = check_database()
    report["settings"] = check_settings()
    report["processes"] = check_processes(library_roots(token))

    summarise(report["endpoints"], report["proxy"], report["database"], report["processes"])

    if args.json:
        try:
            Path(args.json).write_text(json.dumps(report, indent=2))
            print(f"{C.DIM}JSON written to {args.json}{C.OFF}")
        except Exception as e:
            print(f"{C.R}could not write JSON: {e}{C.OFF}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\naborted")
        sys.exit(130)
