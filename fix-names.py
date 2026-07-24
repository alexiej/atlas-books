#!/usr/bin/env python3
"""
fix-names.py — Rename old-format MP3s in books-source/<id>/ to the simple convention:

  {book_id}-ch01-skala-uciekajaca.mp3  →  ch01.mp3
  {book_id}-ch02-za-i-przeciw.mp3      →  ch02.mp3
  {book_id}.mp3                         →  book.mp3
  ch01.mp3 / book.mp3                  →  already correct (skipped)

Usage:
  python fix-names.py 20-000-mil-podmorskiej-zeglugi
  python fix-names.py 20-000-mil-podmorskiej-zeglugi --dry-run
  python fix-names.py --all
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
BOOK_SOURCE = PROJECT_DIR / "books-source"

_tty = sys.stdout.isatty()
def _c(n: str, s: str) -> str: return f"\033[{n}m{s}\033[0m" if _tty else s
def gold(s: str)  -> str: return _c("33", s)
def green(s: str) -> str: return _c("32", s)
def red(s: str)   -> str: return _c("31", s)
def gray(s: str)  -> str: return _c("90", s)
def bold(s: str)  -> str: return _c("1",  s)
def dim(s: str)   -> str: return _c("2",  s)
def yellow(s: str)-> str: return _c("33", s)

def ok(msg: str)   -> None: print(f"  {green('✓')} {msg}")
def err(msg: str)  -> None: print(f"  {red('✗')} {msg}", file=sys.stderr)
def info(msg: str) -> None: print(f"  {gold('·')} {msg}")
def warn(msg: str) -> None: print(f"  {yellow('⚠')} {msg}")


def fix_book(book_id: str, dry_run: bool = False) -> None:
    book_dir = BOOK_SOURCE / book_id
    if not book_dir.exists():
        err(f"books-source/{book_id}/ not found")
        return

    print()
    print(f"  {bold(gold('★ fix-names'))}: {bold(book_id)}")
    print()

    mp3s = sorted(book_dir.glob("*.mp3"))
    if not mp3s:
        warn(f"No MP3 files found in books-source/{book_id}/")
        return

    renamed = skipped = 0

    for mp3 in mp3s:
        stem = mp3.stem
        new_name: str | None = None

        # Already correct
        if re.match(r'^ch\d+$', stem) or stem == "book":
            info(f"Already correct: {mp3.name}")
            skipped += 1
            continue

        # {book_id}-ch{NN}[-rest].mp3  →  ch{NN}.mp3
        m = re.match(rf'^{re.escape(book_id)}-ch(\d+)', stem)
        if m:
            new_name = f"ch{int(m.group(1)):02d}.mp3"
        else:
            # Generic: find -ch{NN} anywhere in stem
            m = re.search(r'(?:^|-)ch(\d+)', stem)
            if m:
                new_name = f"ch{int(m.group(1)):02d}.mp3"

        # {book_id}[-rest].mp3  →  book.mp3
        if new_name is None:
            if stem == book_id or stem.startswith(f"{book_id}-") or stem.startswith(f"{book_id}_"):
                new_name = "book.mp3"

        if new_name is None:
            warn(f"Cannot determine new name — skipping: {mp3.name}")
            skipped += 1
            continue

        dest = book_dir / new_name
        if dest.exists():
            warn(f"{new_name} already exists — skipping {mp3.name}")
            skipped += 1
            continue

        print(f"  {dim(mp3.name)}  →  {bold(new_name)}")
        if not dry_run:
            mp3.rename(dest)
        renamed += 1

    print()
    label = "Would rename" if dry_run else "Renamed"
    ok(f"{label} {renamed} file(s)  (skipped {skipped})")
    if renamed and not dry_run:
        info(f"Next: python generate.py {book_id}  # → audio/ch01-part000.mp3")
    print()


def main() -> None:
    p = argparse.ArgumentParser(prog="fix-names.py", add_help=False)
    p.add_argument("book_id",   nargs="?", default=None)
    p.add_argument("--all",     action="store_true", help="Fix all books in books-source/")
    p.add_argument("--dry-run", action="store_true", help="Preview only, no renames")
    p.add_argument("-h","--help",action="store_true")
    args = p.parse_args()

    if args.help or (not args.book_id and not args.all):
        print(__doc__)
        sys.exit(0)

    if args.all:
        if not BOOK_SOURCE.exists():
            err(f"books-source/ not found"); sys.exit(1)
        for d in sorted(BOOK_SOURCE.iterdir()):
            if d.is_dir():
                fix_book(d.name, dry_run=args.dry_run)
        sys.exit(0)

    fix_book(args.book_id, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
