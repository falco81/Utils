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
3) MKVToolNix pro Windows:
   - stáhni instalátor z https://mkvtoolnix.download/downloads.html#windows
   - nainstaluj (instalátor sám nabídne přidání do PATH, nebo zaškrtni tu možnost)
   - ověř v cmd: mkvmerge --version
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
    log_info(f"ffmpeg nenalezen; stahuji z {url}")
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
                print(f"\r  {Fore.CYAN}{done * 100 // total:3d}%{Style.RESET_ALL}  "
                      f"{done // 1048576} / {total // 1048576} MB", end="")
        print()
    log_info("Rozbaluji ffmpeg ...")
    _extract_archive(tmp, cache, url)
    try:
        os.remove(tmp)
    except OSError:
        pass


def ensure_ffmpeg(target_dir, allow_download):
    """Najde ffmpeg: PATH -> --ffmpeg/FFMPEG override -> cache .ffmpeg vedle
    skriptu/u videa/v cwd -> (pokud povoleno) stáhne a rozbalí z gyan.dev."""
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
            log_warn(f"Stažení ffmpeg selhalo: {e}")
        ff = _find_cached("ffmpeg", search)
        if not _try_ff(ff):
            ff = None
    return ff


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
# main
# ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Opraví časování titulků podle referenčních titulků a/nebo zvukové stopy z MKV (bez alass; mkvtoolnix pro titulky, ffmpeg volitelně pro zvuk)."
    )
    parser.add_argument("mkv", type=Path, help="Vstupní MKV/MP4 soubor obsahující referenční titulky a/nebo zvuk")
    parser.add_argument("subtitle_to_fix", type=Path, nargs="?", help="SRT se špatným časováním, který chceme opravit")
    parser.add_argument("output", type=Path, nargs="?", help="Cesta k výstupnímu opravenému SRT")

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
    parser.add_argument("--mkvmerge", help="Cesta k mkvmerge.exe, pokud není v PATH")
    parser.add_argument("--mkvextract", help="Cesta k mkvextract.exe, pokud není v PATH")
    parser.add_argument("--ffmpeg", help="Cesta k ffmpeg.exe nebo ke složce s ním (jen pro --audio-mode replace/combine)")
    parser.add_argument("--no-ffmpeg-download", action="store_true",
                         help="Nezkoušet automaticky stáhnout ffmpeg, pokud nebyl nikde nalezen")
    args = parser.parse_args()

    if not args.mkv.exists():
        die(f"Vstupní soubor neexistuje: {args.mkv}")

    is_mkv_container = args.mkv.suffix.lower() in MKVEXTRACT_CONTAINER_EXTS
    need_sub_extraction = args.audio_mode in ("off", "combine")
    need_audio = args.audio_mode in ("replace", "combine")
    need_ffmpeg = need_audio or (need_sub_extraction and not is_mkv_container)

    mkvmerge_bin = args.mkvmerge or find_tool(["mkvmerge", "mkvmerge.exe"])
    if not mkvmerge_bin:
        die(
            "mkvmerge nenalezen v PATH. Stáhni a nainstaluj MKVToolNix z "
            "https://mkvtoolnix.download/downloads.html#windows (instalátor nabízí "
            "přidání do PATH), nebo použij --mkvmerge s plnou cestou k mkvmerge.exe "
            "(obvykle C:\\Program Files\\MKVToolNix\\mkvmerge.exe). Používá se i pro "
            "MP4 jen na výpis/identifikaci stop, samotnou extrakci z MP4 dělá ffmpeg."
        )
    mkvextract_bin = args.mkvextract or find_tool(["mkvextract", "mkvextract.exe"])
    if need_sub_extraction and is_mkv_container and not mkvextract_bin:
        die(
            "mkvextract nenalezen v PATH (potřebný pro extrakci titulků z .mkv/.webm). "
            "Nainstaluj MKVToolNix nebo použij --mkvextract s plnou cestou k .exe."
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
