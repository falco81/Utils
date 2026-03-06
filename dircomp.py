#!/usr/bin/env python3
"""
dircomp.py — Compare the contents of two directories using SHA-256 file hashes.

Usage:
    python dircomp.py <dir_A> <dir_B> [--ignore PATTERN] [--output report.txt]

Examples:
    python dircomp.py C:\\backup\\old C:\\backup\\new
    python dircomp.py .\\src .\\dst --ignore "*.pyc" --ignore "__pycache__"
    python dircomp.py D:\\data\\a D:\\data\\b --output diff_report.txt
"""

import os
import sys
import hashlib
import argparse
import fnmatch
from pathlib import Path
from collections import defaultdict
from datetime import datetime


# ── Windows ANSI support ───────────────────────────────────────────────────────

def _enable_windows_ansi():
    """Enable ANSI escape codes on Windows 10 (requires Win10 1511+)."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        return True
    except Exception:
        return False

# Set UTF-8 output on Windows to avoid encoding errors with special characters
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_ANSI_ENABLED = _enable_windows_ansi()


# ── Colours ────────────────────────────────────────────────────────────────────

class C:
    RESET  = "\033[0m"
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    CYAN   = "\033[96m"
    GRAY   = "\033[90m"


def colorize(text: str, color: str) -> str:
    """Wrap text in ANSI colour codes only when the terminal supports it."""
    if _ANSI_ENABLED and sys.stdout.isatty():
        return f"{color}{text}{C.RESET}"
    return text


# ── SHA-256 hash ───────────────────────────────────────────────────────────────

def sha256(path: Path, chunk: int = 1 << 20) -> str:
    """Compute the SHA-256 hash of a file in chunks (memory-friendly)."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                data = f.read(chunk)
                if not data:
                    break
                h.update(data)
        return h.hexdigest()
    except (PermissionError, OSError) as e:
        return f"ERROR:{e}"


# ── Directory indexing ─────────────────────────────────────────────────────────

def index_dir(root: Path, ignore_patterns: list) -> dict:
    """
    Walk a directory and return:
        { "relative/path/file" : { "abs": Path, "size": int, "hash": str|None } }
    """
    index = {}
    root = root.resolve()

    for dirpath, dirnames, filenames in os.walk(root):
        # Filter ignored directories in-place so os.walk skips them entirely
        dirnames[:] = [
            d for d in dirnames
            if not any(fnmatch.fnmatch(d, pat) for pat in ignore_patterns)
        ]

        for fname in filenames:
            if any(fnmatch.fnmatch(fname, pat) for pat in ignore_patterns):
                continue

            abs_path = Path(dirpath) / fname
            rel_path = abs_path.relative_to(root).as_posix()  # forward slashes

            try:
                size = abs_path.stat().st_size
            except OSError:
                size = -1

            index[rel_path] = {
                "abs":  abs_path,
                "size": size,
                "hash": None,   # filled lazily
            }

    return index


def fill_hashes(index: dict, label: str):
    """Compute SHA-256 for every entry in the index with a progress indicator."""
    total = len(index)
    for i, (rel, entry) in enumerate(index.items(), 1):
        print(f"\r  [{label}] Hashing: {i}/{total} ...", end="", flush=True)
        entry["hash"] = sha256(entry["abs"])
    print()


# ── Comparison logic ───────────────────────────────────────────────────────────

def compare(dir_a: Path, dir_b: Path, ignore_patterns: list) -> dict:
    print(colorize(f"\n[A] {dir_a}", C.CYAN))
    print(colorize(f"[B] {dir_b}\n",  C.CYAN))

    print(colorize("Scanning directories ...", C.GRAY))
    idx_a = index_dir(dir_a, ignore_patterns)
    idx_b = index_dir(dir_b, ignore_patterns)

    keys_a = set(idx_a.keys())
    keys_b = set(idx_b.keys())

    only_in_a = sorted(keys_a - keys_b)
    only_in_b = sorted(keys_b - keys_a)
    in_both   = sorted(keys_a & keys_b)

    print(colorize("Computing SHA-256 hashes ...", C.GRAY))
    fill_hashes(idx_a, "A")
    fill_hashes(idx_b, "B")

    # Classify files present in both directories
    identical = []
    different = []
    for rel in in_both:
        if idx_a[rel]["hash"] == idx_b[rel]["hash"]:
            identical.append(rel)
        else:
            different.append(rel)

    # Detect moved/renamed files: same hash, different path
    hash_to_a = defaultdict(list)
    for rel in only_in_a:
        h = idx_a[rel]["hash"]
        if not h.startswith("ERROR"):
            hash_to_a[h].append(rel)

    moved = []           # list of (path_in_A, path_in_B)
    truly_only_b = []
    for rel in only_in_b:
        h = idx_b[rel]["hash"]
        if h and not h.startswith("ERROR") and h in hash_to_a:
            moved.append((hash_to_a[h][0], rel))
        else:
            truly_only_b.append(rel)

    moved_paths_a = {m[0] for m in moved}
    truly_only_a  = [r for r in only_in_a if r not in moved_paths_a]

    return {
        "dir_a":     dir_a,
        "dir_b":     dir_b,
        "idx_a":     idx_a,
        "idx_b":     idx_b,
        "identical": identical,
        "different": different,
        "only_in_a": truly_only_a,
        "only_in_b": truly_only_b,
        "moved":     moved,
    }


# ── Report output ──────────────────────────────────────────────────────────────

def fmt_size(b: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def print_report(result: dict, show_identical: bool, output_path: str = None):
    lines = []

    def w(text="", color=None):
        lines.append(text)
        if output_path is None:
            print(colorize(text, color) if color else text)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    w("=" * 70)
    w(f"  DIRECTORY COMPARISON  --  {ts}")
    w("=" * 70)
    w(f"  A: {result['dir_a']}")
    w(f"  B: {result['dir_b']}")
    w()

    # Summary
    w("-- SUMMARY " + "-" * 59)
    w(f"  Identical files          : {len(result['identical'])}",
      color=C.GREEN if result["identical"] else None)
    w(f"  Content differs          : {len(result['different'])}",
      color=C.RED if result["different"] else None)
    w(f"  Only in A (missing in B) : {len(result['only_in_a'])}",
      color=C.YELLOW if result["only_in_a"] else None)
    w(f"  Only in B (missing in A) : {len(result['only_in_b'])}",
      color=C.YELLOW if result["only_in_b"] else None)
    w(f"  Moved / renamed          : {len(result['moved'])}",
      color=C.CYAN if result["moved"] else None)
    w()

    # Files with different content
    if result["different"]:
        w("-- CONTENT DIFFERS " + "-" * 51, color=C.RED)
        for rel in result["different"]:
            ea = result["idx_a"][rel]
            eb = result["idx_b"][rel]
            size_diff = eb["size"] - ea["size"]
            sign = "+" if size_diff >= 0 else ""
            w(f"  != {rel}")
            w(f"       A: {fmt_size(ea['size'])}  sha256:{ea['hash'][:16]}...")
            w(f"       B: {fmt_size(eb['size'])}  sha256:{eb['hash'][:16]}...  ({sign}{fmt_size(size_diff)})")
        w()

    # Only in A
    if result["only_in_a"]:
        w("-- ONLY IN A (missing in B) " + "-" * 43, color=C.YELLOW)
        for rel in result["only_in_a"]:
            e = result["idx_a"][rel]
            w(f"  -  {rel}  ({fmt_size(e['size'])})")
        w()

    # Only in B
    if result["only_in_b"]:
        w("-- ONLY IN B (missing in A) " + "-" * 43, color=C.YELLOW)
        for rel in result["only_in_b"]:
            e = result["idx_b"][rel]
            w(f"  +  {rel}  ({fmt_size(e['size'])})")
        w()

    # Moved / renamed
    if result["moved"]:
        w("-- MOVED / RENAMED " + "-" * 51, color=C.CYAN)
        for pa, pb in result["moved"]:
            w(f"  -> {pa}")
            w(f"       -> {pb}")
        w()

    # Identical (optional)
    if show_identical and result["identical"]:
        w("-- IDENTICAL " + "-" * 57, color=C.GREEN)
        for rel in result["identical"]:
            e = result["idx_a"][rel]
            w(f"  ok {rel}  ({fmt_size(e['size'])})")
        w()

    w("=" * 70)

    # Write report file
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(colorize(f"\nReport saved to: {output_path}", C.CYAN))


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Compare two directories by SHA-256 file hashes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("dir_a", help="First directory (A)")
    parser.add_argument("dir_b", help="Second directory (B)")
    parser.add_argument(
        "--ignore", "-i", metavar="PATTERN", action="append", default=[],
        help="Glob pattern to ignore files/dirs (can be repeated)",
    )
    parser.add_argument(
        "--show-identical", "-s", action="store_true",
        help="Also list files that are identical",
    )
    parser.add_argument(
        "--output", "-o", metavar="FILE",
        help="Save the report to a text file",
    )

    args = parser.parse_args()

    dir_a = Path(args.dir_a)
    dir_b = Path(args.dir_b)

    for d, name in [(dir_a, "A"), (dir_b, "B")]:
        if not d.is_dir():
            print(colorize(f"ERROR: '{d}' is not a valid directory ({name}).", C.RED),
                  file=sys.stderr)
            sys.exit(1)

    result = compare(dir_a, dir_b, args.ignore)
    print_report(result, args.show_identical, args.output)

    # Exit code: 0 = identical, 1 = differences found
    has_diff = any([result["different"], result["only_in_a"],
                    result["only_in_b"], result["moved"]])
    sys.exit(1 if has_diff else 0)


if __name__ == "__main__":
    main()
