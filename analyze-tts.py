#!/usr/bin/env python3
"""
analyze-tts.py — Word-level timing alignment for Atlas Books

Pipeline position:
  generate.py  →  [generate-tts.py]  →  analyze-tts.py  →  publish.py

Two-stage workflow:
  Stage 1 (transcribe): MP3s → raw Whisper words → tts-transcript-{lang}.json
  Stage 2 (align):      tts-transcript-{lang}.json + book.md → tts-audio-{lang}.json

Usage:
  python analyze-tts.py homer-odyseja                  # both stages (default)
  python analyze-tts.py homer-odyseja --stage transcribe  # stage 1 only (Whisper → transcript)
  python analyze-tts.py homer-odyseja --stage align       # stage 2 only (transcript → timing)
  python analyze-tts.py homer-odyseja --lang pl
  python analyze-tts.py homer-odyseja --chapters 1-5
  python analyze-tts.py homer-odyseja --model large-v3
  python analyze-tts.py homer-odyseja --replace        # re-run completed chapters
  python analyze-tts.py homer-odyseja --list           # show chapter/audio/transcript status
  python analyze-tts.py --list-models

Fixing wrong alignment without re-transcribing:
  1. python analyze-tts.py book-id --stage transcribe --replace
     → saves raw Whisper words to tts-transcript-{lang}.json
  2. edit tts-transcript-{lang}.json if needed (fix chapter key mapping)
  3. python analyze-tts.py book-id --stage align --replace
     → re-aligns using saved transcript; fast, no Whisper needed

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

_DIAC_TABLE = str.maketrans(
    "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ",
    "acelnoszzACELNOSZZ",
)

def _diac_norm(w: str) -> str:
    """Normalize + strip Polish diacritics — for fuzzy matching with noisy ASR."""
    return _norm(w.translate(_DIAC_TABLE))


def build_full_ref_words(chapters: list[dict], lang: str) -> list[dict]:
    """
    Build flat reference word list for the entire book.
    Each word carries chapter_id so smart_align_book can assign timing to chapters.
    """
    all_words: list[dict] = []
    for ch in chapters:
        ch_id = ch.get("id", "")
        for w in build_ref_words(ch, lang):
            w = dict(w)
            w["chapter_id"] = ch_id
            all_words.append(w)
    return all_words


def scan_audio_groups(dest: Path, book_id: str) -> dict[str, list[Path]]:
    """
    Scan dest/ for MP3 files and group by chapter.

    Two modes:
    • Per-chapter files  — names contain ch{N} (e.g. *-ch1-part000.mp3)
                           → {ch1: [...], ch2: [...], …}
    • Single / whole-book file — no ch{N} in any filename
                           → {"ch0": [all mp3s sorted by name]}
                           The aligner will find seek_to per chapter automatically.
    """
    groups: dict[str, list[Path]] = {}
    for mp3 in sorted(dest.glob("*.mp3")):
        m = re.search(r'(?:^|-)ch(\d+)', mp3.stem)
        if m:
            key = f"ch{m.group(1)}"
            groups.setdefault(key, []).append(mp3)
    if groups:
        return dict(sorted(groups.items(), key=lambda x: int(x[0][2:])))

    # No ch{N} files — treat all MP3s as a single whole-book group
    all_mp3 = sorted(dest.glob("*.mp3"))
    if all_mp3:
        info(f"No ch{{N}} files — treating {len(all_mp3)} MP3(s) as single group (whole-book audio)")
        return {"ch0": all_mp3}
    return {}


def smart_align_book(
    all_whisper: list[dict],
    all_ref: list[dict],
    min_density: float = 0.05,
) -> tuple[dict[int, int], set[str], list[dict]]:
    """
    Whole-book fuzzy alignment: Whisper words against the full book reference.

    Two-phase strategy:
      Phase 1 — exact match after normalization (lowercase, strip punctuation)
      Phase 2 — diacritic-insensitive match for words missed in phase 1
                 (handles tiny-model errors like 'panstwo' → 'państwo')

    After both phases, global monotonicity is enforced (bk_idx AND wh_idx both
    must be strictly increasing) to prevent phase-2 spurious matches from creating
    backwards timestamps across chapter boundaries.

    Only chapters where direct_matches / total_ref_words ≥ min_density are
    considered "covered" and get timing/audio assigned. This prevents chapters
    with zero real audio from receiving spurious timing via interpolation.

    Returns:
      bk2wh   — {book_word_idx: whisper_word_idx} for direct matches
      covered — set of chapter_id strings with sufficient match density
      timing  — timing list for covered chapters only (includes 'chapter_id')
    """
    if not all_whisper or not all_ref:
        return {}, set(), []

    wh_norm   = [_norm(w["word"])       for w in all_whisper]
    wh_nodiac = [_diac_norm(w["word"])  for w in all_whisper]
    bk_norm   = [_norm(w["w"])          for w in all_ref]
    bk_nodiac = [_diac_norm(w["w"])     for w in all_ref]

    # ── Phase 1: exact normalized match ───────────────────────────────────────
    m1 = difflib.SequenceMatcher(None, bk_norm, wh_norm, autojunk=False)
    bk2wh: dict[int, int] = {}
    for bk_s, wh_s, length in m1.get_matching_blocks():
        if length == 0:
            continue
        for i in range(length):
            if bk_norm[bk_s + i] and wh_norm[wh_s + i]:
                bk2wh[bk_s + i] = wh_s + i

    # ── Phase 2: diacritic-stripped match on remainder ─────────────────────────
    used_wh          = set(bk2wh.values())
    unmatched_bk_idx = [i for i in range(len(all_ref))     if i not in bk2wh     and bk_nodiac[i]]
    unmatched_wh_idx = [j for j in range(len(all_whisper)) if j not in used_wh   and wh_nodiac[j]]

    if unmatched_bk_idx and unmatched_wh_idx:
        sub_bk = [bk_nodiac[i] for i in unmatched_bk_idx]
        sub_wh = [wh_nodiac[j] for j in unmatched_wh_idx]
        m2 = difflib.SequenceMatcher(None, sub_bk, sub_wh, autojunk=False)
        for b_s, w_s, length in m2.get_matching_blocks():
            if length == 0:
                continue
            for k in range(length):
                orig_bk = unmatched_bk_idx[b_s + k]
                orig_wh = unmatched_wh_idx[w_s + k]
                if orig_bk not in bk2wh:
                    bk2wh[orig_bk] = orig_wh

    # ── Global monotonicity — enforce bk_idx AND wh_idx strictly increasing ───
    # Phase 2 can create backwards whisper-index assignments across chapter
    # boundaries (e.g. ch2 word matched to wh[500] while ch1 word → wh[3000]).
    bk2wh_clean: dict[int, int] = {}
    last_wh = -1
    for bk_idx, wh_idx in sorted(bk2wh.items()):
        if wh_idx > last_wh:
            bk2wh_clean[bk_idx] = wh_idx
            last_wh = wh_idx
    bk2wh = bk2wh_clean

    # ── Per-chapter direct match density → which chapters are "covered" ────────
    ch_direct: dict[str, int] = {}
    ch_total:  dict[str, int] = {}
    for bi, bw in enumerate(all_ref):
        ch_id = bw.get("chapter_id", "")
        ch_total[ch_id]  = ch_total.get(ch_id, 0) + 1
        if bi in bk2wh:
            ch_direct[ch_id] = ch_direct.get(ch_id, 0) + 1

    covered: set[str] = {
        ch_id
        for ch_id, total in ch_total.items()
        if ch_direct.get(ch_id, 0) / max(1, total) >= min_density
    }

    # ── Build timing — covered chapters only; interpolate from covered anchors ─
    # Using only covered-chapter matches as anchors prevents cross-chapter
    # interpolation from pulling uncovered chapter words into wrong time ranges.
    covered_sorted = sorted(bi for bi in bk2wh if all_ref[bi].get("chapter_id") in covered)

    result: list[dict] = []
    for bi, bw in enumerate(all_ref):
        ch_id = bw.get("chapter_id", "")
        if ch_id not in covered:
            continue   # skip uncovered chapters entirely

        entry = {k: v for k, v in bw.items()}   # includes chapter_id

        if bi in bk2wh:
            whi = bk2wh[bi]
            ww  = all_whisper[whi]
            entry["s"] = round(ww["start"], 3)
            entry["e"] = round(ww["end"],   3)
        else:
            prev_bi = max((k for k in covered_sorted if k < bi), default=None)
            next_bi = min((k for k in covered_sorted if k > bi), default=None)

            if prev_bi is not None and next_bi is not None:
                t0     = all_whisper[bk2wh[prev_bi]]["end"]
                t1     = all_whisper[bk2wh[next_bi]]["start"]
                span   = max(0.0, t1 - t0)
                steps  = next_bi - prev_bi
                step_t = span / steps if steps > 0 else 0.05
                frac   = bi - prev_bi
                entry["s"] = round(t0 + frac * step_t, 3)
                entry["e"] = round(t0 + (frac + 1) * step_t, 3)
            elif prev_bi is not None:
                t0 = all_whisper[bk2wh[prev_bi]]["end"]
                d  = bi - prev_bi
                entry["s"] = round(t0 + d * 0.07, 3)
                entry["e"] = round(t0 + (d + 1) * 0.07, 3)
            elif next_bi is not None:
                t1 = all_whisper[bk2wh[next_bi]]["start"]
                d  = next_bi - bi
                entry["s"] = round(max(0.0, t1 - d * 0.07), 3)
                entry["e"] = round(max(0.0, t1 - (d - 1) * 0.07), 3)
            else:
                continue   # no anchor at all — omit word

        result.append(entry)

    return bk2wh, covered, result


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


def transcribe_chapter(
    entry: dict,
    dest: Path,
    model: object,
    lang: str,
    live: LiveProgress | None = None,
) -> list[dict] | None:
    """
    Stage 1: transcribe MP3 files for one chapter using Whisper.
    Returns list of {word, start, end} dicts or None on failure.
    """
    audio = entry.get("audio")
    if not audio:
        return None
    mp3_files = [dest / audio] if isinstance(audio, str) else [dest / f for f in audio]
    missing = [f for f in mp3_files if not f.exists()]
    if missing:
        err(f"MP3 not found: {', '.join(f.name for f in missing)}")
        return None
    cb = live.update if live is not None else None
    words = transcribe_parts(mp3_files, model, language=lang, on_progress=cb)
    if not words:
        warn("Whisper returned no words")
        return None
    return words


def align_chapter(
    chapter: dict,
    whisper_words: list[dict],
    lang: str,
) -> tuple[list[dict], str] | None:
    """
    Stage 2: align whisper words to book.md reference text.
    Returns (timing, stats_string) or None on failure.
    """
    ref_words = build_ref_words(chapter, lang)
    if not ref_words:
        warn("No text in chapter — skipping")
        return None
    timing = align_to_book(whisper_words, ref_words)
    return timing, alignment_stats(ref_words, timing, whisper_words)


def analyze_chapter(
    chapter: dict,
    entry: dict,
    dest: Path,
    model: object,
    lang: str,
    live: LiveProgress | None = None,
    whisper_words: list[dict] | None = None,
) -> tuple[list[dict], str] | None:
    """
    Full pipeline: transcribe (unless whisper_words provided) + align.
    Returns (timing, stats) or None on failure.
    """
    if whisper_words is None:
        whisper_words = transcribe_chapter(entry, dest, model, lang, live)
        if whisper_words is None:
            return None
    return align_chapter(chapter, whisper_words, lang)

# ── List chapters ──────────────────────────────────────────────────────────────
def list_chapters(book_id: str, lang: str) -> None:
    dest = BOOK_DEST / book_id
    tts_path    = dest / f"tts-audio-{lang}.json"
    tr_path     = dest / f"tts-transcript-{lang}.json"
    book_md     = dest / "book.md"

    if not book_md.exists():
        err(f"books-dest/{book_id}/book.md not found — run generate.py first")
        return
    if not tts_path.exists():
        err(f"books-dest/{book_id}/tts-audio-{lang}.json not found")
        return

    chapters = md_to_chapters(book_md)
    tts_map  = json.loads(tts_path.read_text(encoding="utf-8"))
    tr_map: dict = {}
    if tr_path.exists():
        try:
            tr_map = json.loads(tr_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    print(f"\n  {bold(gold(book_id))}  ·  {lang.upper()}  ·  {len(chapters)} chapters\n")
    print(f"  {'#':>3}  {'chapter id':<40}  {'timing':<26}  transcript")
    print(f"  {'-'*3}  {'-'*40}  {'-'*26}  ----------")
    for i, ch in enumerate(chapters):
        ch_id   = ch.get("id", f"ch{i+1}")
        entry   = tts_map.get(ch_id, {})
        audio   = entry.get("audio")
        timing  = entry.get("timing")
        tr_entry = tr_map.get(ch_id, {})
        tr_words = tr_entry.get("words")

        if not audio:
            t_status = gray("no audio")
        elif timing:
            t_status = green(f"✓ {len(timing)} words")
        else:
            t_status = yellow("· no timing")

        if tr_words:
            tr_status = green(f"✓ {len(tr_words)} words")
        elif audio:
            tr_status = yellow("· not saved")
        else:
            tr_status = gray("—")

        mp3_label = ""
        if audio:
            mp3_label = gray(f"  [{len(audio) if isinstance(audio, list) else 1} mp3]")

        print(f"  {dim(str(i+1).rjust(3))}  {ch_id:<40}  {t_status:<35}  {tr_status}{mp3_label}")
    print()
    if tr_path.exists():
        info(f"Transcript: {tr_path.relative_to(PROJECT_DIR)}")
    else:
        info(f"No transcript file yet — run: python analyze-tts.py {book_id} --stage transcribe")
    print()

# ── Main analyze function ──────────────────────────────────────────────────────
def analyze_book(
    book_id: str,
    lang: str = "pl",
    chapters_spec: str = "",
    model_name: str = "medium",
    replace: bool = False,
    verbose: bool = False,
    stage: str = "both",   # "transcribe" | "align" | "both"
) -> bool:
    dest = BOOK_DEST / book_id
    if not dest.exists():
        err(f"books-dest/{book_id}/ not found — run: python generate.py {book_id}")
        return False

    book_md    = dest / "book.md"
    tts_path   = dest / f"tts-audio-{lang}.json"
    tr_path    = dest / f"tts-transcript-{lang}.json"
    align_path = dest / f"tts-transcript-align-{lang}.json"

    if not book_md.exists():
        err(f"book.md missing in books-dest/{book_id}/ — run generate.py first")
        return False

    do_transcribe = stage in ("transcribe", "both")
    do_align      = stage in ("align", "both")

    wall_start = time.monotonic()

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE 1 — TRANSCRIBE: scan MP3 files by filename, build transcript
    # ══════════════════════════════════════════════════════════════════════════
    if do_transcribe:
        # Scan directory for {book_id}-ch{N}[-part*].mp3
        audio_groups = scan_audio_groups(dest, book_id)
        if not audio_groups:
            err(f"No MP3 files found in books-dest/{book_id}/ — run generate.py first")
            return False

        # Load existing transcript (ch-indexed only — discard old chapter-slug keys)
        tr_map: dict[str, dict] = {}
        if tr_path.exists():
            try:
                raw = json.loads(tr_path.read_text(encoding="utf-8"))
                tr_map = {k: v for k, v in raw.items() if re.match(r"ch\d+$", k)}
                old_skipped = len(raw) - len(tr_map)
                if old_skipped:
                    info(f"Discarded {old_skipped} non-ch{{N}} entries from old transcript")
            except Exception:
                warn(f"Could not parse {tr_path.name} — starting fresh")

        # Filter groups to process
        if chapters_spec:
            # chapters_spec here refers to ch-group numbers: "1-3" → ch1, ch2, ch3
            nums   = parse_chapters_spec(chapters_spec, list(audio_groups.keys()))
            groups = {k: v for k, v in audio_groups.items() if k in set(nums)}
        else:
            groups = audio_groups

        if not replace:
            pending_groups = {k: v for k, v in groups.items()
                              if not tr_map.get(k, {}).get("words")}
            skipped_n = len(groups) - len(pending_groups)
            if skipped_n:
                info(f"Skipping {skipped_n} already-transcribed groups (use --replace to redo)")
        else:
            pending_groups = groups

        if pending_groups:
            # ── Load Whisper model ────────────────────────────────────────────
            step(f"Loading Whisper model: {bold(model_name)}")
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                err("faster-whisper not installed — run: pip install faster-whisper")
                return False
            try:
                import torch
                device  = "cuda" if torch.cuda.is_available() else "cpu"
                compute = "float16" if device == "cuda" else "int8"
            except ImportError:
                device, compute = "cpu", "int8"
            info(f"device={device}  compute_type={compute}")
            try:
                model = WhisperModel(model_name, device=device, compute_type=compute)
            except Exception as e:
                err(f"Failed to load model: {e}"); return False
            ok("Model ready")

            total_audio = sum(
                sum(get_mp3_duration(f) for f in files)
                for files in pending_groups.values()
            )
            step(f"Transcribing {len(pending_groups)} audio groups  ·  lang={lang}"
                 + (f"  ·  {_fmt_dur(total_audio)} audio" if total_audio > 0 else ""))
            live = LiveProgress(len(pending_groups), total_audio)
            done_t, failed_t = 0, 0

            for i, (ch_key, mp3_files) in enumerate(pending_groups.items()):
                if _stop:
                    warn("Stopped by user — transcript saved")
                    break
                ch_dur = sum(get_mp3_duration(f) for f in mp3_files)
                live.begin_chapter(i + 1, ch_key, ch_dur)
                try:
                    cb    = live.update
                    words = transcribe_parts(mp3_files, model, language=lang, on_progress=cb)
                    if not words:
                        warn(f"Whisper returned no words for {ch_key}")
                        live.fail_chapter(ch_dur)
                        failed_t += 1
                        continue
                    tr_map[ch_key] = {
                        "audio": [f.name for f in mp3_files],
                        "words": words,
                    }
                    tr_path.write_text(
                        json.dumps(tr_map, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    live.finish_chapter(ch_dur, f"{len(words)} words")
                    done_t += 1
                except Exception as e:
                    live.fail_chapter(ch_dur)
                    err(f"Error transcribing {ch_key}: {e}")
                    import traceback; traceback.print_exc()
                    failed_t += 1

            total_time = time.monotonic() - wall_start
            print()
            if done_t:
                ok(f"{done_t}/{len(pending_groups)} groups transcribed  ·  {_fmt_secs(total_time)}")
            if failed_t:
                warn(f"{failed_t} groups failed")
            ok(f"Transcript → {tr_path.relative_to(PROJECT_DIR)}")
        else:
            ok("All audio groups already transcribed")

        if not do_align:
            info(f"Next: python analyze-tts.py {book_id} --stage align")
            print()
            return True

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE 2 — ALIGN: whole-book fuzzy matching → tts-transcript-align + tts-audio
    # ══════════════════════════════════════════════════════════════════════════
    if do_align:
        # Load transcript
        if not tr_path.exists():
            err(f"tts-transcript-{lang}.json not found — run --stage transcribe first")
            return False
        try:
            raw_tr = json.loads(tr_path.read_text(encoding="utf-8"))
            # Accept only ch{N}-keyed entries; discard old chapter-slug keys
            tr_map = {k: v for k, v in raw_tr.items() if re.match(r"ch\d+$", k)}
            stale = len(raw_tr) - len(tr_map)
            if stale:
                info(f"Ignoring {stale} non-ch{{N}} entries in transcript (old format)")
        except Exception as e:
            err(f"Cannot parse tts-transcript-{lang}.json: {e}"); return False

        # Sort ch-keys by number (ch0 < ch1 < ch2 …)
        ch_keys = sorted(
            (k for k in tr_map if tr_map[k].get("words")),
            key=lambda k: int(re.search(r"\d+", k).group()),  # safe: re.match above guarantees digits
        )
        if not ch_keys:
            err("No transcribed groups in transcript file — run --stage transcribe first")
            return False

        # Build flat whisper list + track source ranges
        all_whisper: list[dict] = []
        ch_word_ranges: dict[str, tuple[int, int]] = {}
        for ck in ch_keys:
            start = len(all_whisper)
            all_whisper.extend(tr_map[ck]["words"])
            ch_word_ranges[ck] = (start, len(all_whisper))

        # Load book
        step("Loading book structure")
        chapters    = md_to_chapters(book_md)
        ok(f"{len(chapters)} chapters from book.md")

        # Build flat book reference (all chapters, chapter_id attached)
        step(f"Smart whole-book alignment  ·  lang={lang}  ·  "
             f"{len(all_whisper)} whisper words  ·  {len(chapters)} chapters")
        all_ref = build_full_ref_words(chapters, lang)
        info(f"Book reference: {len(all_ref)} words total")

        # Run smart alignment (2-phase: exact + diacritic-insensitive)
        bk2wh, covered, all_timing = smart_align_book(all_whisper, all_ref)

        # ── Detect whole-book mode ────────────────────────────────────────────
        # Single audio group = one continuous recording covering all chapters.
        # In this mode every chapter gets audio + seek_to; timing is optional.
        whole_book_mode = len(ch_keys) == 1
        if whole_book_mode:
            info("Whole-book mode: all chapters will receive audio + seek_to")

        # ── Audio assignment ───────────────────────────────────────────────────
        # Per-chapter mode: only covered chapters vote for their audio group.
        # Whole-book mode:  all chapters with any match vote; gaps filled with ch0.
        wh_to_chkey: dict[int, str] = {}
        for ck, (start, end) in ch_word_ranges.items():
            for j in range(start, end):
                wh_to_chkey[j] = ck

        ch_audio_votes: dict[str, dict[str, int]] = {}
        for bk_idx, wh_idx in bk2wh.items():
            ch_id = all_ref[bk_idx].get("chapter_id", "")
            if ch_id not in covered and not whole_book_mode:
                continue   # per-chapter: only covered chapters get audio
            ck = wh_to_chkey.get(wh_idx, "")
            if ch_id and ck:
                ch_audio_votes.setdefault(ch_id, {})
                ch_audio_votes[ch_id][ck] = ch_audio_votes[ch_id].get(ck, 0) + 1

        chapter_to_audio: dict[str, list[str]] = {}
        for ch_id, votes in ch_audio_votes.items():
            best_ck = max(votes, key=votes.__getitem__)
            chapter_to_audio[ch_id] = tr_map[best_ck].get("audio", [])

        if whole_book_mode:
            # Every chapter shares the same single audio group; fill any gaps
            whole_audio = tr_map[ch_keys[0]].get("audio", [])
            for ch in chapters:
                ch_id = ch.get("id", "")
                if ch_id and ch_id not in chapter_to_audio:
                    chapter_to_audio[ch_id] = whole_audio

        # ── Compute seek_to from ALL matched words (not just covered chapters) ─
        # Use bk2wh directly so even below-threshold chapters get a seek_to if
        # at least one word matched.  In whole-book mode this is essential.
        ch_seek_to: dict[str, float] = {}
        for bk_idx in sorted(bk2wh.keys()):
            ch_id = all_ref[bk_idx].get("chapter_id", "")
            if ch_id and ch_id not in ch_seek_to:
                whi = bk2wh[bk_idx]
                ch_seek_to[ch_id] = round(all_whisper[whi]["start"], 3)

        # ── Group timing by chapter_id (covered chapters only) ────────────────
        ch_timing: dict[str, list[dict]] = {}
        for entry in all_timing:
            ch_id = entry.pop("chapter_id", "")
            ch_timing.setdefault(ch_id, []).append(entry)

        # ── Coverage report ────────────────────────────────────────────────────
        matched_bk  = len(bk2wh)
        total_ref   = len(all_ref)
        total_wh    = len(all_whisper)
        book_cov    = matched_bk / total_ref * 100 if total_ref else 0
        wh_starts   = {round(w["start"], 3) for w in all_whisper}
        direct      = sum(1 for t in all_timing if t.get("s") in wh_starts)
        interp      = len(all_timing) - direct
        ok(f"Alignment: {book_cov:.0f}% coverage  "
           f"direct {direct}  interpolated {interp}  "
           f"/ whisper {total_wh}  ref {total_ref}")
        info(f"Covered chapters ({len(covered)}): {', '.join(sorted(covered))}")

        # ── Save tts-transcript-align-{lang}.json ─────────────────────────────
        # In per-chapter mode: only covered chapters are included.
        # In whole-book mode:  all chapters with an audio assignment are included
        #   (timing is still only for covered chapters; seek_to for any matched).
        align_out: dict[str, dict] = {}
        for ch in chapters:
            ch_id = ch.get("id", "")
            audio   = chapter_to_audio.get(ch_id)
            if not audio:
                continue  # no audio → skip entirely
            if not whole_book_mode and ch_id not in covered:
                continue  # per-chapter: only covered chapters in output
            timing  = ch_timing.get(ch_id, [])
            seek_to = ch_seek_to.get(ch_id)
            out_entry: dict = {"audio": audio}
            if timing:
                out_entry["timing"] = timing
            if seek_to is not None:
                out_entry["seek_to"] = seek_to
            align_out[ch_id] = out_entry
        align_path.write_text(
            json.dumps(align_out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        ok(f"Align map  → {align_path.relative_to(PROJECT_DIR)}")

        # ── Update tts-audio-{lang}.json ──────────────────────────────────────
        # Load existing (or start fresh) and replace with correct assignments
        tts_map: dict[str, dict] = {}
        if tts_path.exists():
            try:
                tts_map = json.loads(tts_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        # Remove old wrong-chapter entries not in current book
        valid_ids = {ch.get("id", "") for ch in chapters}
        for k in list(tts_map.keys()):
            if k not in valid_ids:
                del tts_map[k]
        # Write correct entries — only covered chapters
        for ch_id, data in align_out.items():
            timing  = data.get("timing", [])
            audio   = data.get("audio")
            seek_to = data.get("seek_to")
            tts_entry: dict = {}
            if audio:
                tts_entry["audio"] = audio
            if seek_to is not None:
                tts_entry["seek_to"] = seek_to
            if timing:
                tts_entry["timing"] = timing
            if tts_entry:
                tts_map[ch_id] = tts_entry
            else:
                tts_map.pop(ch_id, None)
        # Remove chapters that are no longer in align_out.
        # In whole-book mode we keep existing entries that still have valid audio
        # (align_out already includes all chapters with audio, so this mainly
        # cleans up per-chapter mode orphans).
        for ch_id in list(tts_map.keys()):
            if ch_id not in align_out and ch_id in valid_ids:
                if not whole_book_mode:
                    tts_map.pop(ch_id, None)
        tts_path.write_text(
            json.dumps(tts_map, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        ok(f"Audio map  → {tts_path.relative_to(PROJECT_DIR)}")

        total_time = time.monotonic() - wall_start
        info(f"Total time: {_fmt_secs(total_time)}")
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
    p.add_argument("--stage",    default="both",   metavar="STAGE",
                   choices=["transcribe", "align", "both"],
                   help="transcribe: MP3→transcript only  |  align: transcript→timing only  |  both (default)")
    p.add_argument("--chapters", default="",       metavar="SPEC",
                   help="Chapter range: '1-5', '3,7,12', 'all'  (default: all pending)")
    p.add_argument("--model",    default="medium", metavar="MODEL",
                   help="Whisper model: tiny/base/small/medium/large-v3  (default: medium)")
    p.add_argument("--replace",  action="store_true",
                   help="Re-process chapters that are already done")
    p.add_argument("--list",     action="store_true",
                   help="List chapters with audio/transcript/timing status, then exit")
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
        book_id       = args.book_id,
        lang          = args.lang,
        chapters_spec = args.chapters,
        model_name    = args.model,
        replace       = args.replace,
        verbose       = args.verbose,
        stage         = args.stage,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
