"""
sync_bank_csv.py  --  Pull the newest bank CSV from Downloads into the planner
==============================================================================

WHAT IT DOES
------------
You download your transaction CSV from online banking the usual way. This
script then does the tedious part:

  1. Finds the NEWEST matching CSV in your Downloads folder.
  2. Validates it actually looks like your bank export (has the right columns)
     -- so a stray CSV in Downloads can't silently corrupt your planner.
  3. Backs up the current file (so a bad sync is always reversible).
  4. Copies the new one to where debt_planner_v2.py reads from.

It only ever touches local files. No bank login, no network, no credentials.
The worst case is a copied file, and the backup makes even that reversible.

TWO MODES
---------
  python sync_bank_csv.py            # one-shot: grab newest, copy, done
  python sync_bank_csv.py --watch    # sit and watch; copy whenever a newer
                                      # matching CSV shows up in Downloads
  python sync_bank_csv.py --dry-run  # show what it WOULD do, change nothing

CONFIGURE
---------
Edit the constants just below, or override them with the command-line flags
(run with --help to see them). Defaults assume Windows + this script living in
your DebtFree folder.
"""

from __future__ import annotations

import argparse
import csv
import filecmp
import shutil
import time
from datetime import datetime
from pathlib import Path


# ----------------------------------------------------------------------------
# CONFIG -- edit these, or override on the command line
# ----------------------------------------------------------------------------

# Where your browser drops downloads.
DEFAULT_SOURCE = Path.home() / "Downloads"

# Which files to consider. Keep it specific if your bank uses a predictable
# name (e.g. "ExportedTransactions*.csv") so it can't grab an unrelated CSV.
DEFAULT_PATTERN = "*.csv"

# The file debt_planner_v2.py reads from. By default we write next to THIS
# script. If you rename it, rename it in the planner too.
DEFAULT_DEST = Path(__file__).parent / "bank_sample.csv"

# A real export must contain all of these columns or we refuse to copy it.
EXPECTED_COLUMNS = {"amount", "date", "description", "newbalance", "type"}

# How often --watch checks the folder, in seconds.
DEFAULT_INTERVAL = 30


# ----------------------------------------------------------------------------
# CORE STEPS
# ----------------------------------------------------------------------------

def newest_matching(source: Path, pattern: str) -> Path | None:
    """Return the most-recently-modified file matching pattern, or None."""
    candidates = list(source.glob(pattern))
    if not candidates:
        return None
    # max by modification time -- the most reliable "newest" signal for downloads
    return max(candidates, key=lambda p: p.stat().st_mtime)


def looks_like_bank_export(path: Path) -> bool:
    """
    Cheap guardrail: peek at the header row and confirm every expected column
    is present. A file can have EXTRA columns (that's fine) but not be missing
    any of the ones the planner relies on.
    """
    try:
        with path.open(newline="") as f:
            header = next(csv.reader(f), [])
    except (OSError, StopIteration):
        return False
    columns = {h.strip().lower() for h in header}
    return EXPECTED_COLUMNS.issubset(columns)


def already_current(source_file: Path, dest: Path) -> bool:
    """True if dest exists and is byte-for-byte identical to the source file."""
    return dest.exists() and filecmp.cmp(source_file, dest, shallow=False)


def backup(dest: Path) -> Path | None:
    """Copy the existing dest aside before we overwrite it. Returns backup path."""
    if not dest.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = dest.with_name(f"{dest.stem}.{stamp}.bak{dest.suffix}")
    shutil.copy2(dest, bak)
    return bak


def sync_once(source: Path, pattern: str, dest: Path, dry_run: bool) -> bool:
    """
    Do one find-validate-backup-copy cycle. Returns True if a new file was
    copied, False otherwise (nothing found, invalid, or already current).
    """
    if not source.is_dir():
        print(f"  ! Source folder not found: {source}")
        return False

    candidate = newest_matching(source, pattern)
    if candidate is None:
        print(f"  - No files matching '{pattern}' in {source}")
        return False

    mtime = datetime.fromtimestamp(candidate.stat().st_mtime)
    print(f"  Newest match: {candidate.name}  (modified {mtime:%b %d %I:%M %p})")

    if not looks_like_bank_export(candidate):
        print(f"  ! Skipped -- '{candidate.name}' is missing expected columns "
              f"({', '.join(sorted(EXPECTED_COLUMNS))}).")
        return False

    if already_current(candidate, dest):
        print("  = Already up to date; nothing to copy.")
        return False

    if dry_run:
        print(f"  (dry run) would back up and copy -> {dest}")
        return False

    bak = backup(dest)
    if bak:
        print(f"  Backed up old file -> {bak.name}")
    shutil.copy2(candidate, dest)
    print(f"  Copied -> {dest}")
    return True


# ----------------------------------------------------------------------------
# ENTRY POINT
# ----------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Sync newest bank CSV into the planner.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                        help=f"folder to watch (default: {DEFAULT_SOURCE})")
    parser.add_argument("--pattern", default=DEFAULT_PATTERN,
                        help=f"filename glob (default: {DEFAULT_PATTERN})")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST,
                        help=f"where to copy it (default: {DEFAULT_DEST})")
    parser.add_argument("--watch", action="store_true",
                        help="keep running and copy whenever a newer file appears")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"seconds between checks in --watch (default: {DEFAULT_INTERVAL})")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would happen without copying anything")
    args = parser.parse_args()

    print("Bank CSV sync")
    print("-" * 56)
    print(f"Source : {args.source}")
    print(f"Pattern: {args.pattern}")
    print(f"Dest   : {args.dest}\n")

    if not args.watch:
        sync_once(args.source, args.pattern, args.dest, args.dry_run)
        return

    # --watch: poll until interrupted. We track the last mtime we acted on so
    # we don't recopy the same file every interval.
    print(f"Watching every {args.interval}s. Press Ctrl+C to stop.\n")
    last_seen = None
    try:
        while True:
            candidate = newest_matching(args.source, args.pattern)
            current = candidate.stat().st_mtime if candidate else None
            if current != last_seen:
                print(f"[{datetime.now():%H:%M:%S}] checking...")
                if sync_once(args.source, args.pattern, args.dest, args.dry_run):
                    last_seen = current
                else:
                    # remember it even if we didn't copy, so we don't re-log it
                    last_seen = current
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()