#!/usr/bin/env python3
"""
fix-names.py — Rename MP3s in books-source to the standard convention:
  {book_id}-ch{N:02d}-{chapter_id}.mp3

Chapter detection priority (tried in order):
  1. tts-timing-{lang}.json sidecar  (ch_id key → mp3 filename)
  2. -ch{N}- or -ch{N:02d}-          (already the new format / need zero-padding)
  3. _{NNN}_ numeric prefix           (e.g. _001_ → chapters[0])
  4. -{chapter_id}.mp3               (old generate-tts.py format: book_id-slug.mp3)
  5. Fuzzy slug match against chapter title
  6. Unmatched → report and skip

Usage:
  python fix-names.py 20-000-mil-podmorskiej-zeglugi          # preview + confirm
  python fix-names.py 20-000-mil-podmorskiej-zeglugi --dry-run # preview only
  python fix-names.py 20-000-mil-podmorskiej-zeglugi --yes     # rename without confirm
  python fix-names.py --all                                     # all books in books-source
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
BOOK_SOURCE  = PROJECT_DIR / "books-source"

sys.path.insert(0, str(PROJECT_DIR))
try:
    from generate import md_to_chapters, epub_to_chapters, pdf_to_chapters, slugify
except ImportError as e:
    print(f"ERROR: cannot import from generate.py: {e}", file=sys.stderr)
    sys.exit(1)

# ── ANSI colours ───────────────────────────────────────────────────────────────
_tty = sys.stdout.isatty()
def _c(n: str, s: str) -> str: return f"\033[{n}m{s}\033[0m" if _tty else s
def gold(s: str)  -> str: return _c("33", s)
def green(s: str) -> str: return _c("32", s)
def red(s: str)   -> str: return _c("31", s)
def gray(s: str)  -> str: return _c("90", s)
def cyan(s: str)  -> str: return _c("36", s)
def bold(s: str)  -> str: return _c("1",  s)
def dim(s: str)   -> str: return _c("2",  s)
def yellow(s: str)-> str: return _c("33", s)

def ok(msg: str)   -> None: print(f"  {green('✓')} {msg}")
def err(msg: str)  -> None: print(f"  {red('✗')} {msg}", file=sys.stderr)
def info(msg: str) -> None: print(f"  {gold('·')} {msg}")
def warn(msg: str) -> None: print(f"  {yellow('⚠')} {msg}")


# ── Load chapters from source book ────────────────────────────────────────────
def load_chapters(book_dir: Path) -> list[dict]:
    for name, fn in [("book.md", md_to_chapters), ("book.epub", epub_to_chapters),
                     ("book.pdf", pdf_to_chapters)]:
        p = book_dir / name
        if p.exists():
            try:
                chs = fn(p)
                info(f"Source: {name}  ({len(chs)} chapters)")
                return chs
            except Exception as e:
                err(f"Failed to parse {name}: {e}")
    err("No source file found (book.md / book.epub / book.pdf)")
    return []


# ── Slug normalisation for fuzzy matching ─────────────────────────────────────
def _slug_key(s: str) -> str:
    """Aggressively normalize for comparison: lower, ASCII letters+digits only."""
    s = s.lower()
    # strip Polish diacritics
    s = s.translate(str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ"))
    return re.sub(r"[^a-z0-9]+", "", s)


# ── Build rename plan for one book ────────────────────────────────────────────
def build_rename_plan(book_id: str, lang: str = "pl") -> list[tuple[Path, Path]] | None:
    """
    Returns list of (old_path, new_path) pairs.
    old_path == new_path means already correct (skipped).
    Returns None on fatal error.
    """
    book_dir = BOOK_SOURCE / book_id
    if not book_dir.exists():
        err(f"books-source/{book_id}/ not found")
        return None

    # ── Load chapters ─────────────────────────────────────────────────────────
    chapters = load_chapters(book_dir)
    if not chapters:
        return None

    # Build lookup structures
    # chapter_by_id[ch_id] = chapter
    # chapter_by_number[N] = chapter  (1-based, from ch.number)
    # chapter_by_order[i]  = chapter  (0-based list index)
    chapter_by_id:     dict[str, dict] = {}
    chapter_by_number: dict[int, dict] = {}
    chapter_by_slug_key: dict[str, dict] = {}
    for i, ch in enumerate(chapters):
        ch_id = ch.get("id", f"ch{i+1}")
        ch_num = ch.get("number", i + 1)
        chapter_by_id[ch_id] = ch
        chapter_by_number[ch_num] = ch
        chapter_by_slug_key[_slug_key(ch_id)] = ch
        chapter_by_slug_key[_slug_key(ch.get("title", ""))] = ch

    # ── Load sidecar (optional) ───────────────────────────────────────────────
    # Maps chapter_id → current mp3 filename (source dir)
    sidecar_path = book_dir / f"tts-timing-{lang}.json"
    sidecar: dict = {}
    mp3_to_chid_from_sidecar: dict[str, str] = {}   # basename → chapter_id
    if sidecar_path.exists():
        try:
            raw = json.loads(sidecar_path.read_text(encoding="utf-8"))
            for ch_id, entry in raw.get("chapters", {}).items():
                mp3 = entry.get("mp3", "")
                if mp3:
                    mp3_to_chid_from_sidecar[mp3] = ch_id
            sidecar = raw
            info(f"Sidecar: {sidecar_path.name}  ({len(mp3_to_chid_from_sidecar)} entries)")
        except Exception as e:
            warn(f"Could not parse sidecar: {e}")

    # ── Find all MP3s ─────────────────────────────────────────────────────────
    all_mp3 = sorted(book_dir.glob("*.mp3"))
    if not all_mp3:
        warn("No MP3 files found in source directory")
        return []

    # ── Detect chapter for each MP3 ───────────────────────────────────────────
    plan: list[tuple[Path, Path]] = []
    unmatched: list[Path] = []

    for mp3 in all_mp3:
        name = mp3.name
        stem = mp3.stem
        chapter: dict | None = None
        detection = ""
        # slug to use in new name — may be extracted from filename or from chapter id
        explicit_slug: str | None = None

        # Strip book_id prefix once for all strategies
        bid_prefix = book_id + "-"
        bid_prefix2 = book_id + "_"
        rest_after_bookid: str | None = None
        if stem.startswith(bid_prefix):
            rest_after_bookid = stem[len(bid_prefix):]
        elif stem.startswith(bid_prefix2):
            rest_after_bookid = stem[len(bid_prefix2):]

        # ── Strategy 1: sidecar ──────────────────────────────────────────────
        if name in mp3_to_chid_from_sidecar:
            ch_id = mp3_to_chid_from_sidecar[name]
            chapter = chapter_by_id.get(ch_id)
            detection = "sidecar"

        # ── Strategy 2: already ch{N} or ch{N}-{slug} format ───────────────
        if chapter is None and rest_after_bookid:
            m = re.match(r'^ch(\d+)(?:-(.+))?$', rest_after_bookid)
            if m:
                n = int(m.group(1))
                chapter = chapter_by_number.get(n)
                if chapter:
                    slug_part = m.group(2)          # None when no slug suffix
                    if slug_part:
                        explicit_slug = slug_part   # keep meaningful slug
                    # else: no slug → new name will also have no slug
                    detection = f"already ch{n} format"

        # ── Strategy 3: _{NNN}_{slug} or -{NNN}-{slug} after book_id ────────
        # e.g. book_001_chapter-title.mp3 → strip book_id → 001_chapter-title
        if chapter is None and rest_after_bookid:
            m = re.match(r'^(\d{2,3})[_-](.+)$', rest_after_bookid)
            if m:
                n = int(m.group(1))
                slug_from_name = m.group(2)
                # 1-based order: 001 → chapters[0]
                idx = n - 1
                if 0 <= idx < len(chapters):
                    chapter = chapters[idx]
                    explicit_slug = slug_from_name   # use meaningful slug from filename
                    detection = f"_{n:03d}_ numeric prefix"

        # ── Strategy 4: old format {book_id}-{chapter_id}.mp3 ───────────────
        if chapter is None and rest_after_bookid:
            rest = rest_after_bookid
            # Exact chapter_id match
            chapter = chapter_by_id.get(rest)
            if chapter:
                detection = f"exact slug match '{rest}'"
            else:
                # Fuzzy slug_key match
                key = _slug_key(rest)
                chapter = chapter_by_slug_key.get(key)
                if chapter:
                    explicit_slug = rest  # keep original slug from filename
                    detection = f"fuzzy slug '{rest}'"

        # ── Strategy 5: title slug appears in the stem ───────────────────────
        if chapter is None:
            stem_key = _slug_key(stem)
            for ch_key, ch in sorted(chapter_by_slug_key.items(),
                                     key=lambda kv: -len(kv[0])):  # longest match first
                if ch_key and len(ch_key) >= 4 and ch_key in stem_key:
                    chapter = ch
                    detection = f"fuzzy stem '{ch_key}'"
                    break

        # ── No match ─────────────────────────────────────────────────────────
        if chapter is None:
            unmatched.append(mp3)
            continue

        # Compute canonical new name
        ch_idx = next((i for i, c in enumerate(chapters) if c is chapter), 0)
        ch_id  = chapter.get("id", f"ch{ch_idx+1}")
        ch_num = chapter.get("number", ch_idx + 1)
        # Prefer explicit_slug (from filename); fall back to chapter id.
        # Skip the slug entirely when it's a generic chN pattern (e.g. "ch1" from epub).
        use_slug = explicit_slug if explicit_slug else ch_id
        if re.fullmatch(r'ch\d+', use_slug):
            new_name = f"{book_id}-ch{ch_num:02d}.mp3"
        else:
            new_name = f"{book_id}-ch{ch_num:02d}-{use_slug}.mp3"
        new_path = book_dir / new_name

        plan.append((mp3, new_path))
        if mp3 != new_path:
            info(f"  {dim(name)}  →  {cyan(new_name)}  {gray('(' + detection + ')')}")
        else:
            info(f"  {green('✓')} {dim(name)}  (already correct)")

    if unmatched:
        print()
        warn(f"{len(unmatched)} file(s) could not be matched to any chapter:")
        for p in unmatched:
            print(f"    {red('?')} {p.name}")

    return plan


# ── Apply rename plan ─────────────────────────────────────────────────────────
def apply_plan(plan: list[tuple[Path, Path]], book_id: str, lang: str = "pl") -> None:
    book_dir    = BOOK_SOURCE / book_id
    sidecar_path = book_dir / f"tts-timing-{lang}.json"
    sidecar: dict = {}
    if sidecar_path.exists():
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    renamed = 0
    skipped = 0
    conflicts: list[tuple[Path, Path]] = []

    for old, new in plan:
        if old == new:
            skipped += 1
            continue
        if new.exists() and new != old:
            conflicts.append((old, new))
            continue
        old.rename(new)
        renamed += 1

        # Update sidecar if present: find the entry that had old.name and update mp3 field
        for ch_id, entry in sidecar.get("chapters", {}).items():
            if entry.get("mp3") == old.name:
                entry["mp3"] = new.name
                break

    if sidecar:
        sidecar_path.write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        ok(f"Updated sidecar: {sidecar_path.name}")

    if conflicts:
        warn(f"{len(conflicts)} conflict(s) — target already exists (skipped):")
        for old, new in conflicts:
            print(f"    {old.name}  →  {new.name}")

    print()
    ok(f"Renamed {renamed} file(s)  ·  {skipped} already correct  ·  {len(conflicts)} skipped (conflicts)")


# ── Fix one book ──────────────────────────────────────────────────────────────
def fix_book(book_id: str, dry_run: bool, yes: bool, lang: str = "pl") -> bool:
    print()
    print(f"  {bold(gold('★ fix-names'))}  {bold(book_id)}")
    print()

    plan = build_rename_plan(book_id, lang)
    if plan is None:
        return False
    if not plan:
        ok("Nothing to rename.")
        return True

    renames = [(o, n) for o, n in plan if o != n]
    if not renames:
        ok("All files already use the correct naming convention.")
        return True

    print()
    print(f"  {bold(str(len(renames)))} file(s) to rename")

    if dry_run:
        info("Dry run — no files changed.")
        return True

    if not yes:
        try:
            ans = input(f"\n  Proceed? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            info("Aborted.")
            return True

    apply_plan(plan, book_id, lang)
    return True


# ── CLI ────────────────────────────────────────────────────────────────────────
def main() -> None:
    p = argparse.ArgumentParser(
        prog="fix-names.py",
        description="Rename MP3s in books-source to {book_id}-ch{NN}-{chapter_id}.mp3",
    )
    p.add_argument("book",     nargs="?", default=None, metavar="BOOK_ID")
    p.add_argument("--all",    action="store_true", help="Fix all books in books-source/")
    p.add_argument("--dry-run",action="store_true", help="Preview renames, don't touch files")
    p.add_argument("--yes",    action="store_true", help="Rename without confirmation")
    p.add_argument("--lang",   default="pl",        help="Language for sidecar file")
    args = p.parse_args()

    if not args.book and not args.all:
        p.print_help()
        sys.exit(0)

    if args.all:
        if not BOOK_SOURCE.exists():
            err(f"books-source/ not found: {BOOK_SOURCE}")
            sys.exit(1)
        books = [d.name for d in sorted(BOOK_SOURCE.iterdir())
                 if d.is_dir() and any((d / n).exists() for n in
                    ("book.md", "book.epub", "book.pdf"))]
        if not books:
            err("No books found")
            sys.exit(1)
        for book_id in books:
            fix_book(book_id, args.dry_run, args.yes, args.lang)
        sys.exit(0)

    fix_book(args.book, args.dry_run, args.yes, args.lang)


if __name__ == "__main__":
    main()
