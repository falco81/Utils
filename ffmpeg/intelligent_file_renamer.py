#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Inteligentní přejmenovávač souborů.

Najde soubory odpovídající glob vzoru (např. *.mkv nebo ??Test*.mp3) a sjednotí názvy:
  1) zarovná čísla nulami podle nejdelšího čísla na dané pozici,
  2) doplní chybějící společná slova podle většinového vzoru série,
  3) normalizuje velikost písmen (Title Case; zkratky jako CTLBT/LND zůstanou velké),
  4) odstraní emoji/obrázkové znaky a znaky zakázané ve Windows,
  5) automaticky rozpozná více sérií v jedné složce a každou sjednotí zvlášť.

Tokenizuje podle oddělovačů (mezera, pomlčka, podtržítko), takže funguje i na názvy
typu CTLBT-EP-1-PT-1-REACTION. Původní oddělovače se ve výstupu zachovávají.

Defaultně jen náhled; skutečné přejmenování provede přepínač --apply.
"""

import argparse
import fnmatch
import os
import re
import sys
import unicodedata
import uuid
from collections import Counter, defaultdict

ARROW = "\u2192"
NUM_RE = re.compile(r"\d+")
WORDCHARS = re.compile(r"[^\w]", re.UNICODE)
# tokenizace: číslo | slovo (písmena, s apostrofy) | oddělovač/interpunkce
TOKEN_RE = re.compile(r"\d+|[^\W\d_]+(?:['\u2019][^\W\d_]+)*|[\W_]+", re.UNICODE)


# --------------------------------------------------------------------------- #
#  Konzole (UTF-8 + ANSI barvy pro Windows 10)
# --------------------------------------------------------------------------- #
def _enable_win_ansi():
    try:
        import ctypes
        k = ctypes.windll.kernel32
        ok = True
        for hid in (-11, -12):
            h = k.GetStdHandle(hid)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, mode.value | 0x0004)
            else:
                ok = False
        return ok
    except Exception:
        return False


def setup_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    try:
        import colorama
        try:
            colorama.just_fix_windows_console()
        except AttributeError:
            colorama.init()
        return True
    except ImportError:
        pass
    if os.name == "nt":
        return _enable_win_ansi()
    return True


def want_color(arg_color, ansi_ok):
    if arg_color == "never" or os.environ.get("NO_COLOR") is not None:
        return False
    if arg_color == "always":
        return True
    return ansi_ok and sys.stdout.isatty()


def color(s, c, on):
    codes = {"green": "32", "yellow": "33", "red": "31", "dim": "2", "cyan": "36"}
    return f"\033[{codes[c]}m{s}\033[0m" if on else s


# --------------------------------------------------------------------------- #
#  Zobrazovací šířka (kvůli zarovnání sloupců napříč terminály)
# --------------------------------------------------------------------------- #
def char_width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def disp_width(s):
    return sum(char_width(c) for c in s)


def visible(s):
    """Zobrazovaná podoba názvu bez emoji (stabilní šířka na všech terminálech)."""
    return re.sub(r"\s+", " ", strip_pictographs(s)).strip()


def pad_to(s, width):
    return s + " " * max(0, width - disp_width(visible(s)))


# --------------------------------------------------------------------------- #
#  Čištění názvů
# --------------------------------------------------------------------------- #
ILLEGAL_WIN = set('<>:"/\\|?*')
RESERVED_WIN = {"CON", "PRN", "AUX", "NUL",
                *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
EMOJI_RANGES = [
    (0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x2300, 0x23FF),
    (0x2B00, 0x2BFF), (0x1F1E6, 0x1F1FF), (0xFE00, 0xFE0F), (0x200D, 0x200D),
]


def strip_pictographs(s):
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
    return "".join(ch for ch in s
                   if ch not in ILLEGAL_WIN and unicodedata.category(ch) != "Cc")


def clean_string(stem, removes):
    """Očistí název (emoji, zakázané znaky, uživatelské --remove, sjednocení mezer)."""
    s = strip_pictographs(stem)
    s = strip_illegal(s)
    for rgx in removes:
        s = rgx.sub("", s)
    return re.sub(r"[ \t]+", " ", s).strip()


def finalize_stem(stem):
    stem = re.sub(r"[ \t]+", " ", stem).strip().strip(" .")
    if stem.upper() in RESERVED_WIN:
        stem += "_"
    return stem or "_"


# --------------------------------------------------------------------------- #
#  Tokenizace + velikost písmen
# --------------------------------------------------------------------------- #
SMALL_WORDS = {"a", "an", "the", "and", "but", "or", "nor", "for", "of", "to",
               "in", "on", "at", "by", "vs", "with", "as", "from", "into",
               "over", "per"}
VOWELS = set("AEIOU")


def tokenize(s):
    """Vrátí list (kind, text): kind = 'num' | 'word' | 'sep'."""
    toks = []
    for m in TOKEN_RE.finditer(s):
        t = m.group(0)
        if t.isdigit():
            toks.append(("num", t))
        elif t[0].isalpha() or t[0] in "'\u2019":
            toks.append(("word", t))
        else:
            toks.append(("sep", t))
    return toks


def is_acronym(tok):
    letters = [c for c in tok if c.isalpha()]
    return len(letters) >= 3 and tok == tok.upper() and not (set(tok.upper()) & VOWELS)


def _cap_runs(tok):
    return re.sub(r"[^\W\d_]+(?:['\u2019][^\W\d_]+)*",
                  lambda m: m.group(0)[0].upper() + m.group(0)[1:],
                  tok.lower(), flags=re.UNICODE)


def case_word(tok, mode, first):
    if mode == "lower":
        return tok.lower()
    if mode == "upper":
        return tok.upper()
    if mode == "keep":
        return tok
    if is_acronym(tok):                      # CTLBT, LND, ...
        return tok
    core = WORDCHARS.sub("", tok).lower()
    if core in SMALL_WORDS and not first:    # a, of, the, over, ...
        return tok.lower()
    return _cap_runs(tok)


def apply_case(tokens, mode):
    out, first = [], True
    for kind, text in tokens:
        if kind == "word":
            out.append((kind, case_word(text, mode, first)))
            first = False
        else:
            out.append((kind, text))
    return out


def keys_of(tokens):
    """Klíče slov/čísel (bez oddělovačů): číslo -> '#', slovo -> malými."""
    return [("#" if k == "num" else t.lower()) for k, t in tokens if k != "sep"]


def split_lead(tokens):
    """Non-sep tokeny s předchozím oddělovačem + koncový oddělovač.
    Vrací (list (lead, kind, text), trailing_sep)."""
    res, lead = [], ""
    for kind, text in tokens:
        if kind == "sep":
            lead += text
        else:
            res.append((lead, kind, text))
            lead = ""
    return res, lead


def has_letters(key):
    return any(c.isalpha() for c in key)


# --------------------------------------------------------------------------- #
#  Detekce sérií
# --------------------------------------------------------------------------- #
DEFAULT_STOP = {
    "episode", "episodes", "episod", "ep", "eps", "reaction", "reactions",
    "react", "reacts", "reacting", "uncut", "full", "part", "pt", "video",
    "official", "hd", "fhd", "uhd", "4k", "2k", "premiere", "finale", "final",
    "trailer", "teaser", "subbed", "sub", "dub", "raw", "movie", "series",
    "season", "watch", "watching", "review", "recap", "highlights", "clip",
    "clips", "cut", "edit", "compilation", "special", "bonus", "early",
    "access", "kdrama", "drama", "anime",
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for",
    "with", "as", "by", "from", "into", "is", "are", "was", "were", "be",
    "this", "that", "these", "those", "here", "there", "now", "new", "my",
    "your", "our", "their", "his", "her", "its", "i", "im", "you", "we",
    "they", "he", "she", "it", "vs", "ft", "feat", "no", "yes", "so", "just",
}


def file_words(tokens, stop):
    return [t.lower() for k, t in tokens
            if k == "word" and t.lower() not in stop]


def longest_common_run(a, b):
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
                best = max(best, ndp[j + 1])
        dp = ndp
    return best


def cluster_series(word_lists, min_run):
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
#  Zarovnání čísel + LCS
# --------------------------------------------------------------------------- #
def compute_widths(cleans, min_width):
    vals, rawlen = defaultdict(list), defaultdict(list)
    for s in cleans:
        for i, run in enumerate(NUM_RE.findall(s)):
            vals[i].append(int(run))
            rawlen[i].append(len(run))
    widths = {}
    for i in vals:
        w = max(len(str(max(vals[i]))), max(rawlen[i]))
        if min_width:
            w = max(w, min_width)
        widths[i] = w
    return widths


def lcs_align(ref, other):
    n, m = len(ref), len(other)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            dp[i][j] = dp[i + 1][j + 1] + 1 if ref[i] == other[j] \
                else max(dp[i + 1][j], dp[i][j + 1])
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


def render(out_tokens, widths, trail=""):
    """out_tokens: list (lead, kind, text). Čísla zarovná podle widths."""
    parts, counter = [], 0
    for lead, kind, text in out_tokens:
        parts.append(lead)
        if kind == "num":
            parts.append(text.zfill(widths.get(counter, len(text))))
            counter += 1
        else:
            parts.append(text)
    parts.append(trail)
    return finalize_stem("".join(parts))


# --------------------------------------------------------------------------- #
#  Sjednocení jedné série
# --------------------------------------------------------------------------- #
def consensus_name(item, ref_ns, ref_keys, widths, do_words):
    """Bezpečný režim: zachová formát souboru, doplní chybějící společná slova."""
    file_ns = item["ns"]
    file_keys = item["keys"]
    ops = lcs_align(ref_keys, file_keys)
    matched_ref = {op[1] for op in ops if op[0] == "match"}
    file_word_only = any(op[0] == "file" and file_ns[op[1]][1] == "word" for op in ops)

    out = []
    for op in ops:
        if op[0] == "match":
            out.append(file_ns[op[2]])
        elif op[0] == "file":
            out.append(file_ns[op[1]])
        else:                                 # ref-only – kandidát na doplnění
            r = op[1]
            lead, kind, text = ref_ns[r]
            if not do_words or kind != "word" or file_word_only:
                continue
            # nevkládej slovo vázané na chybějící číslo (např. "Pt" bez čísla)
            bound = ((r + 1 < len(ref_keys) and ref_keys[r + 1] == "#"
                      and (r + 1) not in matched_ref)
                     or (r - 1 >= 0 and ref_keys[r - 1] == "#"
                         and (r - 1) not in matched_ref))
            if bound:
                continue
            out.append((lead, kind, text))
    return render(out, widths, item["trail"])


def strict_name(item, ref_ns, ref_keys, ref_trail, widths, ref_num_slots, ref_word_keys):
    """Striktní režim: přepíše soubor přesně podle vzoru série (mění se jen čísla)."""
    nums = [t for _, k, t in item["ns"] if k == "num"]
    file_word_keys = {k for k in item["keys"] if has_letters(k)}
    overlap = len(ref_word_keys & file_word_keys)
    fits = (len(nums) >= ref_num_slots
            and (not ref_word_keys or overlap >= (len(ref_word_keys) + 1) // 2))
    if not fits:
        return consensus_name(item, ref_ns, ref_keys, widths, do_words=True)

    out, ni = [], 0
    for lead, kind, text in ref_ns:
        if kind == "num":
            out.append((lead, "num", nums[ni])); ni += 1
        else:
            out.append((lead, kind, text))
    return render(out, widths, ref_trail)


def process_group(members, do_pad, do_words, min_width, strict):
    widths = compute_widths([m["clean"] for m in members], min_width) if do_pad else {}
    patterns = [tuple(m["keys"]) for m in members]
    counts = Counter(patterns)
    best = max(counts, key=lambda p: (counts[p], len(p)))
    ref = next(m for m, p in zip(members, patterns) if p == best)
    ref_ns, ref_trail = ref["ns"], ref["trail"]
    ref_keys = list(best)
    ref_num_slots = sum(1 for k in ref_keys if k == "#")
    ref_word_keys = {k for k in ref_keys if has_letters(k)}

    out = []
    for m in members:
        if strict:
            new = strict_name(m, ref_ns, ref_keys, ref_trail, widths,
                              ref_num_slots, ref_word_keys)
        else:
            new = consensus_name(m, ref_ns, ref_keys, widths, do_words)
        out.append((m["name"], new + m["ext"]))
    return out


def build_plan(filenames, do_pad, do_words, min_width, removes=(), case_mode="title",
               strict=False, group=True, group_min=1, stop=None):
    stop = DEFAULT_STOP if stop is None else (DEFAULT_STOP | set(stop))
    items = []
    for name in filenames:
        stem, ext = os.path.splitext(name)
        clean = clean_string(stem, removes)
        toks = apply_case(tokenize(clean), case_mode)
        ns, trail = split_lead(toks)
        items.append({"name": name, "ext": ext, "clean": clean,
                      "ns": ns, "trail": trail, "keys": keys_of(toks),
                      "words": file_words(toks, stop)})

    if group:
        groups = cluster_series([it["words"] for it in items], group_min)
    else:
        groups = [list(range(len(items)))]

    plan = []
    for gi in groups:
        plan.extend(process_group([items[i] for i in gi],
                                  do_pad, do_words, min_width, strict))
    order = {it["name"]: k for k, it in enumerate(items)}
    plan.sort(key=lambda p: order[p[0]])
    return plan, len(groups)


# --------------------------------------------------------------------------- #
#  Kolize, výpis, aplikace
# --------------------------------------------------------------------------- #
def detect_collisions(plan, existing):
    changing = {o: n for o, n in plan if o != n}
    targets = Counter(changing.values())
    sources = set(changing)
    skip = set()
    for o, n in changing.items():
        if targets[n] > 1 or (n in existing and n not in sources):
            skip.add(o)
    return skip


def print_preview(plan, skip, show_all, use_color):
    changed = [(o, n) for o, n in plan if o != n and o not in skip]
    skipped = [(o, n) for o, n in plan if o in skip]
    unchanged = [(o, n) for o, n in plan if o == n]
    width = max((disp_width(visible(o)) for o, _ in plan), default=0)

    for o, n in sorted(changed):
        print(f"  {pad_to(o, width)}  {color(ARROW, 'cyan', use_color)}  "
              f"{color(n, 'green', use_color)}")
    for o, n in sorted(skipped):
        print(f"  {pad_to(o, width)}  {color(ARROW, 'red', use_color)}  "
              f"{color(n + '   [KONFLIKT – přeskočeno]', 'red', use_color)}")
    if show_all:
        for o, _ in sorted(unchanged):
            print(color(f"  {pad_to(o, width)}     (beze změny)", "dim", use_color))

    print()
    print(f"  Změněno: {color(str(len(changed)), 'green', use_color)}   "
          f"Konflikty: {color(str(len(skipped)), 'red' if skipped else 'dim', use_color)}   "
          f"Beze změny: {color(str(len(unchanged)), 'dim', use_color)}")
    return changed


def apply_renames(changed, directory, use_color):
    tag = uuid.uuid4().hex[:8]
    temps = []
    try:
        for i, (o, _) in enumerate(changed):
            tmp = f".__rename_{tag}_{i}__"
            os.rename(os.path.join(directory, o), os.path.join(directory, tmp))
            temps.append(tmp)
        for (o, n), tmp in zip(changed, temps):
            os.rename(os.path.join(directory, tmp), os.path.join(directory, n))
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
               "  intelligent_file_renamer.py \"*.mp4\"\n"
               "  intelligent_file_renamer.py \"*.mkv\" --strict --apply\n"
               "  intelligent_file_renamer.py \"*.mp4\" --strict --min-width 2 --remove \"\\(UNCUT\\)\"")
    ap.add_argument("pattern", help="glob vzor, např. *.mkv nebo ??Test*.mp3")
    ap.add_argument("--apply", action="store_true", help="provést přejmenování")
    ap.add_argument("--dir", default=".", help="adresář se soubory (výchozí: aktuální)")
    ap.add_argument("--no-pad", action="store_true", help="nezarovnávat čísla nulami")
    ap.add_argument("--no-words", action="store_true", help="nedoplňovat chybějící slova")
    ap.add_argument("--strict", action="store_true",
                    help="sjednotit názvy podle vzoru série (zahodí odlišnosti)")
    ap.add_argument("--no-group", action="store_true", help="nerozdělovat do sérií")
    ap.add_argument("--group-min", type=int, default=1,
                    help="min. počet společných slov pro spojení do série (výchozí 1)")
    ap.add_argument("--group-stop", action="append", default=[], metavar="SLOVO",
                    help="další slovo ignorované při detekci sérií")
    ap.add_argument("--case", choices=["title", "lower", "upper", "keep"], default="title",
                    help="velikost písmen (výchozí: title)")
    ap.add_argument("--remove", action="append", default=[], metavar="REGEX",
                    help="regulární výraz k odstranění z názvu (lze vícekrát)")
    ap.add_argument("--min-width", type=int, default=0, help="min. počet číslic zarovnání")
    ap.add_argument("-i", "--ignore-case", action="store_true", help="ignorovat velikost u vzoru")
    ap.add_argument("--all", action="store_true", help="vypsat i nezměněné soubory")
    ap.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    ap.add_argument("--ascii", action="store_true", help="'->' místo šipky '→'")
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

    global strip_pictographs
    plan, n_groups = build_plan(
        files, not args.no_pad, not args.no_words, args.min_width,
        removes=removes, case_mode=args.case, strict=args.strict,
        group=not args.no_group, group_min=args.group_min,
        stop=[w.lower() for w in args.group_stop])
    skip = detect_collisions(plan, set(entries))

    mode = "APLIKACE" if args.apply else "NÁHLED"
    series = "" if args.no_group else f", {n_groups} sérií"
    print(color(f"\n[{mode}] vzor '{args.pattern}'  ({len(files)} souborů{series})\n",
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
