import os
import re
import json
import subprocess

directory = r"."

# Možné umístění mkvpropedit na Windows
MKVPROPEDIT_CANDIDATES = [
    "mkvpropedit",
    r"C:\Program Files\MKVToolNix\mkvpropedit.exe",
    r"C:\Program Files (x86)\MKVToolNix\mkvpropedit.exe",
]

def find_mkvpropedit():
    for candidate in MKVPROPEDIT_CANDIDATES:
        try:
            result = subprocess.run(
                [candidate, "--version"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                return candidate
        except FileNotFoundError:
            continue
    return None

MKVPROPEDIT = find_mkvpropedit()

AUDIO_EXTENSIONS = {".aac", ".ac3", ".eac3", ".dts", ".thd", ".mp3", ".flac", ".mka"}
VIDEO_EXTENSIONS = {".mkv", ".mp4"}

AUDIO_META = {
    ".aac": "English", ".ac3": "English", ".eac3": "English",
    ".dts": "English", ".thd": "English", ".mp3":  "English",
    ".flac": "English", ".mka": "English",
}

LANG_NAMES = {
    "eng": "English", "en":  "English",
    "kor": "Korean",  "ko":  "Korean",
    "ces": "Czech",   "cze": "Czech",   "cs": "Czech",
    "tha": "Thai",    "th":  "Thai",
    "ara": "Arabic",  "ar":  "Arabic",
    "dan": "Danish",  "da":  "Danish",
    "fil": "Filipino",
    "ind": "Indonesian", "id": "Indonesian",
    "jpn": "Japanese",   "ja": "Japanese",
    "msa": "Malay",  "may": "Malay", "ms": "Malay",
    "por": "Portuguese", "pt": "Portuguese",
    "swe": "Swedish", "sv": "Swedish",
    "zho": "Chinese", "chi": "Chinese", "zh": "Chinese",
    "vie": "Vietnamese", "vi": "Vietnamese",
    "dut": "Dutch",   "nld": "Dutch",   "nl": "Dutch",
    "fra": "French",  "fre": "French",  "fr": "French",
    "deu": "German",  "ger": "German",  "de": "German",
    "spa": "Spanish", "es": "Spanish",
    "ita": "Italian", "it": "Italian",
    "rus": "Russian", "ru": "Russian",
    "pol": "Polish",  "pl": "Polish",
    "hun": "Hungarian", "hu": "Hungarian",
    "ron": "Romanian", "rum": "Romanian", "ro": "Romanian",
    "hrv": "Croatian", "hr": "Croatian",
    "srp": "Serbian",  "sr": "Serbian",
    "slk": "Slovak",   "slo": "Slovak", "sk": "Slovak",
    "bul": "Bulgarian", "bg": "Bulgarian",
    "tur": "Turkish",  "tr": "Turkish",
    "heb": "Hebrew",   "he": "Hebrew",
    "und": None,
}

MKV_SUBTITLE_CODECS = {"ass", "ssa", "subrip", "srt", "webvtt", "hdmv_pgs_subtitle", "dvd_subtitle"}
CONVERT_TO_SRT = {"mov_text", "tx3g"}

def extract_episode_key(filename):
    match = re.search(r"(S\d+E\d+)", filename, re.IGNORECASE)
    return match.group(1).upper() if match else None

def get_streams(path):
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return json.loads(result.stdout).get("streams", [])

def build_audio_map(directory):
    audio_map = {}
    for filename in os.listdir(directory):
        ext = os.path.splitext(filename)[1].lower()
        if ext in AUDIO_EXTENSIONS:
            key = extract_episode_key(filename)
            if key and key not in audio_map:
                audio_map[key] = (os.path.join(directory, filename), ext)
    return audio_map

def build_video_map(directory):
    video_map = {}
    for filename in sorted(os.listdir(directory)):
        ext = os.path.splitext(filename)[1].lower()
        if ext in VIDEO_EXTENSIONS:
            key = extract_episode_key(filename)
            if key and key not in video_map:
                video_map[key] = (os.path.join(directory, filename), ext)
    return video_map

def build_ffmpeg_cmd(video_path, audio_path, audio_ext, out_path, streams):
    cmd = ["ffmpeg", "-y", "-i", video_path, "-i", audio_path]
    map_args = ["-map", "1:a"]

    video_streams = [s for s in streams if s.get("codec_type") == "video"
                     and s.get("disposition", {}).get("attached_pic", 0) == 0]
    for s in video_streams:
        map_args += ["-map", f"0:{s['index']}"]

    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    for s in audio_streams:
        map_args += ["-map", f"0:{s['index']}"]

    subtitle_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
    sub_codec_args = []
    sub_index = 0
    skipped_subs = 0
    for s in subtitle_streams:
        codec = s.get("codec_name", "").lower()
        if codec in MKV_SUBTITLE_CODECS:
            map_args += ["-map", f"0:{s['index']}"]
            sub_codec_args += [f"-c:s:{sub_index}", "copy"]
            sub_index += 1
        elif codec in CONVERT_TO_SRT:
            map_args += ["-map", f"0:{s['index']}"]
            sub_codec_args += [f"-c:s:{sub_index}", "srt"]
            sub_index += 1
        else:
            skipped_subs += 1

    if skipped_subs:
        print(f"  [INFO] Přeskočeno {skipped_subs} nekompatibilních subtitle streamů")

    codec_args = ["-c:v", "copy", "-c:a", "copy"] + sub_codec_args
    disp_args = [
        "-disposition:a", "none",
        "-disposition:a:0", "default",
        "-metadata:s:a:0", "language=eng",
        "-metadata:s:a:0", f"title={AUDIO_META.get(audio_ext, 'English')}",
    ]

    return cmd + map_args + codec_args + disp_args + [out_path]

def fix_metadata(mkv_path):
    """Doplní názvy stop a nastaví EN audio + CS titulky jako výchozí."""
    if not MKVPROPEDIT:
        print("  [VAROVÁNÍ] mkvpropedit nenalezen — metadata přeskočena.")
        print("             Nainstaluj MKVToolNix nebo přidej ho do PATH.")
        return

    streams = get_streams(mkv_path)
    edit_args = []

    audio_streams    = [s for s in streams if s.get("codec_type") == "audio"]
    subtitle_streams = [s for s in streams if s.get("codec_type") == "subtitle"]

    # Najdi EN audio a CS titulky
    en_audio_index = None
    cs_sub_index   = None
    for s in audio_streams:
        lang = s.get("tags", {}).get("language", "").strip().lower()
        if lang in ("eng", "en") and en_audio_index is None:
            en_audio_index = s["index"]
    for s in subtitle_streams:
        lang = s.get("tags", {}).get("language", "").strip().lower()
        if lang in ("ces", "cze", "cs") and cs_sub_index is None:
            cs_sub_index = s["index"]

    # Doplň chybějící názvy
    for s in streams:
        if s.get("codec_type") not in ("audio", "subtitle"):
            continue
        tags = s.get("tags", {})
        if tags.get("title", "").strip():
            continue
        lang = tags.get("language", "").strip().lower()
        name = LANG_NAMES.get(lang)
        if name:
            track_num = s["index"] + 1
            edit_args += ["--edit", f"track:{track_num}", "--set", f"name={name}"]

    # Nastav default audio
    for s in audio_streams:
        track_num = s["index"] + 1
        is_en = s["index"] == en_audio_index
        flag = "1" if is_en else "0"
        edit_args += ["--edit", f"track:{track_num}", "--set", f"flag-default={flag}"]

    # Nastav default titulky
    for s in subtitle_streams:
        track_num = s["index"] + 1
        is_cs = s["index"] == cs_sub_index
        flag = "1" if is_cs else "0"
        edit_args += ["--edit", f"track:{track_num}", "--set", f"flag-default={flag}"]

    if not edit_args:
        return

    cmd = [MKVPROPEDIT, mkv_path] + edit_args
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  [CHYBA mkvpropedit] {result.stderr.strip()}")
    else:
        en_info = f"track {en_audio_index + 1}" if en_audio_index is not None else "nenalezeno"
        cs_info = f"track {cs_sub_index + 1}"   if cs_sub_index   is not None else "nenalezeno"
        print(f"  [META] EN audio výchozí: {en_info} | CS titulky výchozí: {cs_info}")

def merge(video_path, audio_path, audio_ext, out_path):
    streams = get_streams(video_path)
    cmd = build_ffmpeg_cmd(video_path, audio_path, audio_ext, out_path, streams)
    subprocess.run(cmd)

def main():
    if MKVPROPEDIT:
        print(f"[INFO] mkvpropedit nalezen: {MKVPROPEDIT}")
    else:
        print("[VAROVÁNÍ] mkvpropedit nenalezen — názvy stop a výchozí stopy nebudou nastaveny.")
        print("           Nainstaluj MKVToolNix a přidej ho do PATH.\n")

    video_map = build_video_map(directory)
    audio_map = build_audio_map(directory)

    if not video_map:
        print("Žádné video soubory (MKV/MP4) nenalezeny.")
        return

    print(f"Nalezeno {len(video_map)} video souborů, {len(audio_map)} audio souborů\n")
    print("=" * 60)

    matched  = 0
    no_audio = 0

    for key in sorted(video_map.keys()):
        video_path, video_ext = video_map[key]
        filename = os.path.basename(video_path)
        base = os.path.splitext(filename)[0]
        out_path = os.path.join(directory, f"{base}_merged.mkv")

        print(f"Video: {filename}")

        if key not in audio_map:
            print(f"  [VAROVÁNÍ] Žádný audio soubor pro {key}, přeskakuji.\n")
            no_audio += 1
            continue

        audio_path, audio_ext = audio_map[key]
        print(f"Audio: {os.path.basename(audio_path)}")
        print(f"  -> {os.path.basename(out_path)}")

        merge(video_path, audio_path, audio_ext, out_path)

        if os.path.exists(out_path):
            fix_metadata(out_path)

        matched += 1
        print()

    print("=" * 60)
    print(f"Zpracováno: {matched}")
    print(f"Bez audio:  {no_audio}")
    print("Hotovo!")

if __name__ == "__main__":
    main()
