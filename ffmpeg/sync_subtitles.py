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

Tento postup je jazykově nezávislý (nepoužívá text, jen časování), takže
funguje i mezi různými jazyky a u jinak rozdělených řádků.

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


def parse_srt(path: Path):
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
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
        die(f"Z souboru {path} se nepodařilo načíst žádné titulky (chybný formát?).")
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


def fix_short_durations(events, min_cps=17.0, min_duration_floor=1.0, min_gap=0.084):
    """
    Prodlouží titulky, které zmizí příliš rychle vzhledem k délce textu,
    a to POUZE pokud je k tomu volné místo (mezera do dalšího titulku) -
    nikdy nepřesáhne mezeru (minus bezpečnostní min_gap před dalším titulkem)
    a nikdy neprodlouží víc, než kolik si text reálně "žádá" (žádné jedno
    slovo nezůstane viset na obrazovce přes celou tichou scénu).

    min_cps           - cílová čtecí rychlost ve znacích/s (default 17;
                         menší hodnota = delší ideální zobrazení)
    min_duration_floor - absolutní podlaha v sekundách bez ohledu na text
    min_gap           - mezera, která musí zůstat zachována před dalším titulkem
    """
    out = [dict(ev) for ev in events]
    n = len(out)
    extended = 0
    for i in range(n):
        char_count = len(re.sub(r"\s+", "", out[i]["text"]))
        if char_count == 0:
            continue
        ideal_duration = max(min_duration_floor, char_count / min_cps)
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


def ask_choice(prompt, options, allow_skip=True, allow_abort=True):
    """Jednoduchý textový interaktivní výběr v CLI. Vrací index (int),
    'skip', nebo 'abort'."""
    while True:
        print(f"{Fore.YELLOW}{prompt}{Style.RESET_ALL}")
        for i, opt in enumerate(options, 1):
            print(f"  {i}) {opt}")
        if allow_skip:
            print("  s) přeskočit tento soubor")
        if allow_abort:
            print("  a) zrušit celý dávkový běh")
        choice = input("Tvoje volba: ").strip().lower()
        if allow_skip and choice == "s":
            return "skip"
        if allow_abort and choice == "a":
            return "abort"
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return int(choice) - 1
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
    if args.ref_lang:
        out += ["--ref-lang", args.ref_lang]
    if args.audio_lang:
        out += ["--audio-lang", args.audio_lang]
    out += ["--vad-percentile", str(args.vad_percentile)]
    out += ["--max-shift", str(args.max_shift)]
    out += ["--tolerance", str(args.tolerance)]
    if args.fix_short_duration:
        out += ["--fix-short-duration"]
    out += ["--min-cps", str(args.min_cps)]
    out += ["--min-duration-floor", str(args.min_duration_floor)]
    out += ["--min-gap", str(args.min_gap)]
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
# main
# ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Opraví časování titulků podle referenčních titulků a/nebo zvukové stopy z MKV (bez alass; mkvtoolnix pro titulky, ffmpeg volitelně pro zvuk)."
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
                              "bez interaktivního dotazu (pro nehlídané/dávkové spouštění).")

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

    parser.add_argument("--list-tracks", action="store_true", help="Jen vypsat titulkové i zvukové stopy v MKV a skončit")
    parser.add_argument("--max-shift", type=float, default=120.0, help="Maximální předpokládaný posun v sekundách (default 120)")
    parser.add_argument("--tolerance", type=float, default=1.5, help="Tolerance v sekundách pro párování při zpřesnění (default 1.5)")

    parser.add_argument("--fix-short-duration", action="store_true",
                         help="Po synchronizaci prodloužit titulky, které zmizí příliš rychle vzhledem "
                              "k délce textu - ale jen pokud je k tomu volné místo (mezera do dalšího "
                              "titulku), nikdy na úkor překryvu s dalším titulkem.")
    parser.add_argument("--min-cps", type=float, default=17.0,
                         help="Cílová čtecí rychlost ve znacích/s pro výpočet ideální min. délky "
                              "zobrazení (default 17; nižší = delší zobrazení pro stejný text)")
    parser.add_argument("--min-duration-floor", type=float, default=1.0,
                         help="Absolutní minimální délka zobrazení titulku v sekundách, bez ohledu "
                              "na délku textu (default 1.0)")
    parser.add_argument("--min-gap", type=float, default=0.084,
                         help="Mezera v sekundách, která musí zůstat zachována před dalším titulkem "
                              "při prodlužování (default 0.084 - cca 2 snímky při 24fps)")
    parser.add_argument("--mkvmerge", help="Cesta k mkvmerge.exe nebo ke složce s ním, pokud není v PATH")
    parser.add_argument("--mkvextract", help="Cesta k mkvextract.exe nebo ke složce s ním, pokud není v PATH")
    parser.add_argument("--no-mkvtoolnix-download", action="store_true",
                         help="Nezkoušet automaticky stáhnout MKVToolNix, pokud nebyl nikde nalezen")
    parser.add_argument("--ffmpeg", help="Cesta k ffmpeg.exe nebo ke složce s ním (jen pro --audio-mode replace/combine)")
    parser.add_argument("--no-ffmpeg-download", action="store_true",
                         help="Nezkoušet automaticky stáhnout ffmpeg, pokud nebyl nikde nalezen")
    args = parser.parse_args()

    if args.all:
        run_batch(args)
        return

    if not args.mkv:
        parser.error("the following arguments are required: mkv")

    if not args.mkv.exists():
        die(f"Vstupní soubor neexistuje: {args.mkv}")

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

        log_info("Hledám hrubý časový posun (FFT křížová korelace)...")
        shift = coarse_offset(ref_events, target_events, max_shift=args.max_shift)
        log_info(f"Hrubý odhad posunu: {shift:+.3f} s")

        log_info("Zpřesňuji (lineární regrese + robustní filtrování)...")
        scale, offset, n_matched = refine_affine(ref_events, target_events, shift, tolerance=args.tolerance)
        log_info(f"Výsledná transformace: nový_čas = {scale:.6f} * starý_čas + {offset:+.3f}")
        log_info(f"Spárováno {n_matched} z {len(ref_events)} referenčních kotev pro zpřesnění")

        if abs(scale - 1.0) > 0.05:
            log_warn("Velký rozdíl v rychlosti (>5%) - možná jiný framerate zdrojů, zkontroluj výsledek.")

        corrected = apply_transform(target_events, scale, offset)

        if args.fix_short_duration:
            corrected, n_extended = fix_short_durations(
                corrected, min_cps=args.min_cps,
                min_duration_floor=args.min_duration_floor, min_gap=args.min_gap,
            )
            log_info(f"Prodlouženo {n_extended} titulků se zkráceným zobrazením (využita volná místa)")

        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_srt(corrected, args.output)

    log_done(f"Synchronizované titulky uloženy do: {args.output}")


if __name__ == "__main__":
    main()
