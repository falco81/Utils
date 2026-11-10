#!/usr/bin/env python3
"""
video_check.py - find videos that are not standard H.264 / H.265, list videos
by codec, and convert them.

Usage:
    python video_check.py -r                       list videos that are not H.264 MP4
    python video_check.py -r --all                 list all videos
    python video_check.py -r --codec h265          list H.265 videos
    python video_check.py -r --codec h265 --names  print only their paths
    python video_check.py -r --fix                 everything -> H.264 MP4
                                                   (H.264 in MKV/AVI is only remuxed)
    python video_check.py -r --to h265             all H.264 -> H.265 MP4
    python video_check.py -r --to h264             all H.265 -> H.264 MP4

Original files are NEVER modified or overwritten:
    --fix:     film.avi -> film.mp4   (film.mp4 -> film_h264.mp4)
    --to h265: film.mkv -> film_h265.mp4
Use -o to save converted files into another folder.

Optional dependency for colored output:
    pip install colorama
"""

import argparse
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    from colorama import Fore, Style, just_fix_windows_console
    just_fix_windows_console()
except ImportError:  # works without colorama, just without colors
    class _NoColor:
        def __getattr__(self, name):
            return ""
    Fore = Style = _NoColor()

DEFAULT_FFMPEG = r"C:\Program Files\ffmpeg\ffmpeg.exe"
BAR_WIDTH = 30

VIDEO_EXTENSIONS = {
    ".avi", ".mkv", ".mp4", ".m4v", ".mov", ".wmv", ".asf", ".flv", ".f4v",
    ".mpg", ".mpeg", ".m2v", ".vob", ".ts", ".mts", ".m2ts", ".webm",
    ".3gp", ".3g2", ".ogv", ".divx", ".xvid", ".rm", ".rmvb", ".dv", ".mxf",
}
STANDARD_CODECS = {"h264", "hevc"}          # shown green in the table
MP4_EXTENSIONS = {".mp4", ".m4v"}
CODEC_NAMES = {"h264": "H.264", "hevc": "H.265"}


# ---------------------------------------------------------------- helpers

def fmt_time(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--:--"
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def fmt_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{int(num_bytes)} B"
        num_bytes /= 1024
    return ""


def color(text: str, c: str) -> str:
    return f"{c}{text}{Style.RESET_ALL}"


def info(msg: str) -> None:
    print(color(msg, Fore.CYAN))


def ok(msg: str) -> None:
    print(color(msg, Fore.GREEN))


def warn(msg: str) -> None:
    print(color(msg, Fore.YELLOW))


def err(msg: str) -> None:
    print(color(msg, Fore.RED))


def fit(text: str, width: int) -> str:
    """Pad or shorten text (with … in the middle) to an exact width."""
    if len(text) <= width:
        return text.ljust(width)
    if width < 5:
        return text[:width]
    head = (width - 1) // 2
    tail = width - 1 - head
    return text[:head] + "…" + text[-tail:]


# ---------------------------------------------------------------- ffmpeg / ffprobe

def find_ffmpeg(custom: str | None) -> str:
    for candidate in (custom, DEFAULT_FFMPEG):
        if candidate and Path(candidate).is_file():
            return candidate
    in_path = shutil.which("ffmpeg")
    if in_path:
        return in_path
    err(f"Error: ffmpeg not found (tried {DEFAULT_FFMPEG} and PATH). "
        "Use --ffmpeg to specify its location.")
    sys.exit(2)


def find_ffprobe(ffmpeg: str) -> str:
    p = Path(ffmpeg)
    candidate = p.with_name(p.name.replace("ffmpeg", "ffprobe"))
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("ffprobe")
    if found:
        return found
    err("Error: ffprobe not found (it should be next to ffmpeg).")
    sys.exit(2)


def probe(ffprobe: str, path: Path) -> dict:
    """Return info about the file: video codec, resolution, audio codec, duration."""
    result = {"path": path, "size": path.stat().st_size, "error": None,
              "vcodec": None, "res": "", "acodec": "", "duration": None}
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json",
             "-show_entries", "format=duration:stream=codec_type,codec_name,width,height"
                              ":stream_disposition=attached_pic",
             str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        data = json.loads(proc.stdout or "{}")
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as e:
        result["error"] = str(e)
        return result

    streams = data.get("streams", [])
    # ignore cover-art images embedded as "video" streams
    videos = [s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")]
    audios = [s for s in streams if s.get("codec_type") == "audio"]

    if not videos:
        msg = proc.stderr.strip().splitlines()
        text = msg[-1] if msg else "no video stream found"
        result["error"] = text.removeprefix(f"{path}: ")   # drop repeated file name
        return result

    v = videos[0]
    result["vcodec"] = v.get("codec_name", "unknown")
    if v.get("width") and v.get("height"):
        result["res"] = f"{v['width']}x{v['height']}"
    result["acodec"] = ",".join(dict.fromkeys(a.get("codec_name", "?") for a in audios)) or "-"
    try:
        result["duration"] = float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        pass
    return result


def collect_files(inputs: list[Path], recursive: bool) -> list[Path]:
    files = []
    for p in inputs:
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            pattern = "**/*" if recursive else "*"
            files += sorted(f for f in p.glob(pattern)
                            if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS)
        else:
            warn(f"Not found: {p}")
    return files


# ---------------------------------------------------------------- conversion

def render_progress(done: float, duration: float | None, started: float,
                    speed: str, size: int) -> tuple[str, int]:
    """Return (colored line, visible length). Optional fields are dropped
    so the line never wraps in a narrow console window."""
    elapsed = time.monotonic() - started
    width = shutil.get_terminal_size((80, 20)).columns - 1

    if duration:
        frac = min(done / duration, 1.0)
        filled = int(BAR_WIDTH * frac)
        bar = "█" * filled + "░" * (BAR_WIDTH - filled)
        eta = (elapsed / frac - elapsed) if frac > 0.002 else None
        pct = f"{frac * 100:5.1f}%"
        eta_s = f"ETA {fmt_time(eta)}"
        parts = [
            (f"   {bar}", f"   {Fore.GREEN}{bar}{Style.RESET_ALL}"),
            (pct, f"{Style.BRIGHT}{pct}{Style.RESET_ALL}"),
            (eta_s, f"{Fore.YELLOW}{eta_s}{Style.RESET_ALL}"),
        ]
    else:
        spinner = "|/-\\"[int(elapsed * 4) % 4]
        parts = [(f"   {spinner} {fmt_time(done)}",) * 2, ("ETA unknown",) * 2]

    speed_s = f"{speed:>6}"
    optional = [
        (speed_s, f"{Fore.MAGENTA}{speed_s}{Style.RESET_ALL}"),
        (f"elapsed {fmt_time(elapsed)}",) * 2,
        (f"{fmt_size(size):>9}",) * 2,
    ]
    if duration:
        optional.insert(1, (f"{fmt_time(done)}/{fmt_time(duration)}",) * 2)

    plain_len = sum(len(p[0]) for p in parts) + 2 * (len(parts) - 1)
    for opt in optional:
        if plain_len + 2 + len(opt[0]) <= width:
            parts.append(opt)
            plain_len += 2 + len(opt[0])
    return "  ".join(p[1] for p in parts), plain_len


def convert(ffmpeg: str, src: Path, dst: Path, duration: float | None,
            target: str, crf: int, preset: str, audio_bitrate: str,
            copy_audio: bool) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if target == "copy":                         # H.264 already -> just remux to MP4
        video = ["-c:v", "copy"]
    elif target == "hevc":
        video = ["-c:v", "libx265", "-crf", str(crf), "-preset", preset,
                 "-x265-params", "log-level=error",
                 "-tag:v", "hvc1"]                  # plays on Apple devices / QuickTime too
    else:
        video = ["-c:v", "libx264", "-crf", str(crf), "-preset", preset]
    audio = ["-c:a", "copy"] if copy_audio else ["-c:a", "aac", "-b:a", audio_bitrate]
    filters = [] if target == "copy" else [
        "-pix_fmt", "yuv420p",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",  # encoder needs even dimensions
    ]
    cmd = [
        ffmpeg,
        "-hide_banner", "-loglevel", "error", "-nostats",
        "-progress", "pipe:1",
        "-n",                                       # never overwrite anything
        *(["-fflags", "+genpts"] if target == "copy" else []),  # fixes AVI timestamps
        "-i", str(src),
        "-map", "0:v:0", "-map", "0:a?",            # first video + all audio tracks
        *video,
        *filters,
        *audio,
        "-movflags", "+faststart",
        str(dst),
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True,
                            encoding="utf-8", errors="replace", bufsize=1)
    stderr_lines: list[str] = []
    t = threading.Thread(target=lambda: stderr_lines.extend(proc.stderr), daemon=True)
    t.start()

    started = time.monotonic()
    done, speed, size, last_len = 0.0, "?", 0, 0
    try:
        for raw in proc.stdout:
            key, _, value = raw.strip().partition("=")
            if key in ("out_time_us", "out_time_ms"):   # both are microseconds
                try:
                    done = max(int(value) / 1_000_000, 0.0)
                except ValueError:
                    pass
            elif key == "speed":
                speed = value.strip() or "?"
            elif key == "total_size":
                try:
                    size = int(value)
                except ValueError:
                    pass
            elif key == "progress":
                if value == "end" and duration:
                    done = duration
                line, vis_len = render_progress(done, duration, started, speed, size)
                sys.stdout.write("\r" + line + " " * max(last_len - vis_len, 0))
                sys.stdout.flush()
                last_len = vis_len
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
        print()
        warn("   Interrupted by user.")
        _remove(dst)
        raise

    t.join(timeout=5)
    print()
    if proc.returncode != 0:
        for line in stderr_lines[-10:]:
            err("   ffmpeg: " + line.rstrip())
        _remove(dst)
        return False
    return True


def _remove(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


CODEC_ALIASES = {
    "h264": "h264", "h.264": "h264", "avc": "h264", "x264": "h264", "264": "h264",
    "h265": "hevc", "h.265": "hevc", "hevc": "hevc", "x265": "hevc", "265": "hevc",
    "divx": "mpeg4", "xvid": "mpeg4", "mpeg4": "mpeg4",
    "mpeg2": "mpeg2video", "mpeg-2": "mpeg2video", "mpeg1": "mpeg1video",
    "wmv": "wmv3", "vc1": "vc1", "vc-1": "vc1",
}
TARGETS = {"h264": "h264", "h265": "hevc"}         # --to value -> ffprobe codec name
DEFAULT_CRF = {"h264": 23, "hevc": 28}             # visually about the same quality
MP4_SAFE_AUDIO = {"aac", "mp3", "ac3", "eac3"}     # can be copied into MP4 untouched


def normalize_codec(name: str) -> str:
    n = name.strip().lower()
    return CODEC_ALIASES.get(n, n)


def output_path(src: Path, inputs: list[Path], out_dir: Path | None,
                tag: str | None = None, with_ext: bool = False) -> Path:
    """tag=None (--fix): film.avi -> film.mp4, film.mp4 -> film_h264.mp4
       tag='h265' (--to): always film_h265.mp4
       with_ext=True adds the source extension to avoid name clashes:
       film.avi -> film_avi.mp4 / film_avi_h265.mp4"""
    stem = f"{src.stem}_{src.suffix.lower().lstrip('.')}" if with_ext else src.stem
    if tag:
        name = f"{stem}_{tag}.mp4"
    else:
        name = stem + ("_h264.mp4" if src.suffix.lower() == ".mp4" else ".mp4")
    if out_dir is None:
        dst = src.with_name(name)
    else:
        base = next((p for p in inputs
                     if p.is_dir() and src.resolve().is_relative_to(p.resolve())), None)
        rel_dir = src.resolve().parent.relative_to(base.resolve()) if base else Path()
        dst = out_dir / rel_dir / name
    if dst.resolve() == src.resolve():          # paranoia: never target the original
        dst = dst.with_name(f"{src.stem}_{tag or 'h264'}_new.mp4")
    return dst


def plan_outputs(sources: list[dict], all_rows: list[dict], ffprobe: str,
                 args: argparse.Namespace, tag: str | None,
                 wanted: str) -> list[tuple[dict, Path]]:
    """Pick an output path for every source so that no two sources share one
    and no unrelated existing file is mistaken for a finished conversion.
    wanted = codec the output should have ('h264' / 'hevc')."""
    known = {r["path"].resolve(): r for r in all_rows}

    def is_finished(dst: Path) -> bool:
        """An existing file counts as done only if it is in the wanted codec."""
        info_ = known.get(dst.resolve()) or probe(ffprobe, dst)
        return not info_["error"] and info_["vcodec"] == wanted

    # 1) default names; 2) sources that would share a name (film.avi + film.mkv)
    #    or hit an unrelated existing file get the source extension in the name
    default = {id(r): output_path(r["path"], args.inputs, args.output, tag) for r in sources}
    usage: dict[Path, int] = {}
    for d in default.values():
        usage[d.resolve()] = usage.get(d.resolve(), 0) + 1

    plan = []
    for r in sources:
        dst = default[id(r)]
        if usage[dst.resolve()] > 1 or (dst.exists() and not is_finished(dst)):
            dst = output_path(r["path"], args.inputs, args.output, tag, with_ext=True)
        plan.append((r, dst))
    return plan


def fix_action(r: dict) -> str:
    """What --fix would do: 'encode' (not H.264), 'remux' (H.264 but not MP4), '' (ok)."""
    if r["error"]:
        return ""
    if r["vcodec"] != "h264":
        return "encode"
    if r["path"].suffix.lower() not in MP4_EXTENSIONS:
        return "remux"
    return ""


# ---------------------------------------------------------------- report

def print_table(rows: list[dict]) -> None:
    term = shutil.get_terminal_size((100, 20)).columns - 1
    fixed = 2 + 10 + 2 + 9 + 2 + 10 + 2 + 8 + 2 + 9 + 2 + 6   # codec ... size, fix
    name_w = max(20, term - fixed)

    header = (f"{'File':<{name_w}}  {'Video':<10}  {'Res':<9}  {'Audio':<10}  "
              f"{'Length':<8}  {'Size':>9}  {'Fix':<6}")
    print(color(header, Style.BRIGHT))
    print("-" * min(len(header), term))

    for r in rows:
        name = fit(str(r["display"]), name_w)
        if r["error"]:
            print(f"{name}  " + color(fit("ERROR: " + r["error"], term - name_w - 2), Fore.RED))
            continue
        vc = r["vcodec"]
        standard = vc in STANDARD_CODECS
        vc_txt = color(fit(CODEC_NAMES.get(vc, vc), 10),
                       Fore.GREEN if standard else Fore.YELLOW + Style.BRIGHT)
        action = fix_action(r)
        act_txt = color(f"{action or 'ok':<6}",
                        Fore.RED if action == "encode" else
                        Fore.YELLOW if action == "remux" else Fore.GREEN)
        print(f"{name}  {vc_txt}  {fit(r['res'], 9)}  {fit(r['acodec'], 10)}  "
              f"{fmt_time(r['duration'])[:8]:<8}  {fmt_size(r['size']):>9}  {act_txt}")


def run_conversion(jobs: list[tuple[dict, Path, str]], ffmpeg: str,
                   args: argparse.Namespace) -> None:
    """jobs: (row, output path, target) where target is 'h264', 'hevc' or 'copy'."""
    targets = {t for _, _, t in jobs if t != "copy"}
    label = " / ".join(CODEC_NAMES[t] for t in sorted(targets)) or "H.264"
    crf_txt = ", ".join(f"CRF {args.crf if args.crf is not None else DEFAULT_CRF[t]}"
                        for t in sorted(targets))
    remux = sum(t == "copy" for _, _, t in jobs)
    print()
    info(f"Converting {len(jobs)} video(s) to {label} MP4"
         + (f" ({crf_txt}, preset {args.preset})" if targets else "")
         + (f", {remux} of them remux only" if remux else "") + "...\n")
    converted, skipped, failed = 0, 0, []
    total_in = total_out = 0
    batch_start = time.monotonic()
    try:
        for i, (r, dst, target) in enumerate(jobs, 1):
            crf = args.crf if args.crf is not None else DEFAULT_CRF.get(target, 23)
            src = r["path"]
            header = color(f"[{i}/{len(jobs)}]", Style.BRIGHT)
            if dst.exists():
                print(f"{header} " + color(f"Skipped, output already exists: {dst}", Fore.YELLOW))
                skipped += 1
                continue
            audio_codecs = {a for a in r["acodec"].split(",") if a and a != "-"}
            copy_audio = audio_codecs <= MP4_SAFE_AUDIO       # also True if no audio
            how = "remux, no re-encoding" if target == "copy" else \
                  f"{CODEC_NAMES.get(r['vcodec'], r['vcodec'])} -> {CODEC_NAMES[target]}"
            print(f"{header} {src}  ({how}, {fmt_time(r['duration'])})  ->  "
                  f"{color(str(dst), Fore.CYAN)}")
            file_start = time.monotonic()
            if convert(ffmpeg, src, dst, r["duration"], target, crf, args.preset,
                       args.audio_bitrate, copy_audio):
                converted += 1
                out_size = dst.stat().st_size
                total_in += r["size"]
                total_out += out_size
                ok(f"   Done in {fmt_time(time.monotonic() - file_start)}  "
                   f"{fmt_size(r['size'])} -> {fmt_size(out_size)} "
                   f"({out_size / max(r['size'], 1) * 100:.0f}%)")
            else:
                failed.append(src)
                err("   Conversion FAILED.")
            print()
    except KeyboardInterrupt:
        warn("\nAborted.")
        sys.exit(130)

    summary = (f"Finished in {fmt_time(time.monotonic() - batch_start)}: "
               f"{converted} converted, {skipped} skipped, {len(failed)} failed.")
    (err if failed else ok)(summary)
    if converted:
        info(f"Size: {fmt_size(total_in)} -> {fmt_size(total_out)} "
             f"({total_out / max(total_in, 1) * 100:.0f}%)")
    for f in failed:
        err(f"  failed: {f}")
    sys.exit(1 if failed else 0)


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(
        description="List videos that are not H.264/H.265, list videos by codec, "
                    "and convert them (originals are never overwritten).",
        epilog="examples:\n"
               "  video_check.py -r                      list non-standard videos\n"
               "  video_check.py -r --codec h265         list all H.265 videos\n"
               "  video_check.py -r --codec h265 --names only file paths\n"
               "  video_check.py -r --fix                everything -> H.264 MP4\n"
               "  video_check.py -r --to h265            all H.264 -> H.265\n"
               "  video_check.py -r --to h264            all H.265 -> H.264\n"
               "  video_check.py -r --codec mpeg4 --to h265   DivX/Xvid -> H.265",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="*", type=Path, default=[Path(".")],
                    help="folders or files to check (default: current folder)")
    ap.add_argument("-r", "--recursive", action="store_true", help="include subfolders")
    ap.add_argument("--all", action="store_true",
                    help="list all videos, including the standard H.264/H.265 ones")
    ap.add_argument("-c", "--codec", action="append", metavar="CODEC",
                    help="list only videos in this codec, e.g. h264, h265, mpeg4, wmv2, vp9 "
                         "(can be repeated or comma separated)")
    ap.add_argument("--names", action="store_true",
                    help="print just the file paths instead of the table")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--fix", action="store_true",
                      help="make everything H.264 MP4: re-encode other codecs (incl. H.265), "
                           "remux H.264 from MKV/AVI/MOV... without re-encoding")
    mode.add_argument("--to", choices=sorted(TARGETS),
                      help="convert to this codec: --to h265 converts all H.264 videos, "
                           "--to h264 all H.265 videos (or the ones selected with --codec)")
    ap.add_argument("-o", "--output", type=Path,
                    help="output folder for converted files (default: next to the original)")
    ap.add_argument("--crf", type=int, default=None,
                    help="quality 0-51, lower = better and bigger "
                         "(default 23 for H.264, 28 for H.265)")
    ap.add_argument("--preset", default="medium",
                    choices=["ultrafast", "superfast", "veryfast", "faster", "fast",
                             "medium", "slow", "slower", "veryslow"],
                    help="encoding speed (default medium)")
    ap.add_argument("--audio-bitrate", default="192k",
                    help="audio bitrate when audio has to be re-encoded (default 192k)")
    ap.add_argument("--ffmpeg", help=f"path to ffmpeg.exe (default {DEFAULT_FFMPEG})")
    args = ap.parse_args()

    codec_filter: set[str] = set()
    for item in args.codec or []:
        codec_filter |= {normalize_codec(c) for c in item.split(",") if c.strip()}
    if args.fix and codec_filter:
        ap.error("--fix always converts everything to H.264 MP4; "
                 "use --codec ... --to h264 to convert specific codecs")

    ffmpeg = find_ffmpeg(args.ffmpeg)
    ffprobe = find_ffprobe(ffmpeg)
    files = collect_files(args.inputs, args.recursive)
    if not files:
        err("No video files found.")
        sys.exit(1)

    # ---- scan
    if not args.names:
        info(f"Scanning {len(files)} video file(s)...")
    rows = []
    for i, f in enumerate(files, 1):
        if not args.names:
            sys.stdout.write(f"\r   {i}/{len(files)}")
            sys.stdout.flush()
        r = probe(ffprobe, f)
        base = next((p for p in args.inputs if p.is_dir()
                     and f.resolve().is_relative_to(p.resolve())), None)
        r["display"] = f.resolve().relative_to(base.resolve()) if base else f
        rows.append(r)
    if not args.names:
        sys.stdout.write("\r" + " " * 20 + "\r")

    ok_rows = [r for r in rows if not r["error"]]
    to_fix = [r for r in ok_rows if fix_action(r)]       # not H.264 MP4 yet
    encode_n = sum(fix_action(r) == "encode" for r in to_fix)
    remux_n = len(to_fix) - encode_n
    broken = [r for r in rows if r["error"]]

    # ---- what to list
    if codec_filter:
        listed = [r for r in ok_rows if r["vcodec"] in codec_filter]
    elif args.all:
        listed = rows
    else:
        listed = to_fix + broken
        listed.sort(key=lambda r: str(r["display"]).lower())

    if args.names:
        if codec_filter:                 # plain list of paths, nothing else
            for r in listed:
                print(r["path"])
            if not listed:
                names = ", ".join(CODEC_NAMES.get(c, c) for c in sorted(codec_filter))
                print(color(f"No videos in {names} found.", Fore.YELLOW), file=sys.stderr)
        else:                            # all videos grouped by codec
            groups: dict[str, list[dict]] = {}
            for r in rows:
                key = "unreadable" if r["error"] else r["vcodec"]
                groups.setdefault(key, []).append(r)
            for key, items in sorted(groups.items(), key=lambda g: -len(g[1])):
                c = (Fore.RED if key == "unreadable" else
                     Fore.GREEN if key in STANDARD_CODECS else Fore.YELLOW)
                print(color(f"{CODEC_NAMES.get(key, key)} ({len(items)}):", c + Style.BRIGHT))
                for r in items:
                    print(f"  {r['path']}")
                print()
        if not (args.fix or args.to):
            return
    elif listed:
        print()
        print_table(listed)
        print()

    if not args.names:
        stats: dict[str, int] = {}
        for r in ok_rows:
            stats[r["vcodec"]] = stats.get(r["vcodec"], 0) + 1
        stats_txt = ", ".join(f"{CODEC_NAMES.get(k, k)}: {v}"
                              for k, v in sorted(stats.items(), key=lambda x: -x[1]))
        info(f"Codecs: {stats_txt or '-'}")
        if codec_filter:
            names = ", ".join(CODEC_NAMES.get(c, c) for c in sorted(codec_filter))
            info(f"Listed {len(listed)} video(s) in {names}.")
        summary = (f"Total {len(rows)}: {len(ok_rows) - len(to_fix)} OK (H.264 MP4), "
                   f"{encode_n} to re-encode, {remux_n} to remux, "
                   f"{len(broken)} unreadable.")
        (warn if to_fix or broken else ok)(summary)

    # ---- --fix: everything -> H.264 MP4
    if args.fix:
        if not to_fix:
            ok("Nothing to fix, all videos are H.264 MP4.")
            return
        jobs = [(r, dst, "h264" if fix_action(r) == "encode" else "copy")
                for r, dst in plan_outputs(to_fix, rows, ffprobe, args, None, "h264")]
        run_conversion(jobs, ffmpeg, args)
        return

    # ---- --to h264 / h265
    if args.to:
        target = TARGETS[args.to]
        if codec_filter:
            source = [r for r in ok_rows if r["vcodec"] in codec_filter]
            same = [r for r in source if r["vcodec"] == target]
            if same:
                warn(f"{len(same)} video(s) are already {CODEC_NAMES[target]}, skipping them.")
            source = [r for r in source if r["vcodec"] != target]
        else:
            other = "hevc" if target == "h264" else "h264"
            source = [r for r in ok_rows if r["vcodec"] == other]
        # don't re-convert files this script created earlier (name_h264 / name_h265)
        own = [r for r in source
               if r["path"].stem.lower().endswith(("_h264", "_h265"))]
        if own:
            info(f"Skipping {len(own)} file(s) created by an earlier conversion "
                 f"(name ends with _h264/_h265).")
            source = [r for r in source if r not in own]
        if not source:
            ok("Nothing to convert.")
            return
        jobs = [(r, dst, target)
                for r, dst in plan_outputs(source, rows, ffprobe, args, args.to, target)]
        run_conversion(jobs, ffmpeg, args)
        return

    # ---- hints
    if codec_filter:
        return
    already = sum(dst.exists() for _, dst in plan_outputs(to_fix, rows, ffprobe, args, None, "h264"))
    if already:
        info(f"{already} of them already have a converted copy.")
    if to_fix:
        print(f"\nRun again with {Style.BRIGHT}--fix{Style.RESET_ALL} to turn "
              f"the {len(to_fix)} video(s) into H.264 MP4 (originals are kept).")


if __name__ == "__main__":
    main()
