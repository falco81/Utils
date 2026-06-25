#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Inteligentní přejmenovávač souborů.

Najde v adresáři soubory odpovídající zadanému vzoru (glob, např. *.mkv nebo ??Test*.mp3)
a navrhne sjednocení jejich názvů:

  1) Zarovná čísla nulami podle nejdelšího čísla na stejné pozici
     (1 2 11 12  ->  01 02 11 12     |     1 2 114 115  ->  001 002 114 115)
  2) Doplní chybějící společná slova podle většinového vzoru
     ("Would You Marry Me 7.mp4"  ->  "Would You Marry Me 07 Reaction.mp4")

Defaultně pouze zobrazí náhled. Skutečné přejmenování provede přepínač --apply.

Použití:
    python intelligent_file_renamer.py "*.mp4"
    python intelligent_file_renamer.py "*.mp4" --apply
    python intelligent_file_renamer.py "??Test*.mp3" --dir /cesta/k/souborum --apply
"""

import argparse
import fnmatch
import os
import re
import sys
import uuid
from collections import Counter, defaultdict

NUM_RE = re.compile(r"\d+")
ARROW = "\u2192"  # → (přepíná se na "->" přepínačem --ascii)


# --------------------------------------------------------------------------- #
#  Nastavení konzole (UTF-8 + ANSI barvy, hlavně kvůli Windows 10)
# --------------------------------------------------------------------------- #
def _enable_win_ansi():
    """Zapne zpracování ANSI sekvencí v konzoli Windows přes WinAPI (Win10+)."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        ok = True
        for handle_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            h = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
                # 0x0004 = ENABLE_VIRTUAL_TERMINAL_PROCESSING
                kernel32.SetConsoleMode(h, mode.value | 0x0004)
            else:
                ok = False
        return ok
    except Exception:
        return False


def setup_console():
    """
    Zajistí korektní výstup na všech platformách:
      - přepne stdout/stderr na UTF-8 (česká písmena, šipka →),
      - povolí ANSI barvy (přednostně přes colorama, jinak nativní WinAPI).
    Vrací True, pokud lze používat barvy.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except Exception:
            pass

    # Přednostně colorama – nejrobustnější i pro starší Windows konzole.
    try:
        import colorama
        try:
            colorama.just_fix_windows_console()  # colorama >= 0.4.6
        except AttributeError:
            colorama.init()                      # starší colorama
        return True
    except ImportError:
        pass

    if os.name == "nt":
        return _enable_win_ansi()
    return True


def want_color(arg_color, ansi_ok):
    """Rozhodne, zda obarvovat výstup (auto/always/never + proměnná NO_COLOR)."""
    if arg_color == "never" or os.environ.get("NO_COLOR") is not None:
        return False
    if arg_color == "always":
        return True
    return ansi_ok and sys.stdout.isatty()  # auto


# --------------------------------------------------------------------------- #
#  Pomocné funkce pro tokenizaci a klíče
# --------------------------------------------------------------------------- #
def split_fields(stem, sep):
    """Rozdělí název (bez přípony) na pole. sep=None znamená dělení podle bílých znaků."""
    if sep is None:
        return stem.split()
    return [p for p in stem.split(sep) if p != ""]


def key_of(field):
    """Normalizovaný klíč pole pro porovnávání: malá písmena, čísla nahrazena '#'."""
    return NUM_RE.sub("#", field.lower())


def has_letters(key):
    return any(c.isalpha() for c in key)


def is_pure_word_key(key):
    """Pole je čisté slovo (žádná číslice) -> lze ho bezpečně doplnit."""
    return "#" not in key and has_letters(key)


# --------------------------------------------------------------------------- #
#  LCS zarovnání dvou sekvencí klíčů
# --------------------------------------------------------------------------- #
def lcs_align(ref, other):
    """
    Vrátí seznam operací popisujících zarovnání 'ref' a 'other'.
      ('match', ri, fi) – shoda
      ('ref',  ri)      – pole je jen v referenci (chybí v souboru)
      ('file', fi)      – pole je jen v souboru (navíc)
    """
    n, m = len(ref), len(other)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            if ref[i] == other[j]:
                dp[i][j] = dp[i + 1][j + 1] + 1
            else:
                dp[i][j] = max(dp[i + 1][j], dp[i][j + 1])

    ops, i, j = [], 0, 0
    while i < n and j < m:
        if ref[i] == other[j]:
            ops.append(("match", i, j)); i += 1; j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            ops.append(("ref", i)); i += 1
        else:
            ops.append(("file", j)); j += 1
    while i < n:
        ops.append(("ref", i)); i += 1
    while j < m:
        ops.append(("file", j)); j += 1
    return ops


# --------------------------------------------------------------------------- #
#  Výpočet šířek pro zarovnání čísel
# --------------------------------------------------------------------------- #
def compute_widths(stems, min_width):
    """
    Pro každou pozici čísla (pořadí čísla v názvu, zleva doprava) spočítá
    cílovou šířku = max(počet číslic největší hodnoty, nejdelší existující zápis).
    """
    vals = defaultdict(list)
    rawlen = defaultdict(list)
    for stem in stems:
        for idx, run in enumerate(NUM_RE.findall(stem)):
            vals[idx].append(int(run))
            rawlen[idx].append(len(run))
    widths = {}
    for idx in vals:
        w = max(len(str(max(vals[idx]))), max(rawlen[idx]))
        if min_width:
            w = max(w, min_width)
        widths[idx] = w
    return widths


def pad_field(field, counter, widths):
    """Doplní nuly všem číslům v poli podle 'widths'; counter[0] počítá pořadí čísla."""
    def repl(m):
        idx = counter[0]
        counter[0] += 1
        w = widths.get(idx, len(m.group()))
        return m.group().zfill(w)
    return NUM_RE.sub(repl, field)


# --------------------------------------------------------------------------- #
#  Hlavní logika
# --------------------------------------------------------------------------- #
def build_plan(filenames, sep, do_pad, do_words, min_width):
    """Vrátí list dvojic (old_name, new_name)."""
    items = []
    for name in filenames:
        stem, ext = os.path.splitext(name)
        items.append({"name": name, "stem": stem, "ext": ext,
                      "fields": split_fields(stem, sep)})

    # šířky čísel
    widths = compute_widths([it["stem"] for it in items], min_width) if do_pad else {}

    # referenční vzor = nejčastější struktura polí (při shodě delší vzor)
    patterns = [tuple(key_of(f) for f in it["fields"]) for it in items]
    for it, pat in zip(items, patterns):
        it["keys"] = list(pat)
    counts = Counter(patterns)
    best = max(counts.keys(), key=lambda p: (counts[p], len(p)))
    ref_keys = list(best)
    # exemplář pro získání původního tvaru (velikost písmen) doplňovaných slov
    exemplar = next(it["fields"] for it, pat in zip(items, patterns) if pat == best)

    plan = []
    for it in items:
        ops = lcs_align(ref_keys, it["keys"])
        # soubor doplníme slovy jen pokud jeho slova tvoří podmnožinu reference
        # (žádné vlastní slovo navíc -> nehrozí kontaminace z jiné série)
        file_word_only = any(op[0] == "file" and has_letters(it["keys"][op[1]]) for op in ops)
        do_insert = do_words and not file_word_only

        out = []
        counter = [0]
        for op in ops:
            if op[0] == "match":
                out.append(pad_field(it["fields"][op[2]], counter, widths))
            elif op[0] == "file":
                out.append(pad_field(it["fields"][op[1]], counter, widths))
            else:  # 'ref' – chybí v souboru
                if do_insert and is_pure_word_key(ref_keys[op[1]]):
                    out.append(exemplar[op[1]])
                # čísla ani smíšená pole nedoplňujeme (neznáme hodnotu)

        join = " " if sep is None else sep
        new_stem = join.join(out)
        plan.append((it["name"], new_stem + it["ext"]))
    return plan


def detect_collisions(plan, existing):
    """Vrátí množinu starých názvů, které kvůli konfliktu cíle přeskočíme."""
    changing = {old: new for old, new in plan if old != new}
    targets = Counter(changing.values())
    sources = set(changing.keys())
    skip = set()
    for old, new in changing.items():
        if targets[new] > 1:                       # dva soubory na stejný cíl
            skip.add(old)
        elif new in existing and new not in sources:  # cíl už existuje a nepřejmenovává se
            skip.add(old)
    return skip


# --------------------------------------------------------------------------- #
#  Výpis a aplikace
# --------------------------------------------------------------------------- #
def color(s, c, enabled):
    codes = {"green": "32", "yellow": "33", "red": "31", "dim": "2", "cyan": "36"}
    return f"\033[{codes[c]}m{s}\033[0m" if enabled else s


def print_preview(plan, skip, show_all, use_color):
    changed = [(o, n) for o, n in plan if o != n and o not in skip]
    skipped = [(o, n) for o, n in plan if o in skip]
    unchanged = [(o, n) for o, n in plan if o == n]

    width = max((len(o) for o, _ in plan), default=0)

    for old, new in sorted(changed):
        print(f"  {old.ljust(width)}  {color(ARROW, 'cyan', use_color)}  "
              f"{color(new, 'green', use_color)}")
    for old, new in sorted(skipped):
        print(f"  {old.ljust(width)}  {color(ARROW, 'red', use_color)}  "
              f"{color(new + '   [KONFLIKT – přeskočeno]', 'red', use_color)}")
    if show_all:
        for old, _ in sorted(unchanged):
            print(color(f"  {old.ljust(width)}     (beze změny)", "dim", use_color))

    print()
    print(f"  Změněno: {color(str(len(changed)), 'green', use_color)}   "
          f"Konflikty: {color(str(len(skipped)), 'red' if skipped else 'dim', use_color)}   "
          f"Beze změny: {color(str(len(unchanged)), 'dim', use_color)}")
    return changed


def apply_renames(changed, directory, use_color):
    """Bezpečné přejmenování přes dočasné názvy (řeší prohození A<->B i cykly)."""
    tag = uuid.uuid4().hex[:8]
    temps = []
    try:
        for i, (old, _) in enumerate(changed):
            tmp = f".__rename_{tag}_{i}__"
            os.rename(os.path.join(directory, old), os.path.join(directory, tmp))
            temps.append(tmp)
        for (old, new), tmp in zip(changed, temps):
            os.rename(os.path.join(directory, tmp), os.path.join(directory, new))
        print(color(f"\n  Hotovo – přejmenováno {len(changed)} souborů.", "green", use_color))
    except OSError as e:
        print(color(f"\n  CHYBA při přejmenování: {e}", "red", use_color), file=sys.stderr)
        sys.exit(1)


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description="Inteligentní přejmenovávač souborů (náhled; --apply provede změny).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Příklady:\n"
               "  python intelligent_file_renamer.py \"*.mp4\"\n"
               "  python intelligent_file_renamer.py \"*.mp4\" --apply\n"
               "  python intelligent_file_renamer.py \"??Test*.mp3\" --dir ./hudba --apply")
    ap.add_argument("pattern", help="glob vzor souborů, např. *.mkv nebo ??Test*.mp3")
    ap.add_argument("--apply", action="store_true", help="provést přejmenování (jinak jen náhled)")
    ap.add_argument("--dir", default=".", help="adresář se soubory (výchozí: aktuální)")
    ap.add_argument("--sep", default=None,
                    help="oddělovač slov v názvu (výchozí: mezera/bílé znaky)")
    ap.add_argument("--no-pad", action="store_true", help="nezarovnávat čísla nulami")
    ap.add_argument("--no-words", action="store_true", help="nedoplňovat chybějící slova")
    ap.add_argument("--min-width", type=int, default=0,
                    help="minimální počet číslic pro zarovnání")
    ap.add_argument("-i", "--ignore-case", action="store_true",
                    help="ignorovat velikost písmen u vzoru")
    ap.add_argument("--all", action="store_true", help="vypsat i nezměněné soubory")
    ap.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                    help="obarvení výstupu (výchozí: auto)")
    ap.add_argument("--ascii", action="store_true",
                    help="použít '->' místo šipky '→' (pro problémové konzole)")
    args = ap.parse_args()

    global ARROW
    if args.ascii:
        ARROW = "->"

    ansi_ok = setup_console()
    use_color = want_color(args.color, ansi_ok)

    if not os.path.isdir(args.dir):
        print(f"Adresář neexistuje: {args.dir}", file=sys.stderr)
        sys.exit(1)

    entries = [e for e in os.listdir(args.dir)
               if os.path.isfile(os.path.join(args.dir, e))]
    if args.ignore_case:
        pat = re.compile(fnmatch.translate(args.pattern), re.IGNORECASE)
        files = [e for e in entries if pat.match(e)]
    else:
        files = fnmatch.filter(entries, args.pattern)

    if not files:
        print(f"Vzoru '{args.pattern}' v '{args.dir}' neodpovídá žádný soubor.")
        sys.exit(0)

    plan = build_plan(files, args.sep, not args.no_pad, not args.no_words, args.min_width)
    skip = detect_collisions(plan, set(entries))

    mode = "APLIKACE" if args.apply else "NÁHLED"
    print(color(f"\n[{mode}] vzor '{args.pattern}'  ({len(files)} souborů)\n", "yellow", use_color))
    changed = print_preview(plan, skip, args.all, use_color)

    if args.apply:
        if changed:
            apply_renames(changed, args.dir, use_color)
        else:
            print(color("\n  Není co přejmenovat.", "dim", use_color))
    else:
        print(color("\n  Toto byl pouze náhled. Pro provedení přidej přepínač --apply.",
                    "yellow", use_color))


if __name__ == "__main__":
    main()
