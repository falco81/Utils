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
  3) Normalizuje velikost písmen (výchozí Title Case – první písmeno velké)
  4) Odstraní emoji / obrázkové znaky a znaky zakázané ve Windows
     a ošetří rezervované názvy (CON, PRN, ...) i koncové mezery/tečky
  5) Automaticky rozpozná více sérií v jedné složce (shlukování podle
     společné fráze) a každou sjednotí zvlášť

Volitelně:
  --strict        sjednotí VŠECHNY názvy podle společného vzoru (zahodí odlišnosti)
  --remove REGEX  odstraní zadaný řetězec/vzor (lze vícekrát)
  --case          title | lower | upper | keep
  --min-width N   minimální počet číslic pro zarovnání
  --keep-emoji    ponechá emoji

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
import unicodedata
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
#  Čištění názvů (Windows znaky, emoji, velikost písmen)
# --------------------------------------------------------------------------- #
ILLEGAL_WIN = set('<>:"/\\|?*')          # znaky zakázané v názvech souborů Windows
RESERVED_WIN = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
# Rozsahy emoji / obrázkových (piktografických) znaků a modifikátorů
EMOJI_RANGES = [
    (0x1F000, 0x1FAFF),  # emoji, symboly, piktogramy (🩵 ad.)
    (0x2600, 0x27BF),    # různé symboly + dingbaty (☔ ☀ ✂ ad.)
    (0x2300, 0x23FF),    # technické symboly (⏰ ⌛ ad.)
    (0x2B00, 0x2BFF),    # šipky/hvězdy
    (0x1F1E6, 0x1F1FF),  # vlajky (regional indicators)
    (0xFE00, 0xFE0F),    # variation selectors
    (0x200D, 0x200D),    # zero-width joiner
]


def strip_pictographs(s):
    """Odstraní emoji, piktografy a neviditelné spojovací znaky."""
    out = []
    for ch in s:
        cp = ord(ch)
        if any(a <= cp <= b for a, b in EMOJI_RANGES):
            continue
        if unicodedata.category(ch) in ("So", "Cf", "Cs", "Co"):
            continue
        out.append(ch)
    return "".join(out)


def strip_illegal(s):
    """Odstraní znaky zakázané ve Windows a řídicí znaky."""
    return "".join(
        ch for ch in s
        if ch not in ILLEGAL_WIN and unicodedata.category(ch) != "Cc"
    )


SMALL_WORDS = {
    "a", "an", "the", "and", "but", "or", "nor", "for", "of", "to", "in",
    "on", "at", "by", "vs", "with", "as", "from", "into", "over", "per",
}


def _cap_runs(tok):
    """Velké první písmeno každého slova; apostrof neukončuje slovo
    ('non-anime' -> 'Non-Anime', "don't" -> "Don't")."""
    return re.sub(r"[^\W\d_]+(?:['\u2019][^\W\d_]+)*",
                  lambda m: m.group(0)[0].upper() + m.group(0)[1:],
                  tok.lower(), flags=re.UNICODE)


def _title_token(tok, first):
    core = WORDCHARS.sub("", tok).lower()
    if core in SMALL_WORDS and not first:
        return tok.lower()
    return _cap_runs(tok)


def apply_case(s, mode):
    """Normalizace velikosti písmen. 'title' = první písmeno slov velké,
    spojky/členy (a, of, the, ...) zůstanou malé (kromě prvního slova)."""
    if mode == "lower":
        return s.lower()
    if mode == "upper":
        return s.upper()
    if mode == "title":
        toks = s.split(" ")
        out, first = [], True
        for t in toks:
            if t == "":
                out.append(t)
                continue
            out.append(_title_token(t, first))
            first = False
        return " ".join(out)
    return s  # keep


def clean_stem(stem, removes, case_mode):
    """Celý čisticí řetězec aplikovaný na název (bez přípony) PŘED tokenizací."""
    s = strip_pictographs(stem)
    s = strip_illegal(s)
    for rgx in removes:
        s = rgx.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()     # sjednoť mezery PŘED úpravou velikosti
    s = apply_case(s, case_mode)
    return s


def finalize_stem(stem):
    """Závěrečné ošetření výsledku pro Windows (mezery, tečky, rezervované názvy)."""
    stem = re.sub(r"\s+", " ", stem).strip()
    stem = stem.strip(" .")               # Windows nepovoluje koncové mezery/tečky
    if stem.upper() in RESERVED_WIN:      # CON, PRN, COM1 ...
        stem += "_"
    return stem or "_"


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
#  Detekce sérií (shlukování souborů podle společné fráze)
# --------------------------------------------------------------------------- #
# Generická slova, která NEodlišují sérii – pro shlukování se ignorují.
DEFAULT_STOP = {
    "episode", "episodes", "ep", "eps", "reaction", "reactions", "react",
    "reacts", "uncut", "full", "part", "pt", "video", "official", "hd",
    "fhd", "uhd", "4k", "2k", "premiere", "finale", "trailer", "teaser",
    "subbed", "sub", "dub", "raw", "movie", "series",
}

WORDCHARS = re.compile(r"[^\w]", re.UNICODE)


def file_words(fields, stop):
    """Z polí udělá seznam slov (malá, bez interpunkce, bez čísel a stop-slov)."""
    out = []
    for f in fields:
        w = WORDCHARS.sub("", f).lower()
        if not w or not any(c.isalpha() for c in w):
            continue                       # přeskoč čísla a samotnou interpunkci
        if w in stop:
            continue
        out.append(w)
    return out


def longest_common_run(a, b):
    """Délka nejdelší společné SOUVISLÉ posloupnosti slov dvou seznamů."""
    if not a or not b:
        return 0
    dp = [0] * (len(b) + 1)
    best = 0
    for i in range(len(a)):
        ndp = [0] * (len(b) + 1)
        ai = a[i]
        for j in range(len(b)):
            if ai == b[j]:
                ndp[j + 1] = dp[j] + 1
                if ndp[j + 1] > best:
                    best = ndp[j + 1]
        dp = ndp
    return best


def cluster_series(word_lists, min_run):
    """Single-linkage shlukování: spoj soubory se společnou frází délky >= min_run."""
    n = len(word_lists)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if longest_common_run(word_lists[i], word_lists[j]) >= min_run:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj

    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return list(groups.values())


# --------------------------------------------------------------------------- #
#  Hlavní logika
# --------------------------------------------------------------------------- #
def build_plan(filenames, sep, do_pad, do_words, min_width,
               removes=(), case_mode="title", strict=False,
               group=True, group_min=2, stop=None):
    """Vrátí (list dvojic (old, new), počet detekovaných sérií)."""
    join = " " if sep is None else sep
    stop = DEFAULT_STOP if stop is None else (DEFAULT_STOP | set(stop))

    items = []
    for name in filenames:
        stem, ext = os.path.splitext(name)
        clean = clean_stem(stem, removes, case_mode)
        fields = split_fields(clean, sep)
        items.append({"name": name, "ext": ext, "stem": clean,
                      "fields": fields, "keys": [key_of(f) for f in fields],
                      "words": file_words(fields, stop)})

    # rozdělení do sérií
    if group:
        groups = cluster_series([it["words"] for it in items], group_min)
    else:
        groups = [list(range(len(items)))]

    plan = []
    for gidx in groups:
        members = [items[i] for i in gidx]
        plan.extend(process_group(members, do_pad, do_words, min_width, strict, join))

    # zachovej pořadí podle původního názvu
    order = {it["name"]: k for k, it in enumerate(items)}
    plan.sort(key=lambda p: order[p[0]])
    return plan, len(groups)


def process_group(members, do_pad, do_words, min_width, strict, join):
    """Aplikuje sjednocení názvů na jednu sérii (skupinu souborů)."""
    widths = compute_widths([m["stem"] for m in members], min_width) if do_pad else {}

    patterns = [tuple(m["keys"]) for m in members]
    counts = Counter(patterns)
    best = max(counts.keys(), key=lambda p: (counts[p], len(p)))
    ref_keys = list(best)
    exemplar = next(m["fields"] for m, pat in zip(members, patterns) if pat == best)
    ref_num_slots = sum(1 for k in ref_keys if k == "#")
    ref_word_keys = {k for k in ref_keys if has_letters(k)}

    out = []
    for m in members:
        if strict:
            new_stem = strict_name(m, ref_keys, exemplar, widths,
                                   ref_num_slots, ref_word_keys, join)
        else:
            new_stem = consensus_name(m, ref_keys, exemplar, widths, do_words, join)
        out.append((m["name"], finalize_stem(new_stem) + m["ext"]))
    return out


def consensus_name(it, ref_keys, exemplar, widths, do_words, join):
    """Bezpečný režim: doplní chybějící společná slova, vlastní slova zachová."""
    ops = lcs_align(ref_keys, it["keys"])
    # doplníme jen pokud slova souboru tvoří podmnožinu reference (žádné navíc)
    file_word_only = any(op[0] == "file" and has_letters(it["keys"][op[1]]) for op in ops)
    do_insert = do_words and not file_word_only

    out, counter = [], [0]
    for op in ops:
        if op[0] == "match":
            out.append(pad_field(it["fields"][op[2]], counter, widths))
        elif op[0] == "file":
            out.append(pad_field(it["fields"][op[1]], counter, widths))
        else:  # 'ref' – chybí v souboru
            if do_insert and is_pure_word_key(ref_keys[op[1]]):
                out.append(exemplar[op[1]])
    return join.join(out)


def strict_name(it, ref_keys, exemplar, widths, ref_num_slots, ref_word_keys, join):
    """
    Striktní režim: každý soubor přepíše přesně podle referenčního vzoru
    (mění se jen čísla). Soubory, které vzoru zjevně neodpovídají, ponechá.
    """
    nums = NUM_RE.findall(it["stem"])
    file_word_keys = {k for k in it["keys"] if has_letters(k)}
    overlap = len(ref_word_keys & file_word_keys)
    fits = (len(nums) >= ref_num_slots and
            (not ref_word_keys or overlap >= (len(ref_word_keys) + 1) // 2))
    if not fits:
        # nevejde se do vzoru -> jen vyčištěná podoba (z polí)
        return join.join(it["fields"])

    out, ni, counter = [], 0, [0]
    for k, field in zip(ref_keys, exemplar):
        if k == "#":
            out.append(pad_field(nums[ni], counter, widths))
            ni += 1
        else:
            out.append(field)  # slovo/oddělovač z reference (správná velikost písmen)
    return join.join(out)


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


def char_width(ch):
    """Šířka znaku v terminálových buňkách (emoji a CJK = 2, kombinující = 0)."""
    if unicodedata.combining(ch):
        return 0
    cp = ord(ch)
    if cp == 0x200D or 0xFE00 <= cp <= 0xFE0F:   # ZWJ, variation selectors
        return 0
    if (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or
            0x2300 <= cp <= 0x23FF or 0x2B00 <= cp <= 0x2BFF or
            0x1F1E6 <= cp <= 0x1F1FF):           # emoji / piktogramy
        return 2
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def disp_width(s):
    return sum(char_width(c) for c in s)


def pad_to(s, width):
    """Doplní mezery podle ZOBRAZOVACÍ šířky (správně i s emoji)."""
    return s + " " * max(0, width - disp_width(s))


def visible(s):
    """Zobrazovaná podoba názvu: bez emoji (kvůli stabilní šířce sloupců
    napříč terminály) a se sjednocenými mezerami."""
    return re.sub(r"\s+", " ", strip_pictographs(s)).strip()


def disp_width(s):
    """Šířka v terminálových buňkách (CJK = 2, kombinující = 0, jinak 1).
    Emoji se zobrazují přes visible() vždy odstraněná, takže je neřešíme."""
    w = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def pad_to(s, width):
    return s + " " * max(0, width - disp_width(s))


def print_preview(plan, skip, show_all, use_color, oneline=False):
    changed = [(o, n) for o, n in plan if o != n and o not in skip]
    skipped = [(o, n) for o, n in plan if o in skip]
    unchanged = [(o, n) for o, n in plan if o == n]

    width = max((disp_width(visible(o)) for o, _ in plan), default=0)

    for old, new in sorted(changed):
        print(f"  {pad_to(visible(old), width)}  {color(ARROW, 'cyan', use_color)}  "
              f"{color(new, 'green', use_color)}")
    for old, new in sorted(skipped):
        print(f"  {pad_to(visible(old), width)}  {color(ARROW, 'red', use_color)}  "
              f"{color(new + '   [KONFLIKT – přeskočeno]', 'red', use_color)}")
    if show_all:
        for old, _ in sorted(unchanged):
            print(color(f"  {pad_to(visible(old), width)}     (beze změny)", "dim", use_color))

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
    ap.add_argument("--strict", action="store_true",
                    help="sjednotit VŠECHNY názvy podle společného vzoru (zahodí odlišnosti)")
    ap.add_argument("--no-group", action="store_true",
                    help="nerozdělovat soubory do sérií (zpracovat jako jednu skupinu)")
    ap.add_argument("--group-min", type=int, default=2,
                    help="min. počet společných slov pro spojení do série (výchozí 2)")
    ap.add_argument("--group-stop", action="append", default=[], metavar="SLOVO",
                    help="další generické slovo ignorované při detekci sérií (lze vícekrát)")
    ap.add_argument("--case", choices=["title", "lower", "upper", "keep"], default="title",
                    help="velikost písmen (výchozí: title = první písmeno velké)")
    ap.add_argument("--remove", action="append", default=[], metavar="REGEX",
                    help="regulární výraz k odstranění z názvu (lze použít vícekrát)")
    ap.add_argument("--keep-emoji", action="store_true",
                    help="nemazat emoji / obrázkové znaky")
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

    try:
        removes = [re.compile(r) for r in args.remove]
    except re.error as e:
        print(f"Chybný regulární výraz v --remove: {e}", file=sys.stderr)
        sys.exit(1)

    # --keep-emoji: dočasně vyřadíme odstraňování piktografů
    global strip_pictographs
    if args.keep_emoji:
        orig_strip = strip_pictographs
        strip_pictographs = lambda s: s  # noqa: E731

    plan, n_groups = build_plan(
        files, args.sep, not args.no_pad, not args.no_words, args.min_width,
        removes=removes, case_mode=args.case, strict=args.strict,
        group=not args.no_group, group_min=args.group_min,
        stop=[w.lower() for w in args.group_stop])
    skip = detect_collisions(plan, set(entries))

    mode = "APLIKACE" if args.apply else "NÁHLED"
    series_txt = f", {n_groups} sérií" if not args.no_group else ""
    print(color(f"\n[{mode}] vzor '{args.pattern}'  ({len(files)} souborů{series_txt})\n",
                "yellow", use_color))
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
