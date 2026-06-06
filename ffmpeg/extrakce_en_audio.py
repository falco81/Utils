import os
import json
import subprocess

directory = r"."  # složka s MKV soubory, nebo zadej konkrétní cestu

CODEC_EXT = {
    "aac":  ".aac",
    "ac3":  ".ac3",
    "eac3": ".eac3",
    "dts":  ".dts",
    "truehd": ".thd",
    "mp3":  ".mp3",
    "flac": ".flac",
}

def get_en_audio_stream(mkv_path):
    """Vrátí (index_stopy, codec) pro první EN audio stopu, nebo None."""
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-select_streams", "a",
        mkv_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(result.stdout)

    for i, stream in enumerate(data.get("streams", [])):
        tags = stream.get("tags", {})
        lang = tags.get("language", "").lower()
        if lang == "eng":
            codec = stream.get("codec_name", "").lower()
            return i, codec

    return None, None

def extract_audio(mkv_path, stream_index, codec):
    ext = CODEC_EXT.get(codec, ".mka")
    base = os.path.splitext(mkv_path)[0]
    out_path = f"{base}_EN{ext}"

    cmd = [
        "ffmpeg", "-y",
        "-i", mkv_path,
        "-map", f"0:a:{stream_index}",
        "-c:a", "copy",
        out_path
    ]
    print(f"  -> {os.path.basename(out_path)} ({codec.upper()})")
    subprocess.run(cmd)

def main():
    mkv_files = sorted(f for f in os.listdir(directory) if f.lower().endswith(".mkv"))

    if not mkv_files:
        print("Žádné MKV soubory nenalezeny.")
        return

    print(f"Nalezeno {len(mkv_files)} MKV souborů\n")
    print("=" * 50)

    for filename in mkv_files:
        path = os.path.join(directory, filename)
        print(f"Zpracovávám: {filename}")

        stream_index, codec = get_en_audio_stream(path)

        if stream_index is None:
            print("  [VAROVÁNÍ] EN audio stopa nenalezena, přeskakuji.\n")
            continue

        extract_audio(path, stream_index, codec)
        print()

    print("=" * 50)
    print("Hotovo!")

if __name__ == "__main__":
    main()
