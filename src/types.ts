export interface BookBlock {
  type: 'paragraph' | 'heading' | 'quote' | 'divider' | 'callout' | 'image' | 'verse'
  text?: string
  level?: number
  attribution?: string
  url?: string
  alt?: string
  caption?: string
  label?: string
}

/** One word entry from tts.py — embedded in book.json as timing_pl / timing_en */
export interface TimingWord {
  w: string                              // word text (from TTS)
  s: number                              // start time (seconds)
  e: number                              // end time (seconds)
  type: 'intro' | 'block'               // intro = chapter title/number, block = body
  bi?: number                            // block index in chapter.blocks
  part?: 'text' | 'label' | 'attr'      // which part of the block
  wi?: number                            // word index within that part
}

export interface Chapter {
  id: string
  number: string | number
  title: string
  subtitle?: string
  blocks: BookBlock[]
  audio_pl?: string | string[]  // path(s) to Polish MP3 — array when file was split at publish time
  audio_en?: string | string[]  // path(s) to English MP3
  timing_pl?: TimingWord[]  // word timing data for PL audio (embedded by tts.py)
  timing_en?: TimingWord[]  // word timing data for EN audio
}

export interface BookAttribution {
  statement: string    // "Książka pochodzi z serwisu Wolne Lektury"
  source: string       // "Wolne Lektury"
  source_url: string   // "https://wolnelektury.pl"
  book_url?: string    // link to specific book page
  license?: string     // e.g. "CC BY-SA 3.0"
  epub_url?: string    // external download link (e.g. wolnelektury.pl EPUB)
  pdf_url?: string     // external download link (e.g. wolnelektury.pl PDF)
  mp3_zip_url?: string // external MP3 zip link
}

/** Passed to BlockRenderer so it can highlight bookmarked text ranges */
export interface BookmarkHighlight {
  id:           string
  blockIdx:     number
  selectedText: string
}

export interface Book {
  id?: string      // from config.json — used as localStorage key for bookmarks
  title: string
  subtitle?: string
  author?: string
  year?: string
  description?: string
  cover?: string   // base64 data URI or URL — empty = no cover image
  attribution?: BookAttribution
  audio_source?: string  // e.g. "wolnelektury", "generated", "custom" — set in config.json
  audio_label?: string   // custom display label overriding derived label — set in config.json
  chapters: Chapter[]
}
