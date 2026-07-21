#!/usr/bin/env python3
"""
analyze-tts.py — Word-level timing alignment for Atlas Books

Pipeline position:
  generate.py  →  [generate-tts.py]  →  analyze-tts.py  →  publish.py

Reads MP3 files from books-dest/<id>/, transcribes them with Whisper
(word-level timestamps), aligns the transcript to book.md text, then
writes timing data into books-dest/<id>/tts-audio-<lang>.json.

After running analyze-tts.py, run publish.py to embed the timing into
the final HTML. The viewer then highlights the currently spoken word
during playback and (optionally) follows the audio by turning pages.

Works for both:
  • TTS-generated audio (from generate-tts.py) — for re-analysis / fixing
  • External / human-narrated audio (e.g. Wolne Lektury MP3s)

Usage:
  python analyze-tts.py homer-odyseja
  python analyze-tts.py homer-odyseja --lang pl
  python analyze-tts.py homer-odyseja --chapters 1-5
  python analyze-tts.py homer-odyseja --chapters 3,7,12
  python analyze-tts.py homer-odyseja --model large-v3
  python analyze-tts.py homer-odyseja --replace          # re-analyze done chapters
  python analyze-tts.py homer-odyseja --list             # show chapter/audio status
  python analyze-tts.py --list-models

Whisper models (faster-whisper):
  tiny        ~1 min/hour audio  — rough quality
  base        ~2 min/hour        — decent
  small       ~4 min/hour        — good
  medium      ~8 min/hour        — recommended for Polish   [default]
  large-v3    ~20 min/hour       — best accuracy

Install deps:
  pip install faster-whisper mutagen
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import signal
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

# ── Paths ──────────────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent
BOOK_DEST   = PROJECT_DIR / "books-dest"

# ── Import shared helpers from generate.py ────────────────────────────────────
sys.path.insert(0, str(PROJECT_DIR))
try:
    from generate import md_to_chapters, _build_tts_parts
except ImportError as _e:
    print(f"ERROR: cannot import from generate.py: {_e}", file=sys.stderr)
    sys.exit(1)

# ── ANSI colours ───────────────────────────────────────────────────────────────
_tty = sys.stdout.isatty()
def _c(n: str, s: str) -> str: return f"\033[{n}m{s}\033[0m" if _tty else s
def gold(s: str)   -> str: return _c("33", s)
def green(s: str)  -> str: return _c("32", s)
def red(s: str)    -> str: return _c("31", s)
def gray(s: str)   -> str: return _c("90", s)
def cyan(s: str)   -> str: return _c("36", s)
def bold(s: str)   -> str: return _c("1",  s)
def dim(s: str)    -> str: return _c("2",  s)
def yellow(s: str) -> str: return _c("33", s)

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
    print(f"\n\n  {yellow('⚠')}  Interrupt — will stop after current chapter.")

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
    s = max(0, int(s))
    m, s = divmod(s, 60)
    return f"{m}:{s:02d}"

# ── Chapter range parsing ──────────────────────────────────────────────────────
def parse_chapters_spec(spec: str, ids: list[str]) -> list[str]:
    """'1-5', '3,7,12', 'all' → list of chapter IDs (by 1-based position)."""
    if not spec or spec.strip().lower() == "all":
        return ids
    result: list[str] = []
    for part in spec.split(","):
        part = part.strip()
        m = re.match(r'^(\d+)-(\d+)$', part)
        if m:
            a, b = int(m.group(1)) - 1, int(m.group(2)) - 1
            result.extend(ids[max(0, a): min(len(ids), b + 1)])
        elif part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(ids):
                result.append(ids[idx])
        else:
            warn(f"Unrecognised chapter spec: '{part}'")
    # Preserve order, deduplicate
    seen: set[str] = set()
    out: list[str] = []
    for x in result:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

# ── MP3 duration ───────────────────────────────────────────────────────────────
def get_mp3_duration(path: Path) -> float:
    """Return duration of an MP3 file in seconds."""
    try:
        from mutagen.mp3 import MP3
        return float(MP3(str(path)).info.length)
    except Exception:
        # Rough fallback: assume 128 kbps
        return path.stat().st_size / (128 * 1024 / 8)

# ── Live progress display ──────────────────────────────────────────────────────
class LiveProgress:
    """
    Two-row live display:
      Row 1 (static, printed once): chapter header
      Row 2 (updating, \r):         ▷ ████░░░ 4:20 / 8:45  ETA 12m
    """
    BAR_W = 26

    def __init__(self, total_chapters: int, total_audio_secs: float) -> None:
        self.total_ch    = total_chapters
        self.total_audio = total_audio_secs   # total audio across all pending chapters
        self.done_ch     = 0
        self._wall_start = time.monotonic()
        self._done_audio = 0.0                # audio seconds already fully processed
        # Current chapter
        self._ch_dur     = 0.0
        self._ch_proc    = 0.0               # audio seconds processed in current chapter
        self._ch_start   = 0.0
        self._progress_active = False        # True while \r line is live

    # ── Chapter lifecycle ──────────────────────────────────────────────────────
    def begin_chapter(self, pos: int, title: str, ch_dur: float) -> None:
        """Call before starting transcription of a chapter."""
        self._ch_dur   = ch_dur
        self._ch_proc  = 0.0
        self._ch_start = time.monotonic()
        self._progress_active = False
        n = str(pos).rjust(len(str(self.total_ch)))
        parts = gray(f"  {_fmt_dur(ch_dur)}") if ch_dur else ""
        print(f"\n  {dim(n)}/{self.total_ch}  {bold(cyan(title))}{parts}")

    def update(self, audio_t: float, part_offset: float = 0.0) -> None:
        """Call after each Whisper segment. audio_t is the segment end time."""
        self._ch_proc = part_offset + audio_t
        if _tty:
            self._draw()

    def finish_chapter(self, ch_dur: float, stats: str) -> None:
        """Call after chapter is done. Clears the progress line, prints result."""
        if _tty and self._progress_active:
            sys.stdout.write("\r\033[K")   # clear the \r line
            sys.stdout.flush()
        self._progress_active = False
        elapsed = time.monotonic() - self._ch_start
        self._done_audio += ch_dur
        self.done_ch     += 1
        ok(f"{_fmt_secs(elapsed)}  {gray(stats)}")

    def fail_chapter(self, ch_dur: float) -> None:
        if _tty and self._progress_active:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
        self._progress_active = False
        self._done_audio += ch_dur
        self.done_ch += 1

    # ── Internal rendering ─────────────────────────────────────────────────────
    def _bar(self, frac: float) -> str:
        frac = max(0.0, min(1.0, frac))
        n    = int(frac * self.BAR_W)
        return gold("█" * n) + dim("░" * (self.BAR_W - n))

    def _eta_str(self) -> str:
        elapsed = time.monotonic() - self._wall_start
        done_s  = self._done_audio + self._ch_proc
        if done_s < 5 or elapsed < 3:
            return ""
        # real-time factor: elapsed / audio_processed
        rtf     = elapsed / done_s
        remain  = (self.total_audio - done_s) * rtf
        if remain < 5:
            return ""
        return f"  ETA {_fmt_secs(remain)}"

    def _draw(self) -> None:
        frac = self._ch_proc / self._ch_dur if self._ch_dur > 0 else 0
        bar  = self._bar(frac)
        pos  = f"{_fmt_dur(self._ch_proc)} / {_fmt_dur(self._ch_dur)}"
        eta  = self._eta_str()
        line = f"  ▷ {bar}  {pos}{eta}"
        sys.stdout.write(f"\r{line}")
        sys.stdout.flush()
        self._progress_active = True


# ── Whisper transcription ──────────────────────────────────────────────────────
def transcribe_mp3(mp3_path: Path, model: object, language: str,
                   time_offset: float = 0.0,
                   on_progress: object = None,
                   part_offset: float = 0.0) -> list[dict]:
    """
    Transcribe one MP3 file, return list of word dicts.
    on_progress(audio_t, part_offset) is called after each segment (for live bar).
    """
    segments, _ = model.transcribe(  # type: ignore[attr-defined]
        str(mp3_path),
        word_timestamps=True,
        language=language,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300},
    )
    words: list[dict] = []
    for seg in segments:
        if _stop:
            break
        if on_progress is not None:
            on_progress(seg.end, part_offset)
        if not seg.words:
            continue
        for w in seg.words:
            word = w.word.strip()
            if not word:
                continue
            words.append({
                "word":  word,
                "start": round(w.start + time_offset, 3),
                "end":   round(w.end   + time_offset, 3),
            })
    return words


def transcribe_parts(mp3_files: list[Path], model: object, language: str,
                     on_progress: object = None) -> list[dict]:
    """
    Transcribe one or more MP3 parts with cumulative time offsets.
    Returns merged word list. Calls on_progress(audio_t, part_offset) per segment.
    """
    all_words: list[dict] = []
    time_offset  = 0.0   # absolute time offset for timestamps
    part_offset  = 0.0   # offset passed to progress (same thing but tracked separately)

    for mp3_path in mp3_files:
        if not mp3_path.exists():
            err(f"MP3 not found: {mp3_path.name}")
            continue
        words = transcribe_mp3(
            mp3_path, model, language,
            time_offset=time_offset,
            on_progress=on_progress,
            part_offset=part_offset,
        )
        all_words.extend(words)
        dur          = get_mp3_duration(mp3_path)
        time_offset += dur
        part_offset += dur
    return all_words

# ── Reference word list from chapter text ─────────────────────────────────────
def build_ref_words(chapter: dict, lang: str) -> list[dict]:
    """
    Build reference word list from a chapter's text structure.
    Each entry: {"w": str, "type": "intro"|"block", "bi": int, "part": str, "wi": int}
    Mirrors what generate.py/_build_tts_parts produces so indices match the viewer.
    """
    parts = _build_tts_parts(chapter, lang)
    words: list[dict] = []
    for text_frag, src in parts:
        for wi, word in enumerate(text_frag.split()):
            entry = dict(src)
            entry["w"]  = word
            entry["wi"] = wi
            words.append(entry)
    return words

# ── Alignment: Whisper words → book text positions ────────────────────────────
_NORM_RE = re.compile(r"[^\w]", re.UNICODE)

def _norm(w: str) -> str:
    """Normalize word for comparison: lowercase + strip punctuation."""
    return _NORM_RE.sub("", w.lower())


def align_to_book(whisper_words: list[dict], ref_words: list[dict]) -> list[dict]:
    """
    Align Whisper word timestamps to the reference book word list.

    Strategy:
    1. Normalize both lists (lowercase, strip punctuation).
    2. Use difflib.SequenceMatcher to find matching blocks.
    3. For matched words: assign exact Whisper timestamps.
    4. For unmatched ref words: linearly interpolate between nearest
       matched neighbors (handles narrated text that differs from book).

    Returns a list of TimingWord dicts ready for tts-audio-{lang}.json.
    """
    if not whisper_words or not ref_words:
        return []

    wh_norm = [_norm(w["word"]) for w in whisper_words]
    bk_norm = [_norm(w["w"])   for w in ref_words]

    # Remove empty strings from normalized lists (pure-punctuation words)
    # but keep their indices intact for mapping
    matcher = difflib.SequenceMatcher(None, bk_norm, wh_norm, autojunk=False)

    # bk_idx → wh_idx mapping for matched pairs
    bk2wh: dict[int, int] = {}
    for bk_start, wh_start, length in matcher.get_matching_blocks():
        if length == 0:
            continue
        for i in range(length):
            # Only match non-empty normalized words
            if bk_norm[bk_start + i] and wh_norm[wh_start + i]:
                bk2wh[bk_start + i] = wh_start + i

    matched_bk = sorted(bk2wh.keys())

    result: list[dict] = []
    for bi, bw in enumerate(ref_words):
        entry = {k: v for k, v in bw.items()}

        if bi in bk2wh:
            # Direct match
            whi = bk2wh[bi]
            ww = whisper_words[whi]
            entry["s"] = round(ww["start"], 3)
            entry["e"] = round(ww["end"],   3)

        else:
            # Interpolate between nearest matched neighbors
            prev_bi = max((k for k in matched_bk if k < bi), default=None)
            next_bi = min((k for k in matched_bk if k > bi), default=None)

            if prev_bi is not None and next_bi is not None:
                t0    = whisper_words[bk2wh[prev_bi]]["end"]
                t1    = whisper_words[bk2wh[next_bi]]["start"]
                span  = max(0.0, t1 - t0)
                steps = next_bi - prev_bi
                step  = span / steps if steps > 0 else 0.05
                frac  = bi - prev_bi
                entry["s"] = round(t0 + frac * step, 3)
                entry["e"] = round(t0 + (frac + 1) * step, 3)

            elif prev_bi is not None:
                t0   = whisper_words[bk2wh[prev_bi]]["end"]
                d    = bi - prev_bi
                entry["s"] = round(t0 + d * 0.07, 3)
                entry["e"] = round(t0 + (d + 1) * 0.07, 3)

            elif next_bi is not None:
                t1   = whisper_words[bk2wh[next_bi]]["start"]
                d    = next_bi - bi
                entry["s"] = round(max(0.0, t1 - d * 0.07), 3)
                entry["e"] = round(max(0.0, t1 - (d - 1) * 0.07), 3)

            else:
                # No reference anchors at all — skip this word
                continue

        result.append(entry)

    return result

# ── Alignment quality report ───────────────────────────────────────────────────
def alignment_stats(ref_words: list[dict], timing: list[dict],
                    whisper_words: list[dict]) -> str:
    """Return a short quality summary string."""
    if not ref_words:
        return "no ref words"
    coverage = len(timing) / len(ref_words) * 100 if ref_words else 0
    # Count direct matches (words with s/e matching a whisper word boundary exactly)
    wh_starts = {round(w["start"], 3) for w in whisper_words}
    direct    = sum(1 for t in timing if t.get("s") in wh_starts)
    interp    = len(timing) - direct
    return (f"coverage {coverage:.0f}%  direct {direct}  interpolated {interp}  "
            f"/ whisper {len(whisper_words)} words  ref {len(ref_words)} words")

# ── Analyze one chapter ────────────────────────────────────────────────────────
def get_chapter_duration(entry: dict, dest: Path) -> float:
    """Return total audio duration of a chapter (sum of all parts)."""
    audio = entry.get("audio")
    if not audio:
        return 0.0
    files = [dest / audio] if isinstance(audio, str) else [dest / f for f in audio]
    return sum(get_mp3_duration(f) for f in files if f.exists())


def analyze_chapter(
    chapter: dict,
    entry: dict,
    dest: Path,
    model: object,
    lang: str,
    live: LiveProgress | None = None,
) -> list[dict] | None:
    """
    Transcribe + align one chapter.  live is the LiveProgress display.
    Returns timing list or None on failure.
    """
    # Resolve MP3 file(s)
    audio = entry.get("audio")
    if not audio:
        return None
    if isinstance(audio, str):
        mp3_files = [dest / audio]
    else:
        mp3_files = [dest / f for f in audio]

    missing = [f for f in mp3_files if not f.exists()]
    if missing:
        err(f"MP3 not found: {', '.join(f.name for f in missing)}")
        return None

    # Build reference word list
    ref_words = build_ref_words(chapter, lang)
    if not ref_words:
        warn("No text in chapter — skipping")
        return None

    # Transcribe with live progress
    cb = live.update if live is not None else None
    whisper_words = transcribe_parts(mp3_files, model, language=lang, on_progress=cb)

    if not whisper_words:
        warn("Whisper returned no words")
        return None

    # Align
    timing = align_to_book(whisper_words, ref_words)
    return timing, alignment_stats(ref_words, timing, whisper_words)  # type: ignore[return-value]

# ── List chapters ──────────────────────────────────────────────────────────────
def list_chapters(book_id: str, lang: str) -> None:
    dest = BOOK_DEST / book_id
    tts_path = dest / f"tts-audio-{lang}.json"
    book_md  = dest / "book.md"

    if not book_md.exists():
        err(f"books-dest/{book_id}/book.md not found — run generate.py first")
        return
    if not tts_path.exists():
        err(f"books-dest/{book_id}/tts-audio-{lang}.json not found")
        return

    chapters = md_to_chapters(book_md)
    tts_map  = json.loads(tts_path.read_text(encoding="utf-8"))

    print(f"\n  {bold(gold(book_id))}  ·  {lang.upper()}  ·  {len(chapters)} chapters\n")
    for i, ch in enumerate(chapters):
        ch_id   = ch.get("id", f"ch{i+1}")
        title   = ch.get("title", ch_id)
        entry   = tts_map.get(ch_id, {})
        audio   = entry.get("audio")
        timing  = entry.get("timing")

        if not audio:
            status = gray("no audio")
        elif timing:
            wc = len(timing)
            status = green(f"✓ timing  {wc} words")
        else:
            status = yellow("· audio only (no timing)")

        mp3_label = ""
        if audio:
            if isinstance(audio, list):
                mp3_label = gray(f"  {len(audio)} parts")
            else:
                mp3_label = gray(f"  {audio}")

        print(f"  {dim(str(i+1).rjust(3))}  {ch_id:<40}  {status}{mp3_label}")
    print()

# ── Main analyze function ──────────────────────────────────────────────────────
def analyze_book(
    book_id: str,
    lang: str = "pl",
    chapters_spec: str = "",
    model_name: str = "medium",
    replace: bool = False,
    verbose: bool = False,
) -> bool:
    dest = BOOK_DEST / book_id
    if not dest.exists():
        err(f"books-dest/{book_id}/ not found — run: python generate.py {book_id}")
        return False

    book_md  = dest / "book.md"
    tts_path = dest / f"tts-audio-{lang}.json"

    if not book_md.exists():
        err(f"book.md missing in books-dest/{book_id}/ — run generate.py first")
        return False
    if not tts_path.exists():
        err(f"tts-audio-{lang}.json missing — run generate.py first or ensure audio is mapped")
        return False

    # Load book structure
    step("Loading book structure")
    chapters   = md_to_chapters(book_md)
    ch_by_id   = {ch.get("id", f"ch{i+1}"): ch for i, ch in enumerate(chapters)}
    ordered_ids = [ch.get("id", f"ch{i+1}") for i, ch in enumerate(chapters)]
    ok(f"{len(chapters)} chapters from book.md")

    # Load existing TTS map
    tts_map: dict[str, dict] = json.loads(tts_path.read_text(encoding="utf-8"))

    # Determine which chapters to analyze
    audio_ids = [ch_id for ch_id in ordered_ids if tts_map.get(ch_id, {}).get("audio")]
    if not audio_ids:
        err("No chapters with audio found in tts-audio map")
        return False

    if chapters_spec:
        target_ids = parse_chapters_spec(chapters_spec, audio_ids)
    else:
        target_ids = audio_ids

    # Filter out already done (unless --replace)
    if not replace:
        pending = [ch_id for ch_id in target_ids
                   if not tts_map.get(ch_id, {}).get("timing")]
        skipped = len(target_ids) - len(pending)
        if skipped:
            info(f"Skipping {skipped} already-analyzed chapters (use --replace to redo)")
        target_ids = pending

    if not target_ids:
        ok("Nothing to analyze — all chapters already have timing")
        return True

    # Load Whisper model
    step(f"Loading Whisper model: {bold(model_name)}")
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        err("faster-whisper not installed — run: pip install faster-whisper")
        return False

    try:
        # Use GPU if available, else CPU with int8 quantization
        import torch
        device      = "cuda" if torch.cuda.is_available() else "cpu"
        compute     = "float16" if device == "cuda" else "int8"
    except ImportError:
        device, compute = "cpu", "int8"

    info(f"device={device}  compute_type={compute}")
    try:
        model = WhisperModel(model_name, device=device, compute_type=compute)
    except Exception as e:
        err(f"Failed to load model: {e}")
        return False
    ok(f"Model ready")

    # ── Pre-compute chapter durations for ETA ─────────────────────────────────
    ch_durations = {
        ch_id: get_chapter_duration(tts_map[ch_id], dest)
        for ch_id in target_ids
    }
    total_audio = sum(ch_durations.values())

    # ── Live progress + analyze ────────────────────────────────────────────────
    step(f"Analyzing {len(target_ids)} chapters  ·  lang={lang}"
         + (f"  ·  {_fmt_dur(total_audio)} audio total" if total_audio > 0 else ""))

    live      = LiveProgress(len(target_ids), total_audio)
    wall_start= time.monotonic()
    done      = 0
    failed    = 0

    for i, ch_id in enumerate(target_ids):
        if _stop:
            warn("Stopped by user — progress saved")
            break

        chapter = ch_by_id.get(ch_id)
        if not chapter:
            warn(f"Chapter '{ch_id}' not found in book.md — skipping")
            continue

        entry    = tts_map.get(ch_id, {})
        title    = chapter.get("title", ch_id)
        ch_dur   = ch_durations.get(ch_id, 0.0)
        live.begin_chapter(i + 1, title, ch_dur)

        try:
            result = analyze_chapter(chapter, entry, dest, model, lang, live)
            if result is None:
                live.fail_chapter(ch_dur)
                failed += 1
            else:
                timing, stats = result
                live.finish_chapter(ch_dur, stats)
                tts_map[ch_id]["timing"] = timing
                done += 1
                # Save after each chapter (crash-safe)
                tts_path.write_text(
                    json.dumps(tts_map, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
        except Exception as e:
            live.fail_chapter(ch_dur)
            err(f"Error: {e}")
            import traceback; traceback.print_exc()
            failed += 1

    # ── Summary ────────────────────────────────────────────────────────────────
    total_time = time.monotonic() - wall_start
    print()
    if done:
        ok(f"{done}/{len(target_ids)} chapters done  ·  {_fmt_secs(total_time)} total")
    if failed:
        warn(f"{failed} chapters failed")
    ok(f"Saved → {tts_path.relative_to(PROJECT_DIR)}")
    info(f"Next: python publish.py {book_id}")
    print()
    return True

# ── CLI ────────────────────────────────────────────────────────────────────────
def main() -> None:
    p = argparse.ArgumentParser(
        prog="analyze-tts.py",
        description="Word-level timing alignment for Atlas Books (MP3 → timing JSON)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Install deps:")[0].strip(),
    )
    p.add_argument("book_id", nargs="?", help="Book ID (directory name in books-dest/)")
    p.add_argument("--lang",     default="pl",     metavar="LANG",
                   help="Language code: pl, en  (default: pl)")
    p.add_argument("--chapters", default="",       metavar="SPEC",
                   help="Chapter range: '1-5', '3,7,12', 'all'  (default: all pending)")
    p.add_argument("--model",    default="medium", metavar="MODEL",
                   help="Whisper model: tiny/base/small/medium/large-v3  (default: medium)")
    p.add_argument("--replace",  action="store_true",
                   help="Re-analyze chapters that already have timing data")
    p.add_argument("--list",     action="store_true",
                   help="List chapters and their audio/timing status, then exit")
    p.add_argument("--list-models", action="store_true",
                   help="Print available Whisper models and exit")
    p.add_argument("--verbose",  action="store_true",
                   help="Show detailed progress (multi-part transcription, errors)")

    args = p.parse_args()

    if args.list_models:
        print("\n  Whisper models (faster-whisper):\n")
        models = [
            ("tiny",     "~39M",  "fastest, rough"),
            ("base",     "~74M",  "fast, decent"),
            ("small",    "~244M", "good balance"),
            ("medium",   "~769M", "recommended for Polish  [default]"),
            ("large-v2", "~1.5G", "high accuracy"),
            ("large-v3", "~1.5G", "best accuracy"),
        ]
        for name, size, note in models:
            marker = gold("◆") if name == "medium" else dim("○")
            print(f"  {marker}  {bold(name):<12}  {gray(size):<8}  {note}")
        print()
        return

    if not args.book_id:
        # List all books with their status
        if not BOOK_DEST.exists():
            err(f"books-dest/ not found: {BOOK_DEST}"); sys.exit(1)
        print(f"\n  {bold(gold('Atlas Books — analyze-tts status'))}\n")
        for d in sorted(BOOK_DEST.iterdir()):
            if not d.is_dir(): continue
            tts = d / f"tts-audio-{args.lang}.json"
            if not tts.exists():
                print(f"  {dim('○')} {d.name}  {gray('no tts-audio map')}")
                continue
            try:
                data = json.loads(tts.read_text(encoding="utf-8"))
            except Exception:
                print(f"  {dim('○')} {d.name}  {red('invalid JSON')}"); continue
            with_audio   = sum(1 for v in data.values() if isinstance(v, dict) and v.get("audio"))
            with_timing  = sum(1 for v in data.values() if isinstance(v, dict) and v.get("timing"))
            if with_audio == 0:
                status = gray("no audio")
            elif with_timing == with_audio:
                status = green(f"✓ all timing  ({with_timing}/{with_audio})")
            elif with_timing > 0:
                status = yellow(f"· partial  ({with_timing}/{with_audio} chapters)")
            else:
                status = yellow(f"· audio only  ({with_audio} ch, no timing)")
            print(f"  {gold('►')} {bold(d.name):<45}  {status}")
        print()
        info(f"Run: python analyze-tts.py <book-id>")
        print()
        return

    if args.list:
        list_chapters(args.book_id, args.lang)
        return

    success = analyze_book(
        book_id      = args.book_id,
        lang         = args.lang,
        chapters_spec= args.chapters,
        model_name   = args.model,
        replace      = args.replace,
        verbose      = args.verbose,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
