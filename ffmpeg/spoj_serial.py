#!/usr/bin/env python3
"""
spoj_serial.py
--------------
Najde vsechna .mkv soubory v aktualnim adresari, seradi je podle
sezony a epizody (SxxExx) a spoji je do jednoho souboru.

Zachovava:
  - vsechny stopy (video, audio, titulky) spolecne ve vsech epizodach
  - nazvy stop, jazyky, default/forced priznaky
  - KAPITOLY (intro, outro atd.) ze vsech epizod s upravenymi timestampy
  - attachmenty (fonty) z prvni epizody

Postup:
  1. mkvmerge -J  — identifikace stop a trvani
  2. mkvmerge     — normalizace epizod s navic stopami do tmp souboru
  3. mkvextract   — extrakce kapitol z kazde (norm.) epizody
  4. ffmpeg       — finalni bezztrátové spojení (lepe zvlada codec private data)
  5. mkvpropedit  — vlozeni kapitol s upravenymi timestampy do finalniho souboru

Pouziva nastroje z MKVToolNix (mkvmerge, mkvextract, mkvpropedit)
a ffmpeg — vse musi byt ve stejnem adresari jako skript, nebo v PATH.

Pouziti:
    python spoj_serial.py
    python spoj_serial.py --vystup "Muj_Serial.mkv"
    python spoj_serial.py --sezony 1 2
    python spoj_serial.py --epizody 1-5
"""

import json
import re
import sys
import shutil
import tempfile
import argparse
import subprocess
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

# ============================================================
#  Cesta k nastrojum — upravte pokud mate jinou instalaci
# ============================================================
MKVMERGE    = r"C:\Program Files\MKVToolNix\mkvmerge.exe"
MKVEXTRACT  = r"C:\Program Files\MKVToolNix\mkvextract.exe"
MKVPROPEDIT = r"C:\Program Files\MKVToolNix\mkvpropedit.exe"
# ============================================================

TRACK_FLAG = {
    "video":     "-d",
    "audio":     "-a",
    "subtitles": "-s",
    "buttons":   "-b",
}
NO_TRACK_FLAG = {
    "video":     "--no-video",
    "audio":     "--no-audio",
    "subtitles": "--no-subtitles",
    "buttons":   "--no-buttons",
}


# ───────────────────────── pomocne funkce ─────────────────────────

def najdi_a_serad_soubory(adresar: Path, filtry: dict) -> list:
    vzor = re.compile(r"[Ss](\d+)[Ee](\d+)", re.IGNORECASE)
    soubory = []
    for f in sorted(adresar.glob("*.mkv")):
        shoda = vzor.search(f.name)
        if shoda:
            sezona  = int(shoda.group(1))
            epizoda = int(shoda.group(2))
            if filtry.get("sezony") and sezona not in filtry["sezony"]:
                continue
            if filtry.get("ep_od") is not None and epizoda < filtry["ep_od"]:
                continue
            if filtry.get("ep_do") is not None and epizoda > filtry["ep_do"]:
                continue
            soubory.append((sezona, epizoda, f))
        else:
            print(f"  [preskoceno] {f.name}  (nenalezen vzor SxxExx)")
    soubory.sort(key=lambda x: (x[0], x[1]))
    return [f for _, _, f in soubory]


def najdi_nastroj(adresar: Path, exe: str, konstanta: str = "") -> Path | None:
    """Hleda nastroj: 1) pevna cesta, 2) vedle skriptu, 3) PATH."""
    if konstanta and Path(konstanta).exists():
        return Path(konstanta)
    lokalni = adresar / exe
    if lokalni.exists():
        return lokalni
    try:
        subprocess.run([exe, "--version"], capture_output=True, check=True)
        return Path(exe)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(c) for c in cmd],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", errors="replace", **kw
    )


# ───────────────────── identifikace stop a trvani ──────────────────

def get_info(mkvmerge: Path, filepath: Path) -> tuple:
    """Vraci (list_of_tracks, duration_ns)."""
    res = run([mkvmerge, "-J", str(filepath)])
    try:
        data = json.loads(res.stdout)
        tracks   = data.get("tracks", [])
        duration = data.get("container", {}).get("properties", {}).get("duration", 0)
        return tracks, int(duration)
    except (json.JSONDecodeError, ValueError):
        return [], 0


def track_key(track: dict) -> tuple:
    props = track.get("properties", {})
    return (
        track.get("type", ""),
        track.get("codec", ""),
        props.get("language", "und"),
    )


def find_common_tracks(all_tracks: list) -> tuple:
    n = len(all_tracks)
    file_groups = []
    for tracks in all_tracks:
        g = defaultdict(list)
        for t in tracks:
            g[track_key(t)].append(t["id"])
        file_groups.append(g)

    common_order = []
    key_counter: dict = defaultdict(int)
    for t in all_tracks[0]:
        k = track_key(t)
        occ = key_counter[k]
        key_counter[k] += 1
        if all(len(file_groups[fi].get(k, [])) > occ for fi in range(1, n)):
            common_order.append((k, occ))

    file_ids_by_type = []
    for fi, groups in enumerate(file_groups):
        by_type: dict = defaultdict(list)
        for (k, occ) in common_order:
            typ = k[0]
            if len(groups.get(k, [])) > occ:
                by_type[typ].append(groups[k][occ])
        file_ids_by_type.append(dict(by_type))

    return len(common_order), file_ids_by_type


# ─────────────────────────── normalizace ───────────────────────────

def normalizuj_soubor(mkvmerge: Path, filepath: Path,
                      ids_by_type: dict, outpath: Path) -> bool:
    cmd = [mkvmerge, "-o", str(outpath)]
    for typ in TRACK_FLAG:
        ids = ids_by_type.get(typ, [])
        if ids:
            cmd += [TRACK_FLAG[typ], ",".join(str(t) for t in ids)]
        else:
            cmd.append(NO_TRACK_FLAG[typ])
    cmd.append(str(filepath))

    res = run(cmd)
    if res.returncode == 2:
        print(f"  CHYBA normalizace:\n{(res.stdout + res.stderr)[-800:]}")
        return False
    return outpath.exists()


# ──────────────────────────── kapitoly ─────────────────────────────

def parse_ts(ts: str) -> int:
    """HH:MM:SS.NNNNNNNNN  ->  nanoseconds."""
    h, m, rest = ts.split(":")
    if "." in rest:
        s, frac = rest.split(".", 1)
        ns = int(frac.ljust(9, "0")[:9])
    else:
        s, ns = rest, 0
    return (int(h) * 3600 + int(m) * 60 + int(s)) * 1_000_000_000 + ns


def fmt_ts(ns: int) -> str:
    """nanoseconds  ->  HH:MM:SS.NNNNNNNNN"""
    h  = ns  // 3_600_000_000_000
    ns %= 3_600_000_000_000
    m  = ns  // 60_000_000_000
    ns %= 60_000_000_000
    s  = ns  // 1_000_000_000
    ns %= 1_000_000_000
    return f"{h:02d}:{m:02d}:{s:02d}.{ns:09d}"


def extrahuj_kapitoly(mkvextract: Path, filepath: Path, outpath: Path) -> bool:
    """Extrahuje kapitoly do XML souboru. Vraci True kdyz soubor ma kapitoly."""
    res = run([mkvextract, str(filepath), "chapters", str(outpath)])
    return (res.returncode in (0, 1)
            and outpath.exists()
            and outpath.stat().st_size > 50)


def uprav_kapitoly(xml_path: Path, offset_ns: int) -> ET.Element | None:
    """Nacte XML kapitol a prida offset ke vsem casovym znackam."""
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        edition = root.find("EditionEntry")
        if edition is None:
            return None
        for atom in edition.findall(".//ChapterAtom"):
            for tag in ("ChapterTimeStart", "ChapterTimeEnd"):
                el = atom.find(tag)
                if el is not None and el.text:
                    el.text = fmt_ts(parse_ts(el.text.strip()) + offset_ns)
        return edition
    except ET.ParseError:
        return None


def vloz_kapitoly(mkvpropedit: Path, vystup: Path,
                  editions: list, tmpdir: Path) -> bool:
    """Slozi upravene editions do jednoho XML a vlozi do finálniho souboru."""
    if not editions:
        print("  Zadne kapitoly k vlozeni.")
        return True

    root = ET.Element("Chapters")
    edition = ET.SubElement(root, "EditionEntry")
    for ed in editions:
        for atom in ed.findall("ChapterAtom"):
            edition.append(atom)

    chapters_file = tmpdir / "_merged_chapters.xml"
    ET.indent(root, space="  ")
    xml_str = '<?xml version="1.0"?>\n' + ET.tostring(root, encoding="unicode")
    chapters_file.write_text(xml_str, encoding="utf-8")

    res = run([mkvpropedit, str(vystup), "--chapters", str(chapters_file)])
    if res.returncode != 0:
        print(f"  CHYBA mkvpropedit:\n{(res.stdout + res.stderr)[-500:]}")
        return False
    return True


# ──────────────────────── episodic chapters ────────────────────────

def nazev_epizody(filepath: Path) -> str:
    """
    Zkusi vytahnout nazev epizody z nazvu souboru.
    Our.Beloved.Summer.S01E03.10.Things.I.Hate.About.You.mkv
      -> 'S01E03 10 Things I Hate About You'
    """
    stem = filepath.stem
    m = re.search(r"([Ss]\d+[Ee]\d+)(.*)", stem)
    if not m:
        return stem
    kód = m.group(1).upper()
    nazev = m.group(2).lstrip(".-_ ")
    nazev = re.sub(r"[._]", " ", nazev).strip()
    return f"{kód} {nazev}" if nazev else kód


def vygeneruj_ep_kapitoly(soubory: list, norm_durations: list) -> list:
    """
    Vygeneruje seznam EditionEntry s jednou kapitolou na epizodu.
    Vraci list of ET.Element (EditionEntry) — jeden element pro cely serial.
    Chaptery odpovidaji zacatku kazde zdrojove epizody.
    """
    edition = ET.Element("EditionEntry")
    offset_ns = 0
    for i, (f, dur) in enumerate(zip(soubory, norm_durations)):
        atom = ET.SubElement(edition, "ChapterAtom")
        ts = ET.SubElement(atom, "ChapterTimeStart")
        ts.text = fmt_ts(offset_ns)
        disp = ET.SubElement(atom, "ChapterDisplay")
        nazev_el = ET.SubElement(disp, "ChapterString")
        nazev_el.text = nazev_epizody(f)
        lang = ET.SubElement(disp, "ChapterLanguage")
        lang.text = "und"
        offset_ns += dur
    return [edition]


# ──────────────────────── finalni spojeni ──────────────────────────

def spoj_ffmpeg(ffmpeg: Path, soubory: list, vystup: Path, tmpdir: Path) -> bool:
    list_file = tmpdir / "_ffmpeg_list.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for s in soubory:
            cesta = str(s.resolve()).replace("\\", "/").replace("'", "\\'")
            f.write(f"file '{cesta}'\n")

    cmd = [
        ffmpeg, "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy", "-map", "0",
        str(vystup),
    ]
    print("  ffmpeg concat...")
    res = run(cmd)
    if res.returncode != 0:
        chyby = [r for r in res.stderr.splitlines()
                 if any(k in r.lower() for k in ("error", "invalid", "failed"))]
        print("\nCHYBA ffmpeg:")
        for r in (chyby or res.stderr.splitlines())[-15:]:
            print(" ", r)
        return False
    return vystup.exists()


# ─────────────────────────── hlavni tok ────────────────────────────

def spoj_mkvmerge(soubory: list, vystup: Path,
                  mkvmerge: Path, mkvextract: Path,
                  mkvpropedit: Path, ffmpeg: Path,
                  ep_kapitoly: bool = False) -> bool:

    adresar = vystup.parent

    # ── Faze 1: identifikace ──
    print(f"\nFaze 1 — identifikace stop a trvani ({len(soubory)} souboru)...")
    all_tracks, durations = [], []
    for f in soubory:
        tracks, dur = get_info(mkvmerge, f)
        all_tracks.append(tracks)
        durations.append(dur)
        print(f"  {f.name}: {len(tracks)} stop, "
              f"{dur/1_000_000_000:.1f} s")

    common_count, file_ids_by_type = find_common_tracks(all_tracks)
    print(f"\nSpolecnych stop: {common_count}")

    potrebuje_norm = [
        sum(len(v) for v in ids.values()) != len(all_tracks[i])
        for i, ids in enumerate(file_ids_by_type)
    ]
    pocet_norm = sum(potrebuje_norm)
    if pocet_norm:
        print(f"Soubory k normalizaci: {pocet_norm}")
        for i, f in enumerate(soubory):
            if potrebuje_norm[i]:
                extra = len(all_tracks[i]) - sum(len(v) for v in file_ids_by_type[i].values())
                print(f"  {f.name}: -{extra} extra stop")
    else:
        print("Normalizace neni nutna.")

    tmpdir = Path(tempfile.mkdtemp(dir=adresar, prefix="_spoj_tmp_"))
    normalized = list(soubory)
    norm_durations = list(durations)

    try:
        # ── Faze 2: normalizace ──
        if pocet_norm:
            print(f"\nFaze 2 — normalizace ({tmpdir.name})...")
            for i, f in enumerate(soubory):
                if not potrebuje_norm[i]:
                    print(f"  [{i+1}/{len(soubory)}] {f.name}: OK")
                    continue
                temp = tmpdir / f"norm_{i:03d}.mkv"
                print(f"  [{i+1}/{len(soubory)}] {f.name} -> {temp.name} ...")
                ok = normalizuj_soubor(mkvmerge, f, file_ids_by_type[i], temp)
                if not ok:
                    return False
                # Aktualizuj trvani z normalizovaneho souboru
                _, dur = get_info(mkvmerge, temp)
                norm_durations[i] = dur
                normalized[i] = temp
                print(f"    OK ({temp.stat().st_size/(1024**3):.2f} GB)")

        # ── Faze 3: kapitoly ──
        faze = 3 if pocet_norm else 2
        editions = []
        has_chapters = False

        if ep_kapitoly:
            # Rezim --ep-kapitoly: jeden chapter na zacatku kazde epizody
            print(f"\nFaze {faze} — generuji epizodické kapitoly ({len(soubory)} epizod)...")
            for i, f in enumerate(soubory):
                ts = sum(norm_durations[:i])
                print(f"  {soubory[i].name}: {fmt_ts(ts)}")
            editions = vygeneruj_ep_kapitoly(soubory, norm_durations)
            has_chapters = True
        else:
            # Vychozi rezim: zachovat puvodni kapitoly z kazde epizody
            print(f"\nFaze {faze} — extrakce kapitol...")
            offset_ns = 0
            for i, f in enumerate(normalized):
                ch_file = tmpdir / f"chapters_{i:03d}.xml"
                has = extrahuj_kapitoly(mkvextract, f, ch_file)
                if has:
                    has_chapters = True
                    ed = uprav_kapitoly(ch_file, offset_ns)
                    if ed is not None:
                        editions.append(ed)
                        n_kap = len(ed.findall("ChapterAtom"))
                        print(f"  {soubory[i].name}: {n_kap} kapitol "
                              f"(offset {offset_ns/1_000_000_000:.1f} s)")
                    else:
                        print(f"  {soubory[i].name}: kapitoly nelze precist")
                else:
                    print(f"  {soubory[i].name}: bez kapitol")
                offset_ns += norm_durations[i]

        # ── Faze 4: finalni spojeni (ffmpeg) ──
        faze += 1
        print(f"\nFaze {faze} — finalni spojeni -> {vystup.name}")
        ok = spoj_ffmpeg(ffmpeg, normalized, vystup, tmpdir)
        if not ok:
            return False

        # ── Faze 5: vlozeni kapitol ──
        if has_chapters and editions:
            faze += 1
            print(f"\nFaze {faze} — vlozeni kapitol ({sum(len(e.findall('ChapterAtom')) for e in editions)} celkem)...")
            vloz_kapitoly(mkvpropedit, vystup, editions, tmpdir)
        else:
            print("\nZdrojove soubory neobsahuji kapitoly — preskoceno.")

        print(f"\nHotovo!  {vystup.resolve()}")
        print(f"Velikost: {vystup.stat().st_size/(1024**3):.2f} GB")
        return True

    finally:
        if tmpdir.exists():
            print("\nMazam docasne soubory...")
            shutil.rmtree(tmpdir, ignore_errors=True)


# ─────────────────────────────── CLI ───────────────────────────────

def parsuj_rozsah(hodnota: str) -> tuple:
    if "-" in hodnota:
        a, b = hodnota.split("-", 1)
        return int(a), int(b)
    n = int(hodnota)
    return n, n


def main():
    parser = argparse.ArgumentParser(
        description="Spoji MKV soubory serialu vcetne stop a kapitol."
    )
    parser.add_argument("--vystup", "-o",
        help="Nazev vystupniho souboru (default: Serial_komplet.mkv)")
    parser.add_argument("--sezony", "-s", type=int, nargs="+",
        help="Spoj jen tyto sezony, napr. --sezony 1 2")
    parser.add_argument("--epizody", "-e",
        help="Rozsah epizod, napr. --epizody 1-5 nebo --epizody 3")
    parser.add_argument("--ep-kapitoly", dest="ep_kapitoly",
        action="store_true", default=False,
        help="Nahrad kapitoly novymi: jeden chapter = zacatek kazde zdrojove epizody")
    args = parser.parse_args()

    adresar = Path(__file__).parent.resolve()
    print(f"Pracovni adresar: {adresar}\n")

    filtry = {}
    if args.sezony:
        filtry["sezony"] = set(args.sezony)
    if args.epizody:
        ep_od, ep_do = parsuj_rozsah(args.epizody)
        filtry["ep_od"] = ep_od
        filtry["ep_do"] = ep_do

    soubory = najdi_a_serad_soubory(adresar, filtry)
    if not soubory:
        print("Zadne MKV soubory se vzorem SxxExx nebyly nalezeny.")
        sys.exit(1)

    print(f"Nalezeno {len(soubory)} souboru v poradi:")
    for i, f in enumerate(soubory, 1):
        print(f"  {i:3}. {f.name}")

    if args.vystup:
        vystup = adresar / args.vystup
        if not vystup.suffix:
            vystup = vystup.with_suffix(".mkv")
    else:
        vystup = adresar / "Serial_komplet.mkv"

    if vystup in soubory:
        print(f"\nCHYBA: '{vystup.name}' je zaroven vstupnim souborem!")
        sys.exit(1)

    # Najdi nastroje
    mkv_dir = str(Path(MKVMERGE).parent)
    mkvmerge = najdi_nastroj(adresar, "mkvmerge.exe", MKVMERGE)
    mkvextract  = najdi_nastroj(adresar, "mkvextract.exe",  MKVEXTRACT)
    mkvpropedit = najdi_nastroj(adresar, "mkvpropedit.exe", MKVPROPEDIT)
    ffmpeg = (najdi_nastroj(adresar, "ffmpeg.exe") or
              najdi_nastroj(adresar, "ffmpeg"))

    chybi = []
    if not mkvmerge:    chybi.append("mkvmerge")
    if not mkvextract:  chybi.append("mkvextract")
    if not mkvpropedit: chybi.append("mkvpropedit")
    if not ffmpeg:      chybi.append("ffmpeg")

    if chybi:
        print(f"\nCHYBA: Chybi nastroje: {', '.join(chybi)}")
        print(f"  MKVToolNix: https://mkvtoolnix.download/downloads.html#windows")
        print(f"  ffmpeg:     https://ffmpeg.org/download.html")
        sys.exit(1)

    print(f"\nmkvmerge:    {mkvmerge}")
    print(f"mkvextract:  {mkvextract}")
    print(f"mkvpropedit: {mkvpropedit}")
    print(f"ffmpeg:      {ffmpeg}")

    ok = spoj_mkvmerge(soubory, vystup, mkvmerge, mkvextract, mkvpropedit, ffmpeg,
                        ep_kapitoly=args.ep_kapitoly)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
