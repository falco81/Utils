#!/usr/bin/env python3
"""Najde duplicitní soubory v zadaných cestách.

Strategie (kvůli rychlosti, od nejlevnějšího k nejdražšímu):
  1. Seskupí soubory podle velikosti – unikátní velikost = nemůže být duplikát.
  2. U stejně velkých spočítá hash jen z prvních pár KB (rychlé prosítání).
  3. Plný hash spočítá jen u těch, co prošly i druhým krokem.

Použití:
    python3 find_duplicates.py CESTA [CESTA ...]
    python3 find_duplicates.py /home/user/foto /mnt/data
    python3 find_duplicates.py ~/Downloads --clean      # interaktivní mazání
"""

import argparse
import hashlib
import os
import sys
from collections import defaultdict

# Velikost úvodního bloku pro rychlý "partial" hash.
PARTIAL_SIZE = 4096
# Velikost čteného bufferu při plném hashi.
CHUNK_SIZE = 1 << 20  # 1 MiB


def hash_file(path, partial=False):
    """Vrátí blake2b hash souboru. Při partial=True jen z prvních PARTIAL_SIZE bajtů.

    blake2b je ze stdlib a je výrazně rychlejší než md5/sha256.
    Vrací None, pokud soubor nejde přečíst.
    """
    h = hashlib.blake2b()
    try:
        with open(path, "rb", buffering=0) as f:
            if partial:
                h.update(f.read(PARTIAL_SIZE))
            else:
                for block in iter(lambda: f.read(CHUNK_SIZE), b""):
                    h.update(block)
    except OSError as e:
        print(f"VAROVÁNÍ: nelze přečíst {path}: {e}", file=sys.stderr)
        return None
    return h.digest()


def iter_files(paths, follow_symlinks=False):
    """Projde všechny zadané cesty a vrací dvojice (cesta, velikost)."""
    seen_inodes = set()
    for root in paths:
        if os.path.isfile(root):
            try:
                st = os.stat(root)
            except OSError as e:
                print(f"VAROVÁNÍ: nelze získat info o {root}: {e}", file=sys.stderr)
                continue
            yield root, st.st_size
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
            for name in filenames:
                full = os.path.join(dirpath, name)
                # Přeskočit symlinky (ať nepočítáme tentýž soubor dvakrát).
                if not follow_symlinks and os.path.islink(full):
                    continue
                try:
                    st = os.stat(full)
                except OSError as e:
                    print(f"VAROVÁNÍ: nelze získat info o {full}: {e}", file=sys.stderr)
                    continue
                # Deduplikace přes inode (tvrdé linky = stejný soubor).
                key = (st.st_dev, st.st_ino)
                if key in seen_inodes:
                    continue
                seen_inodes.add(key)
                yield full, st.st_size


def group_by_size(files):
    by_size = defaultdict(list)
    for path, size in files:
        by_size[size].append(path)
    return {size: paths for size, paths in by_size.items() if len(paths) > 1}


def refine(groups, partial, keep_digest=False):
    """Rozdělí každou skupinu podle (partial / plného) hashe.

    Vrací seznam skupin. Pokud keep_digest=True, vrací dvojice (digest, cesty).
    """
    refined = []
    for paths in groups:
        by_hash = defaultdict(list)
        for path in paths:
            digest = hash_file(path, partial=partial)
            if digest is not None:
                by_hash[digest].append(path)
        for digest, same in by_hash.items():
            if len(same) > 1:
                refined.append((digest, same) if keep_digest else same)
    return refined


def find_duplicates(paths, follow_symlinks=False):
    """Vrátí seznam dvojic (hex_otisk, [cesty]) pro skupiny duplikátů."""
    size_groups = group_by_size(iter_files(paths, follow_symlinks))

    # Prázdné soubory řešíme zvlášť – všechny jsou shodné, nemá smysl je hashovat.
    empty_files = size_groups.pop(0, [])

    # 2. partial hash
    candidates = refine(list(size_groups.values()), partial=True)
    # 3. plný hash (s ponecháním otisku)
    duplicates = refine(candidates, partial=False, keep_digest=True)

    result = [(digest.hex(), sorted(paths)) for digest, paths in duplicates]

    if len(empty_files) > 1:
        empty_digest = hashlib.blake2b().hexdigest()
        result.append((empty_digest, sorted(empty_files)))

    # Seřadit podle velikosti souboru sestupně (největší žrouti místa nahoře).
    result.sort(key=lambda g: -os.path.getsize(g[1][0]))
    return result


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def clean_group(index, paths):
    """Interaktivně se zeptá, které soubory ze skupiny smazat, a smaže je."""
    print(f"\nSkupina {index}:")
    for i, path in enumerate(paths, 1):
        print(f"  [{i}] {path}")

    while True:
        raw = input(
            "  Smazat která čísla? (oddělená čárkou, Enter = přeskočit): "
        ).strip()
        if not raw:
            print("  Přeskočeno.")
            return 0

        try:
            choices = sorted({int(x) for x in raw.replace(" ", "").split(",") if x})
        except ValueError:
            print("  Neplatný vstup, zadej čísla oddělená čárkou.")
            continue

        if any(c < 1 or c > len(paths) for c in choices):
            print(f"  Čísla musí být v rozsahu 1–{len(paths)}.")
            continue

        if len(choices) == len(paths):
            print("  Nelze smazat všechny – aspoň jedna kopie musí zůstat.")
            continue

        to_delete = [paths[c - 1] for c in choices]
        print("  Ke smazání:")
        for p in to_delete:
            print(f"    {p}")
        confirm = input("  Opravdu smazat? [a/N]: ").strip().lower()
        if confirm not in ("a", "ano", "y", "yes"):
            print("  Zrušeno.")
            return 0

        deleted = 0
        for p in to_delete:
            try:
                os.remove(p)
                print(f"  Smazáno: {p}")
                deleted += 1
            except OSError as e:
                print(f"  CHYBA při mazání {p}: {e}", file=sys.stderr)
        return deleted


def main():
    parser = argparse.ArgumentParser(
        description="Najde duplicitní soubory v zadaných cestách (podle obsahu)."
    )
    parser.add_argument("paths", nargs="+", help="Cesty k prohledání (soubory nebo adresáře)")
    parser.add_argument(
        "-L", "--follow-symlinks", action="store_true",
        help="Následovat symbolické odkazy (výchozí: ne)",
    )
    parser.add_argument(
        "-s", "--summary", action="store_true",
        help="Na konci vypsat shrnutí kolik místa zaberou duplikáty",
    )
    parser.add_argument(
        "--clean", action="store_true",
        help="Po výpisu se u každé skupiny zeptat, které soubory smazat",
    )
    args = parser.parse_args()

    for p in args.paths:
        if not os.path.exists(p):
            print(f"CHYBA: cesta neexistuje: {p}", file=sys.stderr)
            sys.exit(1)

    groups = find_duplicates(args.paths, args.follow_symlinks)

    if not groups:
        print("Žádné duplicity nenalezeny.")
        return

    wasted = 0
    for i, (digest, group) in enumerate(groups, 1):
        size = os.path.getsize(group[0])
        wasted += size * (len(group) - 1)
        print(f"\n# Skupina {i} – {len(group)} souborů, {human_size(size)} každý")
        print(f"  otisk: {digest}")
        for path in group:
            print(f"  {path}")

    if args.summary:
        total_dupes = sum(len(g[1]) - 1 for g in groups)
        print(f"\nCelkem {len(groups)} skupin, {total_dupes} nadbytečných kopií, "
              f"zabírají {human_size(wasted)}.")

    if args.clean:
        print("\n=== Mazání duplikátů ===")
        deleted = 0
        for i, (digest, group) in enumerate(groups, 1):
            deleted += clean_group(i, group)
        print(f"\nHotovo. Smazáno {deleted} souborů.")


if __name__ == "__main__":
    main()
