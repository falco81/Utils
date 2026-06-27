#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Import (mux) titulků do videa pomocí MKVToolNix (mkvmerge).

Najde v adresáři videa (.mkv, .mp4) a titulky (.srt/.ass/.ssa/.sup/.sub),
spáruje je podle kódu epizody SxxExx a každý titulek vloží do odpovídajícího
videa jako stopu. Nastaví jazyk (language), název stopy (track name),
volitelně default flag a forced flag (pozná z názvu ...forced.srt).

mkvmerge přečte i MP4 jako vstup (video/audio jen zkopíruje) a vždy vyrobí
MKV – odpadá tím nespolehlivý mov_text v MP4 i "Conversion failed!".

MKVToolNix se hledá v PATH a v obvyklém umístění (Program Files\\MKVToolNix),
nebo přes --mkvmerge. (Nestahuje se automaticky – https://mkvtoolnix.download/.)

Použití:
    python import_subs.py "D:\\serial"                    # jen NÁHLED
    python import_subs.py "D:\\serial" --apply             # provede mux
    python import_subs.py "D:\\serial" --apply --default --default-lang en
    python import_subs.py "D:\\serial" --apply --replace   # přepíše originál
    python import_subs.py -r --apply                       # i podadresáře
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

VIDEO_EXTS = {".mkv", ".mp4"}
SUB_EXTS = {".srt", ".ass", ".ssa", ".sup", ".sub", ".vtt"}

FLAG_TOKENS = {"forced", "sdh", "cc", "hi", "foreign", "full", "default"}
EPISODE_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,2})")
LANG_RE = re.compile(r"^[A-Za-z]{2,3}$")

LANG3 = {
    "en": "eng", "cs": "cze", "sk": "slo", "de": "ger", "fr": "fre",
    "es": "spa", "it": "ita", "pt": "por", "nl": "dut", "pl": "pol",
    "ru": "rus", "uk": "ukr", "ja": "jpn", "ko": "kor", "zh": "chi",
    "hu": "hun", "ro": "rum", "sv": "swe", "no": "nor", "da": "dan",
    "fi": "fin", "el": "gre", "tr": "tur", "ar": "ara", "he": "heb",
    "th": "tha", "vi": "vie", "id": "ind", "hi": "hin", "bg": "bul",
    "hr": "hrv", "sr": "srp", "sl": "slv", "et": "est", "lv": "lav",
    "lt": "lit",
}
LANG_NAME = {
    "eng": "English", "cze": "Czech", "slo": "Slovak", "ger": "German",
    "fre": "French", "spa": "Spanish", "ita": "Italian", "por": "Portuguese",
    "dut": "Dutch", "pol": "Polish", "rus": "Russian", "ukr": "Ukrainian",
    "jpn": "Japanese", "kor": "Korean", "chi": "Chinese", "hun": "Hungarian",
    "rum": "Romanian", "swe": "Swedish", "nor": "Norwegian", "dan": "Danish",
    "fin": "Finnish", "gre": "Greek", "tur": "Turkish", "ara": "Arabic",
    "heb": "Hebrew", "tha": "Thai", "vie": "Vietnamese", "ind": "Indonesian",
    "hin": "Hindi", "bul": "Bulgarian", "hrv": "Croatian", "srp": "Serbian",
    "slv": "Slovenian",
}


# =========================================================================== #
#  Windows 10 CLI + barvy (colorama volitelně)
# =========================================================================== #
class Palette:
    _CODES = {"RESET": "\033[0m", "DIM": "\033[2m", "BOLD": "\033[1m",
              "RED": "\033[31m", "GREEN": "\033[32m", "YELLOW": "\033[33m",
              "CYAN": "\033[36m", "BLUE": "\033[34m"}

    def __init__(self, on):
        for k, v in self._CODES.items():
            setattr(self, k, v if on else "")


C = Palette(False)  # přepíše se v main()


def _enable_windows_vt():
    if os.name != "nt":
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


def setup_console(no_color):
    # UTF-8 výstup – Windows konzole jinak padá na české znaky a symboly
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    use_color = (not no_color) and bool(
        getattr(sys.stdout, "isatty", lambda: False)())
    if use_color:
        ok = False
        try:
            import colorama
            try:
                colorama.just_fix_windows_console()
            except AttributeError:
                colorama.init()
            ok = True
        except Exception:
            ok = _enable_windows_vt()
        use_color = ok
    return Palette(use_color)


def run_capture(cmd):
    """subprocess.run s bezpečným dekódováním výstupu (Windows kódování)."""
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


# =========================================================================== #
#  MKVToolNix – hledání nástroje
# =========================================================================== #
MKVMERGE = "mkvmerge"
_MKV_DIRS = [r"C:\Program Files\MKVToolNix", r"C:\Program Files (x86)\MKVToolNix"]


def find_mkv_tool(name, extra_dirs):
    exe = name + ".exe" if os.name == "nt" else name
    p = shutil.which(name) or shutil.which(exe)
    if p:
        return p
    for d in list(_MKV_DIRS) + list(extra_dirs):
        if not d or not os.path.isdir(d):
            continue
        direct = os.path.join(d, exe)
        if os.path.isfile(direct):
            return direct
        for root, _dirs, files in os.walk(d):
            if root[len(d):].count(os.sep) >= 3:
                continue
            if exe in files:
                return os.path.join(root, exe)
    return None


def _try_tool(path):
    if not path:
        return False
    try:
        subprocess.run([path, "--version"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception:
        return False


# =========================================================================== #
#  Párování a metadata titulků
# =========================================================================== #
def episode_key(name):
    m = EPISODE_RE.search(name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def fmt_key(k):
    return f"S{k[0]:02d}E{k[1]:02d}"


def parse_sub_meta(sub_name, forced_lang=None):
    stem = os.path.splitext(sub_name)[0]
    parts = stem.split(".")
    lang = forced_lang
    forced = False
    sdh = False
    tail = []
    while parts and len(tail) < 2 and (
        parts[-1].lower() in FLAG_TOKENS or LANG_RE.match(parts[-1])
    ):
        tail.insert(0, parts.pop())
    for tok in tail:
        low = tok.lower()
        if low == "forced":
            forced = True
        elif low in ("sdh", "cc", "hi"):
            sdh = True
        elif LANG_RE.match(tok) and lang is None:
            lang = low
    if lang is None:
        lang3 = "und"
    elif len(lang) == 3:
        lang3 = lang
    else:
        lang3 = LANG3.get(lang, lang)
    name = LANG_NAME.get(lang3, lang3.upper())
    if forced:
        name += " (Forced)"
    elif sdh:
        name += " (SDH)"
    return lang3, name, forced


def collect_track_names(pairs, forced_lang):
    """Interaktivně se zeptá na název stopy pro každou unikátní kombinaci
    (jazyk, forced, SDH). Vrátí dict: (lang3, forced, sdh) -> název.

    Ptá se jen jednou na danou kombinaci – u 10 epizod se stejnými titulky
    není potřeba 10krát potvrzovat 'Czech'.
    """
    seen = {}  # zachová pořadí, ve kterém se objevily
    for v in sorted(pairs):
        for s in pairs[v]:
            lang3, name, forced = parse_sub_meta(
                os.path.basename(s), forced_lang)
            sdh = "(SDH)" in name
            key = (lang3, forced, sdh)
            if key not in seen:
                seen[key] = name

    if not seen:
        return {}

    print(f"\n{C.BOLD}Názvy stop titulků v MKV{C.RESET}")
    print(f"{C.DIM}(Enter = ponechat výchozí, nebo napiš vlastní "
          f"název){C.RESET}\n")
    mapping = {}
    for key, default_name in seen.items():
        lang3, forced, sdh = key
        tag_parts = [lang3]
        if forced:
            tag_parts.append("forced")
        if sdh:
            tag_parts.append("SDH")
        tag = "/".join(tag_parts)
        prompt = f"  {tag} [{C.CYAN}{default_name}{C.RESET}]: "
        try:
            user_input = input(prompt).strip()
        except EOFError:
            user_input = ""
        mapping[key] = user_input if user_input else default_name
    print()
    return mapping


def existing_subtitle_ids(video):
    try:
        out = run_capture([MKVMERGE, "-J", video]).stdout
        data = json.loads(out or "{}")
        return [t["id"] for t in data.get("tracks", [])
                if t.get("type") == "subtitles"]
    except Exception:
        return []


def collect(directory, recursive):
    videos, subs = [], []
    walker = os.walk(directory) if recursive else \
        [(directory, [], os.listdir(directory))]
    for root, _d, files in walker:
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            full = os.path.join(root, f)
            if ext in VIDEO_EXTS:
                videos.append(full)
            elif ext in SUB_EXTS:
                subs.append(full)
    return videos, subs


def build_mkvmerge_cmd(video, sub_list, out_path, set_default,
                       default_lang, name_override, forced_lang,
                       name_map=None):
    metas = []
    for s in sub_list:
        lang3, name, forced = parse_sub_meta(os.path.basename(s), forced_lang)
        if name_override:
            name = name_override
        elif name_map:
            sdh = "(SDH)" in name
            key = (lang3, forced, sdh)
            if key in name_map:
                name = name_map[key]
        metas.append((s, lang3, name, forced))

    chosen = None
    if set_default and metas:
        if default_lang:
            dl = LANG3.get(default_lang.lower(), default_lang.lower())
            for i, (_s, lang3, _n, _f) in enumerate(metas):
                if lang3 == dl:
                    chosen = i
                    break
        if chosen is None:
            chosen = 0

    cmd = [MKVMERGE, "-o", out_path]
    if chosen is not None:
        for tid in existing_subtitle_ids(video):
            cmd += ["--default-track", f"{tid}:no"]
    cmd += [video]

    for i, (subfile, lang3, name, forced) in enumerate(metas):
        cmd += ["--language", f"0:{lang3}", "--track-name", f"0:{name}"]
        cmd += ["--default-track",
                "0:yes" if (chosen is not None and i == chosen) else "0:no"]
        cmd += ["--forced-track", "0:yes" if forced else "0:no"]
        cmd += [subfile]
    return cmd


def main():
    p = argparse.ArgumentParser(
        description="Naimportuje titulky do videa pomocí mkvmerge (MKVToolNix). "
                    "Výstup je vždy MKV.")
    p.add_argument("directory", nargs="?", default=".",
                   help="Adresář (výchozí: aktuální).")
    p.add_argument("-r", "--recursive", action="store_true",
                   help="Projít i podadresáře.")
    p.add_argument("--apply", action="store_true",
                   help="Skutečně spustit (bez něj jen vypíše příkazy).")
    p.add_argument("--default", action="store_true",
                   help="Naimportovaný titulek označit jako výchozí.")
    p.add_argument("--default-lang", metavar="KOD",
                   help="Při více titulcích vybrat výchozí podle jazyka.")
    p.add_argument("--replace", action="store_true",
                   help="Přepsat originál (jinak zapíše do podsložky 'muxed').")
    p.add_argument("--name", metavar="NAZEV",
                   help="Vlastní název stopy místo automatického.")
    p.add_argument("--ask-name", action="store_true",
                   help="Interaktivně se zeptat na název stopy pro každou "
                        "unikátní kombinaci jazyka (a forced/SDH).")
    p.add_argument("--lang", metavar="KOD",
                   help="Vynutit jazyk, když ho v názvu titulku není.")
    p.add_argument("--mkvmerge", metavar="CESTA",
                   help="Cesta k mkvmerge(.exe) nebo ke složce MKVToolNix.")
    p.add_argument("--no-color", action="store_true", help="Vypnout barvy.")
    args = p.parse_args()

    global C
    C = setup_console(args.no_color)

    if not os.path.isdir(args.directory):
        raise SystemExit(f"{C.RED}Není adresář: {args.directory}{C.RESET}")

    global MKVMERGE
    if args.mkvmerge:
        cand = args.mkvmerge
        if os.path.isdir(cand):
            cand = find_mkv_tool("mkvmerge", [cand])
        MKVMERGE = cand
    else:
        MKVMERGE = find_mkv_tool(
            "mkvmerge", [args.directory, os.getcwd(),
                         os.path.dirname(os.path.abspath(__file__))])
    if not _try_tool(MKVMERGE):
        print(f"{C.RED}Nenašel jsem mkvmerge (MKVToolNix).{C.RESET}\n"
              "Nainstaluj z https://mkvtoolnix.download/ a přidej do PATH, "
              "nebo zadej --mkvmerge \"C:\\Program Files\\MKVToolNix\\mkvmerge.exe\".")
        raise SystemExit(1)
    print(f"{C.CYAN}mkvmerge:{C.RESET} {MKVMERGE}\n")

    videos, subs = collect(args.directory, args.recursive)
    print(f"Nalezeno videí: {C.BOLD}{len(videos)}{C.RESET}, "
          f"titulků: {C.BOLD}{len(subs)}{C.RESET}\n")

    vmap = {}
    for v in sorted(videos):
        k = episode_key(os.path.basename(v))
        if k is not None:
            vmap.setdefault(k, v)

    pairs = {}
    for s in sorted(subs):
        k = episode_key(os.path.basename(s))
        if k is None:
            print(f"{C.YELLOW}-  Přeskakuji (chybí SxxExx): "
                  f"{os.path.basename(s)}{C.RESET}")
            continue
        v = vmap.get(k)
        if v is None:
            print(f"{C.YELLOW}-  Bez videa pro {fmt_key(k)}: "
                  f"{os.path.basename(s)}{C.RESET}")
            continue
        pairs.setdefault(v, []).append(s)

    if not pairs:
        print("\nNic ke zpracování.")
        return

    name_map = None
    if args.ask_name and not args.name:
        name_map = collect_track_names(pairs, args.lang)

    jobs = []
    for v in sorted(pairs):
        sub_list = pairs[v]
        vdir = os.path.dirname(v)
        vstem = os.path.splitext(os.path.basename(v))[0]
        if args.replace:
            final = os.path.join(vdir, vstem + ".mkv")
            out_path = os.path.join(vdir, vstem + ".muxtmp.mkv")
        else:
            final = os.path.join(vdir, "muxed", vstem + ".mkv")
            out_path = final
        cmd = build_mkvmerge_cmd(v, sub_list, out_path, args.default,
                                 args.default_lang, args.name, args.lang,
                                 name_map=name_map)
        jobs.append((v, sub_list, cmd, out_path, final))

    print(f"Naplánováno videí ke zpracování: {C.BOLD}{len(jobs)}{C.RESET}\n")
    for v, sub_list, cmd, out_path, final in jobs:
        print(f"{C.BOLD}{C.CYAN}# {os.path.basename(v)}{C.RESET}")
        for s in sub_list:
            print(f"    {C.GREEN}+{C.RESET} {os.path.basename(s)}")
        print(f"    {C.DIM}" + " ".join(_shq(c) for c in cmd) + f"{C.RESET}")
        print()

    if not args.apply:
        print(f"{C.YELLOW}[NÁHLED]{C.RESET} Nic se nespustilo. "
              "Přidej --apply pro provedení.")
        return

    ok = 0
    for v, sub_list, cmd, out_path, final in jobs:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        print(f"{C.CYAN}>>{C.RESET} {os.path.basename(final)}")
        res = run_capture(cmd)
        if res.returncode >= 2 or not os.path.exists(out_path):
            print(f"   {C.RED}CHYBA mkvmerge (konec výpisu):{C.RESET}")
            for l in [l for l in (res.stdout or "").splitlines() if l.strip()][-8:]:
                print(f"   {C.DIM}| {l}{C.RESET}")
            if args.replace and os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass
            continue
        if res.returncode == 1:
            print(f"   {C.YELLOW}(mkvmerge varování – výstup ale vznikl){C.RESET}")
        if args.replace:
            try:
                if os.path.abspath(v) != os.path.abspath(final) and os.path.exists(v):
                    os.remove(v)
                os.replace(out_path, final)
            except OSError as e:
                print(f"   {C.RED}CHYBA při náhradě originálu: {e}{C.RESET}")
                continue
        ok += 1
        print(f"   {C.GREEN}OK{C.RESET}")
    col = C.GREEN if ok == len(jobs) else C.YELLOW
    print(f"\n{C.BOLD}Hotovo:{C.RESET} {col}{ok}/{len(jobs)}{C.RESET} videí.")


def _shq(s):
    if s and all(c.isalnum() or c in "-_.:=/+" for c in s):
        return s
    return '"' + s.replace('"', '\\"') + '"'


if __name__ == "__main__":
    main()
