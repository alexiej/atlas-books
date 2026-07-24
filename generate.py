#!/usr/bin/env python3
"""
generate.py — Build a book for the Atlas Books / Star Federation platform

Source directory (books-source/<name>/):
  config.json              — metadata + TTS settings
  book.md                  — content (primary format); generated if missing
  cover.jpg / cover.png    — cover image (also: book.jpg / book.png)
  ch01.mp3, ch02.mp3 ...  — input audio per chapter (or book.mp3 for whole book)
  audio/                   — processed audio output (ch01-part000.mp3, etc.)
  tts-audio-{lang}.json    — chapter→audio map (written by generate.py)

Usage:
  python generate.py star-federation
  python generate.py star-federation --replace    # re-generate book.md even if it exists
  python generate.py star-federation --no-tts
  python generate.py --all
  python list.py

config.json example:
  {
    "id": "star-federation",
    "title": "Star Federation",
    "author": "Arkadiusz Klemenko",
    "year": "2026",
    "lang": "pl",
    "description": "...",
    "published": false,
    "tts": {
      "voice": "pl-PL-MarekNeural",
      "rate": "+0%",
      "pitch": "+0Hz"
    }
  }
"""

from __future__ import annotations
import argparse, asyncio, base64, json, math, re, shutil, subprocess, sys
from html import escape
from pathlib import Path

# ── MP3 split threshold ────────────────────────────────────────────────────────
# Files larger than this are split into parts so they stay under GitHub's push limits.
MP3_PART_MAX_BYTES = 17 * 1024 * 1024   # 17 MB per part (GitHub warns at 25 MB, blocks at 100 MB)

# ── Paths ──────────────────────────────────────────────────────────────────────
PROJECT_DIR   = Path(__file__).resolve().parent
BOOK_SOURCE   = PROJECT_DIR / "books-source"
PUBLIC_DIR    = PROJECT_DIR / "public"
TEMPLATE_PATH = PUBLIC_DIR / "book-template.html"

# ── ANSI colour helpers ────────────────────────────────────────────────────────
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

# ── Help ───────────────────────────────────────────────────────────────────────
def print_help() -> None:
    print()
    print(f"  {gold(bold('★ ATLAS BOOKS — GENERATE'))}   {dim('tools/generate-book.py')}")
    print()
    print(f"  {bold('USAGE')}")
    def cmd(s: str, c: str = "") -> None:
        suffix = f"  {gray('# ' + c)}" if c else ""
        print(f"    {cyan(s)}{suffix}")
    cmd("python list.py",                              "list all books in books-source/")
    cmd("python generate.py star-federation",          "parse source → book.md + audio/ in books-source/<name>/")
    cmd("python generate.py star-federation --replace","re-generate book.md even if already present")
    cmd("python generate.py star-federation --no-tts", "skip audio processing")
    cmd("python generate.py --all",                    "process all books")
    cmd("python publish.py star-federation",           "books-source/ → HTML/EPUB/PDF → public/<name>/")
    cmd("python generate-tts.py star-federation",      "generate ch01.mp3 etc. (separate step)")
    print()
    print(f"  {bold('PIPELINE')}")
    print(f"    1. {cyan('python generate-tts.py <name>')}   {gray('# TTS → books-source/<name>/ch01.mp3 ...')}")
    print(f"    2. {cyan('python generate.py <name>')}       {gray('# book.md + audio/ → books-source/<name>/')}")
    print(f"    3. {cyan('python analyze-tts.py <name>')}    {gray('# Whisper alignment → tts-transcript-align.json')}")
    print(f"    4. {cyan('python publish.py <name>')}        {gray('# → public/<name>/index.html')}")
    print()
    print(f"  {bold('FLAGS')}")
    cmd("--replace",            "re-generate book.md even if it already exists")
    cmd("--no-tts",             "skip audio processing entirely")
    print()
    print(f"  {bold('TTS')}")
    print(f"    Requires: {cyan('pip install edge-tts')}")
    print()
    def voice(short: str, gender: str, locale: str, tags: str) -> None:
        g = "♀" if gender == "Female" else "♂"
        print(f"    {cyan(short):<42} {g}  {dim(tags)}")
    print(f"  {bold('  pl-PL')}")
    voice("pl-PL-ZofiaNeural",       "Female", "pl-PL", "Friendly, Positive")
    voice("pl-PL-MarekNeural",       "Male",   "pl-PL", "Friendly, Positive")
    print()
    print(f"  {bold('  en-US')}")
    voice("en-US-AriaNeural",        "Female", "en-US", "Positive, Confident  [News, Novel]")
    voice("en-US-JennyNeural",       "Female", "en-US", "Friendly, Considerate  [Conversation, News]")
    voice("en-US-MichelleNeural",    "Female", "en-US", "Friendly, Pleasant  [News, Novel]")
    voice("en-US-AnaNeural",         "Female", "en-US", "Cute  [Cartoon, Conversation]")
    voice("en-US-ChristopherNeural", "Male",   "en-US", "Reliable, Authority  [News, Novel]")
    voice("en-US-EricNeural",        "Male",   "en-US", "Rational  [News, Novel]")
    voice("en-US-GuyNeural",         "Male",   "en-US", "Passion  [News, Novel]")
    voice("en-US-RogerNeural",       "Male",   "en-US", "Lively  [News, Novel]")
    voice("en-US-SteffanNeural",     "Male",   "en-US", "Rational  [News, Novel]")
    print()
    print(f"  {bold('  en-GB')}")
    voice("en-GB-LibbyNeural",       "Female", "en-GB", "Friendly, Positive")
    voice("en-GB-MaisieNeural",      "Female", "en-GB", "Friendly, Positive")
    voice("en-GB-SoniaNeural",       "Female", "en-GB", "Friendly, Positive")
    voice("en-GB-RyanNeural",        "Male",   "en-GB", "Friendly, Positive")
    voice("en-GB-ThomasNeural",      "Male",   "en-GB", "Friendly, Positive")
    print()
    print(f"  {bold('DIRECTORIES')}")
    cmd("books-source/<name>/",      "everything: config.json + book.md + ch01.mp3 + audio/ + tts-audio-*.json")
    cmd("public/<name>/",           "deploy: run publish.py to copy here")
    print()

# ── Config ─────────────────────────────────────────────────────────────────────
def load_config(book_dir: Path) -> dict:
    cfg_path = book_dir / "config.json"
    if not cfg_path.exists():
        raise FileNotFoundError(f"Missing config.json in {book_dir}")
    return json.loads(cfg_path.read_text(encoding="utf-8"))

def save_config(book_dir: Path, config: dict) -> None:
    cfg_path = book_dir / "config.json"
    cfg_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

# ── Cover ──────────────────────────────────────────────────────────────────────
COVER_NAMES = ["cover.jpg", "cover.png", "book.jpg", "book.png"]

def load_cover_b64(book_dir: Path) -> str:
    for name in COVER_NAMES:
        p = book_dir / name
        if p.exists():
            mime = "image/jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "image/png"
            data = base64.b64encode(p.read_bytes()).decode()
            info(f"Cover: {name} ({p.stat().st_size // 1024} kB)")
            return f"data:{mime};base64,{data}"
    return ""

# ── Markdown parser ────────────────────────────────────────────────────────────
def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_-]+', '-', text)
    return re.sub(r'^-+|-+$', '', text) or 'chapter'

def _strip_inline(text: str) -> str:
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'_(.+?)_', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    return text.strip()

def _collapse_letterspace(text: str) -> str:
    """Collapse PDF letter-spaced text: 'K s i ę g a t r z e c i a' → 'Księgatrzecia'.

    PDFs with decorative letter-spacing export each glyph separated by a space.
    We detect this when ≥ 70 % of space-separated tokens are single characters
    and collapse them by removing all spaces.
    """
    tokens = text.split()
    if len(tokens) < 3:
        return text
    single = sum(1 for t in tokens if len(t) == 1)
    if single / len(tokens) >= 0.7:
        return ''.join(tokens)
    return text

def parse_markdown(content: str) -> tuple[dict, list[dict]]:
    """Parse markdown → (front_matter_meta, chapters_list)."""
    raw_lines = content.splitlines()

    # Front matter
    meta: dict = {}
    lines = raw_lines
    if lines and lines[0].strip() == '---':
        i = 1
        while i < len(lines) and lines[i].strip() != '---':
            if ':' in lines[i]:
                key, _, val = lines[i].partition(':')
                meta[key.strip()] = val.strip()
            i += 1
        lines = lines[i + 1:]

    chapters: list[dict] = []
    current: dict | None = None
    chapter_num = 0
    chapter_subtitle_set = False
    skip_next = False
    in_quote: list[str] = []

    # Smart chapter detection: if H2 headings appear BEFORE the first H1,
    # the file mixes H1/H2 as the same semantic level (e.g. PDF conversion artifact).
    # In that case treat H2 as chapter breaks too.
    _h1_positions = [i for i, l in enumerate(lines) if l.strip().startswith('# ') and not l.strip().startswith('## ')]
    _h2_positions = [i for i, l in enumerate(lines) if l.strip().startswith('## ') and not l.strip().startswith('### ')]
    _first_h1 = _h1_positions[0] if _h1_positions else len(lines)
    _h2_before_h1 = any(p < _first_h1 for p in _h2_positions)
    # Also promote H2 when there are very few H1s (≤3) and H2s outnumber them
    _h2_as_chapter = _h2_before_h1 or (len(_h1_positions) <= 3 and len(_h2_positions) > len(_h1_positions))

    def flush_quote():
        nonlocal in_quote
        if in_quote and current is not None:
            texts, attr = [], None
            for ql in in_quote:
                content_q = re.sub(r'^>\s?', '', ql)
                m = re.match(r'^[—–-]\s*(.+)', content_q.strip())
                if m:
                    attr = m.group(1).strip()
                else:
                    texts.append(content_q)
            block: dict = {'type': 'quote', 'text': ' '.join(t for t in texts if t).strip()}
            if attr:
                block['attribution'] = attr
            current['blocks'].append(block)
        in_quote = []

    i = 0
    while i < len(lines):
        if skip_next:
            skip_next = False
            i += 1
            continue

        line  = lines[i]
        strip = line.strip()

        # Verse fence
        if strip.startswith('```verse'):
            flush_quote()
            i += 1
            verse_lines = []
            while i < len(lines) and not lines[i].strip().startswith('```'):
                verse_lines.append(lines[i].rstrip())
                i += 1
            i += 1
            if current is not None:
                current['blocks'].append({'type': 'verse', 'text': '\n'.join(verse_lines)})
            continue

        # Callout :::
        if re.match(r'^:::', strip):
            flush_quote()
            m = re.match(r'^:::\s*(\S+)\s*(.*)', strip)
            if m:
                label = m.group(1)
                title_label = m.group(2).strip()
                call_lines = []
                i += 1
                while i < len(lines) and not lines[i].strip().startswith(':::'):
                    call_lines.append(lines[i].rstrip())
                    i += 1
                text = '\n'.join(call_lines).strip()
                block = {'type': 'callout', 'text': text}
                if title_label:
                    block['label'] = title_label
                elif label != 'callout':
                    block['label'] = label
                if current is not None:
                    current['blocks'].append(block)
            i += 1
            continue

        # Blockquote
        if strip.startswith('>'):
            in_quote.append(line)
            i += 1
            continue
        elif in_quote:
            flush_quote()

        # Divider
        if re.match(r'^[-*_]{3,}$', strip):
            if current is not None:
                current['blocks'].append({'type': 'divider'})
            i += 1
            continue

        # H1 = new chapter
        if strip.startswith('# ') and not strip.startswith('## '):
            if current is not None:
                chapters.append(current)
            chapter_num += 1
            title = _collapse_letterspace(_strip_inline(strip[2:]))
            current = {'id': slugify(title), 'number': chapter_num, 'title': title, 'blocks': []}
            chapter_subtitle_set = False
            i += 1
            continue

        # H2 = chapter break (when promoted) OR section heading / subtitle
        if strip.startswith('## ') and not strip.startswith('### '):
            text = _collapse_letterspace(_strip_inline(strip[3:]))
            if _h2_as_chapter:
                # Treat H2 as a new chapter (same level as H1)
                if current is not None:
                    chapters.append(current)
                chapter_num += 1
                current = {'id': slugify(text), 'number': chapter_num, 'title': text, 'blocks': []}
                chapter_subtitle_set = False
            elif current is not None and not chapter_subtitle_set and not current['blocks']:
                current['subtitle'] = text
                chapter_subtitle_set = True
            elif current is not None:
                current['blocks'].append({'type': 'heading', 'text': text})
            i += 1
            continue

        # H3-H6 — all become inline headings (sub-section labels)
        if strip.startswith('### ') or strip.startswith('#### ') or \
           strip.startswith('##### ') or strip.startswith('###### '):
            # Strip all leading '#' and the following space
            raw = strip.lstrip('#').lstrip()
            if current is not None:
                current['blocks'].append({'type': 'heading', 'text': _strip_inline(raw)})
            i += 1
            continue

        # Image
        if strip.startswith('!['):
            m = re.match(r'!\[([^\]]*)\]\(([^)]+)\)', strip)
            if m and current is not None:
                next_line = lines[i + 1] if i + 1 < len(lines) else None
                block = {'type': 'image', 'alt': m.group(1), 'url': m.group(2)}
                if next_line:
                    cap = re.match(r'^\*(.+)\*$', next_line.strip())
                    if cap:
                        block['caption'] = cap.group(1)
                        skip_next = True
                current['blocks'].append(block)
            i += 1
            continue

        # Paragraph
        if strip and current is not None:
            para = [strip]
            j = i + 1
            while j < len(lines):
                ns = lines[j].strip()
                if (not ns or ns.startswith('#') or ns.startswith('>')
                        or ns.startswith('![') or ns.startswith(':::')
                        or re.match(r'^[-*_]{3,}$', ns)):
                    break
                para.append(ns)
                j += 1
            current['blocks'].append({'type': 'paragraph', 'text': _strip_inline(' '.join(para))})
            i = j
            continue

        i += 1

    flush_quote()
    if current is not None:
        chapters.append(current)

    return meta, chapters

def md_to_chapters(path: Path) -> list[dict]:
    _, chapters = parse_markdown(path.read_text(encoding="utf-8"))
    return chapters

def epub_to_chapters(path: Path) -> list[dict]:
    try:
        import ebooklib as eblib_top
        from ebooklib import epub as eblib
        from html.parser import HTMLParser

        # Files to skip by name fragment (boilerplate / nav / fundraising / annotations)
        _SKIP_NAMES = ("nav", "toc", "cover", "ncx", "title", "support", "fund", "last",
                       "annotation")

        class _DocParser(HTMLParser):
            BLOCK = {"h1", "h2", "h3", "h4", "p", "blockquote", "li"}
            SKIP  = {"style", "script", "nav", "head"}

            def __init__(self) -> None:
                super().__init__()
                self.items: list[dict] = []
                self._skip_depth  = 0
                self._cur_tag     = ""
                self._cur: list[str] = []
                # stanza/verse tracking (for poetry EPUBs like WolneLektury)
                self._div_stack: list[str] = []   # class attr of each open <div>
                self._verse_lines: list[str] = []  # lines collected in current stanza
                self._verse_cur:   list[str] = []  # chars in the current verse line

            @staticmethod
            def _cls(attrs: list) -> str:
                for k, v in attrs:
                    if k == "class":
                        return v or ""
                return ""

            @staticmethod
            def _has(cls: str, name: str) -> bool:
                """True if 'name' is an exact word in the space-separated class string."""
                return name in cls.split()

            def _in_stanza(self) -> bool:
                return any(self._has(c, "stanza") for c in self._div_stack)

            def _top_is_verse(self) -> bool:
                return bool(self._div_stack) and self._has(self._div_stack[-1], "verse")

            def handle_starttag(self, tag: str, attrs: list) -> None:
                if tag in self.SKIP:
                    self._skip_depth += 1
                if self._skip_depth:
                    return

                if tag == "div":
                    cls = self._cls(attrs)
                    self._div_stack.append(cls)
                    if self._has(cls, "verse") and self._in_stanza():
                        self._verse_cur = []
                elif not self._in_stanza() and tag in self.BLOCK:
                    self._cur_tag = tag
                    self._cur = []

            def handle_endtag(self, tag: str) -> None:
                if tag in self.SKIP:
                    self._skip_depth = max(0, self._skip_depth - 1)
                if self._skip_depth:
                    return

                if tag == "div" and self._div_stack:
                    closing_cls = self._div_stack[-1]
                    if self._has(closing_cls, "verse") and self._in_stanza():
                        # close a verse line
                        line = " ".join(" ".join(self._verse_cur).split()).strip()
                        if line:
                            self._verse_lines.append(line)
                        self._verse_cur = []
                    elif self._has(closing_cls, "stanza"):
                        # close a stanza → emit verse block
                        if self._verse_lines:
                            self.items.append({"tag": "stanza", "text": "\n".join(self._verse_lines)})
                            self._verse_lines = []
                    self._div_stack.pop()
                elif not self._in_stanza() and tag in self.BLOCK and self._cur_tag:
                    text = " ".join(" ".join(self._cur).split()).strip()
                    if text:
                        self.items.append({"tag": self._cur_tag, "text": text})
                    self._cur_tag = ""
                    self._cur = []

            def handle_data(self, data: str) -> None:
                if self._skip_depth:
                    return
                if self._top_is_verse():
                    self._verse_cur.append(data)
                elif not self._in_stanza() and self._cur_tag:
                    self._cur.append(data)

        ebook    = eblib.read_epub(str(path))
        chapters: list[dict] = []
        ch_num   = 0

        for item in ebook.get_items_of_type(eblib_top.ITEM_DOCUMENT):
            name = (item.get_name() or "").lower()
            # Skip boilerplate / nav / fundraising pages by filename
            if any(x in name for x in _SKIP_NAMES):
                continue
            content = item.get_content().decode("utf-8", errors="replace")
            # Also skip <nav epub:type=…> blocks
            if "<nav" in content and "epub:type" in content:
                continue

            parser = _DocParser()
            parser.feed(content)
            if not parser.items:
                continue

            # First heading → chapter title
            title      = ""
            body_start = 0
            for i, it in enumerate(parser.items):
                if it["tag"] in ("h1", "h2", "h3"):
                    title      = it["text"]
                    body_start = i + 1
                    break

            blocks: list[dict] = []
            for it in parser.items[body_start:]:
                tag, text = it["tag"], it["text"]
                if tag == "stanza":
                    blocks.append({"type": "verse",     "text": text})
                elif tag in ("h3", "h4"):
                    blocks.append({"type": "heading",   "text": text})
                elif tag == "blockquote":
                    blocks.append({"type": "quote",     "text": text})
                else:
                    blocks.append({"type": "paragraph", "text": text})

            if not blocks and not title:
                continue

            ch_num += 1
            chapters.append({
                "id":     f"ch{ch_num}",
                "number": ch_num,
                "title":  title or f"Część {ch_num}",
                "blocks": blocks,
            })

        return chapters
    except ImportError:
        err("ebooklib not found — install: pip install ebooklib lxml")
        return []

def pdf_to_chapters(path: Path) -> list[dict]:
    try:
        import pypdf
        reader = pypdf.PdfReader(str(path))
        all_text = "\n\n".join(pg.extract_text() or "" for pg in reader.pages)
        blocks = [{"type": "paragraph", "text": p.strip()} for p in all_text.split('\n\n') if p.strip()]
        return [{"id": "ch1", "number": 1, "title": "Content", "blocks": blocks}]
    except ImportError:
        err("pypdf not found — install: pip install pypdf")
        return []

# ── HTML injection ─────────────────────────────────────────────────────────────
def inject_into_template(book_data: dict, template: Path, output: Path) -> None:
    html      = template.read_text(encoding="utf-8")
    book_json = json.dumps(book_data, ensure_ascii=False, separators=(',', ':'))

    # Use a lambda so re.sub does NOT process backslash sequences in book_json
    # (re.sub would turn JSON \n into literal newlines when given a plain string replacement)
    replacement = (
        f'<!--@@BOOK_DATA_START@@-->\n'
        f'    <script>window.__BOOK_DATA__ = {book_json};</script>\n'
        f'    <!--@@BOOK_DATA_END@@-->'
    )
    html = re.sub(
        r'<!--@@BOOK_DATA_START@@-->.*?<!--@@BOOK_DATA_END@@-->',
        lambda _: replacement,
        html, flags=re.DOTALL,
    )
    if 'window.__BOOK_DATA__ = null' in html:
        html = html.replace('window.__BOOK_DATA__ = null;', f'window.__BOOK_DATA__ = {book_json};')

    title = book_data.get("title", "Book")
    html  = re.sub(r'<title>.*?</title>', f'<title>{escape(title)}</title>', html)
    output.write_text(html, encoding="utf-8")

# ── TTS ────────────────────────────────────────────────────────────────────────
QUOTES = {"pl": ("„", "”"), "en": ("“", "”")}

def _build_tts_parts(chapter: dict, lang: str) -> list[tuple[str, dict]]:
    q_open, q_close = QUOTES.get(lang, ('"', '"'))
    parts: list[tuple[str, dict]] = []

    number   = chapter.get("number", "")
    title    = (chapter.get("title") or "").strip()
    subtitle = (chapter.get("subtitle") or "").strip()

    if isinstance(number, int):
        intro = f"Rozdział {number}." if lang == "pl" else f"Chapter {number}."
    else:
        intro = f"{number}." if number else ""

    if intro:    parts.append((intro,           {"type": "intro"}))
    if title:    parts.append((f"{title}.",     {"type": "intro"}))
    if subtitle: parts.append((f"{subtitle}.",  {"type": "intro"}))

    for bi, block in enumerate(chapter.get("blocks", [])):
        btype = block.get("type", "")
        text  = (block.get("text") or "").strip()
        if not text:
            continue
        if btype == "paragraph":
            parts.append((text, {"type": "block", "bi": bi, "part": "text"}))
        elif btype == "heading":
            parts.append((f"  {text}.", {"type": "block", "bi": bi, "part": "text"}))
        elif btype == "quote":
            parts.append((f"{q_open}{text}{q_close}", {"type": "block", "bi": bi, "part": "text"}))
            attr = (block.get("attribution") or "").strip()
            if attr:
                parts.append((f"— {attr}.", {"type": "block", "bi": bi, "part": "attr"}))
        elif btype in ("callout", "verse"):
            label = (block.get("label") or "").strip()
            if label:
                parts.append((f"{label}:", {"type": "block", "bi": bi, "part": "label"}))
            parts.append((text, {"type": "block", "bi": bi, "part": "text"}))

    return parts

def _build_word_sources(parts: list[tuple[str, dict]]) -> list[dict]:
    sources: list[dict] = []
    for text_frag, src in parts:
        for wi, _ in enumerate(text_frag.split()):
            entry = dict(src)
            if entry.get("type") == "block":
                entry["wi"] = wi
            sources.append(entry)
    return sources

def _merge_timing(timing: list[dict], sources: list[dict]) -> list[dict]:
    merged: list[dict] = []
    n = min(len(timing), len(sources))
    for i in range(n):
        entry = dict(timing[i])
        entry.update(sources[i])
        merged.append(entry)
    for i in range(n, len(timing)):
        entry = dict(timing[i])
        entry["type"] = "intro"
        merged.append(entry)
    return merged

async def _synthesize(text: str, voice: str, rate: str, pitch: str, volume: str) -> tuple[bytes, list[dict]]:
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume)
    audio_chunks: list[bytes] = []
    timing_events: list[dict] = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_chunks.append(chunk["data"])
        elif chunk["type"] == "WordBoundary":
            s = round(chunk["offset"] / 10_000_000, 3)
            e = round((chunk["offset"] + chunk["duration"]) / 10_000_000, 3)
            timing_events.append({"w": chunk["text"], "s": s, "e": e})
    return b"".join(audio_chunks), timing_events

async def generate_tts(book_data: dict, book_id: str, dest: Path,
                       voice: str, rate: str, pitch: str, volume: str, lang: str) -> None:
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        err("edge-tts not found — install: pip install edge-tts")
        return

    chapters = book_data.get("chapters", [])
    for i, chapter in enumerate(chapters):
        ch_id    = chapter.get("id", f"ch{i+1}")
        mp3_name = f"{book_id}-{ch_id}.mp3"
        mp3_path = dest / mp3_name

        info(f"TTS {i+1}/{len(chapters)}: {chapter.get('title','')}")
        try:
            parts   = _build_tts_parts(chapter, lang)
            sources = _build_word_sources(parts)
            full_text = " ".join(p[0] for p in parts)
            audio_bytes, timing_events = await _synthesize(full_text, voice, rate, pitch, volume)
            mp3_path.write_bytes(audio_bytes)
            merged = _merge_timing(timing_events, sources)
            if lang == 'pl':
                chapter['timing_pl'] = merged
                chapter['audio_pl']  = mp3_name
            else:
                chapter['timing_en'] = merged
                chapter['audio_en']  = mp3_name
            ok(f"Audio: {mp3_name} ({len(audio_bytes)//1024} kB, {len(merged)} words)")
        except Exception as e:
            err(f"TTS error: {e}")

# ── Export CSS (PDF-safe — no Google Fonts) ────────────────────────────────────
PDF_CSS = """
@page {
  margin: 2.5cm 3cm;
  size: A5;
}
@page cover-page {
  margin: 0;
  size: A5;
}
.cover-page {
  page: cover-page;
  page-break-after: always;
  position: relative;
  width: 148mm;
  height: 210mm;
  overflow: hidden;
  background: #111;
}
.cover-page > img {
  position: absolute;
  top: 0; left: 0;
  width: 148mm;
  height: 210mm;
  object-fit: cover;
}
.cover-overlay {
  position: absolute;
  bottom: 0; left: 0; right: 0;
  padding: 5rem 2rem 2.5rem;
  background: linear-gradient(to bottom, transparent 0%, rgba(0,0,0,0.82) 50%);
  text-align: center;
}
.co-author {
  font-family: 'Courier New', Courier, monospace;
  font-size: 7pt;
  letter-spacing: 0.45em;
  text-transform: uppercase;
  color: #c9a84c;
  margin-bottom: 0.5rem;
}
.co-title {
  font-family: Georgia, 'Times New Roman', serif;
  font-size: 2em;
  font-weight: bold;
  color: #ffffff;
  line-height: 1.2;
  margin-bottom: 0.5rem;
}
.co-year {
  font-family: 'Courier New', Courier, monospace;
  font-size: 7pt;
  letter-spacing: 0.3em;
  color: rgba(255,255,255,0.5);
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: Georgia, 'Times New Roman', Times, serif;
  font-size: 11pt;
  line-height: 1.75;
  color: #1a1714;
  background: white;
}

.chapter {
  page-break-before: always;
  margin-bottom: 3rem;
}
.chapter:first-of-type { page-break-before: auto; }

.chapter-number {
  display: block;
  font-family: 'Courier New', Courier, monospace;
  font-size: 7pt;
  letter-spacing: 0.4em;
  text-transform: uppercase;
  color: #8a6f2e;
  margin-bottom: 0.5rem;
}
.chapter-title {
  font-size: 1.8em;
  font-weight: bold;
  font-style: italic;
  line-height: 1.15;
  color: #1a1714;
  margin-bottom: 0.35rem;
}
.chapter-subtitle {
  font-family: Helvetica, Arial, sans-serif;
  font-size: 8pt;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: #6b6257;
  margin-bottom: 1rem;
}
.chapter-rule {
  width: 36px;
  height: 1px;
  border: none;
  border-top: 1px solid rgba(138,111,46,0.4);
  margin: 1rem 0 2rem;
}

.block + .block { margin-top: 1.1em; }

.block-paragraph {
  color: #1a1714;
  font-size: 1em;
  line-height: 1.75;
  text-align: justify;
  hyphens: auto;
  -webkit-hyphens: auto;
}
.dc {
  float: left;
  font-size: 3.1em;
  line-height: 0.78;
  margin-right: 0.06em;
  margin-top: 0.04em;
  color: #8a6f2e;
  font-weight: bold;
  font-style: italic;
}
.block-paragraph.drop-cap-para {
  text-align: left;
}
.block-paragraph.indent {
  text-indent: 1.5em;
}

.block-heading {
  font-family: 'Courier New', Courier, monospace;
  font-size: 7pt;
  font-weight: bold;
  letter-spacing: 0.3em;
  text-transform: uppercase;
  color: #8a6f2e;
  margin-top: 2.5rem;
  padding-top: 1.25rem;
  border-top: 1px solid rgba(138,111,46,0.3);
  page-break-after: avoid;
}

.block-quote {
  border-left: 2px solid rgba(138,111,46,0.4);
  padding: 0.75rem 1.1rem;
  background: rgba(138,111,46,0.05);
  page-break-inside: avoid;
}
.block-quote-text { font-style: italic; font-size: 1.02em; line-height: 1.7; }
.block-quote-attr {
  display: block;
  font-family: Helvetica, Arial, sans-serif;
  font-size: 8pt;
  letter-spacing: 0.1em;
  color: #8a6f2e;
  text-transform: uppercase;
  margin-top: 0.4rem;
}

.block-divider {
  text-align: center;
  font-size: 10pt;
  color: #6b6257;
  letter-spacing: 0.3em;
  padding: 0.5rem 0;
}

.block-callout {
  border-left: 2px solid #4a6fa5;
  padding: 0.8rem 1rem;
  background: rgba(74,111,165,0.05);
  page-break-inside: avoid;
}
.block-callout-label {
  font-family: 'Courier New', Courier, monospace;
  font-size: 7pt;
  letter-spacing: 0.25em;
  color: #4a6fa5;
  text-transform: uppercase;
  margin-bottom: 0.35rem;
}
.block-callout-text { font-size: 0.94em; line-height: 1.7; }

.block-verse {
  font-style: italic;
  white-space: pre-wrap;
  color: #6b6257;
  line-height: 1.9;
  padding-left: 1.5rem;
  border-left: 1px solid rgba(120,105,80,0.25);
}

figure { margin: 1.5rem 0; page-break-inside: avoid; }
figure img { width: 100%; display: block; }
figcaption {
  margin-top: 0.4rem;
  font-family: Helvetica, Arial, sans-serif;
  font-size: 8pt;
  letter-spacing: 0.08em;
  color: #6b6257;
  text-align: center;
}
"""

def _block_to_html(block: dict, idx: int, prev_type: str | None) -> str:
    btype = block.get("type", "")
    raw   = block.get("text") or ""
    text  = escape(raw)

    if btype == "paragraph":
        cls = "block block-paragraph"
        if idx == 0 and raw:
            first = escape(raw[0])
            rest  = escape(raw[1:])
            text  = f'<span class="dc">{first}</span>{rest}'
            cls  += " drop-cap-para"
        elif prev_type == "paragraph":
            cls += " indent"
        return f'<p class="{cls}">{text}</p>'

    if btype == "heading":
        return f'<h2 class="block block-heading">{text}</h2>'

    if btype == "quote":
        attr = block.get("attribution") or ""
        attr_html = f'<cite class="block-quote-attr">— {escape(attr)}</cite>' if attr else ""
        return (f'<blockquote class="block block-quote">'
                f'<p class="block-quote-text">{text}</p>{attr_html}'
                f'</blockquote>')

    if btype == "divider":
        return '<div class="block block-divider">✦ ✦ ✦</div>'

    if btype == "callout":
        label = block.get("label") or ""
        label_html = f'<div class="block-callout-label">{escape(label)}</div>' if label else ""
        return (f'<div class="block block-callout">'
                f'{label_html}<p class="block-callout-text">{text}</p></div>')

    if btype == "verse":
        return f'<pre class="block block-verse">{text}</pre>'

    if btype == "image":
        url = escape(block.get("url") or "")
        alt = escape(block.get("alt") or "")
        cap = block.get("caption") or ""
        cap_html = f'<figcaption>{escape(cap)}</figcaption>' if cap else ""
        return f'<figure><img src="{url}" alt="{alt}"/>{cap_html}</figure>'

    return ""

def _chapter_to_html(chapter: dict, lang: str) -> str:
    num      = chapter.get("number", "")
    title    = chapter.get("title", "")
    subtitle = chapter.get("subtitle", "")
    blocks   = chapter.get("blocks", [])

    num_label = (f"Rozdział {num}" if lang == "pl" else f"Chapter {num}") if isinstance(num, int) else str(num)
    sub_html  = f'<p class="chapter-subtitle">{escape(subtitle)}</p>' if subtitle else ""

    header = (f'<span class="chapter-number">{escape(num_label)}</span>\n'
              f'<h1 class="chapter-title">{escape(title)}</h1>\n'
              f'{sub_html}\n'
              f'<hr class="chapter-rule"/>\n')

    parts = [header]
    prev_type: str | None = None
    for i, block in enumerate(blocks):
        h = _block_to_html(block, i, prev_type)
        if h:
            parts.append(h)
        btype = block.get("type", "")
        if btype not in ("divider", "image"):
            prev_type = btype

    return "\n".join(parts)

def _book_to_html(book: dict) -> str:
    title    = book.get("title", "Book")
    author   = book.get("author", "")
    subtitle = book.get("subtitle", "")
    lang     = book.get("lang", "en")
    chapters = book.get("chapters", [])
    cover    = book.get("cover", "")

    year = book.get("year", "")
    cover_page_html = ""
    if cover and cover.startswith("data:"):
        ca = f'<p class="co-author">{escape(author)}</p>' if author else ''
        cy = f'<p class="co-year">{escape(year)}</p>' if year else ''
        cover_page_html = (
            f'<div class="cover-page">'
            f'<img src="{cover}" alt="Cover"/>'
            f'<div class="cover-overlay">'
            f'{ca}'
            f'<h1 class="co-title">{escape(title)}</h1>'
            f'{cy}'
            f'</div>'
            f'</div>'
        )

    author_html   = f'<p class="book-author">{escape(author)}</p>' if author else ''
    subtitle_html = f'<p class="book-subtitle">{escape(subtitle)}</p>' if subtitle else ''
    header_html = (
        f'<header class="book-header">\n'
        f'  {author_html}\n'
        f'  <h1 class="book-title">{escape(title)}</h1>\n'
        f'  {subtitle_html}\n'
        f'</header>'
    )

    chapters_html = "\n\n".join(
        f'<section class="chapter" id="ch-{ch.get("id","")}">\n'
        f'{_chapter_to_html(ch, lang)}\n'
        f'</section>'
        for ch in chapters
    )

    return f"""<!DOCTYPE html>
<html lang="{escape(lang)}">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{escape(title)}</title>
  <style>{PDF_CSS}
.book-header {{
  text-align: center;
  padding: 3rem 0 4rem;
  border-bottom: 1px solid rgba(138,111,46,0.3);
  margin-bottom: 3rem;
  page-break-after: always;
}}
.book-author {{
  font-family: 'Courier New', monospace;
  font-size: 7pt;
  letter-spacing: 0.4em;
  text-transform: uppercase;
  color: #8a6f2e;
  margin-bottom: 0.75rem;
}}
.book-title {{
  font-size: 2.2em;
  font-weight: bold;
  font-style: italic;
  color: #1a1714;
}}
.book-subtitle {{
  font-family: Helvetica, Arial, sans-serif;
  font-size: 8pt;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: #6b6257;
  margin-top: 0.5rem;
}}
  </style>
</head>
<body>
  {cover_page_html}
  {header_html}
  {chapters_html}
</body>
</html>"""

# ── EPUB export ────────────────────────────────────────────────────────────────
def make_epub(book: dict, output: Path) -> None:
    try:
        from ebooklib import epub
    except ImportError:
        err("ebooklib not found — install: pip install ebooklib lxml")
        return

    title    = book.get("title", "Book")
    author   = book.get("author", "Unknown")
    lang     = book.get("lang", "en")
    book_id  = book.get("id", "book")
    chapters = book.get("chapters", [])

    ebook = epub.EpubBook()
    ebook.set_identifier(book_id)
    ebook.set_title(title)
    ebook.set_language(lang)
    if author:
        ebook.add_author(author)

    # Cover
    cover_raw = book.get("cover", "")
    if cover_raw and cover_raw.startswith("data:"):
        try:
            mime, b64 = cover_raw.split(";base64,", 1)
            mime = mime.split(":", 1)[1]
            ext  = "jpg" if "jpeg" in mime else "png"
            ebook.set_cover(f"cover.{ext}", base64.b64decode(b64))
        except Exception:
            pass

    css_item = epub.EpubItem(uid="style", file_name="style.css",
                              media_type="text/css", content=PDF_CSS.encode("utf-8"))
    ebook.add_item(css_item)

    spine, toc_list = ["nav"], []
    for ch in chapters:
        ch_id    = str(ch.get("id", "ch"))
        ch_title = ch.get("title", "")
        fname    = f"chapter_{ch_id}.xhtml"
        body_html = _chapter_to_html(ch, lang)
        content   = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">'
            f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}">'
            f'<head><title>{escape(ch_title)}</title>'
            f'<link rel="stylesheet" type="text/css" href="style.css"/>'
            f'</head><body><div class="chapter">{body_html}</div></body></html>'
        )
        epub_ch = epub.EpubHtml(title=ch_title, file_name=fname, lang=lang)
        epub_ch.content = content.encode("utf-8")
        epub_ch.add_item(css_item)
        ebook.add_item(epub_ch)
        spine.append(epub_ch)
        toc_list.append(epub.Link(fname, ch_title, ch_id))

    ebook.toc   = toc_list
    ebook.spine = spine
    ebook.add_item(epub.EpubNcx())
    ebook.add_item(epub.EpubNav())
    epub.write_epub(str(output), ebook)
    ok(f"EPUB: {output.name} ({output.stat().st_size // 1024} kB)")

# ── PDF export ─────────────────────────────────────────────────────────────────
def make_pdf(book: dict, output: Path) -> None:
    html_content = _book_to_html(book)
    tmp_html = output.with_suffix(".tmp.html")
    tmp_html.write_text(html_content, encoding="utf-8")

    try:
        from weasyprint import HTML as WP_HTML
        WP_HTML(filename=str(tmp_html)).write_pdf(str(output))
        tmp_html.unlink(missing_ok=True)
        ok(f"PDF: {output.name} ({output.stat().st_size // 1024} kB)")
    except ImportError:
        # Rename to print-ready HTML
        print_html = output.with_suffix(".print.html")
        tmp_html.rename(print_html)
        info(f"weasyprint not installed — saved print HTML: {print_html.name}")
        info("Open in browser and use Ctrl+P → Save as PDF")
        info("Install: pip install weasyprint")
    except Exception as e:
        tmp_html.unlink(missing_ok=True)
        err(f"PDF error: {e}")

# ── MP3 split helper ───────────────────────────────────────────────────────────
def split_mp3_if_large(src: Path, dest_dir: Path) -> list[str]:
    """Copy *src* MP3 to *dest_dir*, splitting it into parts if it exceeds
    MP3_PART_MAX_BYTES.  Returns a list of filenames (relative to dest_dir).
    A single-item list means the file was copied as-is.
    """
    size = src.stat().st_size
    if size <= MP3_PART_MAX_BYTES:
        shutil.copy2(src, dest_dir / src.name)
        return [src.name]

    # ── Get duration via ffprobe ───────────────────────────────────────────────
    try:
        probe = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', str(src)],
            capture_output=True, text=True, check=True,
        )
        duration = float(json.loads(probe.stdout)['format']['duration'])
    except Exception as e:
        info(f"ffprobe failed ({e}) — copying {src.name} without split")
        shutil.copy2(src, dest_dir / src.name)
        return [src.name]

    # ── Calculate segment duration targeting MP3_PART_MAX_BYTES per part ──────
    bitrate      = size / duration          # bytes per second
    target_secs  = (MP3_PART_MAX_BYTES - 256 * 1024) / bitrate   # 256 kB safety margin
    target_secs  = max(10.0, target_secs)  # never split into < 10 s shards
    n_parts      = math.ceil(duration / target_secs)

    stem    = src.stem
    pattern = str(dest_dir / f"{stem}-part%03d.mp3")
    try:
        subprocess.run(
            ['ffmpeg', '-i', str(src),
             '-f', 'segment',
             '-segment_time', str(target_secs),
             '-c', 'copy',
             '-reset_timestamps', '1',
             '-y', pattern],
            check=True, capture_output=True,
        )
    except Exception as e:
        info(f"ffmpeg split failed ({e}) — copying {src.name} without split")
        shutil.copy2(src, dest_dir / src.name)
        return [src.name]

    parts = sorted(dest_dir.glob(f"{stem}-part*.mp3"))
    if not parts:
        info(f"ffmpeg produced no output — copying {src.name} without split")
        shutil.copy2(src, dest_dir / src.name)
        return [src.name]

    names = [p.name for p in parts]
    ok(f"Split {src.name} ({size // 1024 // 1024} MB → {n_parts} parts)")
    return names

# ── MP3 split to audio dir (always -partNNN suffix) ───────────────────────────
def split_mp3_to_audio_dir(src: Path, audio_dir: Path) -> list[str]:
    """Copy/split *src* MP3 into *audio_dir*, always using -{partNNN} suffix.

    Single files become  {stem}-part000.mp3.
    Large files (> MP3_PART_MAX_BYTES) are split into  {stem}-part000.mp3, -part001.mp3 ...
    Returns list of filenames relative to audio_dir.
    """
    audio_dir.mkdir(parents=True, exist_ok=True)
    stem = src.stem
    size = src.stat().st_size

    if size <= MP3_PART_MAX_BYTES:
        dest_name = f"{stem}-part000.mp3"
        shutil.copy2(src, audio_dir / dest_name)
        return [dest_name]

    # Large file — split with ffmpeg
    try:
        probe = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', str(src)],
            capture_output=True, text=True, check=True,
        )
        duration = float(json.loads(probe.stdout)['format']['duration'])
    except Exception as e:
        info(f"ffprobe failed ({e}) — copying {src.name} as single part")
        dest_name = f"{stem}-part000.mp3"
        shutil.copy2(src, audio_dir / dest_name)
        return [dest_name]

    bitrate     = size / duration
    target_secs = (MP3_PART_MAX_BYTES - 256 * 1024) / bitrate
    target_secs = max(10.0, target_secs)
    n_parts     = math.ceil(duration / target_secs)
    pattern     = str(audio_dir / f"{stem}-part%03d.mp3")

    try:
        subprocess.run(
            ['ffmpeg', '-i', str(src),
             '-f', 'segment',
             '-segment_time', str(target_secs),
             '-c', 'copy',
             '-reset_timestamps', '1',
             '-y', pattern],
            check=True, capture_output=True,
        )
    except Exception as e:
        info(f"ffmpeg split failed ({e}) — copying {src.name} as single part")
        dest_name = f"{stem}-part000.mp3"
        shutil.copy2(src, audio_dir / dest_name)
        return [dest_name]

    parts = sorted(audio_dir.glob(f"{stem}-part*.mp3"))
    if not parts:
        info(f"ffmpeg produced no output — copying {src.name} as single part")
        dest_name = f"{stem}-part000.mp3"
        shutil.copy2(src, audio_dir / dest_name)
        return [dest_name]

    names = [p.name for p in parts]
    ok(f"Split {src.name} ({size // 1024 // 1024} MB → {n_parts} parts)")
    return names

# ── No URL fetching in generate.py — run import-pl.py download first ──────────

# ── Markdown serialiser ────────────────────────────────────────────────────────
def chapters_to_markdown(chapters: list[dict]) -> str:
    """Serialize parsed chapters back to clean, round-trip-safe markdown.

    Rules:
    • Chapter title  → # Heading
    • Chapter subtitle (optional) → ## immediately after title (before any blocks)
    • heading block  → ### (not ## to avoid subtitle detection on small books)
    • paragraph      → plain text
    • quote          → > lines  /  > — attribution
    • divider        → ---
    • callout        → ::: callout [label] / text / :::
    • verse          → ```verse / text / ```
    • image          → ![alt](url)  /  *caption*
    """
    parts: list[str] = []
    for ch in chapters:
        title    = (ch.get("title") or "").strip()
        subtitle = (ch.get("subtitle") or "").strip()

        parts.append(f"# {title}")
        if subtitle:
            parts.append(f"## {subtitle}")
        parts.append("")

        for block in ch.get("blocks", []):
            btype = block.get("type", "")
            text  = (block.get("text") or "")

            if btype == "paragraph":
                parts.append(text)
                parts.append("")
            elif btype == "heading":
                # Use ### so it doesn't trigger subtitle or _h2_as_chapter logic
                parts.append(f"### {text}")
                parts.append("")
            elif btype == "quote":
                for qline in text.split("\n"):
                    parts.append(f"> {qline}")
                attr = (block.get("attribution") or "").strip()
                if attr:
                    parts.append(f"> — {attr}")
                parts.append("")
            elif btype == "divider":
                parts.append("---")
                parts.append("")
            elif btype == "callout":
                label  = (block.get("label") or "").strip()
                header = f"::: callout {label}" if label else "::: callout"
                parts.append(header)
                parts.append(text)
                parts.append(":::")
                parts.append("")
            elif btype == "verse":
                parts.append("```verse")
                parts.append(text)
                parts.append("```")
                parts.append("")
            elif btype == "image":
                url = block.get("url", "")
                alt = block.get("alt", "")
                cap = (block.get("caption") or "").strip()
                parts.append(f"![{alt}]({url})")
                if cap:
                    parts.append(f"*{cap}*")
                parts.append("")

        parts.append("")

    return "\n".join(parts).rstrip() + "\n"

# ── Load book source ───────────────────────────────────────────────────────────
def load_book_source(book_dir: Path) -> dict:
    config = load_config(book_dir)
    cover  = load_cover_b64(book_dir)

    md_path   = book_dir / "book.md"
    epub_path = book_dir / "book.epub"
    pdf_path  = book_dir / "book.pdf"

    if md_path.exists():
        info(f"Source: {md_path.name}")
        chapters = md_to_chapters(md_path)
    elif epub_path.exists():
        info(f"Source: {epub_path.name}")
        chapters = epub_to_chapters(epub_path)
    elif pdf_path.exists():
        info(f"Source: {pdf_path.name}")
        chapters = pdf_to_chapters(pdf_path)
    else:
        raise FileNotFoundError("No content file found: book.md / book.epub / book.pdf")

    return {**config, "cover": cover, "chapters": chapters}

# ── Generate a single book ─────────────────────────────────────────────────────
def generate_book(book_dir: Path, replace_md: bool = False, do_tts: bool = True) -> bool:
    """Parse book source → book.md (if missing) + audio/ assets in books-source/<id>/.

    Operations (all in-place in book_dir):
      book.md                — written only when missing, or when replace_md=True
      audio/ch{NN}-partNNN.mp3 — split from ch{NN}.mp3 / book.mp3 input files
      tts-audio-{lang}.json  — chapter_id → audio paths map

    Run publish.py afterwards to copy to public/<id>/.
    """
    book_id   = book_dir.name
    audio_dir = book_dir / "audio"

    print()
    print(f"  {gold(bold('★ ATLAS BOOKS — GENERATE'))}: {bold(book_id)}")
    print(f"  {dim('dir: ' + str(book_dir))}")

    # 1. Load + parse source (book.md / book.epub / book.pdf)
    step("Parsing source")
    try:
        book_data = load_book_source(book_dir)
        lang      = book_data.get("lang", "pl")
        chapters  = book_data.get("chapters", [])
        ok(f"Title: {book_data.get('title','')} · {len(chapters)} chapters")
    except Exception as e:
        err(str(e)); return False

    # 2. Write clean book.md only when missing or replace_md=True
    md_path = book_dir / "book.md"
    if not md_path.exists() or replace_md:
        step("Writing book.md")
        md_content = chapters_to_markdown(chapters)
        md_path.write_text(md_content, encoding="utf-8")
        ok(f"book.md  ({len(md_content):,} chars, {len(chapters)} chapters)")
    else:
        info(f"book.md exists — skipping  (use --replace to regenerate)")

    # 3. Audio: process input ch{NN}.mp3 / book.mp3 → audio/ subdir
    if do_tts:
        _process_audio(book_dir, audio_dir, chapters, lang)

    print()
    ok(f"Done → {book_dir}")
    info("Next: python publish.py " + book_id)
    print()
    return True


def _process_audio(book_dir: Path, audio_dir: Path, chapters: list[dict], lang: str) -> None:
    """Process ch{NN}.mp3 / book.mp3 from *book_dir* into *audio_dir*.

    Priority:
    1. tts-timing-{lang}.json sidecar (written by generate-tts.py)
    2. ch{NN}.mp3 files directly in book_dir
    3. book.mp3 (single whole-book recording)

    Writes tts-audio-{lang}.json to book_dir when audio is found.
    """
    sidecar_path   = book_dir / f"tts-timing-{lang}.json"
    sidecar_loaded = 0
    ch_audio: dict[int, list[str]] = {}   # chapter_index → [relative audio paths in audio/]

    if sidecar_path.exists():
        try:
            sidecar     = json.loads(sidecar_path.read_text(encoding="utf-8"))
            sidecar_chs = sidecar.get("chapters", {})
            step("Loading TTS sidecar (generate-tts.py)")
            for i, ch in enumerate(chapters):
                ch_id = ch.get("id", f"ch{i+1}")
                entry = sidecar_chs.get(ch_id)
                if entry and entry.get("mp3"):
                    src = book_dir / entry["mp3"]
                    if src.exists():
                        audio_dir.mkdir(parents=True, exist_ok=True)
                        names = split_mp3_to_audio_dir(src, audio_dir)
                        ch_audio[i] = [f"audio/{n}" for n in names]
                        ch[f"timing_{lang}"] = entry.get("timing", [])
                        sidecar_loaded += 1
            if sidecar_loaded:
                ok(f"Sidecar: {sidecar_loaded} chapters  ({sidecar_path.name})")
        except Exception as e:
            info(f"Sidecar error ({e}) — falling back to MP3 scan")
            sidecar_loaded = 0
            ch_audio = {}

    if not sidecar_loaded:
        # Check for whole-book single MP3
        book_mp3 = book_dir / "book.mp3"
        if book_mp3.exists():
            step(f"Processing whole-book audio: book.mp3")
            audio_dir.mkdir(parents=True, exist_ok=True)
            names = split_mp3_to_audio_dir(book_mp3, audio_dir)
            rel   = [f"audio/{n}" for n in names]
            for i in range(len(chapters)):
                ch_audio[i] = rel
            ok(f"book.mp3 → {', '.join(names)}")
        else:
            # Per-chapter ch{NN}.mp3 files
            src_mp3s = sorted(book_dir.glob("ch*.mp3"))
            if src_mp3s:
                step("Processing per-chapter MP3 audio → audio/")
                audio_dir.mkdir(parents=True, exist_ok=True)
                matched, skipped = 0, 0
                for mp3 in src_mp3s:
                    m = re.match(r'^ch(\d+)\.mp3$', mp3.name)
                    if not m:
                        info(f"Unexpected filename — skipping {mp3.name}")
                        skipped += 1
                        continue
                    ch_idx = int(m.group(1))
                    if ch_idx >= len(chapters):
                        info(f"ch{ch_idx:02d} out of range ({len(chapters)} chapters) — skipping {mp3.name}")
                        skipped += 1
                        continue
                    names = split_mp3_to_audio_dir(mp3, audio_dir)
                    ch_audio[ch_idx] = [f"audio/{n}" for n in names]
                    matched += 1
                if matched:
                    info(f"Matched {matched} MP3 files")
                if skipped:
                    info(f"Skipped {skipped} files")
            else:
                info("No audio found — run generate-tts.py to create ch01.mp3 etc.")
                return

    if not ch_audio:
        return

    # Get stable chapter IDs from book.md (slugified titles)
    md_path = book_dir / "book.md"
    reparsed = md_to_chapters(md_path) if md_path.exists() else chapters

    # Build tts-audio-{lang}.json — keyed by ch{NN} (matches MP3 filenames)
    tts_map: dict[str, dict] = {}
    for i, ch in enumerate(chapters):
        paths = ch_audio.get(i)
        if not paths:
            continue
        ch_key = f"ch{i+1:02d}"
        entry: dict = {"audio": paths}
        timing = ch.get(f"timing_{lang}")
        if timing:
            entry["timing"] = timing
        tts_map[ch_key] = entry

    tts_out = book_dir / f"tts-audio-{lang}.json"
    tts_out.write_text(json.dumps(tts_map, ensure_ascii=False, indent=2), encoding="utf-8")
    ok(f"tts-audio-{lang}.json  ({len(tts_map)} chapters with audio)")

# ── List books ─────────────────────────────────────────────────────────────────
def list_books() -> None:
    print(f"\n  {gold(bold('Atlas Books — Source Books'))}  ({BOOK_SOURCE})\n")
    if not BOOK_SOURCE.exists():
        err(f"books-source/ not found: {BOOK_SOURCE}"); return
    for d in sorted(BOOK_SOURCE.iterdir()):
        if not d.is_dir():
            continue
        cfg_path = d / "config.json"
        if not cfg_path.exists():
            print(f"  {dim('○')} {d.name}  {gray('(no config.json)')}")
            continue
        try:
            cfg = json.loads(cfg_path.read_text())
        except Exception:
            print(f"  {dim('○')} {d.name}  {gray('(invalid config.json)')}"); continue

        title     = cfg.get('title', d.name)
        author    = cfg.get('author', '')
        sources   = [f.suffix[1:] for f in [d/'book.md', d/'book.epub', d/'book.pdf'] if f.exists()]
        cover     = any((d/n).exists() for n in COVER_NAMES)
        pub       = cfg.get('published', False)
        pub_dt    = cfg.get('published_date', None)
        has_md    = (d / "book.md").exists()
        has_audio = (d / "audio").is_dir() and any((d / "audio").glob("*.mp3"))

        pub_label = (
            f"{green('published')} {gray(pub_dt)}" if pub and pub_dt
            else (green('published') if pub else gray('draft'))
        )
        print(f"  {gold('►')} {bold(d.name)}")
        print(f"    {dim(title)}{' · ' + dim(author) if author else ''}")
        print(f"    source: {gray(', '.join(sources) or 'missing!')}  "
              f"cover: {'yes' if cover else gray('no')}  "
              f"book.md: {'yes' if has_md else gray('no')}  "
              f"audio: {'yes' if has_audio else gray('no')}  "
              f"status: {pub_label}")
    print()

# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("book",          nargs="?", default=None, metavar="NAME_OR_PATH")
    parser.add_argument("--all",         action="store_true", help="Generate all books in books-source/")
    parser.add_argument("--list",        action="store_true")
    parser.add_argument("--replace",     action="store_true", help="Re-generate book.md even if it already exists")
    parser.add_argument("--no-tts",      action="store_true", help="Skip MP3 audio processing entirely")
    parser.add_argument("-h", "--help",  action="store_true")
    args = parser.parse_args()

    if args.list:
        list_books(); sys.exit(0)

    kwargs = dict(replace_md=args.replace, do_tts=not args.no_tts)

    if args.all:
        if not BOOK_SOURCE.exists():
            err(f"books-source/ not found: {BOOK_SOURCE}"); sys.exit(1)
        books = [d for d in sorted(BOOK_SOURCE.iterdir())
                 if d.is_dir() and (d / "config.json").exists()]
        if not books:
            err("No books found in books-source/"); sys.exit(1)
        success = all(generate_book(b, **kwargs) for b in books)
        sys.exit(0 if success else 1)

    if not args.book or args.help:
        print_help(); sys.exit(0)

    # Resolve name or path
    inp = Path(args.book)
    if inp.is_dir():
        book_dir = inp.resolve()
    elif (BOOK_SOURCE / args.book).is_dir():
        book_dir = (BOOK_SOURCE / args.book).resolve()
    else:
        err(f"Book not found: '{args.book}'  (looked in books-source/ and as a path)"); sys.exit(1)

    ok_result = generate_book(book_dir, **kwargs)
    sys.exit(0 if ok_result else 1)


if __name__ == "__main__":
    main()
