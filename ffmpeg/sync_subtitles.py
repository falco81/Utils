#!/usr/bin/env python3
"""
sync_subtitles.py  (verze pro Windows 10 CLI, bez alass)
==========================================================

Opraví posunuté časování titulků (např. českých) podle správně časovaných
titulků vložených v MKV souboru (např. anglických) - i když jsou v jiném
jazyce a mají jinak rozdělené řádky (profesionální překlad).

Podpora kontejnerů (MKV vs MP4)
--------------------------------
- .mkv / .webm  -> titulky se extrahují přes mkvextract (přesné, beze ztráty).
- .mp4 / .m4v / .mov / ostatní -> mkvextract na ně neumí sáhnout (jen Matroska),
  takže titulky i v tomto případě vytáhne ffmpeg (převod mov_text -> srt).
  Pro MP4 je tedy ffmpeg potřeba VŽDY (i s --audio-mode off), zatímco u MKV
  jen pokud zapneš --audio-mode replace/combine.
Zvuková stopa (VAD) se vždy extrahuje přes ffmpeg, bez ohledu na kontejner.

Použité nástroje
----------------
- mkvtoolnix (mkvmerge + mkvextract) -> pro VYTAŽENÍ titulkové stopy
                        z MKV (text formáty jako SRT/ASS umí mkvextract
                        vytáhnout 1:1, beze ztráty).
- ffmpeg (VOLITELNĚ, jen pro --audio-mode replace/combine) -> dekódování
                        zvukové stopy (AC-3/AAC/DTS/...) na čisté PCM,
                        protože to v Pythonu bez externí binárky nejde.
- numpy              -> pip balíček, pro FFT korelaci a VAD (detekci řeči).
- colorama (VOLITELNĚ) -> barevný výstup na Windows CLI. Bez něj skript
                        funguje stejně, jen bez barev (žádný pád).
- ZBYTEK (parsování SRT, hledání posunu, dopočet časování, detekce řeči)
  je čistý Python napsaný v tomto skriptu - ŽÁDNÝ alass.

Instalace na Windows 10
------------------------
1) Python 3.9+  (https://www.python.org/downloads/)
2) pip install numpy colorama
3) MKVToolNix - NEMUSÍŠ řešit ručně. Skript hledá v tomto pořadí (stažení
   je AŽ POSLEDNÍ MOŽNOST):
     1. PATH / --mkvmerge,--mkvextract
     2. typické instalační cesty (C:\\Program Files\\MKVToolNix apod.)
     3. adresář videa, aktuální adresář, adresář skriptu, cache .mkvtoolnix
     4. teprve když nic z výše uvedeného nenajde: stáhne aktuální portable
        verzi (.7z) z mkvtoolnix.download a rozbalí ji do .mkvtoolnix
        vedle skriptu.
   Rozbalení .7z potřebuje navíc buď balíček `py7zr` (pip install py7zr,
   doporučeno - čistě přes pip), nebo externí 7z/7za v PATH. Bez jednoho
   z těch dvou se stažený archiv nerozbalí - v tom případě nainstaluj
   MKVToolNix klasicky instalátorem.
   Vypnout auto-stažení lze přepínačem --no-mkvtoolnix-download, nebo
   zadat vlastní cestu přes --mkvmerge / --mkvextract.
4) ffmpeg (jen pro --audio-mode replace/combine) - NEMUSÍŠ řešit ručně:
   pokud ho skript nenajde v PATH ani v cache složce ".ffmpeg" vedle sebe,
   automaticky si ho stáhne z gyan.dev a rozbalí do ".ffmpeg\" sám.
   Vypnout auto-stažení lze přepínačem --no-ffmpeg-download, nebo zadat
   vlastní cestu přes --ffmpeg.

Jak algoritmus funguje
-----------------------
1. Získá se referenční časová osa - podle volby --audio-mode:
   - "off" (default): referenční SRT vytažený z MKV (titulková stopa).
   - "replace": ZVUKOVÁ stopa z MKV/MP4 - detekcí řeči (VAD) se najdou
     úseky, kdy někdo mluví, a ty se použijí jako referenční "kotvy".
     Nepotřebuje žádné referenční titulky.
   - "combine": obojí současně - titulkové kotvy i řečové úseky se
     sloučí do jedné referenční osy pro maximální robustnost a přesnost.
2. Referenční osa a opravované titulky se převedou na binární "signál"
   v čase (kdy se "něco děje" - titulek/řeč - a kdy ne).
3. Pomocí křížové korelace (FFT) se najde nejlepší celkový časový posun
   mezi oběma signály - to zvládne i velké počáteční rozjetí.
4. Kolem tohoto hrubého posunu se k jednotlivým kotvám z referenční
   osy dohledají nejbližší titulky z opravované sady a z těchto dvojic
   se spočítá přesná lineární transformace (posun + změna rychlosti/FPS),
   robustně - odlehlé/nespárované dvojice se postupně vyřazují.
5. Tato transformace (a*t + b) se použije na VŠECHNY časy v opravovaných
   titulcích a uloží se výsledný .srt. Text titulků se nijak nemění,
   upravuje se POUZE časování.

Detekce řeči (VAD) je jednoduchá energetická metoda (RMS hlasitost po
30ms rámcích, adaptivní prahování přes percentil) - není to ML model
jako u nástrojů typu ffsubsync, ale na hrubé/jemné dosazení časování
podle dialogů to obvykle funguje dobře. Tichá hudba/efekty bez dialogu
mohou výjimečně VAD zmást - proto je k dispozici i kombinovaný režim.

Dvě metody dopočtu časování (přepínač --method)
-----------------------------------------------
- "affine" = výše popsaný postup: JEDEN globální vztah nový_čas = a*čas + b
  (posun + případná změna rychlosti/FPS). Jazykově nezávislý (jen časování),
  funguje i mezi různými jazyky i jen ze zvuku (VAD). Neumí ale opravit
  ROZSYNC PO ČÁSTECH (když různé úseky epizody potřebují různý posun).
- "warp" = obsahová synchronizace PO VĚTÁCH. Spáruje konkrétní české/cizí
  věty mezi opravovanými a referenčními titulky (podle textu - znakové
  3-gramy + sdílená jména/čísla), z jistých dvojic ("kotev") postaví po
  ČÁSTECH lineární časovou mapu a podle ní přesune každý titulek. Tím
  spraví i blokový/postupný rozsync a jemné ujíždění v jednotlivých
  scénách. Vyžaduje TEXTOVOU referenční stopu (ze zvuku samotného ne).
- "auto" (default) = "combo", když je k dispozici textová referenční stopa a
  najde se dost spolehlivých kotev; jinak se bezpečně vrátí k "affine".
- "combo" = afinní předsrovnání + warp doladění po větách. Nejrobustnější:
  afinní fáze srovná global/rychlost (i když je textových kotev málo), warp
  pak dolaďuje po částech tam, kde má jisté kotvy. Doporučené pro přesnost.

Různé jazyky (--translate)
--------------------------
Různé jazyky (--translate) + detekce jazyka z OBSAHU
----------------------------------------------------
Skript umí REÁLNĚ detekovat jazyk z obsahu titulků (ne podle přípon/tagů;
přes 'langdetect', pokud je nainstalován, jinak vestavěným detektorem pro
cs/sk/pl/en/de/fr/es/it/pt/nl/ru/uk/hu...). V interaktivních režimech --auto
a --auto-all proto sám pozná, jestli jsou opravované a referenční titulky v
jiných jazycích, a teprve pak nabídne překlad.

Metoda "warp" porovnává TEXT, takže nejlíp funguje, když jsou obojí titulky
ve stejném jazyce (dva překlady téhož). Když jsou v RŮZNÝCH jazycích, zapni
--translate google (online) nebo --translate argos (offline): obě strany se
JEN PRO PÁROVÁNÍ přeloží do společného jazyka (--pivot-lang, default 'en') a
teprve překlady se porovnávají. Výstupní text se nepřekládá. Překlady se
kešují na disk. Bez --translate se různé jazyky bezpečně dopočítají afinní
metodou (která je jazykově nezávislá, jen neumí rozsync po částech).

Obě metody mění POUZE časování, text titulků se nikdy nemění.

Použití
-------
    python sync_subtitles.py --list-tracks video.mkv

    python sync_subtitles.py video.mkv titulky_cz.srt vystup_cz_synced.srt

    python sync_subtitles.py video.mkv titulky_cz.srt vystup.srt --ref-lang eng

    python sync_subtitles.py video.mkv titulky_cz.srt vystup.srt --track-id 2

    # synchronizace jen podle zvukové stopy (detekce řeči), bez titulkové reference
    python sync_subtitles.py video.mkv titulky_cz.srt vystup.srt --audio-mode replace

    # kombinace titulkové reference + zvukové analýzy pro max. přesnost
    python sync_subtitles.py video.mkv titulky_cz.srt vystup.srt --audio-mode combine --audio-lang cze
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

try:
    import numpy as np
except ImportError:
    print("[CHYBA] Chybí balíček numpy. Nainstaluj ho: pip install numpy", file=sys.stderr)
    sys.exit(1)

try:
    import colorama
    from colorama import Fore, Style
    colorama.init(autoreset=True)
except ImportError:
    class _NoColor:
        def __getattr__(self, _name):
            return ""
    Fore = _NoColor()
    Style = _NoColor()


# ----------------------------------------------------------------------
# Pomocné funkce / barevný výstup (Windows CLI friendly přes colorama)
# ----------------------------------------------------------------------

def log_info(msg: str):
    print(f"{Fore.CYAN}[INFO]{Style.RESET_ALL} {msg}")


def log_warn(msg: str):
    print(f"{Fore.YELLOW}[VAROVÁNÍ]{Style.RESET_ALL} {msg}")


def log_done(msg: str):
    print(f"{Fore.GREEN}[HOTOVO]{Style.RESET_ALL} {msg}")


def die(msg: str, code: int = 1):
    print(f"{Fore.RED}[CHYBA]{Style.RESET_ALL} {msg}", file=sys.stderr)
    sys.exit(code)


def find_tool(names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


# ----------------------------------------------------------------------
# Práce s MKV přes mkvmerge/mkvextract
# ----------------------------------------------------------------------

def mkvmerge_tracks(mkvmerge_bin: str, mkv_path: Path, track_type: str):
    """track_type: 'subtitles' nebo 'audio'."""
    cmd = [mkvmerge_bin, "-J", str(mkv_path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        die(f"mkvmerge selhalo: {e.stderr}")
    data = json.loads(result.stdout)

    out = []
    for track in data.get("tracks", []):
        if track.get("type") == track_type:
            props = track.get("properties", {})
            out.append({
                "id": track["id"],                       # ID stopy pro mkvextract
                "codec": track.get("codec", "?"),
                "lang": props.get("language", "und"),
                "title": props.get("track_name", ""),
            })
    return out


TEXT_CODEC_KEYWORDS = ("SUBRIP", "SRT", "ASS", "SUBSTATIONALPHA", "SSA", "WEBVTT", "USF", "TIMEDTEXT", "MOV_TEXT", "MOVTEXT")


def is_text_codec(codec: str) -> bool:
    c = codec.upper().replace(" ", "")
    return any(k in c for k in TEXT_CODEC_KEYWORDS)


def pick_reference_track(subs, ref_lang, track_id):
    if not subs:
        die("V MKV souboru nebyla nalezena žádná titulková stopa.")

    if track_id is not None:
        for t in subs:
            if t["id"] == track_id:
                return t
        die(f"Stopa s ID {track_id} nebyla nalezena. Použij --list-tracks.")

    candidates = subs
    if ref_lang:
        matches = [t for t in subs if t["lang"].lower().startswith(ref_lang.lower())]
        if matches:
            candidates = matches
        else:
            log_warn(f"Stopa s jazykem '{ref_lang}' nenalezena, zkouším automatický výběr.")

    text_tracks = [t for t in candidates if is_text_codec(t["codec"])]
    if text_tracks:
        return text_tracks[0]

    any_text = [t for t in subs if is_text_codec(t["codec"])]
    if any_text:
        return any_text[0]

    die(
        "Nalezeny jen obrázkové titulky (např. PGS/VobSub) - ty nelze vytáhnout "
        "jako text. Potřebuješ textovou stopu (SRT/ASS) jako referenci."
    )


def pick_audio_track(audio_tracks, audio_lang, audio_track_id):
    if not audio_tracks:
        die("V MKV/MP4 souboru nebyla nalezena žádná zvuková stopa.")

    if audio_track_id is not None:
        for t in audio_tracks:
            if t["id"] == audio_track_id:
                return t
        die(f"Audio stopa s ID {audio_track_id} nebyla nalezena. Použij --list-tracks.")

    if audio_lang:
        matches = [t for t in audio_tracks if t["lang"].lower().startswith(audio_lang.lower())]
        if matches:
            return matches[0]
        log_warn(f"Audio stopa s jazykem '{audio_lang}' nenalezena, použiji první dostupnou.")

    return audio_tracks[0]


MKVEXTRACT_CONTAINER_EXTS = {".mkv", ".mka", ".webm"}


def extract_subtitle_to_srt(mkvextract_bin: str, mkv_path: Path, track_id: int, out_srt: Path):
    """Pro Matroska kontejnery (.mkv/.webm) - mkvextract umí extrahovat jen z těch."""
    cmd = [mkvextract_bin, "tracks", str(mkv_path), f"{track_id}:{out_srt}"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not out_srt.exists() or out_srt.stat().st_size == 0:
        die(f"mkvextract nedokázal vytáhnout titulkovou stopu {track_id}:\n{result.stderr[-2000:]}")


def extract_subtitle_via_ffmpeg(ffmpeg_bin: str, video_path: Path, sub_position: int, out_srt: Path):
    """Pro MP4/MOV apod. - mkvextract na ně nesahá, takže titulky (typicky mov_text)
    vytáhne a převede na SRT ffmpeg. sub_position = pořadí mezi titulkovými
    stopami (0 = první), odpovídá specifikátoru '0:s:N'."""
    cmd = [ffmpeg_bin, "-y", "-i", str(video_path), "-map", f"0:s:{sub_position}", str(out_srt)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not out_srt.exists() or out_srt.stat().st_size == 0:
        die(f"ffmpeg nedokázal vytáhnout titulkovou stopu:\n{result.stderr[-2000:]}")


# ----------------------------------------------------------------------
# ffmpeg toolkit - PATH / cache ".ffmpeg" vedle skriptu / automatické stažení
# (stejný ověřený mechanismus jako v patreon downloaderu / mux_subs.py)
# ----------------------------------------------------------------------

FFMPEG = "ffmpeg"
FFMPEG_DOWNLOAD_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _exe(name):
    return name + ".exe" if os.name == "nt" else name


def _resolve_tool(value, name):
    """Hodnota smí být: přímá cesta k exe, SLOŽKA s exe (i v bin/), nebo holý
    název hledaný v PATH. Vrací None, pokud nic nenajde."""
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
    """ffmpeg chce '-version' (jedna pomlčka), mkvmerge/mkvextract chtějí
    '--version' (dvě pomlčky, GNU styl) - zkusíme obě varianty."""
    if not path:
        return False
    for flag in ("--version", "-version"):
        try:
            subprocess.run([path, flag], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, check=True)
            return True
        except Exception:
            continue
    return False


def _cache_dir(name=".ffmpeg"):
    argv0 = sys.argv[0] if sys.argv and sys.argv[0] else None
    base = os.path.dirname(os.path.abspath(argv0)) if argv0 else os.getcwd()
    return os.path.join(base, name)


def _find_cached(name, search_dirs, max_depth=None):
    """max_depth omezuje, jak hluboko se chodí (kvůli prohledávání širokých
    adresářů typu Program Files / adresář videa - bez limitu by to mohlo
    procházet celé obrovské stromy). None = bez omezení (cache složky)."""
    exe = _exe(name)
    for d in search_dirs:
        if not d or not os.path.isdir(d):
            continue
        base_depth = os.path.abspath(d).rstrip(os.sep).count(os.sep)
        for root, dirs, files in os.walk(d):
            if max_depth is not None:
                depth = os.path.abspath(root).rstrip(os.sep).count(os.sep) - base_depth
                if depth >= max_depth:
                    dirs[:] = []
                    continue
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
        raise ValueError("Neznámý formát archivu (čekal jsem .zip/.tar.*).")


def _extract_7z(path, dest):
    """Rozbalí .7z. MKVToolNix portable se distribuuje jen jako .7z, ne .zip,
    takže na rozdíl od ffmpeg archivu to potřebuje navíc buď balíček py7zr,
    nebo externí 7z/7za binárku (pokud je v PATH)."""
    try:
        import py7zr
        with py7zr.SevenZipFile(path, mode="r") as z:
            z.extractall(path=dest)
        return True
    except ImportError:
        pass
    except Exception as e:
        log_warn(f"Rozbalení .7z přes py7zr selhalo: {e}")

    seven_zip = find_tool(["7z", "7z.exe", "7za", "7za.exe"])
    if seven_zip:
        result = subprocess.run([seven_zip, "x", f"-o{dest}", "-y", path],
                                 capture_output=True, text=True)
        if result.returncode == 0:
            return True
        log_warn(f"Rozbalení .7z přes {seven_zip} selhalo: {result.stderr[-500:]}")
    return False


def _download_to_file(url, dest_path, label):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req) as r, open(dest_path, "wb") as f:
        total = int(r.headers.get("Content-Length", 0) or 0)
        done = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {Fore.CYAN}{done * 100 // total:3d}%{Style.RESET_ALL}  "
                      f"{label}: {done // 1048576} / {total // 1048576} MB", end="")
        print()


def _download_ffmpeg(url):
    cache = _cache_dir(".ffmpeg")
    os.makedirs(cache, exist_ok=True)
    log_info(f"ffmpeg nenalezen; stahuji z {url}")
    tmp = os.path.join(cache, "ffmpeg_download.tmp")
    _download_to_file(url, tmp, "ffmpeg")
    log_info("Rozbaluji ffmpeg ...")
    _extract_archive(tmp, cache, url)
    try:
        os.remove(tmp)
    except OSError:
        pass


def ensure_ffmpeg(target_dir, allow_download):
    """Najde ffmpeg: PATH -> --ffmpeg/FFMPEG override -> cache .ffmpeg (bez
    omezení hloubky) -> adresář videa/cwd (omezená hloubka, ať to neprochází
    celé obrovské stromy) -> (až jako poslední možnost, pokud povoleno)
    stáhne a rozbalí z gyan.dev."""
    cache_dirs = [_cache_dir(".ffmpeg"), os.path.join(target_dir, ".ffmpeg"),
                  os.path.join(os.getcwd(), ".ffmpeg")]
    broad_dirs = [target_dir, os.getcwd()]
    ff = _resolve_tool(FFMPEG, "ffmpeg")
    if not _try_ff(ff):
        ff = _find_cached("ffmpeg", cache_dirs)
        if not _try_ff(ff):
            ff = _find_cached("ffmpeg", broad_dirs, max_depth=3)
        if not _try_ff(ff):
            ff = None
    if ff is None and allow_download and FFMPEG_DOWNLOAD_URL:
        try:
            _download_ffmpeg(FFMPEG_DOWNLOAD_URL)
        except Exception as e:
            log_warn(f"Stažení ffmpeg selhalo: {e}")
        ff = _find_cached("ffmpeg", cache_dirs)
        if not _try_ff(ff):
            ff = None
    return ff


# ----------------------------------------------------------------------
# mkvtoolnix toolkit - stejný princip jako ffmpeg výše, navíc s o trochu
# složitější logikou: portable balíček je .7z (ne .zip) a stahovací URL
# obsahuje číslo verze, takže se musí nejdřív vyčíst ze stránky downloadů.
# ----------------------------------------------------------------------

MKVMERGE = "mkvmerge"
MKVEXTRACT = "mkvextract"
MKVTOOLNIX_DOWNLOAD_PAGE = "https://mkvtoolnix.download/downloads.html"


def _resolve_mkvtoolnix_url():
    """Stránka downloadů obsahuje verzované odkazy (např. .../99.0/mkvtoolnix-
    -64-bit-99.0.7z) - není tam fixní 'latest' URL, takže si to musíme
    nejdřív z HTML vyčíst."""
    import urllib.request
    req = urllib.request.Request(MKVTOOLNIX_DOWNLOAD_PAGE, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as r:
        html = r.read().decode("utf-8", errors="ignore")

    m = re.search(r"https://mkvtoolnix\.download/windows/releases/[\d.]+/mkvtoolnix-64-bit-[\d.]+\.7z", html)
    if m:
        return m.group(0)

    m = re.search(r"windows/releases/([\d.]+)/mkvtoolnix-64-bit-([\d.]+)\.7z", html)
    if m:
        return f"https://mkvtoolnix.download/windows/releases/{m.group(1)}/mkvtoolnix-64-bit-{m.group(2)}.7z"

    return None


def _download_mkvtoolnix(url):
    cache = _cache_dir(".mkvtoolnix")
    os.makedirs(cache, exist_ok=True)
    log_info(f"mkvtoolnix nenalezen; stahuji portable verzi z {url}")
    tmp = os.path.join(cache, "mkvtoolnix_download.tmp")
    _download_to_file(url, tmp, "mkvtoolnix")
    log_info("Rozbaluji mkvtoolnix (.7z) ...")
    ok = _extract_7z(tmp, cache)
    try:
        os.remove(tmp)
    except OSError:
        pass
    if not ok:
        raise RuntimeError(
            "Nepodařilo se rozbalit .7z archiv. Nainstaluj balíček py7zr "
            "(pip install py7zr) a zkus to znovu, nebo si MKVToolNix "
            "stáhni/nainstaluj manuálně."
        )


MKV_PROGRAM_FILES_DIRS = [
    r"C:\Program Files\MKVToolNix",
    r"C:\Program Files (x86)\MKVToolNix",
]


def ensure_mkvtoolnix(target_dir, allow_download):
    """Najde mkvmerge+mkvextract, V TOMTO POŘADÍ (stažení je AŽ POSLEDNÍ MOŽNOST):
    1) PATH / --mkvmerge,--mkvextract override
    2) typické instalační cesty (Program Files\\MKVToolNix)
    3) adresář videa, aktuální adresář, adresář skriptu, cache .mkvtoolnix
       (kdyby tam zůstal z dřívějška)
    4) až když NIC z výše uvedeného nenajde a je to povoleno: stáhne portable
       .7z z mkvtoolnix.download a rozbalí do .mkvtoolnix vedle skriptu.
    Vrací (mkvmerge, mkvextract), kterákoliv položka může být None."""
    script_dir = os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv and sys.argv[0] else os.getcwd()
    cache = _cache_dir(".mkvtoolnix")

    def _try_path_override(value, name):
        p = _resolve_tool(value, name)
        return p if _try_ff(p) else None

    # 1) PATH / explicitní override
    mm = _try_path_override(MKVMERGE, "mkvmerge")
    me = _try_path_override(MKVEXTRACT, "mkvextract")

    # 2) + 3) širší prohledání adresářů - PŘED jakýmkoliv stahováním
    if mm is None or me is None:
        broad_dirs = MKV_PROGRAM_FILES_DIRS + [target_dir, os.getcwd(), script_dir, cache]
        if mm is None:
            mm = _find_cached("mkvmerge", broad_dirs, max_depth=3)
            mm = mm if _try_ff(mm) else None
        if me is None:
            me = _find_cached("mkvextract", broad_dirs, max_depth=3)
            me = me if _try_ff(me) else None

    # 4) teprve teď, jako poslední možnost, stažení
    if (mm is None or me is None) and allow_download:
        try:
            url = _resolve_mkvtoolnix_url()
            if not url:
                raise RuntimeError("Nenašel jsem odkaz na portable verzi na mkvtoolnix.download.")
            _download_mkvtoolnix(url)
        except Exception as e:
            log_warn(f"Stažení/rozbalení mkvtoolnix selhalo: {e}")
        if mm is None:
            mm = _find_cached("mkvmerge", [cache])
            mm = mm if _try_ff(mm) else None
        if me is None:
            me = _find_cached("mkvextract", [cache])
            me = me if _try_ff(me) else None

    return mm, me


def extract_audio_wav(ffmpeg_bin: str, mkv_path: Path, audio_position: int, out_wav: Path, sample_rate: int = 16000):
    """audio_position = pořadí zvukové stopy mezi audio stopami (0 = první audio stopa v souboru),
    odpovídá ffmpeg specifikátoru '0:a:N'."""
    cmd = [
        ffmpeg_bin, "-y", "-i", str(mkv_path),
        "-map", f"0:a:{audio_position}",
        "-ac", "1", "-ar", str(sample_rate),
        "-f", "wav", str(out_wav),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not out_wav.exists() or out_wav.stat().st_size == 0:
        die(f"ffmpeg nedokázal vytáhnout/dekódovat zvukovou stopu:\n{result.stderr[-2000:]}")


def read_wav_mono(path: Path):
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        n = wf.getnframes()
        raw = wf.readframes(n)
        sampwidth = wf.getsampwidth()
    if sampwidth != 2:
        die(f"Očekáván 16-bit WAV, nalezeno {sampwidth * 8}-bit (neočekávaný výstup ffmpeg).")
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return samples, sr


def detect_speech_events(samples: "np.ndarray", sr: int, frame_ms: float = 30.0,
                          energy_percentile: float = 55.0, min_speech_ms: float = 200.0,
                          max_gap_ms: float = 300.0):
    """
    Jednoduchá energetická VAD (detekce řeči):
    - rozdělí signál na rámce po frame_ms,
    - spočítá RMS hlasitost (v dB) každého rámce,
    - vše nad adaptivním prahem (percentil hlasitosti celé stopy) = "řeč",
    - krátké mezery mezi řečí se sloučí, příliš krátké/nahodilé úseky se zahodí.
    Vrací události ve stejném formátu jako titulky: {"start", "end", "text": ""}.
    """
    frame_len = max(1, int(sr * frame_ms / 1000.0))
    n_frames = len(samples) // frame_len
    if n_frames < 2:
        die("Zvuková stopa je příliš krátká nebo prázdná pro VAD analýzu.")

    frames = samples[: n_frames * frame_len].reshape(n_frames, frame_len)
    rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12)
    db = 20.0 * np.log10(rms + 1e-9)

    threshold = np.percentile(db, energy_percentile)
    active = db > threshold

    frame_dur = frame_len / sr
    raw_events = []
    i = 0
    while i < len(active):
        if active[i]:
            start = i
            while i < len(active) and active[i]:
                i += 1
            raw_events.append([start * frame_dur, i * frame_dur])
        else:
            i += 1

    max_gap = max_gap_ms / 1000.0
    merged = []
    for ev in raw_events:
        if merged and ev[0] - merged[-1][1] <= max_gap:
            merged[-1][1] = ev[1]
        else:
            merged.append(ev)

    min_dur = min_speech_ms / 1000.0
    merged = [m for m in merged if (m[1] - m[0]) >= min_dur]

    if len(merged) < 2:
        die(
            "VAD detekoval příliš málo úseků řeči pro spolehlivou synchronizaci. "
            "Zkus jiný --vad-percentile, jinou audio stopu, nebo použij titulkovou referenci."
        )

    return [{"start": s, "end": e, "text": ""} for s, e in merged]


# ----------------------------------------------------------------------
# Parsování / zápis SRT (čistý Python, žádná externí knihovna)
# ----------------------------------------------------------------------

TIME_RE = re.compile(r"(\d+):(\d{2}):(\d{2})[.,](\d{3})")
BLOCK_RE = re.compile(
    r"(\d+:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d+:\d{2}:\d{2}[.,]\d{3})"
)


def time_to_seconds(s: str) -> float:
    m = TIME_RE.match(s.strip())
    if not m:
        raise ValueError(f"Neplatný časový formát: {s}")
    h, mi, sec, ms = map(int, m.groups())
    return h * 3600 + mi * 60 + sec + ms / 1000.0


def seconds_to_srt_time(t: float) -> str:
    if t < 0:
        t = 0.0
    h = int(t // 3600)
    t -= h * 3600
    m = int(t // 60)
    t -= m * 60
    s = int(t)
    ms = int(round((t - s) * 1000))
    if ms == 1000:
        ms = 0
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _decode_subtitle_bytes(data):
    """Rozumně dekóduje titulky s neznámým kódováním: BOM, čisté UTF-8, UTF-16
    bez BOM, a pak podle vzoru vysokých bajtů rozliší evropské (cp1250) vs
    asijské (Big5/GBK/EUC-KR) kódování - u asijských použije detektor, když je."""
    if not data:
        return ""
    ts = re.compile(r"\d\d:\d\d:\d\d[,.]\d{3}")
    # 1) BOM
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", errors="replace")
    if data[:2] == b"\xff\xfe" or data[:2] == b"\xfe\xff":
        try:
            return data.decode("utf-16", errors="replace")
        except Exception:
            pass
    # 2) čisté UTF-8 s časovými značkami (nejčastější případ)
    try:
        t = data.decode("utf-8")
        if ts.search(t):
            return t
    except Exception:
        pass
    # 3) UTF-16 bez BOM (hodně nulových bajtů)
    if data.count(b"\x00") > len(data) // 4:
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                t = data.decode(enc)
                if ts.search(t):
                    return t
            except Exception:
                pass
    # 4) vysoké bajty: v běhu (CJK) vs izolované (evropské jednobajtové)
    hi = [i for i, b in enumerate(data) if b >= 0x80]
    cjk_like = False
    if hi:
        s = set(hi)
        adj = sum(1 for i in hi if (i - 1) in s or (i + 1) in s)
        cjk_like = (adj / len(hi)) > 0.6
    if cjk_like:
        for libname in ("charset_normalizer", "chardet"):
            try:
                lib = __import__(libname)
                if libname == "charset_normalizer":
                    b = lib.from_bytes(data).best()
                    if b is not None and ts.search(str(b)):
                        return str(b)
                else:
                    enc = (lib.detect(data) or {}).get("encoding")
                    if enc:
                        t = data.decode(enc, errors="replace")
                        if ts.search(t):
                            return t
            except Exception:
                pass
        order = ("gb18030", "big5", "euc-kr", "shift_jis", "cp1250", "cp1252", "latin-1")
    else:
        order = ("cp1250", "cp1252", "latin-1", "gb18030", "big5")
    best, best_score = None, -1
    for enc in order:
        try:
            txt = data.decode(enc)
        except Exception:
            continue
        score = len(ts.findall(txt))
        if score > best_score:
            best, best_score = txt, score
    if best is None or best_score <= 0:
        best = data.decode("utf-8", errors="replace")
    return best


def parse_srt(path: Path, strict=True):
    raw = _decode_subtitle_bytes(Path(path).read_bytes())
    blocks = re.split(r"\r?\n\r?\n+", raw.strip())
    events = []
    for block in blocks:
        lines = block.strip().splitlines()
        if not lines:
            continue
        time_line_idx = None
        for i, line in enumerate(lines):
            if BLOCK_RE.search(line):
                time_line_idx = i
                break
        if time_line_idx is None:
            continue
        m = BLOCK_RE.search(lines[time_line_idx])
        start = time_to_seconds(m.group(1))
        end = time_to_seconds(m.group(2))
        text = "\n".join(lines[time_line_idx + 1:]).strip()
        events.append({"start": start, "end": end, "text": text})
    if not events:
        if strict:
            die(f"Z souboru {path} se nepodařilo načíst žádné titulky (chybný formát?).")
        return []
    return events


def write_srt(events, path: Path):
    with path.open("w", encoding="utf-8") as f:
        for i, ev in enumerate(events, start=1):
            f.write(f"{i}\n")
            f.write(f"{seconds_to_srt_time(ev['start'])} --> {seconds_to_srt_time(ev['end'])}\n")
            f.write(f"{ev['text']}\n\n")


# ----------------------------------------------------------------------
# Jádro synchronizace - vlastní Python algoritmus (bez alass)
# ----------------------------------------------------------------------

def build_signal(events, resolution: float, duration: float):
    n = int(duration / resolution) + 1
    sig = np.zeros(n, dtype=np.float32)
    for ev in events:
        a = max(0, int(ev["start"] / resolution))
        b = min(n, int(ev["end"] / resolution) + 1)
        if b > a:
            sig[a:b] = 1.0
    return sig


def coarse_offset(ref_events, target_events, resolution=0.1, max_shift=120.0):
    """Najde nejlepší celkový časový posun pomocí FFT křížové korelace."""
    duration = max(
        max((e["end"] for e in ref_events), default=0.0),
        max((e["end"] for e in target_events), default=0.0),
    ) + max_shift + 1.0

    ref_sig = build_signal(ref_events, resolution, duration)
    tgt_sig = build_signal(target_events, resolution, duration)

    n = 1
    total_len = len(ref_sig) + len(tgt_sig)
    while n < total_len:
        n *= 2

    fft_ref = np.fft.rfft(ref_sig, n=n)
    fft_tgt = np.fft.rfft(tgt_sig, n=n)
    corr = np.fft.irfft(fft_ref * np.conj(fft_tgt), n=n)

    max_shift_samples = int(max_shift / resolution)
    shifts = np.concatenate([np.arange(0, max_shift_samples + 1),
                              np.arange(n - max_shift_samples, n)])
    shifts = shifts[shifts < n]
    best_k = shifts[np.argmax(corr[shifts])]
    if best_k > n // 2:
        best_k -= n
    return best_k * resolution  # kladné = target je POZDĚJI než ref -> potřeba odečíst


def refine_affine(ref_events, target_events, coarse_shift, tolerance=1.5, iterations=5):
    """
    Najde nejlepší lineární transformaci target_time -> ref_time formou
    target_corrected = scale * target_original + offset,
    pomocí spárování nejbližších začátků titulků a iterativního
    vyřazování odlehlých dvojic (jednoduchá robustní regrese).
    """
    ref_starts = np.array([e["start"] for e in ref_events])
    tgt_starts = np.array([e["start"] for e in target_events])

    scale, offset = 1.0, coarse_shift
    pairs_ref, pairs_tgt = [], []

    for _ in range(iterations):
        corrected = scale * tgt_starts + offset
        pairs_ref, pairs_tgt = [], []
        idx_sorted = np.argsort(corrected)
        sorted_corrected = corrected[idx_sorted]
        for r in ref_starts:
            j = np.searchsorted(sorted_corrected, r)
            best_j, best_d = None, None
            for cand in (j - 1, j):
                if 0 <= cand < len(sorted_corrected):
                    d = abs(sorted_corrected[cand] - r)
                    if best_d is None or d < best_d:
                        best_d, best_j = d, cand
            if best_j is not None and best_d <= tolerance:
                orig_idx = idx_sorted[best_j]
                pairs_ref.append(r)
                pairs_tgt.append(tgt_starts[orig_idx])

        if len(pairs_ref) < 2:
            break

        x = np.array(pairs_tgt)
        y = np.array(pairs_ref)
        A = np.vstack([x, np.ones_like(x)]).T
        new_scale, new_offset = np.linalg.lstsq(A, y, rcond=None)[0]

        resid = np.abs((new_scale * x + new_offset) - y)
        med = np.median(resid) + 1e-6
        keep = resid <= max(tolerance, med * 4)
        if keep.sum() >= 2:
            x2, y2 = x[keep], y[keep]
            A2 = np.vstack([x2, np.ones_like(x2)]).T
            new_scale, new_offset = np.linalg.lstsq(A2, y2, rcond=None)[0]

        scale, offset = float(new_scale), float(new_offset)

    return scale, offset, len(pairs_ref)


def apply_transform(events, scale, offset):
    out = []
    for ev in events:
        out.append({
            "start": scale * ev["start"] + offset,
            "end": scale * ev["end"] + offset,
            "text": ev["text"],
        })
    return out


# ----------------------------------------------------------------------
# Obsahová synchronizace "warp" - po jednotlivých větách (metoda --method warp)
# ----------------------------------------------------------------------
#
# Afinní metoda výše umí jen JEDEN globální vztah a*t+b pro celou epizodu.
# To selže, když je rozsync po ČÁSTECH (různé úseky potřebují různý posun -
# typicky když je reklamní/blokové dělení jinde, nebo když původní titulky
# "ujíždějí" jen v některých scénách). Tahle metoda místo toho:
#   1. Spáruje konkrétní VĚTY mezi opravovanými a referenčními titulky
#      (podle textu - znakové 3-gramy + sdílená jména/čísla, jazykově odolné).
#   2. Důvěřuje JEN jistým, výrazným a jednoznačným dvojicím ("kotvám").
#      Krátké generické řádky ("Ano.", "Co?") se nikdy nepárují podle textu -
#      ty by se trefily kamkoli; dopočítají se mezi kotvami.
#   3. Z kotev postaví po částech lineární časovou mapu a podle ní přesune
#      VŠECHNY titulky; navíc jemně "došťouchne" řádky k odpovídající
#      referenční větě, ale jen v okně pár sekund (nemůže teleportovat).
# Text se NIKDY nemění, mění se jen časy. Vyžaduje TEXTOVOU referenci
# (titulkovou stopu) - u --audio-mode replace (jen VAD) text není, takže
# se v "auto" automaticky použije afinní metoda.

import bisect as _bisect
import unicodedata as _unicodedata
from collections import Counter as _Counter

CA_DEFAULTS = dict(
    coarse_len=20, coarse_sim=0.55, coarse_margin=0.15,   # hrubá globální fáze
    band=45.0, min_len=12, min_sim=0.50, margin=0.10,     # jemná fáze (s)
    snap_win=3.0, snap_sim=0.30,                          # lokální došťouchnutí (s)
    min_dur=0.35,                                         # min. délka titulku (s)
)


def _ca_normalize(text):
    s = "".join(c for c in _unicodedata.normalize("NFKD", text)
                if not _unicodedata.combining(c)).lower()
    s = re.sub(r"[^0-9a-z\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _ca_grams(s, n=3):
    s = "\u2581" + s.replace(" ", "\u2581") + "\u2581"
    return _Counter(s[i:i + n] for i in range(len(s) - n + 1))


def _ca_distinctive(norm_text):
    return set(w for w in norm_text.split() if len(w) >= 4) | \
           set(re.findall(r"\d+", norm_text))


def _ca_prepare(events, sim_texts=None):
    """Vrátí kopie eventů s předpočítanými poli pro porovnávání textu.
    sim_texts (volitelně) = paralelní seznam textů použitých MÍSTO ev['text']
    pro VÝPOČET PODOBNOSTI (např. strojový překlad do společného jazyka).
    Výstupní 'text' (a tedy i uložené titulky) zůstává vždy původní."""
    out = []
    for idx, ev in enumerate(events):
        sim_src = sim_texts[idx] if sim_texts is not None else ev["text"]
        n = _ca_normalize(sim_src)
        g = _ca_grams(n)
        out.append({
            "start": ev["start"], "end": ev["end"], "text": ev["text"],
            "_n": n, "_g": g,
            "_gn": (sum(v * v for v in g.values()) ** 0.5) or 1.0,
            "_tk": _ca_distinctive(n),
            "_music": ("\u266a" in ev["text"] or "\u266b" in ev["text"] or "\u2669" in ev["text"]),
        })
    return out


def _ca_sim(a, b):
    g1, g2 = a["_g"], b["_g"]
    if len(g1) > len(g2):
        g1, g2 = g2, g1
    dot = sum(c * g2[k] for k, c in g1.items() if k in g2)
    cos = dot / (a["_gn"] * b["_gn"])
    return min(1.0, cos + min(0.25, 0.08 * len(a["_tk"] & b["_tk"])))


def _ca_combined(texts):
    n = " ".join(_ca_normalize(t) for t in texts)
    g = _ca_grams(n)
    return {"_n": n, "_g": g, "_gn": (sum(v * v for v in g.values()) ** 0.5) or 1.0,
            "_tk": _ca_distinctive(n)}


def _ca_lis(pairs):
    """Nejdelší ostře rostoucí podposloupnost v j - vynutí monotónní pořadí kotev."""
    if not pairs:
        return []
    js = [p[1] for p in pairs]
    tails, tails_idx, parent = [], [], [-1] * len(pairs)
    for k, jv in enumerate(js):
        pos = _bisect.bisect_left(tails, jv)
        if pos == len(tails):
            tails.append(jv); tails_idx.append(k)
        else:
            tails[pos] = jv; tails_idx[pos] = k
        parent[k] = tails_idx[pos - 1] if pos > 0 else -1
    seq, k = [], (tails_idx[-1] if tails_idx else -1)
    while k != -1:
        seq.append(k); k = parent[k]
    seq.reverse()
    return [pairs[k] for k in seq]


def _ca_find_anchors(S, O, band, min_len, min_sim, margin, prior=None):
    """Jisté dvojice (cíl_i -> ref_j). prior=warp -> hledá kolem predikce,
    jinak globálně (hrubá fáze, zvládne i rozsync o minuty)."""
    m = len(O)
    ostart = [o["start"] for o in O]
    raw = []
    for i, c in enumerate(S):
        if len(c["_n"]) < min_len:
            continue
        if prior is not None:
            centre = prior(c["start"])
            lo = _bisect.bisect_left(ostart, centre - band)
            hi = _bisect.bisect_right(ostart, centre + band)
            rng = range(lo, hi)
        else:
            rng = range(m)
        best, second = (-1.0, -1), -1.0
        for j in rng:
            sc = _ca_sim(c, O[j])
            if sc > best[0]:
                second = best[0]; best = (sc, j)
            elif sc > second:
                second = sc
        if best[1] >= 0 and best[0] >= min_sim and (best[0] - max(second, 0.0)) >= margin:
            raw.append((i, best[1], best[0]))
    return _ca_lis(raw)


def _ca_build_warp(S, O, anchors):
    pts = sorted({(S[i]["start"], O[j]["start"]) for i, j, _ in anchors})
    if not pts:
        return (lambda t: t)
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]

    def warp(t):
        if t <= xs[0]:
            return t + (ys[0] - xs[0])
        if t >= xs[-1]:
            return t + (ys[-1] - xs[-1])
        k = _bisect.bisect_right(xs, t) - 1
        x0, x1, y0, y1 = xs[k], xs[k + 1], ys[k], ys[k + 1]
        return y0 if x1 == x0 else y0 + (t - x0) / (x1 - x0) * (y1 - y0)
    return warp


def _ca_iou(corrected, ref_events):
    """Intersection-over-union dvou časových os (0..1, vyšší = lepší)."""
    def merge(iv):
        iv = sorted(iv); tot = 0.0; cs = ce = None
        for s, e in iv:
            if cs is None:
                cs, ce = s, e
            elif s <= ce:
                ce = max(ce, e)
            else:
                tot += ce - cs; cs, ce = s, e
        if cs is not None:
            tot += ce - cs
        return tot
    A = sorted((e["start"], e["end"]) for e in corrected)
    B = sorted((e["start"], e["end"]) for e in ref_events)
    inter = 0.0; j = 0
    for s, e in A:
        while j < len(B) and B[j][1] < s:
            j += 1
        k = j
        while k < len(B) and B[k][0] < e:
            inter += max(0.0, min(e, B[k][1]) - max(s, B[k][0])); k += 1
    uni = merge(A) + merge(B) - inter
    return inter / uni if uni else 0.0


def warp_align(ref_events, target_events, cfg=None, ref_sim_texts=None, target_sim_texts=None):
    """
    Obsahová (po větách) synchronizace. ref_events musí mít TEXT.
    ref_sim_texts / target_sim_texts (volitelně) = texty pro VÝPOČET podobnosti
    (typicky strojový překlad obou stran do společného jazyka), aby to fungovalo
    i mezi RŮZNÝMI jazyky. Výstupní text titulků se nikdy nemění - jen časování.
    Vrací (corrected_events, stats).
    """
    cfg = dict(CA_DEFAULTS, **(cfg or {}))
    S = _ca_prepare(target_events, sim_texts=target_sim_texts)
    O = _ca_prepare(ref_events, sim_texts=ref_sim_texts)
    n, m = len(S), len(O)
    ostart = [o["start"] for o in O]

    # 1) hrubé globální kotvy -> hrubá mapa
    coarse = _ca_find_anchors(S, O, band=10 ** 9,
                              min_len=cfg["coarse_len"], min_sim=cfg["coarse_sim"],
                              margin=cfg["coarse_margin"], prior=None)
    warp0 = _ca_build_warp(S, O, coarse)
    # 2) jemné kotvy kolem hrubé predikce
    anchors = _ca_find_anchors(S, O, band=cfg["band"],
                               min_len=cfg["min_len"], min_sim=cfg["min_sim"],
                               margin=cfg["margin"], prior=warp0)
    if len(anchors) < len(coarse):
        anchors = coarse
    warp = _ca_build_warp(S, O, anchors)
    amap = {i: j for i, j, _ in anchors}

    out = [None] * n; snapj = [None] * n; used_prev = -1
    for i in range(n):
        if i in amap:
            j = amap[i]; out[i] = [O[j]["start"], O[j]["end"]]; snapj[i] = j; used_prev = j; continue
        tw, twe = warp(S[i]["start"]), warp(S[i]["end"])
        if not S[i]["_music"]:
            lo = _bisect.bisect_left(ostart, tw - cfg["snap_win"])
            hi = _bisect.bisect_right(ostart, tw + cfg["snap_win"])
            best = (-1.0, -1)
            for j in range(lo, hi):
                sc = _ca_sim(S[i], O[j])
                if sc > best[0]:
                    best = (sc, j)
            if best[1] >= 0 and best[0] >= cfg["snap_sim"] and best[1] != used_prev:
                j = best[1]; out[i] = [O[j]["start"], O[j]["end"]]; snapj[i] = j; used_prev = j; continue
        out[i] = [tw, twe]; snapj[i] = None

    # 3) podělit běhy více cílových titulků, které spadly do JEDNÉ ref věty
    i = 0
    while i < n:
        j = snapj[i]
        if j is None:
            i += 1; continue
        k = i
        while k + 1 < n and snapj[k + 1] == j:
            k += 1
        if k > i:
            s, e = O[j]["start"], O[j]["end"]
            tot = sum(max(1, len(S[t]["text"])) for t in range(i, k + 1)); acc = 0
            for t in range(i, k + 1):
                w = max(1, len(S[t]["text"]))
                st = s + (e - s) * acc / tot; acc += w
                out[t] = [st, s + (e - s) * acc / tot]
        i = k + 1

    # 4) monotónní začátky, min. délka, žádné překryvy
    MIN = cfg["min_dur"]
    for i in range(n):
        if i > 0 and out[i][0] < out[i - 1][0]:
            out[i][0] = out[i - 1][0]
        if out[i][1] < out[i][0] + MIN:
            out[i][1] = out[i][0] + MIN
    for i in range(n - 1):
        if out[i][1] > out[i + 1][0]:
            if out[i + 1][0] > out[i][0] + MIN:
                out[i][1] = out[i + 1][0]
            else:
                mid = (out[i][0] + out[i + 1][1]) / 2.0
                out[i][1] = max(out[i][0] + MIN, min(out[i][1], mid))
                if out[i + 1][0] < out[i][1]:
                    out[i + 1][0] = out[i][1]
                if out[i + 1][1] < out[i + 1][0] + MIN:
                    out[i + 1][1] = out[i + 1][0] + MIN

    corrected = [{"start": out[i][0], "end": out[i][1], "text": target_events[i]["text"]}
                 for i in range(n)]
    # záruka: text a pořadí beze změny
    assert [e["text"] for e in corrected] == [e["text"] for e in target_events], \
        "warp_align: text se nesmí změnit!"
    stats = {"anchors": len(anchors), "iou": _ca_iou(corrected, ref_events)}
    return corrected, stats


# ----------------------------------------------------------------------
# Přenos PROFESIONÁLNÍHO překladu na strojově načasované titulky
# (stejný pořad, stejný jazyk, jiný překlad + jiné dělení epizod/času).
# Nahradí text cílových titulků nejpodobnějším profesionálním textem,
# ale ZACHOVÁ cílové časování.
# ----------------------------------------------------------------------
def _best_in_range(s, O, lo, hi):
    """Nejpodobnější ref titulek v rozsahu [lo, hi] (přímý sken)."""
    bj, bs = -1, 0.0
    for j in range(lo, hi + 1):
        sim = _ca_sim(s, O[j])
        if sim > bs:
            bs, bj = sim, j
    return bj, bs


def _transplant_accept(s, o, sim, min_sim):
    """Přijmout náhradu? Kromě prahu vyžaduje aspoň 1 společné obsahové slovo a
    u krátkých titulků vyšší jistotu (zabrání záměně za jinou podobnou větu)."""
    if sim < min_sim:
        return False
    shared = len(s["_tk"] & o["_tk"])
    ntk = len(s["_tk"])
    if shared < 1:
        return False
    if ntk <= 1:
        return sim >= min(0.92, min_sim + 0.28)
    if ntk <= 2:
        return sim >= min_sim + 0.12 and shared >= 1
    if ntk <= 3:
        return sim >= min_sim + 0.05
    return True


def _load_reference_pool(directory, recursive=False, continuous=False):
    """Načte a spojí VŠECHNY .srt v adresáři (setříděné) do jedné zásoby eventů.
    continuous=True: časy jednotlivých souborů posune tak, aby šly plynule za
    sebou (nutné pro přečasování - čas musí být v celé zásobě monotónní).
    Vrací (pool, files)."""
    files = collect_srts(directory, recursive)
    pool = []
    offset = 0.0
    for f in files:
        try:
            evs = parse_srt(Path(f), strict=False)
        except Exception as e:
            log_warn(f"{os.path.basename(f)}: nelze načíst ({e}) - přeskakuji.")
            continue
        if continuous and evs:
            base = offset - evs[0]["start"]
            for e in evs:
                pool.append({"start": e["start"] + base, "end": e["end"] + base, "text": e["text"]})
            offset = pool[-1]["end"] + 2.0
        else:
            pool.extend(evs)
    return pool, files


def retime_professional(ref_events, pool_events, min_sim=0.5, margin=25):
    """Přečasuje PROFESIONÁLNÍ titulky (pool_events, spojená zásoba viki) na
    časování referenčních titulků (ref_events = tvoje strojové s dobrým
    časováním). Zachová 100 % profesionálního textu, jen mu dá tvůj timing.
    Vrací (new_events, n_anchors) nebo (None, n_anchors) při málu kotev."""
    if not ref_events or not pool_events:
        return None, 0
    S = _ca_prepare(ref_events)
    O = _ca_prepare(pool_events)
    M = len(O)
    inv = {}
    for j, o in enumerate(O):
        for t in o["_tk"]:
            inv.setdefault(t, []).append(j)
    common_cap = max(30, int(0.01 * M) + 1)

    def best(i):
        s = S[i]
        if not s["_tk"]:
            return -1, 0.0
        cand = set()
        for t in s["_tk"]:
            post = inv.get(t)
            if post and len(post) <= common_cap:
                cand.update(post)
        bj, bs = -1, 0.0
        for j in cand:
            sim = _ca_sim(s, O[j])
            if sim > bs:
                bs, bj = sim, j
        return bj, bs

    N = len(S)
    raw = [best(i) for i in range(N)]
    conf = [(i, raw[i][0]) for i in range(N)
            if raw[i][0] >= 0 and raw[i][1] >= max(0.5, min_sim) and len(S[i]["_tk"]) >= 2]
    anchors = _ca_lis(conf)
    if len(anchors) < 3:
        return None, len(anchors)

    # výřez zásoby, který patří k tomuto dílu (od první po poslední kotvu + rezerva)
    j_lo = max(0, anchors[0][1] - margin)
    j_hi = min(M - 1, anchors[-1][1] + margin)

    # časová deformace: body (viki_čas -> netflix_čas) z kotev
    pts = {}
    for (i, j) in anchors:
        pts[pool_events[j]["start"]] = ref_events[i]["start"]
    xs = sorted(pts)
    ys = [pts[x] for x in xs]

    def warp_time(t):
        if t <= xs[0]:
            slope = (ys[1] - ys[0]) / (xs[1] - xs[0]) if len(xs) >= 2 and xs[1] > xs[0] else 1.0
            return ys[0] + (t - xs[0]) * slope
        if t >= xs[-1]:
            slope = (ys[-1] - ys[-2]) / (xs[-1] - xs[-2]) if len(xs) >= 2 and xs[-1] > xs[-2] else 1.0
            return ys[-1] + (t - xs[-1]) * slope
        k = _bisect.bisect_right(xs, t) - 1
        x0, x1, y0, y1 = xs[k], xs[k + 1], ys[k], ys[k + 1]
        return y0 if x1 <= x0 else y0 + (t - x0) * (y1 - y0) / (x1 - x0)

    out = []
    for j in range(j_lo, j_hi + 1):
        e = pool_events[j]
        ns = warp_time(e["start"])
        ne = warp_time(e["end"])
        if ne <= ns:
            ne = ns + max(0.3, e["end"] - e["start"])
        out.append({"start": ns, "end": ne, "text": e["text"]})

    out.sort(key=lambda x: x["start"])
    # lehké zamezení překryvů (posuň konec pod začátek dalšího)
    for k in range(len(out) - 1):
        if out[k]["end"] > out[k + 1]["start"] - 0.02:
            out[k]["end"] = max(out[k]["start"] + 0.3, out[k + 1]["start"] - 0.05)
    return out, len(anchors)


def build_transplanter(ref_events):
    """Připraví zásobu profesionálních titulků JEDNOU a vrátí funkci, která na ni
    napasuje libovolný cílový soubor. Vrací (transplant_fn, pocet_ref_cues)."""
    O = _ca_prepare(ref_events)
    M = len(O)
    inv = {}
    for j, o in enumerate(O):
        for t in o["_tk"]:
            inv.setdefault(t, []).append(j)
    common_cap = max(30, int(0.01 * M) + 1)

    def best_global(S, i):
        s = S[i]
        if not s["_tk"]:
            return -1, 0.0
        cand = set()
        for t in s["_tk"]:
            post = inv.get(t)
            if post and len(post) <= common_cap:
                cand.update(post)
        bj, bs = -1, 0.0
        for j in cand:
            sim = _ca_sim(s, O[j])
            if sim > bs:
                bs, bj = sim, j
        return bj, bs

    def transplant(target_events, min_sim=0.55, window=40):
        if not target_events or M == 0:
            return ([dict(e) for e in target_events], 0, len(target_events))
        S = _ca_prepare(target_events)
        N = len(S)
        raw = [best_global(S, i) for i in range(N)]
        conf = [(i, raw[i][0]) for i in range(N)
                if raw[i][0] >= 0 and raw[i][1] >= max(0.5, min_sim) and len(S[i]["_tk"]) >= 2]
        anchors = _ca_lis(conf)
        result = [{"start": e["start"], "end": e["end"], "text": e["text"]} for e in target_events]
        n_repl = 0
        if anchors:
            ai = [a[0] for a in anchors]
            aj = [a[1] for a in anchors]

            def predict(i):
                p = _bisect.bisect_left(ai, i)
                if p == 0:
                    return aj[0] + (i - ai[0])
                if p >= len(ai):
                    return aj[-1] + (i - ai[-1])
                if ai[p] == i:
                    return aj[p]
                i0, i1, j0, j1 = ai[p - 1], ai[p], aj[p - 1], aj[p]
                frac = (i - i0) / (i1 - i0) if i1 > i0 else 0.0
                return int(round(j0 + frac * (j1 - j0)))

            for i in range(N):
                r0 = max(0, min(M - 1, predict(i)))
                lo, hi = max(0, r0 - window), min(M - 1, r0 + window)
                bj, bs = _best_in_range(S[i], O, lo, hi)
                if bj >= 0 and _transplant_accept(S[i], O[bj], bs, min_sim):
                    result[i]["text"] = O[bj]["text"]
                    n_repl += 1
        else:
            for i in range(N):
                bj, bs = raw[i]
                if bj >= 0 and _transplant_accept(S[i], O[bj], bs, min_sim):
                    result[i]["text"] = O[bj]["text"]
                    n_repl += 1
        for i in range(1, N):
            a = result[i - 1]["text"].strip()
            b = result[i]["text"].strip()
            if a and a == b and b != target_events[i]["text"].strip():
                result[i]["text"] = target_events[i]["text"]
                n_repl -= 1
        return result, n_repl, N

    return transplant, M


def transplant_text(target_events, ref_events, min_sim=0.55, window=40):
    """Tenký obal nad build_transplanter (pro jednorázové/otestování)."""
    fn, _ = build_transplanter(ref_events)
    return fn(target_events, min_sim, window)





# ----------------------------------------------------------------------
# Strojový překlad pro mezijazyčné párování (jen pro VÝPOČET podobnosti)
# ----------------------------------------------------------------------
#
# Když jsou opravované a referenční titulky v RŮZNÝCH jazycích, textová
# podobnost (znakové 3-gramy) sama nestačí. Řešení: obě strany se pro účely
# PÁROVÁNÍ přeloží do společného jazyka (pivot, default angličtina) a teprve
# přeložené texty se porovnávají. Výstupní titulky se NEPŘEKLÁDAJÍ - mění se
# jen časování, původní text zůstává. Překlady se kešují na disk (dedup +
# cache), takže další běhy jsou rychlé a šetří se volání služby.

_TRANSLATE_CACHE = None
_TRANSLATE_CACHE_PATH = None


def _translate_cache_load(path):
    global _TRANSLATE_CACHE, _TRANSLATE_CACHE_PATH
    _TRANSLATE_CACHE_PATH = path
    if _TRANSLATE_CACHE is not None:
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            _TRANSLATE_CACHE = json.load(f)
    except Exception:
        _TRANSLATE_CACHE = {}


def _translate_cache_save():
    if _TRANSLATE_CACHE_PATH is None or _TRANSLATE_CACHE is None:
        return
    try:
        with open(_TRANSLATE_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_TRANSLATE_CACHE, f, ensure_ascii=False)
    except Exception as e:
        log_warn(f"Nepodařilo se uložit cache překladů: {e}")


class _FatalAPIError(Exception):
    """Neopravitelná chyba API (např. 400/401/403/404) - nemá smysl opakovat."""


def _http_error_detail(e):
    try:
        body = e.read().decode("utf-8", "replace")
    except Exception:
        return ""
    try:
        data = json.loads(body)
        msg = data.get("error", {})
        if isinstance(msg, dict):
            return (msg.get("message") or msg.get("type") or body)[:400]
        return (data.get("message") or body)[:400]
    except Exception:
        return body[:400]


def _call_with_timeout(fn, timeout):
    """Spustí fn() s časovým limitem. Vrací (výsledek, chyba). Při překročení
    limitu vrací (None, TimeoutError) a nechá vlákno doběžet na pozadí (daemon),
    aby zaseknutý síťový požadavek nezablokoval celý běh."""
    import threading
    box = {}

    def run():
        try:
            box["r"] = fn()
        except Exception as e:  # noqa: BLE001
            box["e"] = e
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        return None, TimeoutError("timeout")
    return box.get("r"), box.get("e")


# Veřejný klíč widgetu Google Translate (stejný používá i translatesubtitles.co).
# Není to uživatelský účet - je "zapečený" do webového překladače Googlu.
_GOOGLE_TRANSLATE_PA_KEY = "AIzaSyATBXajvzQLTDHEQbcpq0Ihe0vWDHmO520"
_GOOGLE_TRANSLATE_PA_URL = "https://translate-pa.googleapis.com/v1/translateHtml"


def google_translatehtml(texts, target_lang, source_lang="auto", timeout=30):
    """Přeloží dávku textů přes moderní endpoint Google Translate 'translateHtml'
    (zdarma, bez účtu; totéž, co dělá web translatesubtitles.co a widget Google
    Translate). Vrací seznam stejné délky (None u nezdaru), nebo None při chybě
    celého volání (aby volající mohl přepnout na fallback)."""
    import urllib.request
    import urllib.error
    import html as _html
    escaped = [_html.escape(t if isinstance(t, str) else "", quote=True) for t in texts]
    payload = [[escaped, source_lang, target_lang], "te"]
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json+protobuf",
        "X-Goog-API-Key": _GOOGLE_TRANSLATE_PA_KEY,
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://translatesubtitles.co/",
        "Origin": "https://translatesubtitles.co",
    }
    req = urllib.request.Request(_GOOGLE_TRANSLATE_PA_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    # odpověď: [[<překlady>], [<detekované jazyky>]]
    try:
        translations = data[0]
    except (IndexError, KeyError, TypeError):
        return None
    if not isinstance(translations, list) or len(translations) != len(texts):
        return None
    return [_html.unescape(t) if isinstance(t, str) and t else None for t in translations]


def make_translator(engine, pivot_lang, cache_path=None, api_key=None, model=None):
    """Vrátí funkci translate_list(list[str]) -> list[str], která přeloží texty
    do pivot_lang. Dedup + disková cache. Vrací None, pokud zvolený překladač
    není k dispozici. Podporuje 'google'/'argos' (zdarma), 'deepl' (API klíč)
    a 'claude' (Anthropic API klíč)."""
    if engine in (None, "off"):
        return None
    if cache_path is None:
        cache_path = os.path.join(_cache_dir(".translate_cache"), f"{engine}_{pivot_lang}.json")
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    _translate_cache_load(cache_path)

    backend = None
    if engine == "google":
        # Primárně moderní Google endpoint 'translateHtml' (stejný, jaký používá
        # widget Google Translate i weby jako translatesubtitles.co): zdarma, bez
        # účtu, celá dávka jedním requestem, zachovává HTML/entity. Když selže,
        # spadne na deep-translator (pokud je nainstalován).
        dt = None
        try:
            from deep_translator import GoogleTranslator
            dt = GoogleTranslator(source="auto", target=pivot_lang)
        except Exception:
            dt = None
        _gw_state = {"ok": True}

        def backend(batch):
            if _gw_state["ok"]:
                res = google_translatehtml(batch, pivot_lang)
                if res is not None:
                    return res
                _gw_state["ok"] = False  # endpoint nedostupný -> dál už jen fallback
                if dt is not None:
                    log_warn("Google 'translateHtml' nedostupný - přepínám na deep-translator.")
                else:
                    log_warn("Google 'translateHtml' nedostupný a deep-translator není "
                             "nainstalován (pip install deep-translator).")
            if dt is None:
                return [None] * len(batch)
            try:
                r = dt.translate_batch(batch)
                if isinstance(r, list) and len(r) == len(batch):
                    return [x if isinstance(x, str) and x else None for x in r]
            except Exception:
                pass
            out = []
            for t in batch:
                try:
                    x = dt.translate(t)
                    out.append(x if isinstance(x, str) and x else None)
                except Exception:
                    out.append(None)
            return out

    elif engine == "deepl":
        if not api_key:
            log_warn("Pro --translate deepl chybí API klíč (--deepl-key nebo proměnná "
                     "DEEPL_API_KEY). Pokračuji bez DeepL.")
            return None
        try:
            from deep_translator import DeeplTranslator
        except ImportError:
            log_warn("Pro DeepL chybí balíček 'deep-translator'. "
                     "Nainstaluj: pip install deep-translator. Pokračuji bez DeepL.")
            return None
        try:
            dt = DeeplTranslator(api_key=api_key, source="auto", target=pivot_lang, use_free_api=True)
        except Exception as e:
            log_warn(f"DeepL se nepodařilo inicializovat: {e}")
            return None

        def backend(batch):
            res = []
            for t in batch:
                try:
                    r = dt.translate(t)
                    res.append(r if isinstance(r, str) and r else None)
                except Exception:
                    res.append(None)
            return res

    elif engine == "argos":
        try:
            import argostranslate.translate as _at  # noqa: F401
        except ImportError:
            log_warn("Pro --translate argos chybí balíček 'argostranslate' "
                     "(offline překlad). Nainstaluj: pip install argostranslate. "
                     "Pokračuji bez překladu.")
            return None
        try:
            import langdetect  # noqa: F401
            from langdetect import detect as _detect
        except ImportError:
            log_warn("Offline překlad (argos) potřebuje i 'langdetect' pro detekci "
                     "zdrojového jazyka: pip install langdetect. Pokračuji bez překladu.")
            return None

        def backend(batch):
            import argostranslate.translate as at
            res = []
            for t in batch:
                try:
                    src = _detect(t)
                    res.append(t if src == pivot_lang else at.translate(t, src, pivot_lang))
                except Exception:
                    res.append(None)
            return res

    elif engine == "claude":
        if not api_key:
            log_warn("Pro překlad přes Claude chybí Anthropic API klíč "
                     "(--anthropic-key nebo ANTHROPIC_API_KEY). Pokračuji bez Claude.")
            return None
        cl_model = model or "claude-sonnet-4-6"

        def backend(batch):
            res = anthropic_translate_batch(batch, pivot_lang, api_key, cl_model)
            if not isinstance(res, list) or len(res) != len(batch):
                return [None] * len(batch)
            return res

    elif engine == "gemini":
        if not api_key:
            log_warn("Pro překlad přes Gemini chybí Google API klíč (--gemini-key nebo "
                     "GEMINI_API_KEY / GOOGLE_API_KEY). Zdarma na aistudio.google.com. "
                     "Pokračuji bez Gemini.")
            return None
        gm_model = model or "gemini-2.5-flash"

        def backend(batch):
            res = gemini_translate_batch(batch, pivot_lang, api_key, gm_model)
            if not isinstance(res, list) or len(res) != len(batch):
                return [None] * len(batch)
            return res
    else:
        return None

    def translate_list(texts):
        key_prefix = f"{engine}|{pivot_lang}|"
        # co chybí v cache (dedup podle unikátního textu)
        uniq = []
        seen = set()
        for t in texts:
            kt = t.strip()
            if kt and (key_prefix + kt) not in _TRANSLATE_CACHE and kt not in seen:
                seen.add(kt); uniq.append(kt)
        if uniq:
            total = len(uniq)
            CH = 25 if engine in ("google", "deepl", "claude", "gemini") else 50
            timeout = 90
            log_info(f"Překládám {total} unikátních řádků do '{pivot_lang}' ({engine})... "
                     f"(může chvíli trvat; průběh níže, výsledky se průběžně kešují)")
            done = 0
            failed = 0
            for i in range(0, total, CH):
                chunk = uniq[i:i + CH]
                outs, err = _call_with_timeout(lambda c=chunk: backend(c), timeout)
                if isinstance(err, _FatalAPIError):
                    print()
                    log_warn(f"Překlad ({engine}) zastaven: {err}")
                    log_warn("Zkontroluj API klíč a název modelu, nebo zkus jiný překladač "
                             "(v průvodci volba 'Strojový překladač', nebo přepínač --translate). "
                             "Google je zdarma bez klíče, Gemini zdarma s klíčem. "
                             "Zbytek řádků zůstane v originále.")
                    failed += (total - done)
                    break
                if outs is None:
                    log_warn(f"\nPřekladač neodpověděl do {timeout}s u bloku "
                             f"{i // CH + 1} - přeskakuji (řádky zůstanou v originále).")
                    outs = [None] * len(chunk)
                if not isinstance(outs, list) or len(outs) != len(chunk):
                    outs = [None] * len(chunk)
                for src, dst in zip(chunk, outs):
                    if isinstance(dst, str) and dst:
                        _TRANSLATE_CACHE[key_prefix + src] = dst   # cachuj jen úspěch
                    else:
                        failed += 1
                done += len(chunk)
                _translate_cache_save()                      # průběžně -> lze přerušit a navázat
                pct = int(done * 100 / total)
                print(f"\r  překlad: {done}/{total} ({pct}%)" + (f", selhalo {failed}" if failed else ""),
                      end="", flush=True)
            print()  # nový řádek po průběhu
            if failed:
                log_warn(f"Překlad selhal u {failed}/{total} řádků - "
                         "nepřeložené zůstanou v originále. Zkontroluj klíč/model/připojení, "
                         "nebo zkus jiný překladač (DeepL/Claude/Google/argos).")
            else:
                log_info(f"Přeloženo a uloženo do cache ({done} řádků).")
        # poskládat výstup (klíč chybí = ponech originál)
        return [_TRANSLATE_CACHE.get(key_prefix + t.strip(), t) for t in texts]

    return translate_list


def run_alignment(args, ref_events, ref_events_sub, target_events):
    """Vybere a spustí metodu synchronizace; vrací opravené titulky (events).
    method:
      'affine' - globální a*t+b (jazykově nezávislé, i ze zvuku),
      'warp'   - po větách (textová reference; opraví i rozsync po částech),
      'combo'  - afinní předsrovnání + warp doladění (nejrobustnější),
      'auto'   - combo když je textová reference a dost kotev, jinak affine."""
    method = getattr(args, "method", "auto")
    have_text_ref = bool(ref_events_sub)

    def affine_core(targets):
        shift = coarse_offset(ref_events, targets, max_shift=args.max_shift)
        scale, offset, n_matched = refine_affine(ref_events, targets, shift, tolerance=args.tolerance)
        return apply_transform(targets, scale, offset), scale, offset, n_matched

    def do_affine():
        log_info("Metoda: afinní (globální posun + rychlost).")
        log_info("Hledám hrubý časový posun (FFT křížová korelace)...")
        corrected, scale, offset, n_matched = affine_core(target_events)
        log_info(f"Výsledná transformace: nový_čas = {scale:.6f} * starý_čas + {offset:+.3f}")
        log_info(f"Spárováno {n_matched} z {len(ref_events)} referenčních kotev pro zpřesnění")
        if abs(scale - 1.0) > 0.05:
            log_warn("Velký rozdíl v rychlosti (>5%) - možná jiný framerate zdrojů, zkontroluj výsledek.")
        return corrected

    def _warp_cfg():
        cfg = {}
        if getattr(args, "ca_band", None) is not None:
            cfg["band"] = args.ca_band
        if getattr(args, "ca_snap_win", None) is not None:
            cfg["snap_win"] = args.ca_snap_win
        if getattr(args, "ca_min_sim", None) is not None:
            cfg["min_sim"] = args.ca_min_sim
        return cfg

    def _translations():
        engine = getattr(args, "translate", "off")
        if not engine or engine == "off":
            return None, None
        pivot = getattr(args, "pivot_lang", None) or "en"
        key = model = None
        if engine == "deepl":
            key = getattr(args, "deepl_key", None) or os.environ.get("DEEPL_API_KEY")
        elif engine == "claude":
            key = getattr(args, "anthropic_key", None) or os.environ.get("ANTHROPIC_API_KEY")
            model = getattr(args, "anthropic_model", None)
        elif engine == "gemini":
            key = (getattr(args, "gemini_key", None) or os.environ.get("GEMINI_API_KEY")
                   or os.environ.get("GOOGLE_API_KEY"))
            model = getattr(args, "gemini_model", None)
        translator = make_translator(engine, pivot, api_key=key, model=model)
        if translator is None:
            return None, None
        log_info(f"Mezijazyčný režim: porovnávám přes překlad do '{pivot}' ({engine}).")
        return (translator([e["text"] for e in ref_events_sub]),
                translator([e["text"] for e in target_events]))

    def warp_on(targets, ref_sim, target_sim, label):
        log_info(label)
        corrected, st = warp_align(ref_events_sub, targets, _warp_cfg(),
                                   ref_sim_texts=ref_sim, target_sim_texts=target_sim)
        log_info(f"Použito {st['anchors']} jistých kotev z {len(targets)} titulků; "
                 f"shoda s referencí (IoU): {st['iou']:.3f}")
        return corrected, st["anchors"]

    if method == "affine":
        return do_affine()

    if method in ("warp", "combo"):
        if not have_text_ref:
            die(f"--method {method} potřebuje TEXTOVOU referenční titulkovou stopu, ale "
                "při --audio-mode replace žádná není. Použij --audio-mode off/combine "
                "nebo --method affine.")
        ref_sim, target_sim = _translations()
        if method == "combo":
            log_info("Metoda: kombinovaná (afinní předsrovnání + warp doladění po větách).")
            log_info("1/2 afinní předsrovnání...")
            pre, scale, offset, _ = affine_core(target_events)
            corrected, n_anchors = warp_on(pre, ref_sim, target_sim, "2/2 warp doladění po větách...")
            if n_anchors < 2:
                log_warn("Příliš málo textových kotev - ponechávám výsledek afinní fáze.")
                return pre
            return corrected
        corrected, n_anchors = warp_on(target_events, ref_sim, target_sim,
                                       "Metoda: obsahová 'warp' (párování vět + po částech lineární mapa).")
        if n_anchors < 2:
            log_warn("Příliš málo textových kotev pro spolehlivou 'warp' mapu - "
                     "přepínám na afinní metodu.")
            return do_affine()
        return corrected

    # auto -> combo (afinní předsrovnání + warp), s pojistkou na afinní
    if have_text_ref:
        min_anchors = max(5, len(target_events) // 50)
        ref_sim, target_sim = _translations()
        log_info("Metoda: auto = kombinovaná (afinní předsrovnání + warp).")
        log_info("1/2 afinní předsrovnání...")
        pre, scale, offset, _ = affine_core(target_events)
        corrected, n_anchors = warp_on(pre, ref_sim, target_sim, "2/2 warp doladění po větách...")
        if n_anchors >= min_anchors:
            return corrected
        log_warn(f"Málo textových kotev ({n_anchors} < {min_anchors}) - referenční překlad "
                 "je nejspíš v jiném jazyce/hodně odlišný; používám afinní výsledek "
                 "(zvaž --translate google).")
        return pre
    return do_affine()


# Výchozí hodnoty pro fix_short_durations - jako konstanty, aby na ně šlo
# odkazovat z víc míst (CLI defaulty teď None, aby se poznalo, že je
# uživatel NEzadal explicitně - důležité pro --fix-readability níž).
DEFAULT_MIN_CPS = 17.0
DEFAULT_MIN_DURATION_FLOOR = 1.0
DEFAULT_MIN_GAP = 0.084
DEFAULT_LINE_OVERHEAD = 0.2  # extra sekundy za KAŽDÝ řádek navíc (oči musí "přeskočit" na další řádek)

# Jmenované presety čtecí rychlosti: (cps, floor). Slouží jako rychlá
# volba jak pro --reading-speed na příkazové řádce, tak pro interaktivní
# nabídku u --fix-readability - jedno místo pravdy pro obojí.
READING_SPEED_PRESETS = {
    "normal":    (17.0, 1.0, "Normální tempo"),
    "slow":      (12.0, 1.3, "Pomalí čtenáři"),
    "very-slow": (9.0, 1.6, "Extrémně pomalí / začínající čtenáři"),
}


def resolve_speed_params(args):
    """Sjednocené rozhodnutí cps/floor/gap/line_overhead z args:
    --reading-speed dá základ, explicitní --min-cps/--min-duration-floor/
    --min-gap/--line-overhead (pokud zadané) mají před presetem přednost."""
    if args.reading_speed:
        cps, floor, _label = READING_SPEED_PRESETS[args.reading_speed]
    else:
        cps, floor = DEFAULT_MIN_CPS, DEFAULT_MIN_DURATION_FLOOR
    if args.min_cps is not None:
        cps = args.min_cps
    if args.min_duration_floor is not None:
        floor = args.min_duration_floor
    gap = args.min_gap if args.min_gap is not None else DEFAULT_MIN_GAP
    line_overhead = args.line_overhead if args.line_overhead is not None else DEFAULT_LINE_OVERHEAD
    return cps, floor, gap, line_overhead


def fix_short_durations(events, min_cps=DEFAULT_MIN_CPS, min_duration_floor=DEFAULT_MIN_DURATION_FLOOR,
                         min_gap=DEFAULT_MIN_GAP, line_overhead=DEFAULT_LINE_OVERHEAD):
    """
    Prodlouží titulky, které zmizí příliš rychle vzhledem k délce textu,
    a to POUZE pokud je k tomu volné místo (mezera do dalšího titulku) -
    nikdy nepřesáhne mezeru (minus bezpečnostní min_gap před dalším titulkem)
    a nikdy neprodlouží víc, než kolik si text reálně "žádá". ČASOVÁNÍ MÁ
    VŽDY PŘEDNOST: tahle funkce nikdy nezmění start žádného titulku a nikdy
    nezasáhne do dalšího - to je neporušitelná hranice, bez ohledu na to,
    jaké parametry níž zvolíš.

    Cílová délka zobrazení NENÍ jen "počet znaků / rychlost" - zohledňuje
    i počet řádků (vícero řádků = oko musí navíc přeskočit na další řádek,
    takže krátké jednoslovné titulky nikdy nedostanou stejnou délku jako
    víceřádková věta jen kvůli společné "podlaze").

    min_cps           - cílová čtecí rychlost ve znacích/s (default 17;
                         menší hodnota = delší ideální zobrazení)
    min_duration_floor - absolutní podlaha v sekundách bez ohledu na text
    min_gap           - mezera, která musí zůstat zachována před dalším titulkem
    line_overhead     - extra sekundy za každý řádek NAD první (default 0.2);
                         dvouřádkový titulek tak dostane +0.2s, třířádkový +0.4s
                         navíc oproti čistě znakovému výpočtu
    """
    out = [dict(ev) for ev in events]
    n = len(out)
    extended = 0
    for i in range(n):
        char_count = len(re.sub(r"\s+", "", out[i]["text"]))
        if char_count == 0:
            continue
        line_count = max(1, len([l for l in out[i]["text"].split("\n") if l.strip()]))
        ideal_duration = max(
            min_duration_floor,
            char_count / min_cps + (line_count - 1) * line_overhead,
        )
        duration = out[i]["end"] - out[i]["start"]
        if duration >= ideal_duration:
            continue

        gap_to_next = (out[i + 1]["start"] - out[i]["end"]) if i + 1 < n else float("inf")
        available = max(0.0, gap_to_next - min_gap)
        extend_by = min(available, ideal_duration - duration)
        if extend_by > 0.001:
            out[i]["end"] += extend_by
            extended += 1
    return out, extended


# ----------------------------------------------------------------------
# Dávkové zpracování (--all) - spáruje video<->titulky v adresáři, ověří
# dostupné stopy PŘED zpracováním, na problém se interaktivně zeptá, a
# každý pár pak zpracuje jako podproces tohoto skriptu (žádné riziko, že
# selhání u jednoho dílu zastaví / poškodí ostatní).
# ----------------------------------------------------------------------

VIDEO_EXTS_BATCH = {".mkv", ".mp4", ".m4v", ".mov", ".webm"}


def collect_videos(directory, recursive):
    videos = []
    if recursive:
        for root, _d, files in os.walk(directory):
            for f in files:
                if Path(f).suffix.lower() in VIDEO_EXTS_BATCH:
                    videos.append(os.path.join(root, f))
    else:
        for f in os.listdir(directory):
            full = os.path.join(directory, f)
            if os.path.isfile(full) and Path(f).suffix.lower() in VIDEO_EXTS_BATCH:
                videos.append(full)
    return sorted(videos)


def collect_srts(directory, recursive):
    srts = []
    if recursive:
        for root, _d, files in os.walk(directory):
            for f in files:
                if f.lower().endswith(".srt"):
                    srts.append(os.path.join(root, f))
    else:
        for f in os.listdir(directory):
            full = os.path.join(directory, f)
            if os.path.isfile(full) and f.lower().endswith(".srt"):
                srts.append(full)
    return sorted(srts)


def _srt_lang_tag(srt_path, vstem):
    """'X.S01E01.cs.srt' + vstem='X.S01E01' -> 'cs'. None, pokud žádný tag."""
    sstem = Path(srt_path).stem
    if sstem == vstem:
        return None
    if sstem.startswith(vstem + "."):
        return sstem[len(vstem) + 1:].lower()
    return None


def match_srt_for_video(video_path, srt_candidates, target_lang):
    """Najde .srt soubory se stejným základem jména jako video (+ volitelný
    jazykový/forced tag za poslední tečkou), POUZE ve stejném adresáři jako
    video (aby --recursive nepárovalo přes různé složky)."""
    vstem = Path(video_path).stem
    vdir = os.path.dirname(video_path) or "."
    matches = []
    for s in srt_candidates:
        if (os.path.dirname(s) or ".") != vdir:
            continue
        sstem = Path(s).stem
        if sstem == vstem or sstem.startswith(vstem + "."):
            matches.append(s)
    if target_lang:
        tagged = [s for s in matches if _srt_lang_tag(s, vstem) == target_lang.lower()]
        if tagged:
            matches = tagged
    return matches


# ----------------------------------------------------------------------
# Preset - uložení/přehrání odpovědí interaktivního průvodce (--save/--load)
# ----------------------------------------------------------------------
_PRESET_MODE = None        # None | "save" | "load"
_PRESET_DATA = []          # načtené odpovědi (load)
_PRESET_IDX = 0
_PRESET_REC = []           # zaznamenané odpovědi (save)
_PRESET_CMD = None
_PRESET_PATH = None
_PRESET_SAVED = False
_PRESET_MISS = object()
_SECRET_HINTS = ("klíč", "klic", "key", "heslo", "password", "token")


def preset_is_replaying():
    """True, když právě běží preset z --load (žádný interaktivní uživatel)."""
    return _PRESET_MODE == "load"


def _is_secret_prompt(p):
    pl = str(p).lower()
    return any(h in pl for h in _SECRET_HINTS)


def _preset_replay():
    """Vrátí další uloženou odpověď (load), nebo _PRESET_MISS když není/režim."""
    global _PRESET_IDX
    if _PRESET_MODE != "load" or _PRESET_IDX >= len(_PRESET_DATA):
        return _PRESET_MISS
    item = _PRESET_DATA[_PRESET_IDX]
    _PRESET_IDX += 1
    if item.get("secret"):
        return _PRESET_MISS         # tajné se neukládají -> zeptat se / vzít z configu
    return item.get("a")


def _preset_record(kind, prompt, value):
    if _PRESET_MODE in ("save", "offer"):
        if _is_secret_prompt(prompt):
            _PRESET_REC.append({"k": kind, "secret": True})
        else:
            _PRESET_REC.append({"k": kind, "q": str(prompt)[:60], "a": value})


def preset_begin_save(cmd, path):
    global _PRESET_MODE, _PRESET_REC, _PRESET_CMD, _PRESET_PATH, _PRESET_SAVED
    _PRESET_MODE = "save"
    _PRESET_REC = []
    _PRESET_CMD = cmd
    _PRESET_PATH = path
    _PRESET_SAVED = False


def preset_begin_offer(cmd, path):
    """Jako save, ale uložení se na konci NABÍDNE (otázka), neukládá automaticky."""
    global _PRESET_MODE, _PRESET_REC, _PRESET_CMD, _PRESET_PATH, _PRESET_SAVED
    _PRESET_MODE = "offer"
    _PRESET_REC = []
    _PRESET_CMD = cmd
    _PRESET_PATH = path
    _PRESET_SAVED = False


def preset_begin_load(answers):
    global _PRESET_MODE, _PRESET_DATA, _PRESET_IDX
    _PRESET_MODE = "load"
    _PRESET_DATA = list(answers or [])
    _PRESET_IDX = 0


def _write_preset():
    with open(_PRESET_PATH, "w", encoding="utf-8") as f:
        json.dump({"command": _PRESET_CMD, "answers": _PRESET_REC},
                  f, ensure_ascii=False, indent=2)


def preset_flush_if_save():
    """Volá se těsně před spuštěním operace. Pro 'save' uloží rovnou, pro
    'offer' se zeptá, jestli volby uložit jako preset."""
    global _PRESET_SAVED
    if _PRESET_SAVED or not _PRESET_PATH:
        return
    if _PRESET_MODE == "save":
        try:
            _write_preset()
            _PRESET_SAVED = True
            log_done(f"Preset uložen do {_PRESET_PATH} (příště stačí --load).")
        except Exception as e:
            log_warn(f"Uložení presetu selhalo: {e}")
    elif _PRESET_MODE == "offer":
        _PRESET_SAVED = True   # ať se neptá dvakrát
        # raw vstup (mimo záznam), ať se otázka nedostane do presetu
        raw = input("Uložit tyto volby jako preset, ať příště stačí spustit skript bez ptaní? [a/N]: ").strip().lower()
        if raw in ("a", "y", "ano", "yes", "ja"):
            try:
                _write_preset()
                log_done(f"Preset uložen do {_PRESET_PATH}. Příště se spustí sám (smaž ho pro průvodce).")
            except Exception as e:
                log_warn(f"Uložení presetu selhalo: {e}")


def load_preset_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def default_preset_path():
    argv0 = sys.argv[0] if sys.argv and sys.argv[0] else None
    base = os.path.dirname(os.path.abspath(argv0)) if argv0 else os.getcwd()
    return os.path.join(base, "preset.json")


def resolve_preset_path(args):
    return getattr(args, "preset_file", None) or default_preset_path()


def ask_choice(prompt, options, allow_skip=True, allow_abort=True):
    """Jednoduchý textový interaktivní výběr v CLI. Vrací index (int),
    'skip', nebo 'abort'."""
    r = _preset_replay()
    if r is not _PRESET_MISS:
        if isinstance(r, int) and 0 <= r < len(options):
            return r
        if r in ("skip", "abort"):
            return r
        return 0
    while True:
        print(f"{Fore.YELLOW}{prompt}{Style.RESET_ALL}")
        for i, opt in enumerate(options, 1):
            print(f"  {i}) {opt}")
        if allow_skip:
            print("  s) přeskočit tento soubor")
        if allow_abort:
            print("  a) zrušit celý dávkový běh")
        choice = input("Tvoje volba: ").strip().lower()
        result = None
        if allow_skip and choice == "s":
            result = "skip"
        elif allow_abort and choice == "a":
            result = "abort"
        elif choice.isdigit() and 1 <= int(choice) <= len(options):
            result = int(choice) - 1
        if result is not None:
            _preset_record("choice", prompt, result)
            return result
        print(f"{Fore.RED}Neplatná volba, zkus to znovu.{Style.RESET_ALL}")


def try_list_tracks(mkvmerge_bin, video_path):
    """Jako mkvmerge_tracks(), ale nikdy neumírá (sys.exit) - pro dávkový
    pre-flight, kde chyba u jednoho souboru nemá zastavit celý běh."""
    try:
        result = subprocess.run([mkvmerge_bin, "-J", str(video_path)],
                                 capture_output=True, text=True, timeout=60)
        data = json.loads(result.stdout)
    except Exception as e:
        return None, None, str(e)
    subs, audio = [], []
    for track in data.get("tracks", []):
        props = track.get("properties", {})
        entry = {"id": track["id"], "codec": track.get("codec", "?"),
                  "lang": props.get("language", "und"), "title": props.get("track_name", "")}
        if track.get("type") == "subtitles":
            subs.append(entry)
        elif track.get("type") == "audio":
            audio.append(entry)
    return subs, audio, None


def preflight_check(video_path, mkvmerge_bin, args):
    """Ověří PŘED zpracováním, že video obsahuje stopy potřebné pro zvolený
    --audio-mode (+ --ref-lang/--track-id, --audio-lang/--audio-track-id).
    Vrací (ok: bool, problem: str, sub_tracks, audio_tracks)."""
    sub_tracks, audio_tracks, err = try_list_tracks(mkvmerge_bin, video_path)
    if err:
        return False, f"nelze přečíst stopy ({err})", [], []

    need_sub = args.audio_mode in ("off", "combine")
    need_aud = args.audio_mode in ("replace", "combine")
    problems = []

    if need_sub:
        if args.track_id is not None:
            if not any(t["id"] == args.track_id for t in sub_tracks):
                problems.append(f"titulková stopa s ID {args.track_id} neexistuje")
        else:
            text_tracks = [t for t in sub_tracks if is_text_codec(t["codec"])]
            if not text_tracks:
                problems.append("chybí použitelná textová titulková stopa")
            elif args.ref_lang and not any(
                    t["lang"].lower().startswith(args.ref_lang.lower()) for t in text_tracks):
                avail = ", ".join(t["lang"] for t in text_tracks) or "žádné"
                problems.append(f"chybí titulková stopa v jazyce '{args.ref_lang}' (dostupné: {avail})")

    if need_aud:
        if args.audio_track_id is not None:
            if not any(t["id"] == args.audio_track_id for t in audio_tracks):
                problems.append(f"zvuková stopa s ID {args.audio_track_id} neexistuje")
        else:
            if not audio_tracks:
                problems.append("chybí zvuková stopa")
            elif args.audio_lang and not any(
                    t["lang"].lower().startswith(args.audio_lang.lower()) for t in audio_tracks):
                avail = ", ".join(t["lang"] for t in audio_tracks) or "žádné"
                problems.append(f"chybí zvuková stopa v jazyce '{args.audio_lang}' (dostupné: {avail})")

    return (len(problems) == 0), "; ".join(problems), sub_tracks, audio_tracks


def resolve_preflight_problem(video_path, problem, sub_tracks, audio_tracks):
    """Interaktivně se zeptá, co dělat, když video nemá očekávané stopy.
    Vrací ('skip'|'abort'|'override', track_id_or_None, audio_track_id_or_None)."""
    log_warn(f"{os.path.basename(video_path)}: {problem}")
    if sub_tracks:
        print("  Dostupné titulkové stopy:")
        for t in sub_tracks:
            print(f"    ID={t['id']:>3}  jazyk={t['lang']:<5} kodek={t['codec']}")
    if audio_tracks:
        print("  Dostupné zvukové stopy:")
        for t in audio_tracks:
            print(f"    ID={t['id']:>3}  jazyk={t['lang']:<5} kodek={t['codec']}")

    choice = ask_choice("Co s tím?", ["Zadat konkrétní ID stopy/stop a zkusit to"])
    if choice in ("skip", "abort"):
        return choice, None, None

    t_raw = input("  ID titulkové stopy k použití (Enter = nezadávat): ").strip()
    a_raw = input("  ID zvukové stopy k použití (Enter = nezadávat): ").strip()
    t_id = int(t_raw) if t_raw.isdigit() else None
    a_id = int(a_raw) if a_raw.isdigit() else None
    return "override", t_id, a_id


def build_passthrough_args(args):
    """Sestaví CLI argumenty pro jednotlivé soubory z hodnot v args (mimo
    dávkové/batch-only a positional argumenty a track-id override, ty se
    řeší zvlášť per soubor)."""
    out = ["--audio-mode", args.audio_mode]
    out += ["--method", args.method]
    if args.ca_band is not None:
        out += ["--ca-band", str(args.ca_band)]
    if args.ca_snap_win is not None:
        out += ["--ca-snap-win", str(args.ca_snap_win)]
    if args.ca_min_sim is not None:
        out += ["--ca-min-sim", str(args.ca_min_sim)]
    if getattr(args, "translate", "off") and args.translate != "off":
        out += ["--translate", args.translate, "--pivot-lang", args.pivot_lang]
    if args.ref_lang:
        out += ["--ref-lang", args.ref_lang]
    if args.audio_lang:
        out += ["--audio-lang", args.audio_lang]
    out += ["--vad-percentile", str(args.vad_percentile)]
    out += ["--max-shift", str(args.max_shift)]
    out += ["--tolerance", str(args.tolerance)]
    if args.fix_short_duration:
        out += ["--fix-short-duration"]
    if args.reading_speed:
        out += ["--reading-speed", args.reading_speed]
    if args.min_cps is not None:
        out += ["--min-cps", str(args.min_cps)]
    if args.min_duration_floor is not None:
        out += ["--min-duration-floor", str(args.min_duration_floor)]
    if args.min_gap is not None:
        out += ["--min-gap", str(args.min_gap)]
    if args.line_overhead is not None:
        out += ["--line-overhead", str(args.line_overhead)]
    if args.mkvmerge:
        out += ["--mkvmerge", args.mkvmerge]
    if args.mkvextract:
        out += ["--mkvextract", args.mkvextract]
    if args.no_mkvtoolnix_download:
        out += ["--no-mkvtoolnix-download"]
    if args.ffmpeg:
        out += ["--ffmpeg", args.ffmpeg]
    if args.no_ffmpeg_download:
        out += ["--no-ffmpeg-download"]
    return out


def run_batch(args):
    directory = str(args.mkv) if args.mkv else "."
    if not os.path.isdir(directory):
        die(f"Není adresář: {directory}")

    global MKVMERGE, MKVEXTRACT
    if args.mkvmerge:
        MKVMERGE = args.mkvmerge
    if args.mkvextract:
        MKVEXTRACT = args.mkvextract
    mkvmerge_bin = args.mkvmerge or find_tool(["mkvmerge", "mkvmerge.exe"])
    if not mkvmerge_bin:
        mkvmerge_bin, _ = ensure_mkvtoolnix(directory, allow_download=not args.no_mkvtoolnix_download)
    if not mkvmerge_bin:
        die("mkvmerge nenalezen - nutný i jen pro náhled stop v dávkovém režimu (--all).")

    videos = collect_videos(directory, args.recursive)
    if not videos:
        log_warn(f"V '{directory}' nenalezeny žádné video soubory ({', '.join(sorted(VIDEO_EXTS_BATCH))}).")
        return
    srts = collect_srts(directory, args.recursive)
    log_info(f"Nalezeno {len(videos)} video souborů, {len(srts)} .srt souborů v '{directory}'.")

    plan = []
    skipped = []

    for v in videos:
        matches = match_srt_for_video(v, srts, args.target_lang)
        if not matches:
            log_warn(f"{os.path.basename(v)}: nenalezen odpovídající .srt - přeskočeno")
            skipped.append(v)
            continue

        if len(matches) > 1:
            choice = ask_choice(
                f"{os.path.basename(v)}: nalezeno {len(matches)} odpovídajících .srt - který patří sem?",
                [os.path.basename(m) for m in matches],
            )
            if choice == "skip":
                skipped.append(v)
                continue
            if choice == "abort":
                log_warn("Dávkový běh zrušen uživatelem.")
                return
            srt = matches[choice]
        else:
            srt = matches[0]

        ok, problem, sub_tracks, audio_tracks = preflight_check(v, mkvmerge_bin, args)
        override_track, override_audio = args.track_id, args.audio_track_id
        if not ok:
            if args.yes:
                log_warn(f"{os.path.basename(v)}: {problem} - PŘESKOČENO (--yes, bez dotazu).")
                skipped.append(v)
                continue
            action, t_id, a_id = resolve_preflight_problem(v, problem, sub_tracks, audio_tracks)
            if action == "skip":
                skipped.append(v)
                continue
            if action == "abort":
                log_warn("Dávkový běh zrušen uživatelem.")
                return
            if t_id is not None:
                override_track = t_id
            if a_id is not None:
                override_audio = a_id

        plan.append((v, srt, override_track, override_audio))

    print()
    if skipped:
        log_warn(f"Přeskočeno (chybí titulky/stopy): {len(skipped)}")
        for v in skipped:
            print(f"   - {os.path.basename(v)}")
    if not plan:
        log_warn("Nic ke zpracování.")
        return

    log_info(f"Ke zpracování: {len(plan)} souborů:")
    for v, s, *_ in plan:
        print(f"   {os.path.basename(v)}  <-  {os.path.basename(s)}")
    print()

    base_pass = build_passthrough_args(args)
    ok_count = 0
    fail_count = 0
    for v, s, t_id, a_id in plan:
        srt_path = Path(s)
        if args.overwrite:
            out_path = srt_path
            bak = srt_path.with_suffix(srt_path.suffix + ".bak")
            if not bak.exists():
                shutil.copy(srt_path, bak)
        else:
            out_path = srt_path.with_name(srt_path.stem + ".synced" + srt_path.suffix)

        extra = list(base_pass)
        if t_id is not None:
            extra += ["--track-id", str(t_id)]
        if a_id is not None:
            extra += ["--audio-track-id", str(a_id)]

        cmd = [sys.executable, os.path.abspath(__file__), str(v), str(srt_path), str(out_path)] + extra
        print(f"{Fore.CYAN}### {os.path.basename(v)}{Style.RESET_ALL}")
        result = subprocess.run(cmd)
        if result.returncode == 0:
            ok_count += 1
        else:
            fail_count += 1
            log_warn(f"Zpracování '{os.path.basename(v)}' selhalo (exit code {result.returncode}).")
        print()

    summary_color = Fore.GREEN if fail_count == 0 else Fore.YELLOW
    tail = f", {fail_count} selhalo" if fail_count else ""
    print(f"{summary_color}Hotovo: {ok_count}/{len(plan)} úspěšně{tail}.{Style.RESET_ALL}")


# ----------------------------------------------------------------------
# --fix-readability: samostatný dávkový režim, který NEDĚLÁ ŽÁDNOU
# synchronizaci - jen u titulků (které už mají SPRÁVNÉ časování) prodlouží
# příliš krátké zobrazení pro pohodlnější čtení. Nepotřebuje video ani
# mkvtoolnix/ffmpeg - pracuje čistě s .srt soubory.
#
# Bezpečnost časování: používá tu samou fix_short_durations(), jako
# jednosouborový režim výše - ta NIKDY neposouvá začátek titulku a nikdy
# neprodlouží konec za hranici (mezera do dalšího titulku - bezpečnostní
# rezerva), takže touto operací nelze rozbít existující časování ani
# způsobit překryv mezi titulky.
# ----------------------------------------------------------------------

def estimate_avg_cps(srt_files, sample_limit=5):
    """Pro orientaci uživateli při interaktivním dotazu: spočítá, jaké
    čtecí tempo (znaky/s) typicky MAJÍ aktuální titulky (jen u vět
    dost dlouhých na to, aby to bylo vypovídající - kratší než 0.3s
    se ignorují, často jde o překryvy/efekty)."""
    speeds = []
    for path in srt_files[:sample_limit]:
        try:
            events = parse_srt(Path(path))
        except SystemExit:
            continue
        for ev in events:
            dur = ev["end"] - ev["start"]
            chars = len(re.sub(r"\s+", "", ev["text"]))
            if dur >= 0.3 and chars >= 3:
                speeds.append(chars / dur)
    if not speeds:
        return None
    speeds.sort()
    return speeds[len(speeds) // 2]  # medián


def ask_readability_params(srt_files):
    """Interaktivně se zeptá na parametry prodlužování titulků, s jasným
    vysvětlením, k čemu každý slouží, a s orientačním údajem o aktuálním
    tempu titulků (pokud se podaří spočítat). Vrací (cps, floor, gap, line_overhead)."""
    print()
    print(f"{Fore.CYAN}Nastavení prodlužování titulků pro pohodlnější čtení{Style.RESET_ALL}")
    print(
        "Tohle NEMĚNÍ začátek žádného titulku a nikdy nezasáhne do dalšího "
        "titulku - jen tam, kde je volné místo (ticho/mezera), prodlouží konec "
        "zobrazení, pokud je text na danou dobu zobrazení příliš dlouhý. "
        "ČASOVÁNÍ MÁ VŽDY PŘEDNOST před tímto nastavením."
    )

    avg_cps = estimate_avg_cps(srt_files)
    if avg_cps:
        print(f"  (Pro srovnání: tvoje aktuální titulky mají typické tempo ~{avg_cps:.1f} znaků/s.)")
    print()

    print(
        "1) Čtecí rychlost - kolik znaků titulku má čtenář v průměru přečíst "
        "za 1 sekundu. Nižší číslo = titulky zůstanou na obrazovce déle. "
        "(Krátká slova/věty stejně nikdy nedostanou stejnou délku jako dlouhé "
        "víceřádkové věty - délka se vždy počítá podle skutečné délky textu.)"
    )
    preset_keys = list(READING_SPEED_PRESETS.keys())
    options = [f"{READING_SPEED_PRESETS[k][2]} ({READING_SPEED_PRESETS[k][0]:.0f} znaků/s)" for k in preset_keys]
    options.append("Zadat vlastní čtecí tempo (znaků/s)")
    choice = ask_choice("Zvol cílové čtecí tempo:", options, allow_skip=False, allow_abort=True)
    if choice == "abort":
        return None

    if choice == len(preset_keys):
        raw = input("  Zadej čtecí tempo ve znacích/s (např. 15): ").strip()
        try:
            min_cps = float(raw)
        except ValueError:
            min_cps = DEFAULT_MIN_CPS
        default_floor = DEFAULT_MIN_DURATION_FLOOR
    else:
        min_cps, default_floor, _label = READING_SPEED_PRESETS[preset_keys[choice]]

    print()
    print(
        "2) Minimální délka zobrazení (s) - i jedno krátké slovo se zobrazí "
        f"alespoň takhle dlouho, bez ohledu na čtecí tempo výše. Enter = doporučená {default_floor}."
    )
    raw = input(f"  Minimální délka v sekundách [{default_floor}]: ").strip()
    try:
        min_floor = float(raw) if raw else default_floor
    except ValueError:
        min_floor = default_floor

    print()
    print(
        "3) Bezpečnostní mezera (s) před dalším titulkem, kterou prodloužení "
        f"nikdy nepřekročí (aby se titulky nezačaly překrývat). Enter = výchozí {DEFAULT_MIN_GAP}."
    )
    raw = input(f"  Mezera v sekundách [{DEFAULT_MIN_GAP}]: ").strip()
    try:
        min_gap = float(raw) if raw else DEFAULT_MIN_GAP
    except ValueError:
        min_gap = DEFAULT_MIN_GAP

    print()
    print(
        "4) Příplatek za řádek (s) - kolik sekund navíc dostane titulek za KAŽDÝ "
        "řádek nad první (oči musí přeskočit na další řádek). Díky tomu dvouřádková "
        f"věta dostane víc času než jednoslovný titulek se stejným počtem znaků. Enter = výchozí {DEFAULT_LINE_OVERHEAD}."
    )
    raw = input(f"  Příplatek za řádek v sekundách [{DEFAULT_LINE_OVERHEAD}]: ").strip()
    try:
        line_overhead = float(raw) if raw else DEFAULT_LINE_OVERHEAD
    except ValueError:
        line_overhead = DEFAULT_LINE_OVERHEAD

    return min_cps, min_floor, min_gap, line_overhead


# ----------------------------------------------------------------------
# Detekce jazyka z OBSAHU titulků (ne z přípon/tagů)
# ----------------------------------------------------------------------

_LANG_ALIASES = {
    "cze": "cs", "ces": "cs", "cz": "cs", "slk": "sk", "slo": "sk",
    "ger": "de", "deu": "de", "ger": "de", "eng": "en", "fre": "fr", "fra": "fr",
    "spa": "es", "ita": "it", "por": "pt", "dut": "nl", "nld": "nl",
    "rus": "ru", "ukr": "uk", "hun": "hu", "pol": "pl", "rum": "ro", "ron": "ro",
}


def norm_lang(code):
    if not code:
        return None
    c = str(code).strip().lower()
    if c in ("und", "unknown", ""):
        return None
    return _LANG_ALIASES.get(c, c[:2])


_LANG_STOPWORDS = {
    "cs": "a aby ale ani ano až bez bude budou by byl byla bylo být co či do i jak jako je jeho její jen ještě již k kde když ke která které kteří který má mě mi mít my na nad naše ne nebo není než nic o od on ona oni pak po pod podle pro proč protože při s se si tak také tam ten tedy to toho tom ty u už v vám váš ve však všechno vy z za ze že jsem jsi jsme jste jsou",
    "sk": "a aby ale ani áno až bez bude budú by bol bola bolo byť čo či do i ja ako je jeho jej len ešte už k kde keď ku ktorá ktoré ktorí ktorý má ma mi mať my na nad naša nie alebo nič o od on ona oni potom po pod podľa pre prečo pretože pri s sa si tak tiež tam ten teda to toho tom ty u už v vám váš vo viac však všetko vy z za zo že som sme ste sú",
    "en": "the a an and or but if then is are was were be been to of in on at for with from by as that this these those it he she they we you i me my your his her their our not no yes do does did have has had will would can could should about into over than when what which who",
    "de": "der die das und oder aber wenn dann ist sind war waren sein zu von in an auf für mit aus durch als dass dieser diese dieses es er sie wir ihr ich mich mein dein nicht kein ja doch noch schon auch nur wie wo wann warum weil über unter haben hat",
    "pl": "i a ale lub jeśli wtedy jest są był była było być do z w na o od po pod dla że ten ta to te oni my wy ja mnie mój nie tak czy już jeszcze jak gdzie kiedy dlaczego bo nad przez także również wszystko",
    "es": "el la los las un una unos unas y o pero si entonces es son era fue ser estar de en por para con sin que este esta esto estos ellos nosotros yo me mi tu su no sí ya como donde cuando porque más muy",
    "fr": "le la les un une des et ou mais si alors est sont était être de en pour avec sans que ce cette ces ils nous vous je me mon ton son ne pas plus très comme où quand parce qui",
    "it": "il lo la i gli le un uno una e o ma se allora è sono era essere di in per con senza che questo questa questi quelli noi voi io mi mio tuo suo non sì già come dove quando perché più molto",
    "pt": "o a os as um uma uns umas e ou mas se então é são era ser estar de em por para com sem que este esta isto estes eles nós eu me meu teu seu não sim já como onde quando porque mais muito",
    "nl": "de het een en of maar als dan is zijn was waren te van in op voor met uit door dat deze dit die zij wij jij ik mij mijn niet geen ja nog al ook hoe waar wanneer waarom omdat",
    "ru": "и а но или если то это есть был была было быть в на с по от до для что как эти они мы вы я мне мой не да уже еще где когда почему потому очень так",
    "uk": "і та але або якщо то це є був була було бути в на з по від до для що як ці вони ми ви я мені мій не так вже ще де коли чому тому дуже",
    "hu": "a az és vagy de ha akkor van vannak volt lenni hogy ez ezek ők mi ti én nekem nem igen már még hol mikor miért mert nem is",
}
_LANG_STOPWORDS = {k: set(v.split()) for k, v in _LANG_STOPWORDS.items()}


def _builtin_detect(text):
    low = text.lower()
    letters = [c for c in low if c.isalpha()]
    if not letters:
        return None
    cyr = sum(1 for c in letters if "\u0400" <= c <= "\u04ff")
    if cyr > 0.3 * len(letters):
        return "uk" if any(c in low for c in "іїєґ") else "ru"
    toks = re.findall(r"[^\W\d_]+", low, re.UNICODE)
    if not toks:
        return None
    scores = {lang: sum(1 for t in toks if t in sw) for lang, sw in _LANG_STOPWORDS.items()}
    # boosty podle charakteristických znaků
    if any(c in low for c in "řěů"):
        scores["cs"] += 6
    if any(c in low for c in "ľĺŕôä"):
        scores["sk"] += 5
    if any(c in low for c in "łżśźćń"):
        scores["pl"] += 6
    if "ß" in low:
        scores["de"] += 3
    if any(c in low for c in "ñ¿¡"):
        scores["es"] += 3
    if any(c in low for c in "çœ"):
        scores["fr"] += 2
    if any(c in low for c in "őű"):
        scores["hu"] += 5
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else None


def detect_language(text, sample_chars=6000):
    """Detekuje jazyk z OBSAHU textu. Preferuje 'langdetect' (pip install
    langdetect), pokud je nainstalován; jinak vestavěný detektor (cs/sk/pl/en/
    de/fr/es/it/pt/nl/ru/uk/hu...). Vrací 2písmenný kód nebo None."""
    sample = (text or "")[:sample_chars]
    if len(sample.strip()) < 10:
        return None
    try:
        from langdetect import detect, DetectorFactory
        DetectorFactory.seed = 0
        return norm_lang(detect(sample))
    except Exception:
        pass
    return norm_lang(_builtin_detect(sample))


def detect_sub_language(events, max_lines=400):
    """Detekuje jazyk z titulkových eventů (vzorek prvních max_lines řádků)."""
    txt = " ".join(e["text"] for e in events[:max_lines] if e.get("text"))
    return detect_language(txt)


def detect_srt_file_language(path):
    try:
        return detect_sub_language(parse_srt(Path(path), strict=False))
    except Exception:
        return None


def detect_lang_tags(srt_files):
    """Odhadne jazykové/jiné tagy z názvů souborů ('epizoda.cs.srt' -> 'cs').
    Jen orientační - používá se pro interaktivní nabídku, ne pro tvrdé filtrování."""
    tags = set()
    for s in srt_files:
        stem = Path(s).stem
        parts = stem.rsplit(".", 1)
        if len(parts) == 2 and 1 <= len(parts[1]) <= 8 and parts[1].isalpha():
            tags.add(parts[1].lower())
    return sorted(tags)


def filter_by_tag(srt_files, tag):
    def has_tag(p):
        stem = Path(p).stem
        parts = stem.rsplit(".", 1)
        return len(parts) == 2 and parts[1].lower() == tag.lower()
    tagged = [s for s in srt_files if has_tag(s)]
    return tagged if tagged else srt_files


def ask_yes_no(prompt, default_no=True):
    r = _preset_replay()
    if r is not _PRESET_MISS:
        return bool(r)
    suffix = "[a/N]" if default_no else "[A/n]"
    raw = input(f"{prompt} {suffix}: ").strip().lower()
    val = (not default_no) if not raw else (raw in ("a", "y", "ano", "yes", "ja"))
    _preset_record("yesno", prompt, val)
    return val


def run_fix_readability(args):
    interactive = not args.yes
    print()
    if interactive and not args.mkv:
        print(f"{Fore.CYAN}--fix-readability: úprava délky zobrazení titulků (bez ovlivnění časování){Style.RESET_ALL}")
        print("Postupně se zeptám na pár věcí - cokoliv je možné zadat i přímo jako parametr příkazu, "
              "abys to příště nemusel/a vyplňovat znovu (viz --help).")
        print()

    # 1) adresář nebo konkrétní .srt soubor ----------------------------------
    if args.mkv:
        target = str(args.mkv)
    elif interactive:
        print("1) Co zpracovat - adresář k prohledání, nebo přímo konkrétní .srt soubor.")
        raw = input("   Cesta [. = aktuální adresář]: ").strip()
        target = raw if raw else "."
        print()
    else:
        target = "."

    if os.path.isfile(target) and target.lower().endswith(".srt"):
        srt_files = [target]
        is_single_file = True
    elif os.path.isdir(target):
        is_single_file = False
        # 2) rekurzivní hledání ------------------------------------------------
        recursive = args.recursive
        if interactive and not args.recursive:
            print("2) Mám prohledat i podadresáře, nebo jen tento jeden adresář?")
            recursive = ask_yes_no("   Prohledat i podadresáře?", default_no=True)
            print()
        srt_files = collect_srts(target, recursive)
    else:
        die(f"Není to ani .srt soubor, ani adresář: {target}")

    if not srt_files:
        vids = [] if is_single_file else collect_videos(target, recursive)
        if vids and (args.yes or ask_yes_no(
                f"Nenašel jsem žádné .srt, ale je tu {len(vids)} videí. Vytáhnout z nich titulky?",
                default_no=False)):
            _saved = args.mkv
            args.mkv = Path(target)
            try:
                run_extract_subs(args, minimal=True)
            finally:
                args.mkv = _saved
            srt_files = collect_srts(target, recursive)
            if srt_files:
                log_info("Titulky vytažené - teď na ně použiju čitelnost.")
        if not srt_files:
            log_warn("Nenalezeny žádné .srt soubory ke zpracování.")
            return

    # 3) jazykový/jiný filtr - jen když je víc variant a uživatel nezadal -----
    target_lang = args.target_lang
    if not is_single_file and target_lang is None and interactive and len(srt_files) > 1:
        tags = detect_lang_tags(srt_files)
        if len(tags) > 1:
            print(f"3) Nalezeno {len(srt_files)} .srt souborů s různými jazykovými tagy v názvu ({', '.join(tags)}).")
            options = [f"jen '{t}'" for t in tags] + ["všechny (nefiltrovat)"]
            choice = ask_choice("   Co zpracovat?", options, allow_skip=False, allow_abort=True)
            if choice == "abort":
                log_warn("Zrušeno uživatelem.")
                return
            if choice < len(tags):
                target_lang = tags[choice]
            print()
    if target_lang:
        srt_files = filter_by_tag(srt_files, target_lang)

    log_info(f"Ke zpracování: {len(srt_files)} .srt souborů.")

    # 4) přepsat originál, nebo uložit jako nový soubor -----------------------
    overwrite = args.overwrite
    if not args.overwrite and interactive:
        print()
        print("4) Jak uložit výsledek?")
        choice = ask_choice(
            "   Zvol režim uložení:",
            ["Nový soubor '<jméno>.readability.srt' vedle originálu (doporučeno - nic se nepřepíše)",
             "Přepsat originál přímo (jednorázově se vytvoří '.bak' záloha)"],
            allow_skip=False, allow_abort=True,
        )
        if choice == "abort":
            log_warn("Zrušeno uživatelem.")
            return
        overwrite = (choice == 1)
        print()

    # 5) parametry čtecí rychlosti ---------------------------------------------
    has_explicit_speed_choice = (
        args.reading_speed is not None or args.min_cps is not None or args.min_duration_floor is not None
    )
    if not has_explicit_speed_choice and interactive:
        result = ask_readability_params(srt_files)
        if result is None:
            log_warn("Zrušeno uživatelem.")
            return
        min_cps, min_floor, min_gap, line_overhead = result
    else:
        min_cps, min_floor, min_gap, line_overhead = resolve_speed_params(args)

    print()
    log_info(
        f"Používám: čtecí tempo {min_cps:.1f} znaků/s, min. délka {min_floor:.2f}s, "
        f"mezera {min_gap:.3f}s, příplatek za řádek {line_overhead:.2f}s, "
        f"výstup: {'přepsat originál (+.bak)' if overwrite else '*.readability.srt'}"
    )
    print()

    changed = 0
    unchanged = 0
    failed = 0

    for srt_file in srt_files:
        name = os.path.basename(srt_file)
        try:
            events = parse_srt(Path(srt_file))
        except SystemExit:
            log_warn(f"{name}: nepodařilo se načíst (přeskočeno)")
            failed += 1
            continue

        fixed, n_extended = fix_short_durations(
            events, min_cps=min_cps, min_duration_floor=min_floor, min_gap=min_gap, line_overhead=line_overhead
        )

        if n_extended == 0:
            print(f"  = {name} - beze změny")
            unchanged += 1
            continue

        srt_path = Path(srt_file)
        if overwrite:
            bak = srt_path.with_suffix(srt_path.suffix + ".bak")
            if not bak.exists():
                shutil.copy(srt_path, bak)
            out_path = srt_path
        else:
            out_path = srt_path.with_name(srt_path.stem + ".readability" + srt_path.suffix)

        write_srt(fixed, out_path)
        print(f"  {Fore.GREEN}+{Style.RESET_ALL} {name} - prodlouženo {n_extended} titulků -> {out_path.name}")
        changed += 1

    print()
    tail = f", {failed} selhalo" if failed else ""
    log_done(f"Hotovo: {changed} upraveno, {unchanged} beze změny{tail} (z {len(srt_files)}).")


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# Extrakce + překlad titulků do nového jazyka a uložení .srt (--translate-subs)
# ----------------------------------------------------------------------
#
# Pro každé video v adresáři: vytáhne zvolenou titulkovou stopu, získá titulky
# v CÍLOVÉM jazyce a uloží je jako <video>.<lang>.srt. Kvalitu řeší dvěma
# cestami (lze kombinovat):
#   1) OpenSubtitles - stáhne HOTOVÉ lidské titulky v cílovém jazyce (nejlepší
#      kvalita), volitelně je časově srovná s videem naším synchronizačním
#      jádrem (affine).
#   2) Strojový překlad extrahované stopy (DeepL = nejlepší kvalita, Google,
#      nebo offline Argos) + korektura (pravidlové očištění zdarma, volitelně
#      AI korektura přes OpenAI-kompatibilní API).
# U strojového překladu zůstává PŮVODNÍ ČASOVÁNÍ (překládá se jen text), takže
# výsledek sedí na video bez další synchronizace.


def _resolve_tools_for_extract(args, video):
    is_mkv = video.suffix.lower() in MKVEXTRACT_CONTAINER_EXTS
    mkvmerge_bin = getattr(args, "mkvmerge", None) or find_tool(["mkvmerge", "mkvmerge.exe"])
    mkvextract_bin = getattr(args, "mkvextract", None) or find_tool(["mkvextract", "mkvextract.exe"])
    if not mkvmerge_bin or (is_mkv and not mkvextract_bin):
        try:
            mkvmerge_bin, mkvextract_bin = ensure_mkvtoolnix(
                str(video.parent), allow_download=not getattr(args, "no_mkvtoolnix_download", False))
        except Exception:
            pass
    ffmpeg_bin = None
    if not is_mkv:
        ffmpeg_bin = getattr(args, "ffmpeg", None) or find_tool(["ffmpeg", "ffmpeg.exe"])
        if not ffmpeg_bin:
            try:
                ffmpeg_bin = ensure_ffmpeg(str(video.parent), allow_download=not getattr(args, "no_ffmpeg_download", False))
            except Exception:
                pass
    return mkvmerge_bin, mkvextract_bin, ffmpeg_bin, is_mkv


def extract_subtitle_events(args, video, track_id=None, ref_lang=None):
    """Vytáhne zvolenou titulkovou stopu do events (track_id > ref_lang > první).
    Vrací (events, chosen_track) nebo (None, None). Neukončuje běh při chybě."""
    video = Path(video)
    mkvmerge_bin, mkvextract_bin, ffmpeg_bin, is_mkv = _resolve_tools_for_extract(args, video)
    if not mkvmerge_bin:
        log_warn(f"{video.name}: mkvmerge nenalezen - přeskakuji.")
        return None, None
    try:
        sub_tracks = mkvmerge_tracks(mkvmerge_bin, video, "subtitles")
    except (Exception, SystemExit) as e:
        log_warn(f"{video.name}: nelze přečíst stopy ({e}) - přeskakuji.")
        return None, None
    if not sub_tracks:
        log_warn(f"{video.name}: žádné titulkové stopy.")
        return None, None
    try:
        chosen = pick_reference_track(sub_tracks, ref_lang, track_id)
    except SystemExit:
        chosen = sub_tracks[0]
    except Exception:
        chosen = sub_tracks[0]
    tmpd = tempfile.mkdtemp()
    try:
        outp = Path(tmpd) / "track.srt"
        if is_mkv:
            if not mkvextract_bin:
                log_warn(f"{video.name}: mkvextract nenalezen.")
                return None, None
            extract_subtitle_to_srt(mkvextract_bin, video, chosen["id"], outp)
        else:
            if not ffmpeg_bin:
                log_warn(f"{video.name}: ffmpeg nenalezen (extrakce z MP4).")
                return None, None
            pos = [t["id"] for t in sub_tracks].index(chosen["id"])
            extract_subtitle_via_ffmpeg(ffmpeg_bin, video, pos, outp)
        ev = parse_srt(outp, strict=False)
        # Fallback: stopa nemusí být SubRip (ASS/SSA/jiné kódování) - když je
        # výsledek prázdný a máme ffmpeg, necháme ho převést na SRT.
        if not ev and ffmpeg_bin:
            try:
                pos = [t["id"] for t in sub_tracks].index(chosen["id"])
                outp2 = Path(tmpd) / "track_ff.srt"
                extract_subtitle_via_ffmpeg(ffmpeg_bin, video, pos, outp2)
                ev = parse_srt(outp2, strict=False)
            except Exception:
                pass
        if not ev:
            return None, chosen
        return ev, chosen
    except (Exception, SystemExit) as e:
        log_warn(f"{video.name}: extrakce stopy selhala: {e}")
        return None, None
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def _track_tag(track, all_text_tracks):
    """Deterministické pojmenování: první stopa daného jazyka -> '<jazyk>',
    další téhož jazyka -> '<jazyk>.2', '.3'... (nezávislé na pořadí extrakce)."""
    lang = track["lang"] if track.get("lang") and track["lang"] != "und" else f"track{track['id']}"
    same = [t for t in all_text_tracks if (t.get("lang") or "") == (track.get("lang") or "")]
    if len(same) <= 1:
        return lang
    try:
        pos = same.index(track) + 1
    except ValueError:
        pos = 1
    return lang if pos == 1 else f"{lang}.{pos}"


def probe_text_subtitle_tracks(args, video):
    """Bezpečně (nikdy neumře) vrátí seznam TEXTOVÝCH titulkových stop videa."""
    mm = getattr(args, "mkvmerge", None)
    if not mm:
        mm, me, ff, _ = _resolve_tools_for_extract(args, Path(video))
        if mm:
            args.mkvmerge = args.mkvmerge or mm
    if not mm:
        return []
    try:
        tracks = mkvmerge_tracks(mm, Path(video), "subtitles")
    except (Exception, SystemExit):
        return []
    return [t for t in tracks if is_text_codec(t["codec"])]


def extract_with_fallback(args, video, initial_track, text_tracks, done_ids=None,
                          interactive=True):
    """Vytáhne 'initial_track'; když stopa selže (prázdná/nečitelná/obrázková) a
    jsme interaktivně, NASKENUJE video a nabídne jinou stopu k výběru (nebo
    přeskočení). Vrací (events, chosen_track) nebo (None, None).
    Nastavuje args._extract_skip_prompts, když uživatel zvolí 'už se neptat'."""
    done_ids = done_ids or set()
    tried = set()
    track = initial_track
    vname = Path(video).name
    while track is not None:
        tried.add(track["id"])
        events, chosen = extract_subtitle_events(args, video, track_id=track["id"])
        if events:
            return events, (chosen or track)

        # stopa selhala
        if not interactive or getattr(args, "_extract_skip_prompts", False):
            log_warn(f"{vname}: stopa #{track['id']} ({track.get('lang', '?')}) "
                     "je prázdná/nečitelná - přeskakuji.")
            return None, None

        alts = [t for t in text_tracks if t["id"] not in tried and t["id"] not in done_ids]
        log_warn(f"{vname}: stopa #{track['id']} ({track.get('lang', '?')}) je prázdná nebo "
                 "nečitelná (možná obrázkové titulky nebo poškozená).")
        labels = [f"#{t['id']}  {t['lang']:4} {t['codec']}  {t.get('title', '')}".rstrip() for t in alts]
        labels += ["přeskočit toto video", "u dalších videí se už neptat (jen přeskakovat)"]
        i = ask_pick(f"{vname}: zkusit jinou titulkovou stopu?", labels,
                     default=0 if alts else len(alts),
                     help=([f"Vytáhne místo toho stopu #{t['id']} ({t['lang']}, {t['codec']})."
                            for t in alts]
                           + ["Tohle video přeskočí (žádný .srt se z něj neuloží).",
                              "U všech dalších selhaných stop se už nebude ptát a rovnou je přeskočí."]))
        if i < len(alts):
            track = alts[i]
        elif i == len(alts):
            return None, None
        else:
            args._extract_skip_prompts = True
            return None, None
    return None, None


def clean_subtitle_text(text, max_line=42):
    """Pravidlová korektura: sjednotí mezery, opraví mezery před interpunkcí a
    přebytečné prázdné řádky, a dlouhý jednořádkový text rozlomí na 2 řádky."""
    t = text.replace("\r", "")
    lines = [l.strip() for l in t.split("\n") if l.strip()]
    t = " ".join(lines)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s+([,.!?;:…])", r"\1", t)
    t = re.sub(r"([¿¡])\s+", r"\1", t)
    t = re.sub(r"\.{3,}", "…", t)
    if len(t) > max_line and " " in t:
        words = t.split(" ")
        target = len(t) / 2.0
        best_i, best_d, acc = None, None, 0
        for i, w in enumerate(words[:-1]):
            acc += len(w) + 1
            d = abs(acc - target)
            if best_d is None or d < best_d:
                best_d, best_i = d, i
        l1 = " ".join(words[:best_i + 1])
        l2 = " ".join(words[best_i + 1:])
        if l1 and l2:
            t = l1 + "\n" + l2
    return t


_SENT_ENDERS = ".!?…:"


def _looks_continuation(cur, nxt, gap, max_gap=2.0):
    """Je 'nxt' pokračováním věty z 'cur'? (pro spojení fragmentů před překladem)"""
    c = (cur or "").replace("\n", " ").strip()
    n = (nxt or "").replace("\n", " ").strip()
    if not c or not n or gap > max_gap:
        return False
    if n[:1] in "-–—":          # nový mluvčí (pomlčka) -> nespojovat
        return False
    last = c[-1]
    if last in _SENT_ENDERS:
        return False
    if last in "\"”»)]" and len(c) >= 2 and c[-2] in _SENT_ENDERS:
        return False
    return True


def _merge_sentence_groups(events, max_group=4, max_gap=2.0):
    """Seskupí za sebou jdoucí titulky, které tvoří jednu větu."""
    groups = []
    i, n = 0, len(events)
    while i < n:
        grp = [i]
        while (len(grp) < max_group and i + 1 < n
               and _looks_continuation(events[i]["text"], events[i + 1]["text"],
                                       events[i + 1]["start"] - events[i]["end"], max_gap)):
            i += 1
            grp.append(i)
        groups.append(grp)
        i += 1
    return groups


def _split_translation(translated, parts):
    """Rozdělí přeloženou větu zpět na len(parts) kusů úměrně délce originálů,
    na hranicích slov (aby časování zůstalo, ale text seděl na původní řádky)."""
    translated = (translated or "").strip()
    if len(parts) == 1:
        return [translated]
    words = translated.split()
    if not words:
        return [translated] + [""] * (len(parts) - 1)
    total = sum(max(1, len(p.replace("\n", " ").strip())) for p in parts)
    out, wi, nw = [], 0, len(words)
    acc = 0.0
    for k, p in enumerate(parts):
        if k == len(parts) - 1:
            out.append(" ".join(words[wi:]))
            break
        acc += max(1, len(p.replace("\n", " ").strip())) / total
        tw = int(round(acc * nw))
        tw = max(wi + 1, min(tw, nw - (len(parts) - 1 - k)))
        out.append(" ".join(words[wi:tw]))
        wi = tw
    return out


def translate_events_to(events, engine, target_lang, api_key=None, model=None, sentence_aware=True):
    """Přeloží text eventů do target_lang (časování beze změny). Vrací nové
    events, nebo None když překladač není k dispozici.
    sentence_aware=True: spojí větné fragmenty roztržené přes víc titulků,
    přeloží je jako CELOU větu (kvalitnější a s kontextem) a rozdělí zpět."""
    tr = make_translator(engine, target_lang, api_key=api_key, model=model)
    if tr is None:
        return None

    if not sentence_aware:
        out = tr([e["text"].replace("\n", " ") for e in events])
        return [{"start": e["start"], "end": e["end"], "text": (o or e["text"])}
                for e, o in zip(events, out)]

    groups = _merge_sentence_groups(events)
    merged = [" ".join(events[i]["text"].replace("\n", " ").strip() for i in g) for g in groups]
    translated = tr(merged)
    result = [{"start": e["start"], "end": e["end"], "text": e["text"]} for e in events]
    for g, tsent in zip(groups, translated):
        parts = [events[i]["text"] for i in g]
        if not tsent:
            continue  # překlad selhal -> ponech originál
        pieces = _split_translation(tsent, parts)
        for idx, piece in zip(g, pieces):
            if piece.strip():
                result[idx]["text"] = piece.strip()
    return result


def anthropic_messages(prompt, api_key, model, max_tokens=4000, timeout=180):
    """Jedno volání Anthropic Messages API. Vrací text odpovědi nebo None.
    Při 4xx vyhodí _FatalAPIError s konkrétní zprávou (nemá smysl opakovat)."""
    import urllib.request
    import urllib.error
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    body = json.dumps({"model": model, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                                 data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = _http_error_detail(e)
        msg = f"HTTP {e.code}: {detail or e.reason}"
        if e.code in (400, 401, 403, 404):
            raise _FatalAPIError(msg)
        raise RuntimeError(msg)
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "".join(parts) if parts else None


_ANTHROPIC_MODEL_HINTS = (
    ("opus", "nejvýkonnější (nejnáročnější úlohy), nejdražší za tokeny"),
    ("fable", "špičkový model pro velmi náročné úlohy"),
    ("sonnet", "vyvážený poměr kvalita/cena/rychlost - doporučeno"),
    ("haiku", "nejrychlejší a nejlevnější (jednoduché úlohy)"),
)
_ANTHROPIC_STATIC_MODELS = [
    ("claude-opus-4-8", "Claude Opus 4.8"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
    ("claude-haiku-4-5", "Claude Haiku 4.5"),
    ("claude-opus-4-5-20251101", "Claude Opus 4.5 (pinned)"),
    ("claude-sonnet-4-5-20250929", "Claude Sonnet 4.5 (pinned)"),
]


def _anthropic_model_hint(mid):
    m = (mid or "").lower()
    for k, v in _ANTHROPIC_MODEL_HINTS:
        if k in m:
            return v
    return ""


def anthropic_list_models(api_key, timeout=30):
    """Vrátí seznam modelů dostupných pro daný klíč (Anthropic /v1/models)."""
    import urllib.request
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    req = urllib.request.Request("https://api.anthropic.com/v1/models?limit=100",
                                 headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data.get("data", [])


def anthropic_model_info(api_key, model_id, timeout=15):
    """Detail modelu vč. token limitů (max_input_tokens, max_tokens)."""
    import urllib.request
    from urllib.parse import quote
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    req = urllib.request.Request(f"https://api.anthropic.com/v1/models/{quote(model_id)}",
                                 headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _print_anthropic_models(args=None):
    key = (getattr(args, "anthropic_key", None) if args else None) or os.environ.get("ANTHROPIC_API_KEY")
    models = None
    if key:
        try:
            print(f"{Fore.CYAN}Načítám seznam modelů z Anthropic API...{Style.RESET_ALL}")
            models = anthropic_list_models(key)
        except Exception as e:
            log_warn(f"Online seznam se nepodařilo načíst ({e}). Ukážu vestavěný přehled.")
    else:
        log_info("Anthropic klíč není nastaven - ukážu vestavěný přehled (online seznam vyžaduje klíč).")

    print(f"{Fore.MAGENTA}Dostupné modely Claude:{Style.RESET_ALL}")
    if models:
        for m in models:
            mid = m.get("id", "?")
            name = m.get("display_name", "")
            info = ""
            if key:
                try:
                    d = anthropic_model_info(key, mid)
                    ctx = d.get("max_input_tokens")
                    out = d.get("max_tokens")
                    if ctx or out:
                        info = f" | kontext {ctx}, max. výstup {out} tok."
                except Exception:
                    pass
            hint = _anthropic_model_hint(mid)
            print(f"  {mid:<30}{name}{info}" + (f"  [{hint}]" if hint else ""))
    else:
        for mid, name in _ANTHROPIC_STATIC_MODELS:
            hint = _anthropic_model_hint(mid)
            print(f"  {mid:<30}{name}" + (f"  [{hint}]" if hint else ""))
    print(f"{Fore.CYAN}Pozn.: Haiku = nejlevnější/nejrychlejší, Sonnet = vyvážené, Opus = "
          f"nejdražší/nejvýkonnější. Přesné ceny za tokeny viz anthropic.com/pricing.{Style.RESET_ALL}")


def _subtitle_translate_prompt(batch, target_lang):
    """Kvalitní prompt pro překlad titulků (laděno hlavně na češtinu)."""
    numbered = "\n".join(f"{k + 1}. {t.replace(chr(10), ' / ')}" for k, t in enumerate(batch))
    lang_note = ""
    if target_lang in ("cs", "sk"):
        lang_note = (" Používej přirozenou, hovorovou " + ("češtinu" if target_lang == "cs" else "slovenštinu")
                     + ", správnou diakritiku a interpunkci. Udrž konzistentní oslovování (tykání/vykání) "
                     "podle kontextu scény. Jména postav a názvy NEPŘEKLÁDEJ.")
    return (f"Jsi zkušený překladatel filmových a seriálových titulků. Přelož následující "
            f"číslované řádky do jazyka '{target_lang}'. Překládej PŘIROZENĚ a IDIOMATICKY "
            f"(ne doslovně), zachovej význam, tón, rejstřík i humor.{lang_note} "
            "Řádky jdou po sobě jako souvislý dialog - využij kontext, ale zachovej STEJNÝ "
            "POČET a POŘADÍ položek. Víceřádkový titulek odděl ' / '. Vrať POUZE číslované "
            "řádky ve tvaru 'číslo. překlad', bez uvozovek a bez jakýchkoli komentářů.\n\n" + numbered)


def anthropic_translate_batch(batch, target_lang, api_key, model):
    """Přeloží dávku řádků titulků do target_lang přes Claude. Vrací seznam
    stejné délky (None u nezdaru). Fatální chyby (4xx) propaguje výš."""
    prompt = _subtitle_translate_prompt(batch, target_lang)
    try:
        content = anthropic_messages(prompt, api_key, model)
    except _FatalAPIError:
        raise
    except Exception:
        return [None] * len(batch)
    if not content:
        return [None] * len(batch)
    parsed = _parse_numbered(content, len(batch))
    return [(p.replace(" / ", "\n") if p else None) for p in parsed]


def gemini_generate(prompt, api_key, model="gemini-2.5-flash", timeout=120):
    """Jedno volání Google Gemini (generativelanguage). Vrací text nebo None.
    Při 4xx vyhodí _FatalAPIError (neopakovat)."""
    import urllib.request
    import urllib.error
    from urllib.parse import quote
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{quote(model)}"
           f":generateContent?key={quote(api_key)}")
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = _http_error_detail(e)
        msg = f"HTTP {e.code}: {detail or e.reason}"
        if e.code in (400, 401, 403, 404):
            raise _FatalAPIError(msg)
        if e.code == 429 and ("limit: 0" in detail or "free_tier" in detail.lower() or "quota" in detail.lower()):
            raise _FatalAPIError(msg + "  → Tenhle model nemá pro tvůj účet/region bezplatnou kvótu. "
                                 "Zkus jiný model ('?' u modelu, nebo --gemini-model, např. "
                                 "gemini-1.5-flash), nebo použij engine 'google' (zcela zdarma bez klíče).")
        raise RuntimeError(msg)
    cands = data.get("candidates", [])
    if not cands:
        return None
    parts = cands[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts)
    return text or None


def gemini_translate_batch(batch, target_lang, api_key, model):
    """Přeloží dávku titulků do target_lang přes Gemini (stejný prompt jako Claude)."""
    prompt = _subtitle_translate_prompt(batch, target_lang)
    try:
        content = gemini_generate(prompt, api_key, model)
    except _FatalAPIError:
        raise
    except Exception:
        return [None] * len(batch)
    if not content:
        return [None] * len(batch)
    parsed = _parse_numbered(content, len(batch))
    return [(p.replace(" / ", "\n") if p else None) for p in parsed]


_GEMINI_STATIC_MODELS = [
    ("gemini-2.5-flash", "rychlý, kvalitní"),
    ("gemini-2.0-flash-lite", "nejlevnější/nejrychlejší"),
    ("gemini-1.5-flash", "starší flash - často má free kvótu"),
    ("gemini-1.5-flash-8b", "malý, levný"),
    ("gemini-2.5-flash", "novější flash"),
]


def gemini_list_models(api_key, timeout=30):
    import urllib.request
    from urllib.parse import quote
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={quote(api_key)}&pageSize=200"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data.get("models", [])


def _print_gemini_models(args=None):
    key = ((getattr(args, "gemini_key", None) if args else None)
           or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    models = None
    if key:
        try:
            print(f"{Fore.CYAN}Načítám seznam modelů Gemini...{Style.RESET_ALL}")
            models = gemini_list_models(key)
        except Exception as e:
            log_warn(f"Online seznam se nepodařilo načíst ({e}). Ukážu vestavěný přehled.")
    else:
        log_info("Gemini klíč není nastaven - ukážu vestavěný přehled.")

    print(f"{Fore.MAGENTA}Modely Gemini pro překlad:{Style.RESET_ALL}")
    if models:
        for m in models:
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            mid = m.get("name", "").split("/")[-1]
            if "embedding" in mid or "aqa" in mid:
                continue
            disp = m.get("displayName", "")
            it = m.get("inputTokenLimit")
            ot = m.get("outputTokenLimit")
            lim = f" | vstup {it}, výstup {ot}" if it else ""
            print(f"  {mid:<28}{disp}{lim}")
    else:
        for mid, note in _GEMINI_STATIC_MODELS:
            print(f"  {mid:<28}{note}")
    print(f"{Fore.CYAN}Pozn.: zdarma bývají 'flash' modely. Když některý hlásí 'limit: 0' (žádná "
          f"free kvóta), zkus jiný flash, nebo použij engine 'google' (zdarma bez klíče).{Style.RESET_ALL}")


def anthropic_proofread(events, target_lang, api_key, model, batch=40):
    """Korektura titulků přes Claude. Vrací seznam textů stejné délky.
    Při fatální chybě (4xx) korekturu zastaví a zbytek nechá beze změny."""
    out = []
    stop = None
    for i in range(0, len(events), batch):
        chunk = events[i:i + batch]
        if stop is None:
            numbered = "\n".join(f"{k + 1}. {c['text'].replace(chr(10), ' / ')}" for k, c in enumerate(chunk))
            prompt = (f"Jsi profesionální korektor filmových titulků v jazyce '{target_lang}'. "
                      "Oprav gramatiku, překlepy a nepřirozené formulace strojového překladu, "
                      "zachovej VÝZNAM, POŘADÍ i POČET položek a nepřekládej do jiného jazyka. "
                      "Víceřádkový titulek odděl ' / '. Vrať POUZE číslované řádky ve stejném "
                      "pořadí a počtu, bez komentářů.\n\n" + numbered)
            try:
                content = anthropic_messages(prompt, api_key, model)
                fixed = _parse_numbered(content or "", len(chunk))
            except _FatalAPIError as e:
                log_warn(f"Korektura Claude zastavena: {e} (zkontroluj --anthropic-model a klíč).")
                stop = True
                fixed = [c["text"] for c in chunk]
            except Exception as e:
                log_warn(f"Claude korektura selhala u bloku {i // batch + 1}: {e}")
                fixed = [c["text"] for c in chunk]
        else:
            fixed = [c["text"] for c in chunk]
        for c, t in zip(chunk, fixed):
            out.append(t.replace(" / ", "\n") if t else c["text"])
    return out


def llm_proofread(events, target_lang, api_url, api_key, model, batch=40):
    """Korektura přes OpenAI-kompatibilní API (/chat/completions). Vrací seznam
    textů stejné délky; při chybě bloku ponechá originál."""
    import urllib.request
    import urllib.error
    out = []
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    stop = None
    for i in range(0, len(events), batch):
        chunk = events[i:i + batch]
        if stop is not None:
            out.extend(c["text"] for c in chunk)
            continue
        numbered = "\n".join(f"{k + 1}. {c['text'].replace(chr(10), ' / ')}" for k, c in enumerate(chunk))
        prompt = (f"Jsi profesionální korektor filmových titulků v jazyce '{target_lang}'. "
                  "Oprav gramatiku, překlepy a nepřirozené formulace strojového překladu, "
                  "zachovej VÝZNAM i POŘADÍ a POČET položek. Nepřekládej do jiného jazyka. "
                  "Vrať POUZE číslované řádky ve stejném pořadí a počtu (číslo. text), "
                  "víceřádkový titulek odděl ' / '. Bez jakýchkoli komentářů.\n\n" + numbered)
        body = json.dumps({"model": model, "temperature": 0.2,
                           "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
        try:
            req = urllib.request.Request(api_url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            fixed = _parse_numbered(content, len(chunk))
        except urllib.error.HTTPError as e:
            detail = _http_error_detail(e)
            log_warn(f"Korektura LLM zastavena: HTTP {e.code}: {detail or e.reason} "
                     "(zkontroluj --llm-api, --llm-model a klíč).")
            stop = True
            fixed = [c["text"] for c in chunk]
        except Exception as e:
            log_warn(f"AI korektura selhala u bloku {i // batch + 1}: {e}")
            fixed = [c["text"] for c in chunk]
        for c, t in zip(chunk, fixed):
            out.append(t.replace(" / ", "\n") if t else c["text"])
    return out


def apply_proofread(events, provider, target_lang, args):
    """Aplikuje korekturu podle providera ('rules'/'llm'/'anthropic'/'off')."""
    if provider == "rules":
        for e in events:
            e["text"] = clean_subtitle_text(e["text"])
    elif provider == "llm":
        key = getattr(args, "llm_key", None) or os.environ.get("OPENAI_API_KEY")
        url = getattr(args, "llm_api", None) or "https://api.openai.com/v1/chat/completions"
        model = getattr(args, "llm_model", None) or "gpt-4o-mini"
        if not key:
            log_warn("Korektura LLM přeskočena - chybí API klíč.")
            return events
        fixed = llm_proofread(events, target_lang, url, key, model)
        for e, t in zip(events, fixed):
            if t:
                e["text"] = t
    elif provider == "anthropic":
        key = getattr(args, "anthropic_key", None) or os.environ.get("ANTHROPIC_API_KEY")
        model = getattr(args, "anthropic_model", None) or "claude-sonnet-4-6"
        if not key:
            log_warn("Korektura přes Claude přeskočena - chybí Anthropic API klíč.")
            return events
        fixed = anthropic_proofread(events, target_lang, key, model)
        for e, t in zip(events, fixed):
            if t:
                e["text"] = t
    return events


def _parse_numbered(content, n):
    res = [None] * n
    for line in content.split("\n"):
        m = re.match(r"\s*(\d+)[.)]\s*(.*)", line)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < n:
                res[idx] = m.group(2).strip()
    return res  # caller handles None? -> replace below


# --- OpenSubtitles -----------------------------------------------------

def opensubtitles_hash(path):
    """Oficiální OpenSubtitles moviehash (velikost + 64 kB ze začátku a konce)."""
    import struct
    fmt = "<q"
    size = struct.calcsize(fmt)
    filesize = os.path.getsize(path)
    if filesize < 65536 * 2:
        return None
    h = filesize
    with open(path, "rb") as f:
        for _ in range(65536 // size):
            (val,) = struct.unpack(fmt, f.read(size))
            h = (h + val) & 0xFFFFFFFFFFFFFFFF
        f.seek(filesize - 65536, 0)
        for _ in range(65536 // size):
            (val,) = struct.unpack(fmt, f.read(size))
            h = (h + val) & 0xFFFFFFFFFFFFFFFF
    return "%016x" % h


class OpenSubtitles:
    BASE = "https://api.opensubtitles.com/api/v1"

    def __init__(self, api_key, ua="sync_subtitles v1.0", username=None, password=None):
        self.key = api_key
        self.ua = ua
        self.user = username
        self.password = password
        self.token = None

    def _req(self, method, path, body=None, auth=False):
        import urllib.request
        headers = {"Api-Key": self.key, "Content-Type": "application/json",
                   "Accept": "application/json", "User-Agent": self.ua}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.BASE + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))

    def login(self):
        if not (self.user and self.password):
            return False
        try:
            d = self._req("POST", "/login", {"username": self.user, "password": self.password})
            self.token = d.get("token")
            return bool(self.token)
        except Exception as e:
            log_warn(f"OpenSubtitles login selhal: {e}")
            return False

    def search(self, languages, moviehash=None, query=None):
        from urllib.parse import urlencode
        params = {"languages": languages}
        if moviehash:
            params["moviehash"] = moviehash
        if query:
            params["query"] = query
        try:
            d = self._req("GET", "/subtitles?" + urlencode(params))
            return d.get("data", [])
        except Exception as e:
            log_warn(f"OpenSubtitles hledání selhalo: {e}")
            return []

    def download_srt(self, file_id):
        try:
            d = self._req("POST", "/download", {"file_id": file_id}, auth=True)
        except Exception as e:
            log_warn(f"OpenSubtitles /download selhal (účet/limit?): {e}")
            return None
        url = d.get("link")
        if not url:
            return None
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=60) as r:
                return _decode_subtitle_bytes(r.read())
        except Exception as e:
            log_warn(f"OpenSubtitles stažení souboru selhalo: {e}")
            return None


def fetch_opensubtitles_events(video, lang, api_key, username=None, password=None, pick="downloads"):
    """Najde a stáhne shodu lidských titulků v jazyce `lang`.
    pick: 'downloads' = nejstahovanější, 'rating' = nejlépe hodnocené,
    'ask' = nabídnout výběr ze seznamu. Vrací events nebo None.
    Stahování vyžaduje účet (uživatel/heslo)."""
    client = OpenSubtitles(api_key, username=username, password=password)
    moviehash = None
    try:
        moviehash = opensubtitles_hash(str(video))
    except Exception:
        pass
    results = client.search(lang, moviehash=moviehash, query=None if moviehash else Path(video).stem)
    if not results and moviehash:
        results = client.search(lang, query=Path(video).stem)
    if not results:
        log_info(f"{Path(video).name}: OpenSubtitles - žádná shoda v '{lang}'.")
        return None

    def _attr(r, k, d=0):
        return r.get("attributes", {}).get(k, d) or d

    if pick == "ask" and len(results) > 1:
        ranked = sorted(results, key=lambda r: _attr(r, "download_count"), reverse=True)[:10]
        labels = []
        for r in ranked:
            a = r.get("attributes", {})
            rel = a.get("release", a.get("feature_details", {}).get("title", "?"))
            labels.append(f"{str(rel)[:50]}  | stažení {a.get('download_count', 0)} "
                          f"| hodnocení {a.get('ratings', 0)} | hi={a.get('hearing_impaired', False)} "
                          f"| fps {a.get('fps', '?')}")
        idx = ask_pick(f"{Path(video).name}: vyber verzi titulků:", labels, default=0)
        best = ranked[idx]
    elif pick == "rating":
        best = max(results, key=lambda r: _attr(r, "ratings"))
    else:
        best = max(results, key=lambda r: _attr(r, "download_count"))

    files = best.get("attributes", {}).get("files", [])
    if not files:
        return None
    file_id = files[0].get("file_id")
    client.login()  # download vyžaduje token (účet)
    srt_text = client.download_srt(file_id)
    if not srt_text:
        return None
    tmp = tempfile.mkdtemp()
    try:
        p = Path(tmp) / "dl.srt"
        p.write_text(srt_text, encoding="utf-8")
        return parse_srt(p, strict=False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _affine_sync_args(base_args):
    import copy
    a = copy.copy(base_args)
    a.method = "affine"
    a.translate = "off"
    return a


# ----------------------------------------------------------------------
# Konfigurace (config.json) - API klíče a výchozí volby
# ----------------------------------------------------------------------
#
# --config spustí průvodce, který se zeptá jen na to, co chceš zapnout
# (DeepL / OpenSubtitles / AI korektura přes Claude nebo OpenAI / výchozí
# jazyk) a uloží to do config.json vedle skriptu. Při startu se config
# automaticky načte. Priorita hodnot: parametr na příkazové řádce >
# proměnná prostředí > config.json > výchozí. Vše je VOLITELNÉ - skript
# funguje i úplně bez configu a bez online funkcí.

# (config_key, args_attr, env_var, is_secret)
CONFIG_FIELDS = [
    ("deepl_key", "deepl_key", "DEEPL_API_KEY", True),
    ("opensubtitles_key", "opensubtitles_key", "OPENSUBTITLES_API_KEY", True),
    ("opensubtitles_user", "opensubtitles_user", "OPENSUBTITLES_USER", False),
    ("opensubtitles_password", "opensubtitles_password", "OPENSUBTITLES_PASSWORD", True),
    ("anthropic_key", "anthropic_key", "ANTHROPIC_API_KEY", True),
    ("anthropic_model", "anthropic_model", None, False),
    ("gemini_key", "gemini_key", "GEMINI_API_KEY", True),
    ("gemini_model", "gemini_model", None, False),
    ("llm_key", "llm_key", "OPENAI_API_KEY", True),
    ("llm_api", "llm_api", None, False),
    ("llm_model", "llm_model", None, False),
    ("out_lang", "out_lang", None, False),
]


def default_config_path():
    argv0 = sys.argv[0] if sys.argv and sys.argv[0] else None
    base = os.path.dirname(os.path.abspath(argv0)) if argv0 else os.getcwd()
    return os.path.join(base, "config.json")


def resolve_config_path(args):
    return getattr(args, "config_file", None) or default_config_path()


def load_config(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(path, cfg):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    try:
        if os.name != "nt":
            os.chmod(path, 0o600)   # jen pro vlastníka (jsou tam klíče)
    except Exception:
        pass


def apply_config_to_args(args, cfg):
    """Doplní do args hodnoty, které nebyly zadané na příkazové řádce.
    Priorita: CLI (už nastaveno) > proměnná prostředí > config.json."""
    for ckey, attr, env, _secret in CONFIG_FIELDS:
        if getattr(args, attr, None):
            continue  # zadáno na CLI
        val = (os.environ.get(env) if env else None) or cfg.get(ckey)
        if val:
            setattr(args, attr, val)
    # Gemini akceptuje i GOOGLE_API_KEY
    if not getattr(args, "gemini_key", None):
        g = os.environ.get("GOOGLE_API_KEY")
        if g:
            args.gemini_key = g


def _mask(s):
    if not s:
        return "(nenastaveno)"
    s = str(s)
    return (s[:4] + "…" + s[-2:]) if len(s) > 8 else "••••"


def run_config(args):
    """Interaktivní průvodce nastavením config.json. Ptá se jen na to, co
    chceš zapnout; nevyplněné nechá být. Klíče jsou v souboru v čitelné
    podobě - drž ho v bezpečí."""
    path = resolve_config_path(args)
    cfg = load_config(path)
    print(f"{Fore.MAGENTA}=== Nastavení (config.json) ==={Style.RESET_ALL}")
    log_info(f"Soubor: {path}")
    if cfg:
        log_info("Načten stávající config - prázdná odpověď ponechá současnou hodnotu.")
    log_warn("Pozor: klíče se ukládají v čitelné podobě. Drž config.json v bezpečí.")

    def ask_secret(label, ckey):
        cur = cfg.get(ckey)
        prompt = f"{label}" + (f" (teď {_mask(cur)}, Enter = ponechat)" if cur else " (Enter = přeskočit)")
        val = ask_text(prompt, "")
        if val:
            cfg[ckey] = val

    def ask_plain(label, ckey, default=""):
        cur = cfg.get(ckey, default)
        val = ask_text(label, cur or "")
        if val:
            cfg[ckey] = val
        elif ckey in cfg and not val and not cur:
            pass

    # DeepL
    if ask_yes_no("Nastavit DeepL (kvalitní strojový překlad)?", default_no=not cfg.get("deepl_key")):
        ask_secret("DeepL API klíč", "deepl_key")

    # OpenSubtitles
    if ask_yes_no("Nastavit OpenSubtitles (stahování hotových lidských titulků)?",
                  default_no=not cfg.get("opensubtitles_key")):
        ask_secret("OpenSubtitles API klíč", "opensubtitles_key")
        if ask_yes_no("Přidat i účet (uživatel/heslo) pro STAHOVÁNÍ?",
                      default_no=not cfg.get("opensubtitles_user")):
            ask_plain("OpenSubtitles uživatel", "opensubtitles_user")
            ask_secret("OpenSubtitles heslo", "opensubtitles_password")

    # AI korektura / překlad
    if ask_yes_no("Nastavit Google Gemini (AI PŘEKLAD ZDARMA, doporučeno pro češtinu)?",
                  default_no=not cfg.get("gemini_key")):
        ask_secret("Gemini API klíč (zdarma na aistudio.google.com)", "gemini_key")
        _gm = ask_gemini_model("Model Gemini", cfg.get("gemini_model") or "gemini-2.5-flash",
                               type("A", (), {"gemini_key": cfg.get("gemini_key")
                                              or os.environ.get("GEMINI_API_KEY")
                                              or os.environ.get("GOOGLE_API_KEY")})())
        if _gm:
            cfg["gemini_model"] = _gm
    if ask_yes_no("Nastavit AI přes Claude / OpenAI-kompatibilní API (překlad i korektura)?",
                  default_no=not (cfg.get("anthropic_key") or cfg.get("llm_key"))):
        which = ask_pick("Který AI provider nastavit?",
                         ["Anthropic (Claude)", "OpenAI-kompatibilní (OpenAI/lokální)", "oba"], default=0)
        if which in (0, 2):
            ask_secret("Anthropic API klíč", "anthropic_key")
            _am = ask_anthropic_model("Claude model", cfg.get("anthropic_model") or "claude-sonnet-4-6",
                                      type("A", (), {"anthropic_key": cfg.get("anthropic_key")
                                                     or os.environ.get("ANTHROPIC_API_KEY")})())
            if _am:
                cfg["anthropic_model"] = _am
        if which in (1, 2):
            ask_secret("OpenAI API klíč", "llm_key")
            ask_plain("API URL (chat/completions)", "llm_api", "https://api.openai.com/v1/chat/completions")
            ask_plain("Model", "llm_model", "gpt-4o-mini")

    # výchozí jazyk
    if ask_yes_no("Nastavit výchozí cílový jazyk pro --translate-subs?",
                  default_no=not cfg.get("out_lang")):
        _v = ask_language("Výchozí cílový jazyk (kód, např. cs)", cfg.get("out_lang") or "cs")
        if _v:
            cfg["out_lang"] = _v

    if ask_yes_no("Vymazat některou uloženou hodnotu?", default_no=True):
        keys = list(cfg.keys())
        if keys:
            labels = [f"{k} = {_mask(cfg[k]) if 'key' in k or 'password' in k else cfg[k]}" for k in keys]
            labels.append("(nic nemazat)")
            idx = ask_pick("Co vymazat?", labels, default=len(labels) - 1)
            if idx < len(keys):
                cfg.pop(keys[idx], None)

    save_config(path, cfg)
    print()
    log_done(f"Uloženo do {path}")
    enabled = []
    if cfg.get("gemini_key"):
        enabled.append("Gemini")
    if cfg.get("deepl_key"):
        enabled.append("DeepL")
    if cfg.get("opensubtitles_key"):
        enabled.append("OpenSubtitles" + ("+účet" if cfg.get("opensubtitles_user") else ""))
    if cfg.get("anthropic_key"):
        enabled.append("Claude")
    if cfg.get("llm_key"):
        enabled.append("OpenAI")
    log_info("Aktivní: " + (", ".join(enabled) if enabled else "(žádné online funkce)"))


def run_test_api(args):
    """Rychlý test API: pošle triviální požadavek a vypíše PŘESNOU odpověď/chybu
    (včetně těla od serveru). Pomáhá odhalit příčinu chyb jako HTTP 400."""
    import urllib.request
    import urllib.error
    tested = 0

    akey = getattr(args, "anthropic_key", None) or os.environ.get("ANTHROPIC_API_KEY")
    amodel = getattr(args, "anthropic_model", None) or "claude-sonnet-4-6"
    if akey:
        tested += 1
        log_info(f"Test Anthropic (Claude) - model '{amodel}'...")
        try:
            txt = anthropic_messages("Odpověz jediným slovem: OK.", akey, amodel, max_tokens=16)
            log_done(f"Anthropic OK. Odpověď: {txt!r}")
        except _FatalAPIError as e:
            log_warn(f"Anthropic SELHALO: {e}")
            log_warn("Pokud zpráva zmiňuje 'model', oprav --anthropic-model (--config). "
                     "Jinak je problém v tomto konkrétním poli/parametru.")
        except Exception as e:
            log_warn(f"Anthropic SELHALO: {e}")
    else:
        log_info("Anthropic klíč nenastaven - přeskakuji.")

    gkey = (getattr(args, "gemini_key", None) or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY"))
    gmodel = getattr(args, "gemini_model", None) or "gemini-2.5-flash"
    if gkey:
        tested += 1
        log_info(f"Test Google Gemini - model '{gmodel}'...")
        try:
            txt = gemini_generate("Odpověz jediným slovem: OK.", gkey, gmodel)
            log_done(f"Gemini OK. Odpověď: {txt!r}")
        except _FatalAPIError as e:
            log_warn(f"Gemini SELHALO: {e}")
        except Exception as e:
            log_warn(f"Gemini SELHALO: {e}")
    else:
        log_info("Gemini klíč nenastaven - přeskakuji.")

    okey = getattr(args, "llm_key", None) or os.environ.get("OPENAI_API_KEY")
    if okey:
        tested += 1
        url = getattr(args, "llm_api", None) or "https://api.openai.com/v1/chat/completions"
        omodel = getattr(args, "llm_model", None) or "gpt-4o-mini"
        log_info(f"Test OpenAI-kompatibilního API ({url}, model '{omodel}')...")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {okey}"}
        body = json.dumps({"model": omodel, "max_tokens": 16,
                           "messages": [{"role": "user", "content": "Reply with: OK"}]}).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
            log_done(f"OpenAI OK. Odpověď: {data['choices'][0]['message']['content']!r}")
        except urllib.error.HTTPError as e:
            log_warn(f"OpenAI SELHALO: HTTP {e.code}: {_http_error_detail(e)}")
        except Exception as e:
            log_warn(f"OpenAI SELHALO: {e}")

    if not tested:
        log_warn("Není nastaven žádný AI klíč. Nastav ho přes --config, --anthropic-key nebo --llm-key.")


def run_translate_subs(args):
    """Interaktivní průvodce: zdroj je buď titulková stopa z VIDEÍ (vytáhne se),
    nebo přímo EXISTUJÍCÍ titulkové soubory v adresáři (jeden/několik/všechny).
    Získá cílový jazyk (OpenSubtitles a/nebo strojový překlad + korektura),
    volitelně opraví čitelnost a uloží .srt."""
    if args.mkv and args.mkv.is_dir():
        directory = str(args.mkv)
    elif args.mkv and args.mkv.exists():
        directory = os.path.dirname(str(args.mkv)) or "."
    else:
        directory = "."

    print(f"{Fore.MAGENTA}=== Přeložit titulky a uložit .srt ==={Style.RESET_ALL}")
    log_info(f"Pracovní adresář: {os.path.abspath(directory)}")

    recursive = ask_yes_no("Prohledat i podadresáře?", default_no=True)

    src_type = ask_pick(
        "Co chceš přeložit?",
        ["Titulkové stopy z VIDEÍ v adresáři (vytáhnout z videa)",
         "Existující TITULKOVÉ SOUBORY v adresáři (.srt)"],
        default=0,
        help=["Z videí: pro každé video vytáhne zvolenou titulkovou stopu a tu přeloží "
              "(u 'auto' může místo překladu stáhnout hotové z OpenSubtitles).",
              "Z titulkových souborů: přeloží přímo existující .srt v adresáři - můžeš vybrat "
              "jeden, několik, nebo všechny. Žádné video ani nástroje nejsou potřeba."])

    jobs = []  # list of (path, kind) kind = "video" | "sub"
    args.track_id = None
    if src_type == 0:
        videos = collect_videos(directory, recursive)
        if not videos:
            die("Žádná videa v adresáři. Zvol 'titulkové soubory', nebo použij --auto pro synchronizaci.")
        log_info(f"Nalezeno {len(videos)} videí.")
        sample = Path(videos[0])
        mkvmerge_bin, _, _, _ = _resolve_tools_for_extract(args, sample)
        try:
            sub_tracks = mkvmerge_tracks(mkvmerge_bin, sample, "subtitles") if mkvmerge_bin else []
        except (Exception, SystemExit):
            sub_tracks = []
        if sub_tracks:
            labels = [f"#{t['id']}  {t['lang']}  {t['codec']}  {t.get('title', '')}" for t in sub_tracks]
            labels += ["podle jazyka (zadám kód)", "první vhodná"]
            i = ask_pick(f"Kterou titulkovou stopu extrahovat (vzorek {sample.name})?", labels, default=0)
            if i < len(sub_tracks):
                args.track_id = sub_tracks[i]["id"]; args.ref_lang = None
            elif i == len(sub_tracks):
                args.ref_lang = norm_lang(ask_language("Jazyk zdrojové stopy (eng/cze/...)", "") or None)
            else:
                args.ref_lang = None
        else:
            log_warn("Stopy vzorového videa se nepodařilo přečíst - vyberu podle jazyka/první.")
            args.ref_lang = norm_lang(ask_language("Jazyk zdrojové titulkové stopy (eng/...; prázdné=první)", "") or None)
        jobs = [(v, "video") for v in videos]
    else:
        subs = collect_srts(directory, recursive)
        if not subs:
            die("V adresáři nejsou žádné .srt soubory.")
        sel = ask_pick(f"Které titulkové soubory přeložit? (nalezeno {len(subs)})",
                       ["všechny", "jeden", "několik (vyberu čísla)"], default=0)
        if sel == 0:
            chosen = subs
        elif sel == 1:
            idx = ask_pick("Který soubor?", [os.path.basename(s) for s in subs], default=0)
            chosen = [subs[idx]]
        else:
            for k, s in enumerate(subs, 1):
                print(f"  {k}) {os.path.basename(s)}")
            raw = ask_text("Zadej čísla oddělená čárkou (např. 1,3,4)", "")
            chosen = [subs[int(t) - 1] for t in raw.replace(" ", "").split(",")
                      if t.isdigit() and 1 <= int(t) <= len(subs)] or subs
        jobs = [(s, "sub") for s in chosen]
        log_info(f"Vybráno {len(jobs)} titulkových souborů k překladu.")

    # cílový jazyk
    out_lang = (ask_language("Do jakého jazyka přeložit (kód, např. cs/en/de)",
                             getattr(args, "out_lang", None) or "cs") or "cs").lower()

    # strategie zdroje (OpenSubtitles dává smysl jen u videí)
    if src_type == 0:
        si = ask_pick("Odkud vzít cílové titulky?",
                      ["auto - nejdřív zkus hotové lidské (OpenSubtitles), jinak strojový překlad",
                       "jen strojový překlad extrahované stopy",
                       "jen stáhnout hotové z OpenSubtitles"], default=0,
                      help=["auto: nejdřív zkusí stáhnout hotové lidské titulky z OpenSubtitles "
                            "(nejlepší kvalita); když nejsou/nelze, přeloží extrahovanou stopu strojově.",
                            "jen strojový překlad: vždy přeloží extrahovanou titulkovou stopu z videa "
                            "(zachová původní časování, sedí na video).",
                            "jen OpenSubtitles: použije pouze stažené lidské titulky; když nejsou, "
                            "video se přeskočí."])
        strategy = ["auto", "mt", "opensubtitles"][si]
    else:
        strategy = "mt"  # titulkový soubor se prostě přeloží

    engine = mt_key = mt_model = None
    if strategy in ("auto", "mt"):
        ei = ask_pick("Strojový překladač:",
                      ["gemini - AI kvalita ZDARMA (Google AI Studio klíč) - doporučeno pro češtinu",
                       "deepl  - výborná kvalita (API klíč, free tier)",
                       "google - úplně zdarma bez klíče, dobrá kvalita (moderní Google endpoint)",
                       "claude - AI přes Anthropic API (placené)",
                       "argos  - offline, zdarma (nižší kvalita)"], default=0,
                      help=["gemini: AI překlad od Googlu, ZDARMA s API klíčem z aistudio.google.com "
                            "(štědrý free limit). Nejlepší poměr kvalita/cena pro češtinu.",
                            "deepl: špičková kvalita, vyžaduje klíč (má free tier ~500k znaků/měsíc).",
                            "google: úplně ZDARMA a bez klíče. Používá stejný moderní endpoint Google "
                            "Translate jako web translatesubtitles.co (dávkově, HTML-aware). Se "
                            "spojováním vět je výsledek slušný.",
                            "claude: velmi kvalitní, placené (za tokeny).",
                            "argos: plně offline, zdarma, ale znatelně nižší kvalita."])
        engine = ["gemini", "deepl", "google", "claude", "argos"][ei]
        if engine == "gemini":
            mt_key = (getattr(args, "gemini_key", None) or os.environ.get("GEMINI_API_KEY")
                      or os.environ.get("GOOGLE_API_KEY")
                      or ask_text("Gemini API klíč (zdarma na aistudio.google.com)", ""))
            args.gemini_key = args.gemini_key or mt_key
            mt_model = getattr(args, "gemini_model", None) or ask_gemini_model(
                "Model Gemini", "gemini-2.5-flash", args)
            args.gemini_model = args.gemini_model or mt_model
        elif engine == "deepl":
            mt_key = (getattr(args, "deepl_key", None) or os.environ.get("DEEPL_API_KEY")
                      or ask_text("DeepL API klíč", ""))
        elif engine == "claude":
            mt_key = (getattr(args, "anthropic_key", None) or os.environ.get("ANTHROPIC_API_KEY")
                      or ask_text("Anthropic API klíč", ""))
            mt_model = getattr(args, "anthropic_model", None) or ask_anthropic_model("Claude model", "claude-sonnet-4-6", args)
            args.anthropic_key = args.anthropic_key or mt_key
            args.anthropic_model = args.anthropic_model or mt_model

    os_key = os_user = os_pw = None
    os_pick = "downloads"
    if src_type == 0 and strategy in ("auto", "opensubtitles"):
        os_key = (getattr(args, "opensubtitles_key", None) or os.environ.get("OPENSUBTITLES_API_KEY")
                  or ask_text("OpenSubtitles API klíč (prázdné = přeskočit OpenSubtitles)", ""))
        os_user = getattr(args, "opensubtitles_user", None) or os.environ.get("OPENSUBTITLES_USER")
        os_pw = getattr(args, "opensubtitles_password", None) or os.environ.get("OPENSUBTITLES_PASSWORD")
        if os_key and not os_user and ask_yes_no("Máš účet OpenSubtitles (nutný pro STAHOVÁNÍ)?", default_no=True):
            os_user = ask_text("Uživatel", "")
            os_pw = ask_text("Heslo", "")
        if os_key:
            pp = ask_pick("Při více nalezených verzích titulků:",
                          ["nejstahovanější (automaticky)",
                           "nejlépe hodnocené (automaticky)",
                           "vybrat ručně u každého videa"], default=0)
            os_pick = ["downloads", "rating", "ask"][pp]
        if os_key and not os_user:
            log_warn("Bez účtu OpenSubtitles půjde jen vyhledat, ne stáhnout - u 'auto' se "
                     "přejde na strojový překlad.")

    pi = ask_pick("Korektura výsledku:",
                  ["rules  - rychlé pravidlové očištění (zdarma)",
                   "off    - žádná",
                   "claude - AI korektura přes Anthropic API (klíč)",
                   "llm    - AI korektura přes OpenAI-kompatibilní API (klíč)"], default=0,
                  help=["rules: zdarma, offline. Sjednotí mezery/interpunkci, rozlomí dlouhé řádky. "
                        "Nemění význam.",
                        "off: ponechá překlad tak, jak je.",
                        "claude: AI korektura gramatiky a přirozenosti přes Anthropic API "
                        "(platí se za tokeny).",
                        "llm: AI korektura přes OpenAI-kompatibilní API (OpenAI nebo lokální server)."])
    proofread = ["rules", "off", "anthropic", "llm"][pi]
    if proofread == "llm":
        args.llm_api = (getattr(args, "llm_api", None)
                        or ask_text("API URL (chat/completions)", "https://api.openai.com/v1/chat/completions"))
        args.llm_key = getattr(args, "llm_key", None) or os.environ.get("OPENAI_API_KEY") or ask_text("API klíč", "")
        args.llm_model = getattr(args, "llm_model", None) or ask_text("Model", "gpt-4o-mini")
    elif proofread == "anthropic":
        args.anthropic_key = (getattr(args, "anthropic_key", None) or os.environ.get("ANTHROPIC_API_KEY")
                              or ask_text("Anthropic API klíč", ""))
        args.anthropic_model = getattr(args, "anthropic_model", None) or ask_anthropic_model("Claude model", "claude-sonnet-4-6", args)

    sync_os = (src_type == 0 and strategy != "mt") and ask_yes_no(
        "Stažené (lidské) titulky případně časově srovnat s videem (afinně)?", default_no=False)

    # oprava čitelnosti (prodloužení krátkých titulků) na výsledku
    args.fix_short_duration = False
    _ask_readability(args, [j[0] for j in jobs if j[1] == "sub"][:5])

    overwrite = ask_yes_no("Přepsat existující výstupní .srt?", default_no=True)

    print()
    log_info(f"Cílový jazyk: {out_lang} | zdroj: {'videa' if src_type == 0 else 'titulkové soubory'}"
             + (f"/{strategy}" if src_type == 0 else "")
             + (f" | překladač: {engine}" if engine else "")
             + f" | korektura: {proofread}"
             + (" | čitelnost: ano" if getattr(args, "fix_short_duration", False) else ""))
    if not ask_yes_no(f"Spustit pro {len(jobs)} položek?", default_no=False):
        log_warn("Zrušeno uživatelem.")
        return
    preset_flush_if_save()

    done = skipped = 0
    for path, kind in jobs:
        p = Path(path)
        out_path = p.with_name(p.stem + f".{out_lang}.srt")
        if kind == "sub" and out_path.resolve() == p.resolve():
            out_path = p.with_name(p.stem + f".{out_lang}.tr.srt")
        if out_path.exists() and not overwrite:
            log_info(f"{p.name}: výstup už existuje - přeskakuji.")
            skipped += 1
            continue

        events = None
        source_used = None

        if kind == "video":
            if strategy in ("auto", "opensubtitles") and os_key:
                events = fetch_opensubtitles_events(p, out_lang, os_key, os_user, os_pw, pick=os_pick)
                if events:
                    source_used = "opensubtitles"
                    if sync_os:
                        ref_events, _ = extract_subtitle_events(args, p, args.track_id, args.ref_lang)
                        if ref_events:
                            log_info(f"{p.name}: srovnávám stažené titulky s videem (affine)...")
                            events = run_alignment(_affine_sync_args(args), ref_events, ref_events, events)
            if events is None and strategy in ("auto", "mt"):
                _vtext = probe_text_subtitle_tracks(args, p)
                _init = None
                if args.track_id is not None:
                    _init = next((t for t in _vtext if t["id"] == args.track_id), None)
                if _init is None and args.ref_lang:
                    _init = next((t for t in _vtext if t["lang"].lower().startswith(str(args.ref_lang).lower())), None)
                if _init is None and _vtext:
                    _init = _vtext[0]
                if _init is not None:
                    src_events, _chosen = extract_with_fallback(args, p, _init, _vtext,
                                                                interactive=not preset_is_replaying())
                else:
                    src_events = None
                if src_events:
                    log_info(f"{p.name}: překládám {len(src_events)} titulků do '{out_lang}' ({engine})...")
                    translated = translate_events_to(src_events, engine, out_lang, mt_key, mt_model)
                    if translated:
                        changed = sum(1 for a, b in zip(src_events, translated)
                                      if a["text"].replace("\n", " ").strip() != b["text"].replace("\n", " ").strip())
                        if changed < max(1, int(0.05 * len(translated))):
                            log_warn(f"{p.name}: překlad se nezdařil (přeloženo {changed}/{len(translated)}) - "
                                     f"NEUKLÁDÁM nepřeložený text jako '{out_lang}'. Zkontroluj klíč/model/engine.")
                            skipped += 1
                            continue
                        events = translated
                        source_used = f"mt:{engine}"
        else:  # kind == "sub"
            try:
                src_events = parse_srt(p, strict=False)
            except Exception as e:
                log_warn(f"{p.name}: nelze načíst ({e}) - přeskakuji.")
                skipped += 1
                continue
            sl = detect_sub_language(src_events)
            if sl and sl == out_lang:
                log_info(f"{p.name}: zdroj je už v jazyce '{out_lang}' - přeskakuji.")
                skipped += 1
                continue
            log_info(f"{p.name}: překládám {len(src_events)} titulků do '{out_lang}' ({engine})...")
            translated = translate_events_to(src_events, engine, out_lang, mt_key, mt_model)
            if translated:
                changed = sum(1 for a, b in zip(src_events, translated)
                              if a["text"].replace("\n", " ").strip() != b["text"].replace("\n", " ").strip())
                if changed < max(1, int(0.05 * len(translated))):
                    log_warn(f"{p.name}: překlad se nezdařil (přeloženo {changed}/{len(translated)}) - "
                             f"NEUKLÁDÁM nepřeložený text. Zkontroluj klíč/model/engine.")
                    skipped += 1
                    continue
                events = translated
                source_used = f"mt:{engine}"

        if not events:
            log_warn(f"{p.name}: nepodařilo se získat cílové titulky - přeskakuji.")
            skipped += 1
            continue

        if proofread != "off":
            apply_proofread(events, proofread, out_lang, args)

        if getattr(args, "fix_short_duration", False):
            cps, floor, gap, overhead = resolve_speed_params(args)
            events, n_ext = fix_short_durations(
                events, min_cps=cps, min_duration_floor=floor, min_gap=gap, line_overhead=overhead)
            if n_ext:
                log_info(f"{p.name}: čitelnost - prodlouženo {n_ext} krátkých titulků")

        try:
            write_srt(events, out_path)
            log_done(f"{p.name} -> {out_path.name}  ({source_used})")
            done += 1
        except Exception as e:
            log_warn(f"{p.name}: zápis selhal: {e}")
            skipped += 1

    print()
    log_done(f"Hotovo: {done} uloženo, {skipped} přeskočeno (z {len(jobs)}).")


def run_extract_subs(args, minimal=False):
    """Průvodce: vytáhne titulkové stopy z videí (mkv/mp4/...) do .srt. Stopy se
    detekují přímo z videa; interaktivně vybereš, které (podle jazyka, konkrétní
    stopy ze vzorku, nebo všechny textové).
    minimal=True: čistá extrakce bez dotazů na očištění/čitelnost (používá se jako
    mezikrok pro jiný režim, který si úpravy udělá sám)."""
    if args.mkv and args.mkv.is_dir():
        directory = str(args.mkv)
    elif args.mkv and args.mkv.exists():
        directory = os.path.dirname(str(args.mkv)) or "."
    else:
        directory = "."

    print(f"{Fore.MAGENTA}=== Extrahovat titulky z videí (do .srt) ==={Style.RESET_ALL}")
    log_info(f"Pracovní adresář: {os.path.abspath(directory)}")

    recursive = bool(getattr(args, "recursive", False)) if minimal else ask_yes_no(
        "Prohledat i podadresáře?", default_no=True)
    videos = collect_videos(directory, recursive)
    if not videos:
        die("Žádná videa (mkv/mp4/...) v adresáři.")
    log_info(f"Nalezeno {len(videos)} videí.")

    # detekce stop ze vzorku (zkus videa postupně, dokud se nějaké nepřečte)
    mkvmerge_bin, mkvextract_bin, ffmpeg_bin, _is_mkv = _resolve_tools_for_extract(args, Path(videos[0]))
    if not mkvmerge_bin:
        die("Nenašel jsem mkvmerge (mkvtoolnix) pro čtení stop. Nainstaluj mkvtoolnix, "
            "nebo zkontroluj připojení pro automatické stažení.")
    # zapamatuj nástroje, ať se neřeší u každého videa znovu
    args.mkvmerge = args.mkvmerge or mkvmerge_bin
    if mkvextract_bin:
        args.mkvextract = args.mkvextract or mkvextract_bin
    if ffmpeg_bin:
        args.ffmpeg = args.ffmpeg or ffmpeg_bin

    sample = None
    sub_tracks = []
    for cand in videos:
        try:
            st = mkvmerge_tracks(mkvmerge_bin, Path(cand), "subtitles")
        except (Exception, SystemExit):
            continue
        if st:
            sample = Path(cand)
            sub_tracks = st
            break
    if sample is None:
        die("Nepodařilo se přečíst titulkové stopy ze žádného videa (poškozené soubory, "
            "nebo videa nemají titulky).")
    log_info(f"Titulkové stopy ve vzorku ({sample.name}):")
    for t in sub_tracks:
        txt = "text" if is_text_codec(t["codec"]) else "OBRÁZKOVÉ (nelze do .srt)"
        print(f"    #{t['id']}  {t['lang']:4}  {t['codec']:16} {t.get('title', '')}  [{txt}]")
    text_tracks = [t for t in sub_tracks if is_text_codec(t["codec"])]
    if not text_tracks:
        log_warn("Vzorové video má jen obrázkové titulky (PGS/VobSub). Můžeš přesto zkusit jiná "
                 "videa přes výběr podle jazyka - textové stopy se vytáhnou, obrázkové přeskočí.")

    mode = ask_pick("Které titulkové stopy vytáhnout?",
                    ["podle JAZYKA (zadám kódy - robustní pro celou složku)",
                     "konkrétní STOPY ze vzorku (podle čísla výše)",
                     "VŠECHNY textové titulkové stopy z každého videa"], default=0,
                    help=["podle jazyka: zadáš kódy (např. eng,cze,ger) a z KAŽDÉHO videa se "
                          "vytáhnou stopy v těch jazycích - i když mají v různých videích jiná ID.",
                          "konkrétní stopy: vybereš čísla stop podle vzorku výše; stejná ID se "
                          "použijí u všech videí (vhodné, když mají všechna stejné pořadí stop).",
                          "všechny: z každého videa vytáhne všechny textové titulkové stopy."])

    want_langs = None
    want_ids = None
    if mode == 0:
        raw = ask_text("Kódy jazyků oddělené čárkou (např. eng,cze,ger; prázdné = všechny)", "")
        want_langs = [x.strip().lower() for x in raw.replace(" ", "").split(",") if x.strip()]
        if not want_langs:
            want_langs = None  # = všechny textové
    elif mode == 1:
        if not text_tracks:
            log_warn("Vzorové video nemá textové stopy k výběru podle čísla - přepínám na výběr "
                     "podle jazyka (projde se každé video zvlášť).")
            raw = ask_text("Kódy jazyků oddělené čárkou (např. eng,cze; prázdné = všechny)", "")
            want_langs = [x.strip().lower() for x in raw.replace(" ", "").split(",") if x.strip()] or None
            want_ids = None
        else:
            labels = [f"#{t['id']}  {t['lang']}  {t['codec']}  {t.get('title', '')}" for t in text_tracks]
            for k, lab in enumerate(labels, 1):
                print(f"  {k}) {lab}")
            raw = ask_text("Zadej čísla stop oddělená čárkou (např. 1,3)", "1")
            picks = [int(x) for x in raw.replace(" ", "").split(",") if x.isdigit() and 1 <= int(x) <= len(text_tracks)]
            want_ids = [text_tracks[k - 1]["id"] for k in picks] or [text_tracks[0]["id"]]

    do_clean = False if minimal else ask_yes_no(
        "Pravidlově očistit text (mezery, interpunkce, rozlomení dlouhých řádků)?", default_no=True)
    args.fix_short_duration = False
    if not minimal:
        _ask_readability(args, [])
    overwrite = True if minimal else ask_yes_no("Přepsat existující výstupní .srt?", default_no=True)

    print()
    seltxt = ("jazyky: " + ",".join(want_langs)) if want_langs else \
             ("stopy #" + ",".join(str(i) for i in want_ids)) if want_ids else "všechny textové"
    log_info(f"Výběr: {seltxt} | videí: {len(videos)}"
             + (" | očištění" if do_clean else "")
             + (" | čitelnost" if getattr(args, 'fix_short_duration', False) else ""))
    if not minimal and not ask_yes_no(f"Spustit pro {len(videos)} videí?", default_no=False):
        log_warn("Zrušeno uživatelem.")
        return
    preset_flush_if_save()

    done = skipped = wrote = 0
    for vid in videos:
        v = Path(vid)
        try:
            vtracks = mkvmerge_tracks(args.mkvmerge, v, "subtitles")
        except (Exception, SystemExit) as e:
            log_warn(f"{v.name}: nelze přečíst stopy ({e}) - přeskakuji.")
            skipped += 1
            continue
        vtext = [t for t in vtracks if is_text_codec(t["codec"])]
        if want_langs is not None:
            sel = [t for t in vtext if any(t["lang"].lower().startswith(l) for l in want_langs)]
        elif want_ids is not None:
            sel = [t for t in vtext if t["id"] in want_ids]
        else:
            sel = vtext
        if not sel:
            log_warn(f"{v.name}: žádná odpovídající textová stopa - přeskakuji.")
            skipped += 1
            continue

        done_ids = set()
        any_written = False
        for t in sel:
            if t["id"] in done_ids:
                continue
            out_path = v.with_name(v.stem + f".{_track_tag(t, vtext)}.srt")
            if out_path.exists() and not overwrite:
                log_info(f"{out_path.name}: už existuje - přeskakuji.")
                done_ids.add(t["id"])
                continue
            events, chosen = extract_with_fallback(args, v, t, vtext, done_ids=done_ids,
                                                   interactive=not minimal and not preset_is_replaying())
            if not events:
                continue
            # když fallback vybral jinou stopu, pojmenuj podle skutečné stopy
            if chosen and chosen["id"] != t["id"]:
                out_path = v.with_name(v.stem + f".{_track_tag(chosen, vtext)}.srt")
                if out_path.exists() and not overwrite:
                    log_info(f"{out_path.name}: už existuje - přeskakuji.")
                    done_ids.add(chosen["id"])
                    continue
            done_ids.add(chosen["id"] if chosen else t["id"])
            if do_clean:
                for e in events:
                    e["text"] = clean_subtitle_text(e["text"])
            if getattr(args, "fix_short_duration", False):
                cps, floor, gap, overhead = resolve_speed_params(args)
                events, _n = fix_short_durations(events, min_cps=cps, min_duration_floor=floor,
                                                 min_gap=gap, line_overhead=overhead)
            ch = chosen or t
            try:
                write_srt(events, out_path)
                log_done(f"{v.name}: stopa #{ch['id']} ({ch['lang']}, {ch['codec']}) -> {out_path.name} "
                         f"({len(events)} titulků)")
                wrote += 1
                any_written = True
            except Exception as e:
                log_warn(f"{v.name}: zápis {out_path.name} selhal: {e}")
        if any_written:
            done += 1
        else:
            skipped += 1

    print()
    log_done(f"Hotovo: {wrote} .srt souborů z {done} videí ({skipped} videí přeskočeno).")


# ======================================================================
# Práce se stopami v kontejnerech (mkv/mp4) - port samostatných skriptů:
# import titulků, mazání stop, výchozí stopa, přejmenování titulků.
# ======================================================================
_LANG3_FROM2 = {
    "en": "eng", "cs": "cze", "sk": "slo", "de": "ger", "fr": "fre", "es": "spa",
    "it": "ita", "pt": "por", "nl": "dut", "pl": "pol", "ru": "rus", "uk": "ukr",
    "ja": "jpn", "ko": "kor", "zh": "chi", "hu": "hun", "ro": "rum", "sv": "swe",
    "no": "nor", "da": "dan", "fi": "fin", "el": "gre", "tr": "tur", "ar": "ara",
    "he": "heb", "th": "tha", "vi": "vie", "id": "ind", "hi": "hin", "bg": "bul",
    "hr": "hrv", "sr": "srp", "sl": "slv", "et": "est", "lv": "lav", "lt": "lit",
    "fil": "fil", "ms": "msa",
}
_LANG3_ALIAS = {"ces": "cze", "deu": "ger", "fra": "fre", "nld": "dut", "ron": "rum",
                "slk": "slo", "zho": "chi", "ell": "gre", "may": "msa"}
_LANG3_NAME = {
    "eng": "English", "cze": "Czech", "slo": "Slovak", "ger": "German", "fre": "French",
    "spa": "Spanish", "ita": "Italian", "por": "Portuguese", "dut": "Dutch", "pol": "Polish",
    "rus": "Russian", "ukr": "Ukrainian", "jpn": "Japanese", "kor": "Korean", "chi": "Chinese",
    "hun": "Hungarian", "rum": "Romanian", "swe": "Swedish", "nor": "Norwegian", "dan": "Danish",
    "fin": "Finnish", "gre": "Greek", "tur": "Turkish", "ara": "Arabic", "heb": "Hebrew",
    "tha": "Thai", "vie": "Vietnamese", "ind": "Indonesian", "hin": "Hindi", "bul": "Bulgarian",
    "hrv": "Croatian", "srp": "Serbian", "slv": "Slovenian", "fil": "Filipino", "msa": "Malay",
    "und": "(neznámý)",
}
_EPISODE_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,2})")
_LANG_TOKEN_RE = re.compile(r"^[A-Za-z]{2,3}$")
_FLAG_TOKENS = {"forced", "sdh", "cc", "hi", "foreign", "full", "default"}


def _canon3(lang):
    l = (lang or "").strip().lower()
    if not l or l == "und":
        return "und"
    if l in _LANG3_ALIAS:
        return _LANG3_ALIAS[l]
    if len(l) == 2:
        return _LANG3_FROM2.get(l, l)
    return l


def _lang3_name(code3):
    return _LANG3_NAME.get(code3, code3.upper())


def _episode_key(name):
    m = _EPISODE_RE.search(name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _fmt_ep(k):
    return f"S{k[0]:02d}E{k[1]:02d}"


def _find_mkvpropedit(mkvmerge_bin):
    if mkvmerge_bin:
        d = os.path.dirname(mkvmerge_bin)
        for n in ("mkvpropedit", "mkvpropedit.exe"):
            c = os.path.join(d, n)
            if os.path.isfile(c):
                return c
    return find_tool(["mkvpropedit", "mkvpropedit.exe"])


def _ensure_mkv_tools(args, target_dir, need_propedit=False):
    """Najde mkvmerge/mkvextract/mkvpropedit; když chybí, stáhne MKVToolNix do
    .mkvtoolnix (stejně jako ffmpeg). Vrací (mkvmerge, mkvextract, mkvpropedit)."""
    mm = getattr(args, "mkvmerge", None) or find_tool(["mkvmerge", "mkvmerge.exe"])
    me = getattr(args, "mkvextract", None) or find_tool(["mkvextract", "mkvextract.exe"])
    mp = _find_mkvpropedit(mm)
    if not mm or not me or (need_propedit and not mp):
        try:
            mm2, me2 = ensure_mkvtoolnix(target_dir, allow_download=not getattr(args, "no_mkvtoolnix_download", False))
            mm = mm or mm2
            me = me or me2
            mp = mp or _find_mkvpropedit(mm)
        except Exception as e:
            log_warn(f"MKVToolNix se nepodařilo zajistit: {e}")
    if mm:
        args.mkvmerge = args.mkvmerge or mm
    if me:
        args.mkvextract = args.mkvextract or me
    return mm, me, mp


def _mkv_probe_full(mkvmerge_bin, video):
    """Vrátí {'audio':[...], 'subs':[...]}; každá stopa má id, lang, name, codec,
    default, forced, a selektor sel (a1/s1... pro mkvpropedit)."""
    try:
        out = subprocess.run([mkvmerge_bin, "-J", str(video)], capture_output=True, text=True)
        data = json.loads(out.stdout or "{}")
    except Exception:
        return {"audio": [], "subs": []}
    audio, subs = [], []
    ai = si = 0
    for t in data.get("tracks", []):
        pr = t.get("properties", {}) or {}
        rec = {"id": t["id"], "lang": _canon3(pr.get("language")), "name": pr.get("track_name") or "",
               "codec": t.get("codec") or "", "default": bool(pr.get("default_track")),
               "forced": bool(pr.get("forced_track"))}
        if t.get("type") == "audio":
            ai += 1
            rec["sel"] = f"a{ai}"
            audio.append(rec)
        elif t.get("type") == "subtitles":
            si += 1
            rec["sel"] = f"s{si}"
            subs.append(rec)
    return {"audio": audio, "subs": subs}


def _collect_videos_subs(directory, recursive, sub_exts=(".srt", ".ass", ".ssa", ".sup", ".sub", ".vtt")):
    videos, subs = [], []
    walker = os.walk(directory) if recursive else [(directory, [], os.listdir(directory))]
    for root, _d, files in walker:
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            full = os.path.join(root, f)
            if ext in (".mkv", ".mp4"):
                videos.append(full)
            elif ext in sub_exts:
                subs.append(full)
    return sorted(videos), sorted(subs)


def _parse_sub_meta(sub_name, forced_lang=None):
    """Z názvu titulku vytáhne (lang3, název_stopy, forced)."""
    stem = os.path.splitext(sub_name)[0]
    parts = stem.split(".")
    lang = forced_lang
    forced = sdh = False
    tail = []
    while parts and len(tail) < 2 and (parts[-1].lower() in _FLAG_TOKENS or _LANG_TOKEN_RE.match(parts[-1])):
        tail.insert(0, parts.pop())
    for tok in tail:
        low = tok.lower()
        if low == "forced":
            forced = True
        elif low in ("sdh", "cc", "hi"):
            sdh = True
        elif _LANG_TOKEN_RE.match(tok) and lang is None:
            lang = low
    lang3 = "und" if lang is None else _canon3(lang)
    name = _lang3_name(lang3)
    if forced:
        name += " (Forced)"
    elif sdh:
        name += " (SDH)"
    return lang3, name, forced


# ---------------------------------------------------------------- import
def run_import_subs(args):
    """Vloží (mux) titulkové soubory do videí (párování přes SxxExx) pomocí
    mkvmerge; nastaví jazyk, název stopy, forced a volitelně default. Výstup je
    vždy MKV."""
    directory = str(args.mkv) if args.mkv and args.mkv.is_dir() else "."
    print(f"{Fore.MAGENTA}=== Vložit (mux) titulky do videí ==={Style.RESET_ALL}")
    log_info(f"Pracovní adresář: {os.path.abspath(directory)}")
    recursive = ask_yes_no("Prohledat i podadresáře?", default_no=True)
    mm, me, mp = _ensure_mkv_tools(args, directory)
    if not mm:
        die("Nenašel jsem mkvmerge (MKVToolNix) ani se ho nepodařilo stáhnout.")

    videos, subs = _collect_videos_subs(directory, recursive)
    if not videos:
        die("Žádná videa (mkv/mp4) v adresáři.")
    if not subs:
        die("Žádné titulkové soubory (.srt/.ass/...) v adresáři.")
    # párování dle epizody
    vmap = {}
    for v in videos:
        k = _episode_key(os.path.basename(v))
        if k and k not in vmap:
            vmap[k] = v
    pairs = {}
    for s in subs:
        k = _episode_key(os.path.basename(s))
        if k and k in vmap:
            pairs.setdefault(vmap[k], []).append(s)
    if not pairs:
        die("Nepodařilo se spárovat titulky s videi podle SxxExx.")
    log_info(f"Spárováno {sum(len(x) for x in pairs.values())} titulků k {len(pairs)} videím.")

    set_default = ask_yes_no("Nastavit jednu titulkovou stopu jako VÝCHOZÍ (default)?", default_no=True)
    default_lang = None
    if set_default:
        default_lang = ask_language("Jazyk výchozích titulků (kód, např. cs)", "cs") or None
    forced_lang = None
    if ask_yes_no("Nemají soubory jazyk v názvu? Zadat jeden jazyk pro všechny?", default_no=True):
        forced_lang = ask_language("Jazyk titulků (kód)", "cs") or None
    replace = ask_yes_no("Přepsat původní video výsledným MKV? (jinak vedle jako <jméno>.muxed.mkv)", default_no=True)

    print()
    if not ask_yes_no(f"Spustit mux pro {len(pairs)} videí?", default_no=False):
        log_warn("Zrušeno uživatelem.")
        return
    preset_flush_if_save()

    done = failed = 0
    for v in sorted(pairs):
        vp = Path(v)
        sub_list = pairs[v]
        metas = []
        for s in sub_list:
            lang3, name, forced = _parse_sub_meta(os.path.basename(s), forced_lang)
            metas.append((s, lang3, name, forced))
        chosen = None
        if set_default and metas:
            if default_lang:
                dl = _canon3(default_lang)
                for i, (_s, l3, _n, _f) in enumerate(metas):
                    if l3 == dl:
                        chosen = i
                        break
            if chosen is None:
                chosen = 0
        out_path = vp.with_suffix(".mkv") if replace else vp.with_name(vp.stem + ".muxed.mkv")
        tmp_out = str(vp.with_name(vp.stem + ".muxing.tmp.mkv"))
        cmd = [mm, "-o", tmp_out]
        if chosen is not None:
            for t in _mkv_probe_full(mm, v)["subs"]:
                cmd += ["--default-track", f"{t['id']}:no"]
        cmd += [str(v)]
        for i, (subfile, lang3, name, forced) in enumerate(metas):
            cmd += ["--language", f"0:{lang3}", "--track-name", f"0:{name}",
                    "--default-track", "0:yes" if (chosen is not None and i == chosen) else "0:no",
                    "--forced-track", "0:yes" if forced else "0:no", str(subfile)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode >= 2 or not os.path.exists(tmp_out):
            tail = " | ".join([l for l in (res.stdout or "").splitlines() if l.strip()][-3:])
            log_warn(f"{vp.name}: mkvmerge selhalo: {tail}")
            failed += 1
            try:
                os.path.exists(tmp_out) and os.remove(tmp_out)
            except OSError:
                pass
            continue
        try:
            if replace:
                if vp.suffix.lower() != ".mkv":
                    os.remove(str(v))  # původní mp4 nahrazujeme mkv
                os.replace(tmp_out, str(out_path))
            else:
                os.replace(tmp_out, str(out_path))
            log_done(f"{vp.name}: vloženo {len(metas)} titulků -> {out_path.name}")
            done += 1
        except OSError as e:
            log_warn(f"{vp.name}: náhrada selhala: {e}")
            failed += 1
    print()
    log_done(f"Hotovo: {done} videí, {failed} chyb.")


# --------------------------------------------------------- remove tracks
def run_remove_tracks(args):
    """Odebere zvolené audio/titulkové stopy z MKV (mkvmerge -c copy)."""
    directory = str(args.mkv) if args.mkv and args.mkv.is_dir() else "."
    print(f"{Fore.MAGENTA}=== Odebrat audio/titulkové stopy z MKV ==={Style.RESET_ALL}")
    log_info(f"Pracovní adresář: {os.path.abspath(directory)}")
    recursive = ask_yes_no("Prohledat i podadresáře?", default_no=True)
    mm, me, mp = _ensure_mkv_tools(args, directory)
    if not mm:
        die("Nenašel jsem mkvmerge (MKVToolNix) ani se ho nepodařilo stáhnout.")

    mkvs = [v for v in collect_videos(directory, recursive) if Path(v).suffix.lower() == ".mkv"]
    if not mkvs:
        die("Žádné .mkv soubory (tenhle režim umí jen Matroska).")
    log_info(f"Nalezeno {len(mkvs)} MKV. Čtu stopy...")
    infos = {v: _mkv_probe_full(mm, v) for v in mkvs}

    def aggregate(kind):
        counts, order = {}, []
        for info in infos.values():
            for tr in info[kind]:
                l = tr["lang"]
                if l not in counts:
                    counts[l] = 0
                    order.append(l)
                counts[l] += 1
        return [(l, counts[l]) for l in order]

    def ask_remove_langs(kind_label, langs):
        if not langs:
            return set()
        print(f"\n{Fore.CYAN}{kind_label}:{Style.RESET_ALL}")
        for i, (l, n) in enumerate(langs, 1):
            print(f"  {i}) {_lang3_name(l)}  ({n}×)")
        print("  0) nic neodstraňovat")
        raw = ask_text(f"Které {kind_label.lower()} ODSTRANIT (čísla/kódy, čárkou; 0=nic)", "0")
        if raw.strip() in ("", "0"):
            return set()
        out = set()
        for part in re.split(r"[,\s]+", raw.strip().lower()):
            if not part:
                continue
            if part.isdigit() and 1 <= int(part) <= len(langs):
                out.add(langs[int(part) - 1][0])
            else:
                cc = _canon3(part)
                if cc in [l for l, _ in langs]:
                    out.add(cc)
        return out

    a_langs = aggregate("audio")
    s_langs = aggregate("subs")
    rem_audio = ask_remove_langs("Audio jazyky", a_langs)
    rem_subs = ask_remove_langs("Titulkové jazyky", s_langs)
    if not rem_audio and not rem_subs:
        log_warn("Nic k odstranění.")
        return
    replace = ask_yes_no("Přepsat originály? (jinak uložím do podsložky 'trimmed')", default_no=True)

    print()
    log_info("K odstranění -- audio: " + (",".join(_lang3_name(l) for l in rem_audio) or "nic")
             + " | titulky: " + (",".join(_lang3_name(l) for l in rem_subs) or "nic"))
    if not ask_yes_no(f"Spustit pro {len(mkvs)} MKV?", default_no=False):
        log_warn("Zrušeno uživatelem.")
        return
    preset_flush_if_save()

    outdir = None
    if not replace:
        outdir = os.path.join(directory, "trimmed")
        os.makedirs(outdir, exist_ok=True)

    done = skipped = 0
    for v in mkvs:
        vp = Path(v)
        info = infos[v]
        a_all = [t["id"] for t in info["audio"]]
        s_all = [t["id"] for t in info["subs"]]
        a_keep = [t["id"] for t in info["audio"] if t["lang"] not in rem_audio]
        s_keep = [t["id"] for t in info["subs"] if t["lang"] not in rem_subs]
        if a_keep == a_all and s_keep == s_all:
            log_info(f"{vp.name}: nic k odstranění - přeskakuji.")
            skipped += 1
            continue
        if a_all and not a_keep:
            log_warn(f"{vp.name}: odstranění by smazalo VŠECHNO audio - přeskakuji.")
            skipped += 1
            continue
        out_path = vp if replace else Path(outdir) / vp.name
        tmp_out = str(vp.with_name(vp.stem + ".trim.tmp.mkv"))
        cmd = [mm, "-o", tmp_out]
        if a_all and not a_keep:
            cmd += ["--no-audio"]
        elif a_keep and len(a_keep) < len(a_all):
            cmd += ["--audio-tracks", ",".join(str(i) for i in a_keep)]
        if s_all and not s_keep:
            cmd += ["--no-subtitles"]
        elif s_keep and len(s_keep) < len(s_all):
            cmd += ["--subtitle-tracks", ",".join(str(i) for i in s_keep)]
        cmd += [str(v)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode >= 2 or not os.path.exists(tmp_out):
            tail = " | ".join([l for l in (res.stdout or "").splitlines() if l.strip()][-3:])
            log_warn(f"{vp.name}: mkvmerge selhalo: {tail}")
            skipped += 1
            try:
                os.path.exists(tmp_out) and os.remove(tmp_out)
            except OSError:
                pass
            continue
        try:
            os.replace(tmp_out, str(out_path))
            log_done(f"{vp.name}: audio {len(a_keep)}/{len(a_all)}, titulky {len(s_keep)}/{len(s_all)} "
                     f"-> {out_path.name if replace else 'trimmed/' + out_path.name}")
            done += 1
        except OSError as e:
            log_warn(f"{vp.name}: náhrada selhala: {e}")
            skipped += 1
    print()
    log_done(f"Hotovo: {done} MKV upraveno, {skipped} přeskočeno.")


# ---------------------------------------------------- set default tracks
def run_set_default(args):
    """Nastaví výchozí (default) audio/titulkovou stopu podle jazyka. MKV přes
    mkvpropedit (na místě), MP4 přes ffmpeg (přemux)."""
    directory = str(args.mkv) if args.mkv and args.mkv.is_dir() else "."
    print(f"{Fore.MAGENTA}=== Nastavit výchozí (default) stopu podle jazyka ==={Style.RESET_ALL}")
    log_info(f"Pracovní adresář: {os.path.abspath(directory)}")
    recursive = ask_yes_no("Prohledat i podadresáře?", default_no=True)
    mm, me, mp = _ensure_mkv_tools(args, directory, need_propedit=True)
    if not mm:
        die("Nenašel jsem mkvmerge (MKVToolNix) ani se ho nepodařilo stáhnout.")

    videos = collect_videos(directory, recursive)
    if not videos:
        die("Žádná videa (mkv/mp4) v adresáři.")
    has_mp4 = any(Path(v).suffix.lower() == ".mp4" for v in videos)
    ffmpeg_bin = None
    if has_mp4:
        ffmpeg_bin = getattr(args, "ffmpeg", None) or ensure_ffmpeg(directory, allow_download=not getattr(args, "no_ffmpeg_download", False))
    log_info(f"Nalezeno {len(videos)} videí. Čtu stopy...")
    infos = {v: _mkv_probe_full(mm, v) for v in videos}

    def aggregate(kind):
        counts, order = {}, []
        for info in infos.values():
            for tr in info[kind]:
                l = tr["lang"]
                if l not in counts:
                    counts[l] = 0
                    order.append(l)
                counts[l] += 1
        return [(l, counts[l]) for l in order]

    def ask_lang(kind_label, langs, allow_none):
        if not langs:
            return "keep"
        print(f"\n{Fore.CYAN}{kind_label}:{Style.RESET_ALL}")
        for i, (l, n) in enumerate(langs, 1):
            print(f"  {i}) {_lang3_name(l)}  ({n}×)")
        extra = len(langs)
        print(f"  {extra + 1}) neměnit")
        if allow_none:
            print(f"  {extra + 2}) žádná (zrušit všechny default flagy)")
        raw = ask_text(f"Výchozí {kind_label.lower()} - vyber číslo nebo kód", str(extra + 1)).strip().lower()
        if raw == str(extra + 1) or raw in ("keep", ""):
            return "keep"
        if allow_none and (raw == str(extra + 2) or raw == "none"):
            return "none"
        if raw.isdigit() and 1 <= int(raw) <= len(langs):
            return langs[int(raw) - 1][0]
        return _canon3(raw)

    a_choice = ask_lang("Audio jazyk", aggregate("audio"), allow_none=False)
    s_choice = ask_lang("Titulkový jazyk", aggregate("subs"), allow_none=True)
    if a_choice == "keep" and s_choice == "keep":
        log_warn("Nic k nastavení.")
        return

    print()
    log_info(f"Výchozí audio: {a_choice} | výchozí titulky: {s_choice}")
    if not ask_yes_no(f"Spustit pro {len(videos)} videí?", default_no=False):
        log_warn("Zrušeno uživatelem.")
        return
    preset_flush_if_save()

    def desired_default(tracks, choice):
        """Vrátí {sel/rel -> bool} co má být default (první stopa daného jazyka)."""
        res = {}
        if choice == "keep":
            return None
        picked = False
        for tr in tracks:
            want = False
            if choice != "none" and not picked and tr["lang"] == choice:
                want = True
                picked = True
            res[tr["id"]] = want
        return res

    done = failed = 0
    for v in videos:
        vp = Path(v)
        info = infos[v]
        a_want = desired_default(info["audio"], a_choice)
        s_want = desired_default(info["subs"], s_choice)
        if vp.suffix.lower() == ".mkv":
            edits = []
            for kind, want in (("audio", a_want), ("subs", s_want)):
                if want is None:
                    continue
                for tr in info[kind]:
                    d = want.get(tr["id"])
                    if d is not None and bool(d) != bool(tr["default"]):
                        edits += ["--edit", f"track:{tr['sel']}", "--set", f"flag-default={1 if d else 0}"]
            if not edits:
                log_info(f"{vp.name}: beze změny.")
                continue
            res = subprocess.run([mp, str(v)] + edits, capture_output=True, text=True)
            if res.returncode >= 2:
                log_warn(f"{vp.name}: mkvpropedit chyba.")
                failed += 1
            else:
                log_done(f"{vp.name}: výchozí stopy nastaveny.")
                done += 1
        else:
            if not ffmpeg_bin:
                log_warn(f"{vp.name}: MP4 vyžaduje ffmpeg - přeskakuji.")
                failed += 1
                continue
            disp = []
            for kind, tkey, want in (("audio", "a", a_want), ("subs", "s", s_want)):
                if want is None:
                    continue
                for rel, tr in enumerate(info[kind]):
                    flags = []
                    if want.get(tr["id"]):
                        flags.append("default")
                    if tr["forced"]:
                        flags.append("forced")
                    disp += [f"-disposition:{tkey}:{rel}", "+".join(flags) if flags else "0"]
            tmp = str(v) + ".deftmp.mp4"
            cmd = [ffmpeg_bin, "-y", "-i", str(v), "-map", "0", "-c", "copy"] + disp + ["-default_mode", "passthrough", tmp]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0 or not os.path.exists(tmp):
                log_warn(f"{vp.name}: ffmpeg chyba.")
                failed += 1
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                continue
            try:
                os.replace(tmp, str(v))
                log_done(f"{vp.name}: výchozí stopy nastaveny (přemux).")
                done += 1
            except OSError as e:
                log_warn(f"{vp.name}: náhrada selhala: {e}")
                failed += 1
    print()
    log_done(f"Hotovo: {done} videí, {failed} chyb/přeskočeno.")


# -------------------------------------------------------- rename subs
def run_rename_subs(args):
    """Přejmenuje titulky (.srt) podle názvů videí (párování přes SxxExx),
    zachová jazykovou/forced koncovku."""
    directory = str(args.mkv) if args.mkv and args.mkv.is_dir() else "."
    print(f"{Fore.MAGENTA}=== Přejmenovat titulky podle názvů videí ==={Style.RESET_ALL}")
    log_info(f"Pracovní adresář: {os.path.abspath(directory)}")
    recursive = ask_yes_no("Prohledat i podadresáře?", default_no=True)

    videos, subs = _collect_videos_subs(directory, recursive, sub_exts=(".srt",))
    if not subs:
        die("Žádné .srt titulky v adresáři.")
    vmap = {}
    for v in sorted(videos):
        k = _episode_key(os.path.basename(v))
        if k and k not in vmap:
            vmap[k] = v

    planned = []
    used = {}
    for s in sorted(subs):
        sdir = os.path.dirname(s)
        sname = os.path.basename(s)
        k = _episode_key(sname)
        if not k or k not in vmap:
            continue
        stem = sname[:-4]
        parts = stem.split(".")
        suffix = []
        while parts and len(suffix) < 2 and (parts[-1].lower() in _FLAG_TOKENS or _LANG_TOKEN_RE.match(parts[-1])):
            suffix.insert(0, parts.pop())
        vstem = os.path.splitext(os.path.basename(vmap[k]))[0]
        new_name = vstem + ("." + ".".join(suffix) if suffix else "") + ".srt"
        dst = os.path.join(sdir, new_name)
        if os.path.abspath(dst) == os.path.abspath(s) or dst in used or os.path.exists(dst):
            continue
        used[dst] = s
        planned.append((s, dst))

    if not planned:
        log_warn("Nic k přejmenování (buď chybí SxxExx, videa, nebo už sedí).")
        return
    print(f"\nPlán přejmenování ({len(planned)}):")
    for s, d in planned[:30]:
        print(f"  {os.path.basename(s)}\n   -> {os.path.basename(d)}")
    if len(planned) > 30:
        print(f"  ... a další {len(planned) - 30}")
    print()
    if not ask_yes_no(f"Přejmenovat {len(planned)} titulků?", default_no=False):
        log_warn("Zrušeno uživatelem.")
        return
    preset_flush_if_save()
    done = 0
    for s, d in planned:
        try:
            os.rename(s, d)
            done += 1
        except OSError as e:
            log_warn(f"{os.path.basename(s)}: {e}")
    log_done(f"Hotovo, přejmenováno {done} titulků.")



def run_transplant(args):
    """Průvodce: nahradí STROJOVÝ překlad (dobré časování) PROFESIONÁLNÍM textem
    ze samostatného adresáře (jiná verze pořadu, jiné dělení epizod), časování
    zůstává. Párování podle OBSAHU, stejný jazyk."""
    if args.mkv and args.mkv.is_dir():
        directory = str(args.mkv)
    elif args.mkv and args.mkv.exists():
        directory = os.path.dirname(str(args.mkv)) or "."
    else:
        directory = "."

    print(f"{Fore.MAGENTA}=== Nahradit strojový překlad profesionálním (podle obsahu) ==={Style.RESET_ALL}")
    log_info(f"Strojové titulky (dobré časování): {os.path.abspath(directory)}")
    log_info("Vezmu tvoje strojové/AI titulky a text nahradím profesionálním překladem stejného "
             "pořadu z jiného adresáře. ČASOVÁNÍ zůstane tvoje (sedí na tvou verzi).")

    recursive = ask_yes_no("Hledat strojové titulky i v podadresářích?", default_no=True)
    targets = collect_srts(directory, recursive)
    if not targets:
        die("Ve složce nejsou žádné .srt (strojové titulky).")
    sel = ask_pick(f"Které strojové titulky zpracovat? (nalezeno {len(targets)})",
                   ["všechny", "jeden", "několik (vyberu čísla)"], default=0)
    if sel == 0:
        chosen = targets
    elif sel == 1:
        idx = ask_pick("Který soubor?", [os.path.basename(s) for s in targets], default=0)
        chosen = [targets[idx]]
    else:
        for k, s in enumerate(targets, 1):
            print(f"  {k}) {os.path.basename(s)}")
        raw = ask_text("Zadej čísla oddělená čárkou (např. 1,3,4)", "")
        chosen = [targets[int(t) - 1] for t in raw.replace(" ", "").split(",")
                  if t.isdigit() and 1 <= int(t) <= len(targets)] or targets

    # adresář s profesionálními ("viki") titulky
    while True:
        viki_dir = ask_text("Adresář s PROFESIONÁLNÍMI ('viki') titulky", "").strip().strip('"')
        if not viki_dir:
            log_warn("Bez adresáře s profesionálními titulky to nejde. Zrušeno.")
            return
        if os.path.isdir(viki_dir):
            break
        log_warn(f"Adresář '{viki_dir}' neexistuje - zkus to znovu (nebo Enter = konec).")
    viki_rec = ask_yes_no("Hledat 'viki' titulky i v podadresářích?", default_no=True)
    pool, viki_files = _load_reference_pool(viki_dir, viki_rec)
    if not pool:
        die("V adresáři s profesionálními titulky nejsou žádné .srt.")
    log_info(f"Profesionální zásoba: {len(pool)} titulků z {len(viki_files)} souborů "
             f"(dělení epizod nevadí - páruje se podle obsahu).")

    # kontrola jazyka (musí být stejný)
    lp = detect_sub_language(pool)
    lt = detect_srt_file_language(chosen[0]) if chosen else None
    if lp and lt and lp != lt:
        log_warn(f"Pozor: strojové titulky vypadají na '{lt}', profesionální na '{lp}'. "
                 "Tahle funkce páruje podle textu, takže OBA musí být ve STEJNÉM jazyce. "
                 "Když jsou různé, výsledek nebude dávat smysl.")
        if not ask_yes_no("Přesto pokračovat?", default_no=True):
            return

    ti = ask_pick("Jak přísně párovat (kompromis kvalita vs. pokrytí)?",
                  ["konzervativně - jen jisté shody (nejmíň chyb, míň nahrazení)",
                   "vyváženě - rozumný kompromis (doporučeno)",
                   "agresivně - víc nahrazení, ale vyšší riziko chyb"], default=1,
                  help=["konzervativně: nahradí jen tam, kde je shoda velmi jistá. Nejbezpečnější, "
                        "ale profesionálním textem pokryje míň titulků; zbytek zůstane strojový.",
                        "vyváženě: rozumná rovnováha mezi počtem nahrazení a spolehlivostí.",
                        "agresivně: nahradí víc titulků, ale roste šance na chybné spárování "
                        "(hlavně u krátkých/opakujících se řádků)."])
    min_sim = [0.65, 0.55, 0.45][ti]

    args.fix_short_duration = False
    _ask_readability(args, chosen[:5])

    overwrite = ask_yes_no("Přepsat původní strojový soubor? (jinak uložím vedle jako <jméno>.pro.srt)",
                           default_no=True)

    print()
    log_info(f"Práh párování: {min_sim:.2f} | soubory: {len(chosen)} | profi zásoba: {len(pool)} "
             + ("| čitelnost: ano " if getattr(args, 'fix_short_duration', False) else "")
             + ("| přepis originálu" if overwrite else "| ukládám jako .pro.srt"))
    if not ask_yes_no(f"Spustit pro {len(chosen)} souborů?", default_no=False):
        log_warn("Zrušeno uživatelem.")
        return
    preset_flush_if_save()

    log_info("Připravuji profesionální zásobu (jednorázově)...")
    transplant, _M = build_transplanter(pool)

    done = skipped = 0
    tot_repl = tot_cues = 0
    for path in chosen:
        p = Path(path)
        try:
            ev = parse_srt(p, strict=False)
        except Exception as e:
            log_warn(f"{p.name}: nelze načíst ({e}) - přeskakuji.")
            skipped += 1
            continue
        new_ev, n_repl, n_tot = transplant(ev, min_sim=min_sim)
        if getattr(args, "fix_short_duration", False):
            cps, floor, gap, overhead = resolve_speed_params(args)
            new_ev, _n = fix_short_durations(new_ev, min_cps=cps, min_duration_floor=floor,
                                             min_gap=gap, line_overhead=overhead)
        out_path = p if overwrite else p.with_name(p.stem + ".pro.srt")
        try:
            write_srt(new_ev, out_path)
            pct = (100 * n_repl / n_tot) if n_tot else 0
            log_done(f"{p.name} -> {out_path.name}: nahrazeno {n_repl}/{n_tot} ({pct:.0f}%) "
                     f"profesionálním textem, zbytek ponechán strojový.")
            done += 1
            tot_repl += n_repl
            tot_cues += n_tot
        except Exception as e:
            log_warn(f"{p.name}: zápis selhal: {e}")
            skipped += 1

    print()
    pct = (100 * tot_repl / tot_cues) if tot_cues else 0
    log_done(f"Hotovo: {done} souborů uloženo, {skipped} přeskočeno. Celkem nahrazeno "
             f"{tot_repl}/{tot_cues} titulků ({pct:.0f}%).")
    if pct < 20:
        log_warn("Nízké pokrytí - buď jsou překlady dost odlišné, jde o jinou verzi/pořad, "
                 "nebo zkus 'agresivně'. Zkontroluj i, že oba jsou ve stejném jazyce.")


def run_resync_pro(args):
    """Průvodce: vezme PROFESIONÁLNÍ titulky z jiného adresáře (jiná verze/dělení
    epizod) a PŘEČASUJE je na časování tvých strojových titulků. Výsledek = 100 %
    profesionálního textu se správným časováním pro tvé video."""
    if args.mkv and args.mkv.is_dir():
        directory = str(args.mkv)
    elif args.mkv and args.mkv.exists():
        directory = os.path.dirname(str(args.mkv)) or "."
    else:
        directory = "."

    print(f"{Fore.MAGENTA}=== Přečasovat profesionální titulky na moje časování (100% profi text) ==={Style.RESET_ALL}")
    log_info(f"Tvoje titulky s dobrým časováním: {os.path.abspath(directory)}")
    log_info("Vezmu profesionální překlad téže show z jiného adresáře a přečasuju ho na tvoje "
             "časování. Zůstane CELÝ profesionální text, jen dostane timing tvého videa.")

    recursive = ask_yes_no("Hledat tvoje titulky i v podadresářích?", default_no=True)
    targets = collect_srts(directory, recursive)
    if not targets:
        die("Ve složce nejsou žádné .srt (tvoje strojové titulky s dobrým časováním).")
    sel = ask_pick(f"Které tvoje titulky (šablona časování) použít? (nalezeno {len(targets)})",
                   ["všechny", "jeden", "několik (vyberu čísla)"], default=0)
    if sel == 0:
        chosen = targets
    elif sel == 1:
        idx = ask_pick("Který soubor?", [os.path.basename(s) for s in targets], default=0)
        chosen = [targets[idx]]
    else:
        for k, s in enumerate(targets, 1):
            print(f"  {k}) {os.path.basename(s)}")
        raw = ask_text("Zadej čísla oddělená čárkou (např. 1,3,4)", "")
        chosen = [targets[int(t) - 1] for t in raw.replace(" ", "").split(",")
                  if t.isdigit() and 1 <= int(t) <= len(targets)] or targets

    while True:
        viki_dir = ask_text("Adresář s PROFESIONÁLNÍMI ('viki') titulky", "").strip().strip('"')
        if not viki_dir:
            log_warn("Bez adresáře s profesionálními titulky to nejde. Zrušeno.")
            return
        if os.path.isdir(viki_dir):
            break
        log_warn(f"Adresář '{viki_dir}' neexistuje - zkus to znovu (nebo Enter = konec).")
    viki_rec = ask_yes_no("Hledat 'viki' titulky i v podadresářích?", default_no=True)
    pool, viki_files = _load_reference_pool(viki_dir, viki_rec, continuous=True)
    if not pool:
        die("V adresáři s profesionálními titulky nejsou žádné .srt.")
    log_info(f"Profesionální zásoba: {len(pool)} titulků z {len(viki_files)} souborů "
             f"(jiné dělení epizod nevadí - správný výřez se najde podle obsahu).")

    lp = detect_sub_language(pool)
    lt = detect_srt_file_language(chosen[0]) if chosen else None
    if lp and lt and lp != lt:
        log_warn(f"Pozor: tvoje titulky vypadají na '{lt}', profesionální na '{lp}'. Přečasování "
                 "páruje podle textu, takže OBA by měly být ve stejném jazyce.")
        if not ask_yes_no("Přesto pokračovat?", default_no=True):
            return

    args.fix_short_duration = False
    _ask_readability(args, chosen[:5])
    overwrite = ask_yes_no("Přepsat můj původní soubor profesionálním? (jinak uložím vedle jako <jméno>.pro.srt)",
                           default_no=True)

    print()
    log_info(f"Soubory: {len(chosen)} | profi zásoba: {len(pool)} "
             + ("| čitelnost: ano " if getattr(args, 'fix_short_duration', False) else "")
             + ("| přepis originálu" if overwrite else "| ukládám jako .pro.srt"))
    if not ask_yes_no(f"Spustit pro {len(chosen)} souborů?", default_no=False):
        log_warn("Zrušeno uživatelem.")
        return
    preset_flush_if_save()

    done = skipped = 0
    for path in chosen:
        p = Path(path)
        try:
            ev = parse_srt(p, strict=False)
        except Exception as e:
            log_warn(f"{p.name}: nelze načíst ({e}) - přeskakuji.")
            skipped += 1
            continue
        out, n_anchors = retime_professional(ev, pool, min_sim=0.5)
        if out is None:
            log_warn(f"{p.name}: málo shod ({n_anchors} kotev) - nelze spolehlivě přečasovat, "
                     "přeskakuji. (Jsou to opravdu tytéž díly a stejný jazyk?)")
            skipped += 1
            continue
        if getattr(args, "fix_short_duration", False):
            cps, floor, gap, overhead = resolve_speed_params(args)
            out, _n = fix_short_durations(out, min_cps=cps, min_duration_floor=floor,
                                          min_gap=gap, line_overhead=overhead)
        out_path = p if overwrite else p.with_name(p.stem + ".pro.srt")
        try:
            write_srt(out, out_path)
            log_done(f"{p.name} -> {out_path.name}: {len(out)} profesionálních titulků "
                     f"přečasováno na tvůj timing ({n_anchors} kotev).")
            done += 1
        except Exception as e:
            log_warn(f"{p.name}: zápis selhal: {e}")
            skipped += 1

    print()
    log_done(f"Hotovo: {done} souborů uloženo, {skipped} přeskočeno.")
    log_info("Tip: pokud časování někde ujíždí, měj profi zásobu co nejúplnější (všechny díly "
             "dané série), ať je dost kotev; přečasování je tím přesnější.")


def process_single(args):
    """Zpracuje JEDEN video soubor: vytáhne referenci (titulky/zvuk),
    dopočítá časování zvolenou metodou a uloží výsledek."""
    is_mkv_container = args.mkv.suffix.lower() in MKVEXTRACT_CONTAINER_EXTS
    need_sub_extraction = args.audio_mode in ("off", "combine")
    need_audio = args.audio_mode in ("replace", "combine")
    need_ffmpeg = need_audio or (need_sub_extraction and not is_mkv_container)

    mkvmerge_bin = args.mkvmerge or find_tool(["mkvmerge", "mkvmerge.exe"])
    mkvextract_bin = args.mkvextract or find_tool(["mkvextract", "mkvextract.exe"])
    need_mkvextract = need_sub_extraction and is_mkv_container
    if not mkvmerge_bin or (need_mkvextract and not mkvextract_bin):
        global MKVMERGE, MKVEXTRACT
        if args.mkvmerge:
            MKVMERGE = args.mkvmerge
        if args.mkvextract:
            MKVEXTRACT = args.mkvextract
        mkvmerge_bin, mkvextract_bin = ensure_mkvtoolnix(
            str(args.mkv.parent), allow_download=not args.no_mkvtoolnix_download
        )
        if mkvmerge_bin:
            log_info(f"mkvmerge: {mkvmerge_bin}")
        if mkvextract_bin:
            log_info(f"mkvextract: {mkvextract_bin}")

    if not mkvmerge_bin:
        die(
            "mkvmerge nenalezen a automatické stažení se nepodařilo / je vypnuté. "
            "Stáhni a nainstaluj MKVToolNix z https://mkvtoolnix.download/downloads.html#windows "
            "(instalátor nabízí přidání do PATH), nebo použij --mkvmerge s plnou cestou "
            "k mkvmerge.exe (obvykle C:\\Program Files\\MKVToolNix\\mkvmerge.exe). Používá se "
            "i pro MP4 jen na výpis/identifikaci stop, samotnou extrakci z MP4 dělá ffmpeg."
        )
    if need_mkvextract and not mkvextract_bin:
        die(
            "mkvextract nenalezen a automatické stažení se nepodařilo / je vypnuté "
            "(potřebný pro extrakci titulků z .mkv/.webm). Nainstaluj MKVToolNix nebo "
            "použij --mkvextract s plnou cestou k .exe."
        )

    ffmpeg_bin = None
    if need_ffmpeg:
        global FFMPEG
        if args.ffmpeg:
            FFMPEG = args.ffmpeg
        ffmpeg_bin = ensure_ffmpeg(str(args.mkv.parent), allow_download=not args.no_ffmpeg_download)
        if not ffmpeg_bin:
            reasons = []
            if need_audio:
                reasons.append("zvukovou analýzu (VAD)")
            if need_sub_extraction and not is_mkv_container:
                reasons.append("extrakci titulků z MP4 (mkvextract umí jen .mkv/.webm)")
            die(
                f"Potřebuji ffmpeg ({' a '.join(reasons)}) a automatické stažení se "
                "nepodařilo / je vypnuté. Stáhni manuálně z https://www.gyan.dev/ffmpeg/builds/, "
                "rozbal do '.ffmpeg' vedle tohoto skriptu, nebo zadej --ffmpeg s plnou cestou "
                "k ffmpeg.exe."
            )
        log_info(f"ffmpeg: {ffmpeg_bin}")

    sub_tracks = mkvmerge_tracks(mkvmerge_bin, args.mkv, "subtitles")
    audio_tracks = mkvmerge_tracks(mkvmerge_bin, args.mkv, "audio")

    if args.list_tracks or not args.subtitle_to_fix or not args.output:
        if not sub_tracks:
            print("Žádné titulkové stopy nenalezeny.")
        else:
            print(f"{Fore.MAGENTA}Dostupné titulkové stopy:{Style.RESET_ALL}")
            for t in sub_tracks:
                print(f"  ID={t['id']:>3}  jazyk={t['lang']:<5} kodek={t['codec']:<20} titulek={t['title']}")
        if not audio_tracks:
            print("Žádné zvukové stopy nenalezeny.")
        else:
            print(f"{Fore.MAGENTA}Dostupné zvukové stopy:{Style.RESET_ALL}")
            for t in audio_tracks:
                print(f"  ID={t['id']:>3}  jazyk={t['lang']:<5} kodek={t['codec']:<20} titulek={t['title']}")
        if not args.list_tracks:
            print("\nPoužití: python sync_subtitles.py video.mkv titulky.srt vystup.srt [--ref-lang eng] [--audio-mode combine]")
        return

    if not args.subtitle_to_fix.exists():
        die(f"Soubor s titulky k opravě neexistuje: {args.subtitle_to_fix}")

    with tempfile.TemporaryDirectory() as tmpdir:
        ref_events_sub = []
        ref_events_audio = []

        if args.audio_mode in ("off", "combine"):
            chosen_sub = pick_reference_track(sub_tracks, args.ref_lang, args.track_id)
            log_info(f"Referenční titulková stopa: ID={chosen_sub['id']} jazyk={chosen_sub['lang']} kodek={chosen_sub['codec']}")
            ref_srt_path = Path(tmpdir) / "reference.srt"
            if is_mkv_container:
                extract_subtitle_to_srt(mkvextract_bin, args.mkv, chosen_sub["id"], ref_srt_path)
            else:
                sub_position = [t["id"] for t in sub_tracks].index(chosen_sub["id"])
                extract_subtitle_via_ffmpeg(ffmpeg_bin, args.mkv, sub_position, ref_srt_path)
            ref_events_sub = parse_srt(ref_srt_path)
            log_info(f"Referenčních titulků: {len(ref_events_sub)}")

        if args.audio_mode in ("replace", "combine"):
            chosen_audio = pick_audio_track(audio_tracks, args.audio_lang, args.audio_track_id)
            audio_position = [t["id"] for t in audio_tracks].index(chosen_audio["id"])
            log_info(f"Referenční zvuková stopa: ID={chosen_audio['id']} jazyk={chosen_audio['lang']} kodek={chosen_audio['codec']}")

            wav_path = Path(tmpdir) / "reference_audio.wav"
            log_info("Extrahuji a dekóduji zvukovou stopu (ffmpeg)...")
            extract_audio_wav(ffmpeg_bin, args.mkv, audio_position, wav_path)

            samples, sr = read_wav_mono(wav_path)
            log_info(f"Zvuková stopa: {len(samples) / sr:.1f} s, {sr} Hz - hledám úseky řeči (VAD)...")
            ref_events_audio = detect_speech_events(samples, sr, energy_percentile=args.vad_percentile)
            log_info(f"Detekováno {len(ref_events_audio)} úseků řeči")

        if args.audio_mode == "off":
            ref_events = ref_events_sub
        elif args.audio_mode == "replace":
            ref_events = ref_events_audio
        else:  # combine
            ref_events = sorted(ref_events_sub + ref_events_audio, key=lambda e: e["start"])
            log_info(f"Kombinovaná referenční osa: {len(ref_events)} kotev (titulky + řeč)")

        target_events = parse_srt(args.subtitle_to_fix)
        log_info(f"Opravovaných titulků: {len(target_events)}")

        # reálná detekce jazyka z OBSAHU (po extrakci reference) a varování při
        # neshodě, když běží obsahová metoda bez překladu
        if getattr(args, "method", "auto") in ("auto", "warp", "combo") \
                and getattr(args, "translate", "off") == "off" and ref_events_sub:
            tl = detect_sub_language(target_events)
            rl = detect_sub_language(ref_events_sub)
            if tl and rl and tl != rl:
                log_warn(f"Detekované jazyky se liší (opravované='{tl}', reference='{rl}'). "
                         "Obsahové párování (warp) bude slabé - zvaž --translate google "
                         "(nebo --method affine). Bez překladu se použije afinní fáze.")

        corrected = run_alignment(args, ref_events, ref_events_sub, target_events)

        if args.fix_short_duration:
            cps, floor, gap, overhead = resolve_speed_params(args)
            corrected, n_extended = fix_short_durations(
                corrected, min_cps=cps, min_duration_floor=floor, min_gap=gap, line_overhead=overhead,
            )
            log_info(f"Prodlouženo {n_extended} titulků se zkráceným zobrazením (využita volná místa)")

        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_srt(corrected, args.output)

    log_done(f"Synchronizované titulky uloženy do: {args.output}")


# ----------------------------------------------------------------------
# Interaktivní průvodci (--auto pro jeden soubor, --auto-all pro dávku)
# ----------------------------------------------------------------------

SUB_EXTS_AUTO = (".srt", ".ass", ".ssa", ".vtt")


def collect_subs(directory):
    """Najde titulkové soubory v adresáři (.srt/.ass/.ssa/.vtt a .orig
    referenční varianty jako 'jmeno.srt.orig')."""
    out = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return out
    for f in names:
        full = os.path.join(directory, f)
        if not os.path.isfile(full):
            continue
        low = f.lower()
        if low.endswith(SUB_EXTS_AUTO) or low.endswith(".orig"):
            out.append(full)
    return out


def ask_pick(prompt, labels, default=0, help=None):
    """Číslovaný výběr s výchozí volbou (Enter). Vrací index do labels.
    Když je zadán 'help' (seznam/řetězec), '?' vypíše bližší vysvětlení."""
    r = _preset_replay()
    if r is not _PRESET_MISS:
        try:
            r = int(r)
        except Exception:
            r = default
        if not (0 <= r < len(labels)):
            r = default if 0 <= default < len(labels) else 0
        return r
    hint = f"{Fore.CYAN} (? = více info){Style.RESET_ALL}" if help else ""

    def _show():
        print(f"{Fore.YELLOW}{prompt}{Style.RESET_ALL}{hint}")
        for i, l in enumerate(labels):
            mark = f" {Fore.CYAN}(výchozí){Style.RESET_ALL}" if i == default else ""
            print(f"  {i + 1}) {l}{mark}")

    _show()
    while True:
        raw = input(f"Volba [1-{len(labels)}, Enter = {default + 1}]: ").strip()
        if raw == "?" and help:
            if isinstance(help, (list, tuple)):
                for h in help:
                    print(f"  {Fore.CYAN}•{Style.RESET_ALL} {h}")
            else:
                print(f"  {help}")
            _show()
            continue
        val = None
        if raw == "":
            val = default
        elif raw.isdigit() and 1 <= int(raw) <= len(labels):
            val = int(raw) - 1
        if val is not None:
            _preset_record("pick", prompt, val)
            return val
        print(f"{Fore.RED}Neplatná volba, zkus to znovu.{Style.RESET_ALL}")


def ask_text(prompt, default=""):
    r = _preset_replay()
    if r is not _PRESET_MISS:
        return r if r is not None else default
    suffix = f" [{default}]" if default else ""
    raw = input(f"{prompt}{suffix}: ").strip()
    val = raw or default
    _preset_record("text", prompt, val)
    return val


LANGUAGE_NAMES = {
    "cs": "čeština", "sk": "slovenština", "en": "angličtina", "de": "němčina",
    "pl": "polština", "fr": "francouzština", "es": "španělština", "it": "italština",
    "pt": "portugalština", "nl": "nizozemština", "ru": "ruština", "uk": "ukrajinština",
    "hu": "maďarština", "ro": "rumunština", "bg": "bulharština", "el": "řečtina",
    "tr": "turečtina", "sv": "švédština", "no": "norština", "da": "dánština",
    "fi": "finština", "is": "islandština", "hr": "chorvatština", "sr": "srbština",
    "sl": "slovinština", "et": "estonština", "lv": "lotyština", "lt": "litevština",
    "ga": "irština", "mt": "maltština", "ar": "arabština", "he": "hebrejština",
    "fa": "perština", "hi": "hindština", "bn": "bengálština", "ur": "urdština",
    "ta": "tamilština", "th": "thajština", "vi": "vietnamština", "id": "indonéština",
    "ms": "malajština", "tl": "tagalog", "ja": "japonština", "ko": "korejština",
    "zh": "čínština", "zh-cn": "čínština (zjedn.)", "zh-tw": "čínština (trad.)",
    "ca": "katalánština", "eu": "baskičtina", "gl": "galicijština",
    "af": "afrikánština", "sw": "svahilština", "la": "latina",
}


def _print_language_list():
    items = sorted(LANGUAGE_NAMES.items())
    cells = [f"{c:<6}{n}" for c, n in items]
    width = max(len(x) for x in cells) + 2
    cols = 3
    print(f"{Fore.MAGENTA}Běžné jazykové kódy:{Style.RESET_ALL}")
    for i in range(0, len(cells), cols):
        print("  " + "".join(cell.ljust(width) for cell in cells[i:i + cols]))
    try:
        from deep_translator import GoogleTranslator
        sup = GoogleTranslator().get_supported_languages(as_dict=True)  # name -> code
        extra = sorted(set(sup.values()) - set(LANGUAGE_NAMES.keys()))
        if extra:
            print(f"{Fore.CYAN}Další kódy podporované Googlem:{Style.RESET_ALL}")
            for i in range(0, len(extra), 12):
                print("  " + " ".join(extra[i:i + 12]))
    except Exception:
        pass
    print(f"{Fore.CYAN}Pozn.:{Style.RESET_ALL} přesná podpora závisí na službě "
          "(Google ~130, DeepL ~30, OpenSubtitles dle dostupnosti).")


def ask_language(prompt, default=""):
    """Jako ask_text, ale '?' vypíše seznam dostupných jazykových kódů."""
    hint = f"{prompt} (? = seznam jazyků)"
    while True:
        raw = ask_text(hint, default)
        if raw.strip() == "?":
            _print_language_list()
            continue
        return raw


def ask_anthropic_model(prompt, default="", args=None):
    """Jako ask_text, ale '?' vypíše seznam modelů Claude (online dle klíče,
    s token limity a poznámkou k ceně/použití; jinak vestavěný přehled)."""
    hint = f"{prompt} (? = seznam modelů)"
    while True:
        raw = ask_text(hint, default)
        if raw.strip() == "?":
            _print_anthropic_models(args)
            continue
        return raw


def ask_gemini_model(prompt, default="", args=None):
    """Jako ask_text, ale '?' vypíše seznam modelů Gemini (online dle klíče)."""
    hint = f"{prompt} (? = seznam modelů)"
    while True:
        raw = ask_text(hint, default)
        if raw.strip() == "?":
            _print_gemini_models(args)
            continue
        return raw


def _pick_reading_speed():
    keys = list(READING_SPEED_PRESETS.keys())
    labels = [f"{k} - {READING_SPEED_PRESETS[k][2]} "
              f"({READING_SPEED_PRESETS[k][0]:.0f} zn/s, podlaha {READING_SPEED_PRESETS[k][1]:.1f}s)"
              for k in keys]
    return keys[ask_pick("Rychlost čtení (cílové tempo):", labels, default=0)]


def _ask_readability(into_args, srt_files):
    """Zeptá se na prodlužování krátkých titulků; volitelně i na detailní
    parametry (min. doba zobrazení, mezera, příplatek za řádek)."""
    if not ask_yes_no("Prodloužit příliš krátké titulky kvůli čitelnosti "
                      "(jen do volného místa, nikdy přes překryv)?", default_no=True):
        return
    into_args.fix_short_duration = True
    if ask_yes_no("Nastavit čitelnost DETAILNĚ (čtecí tempo, min. doba zobrazení, "
                  "bezpečnostní mezera, příplatek za řádek)?", default_no=True):
        res = ask_readability_params(srt_files or [])
        if res:
            cps, floor, gap, overhead = res
            into_args.min_cps = cps
            into_args.min_duration_floor = floor
            into_args.min_gap = gap
            into_args.line_overhead = overhead
            return
    into_args.reading_speed = _pick_reading_speed()


def _pick_method(into_args):
    mi = ask_pick(
        "Metoda dopočtu časování:",
        ["auto  - doporučeno (kombinace: afinní předsrovnání + warp; jinak afinní)",
         "combo - afinní předsrovnání + warp doladění po větách (nejrobustnější)",
         "warp  - jen po VĚTÁCH (rychlé; potřebuje textovou referenci)",
         "affine - jen globální posun + rychlost (jazykově nezávislé, i ze zvuku)"],
        default=0,
        help=["auto: sám zvolí nejlepší postup - když je textová reference a dost kotev, "
              "udělá combo; jinak spadne na afinní.",
              "combo: nejdřív srovná globální posun+rychlost (afinní), pak doladí po větách "
              "(warp). Nejrobustnější a nejpřesnější.",
              "warp: jen párování po větách + po částech lineární mapa. Opraví i rozsync po "
              "částech, ale potřebuje textovou referenci.",
              "affine: jen jeden globální posun a rychlost (a*t+b). Funguje i jen ze zvuku a "
              "napříč jazyky, ale neopraví rozsync po částech."])
    into_args.method = ["auto", "combo", "warp", "affine"][mi]


def _enable_translate_prompt(into_args):
    ei = ask_pick("Překladač pro mezijazyčné párování:",
                  ["gemini - AI zdarma (Google AI Studio klíč)",
                   "google - online, zdarma bez klíče",
                   "deepl  - lepší kvalita (API klíč)",
                   "claude - přes Anthropic API (placené)",
                   "argos  - offline (pip install argostranslate langdetect)"], default=1)
    into_args.translate = ["gemini", "google", "deepl", "claude", "argos"][ei]
    into_args.pivot_lang = ask_text("Společný jazyk pro párování (pivot)", "en") or "en"
    if into_args.translate == "gemini" and not getattr(into_args, "gemini_key", None):
        into_args.gemini_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
                                or ask_text("Gemini API klíč (zdarma na aistudio.google.com)", "") or None)
    elif into_args.translate == "deepl" and not getattr(into_args, "deepl_key", None):
        into_args.deepl_key = os.environ.get("DEEPL_API_KEY") or ask_text("DeepL API klíč", "") or None
    elif into_args.translate == "claude" and not getattr(into_args, "anthropic_key", None):
        into_args.anthropic_key = os.environ.get("ANTHROPIC_API_KEY") or ask_text("Anthropic API klíč", "") or None


def _setup_languages_translate(into_args, target_lang, ref_lang):
    """Podle DETEKOVANÝCH jazyků (z obsahu) rozhodne o překladu pro párování.
    Pro 'affine' nemá překlad smysl."""
    if into_args.method == "affine":
        return
    log_info(f"Detekovaný jazyk - opravované: {target_lang or '?'}, reference: {ref_lang or '?'}")
    if target_lang and ref_lang:
        if target_lang == ref_lang:
            log_info("Stejný jazyk - překlad pro párování není potřeba.")
            return
        log_warn(f"RŮZNÉ jazyky ({target_lang} vs {ref_lang}). Pro přesné párování "
                 "po větách pomáhá překlad JEN pro účely párování (text se nemění).")
        if ask_yes_no("Zapnout překlad pro mezijazyčné párování?", default_no=False):
            _enable_translate_prompt(into_args)
        else:
            log_info("Bez překladu - různé jazyky se dopočítají afinní fází.")
    else:
        if ask_yes_no("Jazyk se nepodařilo jednoznačně určit. Jsou opravované a referenční "
                      "titulky v RŮZNÝCH jazycích (zapnout překlad pro párování)?", default_no=True):
            _enable_translate_prompt(into_args)


def _resolve_mkvmerge(into_args):
    if getattr(into_args, "mkvmerge", None):
        return into_args.mkvmerge
    return find_tool(["mkvmerge", "mkvmerge.exe"])


def _is_text_sub(codec):
    c = (codec or "").lower()
    return any(x in c for x in ("subrip", "srt", "ass", "ssa", "substation", "vtt", "webvtt", "text"))


def _offer_video_reference(into_args, video_path):
    """Probne reálné stopy ve videu a nabídne je. Vrací (audio_mode, ref_lang)
    a nastaví track_id / audio_track_id podle volby."""
    mkvmerge_bin = _resolve_mkvmerge(into_args)
    subs = audio = None
    if mkvmerge_bin:
        subs, audio, _err = try_list_tracks(mkvmerge_bin, video_path)
    if not subs and not audio:
        log_warn("Nepodařilo se přečíst stopy z videa (chybí mkvmerge nebo nečitelný "
                 "kontejner) - použiju automatický výběr.")
        am = ask_pick("Reference z videa - co použít?",
                      ["titulková stopa (auto)", "zvuk - detekce řeči/VAD", "obojí"], default=0)
        mode = ["off", "replace", "combine"][am]
        rl = None
        if mode in ("off", "combine"):
            rl = norm_lang(ask_language("Jazyk referenční stopy (eng/cze; prázdné=auto)", "") or None)
        return mode, rl

    labels, opts = [], []
    for t in (subs or []):
        kind = "text" if _is_text_sub(t["codec"]) else "obrázkové (nelze jako text)"
        title = f" '{t['title']}'" if t.get("title") else ""
        labels.append(f"titulky #{t['id']}  {t['lang']}  {t['codec']}{title}  [{kind}]")
        opts.append(("off", t["id"], norm_lang(t["lang"])))
    for t in (audio or []):
        title = f" '{t['title']}'" if t.get("title") else ""
        labels.append(f"zvuk   #{t['id']}  {t['lang']}  {t['codec']}{title}  [detekce řeči/VAD]")
        opts.append(("replace", t["id"], None))
    labels.append("obojí: titulková stopa + zvuk dohromady (max. robustnost)")
    opts.append(("combine", None, None))

    idx = ask_pick("Co použít jako referenci? (skutečné stopy nalezené ve videu)", labels, default=0)
    mode, track_id, lang = opts[idx]
    if mode == "off" and track_id is not None:
        into_args.track_id = track_id
    elif mode == "replace" and track_id is not None:
        into_args.audio_track_id = track_id
    return mode, lang


def sync_two_subs(target_path, ref_path, output, args):
    """Přímá synchronizace dvou titulkových souborů (referencí je druhý .srt),
    bez videa a bez externích nástrojů. Text se nemění, jen časování."""
    target_events = parse_srt(Path(target_path))
    ref_events = parse_srt(Path(ref_path))
    log_info(f"Opravovaných titulků: {len(target_events)}; referenčních: {len(ref_events)}")
    corrected = run_alignment(args, ref_events, ref_events, target_events)
    if getattr(args, "fix_short_duration", False):
        cps, floor, gap, overhead = resolve_speed_params(args)
        corrected, n_ext = fix_short_durations(
            corrected, min_cps=cps, min_duration_floor=floor, min_gap=gap, line_overhead=overhead)
        log_info(f"Prodlouženo {n_ext} titulků se zkráceným zobrazením (využita volná místa)")
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    write_srt(corrected, Path(output))
    log_done(f"Synchronizované titulky uloženy do: {output}")


def run_auto_single(args):
    """Interaktivní průvodce pro JEDEN soubor: prohledá adresář, nabídne
    titulky k opravě i zdroj reference (video se skutečnými stopami, nebo
    druhý titulkový soubor), reálně detekuje jazyky z obsahu a podle toho
    nabídne překlad, zeptá se na metodu, výstup a čitelnost."""
    if args.mkv and args.mkv.is_dir():
        directory = str(args.mkv)
    elif args.mkv and args.mkv.exists():
        directory = os.path.dirname(str(args.mkv)) or "."
    else:
        directory = "."

    print(f"{Fore.MAGENTA}=== Interaktivní synchronizace (jeden soubor) ==={Style.RESET_ALL}")
    log_info(f"Pracovní adresář: {os.path.abspath(directory)}")

    subs = collect_subs(directory)
    videos = collect_videos(directory, recursive=False)
    if not subs:
        die("V adresáři nejsou žádné titulkové soubory (.srt/.ass/.vtt/.orig). "
            "Spusť skript ve složce s titulky nebo zadej cestu k ní jako 1. argument.")

    ti = ask_pick("Který titulkový soubor se má OPRAVIT (má špatné časování)?",
                  [os.path.basename(s) for s in subs])
    target = subs[ti]
    target_lang = detect_srt_file_language(target)

    ref_opts, labels = [], []
    for v in videos:
        ref_opts.append(("video", v))
        labels.append(f"[video]   {os.path.basename(v)}  (vytáhnout referenční stopu z videa)")
    for s in subs:
        if s == target:
            continue
        ref_opts.append(("sub", s))
        labels.append(f"[titulky] {os.path.basename(s)}  (reference přímo z tohoto souboru)")
    if not ref_opts:
        die("Nenašel jsem žádný zdroj reference (video ani druhý titulkový soubor). "
            "Potřebuješ buď video se správně časovanou stopou, nebo druhý .srt jako referenci.")

    ri = ask_pick("Odkud vzít SPRÁVNÉ časování (reference)?", labels, default=0)
    kind, refpath = ref_opts[ri]

    _pick_method(args)

    audio_mode, ref_lang = "off", None
    if kind == "video":
        audio_mode, ref_lang = _offer_video_reference(args, refpath)
    else:
        ref_lang = detect_srt_file_language(refpath)

    _setup_languages_translate(args, target_lang, ref_lang)

    tgt = Path(target)
    default_out = str(tgt.with_name(tgt.stem + ".synced" + tgt.suffix))
    if ask_yes_no(f"Přepsat původní soubor '{tgt.name}' (vytvoří se jednorázově .bak záloha)?", default_no=True):
        output = target
        bak = tgt.with_suffix(tgt.suffix + ".bak")
        if not bak.exists():
            shutil.copy(target, bak)
    else:
        output = ask_text("Výstupní soubor", default_out)

    _ask_readability(args, [target])

    args.audio_mode = audio_mode
    args.ref_lang = ref_lang
    args.output = Path(output)

    print()
    log_info(f"Opravit:   {os.path.basename(target)} (jazyk: {target_lang or '?'})")
    log_info(f"Reference: {os.path.basename(refpath)} ({'video' if kind == 'video' else 'titulky'}"
             + (f", jazyk: {ref_lang}" if ref_lang else "") + ")")
    log_info(f"Metoda:    {args.method}"
             + (f" | audio-mode {audio_mode}" if kind == 'video' else "")
             + (f" | překlad {args.translate}->{args.pivot_lang}" if getattr(args, 'translate', 'off') != 'off' else ""))
    log_info(f"Výstup:    {output}")
    if not ask_yes_no("Spustit synchronizaci?", default_no=False):
        log_warn("Zrušeno uživatelem.")
        return
    preset_flush_if_save()

    if kind == "sub":
        sync_two_subs(target, refpath, args.output, args)
    else:
        args.mkv = Path(refpath)
        args.subtitle_to_fix = Path(target)
        process_single(args)


def run_auto_all(args):
    """Interaktivní průvodce pro DÁVKU (--all): nabídne skutečné stopy ze
    vzorového videa, reálně detekuje jazyky z obsahu titulků a podle toho
    nabídne překlad; pak spustí standardní dávkové zpracování."""
    if args.mkv and args.mkv.is_dir():
        directory = str(args.mkv)
    elif args.mkv and args.mkv.exists():
        directory = os.path.dirname(str(args.mkv)) or "."
    else:
        directory = "."

    print(f"{Fore.MAGENTA}=== Interaktivní dávková synchronizace (--all) ==={Style.RESET_ALL}")
    log_info(f"Pracovní adresář: {os.path.abspath(directory)}")

    args.recursive = ask_yes_no("Prohledat i podadresáře?", default_no=True)
    videos = collect_videos(directory, args.recursive)
    srts = collect_srts(directory, args.recursive)
    log_info(f"Nalezeno {len(videos)} videí a {len(srts)} .srt souborů.")
    if not videos:
        die("Žádná videa k dávkovému zpracování. Pro synchronizaci dvojice titulkových "
            "souborů (.srt + reference) použij --auto.")

    _pick_method(args)

    # nabídka reference ze SKUTEČNÝCH stop vzorového videa
    sample_video = videos[0]
    log_info(f"Čtu stopy ze vzorového videa: {os.path.basename(sample_video)}")
    mkvmerge_bin = _resolve_mkvmerge(args)
    subs_tracks = None
    if mkvmerge_bin:
        subs_tracks, _audio, _err = try_list_tracks(mkvmerge_bin, sample_video)

    am = ask_pick("Zdroj reference pro celou dávku:",
                  ["titulková stopa z videa (doporučeno)",
                   "zvuk - detekce řeči/VAD",
                   "obojí dohromady (max. robustnost)"], default=0)
    args.audio_mode = ["off", "replace", "combine"][am]
    if args.audio_mode in ("off", "combine"):
        langs = sorted({norm_lang(t["lang"]) for t in (subs_tracks or []) if norm_lang(t["lang"])})
        if langs:
            labels = [f"{l}" for l in langs] + ["jiný (napsat ručně)", "automaticky (nevybírat)"]
            i = ask_pick(f"Jazyk referenční titulkové stopy (nalezeno ve vzorku): ", labels, default=0)
            if i < len(langs):
                args.ref_lang = langs[i]
            elif i == len(langs):
                args.ref_lang = norm_lang(ask_language("Jazyk (eng/cze/...)", "") or None)
            else:
                args.ref_lang = None
        else:
            args.ref_lang = norm_lang(ask_language(
                "Jazyk referenční titulkové stopy (eng/cze; prázdné=auto)", args.ref_lang or "") or None)

    args.target_lang = ask_text(
        "Když k jednomu videu sedí víc .srt, preferovaný jazykový tag v názvu (např. cs; prázdné = zeptat se)",
        args.target_lang or "") or None

    # reálná detekce jazyka opravovaných titulků z OBSAHU vzorového .srt
    sample_srts = filter_by_tag(srts, args.target_lang) if (args.target_lang and srts) else srts
    target_lang = detect_srt_file_language(sample_srts[0]) if sample_srts else None
    _setup_languages_translate(args, target_lang, args.ref_lang)

    args.overwrite = ask_yes_no("Přepsat původní .srt přímo (vytvoří se .bak zálohy)?", default_no=True)

    _ask_readability(args, (sample_srts or srts)[:5])

    args.yes = ask_yes_no("Když u videa chybí potřebné stopy, automaticky přeskočit bez ptaní?", default_no=True)

    print()
    log_info(f"Metoda: {args.method} | audio-mode: {args.audio_mode}"
             + (f" | překlad {args.translate}->{args.pivot_lang}" if getattr(args, 'translate', 'off') != 'off' else "")
             + f" | přepis: {'ano' if args.overwrite else 'ne'} | rekurzivně: {'ano' if args.recursive else 'ne'}")
    if not ask_yes_no("Spustit dávkové zpracování?", default_no=False):
        log_warn("Zrušeno uživatelem.")
        return
    preset_flush_if_save()

    args.mkv = Path(directory)
    run_batch(args)


def run_master_wizard(args):
    """Hlavní průvodce při spuštění BEZ parametrů: zeptá se, co chceš udělat,
    spustí příslušný dílčí průvodce a na konci nabídne uložení jako preset."""
    print(f"{Fore.MAGENTA}=== sync_subtitles - interaktivní průvodce ==={Style.RESET_ALL}")
    log_info("Spuštěno bez parametrů. Projdu s tebou, co se má udělat.")
    log_info("Tip: až to vyplníš, můžeš si volby uložit jako preset - příště se to spustí samo.")
    print()

    mode = ask_pick(
        "Co chceš udělat?",
        ["Synchronizovat JEDNY titulky (špatně načasované) podle videa nebo druhých titulků",
         "Synchronizovat CELOU SLOŽKU (dávka videí + jejich titulky)",
         "Přeložit titulky z videa do jiného jazyka a uložit jako .srt",
         "Vytáhnout (extrahovat) titulky z videí do .srt - vyber které stopy",
         "Nahradit STROJOVÝ překlad PROFESIONÁLNÍM (podle obsahu, časování zůstane)",
         "Přečasovat PROFESIONÁLNÍ titulky na MOJE časování (100% profi text)",
         "Vložit (mux) titulky ze složky do videí (podle SxxExx)",
         "Odebrat audio/titulkové stopy z MKV (podle jazyka)",
         "Nastavit VÝCHOZÍ (default) stopu podle jazyka",
         "Přejmenovat titulky podle názvů videí (SxxExx)",
         "Jen opravit ČITELNOST titulků (prodloužit příliš krátké)",
         "Nastavit API klíče a výchozí volby (config.json)",
         "Otestovat AI API (Anthropic/OpenAI) - ověřit klíč a model"],
        default=1,
        help=["Synchronizace jedněch titulků: vybereš titulkový soubor se špatným časováním a "
              "zdroj správného časování (titulková stopa z videa, nebo druhé .srt). Bez videa to "
              "umí i mezi dvěma .srt.",
              "Dávka celé složky: pro každé video v adresáři dopočítá časování k jeho titulkům.",
              "Překlad titulků: vytáhne stopu z videa nebo vezme existující .srt, přeloží do cílového "
              "jazyka (Gemini/Google/DeepL/Claude/Argos) + korektura, uloží <jméno>.<jazyk>.srt.",
              "Extrakce titulků: z každého videa (mkv/mp4/...) vytáhne titulkové stopy do .srt. "
              "Stopy se detekují přímo z videa; vybereš které (podle jazyka, konkrétní stopy, nebo "
              "všechny textové). Obrázkové titulky (PGS/VobSub) nelze do textu.",
              "Nahradit strojový překlad profesionálním: máš vlastní/AI titulky s dobrým časováním a "
              "jinde profesionální překlad TÉŽE show (klidně jiná verze / jiný počet epizod). Podle "
              "OBSAHU spáruje řádky a dosadí profesionální text, ale ZACHOVÁ tvoje časování. Pokryje "
              "jen řádky, kde je jistá shoda; zbytek nechá strojový.",
              "Přečasovat profesionální titulky na moje časování: OPAČNÝ postup - vezme CELÝ "
              "profesionální překlad z jiného adresáře a přečasuje ho na tvoje časování. Výsledek = "
              "100 % profesionálního textu se správným timingem. Nejlepší, když chceš profi titulky "
              "na svou verzi videa.",
              "Vložit titulky do videí: .srt/.ass ze složky namuxuje do videí (párování přes SxxExx) "
              "pomocí MKVToolNix. Nastaví jazyk, název stopy, forced a volitelně výchozí stopu. "
              "Výstup je vždy MKV (i z MP4).",
              "Odebrat stopy z MKV: ukáže jazyky audio/titulkových stop a vybereš, které vyhodit "
              "(rychlý přemux -c copy). Originály se dají zachovat (podsložka 'trimmed').",
              "Výchozí stopa: zruší staré default flagy a nastaví výchozí audio/titulky podle jazyka. "
              "MKV na místě (mkvpropedit), MP4 přemuxem (ffmpeg).",
              "Přejmenovat titulky: .srt přejmenuje podle názvu odpovídajícího videa (párování přes "
              "SxxExx), zachová jazykovou/forced koncovku. Nejdřív ukáže plán.",
              "Čitelnost: jen prodlouží příliš krátce zobrazené titulky do volného místa. Když ve "
              "složce nejsou .srt, nabídne extrakci z videí.",
              "Config: uloží API klíče a výchozí volby do config.json (načítá se automaticky).",
              "Test API: pošle triviální dotaz a vypíše přesnou odpověď/chybu (ladění např. HTTP 400)."])

    if mode == 11:
        run_config(args)
        return
    if mode == 12:
        run_test_api(args)
        return
    if mode == 10:
        args.fix_readability = True
        run_fix_readability(args)
        return
    if mode == 9:
        args.rename_subs = True
        preset_begin_offer("rename-subs", resolve_preset_path(args))
        run_rename_subs(args)
        return
    if mode == 8:
        args.set_default = True
        preset_begin_offer("set-default", resolve_preset_path(args))
        run_set_default(args)
        return
    if mode == 7:
        args.remove_tracks = True
        preset_begin_offer("remove-tracks", resolve_preset_path(args))
        run_remove_tracks(args)
        return
    if mode == 6:
        args.import_subs = True
        preset_begin_offer("import-subs", resolve_preset_path(args))
        run_import_subs(args)
        return
    if mode == 5:
        args.resync_pro = True
        preset_begin_offer("resync-pro", resolve_preset_path(args))
        run_resync_pro(args)
        return
    if mode == 4:
        args.merge_pro = True
        preset_begin_offer("merge-pro", resolve_preset_path(args))
        run_transplant(args)
        return
    if mode == 3:
        args.extract_subs = True
        preset_begin_offer("extract-subs", resolve_preset_path(args))
        run_extract_subs(args)
        return

    cmd = ["auto", "auto-all", "translate-subs"][mode]
    if mode == 0:
        args.auto = True
    elif mode == 1:
        args.auto_all = True
    else:
        args.translate_subs = True

    # nabídnout uložení presetu (otázka padne až na konci, těsně před spuštěním)
    preset_begin_offer(cmd, resolve_preset_path(args))
    if cmd == "auto":
        run_auto_single(args)
    elif cmd == "auto-all":
        run_auto_all(args)
    else:
        run_translate_subs(args)


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Opraví časování titulků podle referenčních titulků a/nebo zvukové stopy z MKV (bez alass; mkvtoolnix pro titulky, ffmpeg volitelně pro zvuk).",
        epilog=r"""
WORKFLOW - jak se skriptem reálně pracovat
==========================================

KROK 0 (jednou):  pip install numpy colorama
                  (mkvtoolnix/ffmpeg si skript v případě potřeby stáhne sám)

NEJJEDNODUŠŠÍ - interaktivní průvodce (skript se na VŠE postupně zeptá):
    python sync_subtitles.py --auto                 # jeden soubor, prohledá tento adresář
    python sync_subtitles.py --auto D:\slozka       # ... v zadaném adresáři
    python sync_subtitles.py --auto-all D:\serial    # celá dávka, ale s otázkami předem
  --auto nabídne titulky k opravě i zdroj reference; jako referenci umí vzít
  i DRUHÝ TITULKOVÝ SOUBOR (.srt/.orig) úplně BEZ videa - ideální, když máš
  jen dvoje titulky (špatně časované + správně časované). U videa nabídne
  SKUTEČNÉ stopy (mkvmerge), sám DETEKUJE jazyky z obsahu a při různých
  jazycích nabídne překlad jen pro párování. Výchozí metoda = combo
  (afinní předsrovnání + warp doladění).

PŘELOŽIT TITULKY DO JINÉHO JAZYKA (vytáhnout z videa a uložit .srt):
    python sync_subtitles.py --translate-subs D:\serial
  Pro všechna videa v adresáři: vytáhne zvolenou titulkovou stopu a uloží
  titulky v cílovém jazyce jako <video>.<lang>.srt. Na vše se zeptá. Kvalitu
  řeší přes: hotové LIDSKÉ titulky z OpenSubtitles (nejlepší; vyžaduje API
  klíč a pro stahování i účet), nebo strojový překlad (DeepL = nejlepší
  kvalita s API klíčem / Google zdarma / Argos offline) + korekturu
  (pravidlové očištění zdarma, volitelně AI korektura přes OpenAI-kompatibilní
  API). U strojového překladu zůstává původní časování, takže výsledek sedí
  na video. Klíče lze dát i přes proměnné DEEPL_API_KEY / OPENSUBTITLES_API_KEY
  / OPENAI_API_KEY.

Nebo ručně, když přesně víš, co chceš:

KROK 1 - podívej se, co je ve videu:
    python sync_subtitles.py --list-tracks video.mkv
  Vypíše titulkové i zvukové stopy s jejich ID a jazyky. Z toho zjistíš,
  jestli má video použitelnou TEXTOVOU titulkovou stopu (SRT/ASS) jako
  referenci a jaký má jazyk.

KROK 2 - srovnej jeden soubor (nejčastější případ):
    python sync_subtitles.py video.mkv titulky_cz.srt vystup_cz.srt
  Ve výchozím stavu (--method auto, --audio-mode off) vezme referenční
  titulkovou stopu z videa a srovná podle ní tvůj český .srt. Když je
  ve videu textová reference, použije se nová obsahová metoda "warp"
  (po větách - umí i rozsync po částech); jinak se vrátí k afinní.

  - Konkrétní/vícejazyčné stopy:   --ref-lang eng    nebo    --track-id 3
  - Žádná použitelná titulková reference (jen obrázkové PGS, nebo žádné)?
        python sync_subtitles.py video.mkv t_cz.srt out.srt --audio-mode replace
    -> srovná podle ZVUKU (detekce řeči, VAD). Tady jede vždy "affine".
  - Maximální robustnost (titulky + zvuk dohromady):  --audio-mode combine

KROK 3 - zkontroluj výsledek v přehrávači. Když to skoro sedí, ale některé
  scény "ujíždějí" jinam než zbytek = rozsync po částech -> vynuť obsahovou
  metodu:
    python sync_subtitles.py video.mkv t_cz.srt out.srt --method warp
  Když je naopak referenční překlad hodně odlišný (málo společných vět),
  je jistější:  --method affine

KROK 4 (volitelně) - čitelnost: prodluž titulky, co moc rychle mizí, ale
  jen do volného místa (nikdy přes překryv):
    ... --fix-short-duration --reading-speed slow

DÁVKOVĚ (celý seriál v jednom adresáři) - videa se spárují s .srt podle názvu:
    python sync_subtitles.py D:\serial --all --target-lang cs --overwrite
  (--overwrite přepíše originály a udělá jednorázově .bak; bez něj vznikne
   '<jméno>.synced.srt'. --yes = neptat se a chybějící stopy přeskočit.)

JEN ČITELNOST, bez synchronizace (titulky už mají správný čas):
    python sync_subtitles.py D:\serial --fix-readability

TIPY K LADĚNÍ "warp":
    --ca-band 60        širší hledání kotev (větší/blokový rozsync)
    --ca-snap-win 2     opatrnější lokální dolaďování
    --ca-min-sim 0.6    přísnější kotvy (jistější, ale méně jich bude)

RŮZNÉ JAZYKY (target vs reference):
    ... --translate google                 # online, přeloží jen pro párování
    ... --translate argos --pivot-lang en   # offline (pip install argostranslate langdetect)
  Bez --translate se odlišné jazyky řeší afinní metodou (časování). Text
  titulků se nikdy nepřekládá, mění se jen časy. Překlady se kešují.
""",
    )
    parser.add_argument("mkv", type=Path, nargs="?",
                         help="Vstupní MKV/MP4 soubor (nebo s --all: adresář k prohledání, default '.')")
    parser.add_argument("subtitle_to_fix", type=Path, nargs="?", help="SRT se špatným časováním, který chceme opravit")
    parser.add_argument("output", type=Path, nargs="?", help="Cesta k výstupnímu opravenému SRT")

    parser.add_argument("--all", action="store_true",
                         help="Dávkový režim: zpracuje všechna videa v adresáři (1. argument, default "
                              "aktuální adresář). Pro každé video se podle názvu souboru dohledá "
                              "odpovídající .srt, ověří se dostupné stopy, a teprve poté se zpracuje.")
    parser.add_argument("-r", "--recursive", action="store_true",
                         help="S --all: prohledat i podadresáře.")
    parser.add_argument("--target-lang", help="S --all: pokud k jednomu videu sedí víc .srt souborů "
                                                "(různé jazyky), použít ten s tímto jazykovým tagem v názvu "
                                                "(např. 'cs' pro 'epizoda.cs.srt').")
    parser.add_argument("--overwrite", action="store_true",
                         help="S --all: přepsat původní .srt přímo (vytvoří jednorázově .bak zálohu). "
                              "Bez tohoto se výstup ukládá jako '<jméno>.synced.srt' vedle originálu.")
    parser.add_argument("--yes", action="store_true",
                         help="S --all: když u videa chybí potřebné stopy, automaticky přeskočit "
                              "bez interaktivního dotazu (pro nehlídané/dávkové spouštění). "
                              "S --fix-readability: nepoužívat výchozí hodnoty parametrů bez ptaní.")

    parser.add_argument("--fix-readability", action="store_true",
                         help="Samostatný režim (BEZ synchronizace): najde v adresáři (1. argument, "
                              "default aktuální adresář; nebo přímo konkrétní .srt) všechny titulky, "
                              "které už mají SPRÁVNÉ časování, a jen prodlouží ty, co zmizí příliš "
                              "rychle na pohodlné přečtení - výhradně do volného místa, nikdy na úkor "
                              "překryvu. Bez --min-cps/--min-duration-floor/--min-gap se na hodnoty "
                              "interaktivně zeptá (s vysvětlením a orientačním odhadem aktuálního tempa).")

    parser.add_argument("--ref-lang", help="Jazyk referenční TITULKOVÉ stopy v MKV, např. eng, cze, ces")
    parser.add_argument("--track-id", type=int, help="ID titulkové stopy v MKV (viz --list-tracks)")

    parser.add_argument(
        "--audio-mode", choices=["off", "replace", "combine"], default="off",
        help="off = jen titulková reference (default); replace = jen analýza zvuku (VAD), "
             "titulková reference se nepoužije; combine = titulková reference + zvuk společně "
             "pro maximální přesnost.",
    )
    parser.add_argument("--audio-lang", help="Jazyk zvukové stopy pro VAD, např. eng, cze, ces")
    parser.add_argument("--audio-track-id", type=int, help="ID zvukové stopy v MKV (viz --list-tracks)")
    parser.add_argument("--vad-percentile", type=float, default=55.0,
                         help="Práh hlasitosti pro detekci řeči, percentil 0-100 (default 55; "
                              "zvyš při hlučném pozadí/hudbě, sniž pro tišší dialogy)")

    parser.add_argument(
        "--method", choices=["auto", "affine", "warp", "combo"], default="auto",
        help="Jak dopočítat časování. 'affine' = globální posun+rychlost (a*t+b, "
             "jazykově nezávislé, i jen ze zvuku). 'warp' = obsahová metoda po VĚTÁCH "
             "(opraví i rozsync po částech; potřebuje textovou referenci). 'combo' = "
             "afinní předsrovnání + warp doladění (nejrobustnější). 'auto' (default) = "
             "combo když je textová reference a dost kotev, jinak affine.")
    parser.add_argument("--ca-band", type=float, default=None,
                         help="(jen --method warp/auto) poloměr hledání kotev v sekundách (default 45)")
    parser.add_argument("--ca-snap-win", type=float, default=None,
                         help="(jen --method warp/auto) okno lokálního došťouchnutí v sekundách "
                              "(default 3; menší = opatrnější)")
    parser.add_argument("--ca-min-sim", type=float, default=None,
                         help="(jen --method warp/auto) min. textová podobnost pro kotvu 0-1 (default 0.50)")
    parser.add_argument("--translate", choices=["off", "google", "deepl", "argos", "claude", "gemini"], default="off",
                         help="Mezijazyčné párování (jen pro metodu warp/auto/combo): když jsou opravované a "
                              "referenční titulky v JINÝCH jazycích, přeloží obě strany do společného "
                              "jazyka (--pivot-lang) JEN pro účely párování - text titulků se nemění. "
                              "'google' zdarma, 'deepl'/'claude' lepší kvalita (API klíč), 'argos' offline. "
                              "Překlady se kešují. Bez tohoto se různé jazyky řeší afinní metodou.")
    parser.add_argument("--pivot-lang", default="en",
                         help="Společný jazyk pro mezijazyčné párování s --translate (default 'en').")

    parser.add_argument("--list-tracks", action="store_true", help="Jen vypsat titulkové i zvukové stopy v MKV a skončit")
    parser.add_argument("--max-shift", type=float, default=120.0, help="Maximální předpokládaný posun v sekundách (default 120)")
    parser.add_argument("--tolerance", type=float, default=1.5, help="Tolerance v sekundách pro párování při zpřesnění (default 1.5)")

    parser.add_argument("--fix-short-duration", action="store_true",
                         help="Po synchronizaci prodloužit titulky, které zmizí příliš rychle vzhledem "
                              "k délce textu - ale jen pokud je k tomu volné místo (mezera do dalšího "
                              "titulku), nikdy na úkor překryvu s dalším titulkem.")
    parser.add_argument("--reading-speed", choices=list(READING_SPEED_PRESETS.keys()),
                         help="Rychlá volba presetu čtecí rychlosti místo ručního --min-cps/--min-duration-floor: "
                              + "; ".join(f"'{k}' = {v[2]} ({v[0]:.0f} znaků/s, podlaha {v[1]:.1f}s)"
                                          for k, v in READING_SPEED_PRESETS.items())
                              + ". Explicitně zadané --min-cps/--min-duration-floor mají před presetem přednost.")
    parser.add_argument("--min-cps", type=float, default=None,
                         help=f"Cílová čtecí rychlost ve znacích/s pro výpočet ideální min. délky "
                              f"zobrazení (default {DEFAULT_MIN_CPS}; nižší = delší zobrazení pro stejný text)")
    parser.add_argument("--min-duration-floor", type=float, default=None,
                         help=f"Absolutní minimální délka zobrazení titulku v sekundách, bez ohledu "
                              f"na délku textu (default {DEFAULT_MIN_DURATION_FLOOR})")
    parser.add_argument("--min-gap", type=float, default=None,
                         help=f"Mezera v sekundách, která musí zůstat zachována před dalším titulkem "
                              f"při prodlužování (default {DEFAULT_MIN_GAP} - cca 2 snímky při 24fps)")
    parser.add_argument("--line-overhead", type=float, default=None,
                         help=f"Extra sekundy navíc za KAŽDÝ řádek titulku nad první - vícero řádků "
                              f"potřebuje navíc čas na přeskočení očí (default {DEFAULT_LINE_OVERHEAD}; "
                              f"díky tomu jednoslovný titulek nikdy nedostane stejnou délku jako "
                              f"víceřádková věta jen kvůli společné podlaze)")
    parser.add_argument("--mkvmerge", help="Cesta k mkvmerge.exe nebo ke složce s ním, pokud není v PATH")
    parser.add_argument("--mkvextract", help="Cesta k mkvextract.exe nebo ke složce s ním, pokud není v PATH")
    parser.add_argument("--no-mkvtoolnix-download", action="store_true",
                         help="Nezkoušet automaticky stáhnout MKVToolNix, pokud nebyl nikde nalezen")
    parser.add_argument("--ffmpeg", help="Cesta k ffmpeg.exe nebo ke složce s ním (jen pro --audio-mode replace/combine)")
    parser.add_argument("--no-ffmpeg-download", action="store_true",
                         help="Nezkoušet automaticky stáhnout ffmpeg, pokud nebyl nikde nalezen")
    parser.add_argument("--auto", action="store_true",
                         help="Interaktivní průvodce pro JEDEN soubor: prohledá adresář, nabídne titulky k opravě i zdroj reference (video NEBO druhý titulkový soubor) a na vše se postupně zeptá.")
    parser.add_argument("--auto-all", action="store_true",
                         help="Interaktivní průvodce pro DÁVKU (jako --all, ale na nastavení se nejdřív zeptá): metoda, zdroj reference, jazyk, přepis, čitelnost - pak zpracuje celý adresář.")
    parser.add_argument("--translate-subs", action="store_true",
                         help="Interaktivní režim: pro všechna videa v adresáři vytáhne zvolenou titulkovou stopu, získá titulky v cílovém jazyce (OpenSubtitles a/nebo strojový překlad + korektura) a uloží je jako <video>.<lang>.srt.")
    parser.add_argument("--out-lang", default=None, help="(--translate-subs) cílový jazyk překladu, např. cs")
    parser.add_argument("--merge-pro", action="store_true",
                        help="Interaktivní režim: nahradí strojový překlad tvých titulků PROFESIONÁLNÍM "
                             "překladem téže show z jiného adresáře (párování podle obsahu), časování "
                             "zůstane. Zeptá se na adresář s 'viki' titulky.")
    parser.add_argument("--resync-pro", action="store_true",
                        help="Interaktivní režim: OPAČNĚ - vezme profesionální titulky z jiného adresáře "
                             "a přečasuje je na tvoje časování (100%% profi text se správným timingem).")
    parser.add_argument("--extract-subs", action="store_true",
                        help="Interaktivní režim: vytáhne titulkové stopy z videí (mkv/mp4/...) do .srt; "
                             "stopy detekuje z videa a vybíráš, které (podle jazyka / konkrétní / všechny).")
    parser.add_argument("--import-subs", action="store_true",
                        help="Interaktivní režim: vloží (mux) titulky ze složky do videí podle SxxExx (MKVToolNix).")
    parser.add_argument("--remove-tracks", action="store_true",
                        help="Interaktivní režim: odebere audio/titulkové stopy z MKV podle jazyka.")
    parser.add_argument("--set-default", action="store_true",
                        help="Interaktivní režim: nastaví výchozí (default) audio/titulkovou stopu podle jazyka.")
    parser.add_argument("--rename-subs", action="store_true",
                        help="Interaktivní režim: přejmenuje .srt podle názvů videí (párování SxxExx).")
    parser.add_argument("--sub-source", choices=["auto", "mt", "opensubtitles"], default="auto",
                         help="(--translate-subs) odkud vzít cílové titulky (default auto).")
    parser.add_argument("--deepl-key", default=None, help="API klíč pro DeepL (nebo proměnná DEEPL_API_KEY).")
    parser.add_argument("--opensubtitles-key", default=None, help="API klíč pro OpenSubtitles (nebo OPENSUBTITLES_API_KEY).")
    parser.add_argument("--llm-key", default=None, help="API klíč pro AI korekturu (nebo OPENAI_API_KEY).")
    parser.add_argument("--config", action="store_true",
                         help="Interaktivní nastavení API klíčů a výchozích voleb do config.json (ptá se jen na to, co chceš zapnout). Config se při startu načítá automaticky.")
    parser.add_argument("--config-file", default=None, help="Cesta k config.json (default vedle skriptu).")
    parser.add_argument("--no-config", action="store_true", help="Nenačítat config.json při startu.")
    parser.add_argument("--anthropic-key", default=None, help="Anthropic (Claude) API klíč (nebo ANTHROPIC_API_KEY).")
    parser.add_argument("--anthropic-model", default=None, help="Model Claude (default claude-sonnet-4-6).")
    parser.add_argument("--gemini-key", default=None, help="Google Gemini API klíč - AI překlad ZDARMA (nebo GEMINI_API_KEY/GOOGLE_API_KEY). Získáš na aistudio.google.com.")
    parser.add_argument("--gemini-model", default=None, help="Model Gemini (default gemini-2.0-flash).")
    parser.add_argument("--llm-api", default=None, help="URL OpenAI-kompatibilního API (/chat/completions).")
    parser.add_argument("--llm-model", default=None, help="Model pro OpenAI-kompatibilní korekturu.")
    parser.add_argument("--opensubtitles-user", default=None, help="OpenSubtitles uživatel (pro stahování).")
    parser.add_argument("--opensubtitles-password", default=None, help="OpenSubtitles heslo (pro stahování).")
    parser.add_argument("--save", action="store_true",
                         help="Jen s interaktivním příkazem (--auto/--auto-all/--translate-subs): "
                              "po vyplnění uloží VŠECHNY volby do preset.json a operaci spustí. "
                              "API klíče se do presetu NEUKLÁDAJÍ (ty patří do --config).")
    parser.add_argument("--load", action="store_true",
                         help="Načte preset.json a okamžitě spustí uloženou operaci bez dotazů "
                              "(příkaz se vezme z presetu, nebo ho upřesni, např. --load --translate-subs).")
    parser.add_argument("--preset-file", default=None, help="Cesta k preset.json (default vedle skriptu).")
    parser.add_argument("--test-api", action="store_true",
                         help="Pošle triviální požadavek na nastavené AI API (Anthropic/OpenAI) a vypíše "
                              "přesnou odpověď nebo chybu (včetně těla od serveru) - pro ladění např. HTTP 400.")
    args = parser.parse_args()

    _cfg = {} if getattr(args, "no_config", False) else load_config(resolve_config_path(args))
    apply_config_to_args(args, _cfg)
    if args.config:
        run_config(args)
        return
    if args.test_api:
        run_test_api(args)
        return

    # Spuštění BEZ jakéhokoli parametru:
    #   - když existuje preset.json -> rovnou spustí uloženou akci (bez dotazů)
    #   - jinak -> hlavní interaktivní průvodce (na konci nabídne uložení presetu)
    if len(sys.argv) <= 1 and not args.load and not args.save:
        _ppath = resolve_preset_path(args)
        _preset = load_preset_file(_ppath)
        if _preset and _preset.get("command"):
            cmd = _preset["command"]
            log_info(f"Našel jsem preset: {_ppath}")
            log_info(f"Spouštím uloženou akci '{cmd}' bez dotazů. (Pro průvodce smaž preset.json.)")
            preset_begin_load(_preset.get("answers", []))
            if cmd == "auto-all":
                run_auto_all(args)
            elif cmd == "auto":
                run_auto_single(args)
            elif cmd == "translate-subs":
                run_translate_subs(args)
            elif cmd == "merge-pro":
                run_transplant(args)
            elif cmd == "resync-pro":
                run_resync_pro(args)
            elif cmd == "extract-subs":
                run_extract_subs(args)
            elif cmd == "import-subs":
                run_import_subs(args)
            elif cmd == "remove-tracks":
                run_remove_tracks(args)
            elif cmd == "set-default":
                run_set_default(args)
            elif cmd == "rename-subs":
                run_rename_subs(args)
            else:
                die(f"Neznámý příkaz v presetu: {cmd}")
            return
        run_master_wizard(args)
        return

    # --- preset (--save / --load) pro interaktivní příkazy ---------------
    interactive_cmd = ("auto-all" if args.auto_all else "auto" if args.auto
                       else "translate-subs" if args.translate_subs
                       else "merge-pro" if args.merge_pro
                       else "resync-pro" if args.resync_pro
                       else "extract-subs" if args.extract_subs
                       else "import-subs" if args.import_subs
                       else "remove-tracks" if args.remove_tracks
                       else "set-default" if args.set_default
                       else "rename-subs" if args.rename_subs else None)
    if args.save and args.load:
        die("--save a --load nelze kombinovat.")
    if args.save and not interactive_cmd:
        die("--save funguje jen s interaktivním příkazem (--auto / --auto-all / --translate-subs / --merge-pro / --resync-pro).")
    if args.load:
        preset = load_preset_file(resolve_preset_path(args))
        if not preset:
            die(f"Preset nenalezen nebo prázdný: {resolve_preset_path(args)} "
                "(nejdřív spusť stejný příkaz s --save).")
        cmd = interactive_cmd or preset.get("command")
        if not cmd:
            die("Preset neobsahuje uložený příkaz - spusť ho např. jako '--load --translate-subs'.")
        if interactive_cmd and preset.get("command") and interactive_cmd != preset.get("command"):
            log_warn(f"Preset je pro '{preset.get('command')}', ale spouštíš '{interactive_cmd}'.")
        preset_begin_load(preset.get("answers", []))
        log_info(f"Načítám preset ({len(preset.get('answers', []))} voleb) a spouštím '{cmd}' bez dotazů.")
        interactive_cmd = cmd
    if args.save and interactive_cmd:
        preset_begin_save(interactive_cmd, resolve_preset_path(args))

    if interactive_cmd and (args.save or args.load):
        if interactive_cmd == "auto-all":
            run_auto_all(args)
        elif interactive_cmd == "auto":
            run_auto_single(args)
        elif interactive_cmd == "translate-subs":
            run_translate_subs(args)
        elif interactive_cmd == "merge-pro":
            run_transplant(args)
        elif interactive_cmd == "resync-pro":
            run_resync_pro(args)
        elif interactive_cmd == "extract-subs":
            run_extract_subs(args)
        elif interactive_cmd == "import-subs":
            run_import_subs(args)
        elif interactive_cmd == "remove-tracks":
            run_remove_tracks(args)
        elif interactive_cmd == "set-default":
            run_set_default(args)
        elif interactive_cmd == "rename-subs":
            run_rename_subs(args)
        preset_flush_if_save()
        return

    if args.auto_all:
        run_auto_all(args)
        return
    if args.auto:
        run_auto_single(args)
        return
    if args.translate_subs:
        run_translate_subs(args)
        return
    if args.merge_pro:
        run_transplant(args)
        return
    if args.resync_pro:
        run_resync_pro(args)
        return
    if args.extract_subs:
        run_extract_subs(args)
        return
    if args.import_subs:
        run_import_subs(args)
        return
    if args.remove_tracks:
        run_remove_tracks(args)
        return
    if args.set_default:
        run_set_default(args)
        return
    if args.rename_subs:
        run_rename_subs(args)
        return

    if args.all and args.fix_readability:
        die("--all a --fix-readability nelze použít současně (jsou to dva oddělené dávkové režimy).")

    if args.fix_readability:
        run_fix_readability(args)
        return

    if args.all:
        run_batch(args)
        return

    if not args.mkv:
        parser.error("the following arguments are required: mkv")

    if not args.mkv.exists():
        die(f"Vstupní soubor neexistuje: {args.mkv}")

    process_single(args)


if __name__ == "__main__":
    main()
