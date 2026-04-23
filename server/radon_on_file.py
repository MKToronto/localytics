#!/usr/bin/env python3
"""
radon_file.py — Analyze cyclomatic complexity (CCN) for a single Python file.

Usage:
  python radon_file.py path/to/file.py
  python radon_file.py path/to/file.py --threshold 10
  python radon_file.py path/to/file.py --json

Requires:
  pip install radon
"""

import argparse
import json
from pathlib import Path
from datetime import datetime
import subprocess
from radon.complexity import cc_visit

DEFAULT_THRESHOLD = 10  # same as your server code (CCN > 10)

def get_last_modified_date_by_blame(filepath: Path, lineno: int, endline: int) -> str | None:
    """
    Uses git blame to get the most recent modification date of a line range in a file.
    Returns the date/time in ISO format: YYYY-MM-DD HH:MM:SS or None if unavailable.
    """
    try:
        output = subprocess.check_output(
            ["git", "blame", f"-L{lineno},{endline}", "--date=iso", str(filepath)],
            cwd=str(filepath.parent),
            text=True,
            stderr=subprocess.DEVNULL,
        )
        dates = []
        for line in output.splitlines():
            # Find tokens that look like ISO "YYYY-MM-DD HH:MM:SS"
            parts = line.split()
            for i, part in enumerate(parts):
                if part.count("-") == 2 and i + 1 < len(parts) and ":" in parts[i + 1]:
                    date_str = f"{part} {parts[i + 1]}"
                    try:
                        dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
                        dates.append(dt)
                    except ValueError:
                        continue
        if dates:
            return max(dates).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return None

def read_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")

def analyze_file(filepath: Path, threshold: int = DEFAULT_THRESHOLD):
    """
    Returns (total_functions, avg_ccn, high_ccn_list) where high_ccn_list items are dicts:
      {"filename","function","ccn","last_modified","lineno","endline"}
    """
    source = read_source(filepath)
    results = cc_visit(source)

    total_functions = len(results)
    total_ccn = sum(r.complexity for r in results)
    avg_ccn = (total_ccn / total_functions) if total_functions else 0

    high = []
    for r in results:
        if r.complexity > threshold:
            last_modified = get_last_modified_date_by_blame(filepath, r.lineno, getattr(r, "endline", r.lineno))
            high.append({
                "filename": filepath.name,
                "function": r.name,
                "ccn": r.complexity,
                "last_modified": last_modified or "unknown",
                "lineno": r.lineno,
                "endline": getattr(r, "endline", None),
            })

    high.sort(key=lambda x: x["ccn"], reverse=True)
    return total_functions, avg_ccn, high

def main():
    parser = argparse.ArgumentParser(description="Run Radon CCN on a single file quickly.")
    parser.add_argument("path", help="Path to the Python file to analyze")
    parser.add_argument("--threshold", "-t", type=int, default=DEFAULT_THRESHOLD,
                        help=f"CCN threshold for 'high' functions (default: {DEFAULT_THRESHOLD})")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    file_path = Path(args.path).expanduser().resolve()
    if not file_path.exists() or not file_path.is_file():
        print(f"🚫 File not found: {file_path}")
        raise SystemExit(2)

    total, avg, high = analyze_file(file_path, threshold=args.threshold)

    if args.json:
        print(json.dumps({
            "file": str(file_path),
            "total_functions_radon": total,
            "avg_ccn_radon": avg,
            "high_complexity_functions_radon": high,
            "threshold": args.threshold,
        }, indent=2))
    else:
        print(f"📄 File: {file_path}")
        print(f"📈 Total functions: {total}")
        print(f"📊 Average CCN: {avg:.2f}")
        print(f"⚠️  High-CCN functions (CCN > {args.threshold}): {len(high)}")
        if high:
            for item in high:
                print(
                    f"  - {item['function']} "
                    f"(CCN={item['ccn']}, lines {item['lineno']}-{item['endline']}, "
                    f"last_modified={item['last_modified']})"
                )

if __name__ == "__main__":
    main()
