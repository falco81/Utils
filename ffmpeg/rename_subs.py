#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Přejmenování titulků podle názvů videí.

Najde v adresáři všechny video soubory (.mkv, .mp4) a titulky (.srt),
spáruje je podle epizodního kódu SxxExx a přejmenuje každý titulek tak,
aby měl stejný základ názvu jako odpovídající video plus původní jazykovou
koncovku, tedy ve tvaru:  <nazev_videa>.<jazyk>.srt
(např. ...AppleTor.en.srt).

Použití:
    python rename_subs.py                  # aktuální adresář, jen NÁHLED
    python rename_subs.py "D:/serial"      # jiný adresář
    python rename_subs.py --apply          # skutečně přejmenuje
    python rename_subs.py -r --apply       # projde i podadresáře
"""

import argparse
import os
import re

VIDEO_EXTS = {".mkv", ".mp4"}
SUB_EXT = ".srt"

# Tokeny, které se na konci názvu titulku berou jako jazyk / příznak
FLAG_TOKENS = {"forced", "sdh", "cc", "hi", "foreign", "full", "default"}
EPISODE_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,2})")
LANG_RE = re.compile(r"^[A-Za-z]{2,3}$")  # en, cs, eng, ces ...


def episode_key(name):
    """Vrátí (sezona, epizoda) z prvního výskytu SxxExx, jinak None."""
    m = EPISODE_RE.search(name)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def fmt_key(k):
    return f"S{k[0]:02d}E{k[1]:02d}"


def is_lang_or_flag(token):
    return token.lower() in FLAG_TOKENS or bool(LANG_RE.match(token))


def split_suffix(stem):
    """Z názvu titulku (bez .srt) vytáhne koncové jazykové/flag tokeny.

    Vrací seznam tokenů, např. ['en'] nebo ['en', 'forced'].
    Bere max 2 koncové tokeny (jazyk + případně jeden příznak),
    aby se omylem nesnědlo slovo z názvu seriálu.
    """
    parts = stem.split(".")
    suffix = []
    while parts and len(suffix) < 2 and is_lang_or_flag(parts[-1]):
        suffix.insert(0, parts.pop())
    return suffix


def collect(directory, recursive):
    videos, subs = [], []
    if recursive:
        walker = os.walk(directory)
    else:
        walker = [(directory, [], os.listdir(directory))]
    for root, _dirs, files in walker:
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            full = os.path.join(root, f)
            if ext in VIDEO_EXTS:
                videos.append(full)
            elif ext == SUB_EXT:
                subs.append(full)
    return videos, subs


def build_plan(videos, subs):
    # Mapa epizoda -> video
    vmap = {}
    for v in sorted(videos):
        k = episode_key(os.path.basename(v))
        if k is None:
            continue
        if k in vmap:
            print(f"!  Více videí pro {fmt_key(k)}, používám: "
                  f"{os.path.basename(vmap[k])}")
            continue
        vmap[k] = v

    planned = []
    used_targets = {}
    for s in sorted(subs):
        sdir = os.path.dirname(s)
        sname = os.path.basename(s)
        k = episode_key(sname)
        if k is None:
            print(f"-  Přeskakuji (chybí SxxExx): {sname}")
            continue
        v = vmap.get(k)
        if v is None:
            print(f"-  Bez videa pro {fmt_key(k)}: {sname}")
            continue

        stem = sname[: -len(SUB_EXT)]            # název bez .srt
        suffix = split_suffix(stem)               # ['en'] apod.
        vstem = os.path.splitext(os.path.basename(v))[0]
        new_name = vstem + ("." + ".".join(suffix) if suffix else "") + SUB_EXT
        dst = os.path.join(sdir, new_name)

        if os.path.abspath(dst) == os.path.abspath(s):
            print(f"=  Už správně pojmenováno: {sname}")
            continue
        if dst in used_targets:
            print(f"!  Kolize cíle ({new_name}) – přeskakuji: {sname}")
            continue
        if os.path.exists(dst):
            print(f"!  Cíl už existuje, přeskakuji: {new_name}")
            continue

        used_targets[dst] = s
        planned.append((s, dst))
    return planned


def main():
    p = argparse.ArgumentParser(
        description="Přejmenuje titulky podle videí (párování přes SxxExx).")
    p.add_argument("directory", nargs="?", default=".",
                   help="Adresář (výchozí: aktuální).")
    p.add_argument("-r", "--recursive", action="store_true",
                   help="Projít i podadresáře.")
    p.add_argument("--apply", action="store_true",
                   help="Skutečně přejmenovat (bez něj jen náhled).")
    args = p.parse_args()

    if not os.path.isdir(args.directory):
        raise SystemExit(f"Není adresář: {args.directory}")

    videos, subs = collect(args.directory, args.recursive)
    print(f"Nalezeno videí: {len(videos)}, titulků: {len(subs)}\n")

    planned = build_plan(videos, subs)

    if not planned:
        print("\nNic k přejmenování.")
        return

    print(f"\nPlán přejmenování ({len(planned)}):")
    for s, d in planned:
        print(f"  {os.path.basename(s)}")
        print(f"   -> {os.path.basename(d)}")

    if not args.apply:
        print("\n[NÁHLED] Nic se nezměnilo. Spusť znovu s --apply pro provedení.")
        return

    print()
    for s, d in planned:
        os.rename(s, d)
        print(f"OK: {os.path.basename(d)}")
    print(f"\nHotovo, přejmenováno {len(planned)} titulků.")


if __name__ == "__main__":
    main()
