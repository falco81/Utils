import subprocess
import sys
import json
import re
from pathlib import Path

# ============================================================
# NASTAVENÍ - změň podle potřeby
FONT_SIZE = 60        # velikost písma titulků (doporučeno 45-65 pro 1080p)
FONT_NAME = "Arial"   # font
MKVMERGE = r"C:\Program Files\MKVToolNix\mkvmerge.exe"
# ============================================================

def run(cmd, silent=True, **kwargs):
    if silent:
        return subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    else:
        return subprocess.run(cmd, text=True, **kwargs)

def get_subtitle_streams(mkv_path):
    result = run([
        'ffprobe', '-v', 'quiet',
        '-print_format', 'json',
        '-show_streams',
        '-select_streams', 's',
        str(mkv_path)
    ])
    data = json.loads(result.stdout)
    streams = []
    for stream in data.get('streams', []):
        tags = stream.get('tags', {})
        disposition = stream.get('disposition', {})
        streams.append({
            'index': stream['index'],
            'language': tags.get('language', 'und'),
            'title': tags.get('title', ''),
            'default': disposition.get('default', 0),
            'forced': disposition.get('forced', 0),
            'hearing_impaired': disposition.get('hearing_impaired', 0),
            'original': disposition.get('original', 0),
            'dub': disposition.get('dub', 0),
        })
    return streams

def extract_srt(mkv_path, stream_index, out_path):
    result = run([
        'ffmpeg', '-y',
        '-i', str(mkv_path),
        '-map', f'0:{stream_index}',
        '-c:s', 'srt',
        str(out_path)
    ])
    return result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0

def patch_se_settings(se_path):
    """Nastaví velikost písma v SubtitleEdit konfiguráku"""
    settings_path = se_path.parent / 'Settings.xml'
    if not settings_path.exists():
        print(f"  Varování: Settings.xml nenalezen")
        return

    content = settings_path.read_text(encoding='utf-8', errors='replace')
    content = re.sub(r'<ExportBluRayFontName>.*?</ExportBluRayFontName>', f'<ExportBluRayFontName>{FONT_NAME}</ExportBluRayFontName>', content)
    content = re.sub(r'<ExportBluRayFontSize>.*?</ExportBluRayFontSize>', f'<ExportBluRayFontSize>{FONT_SIZE}</ExportBluRayFontSize>', content)
    content = re.sub(r'<ExportLastFontSize>.*?</ExportLastFontSize>', f'<ExportLastFontSize>{FONT_SIZE}</ExportLastFontSize>', content)
    content = re.sub(r'<ExportBluRayVideoResolution>.*?</ExportBluRayVideoResolution>', f'<ExportBluRayVideoResolution>1920x1080</ExportBluRayVideoResolution>', content)
    settings_path.write_text(content, encoding='utf-8')
    print(f"  SubtitleEdit nastaven: font={FONT_NAME}, size={FONT_SIZE}px, rozlišení=1920x1080")

def convert_to_sup(se_path, srt_path, sup_path):
    """Přímá konverze SRT -> SUP"""
    result = run([
        str(se_path),
        '/convert', str(srt_path),
        'Blu-raysup',
        '/resolution:1920x1080',
        '/overwrite'
    ])
    return sup_path.exists()

def merge_mkv(mkv_path, sup_files, streams, out_path):
    """Použij mkvmerge pro správné zachování timingu"""

    cmd = [
        MKVMERGE,
        '-o', str(out_path),
        '--no-subtitles',
        str(mkv_path)
    ]

    for sup, stream in zip(sup_files, streams):
        cmd += ['--language', f'0:{stream["language"]}']

        if stream['title']:
            cmd += ['--track-name', f'0:{stream["title"]}']

        cmd += ['--default-track-flag', f'0:{"yes" if stream["default"] == 1 else "no"}']
        cmd += ['--forced-display-flag', f'0:{"yes" if stream["forced"] == 1 else "no"}']

        if stream.get('hearing_impaired', 0) == 1:
            cmd += ['--hearing-impaired-flag', '0:yes']

        if stream.get('original', 0) == 1:
            cmd += ['--original-flag', '0:yes']

        cmd.append(str(sup))

    result = run(cmd, silent=False)
    return result.returncode == 0

def process_mkv(mkv_path, se_path):
    base = mkv_path.stem
    folder = mkv_path.parent
    out_path = folder / f"{base}_final.mkv"

    if out_path.exists():
        print(f"  Přeskakuji - výstup již existuje: {out_path.name}")
        return

    print(f"\n{'='*60}")
    print(f"Zpracovávám: {mkv_path.name}")
    print(f"{'='*60}")

    streams = get_subtitle_streams(mkv_path)
    if not streams:
        print("  Žádné titulkové stopy - přeskakuji")
        return

    print(f"  Počet titulkových stop: {len(streams)}")
    print(f"  Velikost písma: {FONT_SIZE}px  Font: {FONT_NAME}")

    sup_files = []
    successful_streams = []
    temp_files = []

    for idx, stream in enumerate(streams):
        lang = stream['language']
        title = stream['title']
        print(f"\n  Stopa {stream['index']} - jazyk: {lang} - title: {title}")

        srt_path = folder / f"{base}_tmp_{idx}_{lang}.srt"
        sup_path = folder / f"{base}_tmp_{idx}_{lang}.sup"
        temp_files += [srt_path, sup_path]

        print(f"    Extrahuji SRT...")
        if not extract_srt(mkv_path, stream['index'], srt_path):
            print(f"    CHYBA: Nepodařilo se extrahovat stopu {stream['index']}")
            continue

        print(f"    Převádím SRT -> SUP...")
        if not convert_to_sup(se_path, srt_path, sup_path):
            print(f"    CHYBA: Nepodařilo se převést na SUP")
            srt_path.unlink(missing_ok=True)
            continue
        srt_path.unlink(missing_ok=True)

        print(f"    Hotovo: {sup_path.name}")
        sup_files.append(sup_path)
        successful_streams.append(stream)

    if not sup_files:
        print("\n  CHYBA: Žádné SUP soubory nebyly vytvořeny")
        return

    print(f"\n  Sestavuji výsledné MKV (mkvmerge)...")
    if merge_mkv(mkv_path, sup_files, successful_streams, out_path):
        print(f"  Hotovo: {out_path.name}")
    else:
        print(f"  CHYBA: Nepodařilo se sestavit MKV")

    for f in temp_files:
        f.unlink(missing_ok=True)

def main():
    script_dir = Path(__file__).parent
    se_path = script_dir / 'SubtitleEdit.exe'

    if not se_path.exists():
        print(f"CHYBA: SubtitleEdit.exe nenalezen v {script_dir}")
        sys.exit(1)

    if run(['ffmpeg', '-version']).returncode != 0:
        print("CHYBA: ffmpeg není dostupný v PATH")
        sys.exit(1)

    if run(['ffprobe', '-version']).returncode != 0:
        print("CHYBA: ffprobe není dostupný v PATH")
        sys.exit(1)

    if not Path(MKVMERGE).exists():
        print(f"CHYBA: mkvmerge nenalezen na cestě: {MKVMERGE}")
        sys.exit(1)

    mkv_files = [
        f for f in sorted(script_dir.glob('*.mkv'))
        if not f.stem.endswith('_final')
    ]

    if not mkv_files:
        print("Žádné MKV soubory nenalezeny")
        sys.exit(0)

    print(f"Nalezeno {len(mkv_files)} MKV souborů")
    print(f"Velikost písma: {FONT_SIZE}px  Font: {FONT_NAME}")

    patch_se_settings(se_path)

    for mkv in mkv_files:
        try:
            process_mkv(mkv, se_path)
        except Exception as e:
            print(f"  CHYBA při zpracování {mkv.name}: {e}")
            continue

    print(f"\n{'='*60}")
    print("Vše dokončeno!")

if __name__ == '__main__':
    main()