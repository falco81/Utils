#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Import (mux) titulků do videa pomocí ffmpeg.

Najde v adresáři videa (.mkv, .mp4) a titulky (.srt), spáruje je podle
kódu epizody SxxExx a každý titulek vloží do odpovídajícího videa jako
soft-subtitle stopu. Zároveň:
  - nastaví jazyk stopy (language) podle koncovky názvu titulku (en, cs, ...),
  - nastaví název stopy (title / "name"),
  - volitelně (--default) označí stopu jako výchozí,
  - příznak forced rozpozná z názvu (...forced.srt) a nastaví ho.

Stopy se kopírují BEZ překódování videa/zvuku (-c copy), takže to je rychlé.
ffmpeg ale neumí editovat na místě – vždy zapíše nový soubor.

POZOR k formátu: MKV zvládá flagy default/forced i názvy stop spolehlivě.
U MP4 jsou titulky (mov_text) podporovány hůř napříč přehrávači – proto je
volba --mkv (výstup vždy jako .mkv) doporučená, i pro vstupní .mp4.

Použití:
    python mux_subs.py "D:\\serial"                 # jen NÁHLED příkazů
    python mux_subs.py "D:\\serial" --apply          # provede mux
    python mux_subs.py "D:\\serial" --apply --default # titulek jako výchozí
    python mux_subs.py "D:\\serial" --apply --mkv     # výstup vždy .mkv
    python mux_subs.py "D:\\serial" --apply --replace # přepíše originál
    python mux_subs.py -r --apply                     # i podadresáře

Výchozí chování zapisuje výstup do podsložky "muxed" a originály nechává být.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys

VIDEO_EXTS = {".mkv", ".mp4"}
SUB_EXT = ".srt"

FLAG_TOKENS = {"forced", "sdh", "cc", "hi", "foreign", "full", "default"}
EPISODE_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,2})")
LANG_RE = re.compile(r"^[A-Za-z]{2,3}$")

# Mapování 2-písmenného kódu -> 3-písmenný (ISO 639-2) pro metadata ffmpeg
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
# Lidský název stopy podle 3-písmenného kódu
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


def episode_key(name):
    m = EPISODE_RE.search(name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def fmt_key(k):
    return f"S{k[0]:02d}E{k[1]:02d}"


# Cesty / konfigurace k binárkám (lze přepsat parametrem --ffmpeg / --ffprobe).
# Hodnota smí být: přímá cesta k exe, SLOŽKA, která ho obsahuje (i v bin/),
# nebo holý název "ffmpeg" hledaný v PATH.
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
# Když se ffmpeg nikde nenajde, stáhne se z této adresy (zip/tar.*) a rozbalí
# do ./.ffmpeg/ vedle skriptu. Prázdný řetězec = nestahovat.
# (Stejná logika i URL jako v patreon downloaderu – sdílí tutéž .ffmpeg složku.)
FFMPEG_DOWNLOAD_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _exe(name):
    return name + ".exe" if os.name == "nt" else name


def _resolve_tool(value, name):
    """Hodnotu z konfigurace převede na spustitelnou cestu.

    Přijímá cestu k exe, SLOŽKU s exe (i v bin/), nebo holý název přes PATH.
    Vrací None, pokud zadaná složka exe neobsahuje."""
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
    return shutil.which(value) or shutil.which(exe)  # holý název přes PATH


def _try_exe(path):
    """Ověří, že daná binárka opravdu jde spustit (`-version`)."""
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
    """Najde exe rekurzivně v zadaných složkách (typicky rozbalená .ffmpeg)."""
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
    """Stáhne a rozbalí ffmpeg do ./.ffmpeg/ (jen stdlib, přes urllib)."""
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
    """Zajistí funkční ffmpeg + ffprobe. Vrátí (ffmpeg, ffprobe), může být None.

    Pořadí: konfigurace/PATH -> cache .ffmpeg -> (volitelně) stažení.
    ffprobe se hledá přednostně vedle ffmpegu (je ve stejném buildu).
    """
    global FFMPEG, FFPROBE
    search = [
        _cache_dir(),
        os.path.join(target_dir, ".ffmpeg"),
        target_dir,
        os.path.join(os.getcwd(), ".ffmpeg"),
        os.getcwd(),
    ]

    # --- ffmpeg ---
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

    # --- ffprobe (nejdřív vedle ffmpegu) ---
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


def parse_sub_meta(sub_name, forced_lang=None):
    """Z názvu titulku vytáhne (lang3, název_stopy, je_forced).

    Jazyk se hledá jako 2–3 písmenný token na konci (před .srt),
    např. en -> eng. forced/sdh apod. se berou jako příznaky.
    """
    stem = sub_name[: -len(SUB_EXT)]
    parts = stem.split(".")
    lang = forced_lang
    forced = False
    sdh = False
    # projdeme max 2 koncové tokeny
    tail = []
    while parts and len(tail) < 2 and (
        parts[-1].lower() in FLAG_TOKENS or LANG_RE.match(parts[-1])
    ):
        tail.insert(0, parts.pop())
    for tok in tail:
        low = tok.lower()
        if low in ("forced",):
            forced = True
        elif low in ("sdh", "cc", "hi"):
            sdh = True
        elif LANG_RE.match(tok) and lang is None:
            lang = low
    # normalizace jazyka na 3 písmena
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


def count_sub_streams(video):
    if not FFPROBE:
        return 0
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "s",
             "-show_entries", "stream=index", "-of", "csv=p=0", video],
            capture_output=True, text=True, check=True,
        ).stdout
        return len([ln for ln in out.splitlines() if ln.strip()])
    except Exception:
        return 0


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
            elif ext == SUB_EXT:
                subs.append(full)
    return videos, subs


def build_command(video, sub_list, out_path, set_default,
                  default_lang, name_override, forced_lang=None):
    """Sestaví ffmpeg příkaz pro jedno video + jeho titulky."""
    out_ext = os.path.splitext(out_path)[1].lower()
    n_existing = count_sub_streams(video)

    cmd = [FFMPEG, "-y", "-i", video]
    for s in sub_list:
        cmd += ["-i", s]

    if out_ext == ".mp4":
        # do MP4 bereme jen video/audio/titulky – datové a timecode stopy
        # mov_text v MP4 často shodí ("Conversion failed!")
        cmd += ["-map", "0:v?", "-map", "0:a?", "-map", "0:s?"]
    else:
        cmd += ["-map", "0"]
    for i in range(len(sub_list)):
        cmd += ["-map", str(i + 1)]

    # kodeky: video/audio kopírovat; titulky podle kontejneru
    if out_ext == ".mp4":
        cmd += ["-c", "copy", "-c:s", "mov_text"]
    else:  # .mkv
        cmd += ["-c", "copy"]

    # metadata jazyka + názvu pro každou novou stopu
    metas = []
    for s in sub_list:
        lang3, name, forced = parse_sub_meta(os.path.basename(s), forced_lang)
        if name_override:
            name = name_override
        metas.append((lang3, name, forced))

    for i, (lang3, name, _forced) in enumerate(metas):
        idx = n_existing + i
        cmd += [f"-metadata:s:s:{idx}", f"language={lang3}"]
        cmd += [f"-metadata:s:s:{idx}", f"title={name}"]

    # výběr stopy, která má být default
    chosen = None
    if set_default and metas:
        if default_lang:
            dl = LANG3.get(default_lang.lower(), default_lang.lower())
            for i, (lang3, _n, _f) in enumerate(metas):
                if lang3 == dl:
                    chosen = i
                    break
        if chosen is None:
            chosen = 0

    # vyčistit stávající default u titulků (chceme jen jeden default)
    if chosen is not None:
        for j in range(n_existing):
            cmd += [f"-disposition:s:{j}", "0"]

    # disposition pro nové stopy (default + forced)
    for i, (_l, _n, forced) in enumerate(metas):
        idx = n_existing + i
        flags = []
        if chosen is not None and i == chosen:
            flags.append("default")
        if forced:
            flags.append("forced")
        if flags:
            cmd += [f"-disposition:s:{idx}", "+".join(flags)]

    cmd += [out_path]
    return cmd


def main():
    p = argparse.ArgumentParser(
        description="Naimportuje (muxne) titulky do videa pomocí ffmpeg.")
    p.add_argument("directory", nargs="?", default=".",
                   help="Adresář (výchozí: aktuální).")
    p.add_argument("-r", "--recursive", action="store_true",
                   help="Projít i podadresáře.")
    p.add_argument("--apply", action="store_true",
                   help="Skutečně spustit (bez něj jen vypíše příkazy).")
    p.add_argument("--default", action="store_true",
                   help="Naimportovaný titulek označit jako výchozí.")
    p.add_argument("--default-lang", metavar="KOD",
                   help="Při více titulcích vybrat výchozí podle jazyka "
                        "(např. en, cs).")
    p.add_argument("--mkv", action="store_true",
                   help="Výstup vždy jako .mkv (doporučeno i pro vstup .mp4).")
    p.add_argument("--replace", action="store_true",
                   help="Přepsat originál (jinak zapíše do podsložky 'muxed').")
    p.add_argument("--name", metavar="NAZEV",
                   help="Vlastní název stopy místo automatického.")
    p.add_argument("--lang", metavar="KOD",
                   help="Vynutit jazyk, když ho v názvu titulku není.")
    p.add_argument("--ffmpeg", metavar="CESTA",
                   help="Cesta k ffmpeg.exe (jinak se hledá v PATH a .ffmpeg).")
    p.add_argument("--ffprobe", metavar="CESTA",
                   help="Cesta k ffprobe.exe (jinak se hledá u ffmpegu/PATH/.ffmpeg).")
    p.add_argument("--ffmpeg-url", metavar="URL", default=None,
                   help="URL archivu ffmpeg (zip/tar.*) pro auto-stažení, "
                        "když chybí. Prázdný řetězec stahování vypne.")
    p.add_argument("--no-download", action="store_true",
                   help="Nestahovat ffmpeg, i když chybí.")
    args = p.parse_args()

    if not os.path.isdir(args.directory):
        raise SystemExit(f"Není adresář: {args.directory}")

    # najít / zajistit ffmpeg + ffprobe (stejná logika jako patreon downloader)
    global FFMPEG, FFPROBE, FFMPEG_DOWNLOAD_URL
    if args.ffmpeg:
        FFMPEG = args.ffmpeg
    if args.ffprobe:
        FFPROBE = args.ffprobe
    if args.ffmpeg_url is not None:
        FFMPEG_DOWNLOAD_URL = args.ffmpeg_url

    ff, fp = ensure_tools(args.directory, allow_download=not args.no_download,
                          want_download=args.apply)
    if args.apply and not ff:
        raise SystemExit(
            "Nenašel jsem ffmpeg. Možnosti:\n"
            "  - přidej ffmpeg do PATH,\n"
            "  - dej ho do podsložky '.ffmpeg' vedle skriptu,\n"
            "  - zadej cestu: --ffmpeg \"C:\\cesta\\ffmpeg.exe\" (smí být i složka),\n"
            "  - nech ho stáhnout (výchozí) – nepoužívej --no-download."
        )
    FFMPEG = ff or "ffmpeg"
    FFPROBE = fp or "ffprobe"
    if ff:
        print(f"ffmpeg:  {ff}")
    if fp:
        print(f"ffprobe: {fp}")
    elif args.apply:
        print("ffprobe: nenalezen (předpokládám 0 stávajících titulkových stop).")
    print()

    videos, subs = collect(args.directory, args.recursive)
    print(f"Nalezeno videí: {len(videos)}, titulků: {len(subs)}\n")

    # video mapa podle epizody
    vmap = {}
    for v in sorted(videos):
        k = episode_key(os.path.basename(v))
        if k is None:
            continue
        vmap.setdefault(k, v)

    # spárování titulků k videím
    pairs = {}  # video -> [titulky]
    for s in sorted(subs):
        k = episode_key(os.path.basename(s))
        if k is None:
            print(f"-  Přeskakuji (chybí SxxExx): {os.path.basename(s)}")
            continue
        v = vmap.get(k)
        if v is None:
            print(f"-  Bez videa pro {fmt_key(k)}: {os.path.basename(s)}")
            continue
        pairs.setdefault(v, []).append(s)

    if not pairs:
        print("\nNic ke zpracování.")
        return

    jobs = []
    for v in sorted(pairs):
        sub_list = pairs[v]
        vdir = os.path.dirname(v)
        vstem = os.path.splitext(os.path.basename(v))[0]
        out_ext = ".mkv" if args.mkv else os.path.splitext(v)[1].lower()

        if args.replace:
            final = os.path.join(vdir, vstem + out_ext)
            tmp = os.path.join(vdir, vstem + ".muxtmp" + out_ext)
            out_path = tmp
        else:
            out_dir = os.path.join(vdir, "muxed")
            final = os.path.join(out_dir, vstem + out_ext)
            out_path = final

        # při vynuceném jazyce (--lang) ho předáme jako fallback
        cmd = build_command(v, sub_list, out_path, args.default,
                            args.default_lang, args.name, args.lang)
        jobs.append((v, sub_list, cmd, out_path, final))

    print(f"Naplánováno videí ke zpracování: {len(jobs)}\n")
    for v, sub_list, cmd, out_path, final in jobs:
        print(f"# {os.path.basename(v)}")
        for s in sub_list:
            print(f"    + {os.path.basename(s)}")
        print("    " + " ".join(_shq(c) for c in cmd))
        print()

    if not args.apply:
        print("[NÁHLED] Nic se nespustilo. Přidej --apply pro provedení.")
        return

    ok = 0
    mp4_failures = 0
    for v, sub_list, cmd, out_path, final in jobs:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        print(f">> {os.path.basename(final)}")
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            print("   CHYBA ffmpeg (konec výpisu):")
            lines = [l for l in (res.stderr or "").splitlines() if l.strip()]
            for l in lines[-12:]:
                print("   | " + l)
            if not lines:
                print("   | (žádný text chyby)")
            if out_path.lower().endswith(".mp4"):
                mp4_failures += 1
            # uklidit nedokončený temp
            if args.replace and os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass
            continue
        if args.replace:
            # smazat originál a přejmenovat temp na finální název
            try:
                if os.path.abspath(v) != os.path.abspath(final) and \
                        os.path.exists(v):
                    os.remove(v)
                os.replace(out_path, final)
            except OSError as e:
                print(f"   CHYBA při náhradě originálu: {e}")
                continue
        ok += 1
        print("   OK")
    print(f"\nHotovo: {ok}/{len(jobs)} videí.")
    if mp4_failures:
        print(f"\n[TIP] {mp4_failures}x selhalo při zápisu do MP4 (titulky mov_text "
              "jsou na některých verzích ffmpegu problematické).\n"
              "      Spusť to samé s přepínačem --mkv – výstup bude .mkv, kde se "
              "titulky (i název stopy a default/forced) zapíšou spolehlivě:\n"
              "      mux_subs.py --default --default-lang en --apply --mkv")


def _shq(s):
    """Jednoduché uvozovkování pro čitelný výpis příkazu."""
    if s and all(c.isalnum() or c in "-_.:=/+" for c in s):
        return s
    return '"' + s.replace('"', '\\"') + '"'


if __name__ == "__main__":
    main()
