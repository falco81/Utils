#!/usr/bin/env python3
"""
hist-check.py — proč v grafech chybí data.

Přečte historii démona a ukáže, jak se v čase mění klíče a zaplněnost
jednotlivých řad. Nic nezapisuje, disků se nedotýká.

    python3 hist-check.py
    python3 hist-check.py /var/lib/plex-status/history.json
"""
import json
import sys
import time
from collections import Counter
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1
            else "/var/lib/plex-status/history.json")
if not path.is_file():
    sys.exit(f"nenalezeno: {path}")

data = json.loads(path.read_text())
if not isinstance(data, list):
    sys.exit(f"historie není seznam, ale {type(data).__name__} — starý formát?")

samples = [s for s in data if isinstance(s, dict) and "t" in s]
print(f"soubor:  {path}")
print(f"vzorků:  {len(samples)}  (v souboru celkem {len(data)})")
if not samples:
    sys.exit("žádné použitelné vzorky")

t0, t1 = samples[0]["t"], samples[-1]["t"]
fmt = lambda t: time.strftime("%d.%m %H:%M", time.localtime(t))
print(f"rozsah:  {fmt(t0)} → {fmt(t1)}  ({(t1 - t0) / 3600:.1f} h)")

# ---- klíče disků v čase -----------------------------------------------
print("\n=== klíče disků, pod kterými jsou vzorky uložené ===")
print("(když se klíč v čase změní, graf staré vzorky nenajde)")
runs = []
for s in samples:
    keys = tuple(sorted((s.get("d") or {}).keys()))
    if not runs or runs[-1][0] != keys:
        runs.append([keys, s["t"], s["t"], 1])
    else:
        runs[-1][2] = s["t"]
        runs[-1][3] += 1
for keys, a, b, n in runs:
    shown = ", ".join(keys) if keys else "(žádné disky!)"
    print(f"  {fmt(a)} → {fmt(b)}  {n:4} vzorků")
    print(f"      {shown[:110]}")

# ---- zaplněnost řad ----------------------------------------------------
print("\n=== kolik vzorků má skutečnou hodnotu (ne prázdno) ===")
half = len(samples) // 2
for label, rng in (("starší polovina", samples[:half]),
                   ("novější polovina", samples[half:])):
    if not rng:
        continue
    perf_ok = sum(1 for s in rng if (s.get("p") or [None])[0] is not None)
    sys_ok = sum(1 for s in rng if (s.get("s") or [None])[0] is not None)
    temp_ok = sum(1 for s in rng
                  for row in (s.get("d") or {}).values() if row and row[0] is not None)
    ndisk = sum(len(s.get("d") or {}) for s in rng)
    print(f"  {label:17} ({len(rng)} vzorků, {fmt(rng[0]['t'])} → {fmt(rng[-1]['t'])})")
    print(f"      odezva API   {perf_ok:4}/{len(rng)}")
    print(f"      systém       {sys_ok:4}/{len(rng)}")
    print(f"      teploty disků{temp_ok:4}/{ndisk}")

# ---- rozestupy ---------------------------------------------------------
gaps = [samples[i + 1]["t"] - samples[i]["t"] for i in range(len(samples) - 1)]
if gaps:
    big = [(fmt(samples[i]["t"]), g) for i, g in enumerate(gaps) if g > 1800]
    print(f"\n=== rozestupy vzorků ===")
    print(f"  nejčastější: {Counter(gaps).most_common(3)}")
    if big:
        print("  mezery delší než 30 min:")
        for when, g in big[:10]:
            print(f"      od {when}: {g / 60:.0f} min")

# ---- co vidí web ------------------------------------------------------
web = Path("/var/www/html/smart/history-full.json")
if web.is_file():
    age = time.time() - web.stat().st_mtime
    print(f"\n=== soubor pro web ===")
    print(f"  {web}")
    print(f"  poslední zápis před {age / 60:.0f} min"
          + ("   <-- ZASTARALÝ, démon ho nepřepisuje" if age > 900 else ""))
    try:
        w = json.loads(web.read_text())
        print(f"  bodů: {len(w.get('t', []))}, disků: {list((w.get('disks') or {}).keys())}")
    except Exception as e:
        print(f"  nečitelný: {e}")

print("\nPošli celý tento výstup zpět.")
