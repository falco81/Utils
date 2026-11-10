#!/usr/bin/env python3
"""
avi2mp4.py - convert AVI files to MP4 (H.264 + AAC) using ffmpeg.

Usage:
    python avi2mp4.py video.avi
    python avi2mp4.py C:\\Videos
    python avi2mp4.py C:\\Videos -r -o C:\\Videos\\mp4 --crf 20
    python avi2mp4.py            (converts all AVI files in the current folder)

Optional dependency for colored output:
    pip install colorama
"""

import argparse
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


def info(msg: str) -> None:
    print(f"{Fore.CYAN}{msg}{Style.RESET_ALL}")


def ok(msg: str) -> None:
    print(f"{Fore.GREEN}{msg}{Style.RESET_ALL}")


def warn(msg: str) -> None:
    print(f"{Fore.YELLOW}{msg}{Style.RESET_ALL}")


def err(msg: str) -> None:
    print(f"{Fore.RED}{msg}{Style.RESET_ALL}")


# ---------------------------------------------------------------- ffmpeg

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


def find_ffprobe(ffmpeg: str) -> str | None:
    p = Path(ffmpeg)
    candidate = p.with_name(p.name.replace("ffmpeg", "ffprobe"))
    if candidate.is_file():
        return str(candidate)
    return shutil.which("ffprobe")


def get_duration(ffprobe: str | None, src: Path) -> float | None:
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(src)],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        value = float(out)
        return value if value > 0 else None
    except (ValueError, subprocess.SubprocessError, OSError):
        return None


def collect_files(inputs: list[Path], recursive: bool) -> list[Path]:
    files = []
    for p in inputs:
        if p.is_file():
            if p.suffix.lower() == ".avi":
                files.append(p)
            else:
                warn(f"Skipping (not an .avi file): {p}")
        elif p.is_dir():
            pattern = "**/*" if recursive else "*"
            files += sorted(f for f in p.glob(pattern)
                            if f.is_file() and f.suffix.lower() == ".avi")
        else:
            warn(f"Not found: {p}")
    return files


# ---------------------------------------------------------------- progress

def render_progress(done: float, duration: float | None, started: float,
                    speed: str, size: int) -> tuple[str, int]:
    """Return (colored line, visible length). Optional fields are dropped
    so the line never wraps in a narrow console window."""
    elapsed = time.monotonic() - started
    width = shutil.get_terminal_size((80, 20)).columns - 1

    # (plain text, colored text) - in order of importance
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
    else:  # unknown length -> no percentage
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
            crf: int, preset: str, audio_bitrate: str, overwrite: bool) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-hide_banner", "-loglevel", "error", "-nostats",
        "-progress", "pipe:1",                     # machine-readable progress on stdout
        "-y" if overwrite else "-n",
        "-i", str(src),
        "-map", "0:v:0", "-map", "0:a?",           # first video + all audio tracks
        "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
        "-pix_fmt", "yuv420p",                      # player compatibility
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", # H.264 needs even dimensions
        "-c:a", "aac", "-b:a", audio_bitrate,
        "-movflags", "+faststart",                  # fast start for streaming
        str(dst),
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True,
                            encoding="utf-8", errors="replace", bufsize=1)

    # read stderr in a background thread so the pipe never blocks ffmpeg
    stderr_lines: list[str] = []
    t = threading.Thread(target=lambda: stderr_lines.extend(proc.stderr), daemon=True)
    t.start()

    started = time.monotonic()
    done, speed, size = 0.0, "?", 0
    last_len = 0
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
                pad = max(last_len - vis_len, 0)
                sys.stdout.write("\r" + line + " " * pad)
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
        _remove(dst)  # don't leave a broken file behind
        return False
    return True


def _remove(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description="Convert AVI to MP4 (H.264 + AAC) using ffmpeg.")
    ap.add_argument("inputs", nargs="*", type=Path, default=[Path(".")],
                    help="AVI files or folders (default: current folder)")
    ap.add_argument("-o", "--output", type=Path,
                    help="output folder (default: next to the source file)")
    ap.add_argument("-r", "--recursive", action="store_true",
                    help="include subfolders")
    ap.add_argument("--crf", type=int, default=23,
                    help="quality 0-51, lower = better and bigger (default 23, 18 = near lossless)")
    ap.add_argument("--preset", default="medium",
                    choices=["ultrafast", "superfast", "veryfast", "faster", "fast",
                             "medium", "slow", "slower", "veryslow"],
                    help="encoding speed (default medium; slower = smaller file)")
    ap.add_argument("--audio-bitrate", default="192k", help="audio bitrate (default 192k)")
    ap.add_argument("--overwrite", action="store_true",
                    help="overwrite existing MP4 files (otherwise they are skipped)")
    ap.add_argument("--delete-source", action="store_true",
                    help="delete the original AVI after a successful conversion")
    ap.add_argument("--ffmpeg", help=f"path to ffmpeg.exe (default {DEFAULT_FFMPEG})")
    args = ap.parse_args()

    ffmpeg = find_ffmpeg(args.ffmpeg)
    ffprobe = find_ffprobe(ffmpeg)
    files = collect_files(args.inputs, args.recursive)
    if not files:
        err("No .avi files found.")
        sys.exit(1)

    info(f"ffmpeg : {ffmpeg}")
    info(f"ffprobe: {ffprobe or 'not found (no percentage / ETA)'}")
    info(f"Files to convert: {len(files)}\n")

    converted, skipped, failed = 0, 0, []
    batch_start = time.monotonic()
    try:
        for i, src in enumerate(files, 1):
            if args.output:
                # keep subfolder structure when converting a folder
                base = next((p for p in args.inputs
                             if p.is_dir() and src.resolve().is_relative_to(p.resolve())), None)
                rel = (src.resolve().relative_to(base.resolve()).with_suffix(".mp4")
                       if base else Path(src.stem + ".mp4"))
                dst = args.output / rel
            else:
                dst = src.with_suffix(".mp4")

            header = f"{Style.BRIGHT}[{i}/{len(files)}]{Style.RESET_ALL}"
            if dst.exists() and not args.overwrite:
                print(f"{header} {Fore.YELLOW}Skipped, already exists: {dst}{Style.RESET_ALL}")
                skipped += 1
                continue

            duration = get_duration(ffprobe, src)
            print(f"{header} {src}  ->  {Fore.CYAN}{dst}{Style.RESET_ALL}"
                  f"  ({fmt_time(duration)})")

            file_start = time.monotonic()
            if convert(ffmpeg, src, dst, duration, args.crf, args.preset,
                       args.audio_bitrate, args.overwrite):
                converted += 1
                in_size, out_size = src.stat().st_size, dst.stat().st_size
                ok(f"   Done in {fmt_time(time.monotonic() - file_start)}  "
                   f"{fmt_size(in_size)} -> {fmt_size(out_size)} "
                   f"({out_size / in_size * 100:.0f}%)")
                if args.delete_source:
                    src.unlink()
                    warn("   Source AVI deleted.")
            else:
                failed.append(src)
                err("   Conversion FAILED.")
            print()
    except KeyboardInterrupt:
        warn("\nAborted.")
        sys.exit(130)

    total = fmt_time(time.monotonic() - batch_start)
    summary = (f"Finished in {total}: {converted} converted, "
               f"{skipped} skipped, {len(failed)} failed.")
    (err if failed else ok)(summary)
    for f in failed:
        err(f"  failed: {f}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
