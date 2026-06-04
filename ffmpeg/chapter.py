import subprocess
import json
import sys
from pathlib import Path

def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)

def get_chapters(ffprobe, mkv_path):
    result = run([
        str(ffprobe), '-v', 'quiet',
        '-print_format', 'json',
        '-show_chapters',
        str(mkv_path)
    ])
    return json.loads(result.stdout).get('chapters', [])

def seconds_to_ts(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:01d}:{m:02d}:{s:06.3f}"

def main():
    if len(sys.argv) < 2:
        print("Použití: python skript.py \"cesta\\k\\souboru.mkv\"")
        sys.exit(1)

    mkv_path = Path(sys.argv[1])
    script_dir = Path(__file__).parent
    ffprobe = script_dir / 'ffprobe.exe'

    if not ffprobe.exists():
        print(f"CHYBA: ffprobe.exe nenalezen v {script_dir}")
        sys.exit(1)

    if not mkv_path.exists():
        print(f"CHYBA: MKV soubor nenalezen: {mkv_path}")
        sys.exit(1)

    chapters = get_chapters(ffprobe, mkv_path)

    if not chapters:
        print("Žádné chaptery nenalezeny")
        return

    print(f"Nalezeno {len(chapters)} chapterů\n")
    print(f"{'Timestamp':<20} Název")
    print("-" * 40)

    for ch in chapters:
        start_time = float(ch.get('start_time', 0))
        title = ch.get('tags', {}).get('title', '')
        ts = seconds_to_ts(start_time)
        print(f"{ts:<20} {title}")

    print(f"\n--- Zkopíruj do tsMuxeR (Chapters pole) ---\n")
    for ch in chapters:
        start_time = float(ch.get('start_time', 0))
        print(seconds_to_ts(start_time))

if __name__ == '__main__':
    main()