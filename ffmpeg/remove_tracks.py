#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Odstranění audio a/nebo titulkových stop z MKV souborů.

Proskenuje MKV soubory v adresáři, ukáže jaké stopy obsahují, nabídne
výběr co odstranit (podle jazyka nebo konkrétní ID) a po potvrzení provede
přemux přes mkvmerge (-c copy, takže rychlé – jen kopíruje zbytek).

Výchozí chování: zapisuje do podsložky 'trimmed/' (originály nedotčené).
S --replace přepíše originály (přes dočasný soubor).

Použití:
    python remove_tracks.py                              # interaktivní
    python remove_tracks.py --audio-remove tha,ind       # smaž thai+indo audio
    python remove_tracks.py --sub-remove tha,ind,zh,ko   # smaž některé titulky
    python remove_tracks.py --sub-keep en,cs             # zachovej jen en+cs
    python remove_tracks.py --audio-keep ko --sub-remove all --yes --replace
    python remove_tracks.py "D:\\serial" -r              # i podadresáře
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

VIDEO_EXTS = {".mkv"}
ALL_VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".mov"}

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
#  MKVToolNix – hledání
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
#  Probe / plán
# =========================================================================== #
def collect(directory, recursive):
    """Vrátí (mkv_list, other_list) – other = mp4/m4v/mov v adresáři."""
    mkvs, others = [], []
    walker = os.walk(directory) if recursive else \
        [(directory, [], os.listdir(directory))]
    for root, _d, files in walker:
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            full = os.path.join(root, f)
            if ext in VIDEO_EXTS:
                mkvs.append(full)
            elif ext in ALL_VIDEO_EXTS:
                others.append(full)
    return sorted(mkvs), sorted(others)


def probe_mkv(video):
    """Vrátí {'audio':[...], 'subs':[...]} - každá stopa má 'id' (mkvmerge ID)."""
    out = run_capture([MKVMERGE, "-J", video]).stdout
    data = json.loads(out or "{}")
    audio, subs = [], []
    for t in data.get("tracks", []):
        pr = t.get("properties", {}) or {}
        rec = {
            "id": t["id"],
            "lang": canon(pr.get("language")),
            "name": pr.get("track_name") or "",
            "codec": t.get("codec") or "",
            "default": bool(pr.get("default_track")),
            "forced": bool(pr.get("forced_track")),
        }
        if t.get("type") == "audio":
            audio.append(rec)
        elif t.get("type") == "subtitles":
            subs.append(rec)
    return {"audio": audio, "subs": subs}


def parse_lang_list(s):
    """'en,cs,tha' nebo 'all' nebo 'none' -> set kanonických kódů / 'all' / None."""
    if not s:
        return None
    s = s.strip().lower()
    if s in ("all", "*"):
        return "all"
    if s in ("none", ""):
        return set()
    return {canon(x) for x in s.split(",") if x.strip()}


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


def ask_remove(kind_label, langs):
    """Vrátí set jazyků k odstranění (může být prázdný = nic)."""
    print(f"\n{C.BOLD}{kind_label}:{C.RESET}")
    for i, (l, n) in enumerate(langs, 1):
        print(f"  {C.CYAN}[{i}]{C.RESET} {lang_label(l)}  ({n}×)")
    print(f"  {C.CYAN}[0]{C.RESET} nic neodstraňovat")
    print(f"  {C.DIM}(zadej čísla nebo kódy oddělené čárkou: '2,3' nebo 'tha,ind'){C.RESET}")
    while True:
        try:
            ans = input(f"Vyber jazyky k ODSTRANĚNÍ ({kind_label.lower()}): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return set()
        if ans in ("", "0"):
            return set()
        out = set()
        ok = True
        for part in re.split(r"[,\s]+", ans):
            if not part:
                continue
            if part.isdigit():
                idx = int(part)
                if 1 <= idx <= len(langs):
                    out.add(langs[idx - 1][0])
                else:
                    ok = False
                    break
            else:
                cc = canon(part)
                if cc in [l for l, _ in langs]:
                    out.add(cc)
                else:
                    ok = False
                    break
        if ok:
            return out
        print(f"  {C.YELLOW}Neplatná volba, zkus to znovu.{C.RESET}")


# =========================================================================== #
#  Sestavení mkvmerge příkazu
# =========================================================================== #
def decide_tracks(info, kind, langs_remove, langs_keep, ids_remove, ids_keep):
    """Pro daný typ stop vrátí (keep_ids, remove_ids).

    Priority:
      1) --ids-keep / --ids-remove (přímé ID).
      2) --keep (whitelist) má přednost nad --remove (blacklist).
      3) Když nic z toho, ničí se podle --remove jazyků.
    """
    all_ids = [t["id"] for t in info[kind]]
    if not all_ids:
        return [], []

    keep_set = None
    if ids_keep is not None:
        keep_set = {tid for tid in all_ids if tid in ids_keep}
    elif langs_keep is not None and langs_keep != "all":
        keep_set = {t["id"] for t in info[kind] if t["lang"] in langs_keep}
    elif langs_keep == "all":
        keep_set = set(all_ids)

    if keep_set is not None:
        keep = [tid for tid in all_ids if tid in keep_set]
        remove = [tid for tid in all_ids if tid not in keep_set]
        return keep, remove

    # blacklist (jazyky / IDs)
    remove_set = set()
    if ids_remove:
        remove_set |= set(ids_remove) & set(all_ids)
    if langs_remove == "all":
        remove_set |= set(all_ids)
    elif langs_remove:
        remove_set |= {t["id"] for t in info[kind] if t["lang"] in langs_remove}

    keep = [tid for tid in all_ids if tid not in remove_set]
    remove = [tid for tid in all_ids if tid in remove_set]
    return keep, remove


def build_mkvmerge_cmd(video, out_path, audio_keep, audio_remove,
                       sub_keep, sub_remove, all_audio, all_subs):
    """Sestaví mkvmerge příkaz s pozitivním výběrem stop."""
    cmd = [MKVMERGE, "-o", out_path]
    # audio
    if not audio_keep and all_audio:
        cmd += ["--no-audio"]
    elif audio_keep and len(audio_keep) < len(all_audio):
        cmd += ["--audio-tracks", ",".join(str(i) for i in audio_keep)]
    # subs
    if not sub_keep and all_subs:
        cmd += ["--no-subtitles"]
    elif sub_keep and len(sub_keep) < len(all_subs):
        cmd += ["--subtitle-tracks", ",".join(str(i) for i in sub_keep)]
    cmd += [video]
    return cmd


def describe_kind(label, info, kind, keep, remove):
    tracks = info[kind]
    if not tracks:
        return f"{C.DIM}{label}: —{C.RESET}"
    if not remove:
        return f"{C.DIM}{label}: beze změny ({len(tracks)}×){C.RESET}"
    by_id = {t["id"]: t for t in tracks}
    rm_desc = ", ".join(
        f"id{by_id[i]['id']} ({by_id[i]['lang']})" for i in remove)
    keep_desc = ", ".join(
        f"id{by_id[i]['id']} ({by_id[i]['lang']})" for i in keep) or "—"
    return (f"{label}: {C.RED}odebrat{C.RESET} {rm_desc}; "
            f"{C.GREEN}ponechat{C.RESET} {keep_desc}")


# =========================================================================== #
#  Main
# =========================================================================== #
def main():
    p = argparse.ArgumentParser(
        description="Odstraní zvolené audio/titulkové stopy z MKV (mkvmerge).")
    p.add_argument("directory", nargs="?", default=".",
                   help="Adresář (výchozí: aktuální).")
    p.add_argument("-r", "--recursive", action="store_true",
                   help="Projít i podadresáře.")
    p.add_argument("--audio-remove", metavar="JAZ",
                   help="Smaž audio stopy s těmito jazyky (en,cs,tha) nebo 'all'.")
    p.add_argument("--audio-keep", metavar="JAZ",
                   help="Ponechej jen tyto audio jazyky (whitelist).")
    p.add_argument("--sub-remove", metavar="JAZ",
                   help="Smaž titulky s těmito jazyky nebo 'all'.")
    p.add_argument("--sub-keep", metavar="JAZ",
                   help="Ponechej jen tyto titulkové jazyky.")
    p.add_argument("--audio-ids", metavar="ID",
                   help="Smaž audio stopy podle mkvmerge ID (např. '1,2').")
    p.add_argument("--sub-ids", metavar="ID",
                   help="Smaž titulky podle mkvmerge ID.")
    p.add_argument("--replace", action="store_true",
                   help="Přepsat originál (jinak zapisuje do podsložky 'trimmed').")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Neptat se na potvrzení.")
    p.add_argument("--mkvmerge", metavar="CESTA",
                   help="Cesta k mkvmerge nebo složce MKVToolNix.")
    p.add_argument("--no-color", action="store_true", help="Vypnout barvy.")
    args = p.parse_args()

    global C, MKVMERGE
    C = setup_console(args.no_color)

    if not os.path.isdir(args.directory):
        raise SystemExit(f"{C.RED}Není adresář: {args.directory}{C.RESET}")

    extra = [args.directory, os.getcwd(),
             os.path.dirname(os.path.abspath(__file__))]
    if args.mkvmerge:
        d = args.mkvmerge
        MKVMERGE = find_mkv_tool("mkvmerge", [d]) if os.path.isdir(d) else d
    else:
        MKVMERGE = find_mkv_tool("mkvmerge", extra)
    if not _try_tool(MKVMERGE):
        raise SystemExit(
            f"{C.RED}Nenašel jsem mkvmerge (MKVToolNix).{C.RESET}\n"
            "Nainstaluj z https://mkvtoolnix.download/ a přidej do PATH, "
            "nebo zadej --mkvmerge.")
    print(f"{C.CYAN}mkvmerge:{C.RESET} {MKVMERGE}\n")

    videos, others = collect(args.directory, args.recursive)
    if not videos and not others:
        print("Žádná videa nenalezena.")
        return
    if not videos:
        print(f"{C.YELLOW}Nenalezeno žádné MKV.{C.RESET}")
        print(f"{C.DIM}Tento skript pracuje jen s MKV (přes mkvmerge). "
              f"V adresáři je {len(others)} MP4/M4V/MOV – ty zde nelze upravit.\n"
              f"Pokud z nich potřebuješ zbavit stop, převeď je nejdřív na MKV "
              f"přes import_subs.py nebo přímo mkvmerge.{C.RESET}")
        return
    if others:
        print(f"{C.DIM}Pozn.: v adresáři je {len(others)} ne-MKV videí "
              f"(MP4/M4V/MOV) – přeskakuji, mkvmerge je sice přečte, ale "
              f"tento skript je záměrně omezený jen na MKV.{C.RESET}\n")

    # probe
    infos = {}
    for v in videos:
        try:
            infos[v] = probe_mkv(v)
        except Exception as e:
            print(f"{C.YELLOW}!  nelze přečíst {os.path.basename(v)}: {e}{C.RESET}")
    if not infos:
        return

    audio_langs = aggregate_langs(infos, "audio")
    sub_langs = aggregate_langs(infos, "subs")

    print(f"{C.BOLD}MKV souborů: {len(infos)}{C.RESET}")
    print(f"  audio jazyky:   " +
          (", ".join(f"{l}({n})" for l, n in audio_langs) or "—"))
    print(f"  titulkové jaz.: " +
          (", ".join(f"{l}({n})" for l, n in sub_langs) or "—"))

    # parsování voleb
    a_remove = parse_lang_list(args.audio_remove)
    a_keep = parse_lang_list(args.audio_keep)
    s_remove = parse_lang_list(args.sub_remove)
    s_keep = parse_lang_list(args.sub_keep)
    a_ids_rm = {int(x) for x in re.split(r"[,\s]+", args.audio_ids or "") if x}
    s_ids_rm = {int(x) for x in re.split(r"[,\s]+", args.sub_ids or "") if x}

    if a_keep and a_remove:
        raise SystemExit(f"{C.RED}Nelze kombinovat --audio-keep a --audio-remove.{C.RESET}")
    if s_keep and s_remove:
        raise SystemExit(f"{C.RED}Nelze kombinovat --sub-keep a --sub-remove.{C.RESET}")

    interactive = (a_remove is None and a_keep is None and not a_ids_rm
                   and s_remove is None and s_keep is None and not s_ids_rm)
    if interactive and sys.stdin.isatty():
        if audio_langs:
            a_remove = ask_remove("Audio", audio_langs)
        if sub_langs:
            s_remove = ask_remove("Titulky", sub_langs)

    # plán
    plans = []
    n_changes = 0
    print()
    for v, info in infos.items():
        a_keep_ids, a_rm_ids = decide_tracks(
            info, "audio", a_remove, a_keep, a_ids_rm, None)
        s_keep_ids, s_rm_ids = decide_tracks(
            info, "subs", s_remove, s_keep, s_ids_rm, None)
        will = bool(a_rm_ids or s_rm_ids)

        # bezpečnost: neodstranit poslední audio stopu (přehrávač by ztichl)
        if not a_keep_ids and info["audio"] and a_rm_ids:
            print(f"{C.YELLOW}!  {os.path.basename(v)}: výběr by odstranil "
                  f"VŠECHNA audio – přeskakuji.{C.RESET}")
            will = False

        if will:
            n_changes += 1
        plans.append((v, info, a_keep_ids, a_rm_ids, s_keep_ids, s_rm_ids, will))
        head = f"{C.BOLD}{C.CYAN}# {os.path.basename(v)}{C.RESET}"
        if not will:
            head += f"  {C.DIM}(beze změny){C.RESET}"
        print(head)
        print(f"    {describe_kind('audio', info, 'audio', a_keep_ids, a_rm_ids)}")
        print(f"    {describe_kind('titulky', info, 'subs', s_keep_ids, s_rm_ids)}")

    if n_changes == 0:
        print(f"\n{C.GREEN}Nic k odstranění.{C.RESET}")
        return

    # potvrzení
    print(f"\n{C.YELLOW}Bude přemuxováno {n_changes} souborů "
          f"(mkvmerge -c copy – rychlé, ale píše celý soubor).{C.RESET}")
    if args.replace:
        print(f"{C.YELLOW}--replace: originály budou přepsány.{C.RESET}")
    else:
        print(f"{C.DIM}Výstup do podsložky 'trimmed/'. Originály zůstanou.{C.RESET}")
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
    for v, info, a_keep_ids, a_rm_ids, s_keep_ids, s_rm_ids, will in plans:
        if not will:
            continue
        vdir = os.path.dirname(v)
        vbase = os.path.basename(v)
        if args.replace:
            out_path = os.path.join(vdir, vbase + ".trimtmp.mkv")
            final = v
        else:
            out_dir = os.path.join(vdir, "trimmed")
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, vbase)
            final = out_path

        cmd = build_mkvmerge_cmd(
            v, out_path, a_keep_ids, a_rm_ids, s_keep_ids, s_rm_ids,
            [t["id"] for t in info["audio"]],
            [t["id"] for t in info["subs"]])
        print(f"{C.CYAN}>>{C.RESET} {vbase}")
        res = run_capture(cmd)
        if res.returncode >= 2 or not os.path.exists(out_path):
            print(f"   {C.RED}CHYBA mkvmerge:{C.RESET}")
            for l in [l for l in (res.stdout or "").splitlines() if l.strip()][-6:]:
                print(f"   {C.DIM}| {l}{C.RESET}")
            if os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass
            continue
        if args.replace:
            try:
                os.replace(out_path, final)
            except OSError as e:
                print(f"   {C.RED}CHYBA při náhradě: {e}{C.RESET}")
                continue
        ok += 1
        warn = " (s varováním)" if res.returncode == 1 else ""
        print(f"   {C.GREEN}OK{C.RESET}{warn}")
    col = C.GREEN if ok == n_changes else C.YELLOW
    print(f"\n{C.BOLD}Hotovo:{C.RESET} {col}{ok}/{n_changes}{C.RESET} souborů.")


if __name__ == "__main__":
    main()
