#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nastavení výchozích (default) stop podle jazyka.

Proskenuje videa (.mkv, .mp4) v aktuálním adresáři, ukáže jaké audio a
titulkové jazyky obsahují, nabídne výběr a pak u každého souboru:
  - zruší VŠECHNY default flagy audia / titulků,
  - nastaví jako výchozí první stopu zvoleného jazyka.

  - MKV: přes MKVToolNix (mkvpropedit) – mění flagy NA MÍSTĚ, okamžitě.
  - MP4: přes ffmpeg – musí soubor přemuxovat (přepis), proto pomalejší.

Forced flagy zůstávají nedotčené (mění se jen default).

Nástroje: MKVToolNix (mkvmerge -J + mkvpropedit) pro MKV, ffmpeg/ffprobe pro MP4.
Hledají se v PATH / Program Files\\MKVToolNix / .ffmpeg (ffmpeg se umí i stáhnout).

Použití:
    python set_default_tracks.py                      # interaktivní výběr
    python set_default_tracks.py --audio-lang en --sub-lang cs
    python set_default_tracks.py --audio-lang en --sub-lang none   # titulky: žádné
    python set_default_tracks.py --audio-lang en --sub-lang cs --yes
    python set_default_tracks.py "D:\\serial" -r
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

VIDEO_EXTS = {".mkv", ".mp4"}

LANG3 = {
    "en": "eng", "cs": "cze", "sk": "slo", "de": "ger", "fr": "fre",
    "es": "spa", "it": "ita", "pt": "por", "nl": "dut", "pl": "pol",
    "ru": "rus", "uk": "ukr", "ja": "jpn", "ko": "kor", "zh": "chi",
    "hu": "hun", "ro": "rum", "sv": "swe", "no": "nor", "da": "dan",
    "fi": "fin", "el": "gre", "tr": "tur", "ar": "ara", "he": "heb",
    "th": "tha", "vi": "vie", "id": "ind", "hi": "hin", "bg": "bul",
    "hr": "hrv", "sr": "srp", "sl": "slv", "et": "est", "lv": "lav",
    "lt": "lit", "fil": "fil", "ms": "msa",
}
_ALIAS3 = {"ces": "cze", "deu": "ger", "fra": "fre", "nld": "dut",
           "ron": "rum", "slk": "slo", "zho": "chi", "ell": "gre",
           "may": "msa"}
LANG_NAME = {
    "eng": "English", "cze": "Czech", "slo": "Slovak", "ger": "German",
    "fre": "French", "spa": "Spanish", "ita": "Italian", "por": "Portuguese",
    "dut": "Dutch", "pol": "Polish", "rus": "Russian", "ukr": "Ukrainian",
    "jpn": "Japanese", "kor": "Korean", "chi": "Chinese", "hun": "Hungarian",
    "rum": "Romanian", "swe": "Swedish", "nor": "Norwegian", "dan": "Danish",
    "fin": "Finnish", "gre": "Greek", "tur": "Turkish", "ara": "Arabic",
    "heb": "Hebrew", "tha": "Thai", "vie": "Vietnamese", "ind": "Indonesian",
    "hin": "Hindi", "bul": "Bulgarian", "hrv": "Croatian", "srp": "Serbian",
    "slv": "Slovenian", "fil": "Filipino", "msa": "Malay", "und": "(neznámý)",
}


def canon(lang):
    """Sjednotí jazykový kód na 3-písmenný kanonický tvar (en/cs -> eng/cze)."""
    l = (lang or "").strip().lower()
    if not l:
        return "und"
    if l in LANG3:
        return LANG3[l]
    return _ALIAS3.get(l, l)


def lang_label(code3):
    name = LANG_NAME.get(code3)
    return f"{code3} – {name}" if name else code3


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


C = Palette(False)


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
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


# =========================================================================== #
#  Nástroje – MKVToolNix
# =========================================================================== #
MKVMERGE = "mkvmerge"
MKVPROPEDIT = "mkvpropedit"
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


# =========================================================================== #
#  Nástroje – ffmpeg / ffprobe (PATH / .ffmpeg / stažení)
# =========================================================================== #
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
FFMPEG_DOWNLOAD_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _exe(name):
    return name + ".exe" if os.name == "nt" else name


def _resolve_tool(value, name):
    if not value:
        return None
    exe = _exe(name)
    if os.path.isdir(value):
        for cand in (os.path.join(value, exe), os.path.join(value, "bin", exe)):
            if os.path.isfile(cand):
                return cand
        return None
    if os.path.isfile(value):
        return value
    return shutil.which(value) or shutil.which(exe)


def _try_ff(path):
    if not path:
        return False
    try:
        subprocess.run([path, "-version"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception:
        return False


def _try_tool(path):
    if not path:
        return False
    try:
        subprocess.run([path, "--version"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception:
        return False


def _cache_dir():
    base = os.path.dirname(os.path.abspath(sys.argv[0] or ".")) or os.getcwd()
    return os.path.join(base, ".ffmpeg")


def _find_cached(name, search_dirs):
    exe = _exe(name)
    for d in search_dirs:
        if not d or not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            if exe in files:
                p = os.path.join(root, exe)
                if os.name != "nt":
                    try:
                        os.chmod(p, 0o755)
                    except OSError:
                        pass
                return p
    return None


def _extract_archive(path, dest, url):
    import zipfile
    import tarfile
    lower = (url or path).lower()
    if lower.endswith(".zip") or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            z.extractall(dest)
    elif tarfile.is_tarfile(path):
        with tarfile.open(path) as t:
            t.extractall(dest)
    else:
        raise ValueError("Neznámý formát archivu ffmpeg.")


def _download_ffmpeg(url):
    import urllib.request
    cache = _cache_dir()
    os.makedirs(cache, exist_ok=True)
    print(f"ffmpeg nenalezen; stahuji z {url}")
    tmp = os.path.join(cache, "ffmpeg_download.tmp")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length", 0) or 0)
        done = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done * 100 // total:3d}%  "
                      f"{done // 1048576} / {total // 1048576} MB", end="")
        print()
    print("Rozbaluji ffmpeg ...")
    _extract_archive(tmp, cache, url)
    try:
        os.remove(tmp)
    except OSError:
        pass


def ensure_ffmpeg(target_dir, allow_download):
    global FFMPEG, FFPROBE
    search = [_cache_dir(), os.path.join(target_dir, ".ffmpeg"), target_dir,
              os.path.join(os.getcwd(), ".ffmpeg"), os.getcwd()]
    ff = _resolve_tool(FFMPEG, "ffmpeg")
    if not _try_ff(ff):
        ff = _find_cached("ffmpeg", search)
        if not _try_ff(ff):
            ff = None
    if ff is None and allow_download and FFMPEG_DOWNLOAD_URL:
        try:
            _download_ffmpeg(FFMPEG_DOWNLOAD_URL)
        except Exception as e:
            print(f"Stažení ffmpeg selhalo: {e}")
        ff = _find_cached("ffmpeg", search)
        if not _try_ff(ff):
            ff = None
    fp = None
    if ff:
        cand = os.path.join(os.path.dirname(ff), _exe("ffprobe"))
        if _try_ff(cand):
            fp = cand
    if fp is None:
        cand = _resolve_tool(FFPROBE, "ffprobe")
        if _try_ff(cand):
            fp = cand
    if fp is None:
        fp = _find_cached("ffprobe", search) or None
    return ff, fp


# =========================================================================== #
#  Detekce stop
# =========================================================================== #
def collect(directory, recursive):
    videos = []
    walker = os.walk(directory) if recursive else \
        [(directory, [], os.listdir(directory))]
    for root, _d, files in walker:
        for f in files:
            if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                videos.append(os.path.join(root, f))
    return sorted(videos)


def probe_mkv(video):
    """Vrátí {'audio':[...], 'subs':[...]} se selektory a1/s1 pro mkvpropedit."""
    out = run_capture([MKVMERGE, "-J", video]).stdout
    data = json.loads(out or "{}")
    audio, subs = [], []
    ai = si = 0
    for t in data.get("tracks", []):
        pr = t.get("properties", {}) or {}
        lang = canon(pr.get("language"))
        name = pr.get("track_name") or ""
        default = bool(pr.get("default_track"))
        forced = bool(pr.get("forced_track"))
        if t.get("type") == "audio":
            ai += 1
            audio.append({"sel": f"a{ai}", "lang": lang, "name": name,
                          "default": default, "forced": forced})
        elif t.get("type") == "subtitles":
            si += 1
            subs.append({"sel": f"s{si}", "lang": lang, "name": name,
                         "default": default, "forced": forced})
    return {"audio": audio, "subs": subs}


def probe_mp4(video):
    """Vrátí {'audio':[...], 'subs':[...]} s relativním indexem pro ffmpeg."""
    out = run_capture(
        [FFPROBE, "-v", "error", "-show_entries",
         "stream=index,codec_type:stream_tags=language,title:"
         "stream_disposition=default,forced", "-of", "json", video]).stdout
    data = json.loads(out or "{}")
    audio, subs = [], []
    ai = si = 0
    for s in data.get("streams", []):
        ctype = s.get("codec_type")
        tags = s.get("tags", {}) or {}
        disp = s.get("disposition", {}) or {}
        lang = canon(tags.get("language"))
        name = tags.get("title") or ""
        default = bool(disp.get("default"))
        forced = bool(disp.get("forced"))
        if ctype == "audio":
            rec = {"rel": ai, "lang": lang, "name": name,
                   "default": default, "forced": forced}
            ai += 1
            audio.append(rec)
        elif ctype == "subtitle":
            rec = {"rel": si, "lang": lang, "name": name,
                   "default": default, "forced": forced}
            si += 1
            subs.append(rec)
    return {"audio": audio, "subs": subs}


# =========================================================================== #
#  Výběr jazyka
# =========================================================================== #
def aggregate_langs(infos, kind):
    counts = {}
    order = []
    for info in infos.values():
        for tr in info[kind]:
            l = tr["lang"]
            if l not in counts:
                counts[l] = 0
                order.append(l)
            counts[l] += 1
    return [(l, counts[l]) for l in order]


def ask_choice(kind_label, langs, allow_none):
    """Vrátí kanonický kód jazyka, 'keep' (beze změny) nebo 'none'."""
    print(f"\n{C.BOLD}{kind_label}:{C.RESET}")
    for i, (l, n) in enumerate(langs, 1):
        print(f"  {C.CYAN}[{i}]{C.RESET} {lang_label(l)}  ({n}×)")
    print(f"  {C.CYAN}[0]{C.RESET} nechat beze změny")
    if allow_none:
        print(f"  {C.CYAN}[z]{C.RESET} zrušit všechny (žádný výchozí)")
    while True:
        try:
            ans = input(f"Vyber výchozí {kind_label.lower()}: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "keep"
        if ans in ("0", ""):
            return "keep"
        if allow_none and ans == "z":
            return "none"
        if ans.isdigit() and 1 <= int(ans) <= len(langs):
            return langs[int(ans) - 1][0]
        # i kód jazyka napsaný ručně
        cc = canon(ans)
        if cc in [l for l, _ in langs]:
            return cc
        print(f"  {C.YELLOW}Neplatná volba, zkus to znovu.{C.RESET}")


# =========================================================================== #
#  Plán
# =========================================================================== #
def compute_targets(tracks, choice):
    """Pro daný typ stop vrátí seznam (track, desired_default) nebo None=neměnit.

    choice: kanonický kód / 'keep' / 'none'.
    """
    if choice == "keep":
        return None
    targets = []
    first_done = False
    for tr in tracks:
        if choice == "none":
            desired = False
        else:
            match = (tr["lang"] == choice) and not first_done
            if match:
                first_done = True
            desired = match
        targets.append((tr, desired))
    return targets


def describe(targets, chosen, kind):
    if targets is None:
        return f"{C.DIM}{kind}: beze změny{C.RESET}"
    sel_default = [t for t, d in targets if d]
    parts = []
    if sel_default:
        t = sel_default[0]
        ref = t.get("sel") or f"#{t.get('rel')}"
        parts.append(f"{C.GREEN}{ref} ({t['lang']}) → default{C.RESET}")
    elif chosen == "none":
        parts.append(f"{C.YELLOW}vše vyčištěno{C.RESET}")
    else:
        parts.append(f"{C.YELLOW}jazyk {chosen} není – vyčištěno, bez výchozího{C.RESET}")
    cleared = [t for t, d in targets if not d and t["default"]]
    if cleared:
        refs = ", ".join((t.get("sel") or f"#{t.get('rel')}") for t in cleared)
        parts.append(f"{C.DIM}zrušeno u: {refs}{C.RESET}")
    return f"{kind}: " + "; ".join(parts)


def targets_change_anything(targets):
    if targets is None:
        return False
    return any(bool(d) != bool(t["default"]) for t, d in targets)


def mp4_adjust(targets):
    """MP4/MOV neumí mít NULA výchozích stop daného typu – mov vždy nechá
    první „zapnutou". Když nic nevybíráme jako default (none / jazyk chybí),
    raději typ neměníme. Vrací (upravené_targets, narazili_jsme_na_limit)."""
    if targets is None:
        return None, False
    if not any(d for _t, d in targets):
        return None, True
    return targets, False


# =========================================================================== #
#  Aplikace
# =========================================================================== #
def apply_mkv(video, a_targets, s_targets):
    edits = []
    for targets in (a_targets, s_targets):
        if targets is None:
            continue
        for tr, desired in targets:
            if bool(desired) != bool(tr["default"]):
                edits += ["--edit", f"track:{tr['sel']}",
                          "--set", f"flag-default={1 if desired else 0}"]
    if not edits:
        return True, "beze změny"
    res = run_capture([MKVPROPEDIT, video] + edits)
    if res.returncode >= 2:
        tail = [l for l in (res.stdout or "").splitlines() if l.strip()][-3:]
        return False, " | ".join(tail) or "chyba mkvpropedit"
    return True, "ok"


def apply_mp4(video, a_targets, s_targets):
    if not targets_change_anything(a_targets) and \
            not targets_change_anything(s_targets):
        return True, "beze změny"
    disp = []
    for targets, t in ((a_targets, "a"), (s_targets, "s")):
        if targets is None:
            continue
        for tr, desired in targets:
            flags = []
            if desired:
                flags.append("default")
            if tr["forced"]:
                flags.append("forced")
            disp += [f"-disposition:{t}:{tr['rel']}", "+".join(flags) if flags else "0"]
    tmp = video + ".deftmp.mp4"
    cmd = ([FFMPEG, "-y", "-i", video, "-map", "0", "-c", "copy"] + disp
           + ["-default_mode", "passthrough", tmp])
    res = run_capture(cmd)
    if res.returncode != 0 or not os.path.exists(tmp):
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        tail = [l for l in (res.stderr or "").splitlines() if l.strip()][-3:]
        return False, " | ".join(tail) or "chyba ffmpeg"
    try:
        os.replace(tmp, video)
    except OSError as e:
        return False, f"náhrada selhala: {e}"
    return True, "ok (přemuxováno)"


# =========================================================================== #
#  Main
# =========================================================================== #
def main():
    p = argparse.ArgumentParser(
        description="Zruší default flagy audia/titulků a nastaví výchozí dle jazyka.")
    p.add_argument("directory", nargs="?", default=".",
                   help="Adresář (výchozí: aktuální).")
    p.add_argument("-r", "--recursive", action="store_true",
                   help="Projít i podadresáře.")
    p.add_argument("--audio-lang", metavar="KOD",
                   help="Jazyk výchozího audia (např. en). 'keep' = neměnit.")
    p.add_argument("--sub-lang", metavar="KOD",
                   help="Jazyk výchozích titulků. 'none' = žádné, 'keep' = neměnit.")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Neptat se na potvrzení, rovnou provést.")
    p.add_argument("--mkvtoolnix", metavar="CESTA",
                   help="Cesta ke složce MKVToolNix nebo k mkvpropedit.")
    p.add_argument("--ffmpeg", metavar="CESTA", help="Cesta k ffmpeg.")
    p.add_argument("--no-download", action="store_true",
                   help="Nestahovat ffmpeg pro MP4.")
    p.add_argument("--no-color", action="store_true", help="Vypnout barvy.")
    args = p.parse_args()

    global C, MKVMERGE, MKVPROPEDIT, FFMPEG, FFPROBE
    C = setup_console(args.no_color)

    if not os.path.isdir(args.directory):
        raise SystemExit(f"{C.RED}Není adresář: {args.directory}{C.RESET}")

    videos = collect(args.directory, args.recursive)
    if not videos:
        print("Žádná videa (MKV/MP4) nenalezena.")
        return
    have_mkv = any(v.lower().endswith(".mkv") for v in videos)
    have_mp4 = any(v.lower().endswith(".mp4") for v in videos)

    # nástroje
    extra = [args.directory, os.getcwd(),
             os.path.dirname(os.path.abspath(__file__))]
    mkv_ok = ff_ok = False
    if have_mkv:
        if args.mkvtoolnix:
            d = args.mkvtoolnix
            MKVMERGE = find_mkv_tool("mkvmerge", [d]) if os.path.isdir(d) else \
                d.replace("mkvpropedit", "mkvmerge")
            MKVPROPEDIT = find_mkv_tool("mkvpropedit", [d]) if os.path.isdir(d) else d
        else:
            MKVMERGE = find_mkv_tool("mkvmerge", extra)
            MKVPROPEDIT = find_mkv_tool("mkvpropedit", extra)
        mkv_ok = _try_tool(MKVMERGE) and _try_tool(MKVPROPEDIT)
        if mkv_ok:
            print(f"{C.CYAN}mkvpropedit:{C.RESET} {MKVPROPEDIT}")
        else:
            print(f"{C.YELLOW}[VAROVÁNÍ] MKVToolNix nenalezen – MKV přeskočím. "
                  f"https://mkvtoolnix.download/ nebo --mkvtoolnix.{C.RESET}")
    if have_mp4:
        if args.ffmpeg:
            FFMPEG = args.ffmpeg
        ff, fp = ensure_ffmpeg(args.directory, not args.no_download)
        if ff and fp:
            FFMPEG, FFPROBE = ff, fp
            ff_ok = True
            print(f"{C.CYAN}ffmpeg:    {C.RESET} {ff}")
        else:
            print(f"{C.YELLOW}[VAROVÁNÍ] ffmpeg/ffprobe nenalezen – MP4 přeskočím.{C.RESET}")
    print()

    # detekce
    infos = {}
    for v in videos:
        is_mkv = v.lower().endswith(".mkv")
        if is_mkv and not mkv_ok:
            continue
        if (not is_mkv) and not ff_ok:
            continue
        try:
            infos[v] = probe_mkv(v) if is_mkv else probe_mp4(v)
        except Exception as e:
            print(f"{C.YELLOW}!  nelze přečíst {os.path.basename(v)}: {e}{C.RESET}")
    if not infos:
        print("Není co zpracovat.")
        return

    audio_langs = aggregate_langs(infos, "audio")
    sub_langs = aggregate_langs(infos, "subs")

    print(f"{C.BOLD}Souborů ke zpracování: {len(infos)}{C.RESET}")
    print(f"  audio jazyky:   " +
          (", ".join(f"{l}({n})" for l, n in audio_langs) or "—"))
    print(f"  titulkové jaz.: " +
          (", ".join(f"{l}({n})" for l, n in sub_langs) or "—"))

    # výběr audia
    if args.audio_lang:
        audio_choice = "keep" if args.audio_lang.lower() == "keep" \
            else canon(args.audio_lang)
    elif audio_langs and sys.stdin.isatty():
        audio_choice = ask_choice("Audio", audio_langs, allow_none=False)
    else:
        audio_choice = "keep"

    # výběr titulků
    if args.sub_lang:
        sl = args.sub_lang.lower()
        sub_choice = sl if sl in ("keep", "none") else canon(args.sub_lang)
    elif sub_langs and sys.stdin.isatty():
        sub_choice = ask_choice("Titulky", sub_langs, allow_none=True)
    else:
        sub_choice = "keep"

    print(f"\n{C.BOLD}Volba:{C.RESET} audio = "
          f"{C.GREEN}{audio_choice}{C.RESET}, titulky = "
          f"{C.GREEN}{sub_choice}{C.RESET}\n")

    # plán
    plans = []
    n_changes = 0
    for v, info in infos.items():
        is_mp4 = v.lower().endswith(".mp4")
        a_t = compute_targets(info["audio"], audio_choice)
        s_t = compute_targets(info["subs"], sub_choice)
        limited = False
        if is_mp4:
            a_t, la = mp4_adjust(a_t)
            s_t, ls = mp4_adjust(s_t)
            limited = la or ls
        will = targets_change_anything(a_t) or targets_change_anything(s_t)
        if will:
            n_changes += 1
        plans.append((v, a_t, s_t, will))
        head = f"{C.BOLD}{C.CYAN}# {os.path.basename(v)}{C.RESET}"
        if not will:
            head += f"  {C.DIM}(beze změny){C.RESET}"
        print(head)
        print(f"    {describe(a_t, audio_choice, 'audio')}")
        print(f"    {describe(s_t, sub_choice, 'titulky')}")
        if limited:
            print(f"    {C.DIM}(MP4: nelze mít nula výchozích stop – "
                  f"první stopa typu zůstává zapnutá){C.RESET}")

    if audio_choice == "keep" and sub_choice == "keep":
        print(f"\n{C.YELLOW}Nic nevybráno – konec.{C.RESET}")
        return
    if n_changes == 0:
        print(f"\n{C.GREEN}Vše už je nastaveno tak, jak chceš. Nic k provedení.{C.RESET}")
        return

    # potvrzení
    mp4_count = sum(1 for v, *_ in plans if v.lower().endswith(".mp4"))
    if mp4_count and ff_ok:
        print(f"\n{C.YELLOW}Pozor: {mp4_count} MP4 se musí přemuxovat "
              f"(přepis celého souboru, pomalejší). MKV se mění na místě.{C.RESET}")
    if not args.yes:
        try:
            ans = input(f"\nProvést změny u {n_changes} souborů? [a/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans not in ("a", "ano", "y", "yes"):
            print("Zrušeno.")
            return

    # aplikace
    print()
    ok = 0
    for v, a_t, s_t, will in plans:
        if not will:
            continue
        is_mkv = v.lower().endswith(".mkv")
        print(f"{C.CYAN}>>{C.RESET} {os.path.basename(v)}")
        success, msg = apply_mkv(v, a_t, s_t) if is_mkv \
            else apply_mp4(v, a_t, s_t)
        if success:
            ok += 1
            print(f"   {C.GREEN}OK{C.RESET} {C.DIM}({msg}){C.RESET}")
        else:
            print(f"   {C.RED}CHYBA: {msg}{C.RESET}")
    col = C.GREEN if ok == n_changes else C.YELLOW
    print(f"\n{C.BOLD}Hotovo:{C.RESET} {col}{ok}/{n_changes}{C.RESET} souborů.")


if __name__ == "__main__":
    main()
