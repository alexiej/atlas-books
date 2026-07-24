#!/usr/bin/env python3
"""
generate-tts.py — Standalone TTS audio generator for Atlas Books

Reads book content from books-source/<id>/, generates MP3 + word-timing data
using Microsoft Azure edge-tts (free, no API key required).

Saves:
  books-source/<id>/ch01.mp3, ch02.mp3   — audio files (one per chapter)
  books-source/<id>/tts-timing-pl.json   — timing sidecar (auto-read by generate.py)

Press Ctrl+C at any time — completed chapters are saved automatically.

Usage:
  python generate-tts.py 20-000-mil-podmorskiej-zeglugi
  python generate-tts.py 20-000-mil-podmorskiej-zeglugi --chapters 1-10
  python generate-tts.py 20-000-mil-podmorskiej-zeglugi --chapters 3,7,12
  python generate-tts.py 20-000-mil-podmorskiej-zeglugi --voice pl-PL-ZofiaNeural
  python generate-tts.py 20-000-mil-podmorskiej-zeglugi --rate "+20%"
  python generate-tts.py 20-000-mil-podmorskiej-zeglugi --quality fast
  python generate-tts.py 20-000-mil-podmorskiej-zeglugi --replace
  python generate-tts.py 20-000-mil-podmorskiej-zeglugi --fix-names
  python generate-tts.py 20-000-mil-podmorskiej-zeglugi --fix-names --src ~/Downloads
  python generate-tts.py --list-voices

Quality modes:
  full  (default)  Audio + word-level timing for text highlighting
  fast             Audio only — no timing data, slightly faster processing

Voices (Polish / pl-PL):
  pl-PL-MarekNeural   ♂  Natural, warm              [default for Polish]
  pl-PL-ZofiaNeural   ♀  Natural, clear

Voices (English / en-US):
  en-US-AriaNeural         ♀  News, conversational
  en-US-JennyNeural        ♀  Friendly, casual
  en-US-ChristopherNeural  ♂  Authoritative, novel
  en-US-EricNeural         ♂  Rational, clear
  en-US-GuyNeural          ♂  Passionate
  en-US-RogerNeural        ♂  Lively
  en-GB-RyanNeural         ♂  British
  en-GB-SoniaNeural        ♀  British
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent
BOOK_SOURCE = PROJECT_DIR / "books-source"

# Import shared parsing + TTS helpers from generate.py
sys.path.insert(0, str(PROJECT_DIR))
try:
    from generate import (  # noqa: E402
        epub_to_chapters,
        md_to_chapters,
        pdf_to_chapters,
        load_config,
        _build_tts_parts,
        _build_word_sources,
        _merge_timing,
    )
except ImportError as _e:
    print(f"ERROR: cannot import from generate.py: {_e}", file=sys.stderr)
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
def step(msg: str) -> None: print(f"\n  {bold(gold('▶'))} {msg}")
def warn(msg: str) -> None: print(f"  {yellow('⚠')} {msg}")

# ── Interrupt handler ──────────────────────────────────────────────────────────
_stop = False

def _sigint(sig: int, frame: object) -> None:
    global _stop
    _stop = True
    print(f"\n\n  {yellow('⚠')}  Interrupt received — will stop after current chapter.")
    print(f"  {gray('Progress saved to tts-timing-pl.json')}\n")

signal.signal(signal.SIGINT, _sigint)

# ── Time helpers ───────────────────────────────────────────────────────────────
def _fmt_secs(s: float) -> str:
    s = max(0, int(s))
    if s < 60:   return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:   return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"

def _fmt_dur(s: float) -> str:
    """Format audio duration as m:ss."""
    s = max(0, int(s))
    m, s = divmod(s, 60)
    return f"{m}:{s:02d}"

# ── Progress tracker ───────────────────────────────────────────────────────────
class Progress:
    BAR_W = 28

    def __init__(self, total: int) -> None:
        self.total    = total
        self.done     = 0
        self.skipped  = 0
        self._times:  list[float] = []     # rolling window of chapter durations
        self._wall_start = time.monotonic()

    def record(self, elapsed: float) -> None:
        self.done += 1
        self._times.append(elapsed)
        if len(self._times) > 6:
            self._times.pop(0)

    @property
    def avg(self) -> float:
        return sum(self._times) / len(self._times) if self._times else 0.0

    @property
    def eta(self) -> float:
        remaining = self.total - self.done - self.skipped
        return self.avg * max(0, remaining)

    def bar(self) -> str:
        pct = (self.done + self.skipped) / self.total if self.total else 0
        n   = int(pct * self.BAR_W)
        return "█" * n + "░" * (self.BAR_W - n)

    def line(self) -> str:
        done  = self.done + self.skipped
        pct   = int(done / self.total * 100) if self.total else 0
        parts = [
            f"{self.bar()}  {pct:3d}%",
            f"{done}/{self.total} ch",
        ]
        if self._times:
            parts.append(f"avg {_fmt_secs(self.avg)}/ch")
            if self.eta > 0:
                parts.append(f"ETA {_fmt_secs(self.eta)}")
        return "  " + "  │  ".join(parts)

# ── Chapter range parsing ──────────────────────────────────────────────────────
def parse_chapters(spec: str, total: int) -> set[int]:
    """'1-10', '3,7,12', 'all' → zero-based indices."""
    if not spec or spec.strip().lower() == "all":
        return set(range(total))
    result: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        m = re.match(r'^(\d+)-(\d+)$', part)
        if m:
            a, b = int(m.group(1)) - 1, int(m.group(2)) - 1
            result.update(range(max(0, a), min(total - 1, b) + 1))
        elif part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < total:
                result.add(idx)
        else:
            warn(f"Ignoring unrecognised chapter spec: '{part}'")
    return result

# ── TTS synthesis ──────────────────────────────────────────────────────────────
async def _synth_full(text: str, voice: str, rate: str, pitch: str, volume: str
                      ) -> tuple[bytes, list[dict]]:
    """Audio + word-boundary timing events."""
    import edge_tts
    comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume)
    audio: list[bytes] = []
    timing: list[dict] = []
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            audio.append(chunk["data"])
        elif chunk["type"] == "WordBoundary":
            s = round(chunk["offset"] / 10_000_000, 3)
            e = round((chunk["offset"] + chunk["duration"]) / 10_000_000, 3)
            timing.append({"w": chunk["text"], "s": s, "e": e})
    return b"".join(audio), timing

async def _synth_fast(text: str, voice: str, rate: str, pitch: str, volume: str
                      ) -> tuple[bytes, list[dict]]:
    """Audio only — no word boundary events."""
    import edge_tts
    comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume)
    audio: list[bytes] = []
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            audio.append(chunk["data"])
    return b"".join(audio), []

# ── Main generation loop ───────────────────────────────────────────────────────
def fix_names(book_id: str, src_dir: Path | None = None) -> None:
    """Rename old-style MP3s to ch{NN}.mp3 / book.mp3 format.

    Patterns recognised:
      {book_id}-ch{NN}[-rest].mp3  →  ch{NN}.mp3
      {book_id}[-rest].mp3         →  book.mp3
      ch{NN}.mp3 / book.mp3        →  already correct

    If --src is given, files are moved from that directory into
    books-source/{book_id}/ (renaming along the way).
    Otherwise, files already in books-source/{book_id}/ are renamed in-place.
    """
    book_dir = BOOK_SOURCE / book_id
    book_dir.mkdir(parents=True, exist_ok=True)

    scan_dir = src_dir if src_dir else book_dir
    if not scan_dir.exists():
        err(f"Source directory not found: {scan_dir}")
        sys.exit(1)

    print()
    print(f"  {bold(gold('★ ATLAS BOOKS — FIX NAMES'))}: {bold(book_id)}")
    if src_dir:
        print(f"  {dim('From:')} {dim(str(src_dir))}")
        print(f"  {dim('To:')}   {dim(str(book_dir))}")
    print()

    mp3s = sorted(scan_dir.glob("*.mp3"))
    if not mp3s:
        warn(f"No MP3 files found in: {scan_dir}")
        if not src_dir:
            info(f"Tip: use --src /path/to/files  to import from another directory")
        print()
        return

    renamed = 0
    skipped = 0

    for mp3 in mp3s:
        stem = mp3.stem

        # ── Already correct format ─────────────────────────────────────
        if re.match(r'^ch\d+$', stem) or stem == "book":
            if src_dir:
                dest = book_dir / mp3.name
                if dest.exists():
                    warn(f"{mp3.name} already exists in dest — skipping")
                    skipped += 1
                else:
                    shutil.move(str(mp3), dest)
                    ok(f"{mp3.name}  →  (moved to books-source/{book_id}/)")
                    renamed += 1
            else:
                info(f"Already correct: {mp3.name}")
                skipped += 1
            continue

        # ── Try {book_id}-ch{NN}*.mp3 ─────────────────────────────────
        m = re.match(rf'^{re.escape(book_id)}-ch(\d+)', stem)
        if not m:
            # Generic: find -ch{NN} or ch{NN} at start
            m = re.search(r'(?:^|-)ch(\d+)', stem)

        if m:
            ch_num   = int(m.group(1))
            new_name = f"ch{ch_num:02d}.mp3"
            dest     = book_dir / new_name
            if dest.exists():
                warn(f"{new_name} already exists — skipping {mp3.name}")
                skipped += 1
                continue
            if src_dir:
                shutil.move(str(mp3), dest)
            else:
                mp3.rename(dest)
            ok(f"{mp3.name}  →  {new_name}")
            renamed += 1
            continue

        # ── Try {book_id}[-rest].mp3  →  book.mp3 ─────────────────────
        if stem == book_id or stem.startswith(f"{book_id}-") or stem.startswith(f"{book_id}_"):
            new_name = "book.mp3"
            dest     = book_dir / new_name
            if dest.exists():
                warn(f"book.mp3 already exists — skipping {mp3.name}")
                skipped += 1
                continue
            if src_dir:
                shutil.move(str(mp3), dest)
            else:
                mp3.rename(dest)
            ok(f"{mp3.name}  →  {new_name}")
            renamed += 1
            continue

        warn(f"Cannot determine chapter number — skipping: {mp3.name}")
        skipped += 1

    print()
    ok(f"Renamed/moved {renamed} file(s)  (skipped {skipped})")
    if renamed:
        info(f"Next: python generate.py {book_id}  # process audio → audio/")
    print()


async def run(
    book_id:      str,
    voice:        str,
    rate:         str,
    pitch:        str,
    volume:       str,
    quality:      str,
    lang:         str,
    chapter_spec: str,
    replace:      bool,
) -> None:
    global _stop

    book_dir = BOOK_SOURCE / book_id
    if not book_dir.exists():
        err(f"books-source/{book_id}/ not found")
        sys.exit(1)

    # Load config
    try:
        config = load_config(book_dir)
    except FileNotFoundError:
        err(f"Missing config.json in books-source/{book_id}/")
        sys.exit(1)

    tts_cfg = config.get("tts", {})
    voice   = voice  or tts_cfg.get("voice", "pl-PL-MarekNeural" if lang == "pl" else "en-US-AriaNeural")
    rate    = rate   or tts_cfg.get("rate",  "+0%")
    pitch   = pitch  or tts_cfg.get("pitch", "+0Hz")
    lang    = config.get("lang", lang)

    # Parse source
    md_path   = book_dir / "book.md"
    epub_path = book_dir / "book.epub"
    pdf_path  = book_dir / "book.pdf"

    if md_path.exists():
        chapters = md_to_chapters(md_path)
        source   = md_path.name
    elif epub_path.exists():
        chapters = epub_to_chapters(epub_path)
        source   = epub_path.name
    elif pdf_path.exists():
        chapters = pdf_to_chapters(pdf_path)
        source   = pdf_path.name
    else:
        err("No content file found: book.md / book.epub / book.pdf")
        sys.exit(1)

    if not chapters:
        err("No chapters found in source file")
        sys.exit(1)

    # Load existing sidecar
    sidecar_path = book_dir / f"tts-timing-{lang}.json"
    sidecar: dict = {"voice": voice, "rate": rate, "pitch": pitch,
                     "quality": quality, "lang": lang, "chapters": {}}
    if sidecar_path.exists():
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            sidecar.setdefault("chapters", {})
        except Exception:
            pass

    # Determine which chapters to generate
    target_indices = parse_chapters(chapter_spec, len(chapters))

    to_do: list[int] = []
    for i, ch in enumerate(chapters):
        if i not in target_indices:
            continue
        ch_id    = ch.get("id", f"ch{i+1}")
        ch_num   = ch.get("number", i + 1)
        mp3_name = f"ch{ch_num:02d}.mp3"
        already  = ch_id in sidecar["chapters"] and (book_dir / mp3_name).exists()
        if already and not replace:
            continue
        to_do.append(i)

    # Header
    print()
    print(f"  {bold(gold('★ ATLAS BOOKS — TTS GENERATOR'))}: {bold(book_id)}")
    print(f"  {dim(config.get('title', book_id))}  ·  {dim(config.get('author', ''))}")
    print()
    print(f"  Source:  {gray(source)}  ({len(chapters)} chapters)")
    print(f"  Voice:   {cyan(voice)}   Rate: {rate}   Pitch: {pitch}")
    print(f"  Quality: {bold('full (audio + word timing)') if quality == 'full' else bold('fast (audio only)')}")
    print(f"  Output:  books-source/{book_id}/ch{{NN}}.mp3  →  tts-timing-{lang}.json")
    print()

    if not to_do:
        print(f"  {green('✓')} All {len(target_indices)} chapter(s) already generated.")
        print(f"  {gray('Use --replace to regenerate.')}\n")
        return

    skipped = len(target_indices) - len(to_do)
    print(f"  Chapters to generate: {bold(str(len(to_do)))}  "
          f"(skipping {skipped} already done, {len(chapters) - len(target_indices)} out of range)")
    print()

    synth = _synth_full if quality == "full" else _synth_fast
    prog  = Progress(len(to_do))
    prog.skipped = 0  # we track separately

    errors: list[str] = []

    for seq, ch_idx in enumerate(to_do):
        if _stop:
            break

        ch       = chapters[ch_idx]
        ch_id    = ch.get("id", f"ch{ch_idx + 1}")
        ch_num   = ch.get("number", ch_idx + 1)
        ch_title = ch.get("title", f"Chapter {ch_num}")
        mp3_name = f"ch{ch_num:02d}.mp3"
        mp3_path = book_dir / mp3_name

        print(f"  {bold(gold(f'[{seq+1:>3}/{len(to_do)}]'))}  {bold(ch_title)}")
        if prog.done and prog.avg:
            print(f"  {prog.line()}")

        t0 = time.monotonic()

        try:
            parts   = _build_tts_parts(ch, lang)
            sources = _build_word_sources(parts)
            text    = " ".join(p[0] for p in parts)

            if not text.strip():
                warn(f"Chapter {ch_id}: no text content — skipping")
                prog.skipped += 1
                print()
                continue

            word_count = len(text.split())
            print(f"  {gray('synthesising')} {dim(str(word_count) + ' words')} …")
            sys.stdout.flush()

            try:
                audio_bytes, timing_raw = await asyncio.wait_for(
                    synth(text, voice, rate, pitch, volume),
                    timeout=600,  # 10 min hard limit
                )
            except asyncio.TimeoutError:
                err(f"Chapter {ch_id}: TTS timed out (>10 min) — skipping")
                errors.append(ch_id)
                print()
                continue

            if not audio_bytes:
                err(f"Chapter {ch_id}: got empty audio — skipping")
                errors.append(ch_id)
                print()
                continue

            mp3_path.write_bytes(audio_bytes)

            # Merge timing with word source map
            merged_timing: list[dict] = []
            if timing_raw:
                merged_timing = _merge_timing(timing_raw, sources)

            sidecar["chapters"][ch_id] = {
                "mp3":    mp3_name,
                "timing": merged_timing,
            }
            sidecar["voice"]   = voice
            sidecar["rate"]    = rate
            sidecar["pitch"]   = pitch
            sidecar["quality"] = quality
            sidecar["lang"]    = lang
            sidecar["updated"] = datetime.now().isoformat(timespec="seconds")

            # Checkpoint save after every chapter
            sidecar_path.write_text(
                json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            elapsed = time.monotonic() - t0
            prog.record(elapsed)

            # Audio duration from timing
            audio_dur = timing_raw[-1]["e"] if timing_raw else 0.0
            dur_str   = f"  {dim(_fmt_dur(audio_dur))}" if audio_dur else ""

            size_kb = len(audio_bytes) // 1024
            timing_info = f"  {gray(str(len(merged_timing)) + ' words')}" if merged_timing else ""
            print(f"  {green('✓')} {gray(mp3_name)}  {dim(str(size_kb) + ' kB')}{dur_str}{timing_info}")
            print(f"  {gray('done in')} {dim(_fmt_secs(elapsed))}")

        except Exception as exc:
            elapsed = time.monotonic() - t0
            err(f"Chapter {ch_id}: {exc}")
            errors.append(ch_id)

        print()

    # ── Summary ────────────────────────────────────────────────────────────────
    total_done = prog.done
    wall_time  = time.monotonic() - prog._wall_start

    if _stop:
        print(f"  {yellow('⚠')}  Stopped after {total_done} chapter(s).")
    else:
        print(f"  {green('✓')}  Done — {total_done} chapter(s) generated in {_fmt_secs(wall_time)}.")

    print(f"  {gray('Sidecar:')} {dim(str(sidecar_path.relative_to(PROJECT_DIR)))}")

    if errors:
        print(f"  {red('✗')} {len(errors)} error(s): {', '.join(errors)}")

    print()
    print(f"  {gray('Next step:')}")
    print(f"    {cyan('python generate.py ' + book_id)}  "
          f"{gray('# process audio → audio/ + tts-audio-{lang}.json')}")
    print()

# ── CLI ────────────────────────────────────────────────────────────────────────
VOICES = [
    ("pl-PL-MarekNeural",       "♂", "Polish",   "Natural, warm             [default for PL]"),
    ("pl-PL-ZofiaNeural",       "♀", "Polish",   "Natural, clear"),
    ("en-US-AriaNeural",        "♀", "English",  "News, conversational      [default for EN]"),
    ("en-US-JennyNeural",       "♀", "English",  "Friendly, casual"),
    ("en-US-ChristopherNeural", "♂", "English",  "Authoritative, novel"),
    ("en-US-EricNeural",        "♂", "English",  "Rational, clear"),
    ("en-US-GuyNeural",         "♂", "English",  "Passionate"),
    ("en-US-RogerNeural",       "♂", "English",  "Lively"),
    ("en-GB-RyanNeural",        "♂", "English",  "British ♂"),
    ("en-GB-SoniaNeural",       "♀", "English",  "British ♀"),
]

def cmd_list_voices() -> None:
    print()
    print(f"  {bold(gold('Available TTS Voices'))}\n")
    cur_lang = ""
    for name, gender, lang, desc in VOICES:
        if lang != cur_lang:
            cur_lang = lang
            print(f"  {bold(lang)}")
        g_col = cyan(gender) if gender == "♀" else gold(gender)
        print(f"    {cyan(name):<42} {g_col}  {dim(desc)}")
    print()
    print(f"  {gray('Usage:  python generate-tts.py BOOK_ID --voice pl-PL-ZofiaNeural')}")
    print()

def main() -> None:
    p = argparse.ArgumentParser(prog="generate-tts", add_help=False)
    p.add_argument("book_id",        nargs="?",  default=None)
    p.add_argument("--voice",        default="", help="TTS voice name")
    p.add_argument("--rate",         default="", help="Speech rate, e.g. +10%")
    p.add_argument("--pitch",        default="", help="Speech pitch, e.g. +2Hz")
    p.add_argument("--volume",       default="+0%")
    p.add_argument("--quality",      default="full", choices=["full", "fast"],
                   help="full=audio+timing (default), fast=audio only")
    p.add_argument("--lang",         default="pl", choices=["pl", "en"])
    p.add_argument("--chapters",     default="all",
                   help="Chapter range: '1-10', '3,7', 'all'")
    p.add_argument("--replace",      action="store_true",
                   help="Re-generate chapters that already have audio")
    p.add_argument("--fix-names",    action="store_true",
                   help="Rename existing MP3s to ch{NN}.mp3 / book.mp3 format, then exit")
    p.add_argument("--src",          default=None, metavar="DIR",
                   help="Source directory with MP3s to rename + move into books-source/{book_id}/")
    p.add_argument("--list-voices",  action="store_true")
    p.add_argument("-h", "--help",   action="store_true")
    args = p.parse_args()

    if args.help or (not args.book_id and not args.list_voices):
        print(__doc__)
        sys.exit(0)

    if args.list_voices:
        cmd_list_voices()
        sys.exit(0)

    if args.fix_names:
        src = Path(args.src).expanduser() if args.src else None
        fix_names(args.book_id, src_dir=src)
        sys.exit(0)

    # Check edge-tts is available
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        err("edge-tts not installed.  Run:  pip install edge-tts")
        sys.exit(1)

    asyncio.run(run(
        book_id      = args.book_id,
        voice        = args.voice,
        rate         = args.rate,
        pitch        = args.pitch,
        volume       = args.volume,
        quality      = args.quality,
        lang         = args.lang,
        chapter_spec = args.chapters,
        replace      = args.replace,
    ))


if __name__ == "__main__":
    main()
