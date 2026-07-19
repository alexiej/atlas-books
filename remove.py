#!/usr/bin/env python3
"""
remove.py — Unpublish a book: remove from catalog + delete files from public/

Usage:
  python remove.py 20-000-mil-podmorskiej-zeglugi
  python remove.py 20-000-mil-podmorskiej-zeglugi --keep-source  # don't touch config.json

What it does:
  1. Removes the book entry from public/catalog.json
  2. Deletes all book files from public/  (.html, .epub, .pdf, cover, all .mp3)
  3. Sets published: false in books-source/<id>/config.json  (unless --keep-source)
  4. Rebuilds public/index.html
"""

from __future__ import annotations
import argparse, json, sys
from datetime import date
from pathlib import Path

PROJECT_DIR  = Path(__file__).resolve().parent
BOOK_SOURCE  = PROJECT_DIR / "books-source"
PUBLIC_DIR   = PROJECT_DIR / "public"
CATALOG_PATH = PUBLIC_DIR / "catalog.json"

# ── ANSI ──────────────────────────────────────────────────────────────────────
def _c(code: str, t: str) -> str:
    return f"\033[{code}m{t}\033[0m" if sys.stdout.isatty() else t
def gold(t: str)  -> str: return _c("33", t)
def green(t: str) -> str: return _c("32", t)
def red(t: str)   -> str: return _c("31", t)
def gray(t: str)  -> str: return _c("90", t)
def cyan(t: str)  -> str: return _c("36", t)
def bold(t: str)  -> str: return _c("1",  t)
def dim(t: str)   -> str: return _c("2",  t)

def ok(msg: str)   -> None: print(f"  {green('✓')} {msg}")
def err(msg: str)  -> None: print(f"  {red('✗')} {msg}")
def info(msg: str) -> None: print(f"  {gold('·')} {msg}")
def skip(msg: str) -> None: print(f"  {gray('–')} {msg}")
def step(msg: str) -> None: print(f"\n  {bold(gold('▶'))} {msg}")

# ── Catalog helpers ────────────────────────────────────────────────────────────
def load_catalog() -> list[dict]:
    if CATALOG_PATH.exists():
        return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return []

def save_catalog(catalog: list[dict]) -> None:
    CATALOG_PATH.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")

# ── Rebuild index.html by delegating to publish.py ────────────────────────────
def rebuild_index(catalog: list[dict]) -> None:
    """Import and call build_index from publish.py."""
    import importlib.util, sys as _sys
    spec = importlib.util.spec_from_file_location("publish", PROJECT_DIR / "publish.py")
    if spec and spec.loader:
        pub = importlib.util.module_from_spec(spec)
        _sys.modules["publish"] = pub
        spec.loader.exec_module(pub)  # type: ignore[union-attr]
        pub.build_index(catalog)  # type: ignore[attr-defined]
    else:
        err("Could not load publish.py to rebuild index")

# ── Remove ─────────────────────────────────────────────────────────────────────
def remove_book(book_id: str, keep_source: bool = False) -> bool:
    print()
    print(f"  {bold(gold('★ ATLAS BOOKS — REMOVE'))}: {bold(book_id)}")

    # 1. Catalog
    step("Removing from catalog")
    catalog = load_catalog()
    before  = len(catalog)
    catalog = [e for e in catalog if e["id"] != book_id]
    if len(catalog) == before:
        info(f"'{book_id}' was not in catalog")
    else:
        save_catalog(catalog)
        ok(f"Removed from catalog ({before} → {len(catalog)} books)")

    # 2. Delete files from public/
    step("Deleting public files")
    deleted = 0

    # Main generated files
    for suffix in [".html", ".epub", ".pdf", ".print.html", ".mobi"]:
        f = PUBLIC_DIR / f"{book_id}{suffix}"
        if f.exists():
            f.unlink()
            ok(f"Deleted: {f.name}")
            deleted += 1

    # Cover image (e.g. book_id-cover.jpg / book_id-cover.png)
    for ext in [".jpg", ".jpeg", ".png", ".webp"]:
        f = PUBLIC_DIR / f"{book_id}-cover{ext}"
        if f.exists():
            f.unlink()
            ok(f"Deleted: {f.name}")
            deleted += 1

    # All .mp3 files that start with the book_id
    for mp3 in sorted(PUBLIC_DIR.glob("*.mp3")):
        if mp3.stem.startswith(book_id):
            mp3.unlink()
            ok(f"Deleted: {mp3.name}")
            deleted += 1

    if deleted == 0:
        skip("No public files found to delete")
    else:
        ok(f"Deleted {deleted} files from public/")

    # 3. Mark as unpublished in config.json
    if not keep_source:
        cfg_path = BOOK_SOURCE / book_id / "config.json"
        if cfg_path.exists():
            step("Marking as unpublished in books-source")
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                cfg["published"] = False
                cfg.pop("published_date", None)
                cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
                ok(f"config.json → published: false")
            except Exception as e:
                err(f"Could not update config.json: {e}")
        else:
            skip(f"books-source/{book_id}/config.json not found")

    # 4. Rebuild index.html
    step("Rebuilding index.html")
    rebuild_index(catalog)

    print()
    ok(f"Done — '{book_id}' removed.")
    if keep_source:
        info("books-source/ config.json was NOT modified (--keep-source)")
    else:
        info("To re-publish: edit config.json and run: python generate.py <id> && python publish.py <id>")
    print()
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="remove",
        description="Unpublish a book: remove from catalog + delete public files",
        add_help=False,
    )
    parser.add_argument("book_id", nargs="?", default=None, help="Book ID (slug)")
    parser.add_argument("--keep-source", action="store_true",
                        help="Do NOT set published:false in books-source/config.json")
    parser.add_argument("--list",  action="store_true", help="List published books in catalog")
    parser.add_argument("-h", "--help", action="store_true")
    args = parser.parse_args()

    if args.help or not (args.book_id or args.list):
        print(__doc__)
        sys.exit(0)

    if args.list:
        catalog = load_catalog()
        print(f"\n  {bold(gold('Published books'))}  ({CATALOG_PATH})\n")
        if not catalog:
            print(f"  {gray('(empty)')}\n")
        for e in catalog:
            audio_n = len(e.get("audio", []))
            print(f"  {gold('►')} {bold(e['id'])}  {dim(e.get('title', ''))}")
            print(f"    published: {e.get('published', '?')}  audio: {audio_n} chapters")
        print()
        sys.exit(0)

    if not args.book_id:
        err("Usage: python remove.py <book-id>")
        sys.exit(1)

    ok_result = remove_book(args.book_id, keep_source=args.keep_source)
    sys.exit(0 if ok_result else 1)


if __name__ == "__main__":
    main()
