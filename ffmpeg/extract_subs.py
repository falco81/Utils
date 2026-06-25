#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extrakce vložených (soft) titulků z videa pomocí ffmpeg.

Najde v adresáři videa (.mkv, .mp4), u každého zjistí titulkové stopy a
vyextrahuje je do samostatných souborů pojmenovaných:

    <název_videa>.<jazyk>[.forced].srt

tedy ve stejném formátu, jaký umí načíst mux_subs.py (round-trip).

Textové titulky (SubRip, mov_text, WebVTT…) se převedou na .srt.
ASS/SSA se uloží jako .ass (zachová styling). Obrázkové titulky
(PGS, VobSub) na .srt převést nejdou – ve výchozím stavu se přeskočí
(s --include-image se uloží jako .sup, ale to chce OCR).

ffmpeg se hledá v PATH, ve složce .ffmpeg vedle skriptu (i bin/),
případně se stáhne – stejně jako mux_subs.py / patreon downloader.

Použití:
    python extract_subs.py "D:\\serial"               # jen NÁHLED
    python extract_subs.py "D:\\serial" --apply        # vyextrahuje
    python extract_subs.py "D:\\serial" --apply --lang en      # jen angličtinu
    python extract_subs.py "D:\\serial" --apply --overwrite    # přepíše existující
    python extract_subs.py -r --apply                  # i podadresáře
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

VIDEO_EXTS = {".mkv", ".mp4"}

# 3-písmenný kód jazyka (ISO 639-2 /B i /T) -> 2-písmenný (pro název souboru)
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

# kodek titulků -> (cílový kodek pro ffmpeg, přípona, je_obrázkový)
TEXT_TO_SRT = {"subrip", "srt", "mov_text", "text", "webvtt", "subviewer",
               "subviewer1", "microdvd", "mpl2"}
ASS_LIKE = {"ass", "ssa"}
IMAGE_SUBS = {"hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvdsub",
              "xsub", "dvb_subtitle"}


# =========================================================================== #
#  ffmpeg / ffprobe – stejná logika jako mux_subs.py (PATH / .ffmpeg / stažení)
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


def _try_exe(path):
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
        raise ValueError("Neznámý formát archivu ffmpeg (čekal jsem .zip nebo .tar.*).")


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


def ensure_tools(target_dir, allow_download, want_download):
    global FFMPEG, FFPROBE
    search = [
        _cache_dir(),
        os.path.join(target_dir, ".ffmpeg"),
        target_dir,
        os.path.join(os.getcwd(), ".ffmpeg"),
        os.getcwd(),
    ]
    ff = _resolve_tool(FFMPEG, "ffmpeg")
    if not _try_exe(ff):
        ff = _find_cached("ffmpeg", search)
        if not _try_exe(ff):
            ff = None
    if ff is None and want_download and allow_download and FFMPEG_DOWNLOAD_URL:
        try:
            _download_ffmpeg(FFMPEG_DOWNLOAD_URL)
        except Exception as e:
            print(f"Stažení ffmpeg selhalo: {e}")
        ff = _find_cached("ffmpeg", search)
        if not _try_exe(ff):
            ff = None
    fp = None
    if ff:
        cand = os.path.join(os.path.dirname(ff), _exe("ffprobe"))
        if _try_exe(cand):
            fp = cand
    if fp is None:
        cand = _resolve_tool(FFPROBE, "ffprobe")
        if _try_exe(cand):
            fp = cand
    if fp is None:
        cand = _find_cached("ffprobe", search)
        if _try_exe(cand):
            fp = cand
    return ff, fp


# =========================================================================== #
#  Vlastní logika
# =========================================================================== #
def collect(directory, recursive):
    videos = []
    walker = os.walk(directory) if recursive else \
        [(directory, [], os.listdir(directory))]
    for root, _d, files in walker:
        for f in files:
            if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                videos.append(os.path.join(root, f))
    return videos


def probe_subs(video):
    """Vrátí seznam titulkových stop: dicty s rel/codec/lang/title/forced."""
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "s",
             "-show_entries",
             "stream=index,codec_name:stream_tags=language,title:"
             "stream_disposition=forced,default",
             "-of", "json", video],
            capture_output=True, text=True, check=True,
        ).stdout
        data = json.loads(out or "{}")
    except Exception as e:
        print(f"!  Nelze přečíst stopy: {os.path.basename(video)} ({e})")
        return []
    subs = []
    for rel, s in enumerate(data.get("streams", [])):
        tags = s.get("tags", {}) or {}
        disp = s.get("disposition", {}) or {}
        lang = (tags.get("language") or "").lower()
        title = tags.get("title") or ""
        subs.append({
            "rel": rel,
            "codec": (s.get("codec_name") or "").lower(),
            "lang": lang,
            "title": title,
            "forced": bool(disp.get("forced")) or "forced" in title.lower(),
        })
    return subs


def lang_for_name(lang3):
    if not lang3:
        return "und"
    if len(lang3) == 2:
        return lang3
    return LANG2.get(lang3, lang3)


def target_for(sub):
    """Vrátí (cílový_kodek, přípona, je_obrázkový) podle kodeku stopy."""
    c = sub["codec"]
    if c in TEXT_TO_SRT:
        return "srt", ".srt", False
    if c in ASS_LIKE:
        return "copy", ".ass", False
    if c in IMAGE_SUBS:
        return "copy", ".sup", True
    # neznámý – zkusíme srt
    return "srt", ".srt", False


def build_jobs(video, subs, lang_filter, include_image, overwrite):
    """Pro jedno video sestaví seznam extrakcí (rel, cmd, out, popis)."""
    vdir = os.path.dirname(video)
    vstem = os.path.splitext(os.path.basename(video))[0]
    used = {}
    jobs = []
    for sub in subs:
        lang2 = lang_for_name(sub["lang"])
        if lang_filter and lang2 not in lang_filter and sub["lang"] not in lang_filter:
            continue
        codec, ext, is_image = target_for(sub)
        if is_image and not include_image:
            jobs.append(("skip", sub, None, None,
                         f"obrázkové titulky ({sub['codec']}) – přeskočeno "
                         f"(nelze .srt; --include-image pro .sup)"))
            continue

        # název: <video>.<lang>[.forced][.N].<ext>
        parts = [lang2]
        if sub["forced"]:
            parts.append("forced")
        base = vstem + "." + ".".join(parts)
        name = base + ext
        # .2/.3 jen při kolizi DVOU stop téhož videa v jednom běhu
        n = 2
        while name.lower() in used:
            name = base + f".{n}" + ext
            n += 1
        used[name.lower()] = True
        out = os.path.join(vdir, name)

        # soubor z dřívějška na disku -> přeskočit (pokud nepřepisujeme)
        if os.path.exists(out) and not overwrite:
            jobs.append(("exists", sub, out, None,
                         f"už existuje: {name}"))
            continue

        cmd = [FFMPEG, "-y", "-i", video, "-map", f"0:s:{sub['rel']}",
               "-c:s", codec, out]
        desc = f"stopa #{sub['rel']} [{sub['codec']}, {sub['lang'] or 'und'}" \
               + (", forced" if sub["forced"] else "") + f"] -> {name}"
        jobs.append(("do", sub, out, cmd, desc))
    return jobs


def main():
    p = argparse.ArgumentParser(
        description="Vyextrahuje vložené titulky z videí do souborů.")
    p.add_argument("directory", nargs="?", default=".",
                   help="Adresář (výchozí: aktuální).")
    p.add_argument("-r", "--recursive", action="store_true",
                   help="Projít i podadresáře.")
    p.add_argument("--apply", action="store_true",
                   help="Skutečně extrahovat (bez něj jen náhled).")
    p.add_argument("--lang", metavar="KODY",
                   help="Jen tyto jazyky, čárkou oddělené (např. en,cs).")
    p.add_argument("--include-image", action="store_true",
                   help="Extrahovat i obrázkové titulky (PGS/VobSub) jako .sup.")
    p.add_argument("--overwrite", action="store_true",
                   help="Přepsat existující soubory titulků.")
    p.add_argument("--ffmpeg", metavar="CESTA", help="Cesta k ffmpeg.exe / složce.")
    p.add_argument("--ffprobe", metavar="CESTA", help="Cesta k ffprobe.exe.")
    p.add_argument("--ffmpeg-url", metavar="URL", default=None,
                   help="URL archivu ffmpeg pro auto-stažení.")
    p.add_argument("--no-download", action="store_true",
                   help="Nestahovat ffmpeg, i když chybí.")
    args = p.parse_args()

    if not os.path.isdir(args.directory):
        raise SystemExit(f"Není adresář: {args.directory}")

    global FFMPEG, FFPROBE, FFMPEG_DOWNLOAD_URL
    if args.ffmpeg:
        FFMPEG = args.ffmpeg
    if args.ffprobe:
        FFPROBE = args.ffprobe
    if args.ffmpeg_url is not None:
        FFMPEG_DOWNLOAD_URL = args.ffmpeg_url

    ff, fp = ensure_tools(args.directory, allow_download=not args.no_download,
                          want_download=True)
    if not ff or not fp:
        raise SystemExit(
            "Nenašel jsem ffmpeg/ffprobe. Přidej je do PATH, dej do '.ffmpeg' "
            "vedle skriptu, zadej --ffmpeg/--ffprobe, nebo nech stáhnout "
            "(nepoužívej --no-download)."
        )
    FFMPEG, FFPROBE = ff, fp
    print(f"ffmpeg:  {ff}\nffprobe: {fp}\n")

    lang_filter = None
    if args.lang:
        lang_filter = {x.strip().lower() for x in args.lang.split(",") if x.strip()}

    videos = collect(args.directory, args.recursive)
    print(f"Nalezeno videí: {len(videos)}\n")

    all_jobs = []   # (video, [job tuples])
    total_do = 0
    for v in sorted(videos):
        subs = probe_subs(v)
        if not subs:
            print(f"# {os.path.basename(v)}\n    (žádné titulkové stopy)")
            continue
        jobs = build_jobs(v, subs, lang_filter, args.include_image, args.overwrite)
        print(f"# {os.path.basename(v)}")
        for kind, sub, out, cmd, desc in jobs:
            mark = {"do": "+", "skip": "-", "exists": "="}.get(kind, " ")
            print(f"    {mark} {desc}")
            if kind == "do":
                total_do += 1
        all_jobs.append((v, jobs))
        print()

    if total_do == 0:
        print("Nic k extrakci.")
        return

    if not args.apply:
        print(f"[NÁHLED] {total_do} titulků k extrakci. Přidej --apply pro provedení.")
        return

    ok = 0
    for v, jobs in all_jobs:
        for kind, sub, out, cmd, desc in jobs:
            if kind != "do":
                continue
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0 or not os.path.exists(out) or \
                    os.path.getsize(out) == 0:
                print(f"   CHYBA: {os.path.basename(out)}")
                for l in [l for l in (res.stderr or "").splitlines()
                          if l.strip()][-6:]:
                    print("   | " + l)
                if os.path.exists(out) and os.path.getsize(out) == 0:
                    try:
                        os.remove(out)
                    except OSError:
                        pass
                continue
            ok += 1
            print(f"OK: {os.path.basename(out)}")
    print(f"\nHotovo: {ok}/{total_do} titulků.")


if __name__ == "__main__":
    main()
