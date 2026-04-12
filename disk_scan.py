#!/usr/bin/env python3
"""
disk_scan.py  -  USB disk duplicate finder

Usage:
  python disk_scan.py scan <path>    -- scan a disk and save fingerprint file
  python disk_scan.py compare        -- compare all scan files and find duplicates

Examples:
  python disk_scan.py scan J: --label red_flash
  python disk_scan.py scan J: --label blue_flash
  python disk_scan.py compare
"""

import os
import sys
import json
import shutil
import hashlib
import argparse
import datetime
from pathlib import Path
from collections import defaultdict

# -------------------------------------------------------------------
#  Windows 10 CLI: force UTF-8 output, replace unencodable chars
# -------------------------------------------------------------------
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# -------------------------------------------------------------------
#  CONFIGURATION
# -------------------------------------------------------------------
SCAN_FILE_PREFIX      = "disk_scan_"
SCAN_FILE_EXT         = ".json"
REPORT_HTML           = "duplicates_report.html"

QUICK_HASH_THRESHOLD  = 512 * 1024   # files larger than this -> partial hash
CHUNK_SIZE            = 256 * 1024   # bytes read at each of the 3 positions
MIN_FILE_SIZE         = 1            # skip empty files


# -------------------------------------------------------------------
#  HELPERS
# -------------------------------------------------------------------
def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def get_label(scan: dict) -> str:
    return scan.get("label") or scan["disk_label"]


# -------------------------------------------------------------------
#  HASHING
# -------------------------------------------------------------------
def hash_file(path: str) -> str | None:
    """
    Fast MD5 fingerprint.
    Small files  (<= QUICK_HASH_THRESHOLD) : full read.
    Large files  (>  QUICK_HASH_THRESHOLD) : read first + middle + last chunk only.
    Returns hex string or None on error.
    """
    try:
        size = os.path.getsize(path)
        md5  = hashlib.md5()
        with open(path, "rb") as f:
            if size <= QUICK_HASH_THRESHOLD:
                for chunk in iter(lambda: f.read(65536), b""):
                    md5.update(chunk)
            else:
                md5.update(f.read(CHUNK_SIZE))
                f.seek(size // 2)
                md5.update(f.read(CHUNK_SIZE))
                f.seek(max(0, size - CHUNK_SIZE))
                md5.update(f.read(CHUNK_SIZE))
        return md5.hexdigest()
    except (PermissionError, OSError, IOError):
        return None


# -------------------------------------------------------------------
#  SCAN
# -------------------------------------------------------------------
def cmd_scan(disk_label: str, custom_label: str | None = None) -> None:
    root = Path(disk_label).resolve()
    if not root.exists():
        print(f"[ERROR] Path does not exist: {root}")
        sys.exit(1)

    if not custom_label:
        default = (
            str(disk_label).replace(":", "").replace("\\", "_")
            .replace("/", "_").strip("_") or "disk"
        )
        try:
            answer = input(
                f"[SCAN] Enter a label for this disk (Enter = '{default}'): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        custom_label = answer if answer else default

    safe_label = (
        custom_label.replace(":", "").replace("\\", "_")
        .replace("/", "_").replace(" ", "_").strip("_") or "disk"
    )

    out_file = Path.cwd() / f"{SCAN_FILE_PREFIX}{safe_label}{SCAN_FILE_EXT}"

    if out_file.exists():
        print(f"[!] File '{out_file.name}' already exists and will be overwritten.")
        try:
            confirm = input("    Continue? (y/N): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            confirm = "n"
        if confirm not in ("y", "yes"):
            print("[SCAN] Cancelled. Use --label <name> to choose a different name.")
            sys.exit(0)

    # -- Disk space info -------------------------------------------
    try:
        usage       = shutil.disk_usage(root)
        free_bytes  = usage.free
        total_bytes = usage.total
        used_bytes  = usage.used
    except OSError:
        free_bytes = total_bytes = used_bytes = 0

    print(f"[SCAN] Path     : {root}")
    print(f"[SCAN] Label    : {custom_label}")
    print(f"[SCAN] Capacity : {human_size(total_bytes)}  "
          f"Free: {human_size(free_bytes)}  "
          f"Used: {human_size(used_bytes)}")
    print(f"[SCAN] Output   : {out_file}")
    print("[SCAN] Scanning ... (may take several minutes on a 2 TB drive)\n")

    files_info = []
    total      = 0
    errors     = 0

    try:
        all_paths = []
        for dirpath, _dirs, filenames in os.walk(root, onerror=lambda e: None):
            for fname in filenames:
                all_paths.append(os.path.join(dirpath, fname))

        total_count = len(all_paths)
        print(f"  Files found: {total_count:,}")

        for idx, fpath in enumerate(all_paths, 1):
            if idx % 500 == 0 or idx == total_count:
                pct = idx / total_count * 100
                print(f"  {idx:>8,} / {total_count:,}  ({pct:.1f}%) ...", end="\r", flush=True)

            try:
                stat  = os.stat(fpath)
                size  = stat.st_size
                if size < MIN_FILE_SIZE:
                    continue
                mtime = datetime.datetime.fromtimestamp(stat.st_mtime).isoformat()
                h     = hash_file(fpath)
                if h is None:
                    errors += 1
                    continue
                files_info.append({"path": fpath, "size": size, "mtime": mtime, "hash": h})
                total += 1
            except (PermissionError, OSError):
                errors += 1

    except KeyboardInterrupt:
        print("\n[!] Interrupted by user -- saving data so far ...")

    print()

    metadata = {
        "disk_label":  disk_label,
        "label":       custom_label,
        "root_path":   str(root),
        "scanned_at":  datetime.datetime.now().isoformat(),
        "free_bytes":  free_bytes,
        "total_bytes": total_bytes,
        "used_bytes":  used_bytes,
        "total_files": total,
        "errors":      errors,
        "files":       files_info,
    }

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    print(f"[SCAN] Done. Scanned: {total:,} files, errors: {errors}")
    print(f"[SCAN] Saved: {out_file}")


# -------------------------------------------------------------------
#  COMPARE -- load and deduplicate
# -------------------------------------------------------------------
def load_scan_files(cwd: Path) -> list[dict]:
    scans = []
    for p in sorted(cwd.glob(f"{SCAN_FILE_PREFIX}*{SCAN_FILE_EXT}")):
        print(f"  Loading: {p.name}")
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["_scan_file"]  = str(p)
        data["_safe_label"] = p.stem[len(SCAN_FILE_PREFIX):]
        scans.append(data)
    return scans


def find_duplicates(scans: list[dict]) -> dict:
    by_hash: dict[str, list[dict]] = defaultdict(list)
    for scan in scans:
        label = get_label(scan)
        for fi in scan["files"]:
            by_hash[fi["hash"]].append({
                "disk":  label,
                "path":  fi["path"],
                "size":  fi["size"],
                "mtime": fi["mtime"],
            })
    return {h: files for h, files in by_hash.items() if len(files) > 1}


# -------------------------------------------------------------------
#  DELETION PLAN -- maximize free space on one disk
# -------------------------------------------------------------------
def is_preferred(path: str) -> bool:
    """
    Returns True if the file resides in a folder whose name starts with '_',
    at any level of the directory tree.
    Preferred files are kept; non-preferred duplicates are deleted first.
    Works with both Windows (backslash) and Unix (slash) paths.
    """
    parts = path.replace("\\", "/").split("/")
    return any(part.startswith("_") for part in parts[:-1] if part)


def pick_survivor(candidates: list[dict], disk_free: dict) -> dict:
    """
    Choose the file to KEEP from a list of candidates:
      1. Prefer files inside a folder starting with '_'.
      2. Among equally preferred, keep the one on the disk with least free space
         (that disk needs the file most; we free space on disks that have more room).
    """
    preferred = [f for f in candidates if is_preferred(f["path"])]
    pool = preferred if preferred else candidates
    return min(pool, key=lambda f: disk_free.get(f["disk"], 0))


def plan_deletions(scans: list[dict], duplicates: dict) -> dict:
    """
    Decide which copy of each duplicate survives (exactly 1 per group).

    Goal: maximise free space gained on one specific disk (the 'target').
    Priority rule: files in folders starting with '_' are always preferred
    for survival over non-preferred duplicates, even when that conflicts
    with the space-optimisation goal.

    Returns a dict with:
      target_disk      -- label of the disk that gains the most space
      freed_target     -- bytes freed on the target disk
      freed_total      -- bytes freed across all disks
      per_disk         -- {label: [paths to delete]}
      disk_free_before -- {label: bytes}
      disk_free_after  -- {label: bytes}
    """
    disk_free = {get_label(s): s.get("free_bytes", 0) for s in scans}
    labels    = list(disk_free.keys())

    best_target = None
    best_freed  = -1
    best_plan   = {}

    for target in labels:
        plan  = defaultdict(list)
        freed = 0

        for h, files in duplicates.items():
            on_target = [f for f in files if f["disk"] == target]
            elsewhere = [f for f in files if f["disk"] != target]

            preferred_elsewhere = [f for f in elsewhere  if is_preferred(f["path"])]
            preferred_on_target = [f for f in on_target  if is_preferred(f["path"])]

            if on_target and elsewhere:
                if preferred_elsewhere:
                    # Keep preferred copy elsewhere, delete all copies on target
                    survivor = pick_survivor(preferred_elsewhere, disk_free)
                    for f in on_target:
                        plan[target].append(f["path"])
                        freed += f["size"]

                elif preferred_on_target:
                    # Only preferred copy is on the target -- do NOT delete it
                    # Delete copies elsewhere instead (target gains nothing here)
                    survivor = pick_survivor(preferred_on_target, disk_free)
                    for f in elsewhere:
                        plan[f["disk"]].append(f["path"])
                    # Delete surplus preferred copies on target (keep one)
                    for f in on_target:
                        if f["path"] != survivor["path"]:
                            plan[target].append(f["path"])
                            freed += f["size"]

                else:
                    # No preferred copies -- standard space-optimisation logic
                    survivor = pick_survivor(elsewhere, disk_free)
                    for f in on_target:
                        plan[target].append(f["path"])
                        freed += f["size"]

                # Remove surplus copies elsewhere (keep only the survivor)
                for f in elsewhere:
                    if f["path"] != survivor["path"]:
                        plan[f["disk"]].append(f["path"])

            elif on_target and not elsewhere:
                # Only on target disk -- keep one, delete the rest
                survivor = pick_survivor(on_target, disk_free)
                for f in on_target:
                    if f["path"] != survivor["path"]:
                        plan[target].append(f["path"])
                        freed += f["size"]

            else:
                # No copies on target -- deduplicate among other disks
                survivor = pick_survivor(files, disk_free)
                for f in files:
                    if f["path"] != survivor["path"]:
                        plan[f["disk"]].append(f["path"])

        if freed > best_freed:
            best_freed  = freed
            best_target = target
            best_plan   = {k: list(set(v)) for k, v in plan.items()}

    # Build size lookup for reporting
    size_lookup = {}
    for files in duplicates.values():
        for f in files:
            size_lookup[f["path"]] = f["size"]

    freed_total = sum(
        size_lookup.get(p, 0)
        for paths in best_plan.values()
        for p in paths
    )
    freed_on_target = sum(size_lookup.get(p, 0) for p in best_plan.get(best_target, []))

    free_after = {
        lbl: disk_free.get(lbl, 0) + sum(
            size_lookup.get(p, 0) for p in best_plan.get(lbl, [])
        )
        for lbl in labels
    }

    return {
        "target_disk":      best_target,
        "freed_target":     freed_on_target,
        "freed_total":      freed_total,
        "per_disk":         best_plan,
        "disk_free_before": dict(disk_free),
        "disk_free_after":  free_after,
    }


# -------------------------------------------------------------------
#  CMD SCRIPT GENERATION
# -------------------------------------------------------------------
def generate_cmd_files(scans: list[dict], plan: dict, cwd: Path) -> list[Path]:
    """Generate one .cmd file per disk containing DEL commands."""
    generated = []
    per_disk  = plan.get("per_disk", {})

    for scan in scans:
        label      = get_label(scan)
        safe_label = scan.get("_safe_label", label)
        cmd_path   = cwd / f"{SCAN_FILE_PREFIX}{safe_label}.cmd"
        paths      = per_disk.get(label, [])

        before = scan.get("free_bytes", 0)
        after  = plan.get("disk_free_after", {}).get(label, before)
        saved  = after - before

        lines = [
            "@echo off",
            f"REM  Auto-generated by disk_scan.py",
            f"REM  Disk    : {label}  ({scan['disk_label']})",
            f"REM  Scanned : {scan.get('scanned_at', '')[:16]}",
            f"REM  Files   : {len(paths)} to delete",
            f"REM  Free before : {human_size(before)}",
            f"REM  Free after  : {human_size(after)}",
            f"REM  Gain        : {human_size(saved)}",
            "REM",
            "REM  WARNING: Review the file list before running!",
            "REM  This script permanently deletes files.",
            "",
        ]

        if not paths:
            lines += [
                "REM  No files to delete on this disk.",
                'echo No duplicate files to delete on this disk.',
                "pause",
            ]
        else:
            lines += [
                f'echo Deleting {len(paths)} duplicate file(s) on disk "{label}" ...',
                "echo.",
            ]
            for p in sorted(paths):
                escaped = p.replace("%", "%%")
                lines.append(f'echo   {escaped}')
                lines.append(f'del /f /q "{escaped}"')
                lines.append("")
            lines += [
                "echo.",
                f'echo Done. {len(paths)} file(s) deleted.',
                "pause",
            ]

        with open(cmd_path, "w", encoding="cp1250", errors="replace", newline="\r\n") as f:
            f.write("\n".join(lines))

        generated.append(cmd_path)

    return generated


# -------------------------------------------------------------------
#  CLI REPORT  (ASCII-only, Windows 10 cmd.exe compatible)
# -------------------------------------------------------------------
def cli_report(scans: list[dict], duplicates: dict, plan: dict) -> None:
    SEP1 = "-" * 80
    SEP2 = "=" * 80

    size_lookup = {}
    for files in duplicates.values():
        for f in files:
            size_lookup[f["path"]] = f["size"]

    wasted_bytes     = sum(f["size"] * (len(files) - 1)
                           for files in duplicates.values() for f in files[:1])
    total_dup_groups = len(duplicates)
    total_dup_files  = sum(len(v) for v in duplicates.values())
    delete_set       = {p for paths in plan["per_disk"].values() for p in paths}

    print()
    print(SEP2)
    print("  DUPLICATE FILE REPORT")
    print(SEP2)
    print(f"  {'Disk':<22} {'Label':<20} {'Files':>8}  {'Capacity':>10}  {'Free':>10}  Scanned")
    print(f"  {SEP1}")
    for s in scans:
        lbl = get_label(s)
        print(
            f"  {s['disk_label']:<22} {lbl:<20} {s['total_files']:>8,}"
            f"  {human_size(s.get('total_bytes', 0)):>10}"
            f"  {human_size(s.get('free_bytes', 0)):>10}"
            f"  {s['scanned_at'][:16]}"
        )
    print(f"  {SEP1}")
    print(f"  Duplicate groups : {total_dup_groups:,}")
    print(f"  Duplicate files  : {total_dup_files:,}")
    print(f"  Wasted space     : {human_size(wasted_bytes)}")
    print(SEP2)

    if not duplicates:
        print("  [OK] No duplicates found.")
        print(SEP2)
        return

    sorted_dups = sorted(duplicates.items(), key=lambda x: x[1][0]["size"], reverse=True)

    for i, (h, files) in enumerate(sorted_dups, 1):
        size   = files[0]["size"]
        wasted = size * (len(files) - 1)
        print(f"\n  [{i:>4}]  Hash: {h}   Size: {human_size(size)}   "
              f"Copies: {len(files)}   Wasted: {human_size(wasted)}")
        print(f"  {'-' * 76}")
        for fi in files:
            fp      = Path(fi["path"])
            pref    = " [PREFERRED]" if is_preferred(fi["path"]) else ""
            action  = "[DELETE]" if fi["path"] in delete_set else "[KEEP]  "
            disk_tag = f"[{fi['disk']}]"
            print(f"    {disk_tag:18s}  {action}  File : {fp.name}{pref}")
            print(f"    {'':18s}           Dir  : {str(fp.parent)}")
            print(f"    {'':18s}           Mod  : {fi['mtime'][:19]}   {human_size(fi['size'])}")

    # -- Deletion plan summary -------------------------------------
    print()
    print(SEP2)
    print("  DELETION PLAN  --  Free Space Optimisation")
    print(SEP2)
    target = plan["target_disk"]
    print(f"  Target disk (maximum gain) : {target}")
    print(f"  Freed on target disk       : {human_size(plan['freed_target'])}")
    print(f"  Freed across all disks     : {human_size(plan['freed_total'])}")
    print()
    print(f"  {'Disk':<22} {'Before':>10}  {'After':>10}  {'Gain':>10}  {'Files to delete':>16}")
    print(f"  {SEP1}")
    for s in scans:
        lbl       = get_label(s)
        before    = s.get("free_bytes", 0)
        after     = plan["disk_free_after"].get(lbl, before)
        saved     = after - before
        del_count = len(plan["per_disk"].get(lbl, []))
        marker    = "  <-- TARGET" if lbl == target else ""
        print(f"  {lbl:<22} {human_size(before):>10}  {human_size(after):>10}  "
              f"{human_size(saved):>10}  {del_count:>16}{marker}")
    print(SEP2)
    print("  CMD scripts generated -- run each on its respective disk to delete files.")
    print("  NOTE: Files in folders starting with '_' are always kept (preferred).")
    print(SEP2)


# -------------------------------------------------------------------
#  HTML REPORT
# -------------------------------------------------------------------
def html_report(scans: list[dict], duplicates: dict, plan: dict, out_path: Path) -> None:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    size_lookup = {}
    for files in duplicates.values():
        for f in files:
            size_lookup[f["path"]] = f["size"]

    delete_set       = {p for paths in plan["per_disk"].values() for p in paths}
    wasted_bytes     = sum(f["size"] * (len(files) - 1)
                           for files in duplicates.values() for f in files[:1])
    total_dup_groups = len(duplicates)
    total_dup_files  = sum(len(v) for v in duplicates.values())
    sorted_dups      = sorted(duplicates.items(), key=lambda x: x[1][0]["size"], reverse=True)

    disk_colors = [
        "#3b82f6","#10b981","#f59e0b","#ef4444",
        "#8b5cf6","#ec4899","#06b6d4","#84cc16",
    ]
    disk_color_map = {get_label(s): disk_colors[i % len(disk_colors)] for i, s in enumerate(scans)}

    # -- Duplicate rows -------------------------------------------
    rows_html = ""
    for i, (h, files) in enumerate(sorted_dups, 1):
        size   = files[0]["size"]
        wasted = size * (len(files) - 1)
        copies = len(files)
        file_rows = ""
        for fi in files:
            color  = disk_color_map.get(fi["disk"], "#6b7280")
            fp     = Path(fi["path"])
            fname  = fp.name
            fdir   = str(fp.parent)
            action = fi["path"] in delete_set
            pref   = is_preferred(fi["path"])
            row_cls = "file-row to-delete" if action else "file-row to-keep"
            action_badge = '<span class="action-del">DELETE</span>' if action else '<span class="action-keep">KEEP</span>'
            pref_badge   = '<span class="pref-badge">PREFERRED</span>' if pref else ""
            file_rows += f"""
            <tr class="{row_cls}">
              <td><span class="disk-badge" style="background:{color}">{fi['disk']}</span></td>
              <td class="fname-cell">{fname} {pref_badge}</td>
              <td class="path-cell muted">{fdir}</td>
              <td class="right nowrap">{fi['mtime'][:19]}</td>
              <td class="center">{action_badge}</td>
            </tr>"""

        rows_html += f"""
        <tr class="group-header" onclick="toggle({i})">
          <td class="idx">#{i}</td>
          <td class="hash-cell"><code>{h[:16]}...</code></td>
          <td class="right">{human_size(size)}</td>
          <td class="center copies-badge">{copies}x</td>
          <td class="right wasted">{human_size(wasted)}</td>
          <td class="center arrow" id="arr{i}">v</td>
        </tr>
        <tr class="detail-row" id="detail{i}">
          <td colspan="6">
            <table class="inner-table">
              <tr><th>Disk</th><th>Filename</th><th>Directory</th><th>Modified</th><th>Action</th></tr>
              {file_rows}
            </table>
          </td>
        </tr>"""

    # -- Disk summary cards ---------------------------------------
    scans_summary = ""
    for s in scans:
        lbl   = get_label(s)
        color = disk_color_map.get(lbl, "#6b7280")
        total = s.get("total_bytes", 0)
        free  = s.get("free_bytes", 0)
        used  = total - free if total else 0
        pct   = (used / total * 100) if total else 0
        scans_summary += f"""
        <div class="disk-card">
          <div class="disk-dot" style="background:{color}"></div>
          <div style="flex:1">
            <div class="disk-name">{lbl} <span class="disk-raw">({s['disk_label']})</span></div>
            <div class="disk-meta">{s['total_files']:,} files &nbsp;&middot;&nbsp; scanned: {s['scanned_at'][:16]}</div>
            <div class="disk-bar-wrap">
              <div class="disk-bar" style="width:{pct:.0f}%;background:{color}88"></div>
            </div>
            <div class="disk-meta">{human_size(free)} free / {human_size(total)} total</div>
          </div>
        </div>"""

    # -- Plan rows ------------------------------------------------
    target    = plan["target_disk"]
    plan_rows = ""
    for s in scans:
        lbl       = get_label(s)
        color     = disk_color_map.get(lbl, "#6b7280")
        before    = s.get("free_bytes", 0)
        after     = plan["disk_free_after"].get(lbl, before)
        saved     = after - before
        del_count = len(plan["per_disk"].get(lbl, []))
        is_tgt    = lbl == target
        row_style = ' style="background:#1a2f1a"' if is_tgt else ""
        star      = ' <span class="target-star">&#9733; TARGET</span>' if is_tgt else ""
        plan_rows += f"""
        <tr{row_style}>
          <td><span class="disk-badge" style="background:{color}">{lbl}</span>{star}</td>
          <td class="right">{human_size(before)}</td>
          <td class="right green">{human_size(after)}</td>
          <td class="right {'wasted' if saved else ''}">{'+' if saved else ''}{human_size(saved)}</td>
          <td class="center">{del_count:,}</td>
        </tr>"""

    # -- CMD file list -------------------------------------------
    cmd_list = ""
    for s in scans:
        lbl       = get_label(s)
        safe_lbl  = s.get("_safe_label", lbl)
        del_count = len(plan["per_disk"].get(lbl, []))
        color     = disk_color_map.get(lbl, "#6b7280")
        cmd_list += f"""
        <div class="cmd-item">
          <span class="disk-badge" style="background:{color}">{lbl}</span>
          <code>disk_scan_{safe_lbl}.cmd</code>
          <span class="muted"> &mdash; {del_count} file(s) to delete</span>
        </div>"""

    no_dup_msg = ""
    if not duplicates:
        no_dup_msg = '<p class="no-dup">[OK] No duplicates found!</p>'

    dup_section = "" if not duplicates else f"""
<h2>Duplicate Files</h2>
<div class="search-bar">
  <input type="text" id="search" placeholder="Filter by path or disk ..."
         oninput="filterRows()">
</div>
<table class="main-table">
  <thead><tr>
    <th>#</th><th>Hash (short)</th>
    <th style="text-align:right">Size</th>
    <th style="text-align:center">Copies</th>
    <th style="text-align:right">Wasted</th>
    <th></th>
  </tr></thead>
  <tbody id="tbody">{rows_html}</tbody>
</table>

<h2>Deletion Plan</h2>
<p style="color:var(--muted);font-size:13px;margin-bottom:12px">
  Target disk <strong style="color:var(--green)">{plan['target_disk']}</strong> gains
  <strong style="color:var(--green)">{human_size(plan['freed_target'])}</strong> of free space.
  Total savings across all disks:
  <strong style="color:var(--green)">{human_size(plan['freed_total'])}</strong>.
  Files in folders starting with <code>_</code> are always kept.
</p>
<table class="plan-table">
  <thead><tr>
    <th>Disk</th>
    <th style="text-align:right">Free before</th>
    <th style="text-align:right">Free after</th>
    <th style="text-align:right">Gain</th>
    <th style="text-align:center">Files to delete</th>
  </tr></thead>
  <tbody>{plan_rows}</tbody>
</table>

<h2>Generated CMD Scripts</h2>
<div class="cmd-box">{cmd_list}</div>
<div class="warning-box">
  <strong>[!] WARNING:</strong> Review the file list before running any CMD script.
  Deletion is permanent. Make sure you have a backup if needed.
</div>
"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Duplicate Report {now}</title>
<style>
  :root {{
    --bg:#0f172a;--surface:#1e293b;--surface2:#273548;
    --border:#334155;--text:#e2e8f0;--muted:#94a3b8;
    --accent:#38bdf8;--warn:#fb923c;--green:#4ade80;--red:#f87171;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);padding:24px;font-size:14px}}
  h1{{font-size:22px;font-weight:700;color:var(--accent);margin-bottom:4px}}
  h2{{font-size:16px;font-weight:600;color:var(--accent);margin:28px 0 12px}}
  .subtitle{{color:var(--muted);margin-bottom:24px;font-size:13px}}
  .stats{{display:flex;gap:16px;margin-bottom:24px;flex-wrap:wrap}}
  .stat-card{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 22px;min-width:160px}}
  .stat-val{{font-size:28px;font-weight:800;color:var(--accent)}}
  .stat-lbl{{font-size:12px;color:var(--muted);margin-top:2px}}
  .disks{{display:flex;gap:12px;margin-bottom:28px;flex-wrap:wrap}}
  .disk-card{{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:10px 16px;display:flex;align-items:flex-start;gap:10px;min-width:220px}}
  .disk-dot{{width:12px;height:12px;border-radius:50%;flex-shrink:0;margin-top:4px}}
  .disk-name{{font-weight:600;font-size:13px}}
  .disk-raw{{font-weight:400;color:var(--muted);font-size:11px}}
  .disk-meta{{font-size:11px;color:var(--muted);margin-top:2px}}
  .disk-bar-wrap{{height:4px;background:#334155;border-radius:2px;margin:6px 0 4px;width:100%}}
  .disk-bar{{height:4px;border-radius:2px}}
  .search-bar{{margin-bottom:16px}}
  .search-bar input{{width:100%;max-width:420px;padding:8px 14px;background:var(--surface);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:14px;outline:none}}
  .search-bar input:focus{{border-color:var(--accent)}}
  table{{width:100%;border-collapse:collapse}}
  .main-table{{background:var(--surface);border-radius:10px;overflow:hidden;border:1px solid var(--border)}}
  .main-table thead th{{background:var(--surface2);padding:10px 14px;text-align:left;font-weight:600;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid var(--border)}}
  .group-header{{cursor:pointer;transition:background .15s}}
  .group-header:hover{{background:var(--surface2)}}
  .group-header td{{padding:11px 14px;border-bottom:1px solid var(--border)}}
  .idx{{color:var(--muted);font-size:12px;width:40px}}
  .hash-cell code{{font-family:'Cascadia Code','Consolas',monospace;font-size:12px;color:var(--accent)}}
  .right{{text-align:right;white-space:nowrap}}
  .center{{text-align:center}}
  .wasted{{color:var(--warn);font-weight:600}}
  .green{{color:var(--green);font-weight:600}}
  .copies-badge{{font-weight:700;color:var(--red)}}
  .arrow{{color:var(--muted);width:30px}}
  .detail-row{{display:none;background:#111b2b}}
  .detail-row.open{{display:table-row}}
  .detail-row>td{{padding:0 20px 14px 40px;border-bottom:1px solid var(--border)}}
  .inner-table{{width:100%;border-collapse:collapse;margin-top:10px}}
  .inner-table th{{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;padding:4px 8px;text-align:left}}
  .file-row td{{padding:6px 8px;border-bottom:1px solid #1e2d40;vertical-align:top}}
  .to-delete{{background:#2d1515}}
  .to-keep{{background:#132013}}
  .fname-cell{{font-family:'Cascadia Code','Consolas',monospace;font-size:13px;font-weight:600;color:var(--text);word-break:break-all}}
  .path-cell{{font-family:'Cascadia Code','Consolas',monospace;font-size:11px;word-break:break-all}}
  .path-cell.muted{{color:var(--muted)}}
  .nowrap{{white-space:nowrap}}
  .disk-badge{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;color:#fff;white-space:nowrap}}
  .pref-badge{{display:inline-block;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:700;background:#1e3a5f;color:#7dd3fc;margin-left:4px}}
  .action-del{{display:inline-block;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:700;background:#7f1d1d;color:#fca5a5}}
  .action-keep{{display:inline-block;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:700;background:#14532d;color:#86efac}}
  .no-dup{{padding:40px;text-align:center;color:var(--green);font-size:18px;font-weight:600}}
  .plan-table{{background:var(--surface);border-radius:10px;overflow:hidden;border:1px solid var(--border)}}
  .plan-table th{{background:var(--surface2);padding:10px 14px;text-align:left;font-size:12px;color:var(--muted);text-transform:uppercase;border-bottom:1px solid var(--border)}}
  .plan-table td{{padding:10px 14px;border-bottom:1px solid var(--border)}}
  .target-star{{color:#fbbf24;font-weight:700;margin-left:8px;font-size:12px}}
  .cmd-box{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 20px}}
  .cmd-item{{padding:6px 0;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
  .cmd-item:last-child{{border-bottom:none}}
  .cmd-item code{{font-family:'Cascadia Code','Consolas',monospace;font-size:13px;color:var(--accent)}}
  .muted{{color:var(--muted)}}
  .warning-box{{background:#1c1208;border:1px solid #78350f;border-radius:8px;padding:12px 16px;margin-top:12px;color:#fde68a;font-size:13px}}
  footer{{margin-top:24px;font-size:11px;color:var(--muted);text-align:center}}
</style>
</head>
<body>
<h1>Duplicate File Report</h1>
<div class="subtitle">Generated: {now}</div>

<div class="stats">
  <div class="stat-card"><div class="stat-val">{len(scans)}</div><div class="stat-lbl">Scanned disks</div></div>
  <div class="stat-card"><div class="stat-val">{total_dup_groups:,}</div><div class="stat-lbl">Duplicate groups</div></div>
  <div class="stat-card"><div class="stat-val">{total_dup_files:,}</div><div class="stat-lbl">Duplicate files</div></div>
  <div class="stat-card" style="border-color:#fb923c44"><div class="stat-val" style="color:var(--warn)">{human_size(wasted_bytes)}</div><div class="stat-lbl">Wasted space</div></div>
  <div class="stat-card" style="border-color:#4ade8044"><div class="stat-val" style="color:var(--green)">{human_size(plan['freed_total'])}</div><div class="stat-lbl">Can be freed</div></div>
</div>

<div class="disks">{scans_summary}</div>
{no_dup_msg}
{dup_section}

<footer>disk_scan.py &nbsp;&middot;&nbsp; {now}</footer>

<script>
function toggle(i){{
  var d=document.getElementById('detail'+i);
  var a=document.getElementById('arr'+i);
  d.classList.toggle('open');
  a.textContent=d.classList.contains('open')?'^':'v';
}}
function filterRows(){{
  var q=document.getElementById('search').value.toLowerCase();
  var tbody=document.getElementById('tbody');
  if(!tbody)return;
  tbody.querySelectorAll('tr.group-header').forEach(function(row){{
    var next=row.nextElementSibling;
    var text=(row.textContent+(next?next.textContent:'')).toLowerCase();
    var show=!q||text.includes(q);
    row.style.display=show?'':'none';
    if(next&&next.classList.contains('detail-row')){{
      if(!show)next.style.display='none';
      else next.style.display=next.classList.contains('open')?'table-row':'none';
    }}
  }});
}}
</script>
</body>
</html>
"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)


# -------------------------------------------------------------------
#  COMPARE -- main entry
# -------------------------------------------------------------------
def cmd_compare() -> None:
    cwd = Path.cwd()
    print(f"[COMPARE] Looking for scan files in: {cwd}\n")

    scans = load_scan_files(cwd)
    if not scans:
        print("[ERROR] No scan files found.")
        print("  Run first: python disk_scan.py scan <path>")
        sys.exit(1)

    print(f"\n  Loaded: {len(scans)} disk(s)\n")

    duplicates = find_duplicates(scans)
    plan       = plan_deletions(scans, duplicates)

    cli_report(scans, duplicates, plan)

    html_path = cwd / REPORT_HTML
    html_report(scans, duplicates, plan, html_path)
    print(f"[COMPARE] HTML report saved : {html_path}")

    cmd_files = generate_cmd_files(scans, plan, cwd)
    print(f"[COMPARE] CMD scripts generated:")
    for p in cmd_files:
        safe = p.stem[len(SCAN_FILE_PREFIX):]
        lbl  = next(
            (get_label(s) for s in scans if s.get("_safe_label") == safe),
            safe
        )
        n = len(plan["per_disk"].get(lbl, []))
        print(f"          {p.name}  ({n} file(s) to delete)")


# -------------------------------------------------------------------
#  MAIN
# -------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="USB disk duplicate finder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    scan_p = sub.add_parser("scan", help="Scan a disk or folder")
    scan_p.add_argument("disk", help="Path to disk (e.g. J: or /mnt/usb)")
    scan_p.add_argument(
        "--label", "-l",
        default=None,
        metavar="NAME",
        help="Custom label for the output file, e.g. --label red_flash. "
             "If omitted, the script will ask interactively.",
    )

    sub.add_parser("compare", help="Compare all scan files and find duplicates")

    args = parser.parse_args()

    if args.command == "scan":
        cmd_scan(args.disk, getattr(args, "label", None))
    elif args.command == "compare":
        cmd_compare()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
