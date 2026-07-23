#!/usr/bin/env python3
"""
publish.py — Publish a book to public/ and rebuild the catalog

Copies generated files from books-dest/<name>/ to public/, updates catalog.json,
and regenerates the bilingual (EN/PL) public/index.html library page.

Usage:
  python publish.py star-federation          # publish one book
  python publish.py --all                    # publish all marked as published
  python publish.py --rebuild                # rebuild index.html only
  python publish.py --build                  # full build (template + all + index)
  python publish.py --list                   # show catalog
  python publish.py --remove star-federation # remove from catalog
"""

from __future__ import annotations
import argparse, base64, json, re, shutil, subprocess, sys
from datetime import date
from html import escape
from pathlib import Path
import generate as _gen   # shared parsing + rendering helpers

PROJECT_DIR  = Path(__file__).resolve().parent
BOOK_SOURCE  = PROJECT_DIR / "books-source"
BOOK_DEST    = PROJECT_DIR / "books-dest"
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
def step(msg: str) -> None: print(f"\n  {bold(gold('▶'))} {msg}")

# ── Catalog ────────────────────────────────────────────────────────────────────
def load_catalog() -> list[dict]:
    if CATALOG_PATH.exists():
        return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return []

def save_catalog(catalog: list[dict]) -> None:
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    CATALOG_PATH.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")

def _read_meta(book_id: str) -> dict:
    """Read metadata config for a book.

    Priority: books-dest/<id>/config.json  >  books-source/<id>/config.json  >  published HTML.
    """
    for cfg_path in [
        BOOK_DEST   / book_id / "config.json",
        BOOK_SOURCE / book_id / "config.json",
    ]:
        if cfg_path.exists():
            try:
                return json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                pass
    # Last resort: parse from published HTML
    html_path = PUBLIC_DIR / f"{book_id}.html"
    if html_path.exists():
        content = html_path.read_text(encoding="utf-8")
        m = re.search(r'window\.__BOOK_DATA__\s*=\s*(\{.*?\});', content, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    return {"id": book_id, "title": book_id}

def _cover_names() -> list[str]:
    return ["cover.jpg", "cover.png", "book.jpg", "book.png"]

def build_catalog_entry(book_id: str) -> dict:
    dest = BOOK_DEST / book_id
    meta = _read_meta(book_id)

    title       = meta.get("title", book_id)
    author      = meta.get("author", "")
    year        = str(meta.get("year", ""))
    description = meta.get("description", "")
    pub_date    = meta.get("published_date") or str(date.today())

    # Find which files were generated
    files: dict[str, str] = {}
    for suffix, key in [(".html", "html"), (".epub", "epub"), (".pdf", "pdf")]:
        if dest.is_dir() and (dest / f"{book_id}{suffix}").exists():
            files[key] = f"{book_id}{suffix}"

    # Audio files (match any .mp3 in the dest folder)
    audio_files: list[str] = []
    if dest.is_dir():
        audio_files = sorted(f.name for f in dest.glob("*.mp3"))

    # Cover: store filename in catalog (not base64)
    cover_file = ""
    if dest.is_dir():
        for name in _cover_names():
            if (dest / name).exists():
                ext = Path(name).suffix
                cover_file = f"{book_id}-cover{ext}"
                break
    if not cover_file:
        # Check books-source for a cover
        for name in _cover_names():
            if (BOOK_SOURCE / book_id / name).exists():
                ext = Path(name).suffix
                cover_file = f"{book_id}-cover{ext}"
                break

    # Attribution (for HTTP links in the library)
    # Supplement with relative filenames when no external URL is set
    attribution = dict(meta.get("attribution", {}))
    if not attribution.get("epub_url") and files.get("epub"):
        attribution["epub_url"] = files["epub"]   # e.g. "arystoteles-polityka.epub"
    if not attribution.get("pdf_url") and files.get("pdf"):
        attribution["pdf_url"] = files["pdf"]

    audio_source = meta.get("audio_source", "")

    # Chapter count (for reading stats on library cards)
    chapter_count = 0
    try:
        md_path = BOOK_DEST / book_id / "book.md"
        if md_path.exists():
            chapter_count = len(_gen.md_to_chapters(md_path))
    except Exception:
        pass

    return {
        "id":            book_id,
        "title":         title,
        "author":        author,
        "year":          year,
        "description":   description,
        "cover_file":    cover_file,
        "files":         files,
        "audio":         audio_files,
        "audio_source":  audio_source,
        "attribution":   attribution,
        "published":     pub_date,
        "chapter_count": chapter_count,
    }

# ── Load book data from books-dest ─────────────────────────────────────────────
def load_book_from_dest(book_id: str) -> dict:
    """Read book.md + config.json + cover + tts-audio from books-dest/<id>/.

    Raises FileNotFoundError when book hasn't been generated yet.
    Returns a book_data dict ready for inject_into_template / make_epub / make_pdf.
    """
    dest = BOOK_DEST / book_id
    md_path  = dest / "book.md"
    cfg_path = dest / "config.json"

    if not md_path.exists():
        raise FileNotFoundError(
            f"books-dest/{book_id}/book.md not found — run: python generate.py {book_id}"
        )
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"books-dest/{book_id}/config.json not found — run: python generate.py {book_id}"
        )

    config   = json.loads(cfg_path.read_text(encoding="utf-8"))
    lang     = config.get("lang", "pl")
    chapters = _gen.md_to_chapters(md_path)

    # Embed cover as base64 (template expects data-URI or empty string)
    cover = ""
    for name in _cover_names():
        p = dest / name
        if p.exists():
            mime  = "image/jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "image/png"
            data  = base64.b64encode(p.read_bytes()).decode()
            cover = f"data:{mime};base64,{data}"
            break

    # Re-apply audio + timing.
    # Priority: tts-transcript-align-{lang}.json (written by analyze-tts.py — has timing + seek_to)
    #           tts-audio-{lang}.json             (written by generate.py    — basic audio list)
    align_path = dest / f"tts-transcript-align-{lang}.json"
    tts_path   = dest / f"tts-audio-{lang}.json"
    align_map: dict = {}
    tts_map:   dict = {}
    try:
        if align_path.exists():
            align_map = json.loads(align_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    try:
        if tts_path.exists():
            tts_map = json.loads(tts_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    if align_map or tts_map:
        for ch in chapters:
            ch_id = ch.get("id", "")
            # If the align file exists, use it exclusively (tts-audio-pl.json may have
            # stale/corrupt data from old analyze-tts.py runs).  Only fall back to
            # tts_map when no align file exists at all (i.e. analyze-tts.py hasn't run yet).
            entry = align_map.get(ch_id) if align_map else tts_map.get(ch_id)
            if not entry:
                continue
            audio = entry.get("audio")
            if audio:
                ch[f"audio_{lang}"] = audio
            seek_to = entry.get("seek_to")
            if seek_to is not None:
                ch[f"seek_to_{lang}"] = seek_to
            timing = entry.get("timing")
            if timing:
                ch[f"timing_{lang}"] = timing

    return {**config, "cover": cover, "chapters": chapters}


# ── Publish one book ───────────────────────────────────────────────────────────
def publish_book(book_id: str, do_epub: bool = True, do_pdf: bool = True) -> bool:
    """Load from books-dest, generate HTML/EPUB/PDF, copy everything to public/."""
    dest = BOOK_DEST / book_id
    if not dest.exists():
        err(f"books-dest/{book_id}/ not found — run: python generate.py {book_id}")
        return False

    # 1. Load book data from dest (book.md + config + cover + tts-audio)
    step("Loading from books-dest")
    try:
        book_data = load_book_from_dest(book_id)
    except FileNotFoundError as e:
        err(str(e)); return False

    lang = book_data.get("lang", "pl")
    ok(f"Title: {book_data.get('title','')}  ·  {len(book_data.get('chapters',[]))} chapters")

    # Patch attribution with relative local-file URLs
    attr = book_data.setdefault("attribution", {})
    if not attr.get("epub_url"):
        attr["epub_url"] = f"{book_id}.epub"
    if not attr.get("pdf_url") and (BOOK_SOURCE / book_id / "book.pdf").exists():
        attr["pdf_url"] = f"{book_id}.pdf"

    # 2. Generate HTML viewer
    step("Building HTML viewer")
    if not _gen.TEMPLATE_PATH.exists():
        err(f"Template not found: {_gen.TEMPLATE_PATH}")
        err("Run: npm run build-viewer   (builds public/book-template.html)")
        return False
    out_html = dest / f"{book_id}.html"
    _gen.inject_into_template(book_data, _gen.TEMPLATE_PATH, out_html)
    ok(f"HTML: {out_html.name}  ({out_html.stat().st_size // 1024} kB)")

    # 3. EPUB
    if do_epub:
        src_epub = BOOK_SOURCE / book_id / "book.epub"
        out_epub = dest / f"{book_id}.epub"
        if src_epub.exists():
            step("Copying EPUB from source")
            shutil.copy2(src_epub, out_epub)
            ok(f"EPUB: {out_epub.name}  ({out_epub.stat().st_size // 1024} kB)")
        else:
            step("Building EPUB")
            _gen.make_epub(book_data, out_epub)

    # 4. PDF
    if do_pdf:
        src_pdf = BOOK_SOURCE / book_id / "book.pdf"
        out_pdf = dest / f"{book_id}.pdf"
        if src_pdf.exists():
            step("Copying PDF from source")
            shutil.copy2(src_pdf, out_pdf)
            ok(f"PDF: {out_pdf.name}  ({out_pdf.stat().st_size // 1024} kB)")
        else:
            step("Building PDF")
            _gen.make_pdf(book_data, out_pdf)

    # 5. Copy everything to public/
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []

    for name in [f"{book_id}.html", f"{book_id}.epub", f"{book_id}.pdf",
                 f"{book_id}.print.html", f"{book_id}.mobi"]:
        src = dest / name
        if src.exists():
            shutil.copy2(src, PUBLIC_DIR / name)
            copied.append(name)

    for mp3 in sorted(dest.glob("*.mp3")):
        shutil.copy2(mp3, PUBLIC_DIR / mp3.name)
        copied.append(mp3.name)

    for name in _cover_names():
        src = dest / name
        if not src.exists():
            src = BOOK_SOURCE / book_id / name
        if src.exists():
            ext      = Path(name).suffix
            dst_name = f"{book_id}-cover{ext}"
            shutil.copy2(src, PUBLIC_DIR / dst_name)
            copied.append(dst_name)
            break

    ok(f"Copied {len(copied)} files → public/  ({book_id})")

    # 6. Update catalog + mark published
    catalog = load_catalog()
    entry   = build_catalog_entry(book_id)
    catalog = [e for e in catalog if e["id"] != book_id]
    catalog.append(entry)
    catalog.sort(key=lambda e: e.get("published", ""), reverse=True)
    save_catalog(catalog)
    ok(f"Catalog: {len(catalog)} books")

    for cfg_path in [dest / "config.json", BOOK_SOURCE / book_id / "config.json"]:
        if cfg_path.exists():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                cfg["published"]      = True
                cfg["published_date"] = str(date.today())
                cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

    return True

# ── Index.html (bilingual) ─────────────────────────────────────────────────────
def _plural_pl(n: int) -> str:
    if n == 1: return "książka"
    if 2 <= n <= 4: return "książki"
    return "książek"

def build_index(catalog: list[dict]) -> None:
    # Sort alphabetically by title
    catalog = sorted(catalog, key=lambda e: e.get("title", "").lower())

    # Enrich existing entries with chapter_count if missing
    for entry in catalog:
        if not entry.get("chapter_count"):
            bid = entry.get("id", "")
            md_path = BOOK_DEST / bid / "book.md"
            if md_path.exists():
                try:
                    entry["chapter_count"] = len(_gen.md_to_chapters(md_path))
                except Exception:
                    pass
    n = len(catalog)

    def card(entry: dict) -> str:
        title  = escape(entry.get("title", ""))
        author = escape(entry.get("author", ""))
        year   = escape(str(entry.get("year", "")))
        desc   = escape(entry.get("description", ""))
        bid    = entry.get("id", "")
        files  = entry.get("files", {})
        cover_file = entry.get("cover_file", "")

        cover_html = ""
        if cover_file:
            cover_html = f'<img class="card-cover" src="{escape(cover_file)}" alt="{title}" loading="lazy"/>'
        else:
            cover_html = (
                f'<div class="card-cover card-cover--placeholder">'
                f'<span class="card-letter">{escape(title[:1])}</span></div>'
            )

        bm_badge = f'<span class="bm-badge" data-bid="{escape(bid)}" style="display:none"></span>'

        # Audio availability + source badge
        audio       = entry.get("audio", [])
        audio_src   = entry.get("audio_source", "")
        _src_labels = {
            "wolnelektury": "Wolne Lektury",
            "generated":    "AI (TTS)",
        }
        src_label = _src_labels.get(audio_src, audio_src) if audio_src else ""
        _src_labels = {
            "wolnelektury": "Wolne Lektury",
            "generated":    "AI",
        }
        src_label = _src_labels.get(audio_src, audio_src) if audio_src else ""
        _music_svg = (
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
            'xmlns="http://www.w3.org/2000/svg" width="12" height="12" style="flex-shrink:0">'
            '<path d="M9 18V5l12-2v13" stroke-linecap="round" stroke-linejoin="round"/>'
            '<circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>'
            '</svg>'
        )
        audio_badge_html = (
            f'<span class="card-audio-badge" title="MP3">'
            f'{_music_svg}'
            f'{f"<span>{escape(src_label)}</span>" if src_label else ""}'
            f'</span>'
        ) if audio else ""

        attr       = entry.get("attribution", {})
        epub_url   = attr.get("epub_url", "")
        pdf_url    = attr.get("pdf_url",  "")

        links_html = ""
        if "html" in files:
            links_html += (
                f'<a class="card-btn card-btn--primary" href="{escape(files["html"])}">'
                f'<span data-en="Read" data-pl="Czytaj">Read</span></a>'
            )
        if epub_url:
            links_html += (
                f'<a class="card-btn" href="{escape(epub_url)}" target="_blank" rel="noreferrer">EPUB</a>'
            )
        if pdf_url:
            links_html += (
                f'<a class="card-btn" href="{escape(pdf_url)}" target="_blank" rel="noreferrer">PDF</a>'
            )

        chapter_count = entry.get("chapter_count", 0)
        prog_bar = (
            f'<div class="card-progress" data-prog-bid="{escape(bid)}">'
            f'<div class="card-progress-fill" style="width:0%"></div></div>'
        )
        status_row = f'<div class="card-status-row" data-status-bid="{escape(bid)}"></div>'
        actions = (
            f'<div class="card-actions">'
            f'<button class="action-btn action-done" data-bid="{escape(bid)}" '
            f'onclick="toggleStatus(this,\'done\')" data-en="✓ Read" data-pl="✓ Przeczytana">✓ Read</button>'
            f'<button class="action-btn action-next" data-bid="{escape(bid)}" '
            f'onclick="toggleStatus(this,\'next\')" data-en="→ Next" data-pl="→ Następna">→ Next</button>'
            f'</div>'
        )
        return f"""
  <article class="book-card" data-id="{escape(bid)}" data-chapters="{chapter_count}">
    <div class="card-cover-wrap" style="position:relative">{cover_html}{bm_badge}{audio_badge_html}{prog_bar}</div>
    <div class="card-body">
      <div class="card-meta">
        {f'<span class="card-author">{author}</span>' if author else ''}
        {f'<span class="card-year">{year}</span>' if year else ''}
      </div>
      <h2 class="card-title">{title}</h2>
      {status_row}
      {f'<p class="card-desc">{desc}</p>' if desc else ''}
      <div class="card-footer">
        <div class="card-links">{links_html}</div>
        {actions}
      </div>
    </div>
  </article>"""

    cards_html = "\n".join(card(e) for e in catalog) if catalog else (
        '<div class="empty-state">'
        '<p data-en="No published books yet." data-pl="Brak opublikowanych książek.">No published books yet.</p>'
        '<p class="empty-hint">'
        'Run: <code>npm run generate &amp;&amp; npm run publish</code>'
        '</p></div>'
    )

    html = f"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title data-en="Library · Atlas Books" data-pl="Biblioteka · Atlas Books">Library · Atlas Books</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Crimson+Text:ital,wght@0,400;0,600;1,400;1,600&family=Exo+2:wght@300;400;600&family=Orbitron:wght@400;700&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0 }}
    :root {{
      --bg:       #ede9e0; --surface: #fffdf9; --text: #1a1714; --text-dim: #6b6257;
      --border:   rgba(120,105,80,0.18); --gold: #8a6f2e; --gold-hi: #b8923c;
      --gold-dim: rgba(138,111,46,0.14); --shad: 0 4px 40px rgba(60,50,30,0.14),0 1px 6px rgba(60,50,30,0.08);
      --font-h: 'Orbitron','Courier New',monospace;
      --font-b: 'Crimson Text','Palatino Linotype',Georgia,serif;
      --font-u: 'Exo 2',system-ui,sans-serif;
      --radius: 6px; --anim: 0.2s ease;
    }}
    [data-theme="dark"] {{
      --bg: #0d0c16; --surface: #14121f; --text: #e2ddd5; --text-dim: #a09688;
      --border: rgba(200,175,80,0.2); --gold: #c8a84b; --gold-hi: #e0c060;
      --gold-dim: rgba(200,175,80,0.12); --shad: 0 4px 60px rgba(0,0,0,0.5);
    }}
    html,body {{ min-height:100%; }}
    body {{ background:var(--bg); color:var(--text); font-family:var(--font-u);
            -webkit-font-smoothing:antialiased; transition:background var(--anim),color var(--anim); }}

    .site-header {{
      display:flex; align-items:center; justify-content:space-between;
      padding:0 2rem; height:52px; border-bottom:1px solid var(--border);
      background:var(--bg); position:sticky; top:0; z-index:10;
    }}
    .site-logo {{ font-family:var(--font-h); font-size:9px; letter-spacing:0.4em;
                  text-transform:uppercase; color:var(--gold); }}
    .header-actions {{ display:flex; gap:0.75rem; align-items:center; }}
    .header-search {{
      background:var(--surface); border:1px solid var(--border); border-radius:4px;
      color:var(--text); font-family:var(--font-u); font-size:11px;
      padding:5px 10px; width:180px; outline:none; transition:border-color var(--anim);
    }}
    .header-search:focus {{ border-color:var(--gold); }}
    .icon-btn {{
      background:none; border:1px solid var(--border); color:var(--text-dim);
      width:30px; height:30px; border-radius:4px; cursor:pointer;
      display:flex; align-items:center; justify-content:center; font-size:14px;
      transition:all var(--anim);
    }}
    .icon-btn:hover {{ color:var(--gold); border-color:var(--gold-dim); }}
    .lang-btn {{ font-family:var(--font-h); font-size:7.5px; letter-spacing:0.12em;
                 text-transform:uppercase; padding:0 10px; width:auto; }}
    .lang-btn.active {{ background:var(--gold-dim); color:var(--gold); border-color:var(--gold-dim); }}

    .library-main {{ max-width:1100px; margin:0 auto; padding:2.5rem 2rem 4rem; }}
    .library-header {{
      display:flex; align-items:baseline; gap:1rem;
      margin-bottom:2rem; padding-bottom:1rem; border-bottom:1px solid var(--border);
    }}
    .library-title {{ font-family:var(--font-b); font-size:1.8rem; font-weight:600;
                      font-style:italic; color:var(--text); }}
    .library-count {{ font-family:var(--font-h); font-size:8px; letter-spacing:0.3em;
                      color:var(--text-dim); text-transform:uppercase; }}
    .books-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:1.5rem; }}

    .book-card {{
      background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
      overflow:hidden; box-shadow:var(--shad); display:flex; flex-direction:column;
      transition:transform 0.18s,box-shadow 0.18s;
    }}
    .book-card:hover {{ transform:translateY(-2px); box-shadow:var(--shad),0 0 0 1px var(--gold-dim); }}
    .book-card.hidden {{ display:none; }}
    .card-cover-wrap {{ width:100%; aspect-ratio:3/4; overflow:hidden;
                        background:var(--gold-dim); flex-shrink:0; }}
    .card-cover {{ width:100%; height:100%; object-fit:cover; object-position:center top; display:block; }}
    .card-cover--placeholder {{
      display:flex; align-items:center; justify-content:center; width:100%; height:100%;
      background:linear-gradient(135deg,var(--gold-dim) 0%,rgba(138,111,46,0.08) 100%);
    }}
    .card-letter {{ font-family:var(--font-b); font-size:5rem; font-style:italic;
                    font-weight:600; color:var(--gold); opacity:0.5; user-select:none; }}
    .card-body {{ padding:1rem; display:flex; flex-direction:column; gap:0.4rem; flex:1; }}
    .card-meta {{ display:flex; justify-content:space-between; align-items:baseline; }}
    .card-author {{ font-family:var(--font-u); font-size:9px; letter-spacing:0.1em;
                    color:var(--text-dim); text-transform:uppercase; }}
    .card-year {{ font-family:var(--font-h); font-size:8px; letter-spacing:0.15em; color:var(--gold); }}
    .card-title {{ font-family:var(--font-b); font-size:1.15rem; font-style:italic;
                   font-weight:600; line-height:1.2; color:var(--text); }}
    .card-desc {{ font-family:var(--font-b); font-size:0.88rem; line-height:1.5; color:var(--text-dim);
                  overflow:hidden; display:-webkit-box; -webkit-line-clamp:3;
                  -webkit-box-orient:vertical; }}
    .card-footer {{ margin-top:auto; padding-top:0.6rem; display:flex; flex-direction:column; gap:0.35rem; }}
    .card-links {{ display:flex; gap:0.4rem; flex-wrap:wrap; }}
    .card-btn {{
      font-family:var(--font-h); font-size:7.5px; letter-spacing:0.15em; text-transform:uppercase;
      text-decoration:none; padding:5px 12px; border-radius:3px; border:1px solid var(--border);
      color:var(--text-dim); background:none; transition:all 0.15s; white-space:nowrap;
    }}
    .card-btn:hover {{ color:var(--gold-hi); border-color:var(--gold-dim); background:var(--gold-dim); }}
    .card-btn--primary {{ background:var(--gold); color:var(--surface); border-color:var(--gold); }}
    .card-btn--primary:hover {{ background:var(--gold-hi); border-color:var(--gold-hi); color:#fff; }}

    .bm-badge {{
      position:absolute; top:0.5rem; right:0.5rem; z-index:2;
      background:var(--gold); color:var(--surface);
      font-family:var(--font-h); font-size:7px; letter-spacing:0.1em;
      padding:3px 6px; border-radius:3px; pointer-events:none;
    }}

    /* ── Reading progress ────────────────────────────── */
    .card-progress {{
      position:absolute; bottom:0; left:0; right:0; height:4px;
      background:var(--gold-dim); z-index:3;
    }}
    .card-progress-fill {{ height:100%; background:var(--gold); transition:width 0.4s; }}

    .card-status-row {{
      font-family:var(--font-h); font-size:7.5px; letter-spacing:0.1em;
      text-transform:uppercase; color:var(--text-dim); min-height:1.2em;
    }}
    .card-status-row.s-reading {{ color:var(--gold); }}
    .card-status-row.s-done    {{ color:#4a9c6a; }}
    .card-status-row.s-next    {{ color:#7b6fcf; }}
    [data-theme="dark"] .card-status-row.s-done {{ color:#6ec98a; }}
    [data-theme="dark"] .card-status-row.s-next {{ color:#a89de0; }}

    .card-actions {{
      display:flex; gap:0.35rem;
    }}
    .action-btn {{
      font-family:var(--font-h); font-size:7px; letter-spacing:0.1em;
      text-transform:uppercase; padding:4px 9px; border-radius:3px;
      border:1px solid var(--border); color:var(--text-dim); background:none;
      cursor:pointer; transition:all 0.15s; white-space:nowrap;
    }}
    .action-btn:hover {{ color:var(--gold); border-color:var(--gold-dim); background:var(--gold-dim); }}
    .action-btn.active.action-done {{ color:#4a9c6a; border-color:rgba(74,156,106,0.35); background:rgba(74,156,106,0.12); }}
    .action-btn.active.action-next {{ color:#7b6fcf; border-color:rgba(123,111,207,0.35); background:rgba(123,111,207,0.12); }}
    [data-theme="dark"] .action-btn.active.action-done {{ color:#6ec98a; border-color:rgba(110,201,138,0.3); background:rgba(110,201,138,0.1); }}
    [data-theme="dark"] .action-btn.active.action-next {{ color:#a89de0; border-color:rgba(168,157,224,0.3); background:rgba(168,157,224,0.1); }}

    /* ── Filter tabs ─────────────────────────────────── */
    .filter-tabs {{ display:flex; gap:0.5rem; margin-bottom:1.5rem; flex-wrap:wrap; }}
    .filter-tab {{
      font-family:var(--font-h); font-size:7.5px; letter-spacing:0.15em;
      text-transform:uppercase; padding:5px 14px; border-radius:3px;
      border:1px solid var(--border); color:var(--text-dim); background:none;
      cursor:pointer; transition:all 0.15s;
    }}
    .filter-tab:hover {{ color:var(--gold); border-color:var(--gold-dim); }}
    .filter-tab.active {{ background:var(--gold-dim); color:var(--gold); border-color:var(--gold-dim); }}
    .card-audio-badge {{
      position:absolute; top:0.5rem; left:0.5rem; z-index:2;
      display:inline-flex; align-items:center; gap:4px;
      padding:3px 6px; border-radius:4px;
      background:rgba(255,253,249,0.88); backdrop-filter:blur(2px);
      border:1px solid var(--border); color:var(--gold);
      box-shadow:0 1px 3px rgba(0,0,0,0.12);
      font-family:var(--font-h); font-size:7px; letter-spacing:0.08em;
      text-transform:uppercase; white-space:nowrap;
    }}

    .empty-state {{ grid-column:1/-1; text-align:center; padding:4rem 2rem;
                    color:var(--text-dim); font-family:var(--font-b); font-size:1.1rem; font-style:italic; }}
    .empty-hint {{ font-style:normal; font-family:var(--font-u); font-size:0.85rem; margin-top:1rem; }}
    .empty-hint code {{ font-family:'Courier New',monospace; font-size:0.8rem;
                        background:var(--gold-dim); padding:2px 6px; border-radius:3px; color:var(--gold); }}

    .site-footer {{
      border-top:1px solid var(--border); padding:1rem 2rem; text-align:center;
      font-family:var(--font-h); font-size:7.5px; letter-spacing:0.3em;
      color:var(--text-dim); opacity:0.6; text-transform:uppercase;
    }}
    @media (max-width:600px) {{
      .books-grid {{ grid-template-columns:repeat(auto-fill,minmax(160px,1fr)); gap:1rem; }}
      .library-main {{ padding:1.5rem 1rem 3rem; }}
      .site-header {{ padding:0 1rem; }}
      .header-search {{ width:130px; }}
    }}
  </style>
</head>
<body>
  <header class="site-header">
    <div style="display:flex;align-items:center;gap:1.5rem">
      <span class="site-logo">★ Atlas Books</span>
    </div>
    <div class="header-actions">
      <input class="header-search" type="search" id="search"
             data-en-placeholder="Search..." data-pl-placeholder="Szukaj..."
             placeholder="Search..." oninput="filterBooks(this.value)"/>
      <button class="icon-btn lang-btn active" id="btn-en" onclick="setLang('en')" title="English">EN</button>
      <button class="icon-btn lang-btn"        id="btn-pl" onclick="setLang('pl')" title="Polski">PL</button>
      <button class="icon-btn" onclick="toggleTheme()" title="Toggle theme">◑</button>
    </div>
  </header>

  <main class="library-main">
    <div class="library-header">
      <h1 class="library-title" data-en="All books" data-pl="Wszystkie książki">All books</h1>
      <span class="library-count"
            data-en="{n} title{'s' if n != 1 else ''}"
            data-pl="{n} {_plural_pl(n)}">{n} title{'s' if n != 1 else ''}</span>
    </div>
    <div class="filter-tabs" id="filter-tabs">
      <button class="filter-tab active" onclick="filterStatus(this,'')" data-en="All" data-pl="Wszystkie">All</button>
      <button class="filter-tab" onclick="filterStatus(this,'reading')" data-en="Reading" data-pl="Czytam">Reading</button>
      <button class="filter-tab" onclick="filterStatus(this,'done')" data-en="Read" data-pl="Przeczytane">Read</button>
      <button class="filter-tab" onclick="filterStatus(this,'next')" data-en="Next" data-pl="Następna">Next</button>
    </div>
    <div class="books-grid" id="grid">
{cards_html}
    </div>
  </main>

  <footer class="site-footer">
    <span data-en="Atlas Books Publishing Platform" data-pl="Platforma wydawnicza Atlas Books">Atlas Books Publishing Platform</span>
    · built {date.today().strftime('%Y-%m-%d')}
  </footer>

  <script>
    // ── Language ───────────────────────────────────────────────────────────────
    function setLang(lang) {{
      document.documentElement.lang = lang;
      localStorage.setItem('atlas-lang', lang);
      document.querySelectorAll('[data-en]').forEach(el => {{
        el.textContent = el.dataset[lang] || el.dataset.en;
      }});
      document.querySelectorAll('[data-en-placeholder]').forEach(el => {{
        el.placeholder = lang === 'pl' ? el.dataset.plPlaceholder : el.dataset.enPlaceholder;
      }});
      const t = document.querySelector('title');
      if (t) t.textContent = t.dataset[lang] || t.dataset.en;
      document.getElementById('btn-en').classList.toggle('active', lang === 'en');
      document.getElementById('btn-pl').classList.toggle('active', lang === 'pl');
    }}
    (function() {{
      const saved = localStorage.getItem('atlas-lang') || 'en';
      if (saved !== 'en') setLang(saved);
    }})();

    // ── Theme ──────────────────────────────────────────────────────────────────
    function toggleTheme() {{
      const html = document.documentElement;
      html.dataset.theme = html.dataset.theme === 'dark' ? 'light' : 'dark';
      localStorage.setItem('atlas-theme', html.dataset.theme);
    }}
    (function() {{
      const saved = localStorage.getItem('atlas-theme');
      if (saved) document.documentElement.dataset.theme = saved;
    }})();

    // ── Bookmark badges ────────────────────────────────────────────────────────
    (function() {{
      document.querySelectorAll('.bm-badge[data-bid]').forEach(function(badge) {{
        var bid = badge.dataset.bid;
        try {{
          var bms = JSON.parse(localStorage.getItem('sf-bm-' + bid) || '[]');
          if (bms.length > 0) {{
            badge.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor" stroke="none" width="10" height="10" xmlns="http://www.w3.org/2000/svg"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg> ' + bms.length;
            badge.style.display = '';
          }}
        }} catch(e) {{}}
      }});
    }})();

    // ── Reading progress ────────────────────────────────────────────────────────
    var _activeFilter = '';

    function _getProgress(bid) {{
      try {{ return JSON.parse(localStorage.getItem('atlas-pos-' + bid) || 'null'); }} catch(e) {{ return null; }}
    }}
    function _fmtTime(s) {{
      if (!s || s <= 0) return '';
      var m = Math.floor(s / 60), sec = Math.floor(s % 60);
      return m + ':' + (sec < 10 ? '0' : '') + sec;
    }}
    function _getStatus(bid) {{
      return localStorage.getItem('atlas-status-' + bid) || '';
    }}
    function _effectiveStatus(bid) {{
      var st = _getStatus(bid);
      if (st) return st;
      var pos = _getProgress(bid);
      if (pos && pos.pct > 0 && pos.pct < 100) return 'reading';
      if (pos && pos.pct >= 100) return 'reading';  // show as reading until explicitly marked done
      return '';
    }}

    function loadBookProgress() {{
      var lang = localStorage.getItem('atlas-lang') || 'en';
      // Progress bars
      document.querySelectorAll('[data-prog-bid]').forEach(function(bar) {{
        var bid = bar.dataset.progBid;
        var st  = _getStatus(bid);
        var pos = _getProgress(bid);
        var pct = (st === 'done') ? 100 : (pos ? pos.pct : 0);
        bar.querySelector('.card-progress-fill').style.width = pct + '%';
      }});
      // Status rows
      document.querySelectorAll('[data-status-bid]').forEach(function(row) {{
        var bid      = row.dataset.statusBid;
        var st       = _getStatus(bid);
        var pos      = _getProgress(bid);
        var card     = row.closest('.book-card');
        var totalCh  = card ? parseInt(card.dataset.chapters || '0', 10) : 0;
        var text = '', cls = '';
        // Build chapter info string when we have position + chapter count
        function _chInfo() {{
          if (!pos || totalCh < 2) return '';
          var cur = (pos.chapter || 0) + 1;
          return lang === 'pl'
            ? ('Rozdział ' + cur + ' z ' + totalCh)
            : ('Chapter '  + cur + ' of ' + totalCh);
        }}
        if (st === 'done') {{
          text = lang === 'pl' ? '✓ Przeczytana' : '✓ Read';
          if (totalCh > 1) text += ' · ' + totalCh + ' ' + (lang === 'pl' ? 'rozdziałów' : 'chapters');
          cls  = 's-done';
        }} else if (st === 'next') {{
          text = lang === 'pl' ? '→ Następna' : '→ Next';
          if (pos && pos.pct > 0) {{
            text += ' · ' + pos.pct + '%';
            var ci = _chInfo(); if (ci) text += ' · ' + ci;
          }}
          cls  = 's-next';
        }} else if (pos && pos.pct > 0) {{
          text = (lang === 'pl' ? '▶ Czytam · ' : '▶ Reading · ') + pos.pct + '%';
          var ci = _chInfo(); if (ci) text += ' · ' + ci;
          if (pos.page > 0 && pos.totalPages > 0) text += ' · ' + (lang === 'pl' ? 'str. ' : 'p. ') + pos.page + '/' + pos.totalPages;
          var at = _fmtTime(pos.audioTime); if (at) text += ' · ♪ ' + at;
          cls  = 's-reading';
        }} else {{
          // Fallback: old-style sf-page-{{bid}} key — at least show "in progress"
          var sfPage = parseInt(localStorage.getItem('sf-page-' + bid) || '0', 10);
          if (sfPage > 0) {{
            text = lang === 'pl' ? '▶ W trakcie' : '▶ In progress';
            cls  = 's-reading';
          }}
        }}
        row.textContent  = text;
        row.className    = 'card-status-row' + (cls ? ' ' + cls : '');
      }});
      // Action buttons
      document.querySelectorAll('.action-btn[data-bid]').forEach(function(btn) {{
        var bid = btn.dataset.bid;
        var st  = _getStatus(bid);
        if (btn.classList.contains('action-done')) btn.classList.toggle('active', st === 'done');
        if (btn.classList.contains('action-next')) btn.classList.toggle('active', st === 'next');
      }});
    }}

    function toggleStatus(btn, newStatus) {{
      var bid     = btn.dataset.bid;
      var current = _getStatus(bid);
      var next    = (current === newStatus) ? '' : newStatus;
      if (next) {{ localStorage.setItem('atlas-status-' + bid, next); }}
      else      {{ localStorage.removeItem('atlas-status-' + bid); }}
      loadBookProgress();
      applyFilters();
    }}

    // ── Filters ────────────────────────────────────────────────────────────────
    function filterStatus(btn, filter) {{
      _activeFilter = filter;
      document.querySelectorAll('.filter-tab').forEach(b => b.classList.toggle('active', b === btn));
      applyFilters();
    }}

    function applyFilters() {{
      var q = (document.getElementById('search').value || '').toLowerCase().trim();
      document.querySelectorAll('.book-card').forEach(function(card) {{
        var bid = card.dataset.id;
        var eff = _effectiveStatus(bid);
        var matchFilter = !_activeFilter || eff === _activeFilter;
        var matchSearch = q.length < 2 || card.textContent.toLowerCase().includes(q);
        card.classList.toggle('hidden', !matchFilter || !matchSearch);
      }});
    }}

    function filterBooks(q) {{ applyFilters(); }}

    // ── Init ───────────────────────────────────────────────────────────────────
    (function() {{ loadBookProgress(); }})();
  </script>
</body>
</html>"""

    out = PUBLIC_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    ok(f"index.html ({out.stat().st_size // 1024} kB, {n} books)")

# ── Build mode ─────────────────────────────────────────────────────────────────
def do_build() -> bool:
    """Full build: copy dist/index.html → book-template.html, publish all, rebuild index."""
    print()
    print(f"  {gold(bold('★ ATLAS BOOKS — BUILD'))}")

    # Step 1: copy Vite output → template
    step("Updating book template")
    dist_html = PROJECT_DIR / "dist" / "index.html"
    if not dist_html.exists():
        err(f"Vite output not found: {dist_html}")
        err("Run: npx vite build  (or npm run build-viewer)")
        return False
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(dist_html, PUBLIC_DIR / "book-template.html")
    ok(f"book-template.html ({(PUBLIC_DIR / 'book-template.html').stat().st_size // 1024} kB)")

    # Step 2: publish all books with published: true in config
    step("Publishing marked books")
    if BOOK_SOURCE.exists():
        published_any = False
        for d in sorted(BOOK_SOURCE.iterdir()):
            if not d.is_dir():
                continue
            cfg_path = d / "config.json"
            if not cfg_path.exists():
                continue
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if cfg.get("published"):
                book_id = d.name
                dest    = BOOK_DEST / book_id
                if dest.exists():
                    publish_book(book_id)
                    published_any = True
                else:
                    info(f"Skipping {book_id} — not generated yet (run: npm run generate -- --input {book_id})")
        if not published_any:
            info("No published books found in books-source/")
    else:
        info("books-source/ not found — skipping")

    # Step 3: rebuild index.html
    step("Rebuilding index.html")
    catalog = load_catalog()
    build_index(catalog)

    print()
    ok(f"Build complete → {PUBLIC_DIR}/")
    print()
    return True

# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("book_id",    nargs="?", default=None)
    parser.add_argument("--all",      action="store_true", help="Publish all books with generated books-dest/")
    parser.add_argument("--rebuild",  action="store_true", help="Rebuild index.html from existing catalog")
    parser.add_argument("--build",    action="store_true", help="Full build (template + all books + index)")
    parser.add_argument("--list",     action="store_true", help="Show catalog")
    parser.add_argument("--remove",   action="store_true", help="Remove book from catalog")
    parser.add_argument("--no-epub",  action="store_true", help="Skip EPUB generation")
    parser.add_argument("--no-pdf",   action="store_true", help="Skip PDF generation")
    parser.add_argument("-h","--help",action="store_true")
    args = parser.parse_args()
    pub_kwargs = dict(do_epub=not args.no_epub, do_pdf=not args.no_pdf)

    if args.help:
        print(__doc__); sys.exit(0)

    if args.build:
        ok_result = do_build()
        sys.exit(0 if ok_result else 1)

    if args.list:
        catalog = load_catalog()
        print(f"\n  {gold(bold('Catalog'))} ({CATALOG_PATH})\n")
        if not catalog:
            print(f"  {gray('(empty)')}\n"); sys.exit(0)
        for e in catalog:
            pub_date = e.get('published', '')
            formats  = ', '.join(e.get('files', {}).keys())
            audio_n  = len(e.get('audio', []))
            print(f"  {gold('►')} {bold(e['id'])}  {dim(e.get('title',''))}")
            print(f"    formats: {formats or gray('none')}  audio: {audio_n} chapters  published: {pub_date}")
        print()
        sys.exit(0)

    if args.rebuild:
        catalog = load_catalog()
        build_index(catalog)
        sys.exit(0)

    if args.all:
        if not BOOK_DEST.exists():
            err(f"books-dest/ not found — run: python generate.py --all first"); sys.exit(1)
        any_done = False
        for d in sorted(BOOK_DEST.iterdir()):
            if not d.is_dir(): continue
            if not (d / "book.md").exists(): continue
            print()
            print(f"  {gold(bold('Publishing'))}: {bold(d.name)}")
            publish_book(d.name, **pub_kwargs)
            any_done = True
        if any_done:
            catalog = load_catalog()
            step("Rebuilding index.html")
            build_index(catalog)
        else:
            info("No generated books found in books-dest/")
            info("Run: python generate.py --all")
        sys.exit(0)

    if not args.book_id:
        print(__doc__); sys.exit(0)

    book_id = args.book_id

    if args.remove:
        catalog = load_catalog()
        catalog = [e for e in catalog if e["id"] != book_id]
        save_catalog(catalog)
        ok(f"Removed '{book_id}' from catalog")
        build_index(catalog)
        sys.exit(0)

    # Publish single book
    print()
    print(f"  {gold(bold('★ ATLAS BOOKS — PUBLISH'))}: {bold(book_id)}")
    ok_result = publish_book(book_id, **pub_kwargs)
    if ok_result:
        catalog = load_catalog()
        step("Rebuilding index.html")
        build_index(catalog)
        print()
        ok(f"Done → {PUBLIC_DIR}/{book_id}.html")
    print()
    sys.exit(0 if ok_result else 1)


if __name__ == "__main__":
    main()
