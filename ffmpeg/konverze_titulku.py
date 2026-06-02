import subprocess
import sys
import json
from pathlib import Path

# ============================================================
# NASTAVENÍ - změň podle potřeby
FONT_SIZE = 55        # velikost písma titulků (doporučeno 45-65 pro 1080p)
FONT_NAME = "Arial"   # font
# ============================================================

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,1,2,10,10,20,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

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

def srt_time_to_ass(ts):
    """Převede SRT časový formát na ASS formát"""
    ts = ts.strip().replace(',', '.')
    h, m, rest = ts.split(':')
    s, ms = rest.split('.')
    ms = ms[:2]  # ASS má jen 2 desetinná místa
    return f"{int(h)}:{m}:{s}.{ms}"

def srt_to_ass(srt_path, ass_path):
    """Převede SRT na ASS s nastavenou velikostí písma"""
    header = ASS_HEADER.format(font=FONT_NAME, size=FONT_SIZE)
    
    content = srt_path.read_text(encoding='utf-8', errors='replace')
    blocks = content.strip().split('\n\n')
    
    lines = []
    for block in blocks:
        block_lines = block.strip().split('\n')
        if len(block_lines) < 3:
            continue
        # Přeskoč číslo titulku
        time_line = None
        text_lines = []
        for bl in block_lines:
            if '-->' in bl:
                time_line = bl
            elif bl.strip().isdigit():
                continue
            elif time_line is not None:
                text_lines.append(bl)
        
        if not time_line or not text_lines:
            continue
        
        try:
            start_raw, end_raw = time_line.split('-->')
            start = srt_time_to_ass(start_raw)
            end = srt_time_to_ass(end_raw)
        except Exception:
            continue
        
        text = r'\N'.join(text_lines)
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")
    
    ass_path.write_text(header + '\n'.join(lines) + '\n', encoding='utf-8')
    return ass_path.exists() and ass_path.stat().st_size > 0

def convert_to_sup(se_path, ass_path, sup_path):
    result = run([
        str(se_path),
        '/convert', str(ass_path),
        'Blu-raysup',
        '/resolution:1920x1080',
        '/overwrite'
    ], silent=False)
    return sup_path.exists()

def merge_mkv(mkv_path, sup_files, streams, out_path):
    cmd = ['ffmpeg', '-y', '-i', str(mkv_path)]
    for sup in sup_files:
        cmd += ['-i', str(sup)]
    cmd += ['-map', '0:v', '-map', '0:a']
    for i in range(len(sup_files)):
        cmd += ['-map', f'{i+1}:s']
    cmd += ['-c:v', 'copy', '-c:a', 'copy', '-c:s', 'copy']
    for i, stream in enumerate(streams):
        cmd += [f'-metadata:s:s:{i}', f'language={stream["language"]}']
        if stream['title']:
            cmd += [f'-metadata:s:s:{i}', f'title={stream["title"]}']
        if stream['default'] == 1:
            cmd += [f'-disposition:s:{i}', 'default']
        elif stream['forced'] == 1:
            cmd += [f'-disposition:s:{i}', 'forced']
        else:
            cmd += [f'-disposition:s:{i}', 'none']
    cmd.append(str(out_path))
    result = run(cmd)
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
        ass_path = folder / f"{base}_tmp_{idx}_{lang}.ass"
        sup_path = folder / f"{base}_tmp_{idx}_{lang}.sup"
        temp_files += [srt_path, ass_path, sup_path]

        print(f"    Extrahuji SRT...")
        if not extract_srt(mkv_path, stream['index'], srt_path):
            print(f"    CHYBA: Nepodařilo se extrahovat stopu {stream['index']}")
            continue

        print(f"    Převádím SRT → ASS (font {FONT_NAME} {FONT_SIZE}px)...")
        if not srt_to_ass(srt_path, ass_path):
            print(f"    CHYBA: Nepodařilo se převést na ASS")
            srt_path.unlink(missing_ok=True)
            continue
        srt_path.unlink(missing_ok=True)

        print(f"    Převádím ASS → SUP...")
        if not convert_to_sup(se_path, ass_path, sup_path):
            print(f"    CHYBA: Nepodařilo se převést na SUP")
            ass_path.unlink(missing_ok=True)
            continue
        ass_path.unlink(missing_ok=True)

        print(f"    Hotovo: {sup_path.name}")
        sup_files.append(sup_path)
        successful_streams.append(stream)

    if not sup_files:
        print("\n  CHYBA: Žádné SUP soubory nebyly vytvořeny")
        return

    print(f"\n  Sestavuji výsledné MKV...")
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

    mkv_files = [
        f for f in sorted(script_dir.glob('*.mkv'))
        if not f.stem.endswith('_final')
    ]

    if not mkv_files:
        print("Žádné MKV soubory nenalezeny")
        sys.exit(0)

    print(f"Nalezeno {len(mkv_files)} MKV souborů")
    print(f"Velikost písma: {FONT_SIZE}px  Font: {FONT_NAME}")

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