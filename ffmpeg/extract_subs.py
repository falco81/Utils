#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extrakce vložených titulků z videa.

  - MKV: nativně přes MKVToolNix (mkvmerge -J + mkvextract) – rychlé, beze ztráty.
  - MP4: přes ffmpeg (mkvextract MP4 neumí); textové titulky se převedou na .srt.

Výstup: <název_videa>.<jazyk>[.forced].<přípona>
(tedy ve formátu, který umí načíst import_subs.py – round-trip).

ASS/SSA -> .ass/.ssa. Obrázkové (PGS/VobSub) se ve výchozím stavu přeskočí
(--include-image je uloží jako .sup/.idx, ty ale potřebují OCR).

mkvextract se hledá v PATH / Program Files\\MKVToolNix / přes --mkvtoolnix.
ffmpeg se hledá v PATH / .ffmpeg / případně stáhne (jako import/patreon skripty).

Použití:
    python extract_subs.py "D:\\serial"               # NÁHLED
    python extract_subs.py "D:\\serial" --apply        # extrahuje
    python extract_subs.py "D:\\serial" --apply --lang en
    python extract_subs.py "D:\\serial" --apply --overwrite
    python extract_subs.py -r --apply
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

VIDEO_EXTS = {".mkv", ".mp4"}

LANG2 = {
    "eng": "en", "cze": "cs", "ces": "cs", "slo": "sk", "slk": "sk",
    "ger": "de", "deu": "de", "fre": "fr", "fra": "fr", "spa": "es",
    "ita": "it", "por": "pt", "dut": "nl", "nld": "nl", "pol": "pl",
    "rus": "ru", "ukr": "uk", "jpn": "ja", "kor": "ko", "chi": "zh",
    "zho": "zh", "hun": "hu", "rum": "ro", "ron": "ro", "swe": "sv",
    "nor": "no", "dan": "da", "fin": "fi", "gre": "el", "ell": "el",
    "tur": "tr", "ara": "ar", "heb": "he", "tha": "th", "vie": "vi",
    "ind": "id", "hin": "hi", "bul": "bg", "hrv": "hr", "srp": "sr",
    "slv": "sl", "est": "et", "lav": "lv", "lit": "lt",
}

MKV_CODEC_EXT = {
    "S_TEXT/UTF8": (".srt", False), "S_TEXT/ASCII": (".srt", False),
    "S_TEXT/ASS": (".ass", False), "S_TEXT/SSA": (".ssa", False),
    "S_TEXT/WEBVTT": (".vtt", False),
    "S_HDMV/PGS": (".sup", True), "S_HDMV/TEXTST": (".sup", True),
    "S_VOBSUB": (".idx", True), "S_DVBSUB": (".sub", True),
}
FF_ASS = {"ass", "ssa"}
FF_IMAGE = {"hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvdsub", "dvb_subtitle"}


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
#  MKVToolNix (mkvmerge, mkvextract)
# =========================================================================== #
MKVMERGE = "mkvmerge"
MKVEXTRACT = "mkvextract"
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
#  ffmpeg / ffprobe (PATH / .ffmpeg / stažení)
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


def ensure_ffmpeg(target_dir, allow_download, want_download):
    global FFMPEG, FFPROBE
    search = [_cache_dir(), os.path.join(target_dir, ".ffmpeg"), target_dir,
              os.path.join(os.getcwd(), ".ffmpeg"), os.getcwd()]
    ff = _resolve_tool(FFMPEG, "ffmpeg")
    if not _try_ff(ff):
        ff = _find_cached("ffmpeg", search)
        if not _try_ff(ff):
            ff = None
    if ff is None and want_download and allow_download and FFMPEG_DOWNLOAD_URL:
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
#  Společné
# =========================================================================== #
def lang_for_name(lang3):
    if not lang3:
        return "und"
    if len(lang3) == 2:
        return lang3
    return LANG2.get(lang3, lang3)


def collect(directory, recursive):
    videos = []
    walker = os.walk(directory) if recursive else \
        [(directory, [], os.listdir(directory))]
    for root, _d, files in walker:
        for f in files:
            if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                videos.append(os.path.join(root, f))
    return videos


def make_name(vstem, lang2, forced, ext, used):
    parts = [lang2] + (["forced"] if forced else [])
    base = vstem + "." + ".".join(parts)
    name = base + ext
    n = 2
    while name.lower() in used:
        name = base + f".{n}" + ext
        n += 1
    used.add(name.lower())
    return name


# =========================================================================== #
#  MKV větev
# =========================================================================== #
def mkv_probe(video):
    out = run_capture([MKVMERGE, "-J", video]).stdout
    data = json.loads(out or "{}")
    subs = []
    for t in data.get("tracks", []):
        if t.get("type") != "subtitles":
            continue
        pr = t.get("properties", {}) or {}
        subs.append({
            "id": t["id"],
            "codec_id": (pr.get("codec_id") or "").upper(),
            "lang": (pr.get("language") or "").lower(),
            "forced": bool(pr.get("forced_track"))
            or "forced" in (pr.get("track_name") or "").lower(),
        })
    return subs


def plan_mkv(video, lang_filter, include_image, overwrite):
    vdir = os.path.dirname(video)
    vstem = os.path.splitext(os.path.basename(video))[0]
    used = set()
    items = []
    for s in mkv_probe(video):
        lang2 = lang_for_name(s["lang"])
        if lang_filter and lang2 not in lang_filter and s["lang"] not in lang_filter:
            continue
        ext, is_image = MKV_CODEC_EXT.get(
            s["codec_id"],
            (".srt", False) if s["codec_id"].startswith("S_TEXT") else (".sub", True))
        if is_image and not include_image:
            items.append(("skip",
                          f"obrázkové ({s['codec_id']}) – přeskočeno", None, None))
            continue
        name = make_name(vstem, lang2, s["forced"], ext, used)
        out = os.path.join(vdir, name)
        if os.path.exists(out) and not overwrite:
            items.append(("exists", f"už existuje: {name}", None, out))
            continue
        items.append(("do",
                      f"stopa {s['id']} [{s['codec_id']}, {s['lang'] or 'und'}"
                      + (", forced" if s["forced"] else "") + f"] -> {name}",
                      s["id"], out))
    return items


def run_mkv(video, items):
    specs = [(it[2], it[3]) for it in items if it[0] == "do"]
    if not specs:
        return 0
    cmd = [MKVEXTRACT, "tracks", video] + [f"{tid}:{out}" for tid, out in specs]
    res = run_capture(cmd)
    ok = 0
    for tid, out in specs:
        if os.path.exists(out) and os.path.getsize(out) > 0:
            ok += 1
            print(f"{C.GREEN}OK:{C.RESET} {os.path.basename(out)}")
        else:
            print(f"   {C.RED}CHYBA: {os.path.basename(out)}{C.RESET}")
    if res.returncode >= 2:
        for l in [l for l in (res.stdout or "").splitlines() if l.strip()][-6:]:
            print(f"   {C.DIM}| {l}{C.RESET}")
    return ok


# =========================================================================== #
#  MP4 větev
# =========================================================================== #
def mp4_probe(video):
    out = run_capture(
        [FFPROBE, "-v", "error", "-select_streams", "s", "-show_entries",
         "stream=index,codec_name:stream_tags=language,title:"
         "stream_disposition=forced", "-of", "json", video]).stdout
    data = json.loads(out or "{}")
    subs = []
    for rel, s in enumerate(data.get("streams", [])):
        tags = s.get("tags", {}) or {}
        disp = s.get("disposition", {}) or {}
        title = tags.get("title") or ""
        subs.append({
            "rel": rel,
            "codec": (s.get("codec_name") or "").lower(),
            "lang": (tags.get("language") or "").lower(),
            "forced": bool(disp.get("forced")) or "forced" in title.lower(),
        })
    return subs


def plan_mp4(video, lang_filter, include_image, overwrite):
    vdir = os.path.dirname(video)
    vstem = os.path.splitext(os.path.basename(video))[0]
    used = set()
    items = []
    for s in mp4_probe(video):
        lang2 = lang_for_name(s["lang"])
        if lang_filter and lang2 not in lang_filter and s["lang"] not in lang_filter:
            continue
        if s["codec"] in FF_ASS:
            codec, ext, is_image = "copy", ".ass", False
        elif s["codec"] in FF_IMAGE:
            codec, ext, is_image = "copy", ".sup", True
        else:
            codec, ext, is_image = "srt", ".srt", False
        if is_image and not include_image:
            items.append(("skip",
                          f"obrázkové ({s['codec']}) – přeskočeno", None, None))
            continue
        name = make_name(vstem, lang2, s["forced"], ext, used)
        out = os.path.join(vdir, name)
        if os.path.exists(out) and not overwrite:
            items.append(("exists", f"už existuje: {name}", None, out))
            continue
        cmd = [FFMPEG, "-y", "-i", video, "-map", f"0:s:{s['rel']}",
               "-c:s", codec, out]
        items.append(("do",
                      f"stopa #{s['rel']} [{s['codec']}, {s['lang'] or 'und'}"
                      + (", forced" if s["forced"] else "") + f"] -> {name}",
                      cmd, out))
    return items


def run_mp4(items):
    ok = 0
    for kind, desc, cmd, out in items:
        if kind != "do":
            continue
        res = run_capture(cmd)
        if res.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
            print(f"   {C.RED}CHYBA: {os.path.basename(out)}{C.RESET}")
            for l in [l for l in (res.stderr or "").splitlines() if l.strip()][-6:]:
                print(f"   {C.DIM}| {l}{C.RESET}")
            if os.path.exists(out) and os.path.getsize(out) == 0:
                try:
                    os.remove(out)
                except OSError:
                    pass
            continue
        ok += 1
        print(f"{C.GREEN}OK:{C.RESET} {os.path.basename(out)}")
    return ok


def main():
    p = argparse.ArgumentParser(
        description="Vyextrahuje vložené titulky (MKV přes mkvextract, MP4 přes ffmpeg).")
    p.add_argument("directory", nargs="?", default=".")
    p.add_argument("-r", "--recursive", action="store_true")
    p.add_argument("--apply", action="store_true",
                   help="Skutečně extrahovat (bez něj jen náhled).")
    p.add_argument("--lang", metavar="KODY", help="Jen tyto jazyky (en,cs).")
    p.add_argument("--include-image", action="store_true",
                   help="Extrahovat i obrázkové titulky (PGS/VobSub).")
    p.add_argument("--overwrite", action="store_true",
                   help="Přepsat existující soubory.")
    p.add_argument("--mkvtoolnix", metavar="CESTA",
                   help="Cesta ke složce MKVToolNix nebo k mkvextract.")
    p.add_argument("--ffmpeg", metavar="CESTA", help="Cesta k ffmpeg.")
    p.add_argument("--no-download", action="store_true",
                   help="Nestahovat ffmpeg pro MP4.")
    p.add_argument("--no-color", action="store_true", help="Vypnout barvy.")
    args = p.parse_args()

    global C
    C = setup_console(args.no_color)

    if not os.path.isdir(args.directory):
        raise SystemExit(f"{C.RED}Není adresář: {args.directory}{C.RESET}")

    videos = collect(args.directory, args.recursive)
    have_mkv = any(v.lower().endswith(".mkv") for v in videos)
    have_mp4 = any(v.lower().endswith(".mp4") for v in videos)

    global MKVMERGE, MKVEXTRACT, FFMPEG, FFPROBE
    extra = [args.directory, os.getcwd(),
             os.path.dirname(os.path.abspath(__file__))]
    if args.mkvtoolnix:
        d = args.mkvtoolnix
        MKVMERGE = find_mkv_tool("mkvmerge", [d]) if os.path.isdir(d) else d
        MKVEXTRACT = find_mkv_tool("mkvextract", [d]) if os.path.isdir(d) \
            else d.replace("mkvmerge", "mkvextract")
    else:
        MKVMERGE = find_mkv_tool("mkvmerge", extra)
        MKVEXTRACT = find_mkv_tool("mkvextract", extra)
    mkv_ok = bool(MKVMERGE and MKVEXTRACT)
    if have_mkv:
        if mkv_ok:
            print(f"{C.CYAN}mkvextract:{C.RESET} {MKVEXTRACT}")
        else:
            print(f"{C.YELLOW}[VAROVÁNÍ] MKVToolNix nenalezen – MKV přeskočím. "
                  f"Nainstaluj z https://mkvtoolnix.download/ nebo --mkvtoolnix.{C.RESET}")

    ff_ok = False
    if have_mp4:
        if args.ffmpeg:
            FFMPEG = args.ffmpeg
        ff, fp = ensure_ffmpeg(args.directory, not args.no_download, want_download=True)
        if ff and fp:
            FFMPEG, FFPROBE = ff, fp
            ff_ok = True
            print(f"{C.CYAN}ffmpeg:    {C.RESET} {ff}")
        else:
            print(f"{C.YELLOW}[VAROVÁNÍ] ffmpeg/ffprobe nenalezen – MP4 přeskočím.{C.RESET}")
    print()

    print(f"Nalezeno videí: {C.BOLD}{len(videos)}{C.RESET}\n")

    lang_filter = None
    if args.lang:
        lang_filter = {x.strip().lower() for x in args.lang.split(",") if x.strip()}

    plans = []
    total_do = 0
    for v in sorted(videos):
        is_mkv = v.lower().endswith(".mkv")
        if is_mkv and not mkv_ok:
            continue
        if (not is_mkv) and not ff_ok:
            continue
        try:
            if is_mkv:
                items = plan_mkv(v, lang_filter, args.include_image, args.overwrite)
            else:
                items = plan_mp4(v, lang_filter, args.include_image, args.overwrite)
        except Exception as e:
            print(f"{C.BOLD}{C.CYAN}# {os.path.basename(v)}{C.RESET}\n"
                  f"    {C.RED}!  chyba čtení: {e}{C.RESET}")
            continue
        print(f"{C.BOLD}{C.CYAN}# {os.path.basename(v)}{C.RESET}")
        if not items:
            print(f"    {C.DIM}(žádné titulkové stopy){C.RESET}")
        for it in items:
            mark, col = {"do": ("+", C.GREEN), "skip": ("-", C.YELLOW),
                         "exists": ("=", C.DIM)}.get(it[0], (" ", ""))
            print(f"    {col}{mark}{C.RESET} {it[1]}")
            if it[0] == "do":
                total_do += 1
        plans.append((v, is_mkv, items))
        print()

    if total_do == 0:
        print("Nic k extrakci.")
        return
    if not args.apply:
        print(f"{C.YELLOW}[NÁHLED]{C.RESET} {total_do} titulků k extrakci. "
              "Přidej --apply.")
        return

    ok = 0
    for v, is_mkv, items in plans:
        ok += run_mkv(v, items) if is_mkv else run_mp4(items)
    col = C.GREEN if ok == total_do else C.YELLOW
    print(f"\n{C.BOLD}Hotovo:{C.RESET} {col}{ok}/{total_do}{C.RESET} titulků.")


if __name__ == "__main__":
    main()
