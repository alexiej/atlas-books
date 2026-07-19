# Star Federation — Atlas Books

A self-contained publishing platform for digital books. Write in Markdown, generate a reader app, audio narration, EPUB, and PDF with a single command.

---

## Directory structure

```
star-federation/
├── books-source/          Source files for each book
│   └── <name>/
│       ├── config.json   Metadata + TTS settings
│       ├── book.md       Content (Markdown)
│       └── cover.jpg     Cover image
│
├── books-dest/            Generated files (commit to git)
│   └── <name>/
│       ├── <name>.html   Self-contained viewer
│       ├── <name>.epub   E-reader format
│       ├── <name>.pdf    Print document
│       └── <name>-*.mp3  TTS audio per chapter
│
├── public/               Deploy output (GitHub Pages)
│   ├── index.html        Library listing (EN/PL)
│   ├── book-template.html Vite viewer template
│   └── <name>.html       Published book viewer
│
├── src/                  React viewer source (Vite + TypeScript)
├── tools/
│   ├── generate-book.py  Book generator (HTML + MP3 + EPUB + PDF)
│   └── publish-book.py   Publisher (copies to public/, updates catalog)
└── .github/workflows/deploy.yml   GitHub Pages CI
```

---

## Quick start

```bash
# Install dependencies
npm install
pip install edge-tts ebooklib lxml weasyprint

# Generate a specific book (HTML + MP3 + EPUB + PDF)
npm run generate -- --input star-federation

# Generate all books in books-source/
npm run generate

# Skip audio
npm run generate -- --input star-federation --no-tts

# Skip EPUB or PDF
npm run generate -- --input star-federation --no-epub --no-pdf

# List all books and their status
npm run list

# Publish to public/
npm run publish -- star-federation

# Generate + publish all books in one step
npm run publish-all

# Full build (viewer template + all published books + index)
npm run build

# Local dev (hot-reload viewer)
npm run dev
```

---

## config.json format

```json
{
  "id": "my-book",
  "title": "My Book",
  "author": "Author Name",
  "year": "2026",
  "lang": "pl",
  "description": "Short description shown in the library.",
  "published": false,
  "published_date": null,
  "tts": {
    "voice": "pl-PL-MarekNeural",
    "rate": "+0%",
    "pitch": "+0Hz"
  }
}
```

Set `"published": true` for the book to be included in `npm run publish-all` and `npm run build`.

---

## Markdown format

```markdown
---
title: My Book
author: Author Name
---

# Chapter One
## Optional chapter subtitle

First paragraph of the chapter. The first letter gets a drop cap in PDF/EPUB.

Second paragraph gets an indent.

> A blockquote
> — Attribution

---

:::Callout Title
Callout content here.
:::

```verse
A poem
across lines
```

## Section heading inside chapter
```

---

## GitHub Pages deployment

Push to `main`. The workflow in `.github/workflows/deploy.yml`:

1. Installs Node 20 + Python 3.11
2. Runs `npm run build` (builds the Vite viewer template, then copies all published books to `public/`)
3. Deploys `public/` to GitHub Pages

**Important:** commit `books-dest/` so CI can deploy without re-running TTS generation.

```bash
git add books-dest/
git commit -m "Generate star-federation"
git push
```

---

## Tools reference

### `generate.py`

Reads from `books-source/<name>/`, writes to `books-dest/<name>/`.  
All formats are **on by default** — generates HTML, MP3, EPUB, and PDF, always replacing existing files.

```
--input <name>   Book name (folder in books-source/) or full path
                 (omit to generate all books)
--all            Generate all books in books-source/
--list           List books and their status
--no-tts         Skip MP3 audio generation
--no-epub        Skip EPUB generation
--no-pdf         Skip PDF generation  (requires: pip install weasyprint)
--tts-voice      Override TTS voice
--tts-rate       Override speech rate (e.g. -10%)
--tts-pitch      Override pitch (e.g. -2Hz)
```

### `publish.py`

Reads from `books-dest/<name>/`, copies to `public/`, updates `catalog.json` and `index.html`.

```
<name>           Publish one book
--all            Publish all books marked published: true
--rebuild        Rebuild index.html from existing catalog only
--build          Full build: update template + publish all + rebuild index
--list           Show current catalog
--remove <name>  Remove a book from the catalog
```

---

## Python dependencies

```bash
pip install edge-tts        # TTS audio (Microsoft Edge voices)
pip install ebooklib lxml   # EPUB export
pip install weasyprint      # PDF export (optional)
```

---

## dist/ directory

`dist/` is **Vite's internal build output** — created when you run `vite build` or `npm run build-viewer`. It holds the compiled React viewer (`dist/index.html`) which `npm run build` then copies to `public/book-template.html`. You never need to edit or commit `dist/` directly.
