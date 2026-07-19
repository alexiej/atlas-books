import {
  useState,
  useEffect,
  useRef,
  useCallback,
} from 'react'
import { prepare, layout } from '@chenglou/pretext'
import type { Book, BookBlock, TimingWord, Chapter, BookmarkHighlight } from './types'
import { BlockRenderer } from './components/BlockRenderer'
import devBookData from './dev-book.json'

declare global { interface Window { __BOOK_DATA__?: Book | null } }
const book = (window.__BOOK_DATA__ ?? devBookData) as Book

// ── Bookmark storage ──────────────────────────────────────────────────────────
// Bookmarks are stored in localStorage (browser-local, never sent to a server).
const BOOK_ID = book.id
  || book.title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')
const BM_KEY  = `sf-bm-${BOOK_ID}`
const POS_KEY = `sf-pos-${BOOK_ID}`   // saved MP3 chapter + time

interface Bookmark {
  id:           string              // unique key (timestamp + random)
  pageIdx:      number              // absolute page index for direct navigation
  chapterIdx:   number
  subPage:      number
  blockIdx:     number              // block within the chapter where text was selected
  selectedText: string              // the highlighted text snippet
  chapterTitle: string
  chapterNum:   string | number
  comment:      string
  createdAt:    string              // ISO 8601
}

function bmLoad(): Bookmark[] {
  try { return JSON.parse(localStorage.getItem(BM_KEY) || '[]') } catch { return [] }
}

type Theme = 'light' | 'dark'

// ── Size presets ──────────────────────────────────────────────────────────────
const PRESETS = [
  { label: 'S',  fontSize: 14, paginate: false },
  { label: 'M',  fontSize: 16, paginate: true  },
  { label: 'L',  fontSize: 18, paginate: true  },
  { label: 'XL', fontSize: 21, paginate: true  },
] as const

// ── Page model ────────────────────────────────────────────────────────────────
interface PageSpec {
  kind: 'cover' | 'content'
  chapterIdx: number
  subPage: number
  totalSubs: number
  blockStart: number   // inclusive index into chapter.blocks
  blockEnd: number     // exclusive
  scrollable?: boolean // true for S preset
}

// ── Block height measurement ──────────────────────────────────────────────────
// Measures a single block's rendered height using pretext (Canvas-based, no DOM).
// blockIdx = position within chapter.blocks (for gap and drop-cap detection).
function measureBlockH(
  block: BookBlock,
  blockIdx: number,
  isFirstChapterBlock: boolean,
  fontSize: number,
  contentWidth: number,
): number {
  const rem   = fontSize
  const lineH = fontSize * 1.82          // matches CSS line-height: 1.82
  const gap   = blockIdx > 0 ? 1.3 * rem : 0  // .block + .block { margin-top: 1.3rem }

  let h = 0
  switch (block.type) {
    case 'paragraph': {
      const handle = prepare(
        block.text || ' ',
        `${fontSize}px "Crimson Text", Georgia, serif`,
      )
      h = layout(handle, Math.max(10, contentWidth), lineH).height
      // Drop cap on very first paragraph: ::first-letter floats at 4.2em,
      // pushing remaining text down by roughly 2.5 extra lines.
      if (isFirstChapterBlock && blockIdx === 0) h += lineH * 2.5
      break
    }
    case 'heading':
      // margin-top:2.25rem + padding-top:1.25rem + 7.5px text + 1px border
      h = (2.25 + 1.25) * rem + 10
      break
    case 'quote': {
      const innerW  = Math.max(20, contentWidth - 2 * 1.2 * rem)
      const handle  = prepare(
        block.text || ' ',
        `italic ${Math.round(fontSize * 1.02)}px "Crimson Text", Georgia, serif`,
      )
      const textH   = layout(handle, innerW, fontSize * 1.7).height
      const attrH   = block.attribution ? 9.5 + 0.5 * rem : 0
      h = 1.6 * rem + textH + attrH   // 0.8rem top + bottom padding + content
      break
    }
    case 'divider':
      h = rem   // 0.5rem + 0.5rem padding
      break
    case 'callout': {
      const innerW = Math.max(20, contentWidth - 2 * 1.1 * rem)
      const handle = prepare(
        block.text || ' ',
        `${Math.round(fontSize * 0.94)}px "Crimson Text", Georgia, serif`,
      )
      const textH  = layout(handle, innerW, fontSize * 1.7).height
      const labelH = block.label ? 7 + 0.4 * rem : 0
      h = 1.7 * rem + textH + labelH
      break
    }
    default:
      h = 3 * rem
  }
  return gap + h
}

// ── Audio helpers ─────────────────────────────────────────────────────────────
function formatTime(sec: number): string {
  if (!isFinite(sec) || sec < 0) return '0:00'
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

// ── Web Speech API ────────────────────────────────────────────────────────────
interface SpeechWord {
  pos:  number                    // char index in utterance text (word start)
  type: 'intro' | 'block'
  bi?:  number                    // block index (type=block only)
  wi?:  number                    // word index within block text (part=text only)
  part?: string
}

function buildSpeechContent(
  chapter: Chapter,
  lang: 'pl' | 'en',
): { text: string; words: SpeechWord[] } {
  const qOpen  = lang === 'pl' ? '„' : '“'
  const qClose = lang === 'pl' ? '“' : '”'

  type Part = { frag: string; type: 'intro' | 'block'; bi?: number; part?: string }
  const parts: Part[] = []

  const num   = chapter.number
  const intro = typeof num === 'number'
    ? (lang === 'pl' ? `Rozdział ${num}.` : `Chapter ${num}.`)
    : (num ? `${num}.` : '')
  if (intro)            parts.push({ frag: intro,                   type: 'intro' })
  if (chapter.title)    parts.push({ frag: `${chapter.title}.`,     type: 'intro' })
  if (chapter.subtitle) parts.push({ frag: `${chapter.subtitle}.`,  type: 'intro' })

  chapter.blocks.forEach((block, bi) => {
    const text = (block.text ?? '').trim()
    if (!text) return
    switch (block.type) {
      case 'paragraph':
        parts.push({ frag: text,                        type: 'block', bi, part: 'text' }); break
      case 'heading':
        parts.push({ frag: `  ${text}.`,                type: 'block', bi, part: 'text' }); break
      case 'quote':
        parts.push({ frag: `${qOpen}${text}${qClose}`,  type: 'block', bi, part: 'text' })
        if (block.attribution)
          parts.push({ frag: `— ${block.attribution}.`, type: 'block', bi, part: 'attr' })
        break
      case 'callout':
        if (block.label)
          parts.push({ frag: `${block.label}:`, type: 'block', bi, part: 'label' })
        parts.push({ frag: text, type: 'block', bi, part: 'text' }); break
      case 'verse':
        parts.push({ frag: text, type: 'block', bi, part: 'text' }); break
    }
  })

  const fullText = parts.map(p => p.frag).join('\n\n')

  const words: SpeechWord[] = []
  let offset = 0
  for (const part of parts) {
    const toks = part.frag.split(/(\s+)/)
    let localPos = 0
    let wi = 0
    for (const tok of toks) {
      if (tok && !/^\s+$/.test(tok)) {
        words.push({
          pos:  offset + localPos,
          type: part.type,
          bi:   part.bi,
          wi:   part.part === 'text' ? wi : undefined,
          part: part.part,
        })
        wi++
      }
      localPos += tok.length
    }
    offset += part.frag.length + 2   // '\n\n' separator
  }

  return { text: fullText, words }
}

// ── SVG icons ─────────────────────────────────────────────────────────────────
const Icon = {
  Moon: () => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  ),
  Sun: () => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
      <circle cx="12" cy="12" r="5" strokeLinecap="round"/>
      <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42" strokeLinecap="round"/>
    </svg>
  ),
  Left: () => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
      <path d="M15 18l-6-6 6-6" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  ),
  Right: () => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
      <path d="M9 18l6-6-6-6" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  ),
  List: () => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
      <path d="M4 6h16M4 12h16M4 18h7" strokeLinecap="round"/>
    </svg>
  ),
  Play: () => (
    <svg viewBox="0 0 24 24" fill="currentColor">
      <path d="M8 5.14v14l11-7-11-7z"/>
    </svg>
  ),
  Pause: () => (
    <svg viewBox="0 0 24 24" fill="currentColor">
      <path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/>
    </svg>
  ),
  Headphones: () => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
      <path d="M3 18v-6a9 9 0 0 1 18 0v6" strokeLinecap="round" strokeLinejoin="round"/>
      <path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/>
    </svg>
  ),
  // Word-highlight / karaoke mode indicator
  Highlight: () => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
      <line x1="4" y1="6" x2="20" y2="6" strokeLinecap="round"/>
      <rect x="4" y="10" width="16" height="4" rx="1" fill="currentColor" opacity="0.35" stroke="none"/>
      <line x1="4" y1="18" x2="15" y2="18" strokeLinecap="round"/>
    </svg>
  ),
  Mic: () => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
      <rect x="9" y="3" width="6" height="10" rx="3"/>
      <path d="M5 10a7 7 0 0 0 14 0" strokeLinecap="round"/>
      <line x1="12" y1="20" x2="12" y2="17" strokeLinecap="round"/>
      <line x1="8" y1="20" x2="16" y2="20" strokeLinecap="round"/>
    </svg>
  ),
  Download: () => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" strokeLinecap="round" strokeLinejoin="round"/>
      <polyline points="7 10 12 15 17 10" strokeLinecap="round" strokeLinejoin="round"/>
      <line x1="12" y1="15" x2="12" y2="3" strokeLinecap="round"/>
    </svg>
  ),
  Bookmark: ({ active }: { active?: boolean }) => (
    <svg viewBox="0 0 24 24" fill={active ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth="1.6">
      <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  ),
  Search: () => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
      <circle cx="11" cy="11" r="7" strokeLinecap="round"/>
      <path d="M21 21l-4.35-4.35" strokeLinecap="round"/>
    </svg>
  ),
  Close: () => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
      <path d="M18 6L6 18M6 6l12 12" strokeLinecap="round"/>
    </svg>
  ),
  SkipBack: () => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
      <polygon points="19 20 9 12 19 4 19 20" fill="currentColor" stroke="none"/>
      <line x1="5" y1="19" x2="5" y2="5" strokeLinecap="round"/>
    </svg>
  ),
  SkipForward: () => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
      <polygon points="5 4 15 12 5 20 5 4" fill="currentColor" stroke="none"/>
      <line x1="19" y1="5" x2="19" y2="19" strokeLinecap="round"/>
    </svg>
  ),
  Music: () => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
      <path d="M9 18V5l12-2v13" strokeLinecap="round" strokeLinejoin="round"/>
      <circle cx="6" cy="18" r="3"/>
      <circle cx="18" cy="16" r="3"/>
    </svg>
  ),
}

// ── Ornament ──────────────────────────────────────────────────────────────────
function StarOrnament({ size = 52 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 52 52" fill="none">
      <circle cx="26" cy="26" r="24" stroke="currentColor" strokeWidth="0.5" opacity="0.35"/>
      <circle cx="26" cy="26" r="17" stroke="currentColor" strokeWidth="0.35" opacity="0.2"/>
      <circle cx="26" cy="26" r="3"  fill="currentColor"  opacity="0.65"/>
      {[0,45,90,135,180,225,270,315].map(deg => {
        const rad = (deg * Math.PI) / 180
        const x1  = 26 + 6 * Math.cos(rad),  y1 = 26 + 6 * Math.sin(rad)
        const x2  = 26 + (deg % 90 === 0 ? 23 : 18) * Math.cos(rad)
        const y2  = 26 + (deg % 90 === 0 ? 23 : 18) * Math.sin(rad)
        return <line key={deg} x1={x1} y1={y1} x2={x2} y2={y2} stroke="currentColor" strokeWidth="0.6" opacity="0.5"/>
      })}
    </svg>
  )
}

// ── Cover page ────────────────────────────────────────────────────────────────
function CoverPage({ onStart }: { onStart: () => void }) {
  const hasCover = !!book.cover

  if (hasCover) {
    return (
      <div className="sf-page cover-page" style={{
        padding: 0,
        overflow: 'hidden',
        justifyContent: 'flex-end',
        position: 'relative',
      }}>
        <img
          src={book.cover}
          alt={book.title}
          style={{
            position: 'absolute', inset: 0,
            width: '100%', height: '100%',
            objectFit: 'cover', objectPosition: 'center top',
          }}
        />
        {/* Gradient overlay */}
        <div style={{
          position: 'absolute', bottom: 0, left: 0, right: 0, height: '60%',
          background: 'linear-gradient(to bottom, transparent, rgba(0,0,0,0.82) 55%, rgba(0,0,0,0.96))',
          zIndex: 1,
        }} />
        {/* Text over image */}
        <div style={{
          position: 'relative', zIndex: 2,
          padding: '0 1.5rem 1.75rem',
          display: 'flex', flexDirection: 'column', alignItems: 'center',
          color: '#fffdf9', width: '100%', boxSizing: 'border-box',
        }}>
          <p style={{ fontFamily: 'var(--font-ui)', letterSpacing: '0.15em', fontSize: '0.6rem', color: 'var(--gold)', textTransform: 'uppercase', margin: '0 0 0.5rem' }}>
            {book.author ?? 'Federation Archives'}
          </p>
          <div style={{ width: '2.5rem', height: '1px', background: 'rgba(200,168,75,0.45)', margin: '0 0 0.75rem' }} />
          <h1 style={{ fontFamily: 'var(--font-heading)', fontSize: 'clamp(1.1rem, 4vw, 1.6rem)', fontWeight: 700, letterSpacing: '0.06em', color: '#fffdf9', margin: '0 0 0.35rem', textAlign: 'center', lineHeight: 1.2 }}>
            {book.title}
          </h1>
          {book.subtitle && (
            <p style={{ fontFamily: 'var(--font-ui)', fontSize: '0.65rem', letterSpacing: '0.12em', color: 'rgba(255,253,249,0.6)', margin: '0 0 0.75rem', textTransform: 'uppercase' }}>
              {book.subtitle}
            </p>
          )}
          <div style={{ width: '2.5rem', height: '1px', background: 'rgba(200,168,75,0.45)', margin: '0 0 0.75rem' }} />
          <p style={{ fontFamily: 'var(--font-ui)', fontSize: '0.6rem', letterSpacing: '0.1em', color: 'rgba(255,253,249,0.38)', margin: '0 0 1.25rem' }}>
            {book.year}
          </p>
          <button className="cover-begin" onClick={onStart}>Begin Reading</button>
          {book.attribution && (
            <p style={{ fontFamily: 'var(--font-ui)', fontSize: '0.52rem', letterSpacing: '0.06em', color: 'rgba(255,253,249,0.32)', margin: '0.9rem 0 0', textAlign: 'center', lineHeight: 1.6 }}>
              {book.attribution.statement}
              {book.attribution.book_url && (<>{' · '}<a href={book.attribution.book_url} style={{ color: 'inherit', textDecoration: 'underline' }} target="_blank" rel="noreferrer">{book.attribution.source}</a></>)}
              {book.attribution.license && <>{' · '}{book.attribution.license}</>}
            </p>
          )}
        </div>
      </div>
    )
  }

  // No cover image — ornament layout
  return (
    <div className="sf-page cover-page">
      <div className="cover-ornament" style={{ color: 'var(--gold)' }}>
        <StarOrnament size={60} />
      </div>
      <p className="cover-series">{book.author ?? 'Federation Archives'}</p>
      <div className="cover-rule" />
      <h1 className="cover-title">{book.title}</h1>
      <p className="cover-subtitle">{book.subtitle}</p>
      <div className="cover-rule" />
      {book.description && <p className="cover-desc">{book.description}</p>}
      <p className="cover-year">{book.year}</p>
      <button className="cover-begin" onClick={onStart}>Begin Reading</button>
      {book.attribution && (
        <p style={{ fontFamily: 'var(--font-ui)', fontSize: '0.52rem', letterSpacing: '0.06em', color: 'var(--text-dim)', margin: '0.9rem 0 0', textAlign: 'center', lineHeight: 1.6 }}>
          {book.attribution.statement}
          {book.attribution.book_url && (<>{' · '}<a href={book.attribution.book_url} style={{ color: 'inherit', textDecoration: 'underline' }} target="_blank" rel="noreferrer">{book.attribution.source}</a></>)}
          {book.attribution.license && <>{' · '}{book.attribution.license}</>}
        </p>
      )}
    </div>
  )
}

// ── Content page ──────────────────────────────────────────────────────────────
interface ContentPageProps {
  spec: PageSpec
  highlightMode?: boolean
  bookmarksForChapter?: Bookmark[]
  onBookmarkClick?: (id: string, rect: DOMRect) => void
}

function ContentPageView({ spec, highlightMode, bookmarksForChapter, onBookmarkClick }: ContentPageProps) {
  const chapter     = book.chapters[spec.chapterIdx]
  const isFirstSub  = spec.subPage === 0
  const visibleBlocks = chapter.blocks.slice(spec.blockStart, spec.blockEnd)

  // Convert to the simpler type BlockRenderer expects
  const highlights: BookmarkHighlight[] = (bookmarksForChapter ?? []).map(bm => ({
    id: bm.id, blockIdx: bm.blockIdx, selectedText: bm.selectedText,
  }))

  return (
    <div className="sf-page content-page">
      {/* Top trim */}
      <div className="page-trim">
        <span className="page-trim-text">{book.title}</span>
      </div>

      {/* Chapter header — only on first sub-page */}
      {isFirstSub && (
        <>
          <header className="page-chapter-header">
            <span className="page-chapter-num">
              {typeof chapter.number === 'string'
                ? chapter.number
                : `Chapter ${chapter.number}`}
            </span>
            <h1 className="page-chapter-title">{chapter.title}</h1>
            {chapter.subtitle && (
              <p className="page-chapter-subtitle">{chapter.subtitle}</p>
            )}
          </header>
          <div className="page-chapter-rule" />
        </>
      )}

      {/* Content area */}
      <div
        className="page-content-clip"
        style={spec.scrollable ? { overflowY: 'auto' } : undefined}
      >
        <div className="page-content-inner">
          {visibleBlocks.map((block, i) => {
            const origIdx = spec.blockStart + i
            return (
              <BlockRenderer
                key={origIdx}
                block={block}
                prevType={origIdx > 0 ? chapter.blocks[origIdx - 1].type : undefined}
                isFirst={origIdx === 0 && isFirstSub}
                highlightMode={highlightMode}
                blockIdx={origIdx}
                bookmarkHighlights={highlights}
                onBookmarkClick={onBookmarkClick}
              />
            )
          })}
        </div>
      </div>

      {/* Footer */}
      <div className="page-footer">
        <span className="page-footer-title">{chapter.title}</span>
        <div className="sub-dots">
          {spec.totalSubs > 1 && spec.totalSubs <= 20 && Array.from({ length: spec.totalSubs }).map((_, i) => (
            <span key={i} className={`sub-dot ${i === spec.subPage ? 'active' : ''}`} />
          ))}
        </div>
        <span className="page-footer-num">
          {typeof chapter.number === 'string' ? chapter.number : `Ch. ${chapter.number}`}
          {spec.totalSubs > 1 ? ` · ${spec.subPage + 1}/${spec.totalSubs}` : ''}
        </span>
      </div>
    </div>
  )
}

// ── TOC overlay ───────────────────────────────────────────────────────────────
interface TocProps {
  pages: PageSpec[]
  currentIdx: number
  onSelect: (idx: number) => void
  onClose: () => void
}

function TocOverlay({ pages, currentIdx, onSelect, onClose }: TocProps) {
  const chapters = pages.filter(p => p.kind === 'content' && p.subPage === 0)

  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 100,
      background: 'var(--page-bg)',
      display: 'flex', flexDirection: 'column',
    }}>
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '1rem 1.5rem', borderBottom: '1px solid var(--border)',
      }}>
        <span style={{ fontFamily: 'var(--font-head)', fontSize: '8px', letterSpacing: '0.35em', textTransform: 'uppercase', color: 'var(--text-dim)' }}>
          Table of Contents
        </span>
        <button className="sf-icon-btn" onClick={onClose} style={{ width: 'auto', padding: '4px 12px', fontSize: '10px', fontFamily: 'var(--font-head)', letterSpacing: '0.2em', textTransform: 'uppercase' }}>
          Close
        </button>
      </div>
      <div style={{ flex: 1, overflowY: 'auto', padding: '1.5rem' }}>
        {/* Cover entry */}
        <button
          onClick={() => { onSelect(0); onClose() }}
          style={{ display: 'flex', width: '100%', textAlign: 'left', background: 'none', border: 'none', cursor: 'pointer', padding: '0.9rem 0', borderBottom: '1px solid var(--border)', gap: '1.25rem', alignItems: 'center' }}
        >
          <span style={{ fontFamily: 'var(--font-head)', fontSize: '7.5px', letterSpacing: '0.25em', color: 'var(--gold)', width: '4.5rem', flexShrink: 0 }}>Cover</span>
          <span style={{ fontFamily: 'var(--font-body)', fontSize: '1rem', fontStyle: 'italic', color: currentIdx === 0 ? 'var(--gold-hi)' : 'var(--text)' }}>{book.title}</span>
        </button>

        {chapters.map(spec => {
          const ch = book.chapters[spec.chapterIdx]
          const pageIdxForChapter = pages.findIndex(
            p => p.kind === 'content' && p.chapterIdx === spec.chapterIdx && p.subPage === 0
          )
          const isActive = pages[currentIdx]?.kind === 'content' && pages[currentIdx]?.chapterIdx === spec.chapterIdx
          return (
            <button
              key={spec.chapterIdx}
              onClick={() => { onSelect(pageIdxForChapter); onClose() }}
              style={{ display: 'flex', width: '100%', textAlign: 'left', background: 'none', border: 'none', cursor: 'pointer', padding: '0.9rem 0', borderBottom: '1px solid var(--border)', gap: '1.25rem', alignItems: 'flex-start' }}
            >
              <span style={{ fontFamily: 'var(--font-head)', fontSize: '7.5px', letterSpacing: '0.25em', color: 'var(--gold)', width: '4.5rem', flexShrink: 0, paddingTop: '0.15rem' }}>
                {typeof ch.number === 'string' ? ch.number : `Ch. ${ch.number}`}
              </span>
              <div>
                <div style={{ fontFamily: 'var(--font-body)', fontSize: '1.05rem', fontStyle: 'italic', color: isActive ? 'var(--gold-hi)' : 'var(--text)', lineHeight: 1.3 }}>
                  {ch.title}
                </div>
                {ch.subtitle && (
                  <div style={{ fontFamily: 'var(--font-ui)', fontSize: '10px', letterSpacing: '0.12em', color: 'var(--text-dim)', marginTop: '0.2rem', textTransform: 'uppercase' }}>
                    {ch.subtitle}
                  </div>
                )}
              </div>
            </button>
          )
        })}
      </div>
    </div>
  )
}

// ── Main App ──────────────────────────────────────────────────────────────────
export default function App() {
  // ── Theme ──────────────────────────────────────────────────────────────────
  const [theme, setTheme] = useState<Theme>(() =>
    (localStorage.getItem('sf-theme') as Theme) ?? 'light'
  )
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    document.body.setAttribute('data-theme', theme)
    localStorage.setItem('sf-theme', theme)
  }, [theme])

  // ── Size preset ────────────────────────────────────────────────────────────
  const [sizePreset, setSizePreset] = useState<number>(() =>
    Math.min(3, Math.max(0, parseInt(localStorage.getItem('sf-size') ?? '1', 10) || 1))
  )
  const sizePresetRef = useRef(sizePreset)
  sizePresetRef.current = sizePreset

  // ── Page list & index ──────────────────────────────────────────────────────
  const [pages, setPages]     = useState<PageSpec[]>([
    { kind: 'cover', chapterIdx: 0, subPage: 0, totalSubs: 1, blockStart: 0, blockEnd: 0 },
  ])
  const [pageIdx, setPageIdx] = useState(0)
  const [toc, setToc]         = useState(false)
  const [measured, setMeasured] = useState(false)

  // ── Audio state ────────────────────────────────────────────────────────────
  const audioRef       = useRef<HTMLAudioElement>(null)
  const [audioEnabled,  setAudioEnabled]  = useState(false)  // toggled from topbar
  const [audioLang,     setAudioLang]     = useState<'pl' | 'en'>('pl')
  const [audioCurCh,    setAudioCurCh]    = useState<number | null>(null)
  const [audioPlaying,  setAudioPlaying]  = useState(false)
  const [audioCurrent,  setAudioCurrent]  = useState(0)
  const [audioDuration, setAudioDuration] = useState(0)
  // Multi-part MP3 support — when a chapter's audio was split into several files
  const audioPartsRef   = useRef<string[]>([])   // all parts for the currently loaded chapter
  const audioPartIdxRef = useRef(0)              // which part is currently loaded

  // ── Word-highlight (karaoke) state ─────────────────────────────────────────
  const [highlightMode, setHighlightMode] = useState(false)
  const timingWordsRef = useRef<TimingWord[]>([])    // current chapter timing
  const prevHlEl       = useRef<HTMLElement | null>(null)  // last highlighted span
  const rafRef         = useRef<number | null>(null)       // rAF handle
  const audioCurChRef  = useRef<number | null>(null)       // stable ref for onEnded closure

  // ── Audio mode: 'mp3' (file) | 'speech' (Web Speech API) ─────────────────
  const [audioMode,      setAudioMode]     = useState<'mp3' | 'speech'>('mp3')
  const [speechPlaying,  setSpeechPlaying] = useState(false)
  const utteranceRef     = useRef<SpeechSynthesisUtterance | null>(null)
  const speechWordMapRef = useRef<SpeechWord[]>([])

  // ── Bookmarks ──────────────────────────────────────────────────────────────
  const [bookmarks,    setBookmarks]    = useState<Bookmark[]>(bmLoad)
  const [bmOpen,       setBmOpen]       = useState(false)
  const [bmActiveId,   setBmActiveId]   = useState<string | null>(null)
  const [bmComment,    setBmComment]    = useState('')
  const [bmPopoverPos, setBmPopoverPos] = useState<{ x: number; y: number } | null>(null)
  // Text selection info for floating bookmark toolbar
  const [selInfo, setSelInfo] = useState<{
    blockIdx:     number
    selectedText: string
    toolbarX:     number
    toolbarY:     number
  } | null>(null)

  // ── Search ─────────────────────────────────────────────────────────────────
  const [searchOpen,  setSearchOpen]  = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const searchInputRef = useRef<HTMLInputElement>(null)

  // ── MP3 position save/restore refs ─────────────────────────────────────────
  const pendingSeekRef = useRef<number | null>(null)
  const posTimerRef    = useRef<ReturnType<typeof setTimeout> | null>(null)

  // ── Export panel ──────────────────────────────────────────────────────────
  const [exportOpen, setExportOpen] = useState(false)

  // ── Animation state ────────────────────────────────────────────────────────
  const [animDir, setAnimDir]         = useState<'next' | 'prev' | null>(null)
  const [exitPageIdx, setExitPageIdx] = useState<number | null>(null)
  const animTimeout = useRef<ReturnType<typeof setTimeout> | null>(null)

  // ── Measurement refs ───────────────────────────────────────────────────────
  // gaugeRef:     content area height on sub-page 0 (has chapter header)
  // gaugeRestRef: content area height on sub-pages 1+ (no header)
  // measureRefs:  off-screen content containers to read content width
  const gaugeRef     = useRef<HTMLDivElement>(null)
  const gaugeRestRef = useRef<HTMLDivElement>(null)
  const measureRefs  = useRef<(HTMLDivElement | null)[]>([])
  const restoredRef  = useRef(false)
  const resizeTimer  = useRef<ReturnType<typeof setTimeout> | null>(null)

  // ── Core measurement ───────────────────────────────────────────────────────
  const runMeasurement = useCallback(() => {
    const h0   = gaugeRef.current?.clientHeight ?? 0
    const hN   = gaugeRestRef.current?.clientHeight ?? 0
    const cw   = measureRefs.current[0]?.clientWidth ?? 0
    if (h0 < 10 || hN < 10 || cw < 10) return

    const preset   = PRESETS[sizePresetRef.current]
    const fontSize = preset.fontSize

    const list: PageSpec[] = [
      { kind: 'cover', chapterIdx: 0, subPage: 0, totalSubs: 1, blockStart: 0, blockEnd: 0 },
    ]

    book.chapters.forEach((chapter, ci) => {
      const blocks = chapter.blocks

      // S preset: entire chapter on one scrollable page (no pagination)
      if (!preset.paginate) {
        list.push({
          kind: 'content', chapterIdx: ci,
          subPage: 0, totalSubs: 1,
          blockStart: 0, blockEnd: blocks.length,
          scrollable: true,
        })
        return
      }

      // Paginated: accumulate block heights and split at block boundaries
      const subPages: Array<{ blockStart: number; blockEnd: number }> = []
      let blockStart = 0
      let acc        = 0
      let capacity   = h0  // first page uses h0 (smaller, has header)

      for (let i = 0; i < blocks.length; i++) {
        const bh = measureBlockH(blocks[i], i, i === 0, fontSize, cw)

        if (acc + bh > capacity && i > blockStart) {
          // This block overflows — cut before it
          subPages.push({ blockStart, blockEnd: i })
          blockStart = i
          acc        = 0
          capacity   = hN  // subsequent pages are taller (no header)
        }
        acc += bh
      }
      // Final (or only) sub-page
      subPages.push({ blockStart, blockEnd: blocks.length })

      const totalSubs = subPages.length
      subPages.forEach(({ blockStart, blockEnd }, sp) => {
        list.push({
          kind: 'content', chapterIdx: ci,
          subPage: sp, totalSubs,
          blockStart, blockEnd,
        })
      })
    })

    setPages(list)
    setPageIdx(prev => {
      if (!restoredRef.current) {
        restoredRef.current = true
        const saved = parseInt(localStorage.getItem('sf-page') ?? '0', 10) || 0
        return Math.min(Math.max(0, saved), list.length - 1)
      }
      return Math.min(prev, list.length - 1)
    })
    setMeasured(true)
  }, []) // stable — reads sizePresetRef

  // ── CSS var + re-measure when preset changes ───────────────────────────────
  useEffect(() => {
    const { fontSize } = PRESETS[sizePreset]
    document.documentElement.style.setProperty('--book-font-size', `${fontSize}px`)
    localStorage.setItem('sf-size', String(sizePreset))
    restoredRef.current = false  // allow restore on next measurement
    // Small delay so the browser applies the new font-size before we read heights
    const t = setTimeout(runMeasurement, 60)
    return () => clearTimeout(t)
  }, [sizePreset, runMeasurement])

  // ── Initial measurement + ResizeObserver ──────────────────────────────────
  useEffect(() => {
    document.fonts.ready.then(runMeasurement)
    const t = setTimeout(runMeasurement, 300)

    const observer = new ResizeObserver(() => {
      if (resizeTimer.current) clearTimeout(resizeTimer.current)
      resizeTimer.current = setTimeout(runMeasurement, 150)
    })
    if (gaugeRef.current) observer.observe(gaugeRef.current)

    return () => {
      clearTimeout(t)
      if (resizeTimer.current) clearTimeout(resizeTimer.current)
      observer.disconnect()
    }
  }, [runMeasurement])

  // ── Save page position ─────────────────────────────────────────────────────
  useEffect(() => {
    localStorage.setItem('sf-page', String(pageIdx))
  }, [pageIdx])

  // ── Save bookmarks whenever they change ────────────────────────────────────
  useEffect(() => {
    localStorage.setItem(BM_KEY, JSON.stringify(bookmarks))
  }, [bookmarks])

  // ── Navigate ───────────────────────────────────────────────────────────────
  const navigate = useCallback((dir: 'next' | 'prev') => {
    if (animDir !== null) return
    const target = dir === 'next' ? pageIdx + 1 : pageIdx - 1
    if (target < 0 || target >= pages.length) return

    setAnimDir(dir)
    setExitPageIdx(pageIdx)
    setPageIdx(target)

    if (animTimeout.current) clearTimeout(animTimeout.current)
    animTimeout.current = setTimeout(() => {
      setAnimDir(null)
      setExitPageIdx(null)
    }, 400)
  }, [animDir, pageIdx, pages.length])

  // Keyboard
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (toc)        { if (e.key === 'Escape') setToc(false);                              return }
      if (bmOpen)     { if (e.key === 'Escape') setBmOpen(false);                           return }
      if (searchOpen) { if (e.key === 'Escape') { setSearchOpen(false); setSearchQuery('') }; return }
      if (e.key === 'Escape') {
        if (bmActiveId) { setBmActiveId(null); return }
        if (selInfo)    { setSelInfo(null); return }
      }
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown') navigate('next')
      if (e.key === 'ArrowLeft'  || e.key === 'ArrowUp')   navigate('prev')
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [navigate, toc, bmOpen, searchOpen, bmActiveId, selInfo])

  // Touch / swipe
  const touchX = useRef<number | null>(null)
  const onTouchStart = (e: React.TouchEvent) => { touchX.current = e.touches[0].clientX }
  const onTouchEnd   = (e: React.TouchEvent) => {
    if (touchX.current === null) return
    const dx = e.changedTouches[0].clientX - touchX.current
    if (Math.abs(dx) > 40) navigate(dx < 0 ? 'next' : 'prev')
    touchX.current = null
  }

  // ── Audio helpers — handle audio_pl / audio_en as string | string[] ────────
  function _audioParts(raw: string | string[] | undefined): string[] {
    if (!raw) return []
    return Array.isArray(raw) ? raw : [raw]
  }
  function _audioFirstPart(raw: string | string[] | undefined): string | undefined {
    const parts = _audioParts(raw)
    return parts[0]
  }

  // ── Audio controls ─────────────────────────────────────────────────────────
  const toggleAudio = useCallback((chapterIdx: number, lang: 'pl' | 'en') => {
    const el  = audioRef.current
    const ch  = book.chapters[chapterIdx]
    const rawSrc = lang === 'pl'
      ? (ch?.audio_pl || ch?.audio_en)
      : (ch?.audio_en || ch?.audio_pl)
    if (!el || !rawSrc) return

    if (audioCurCh === chapterIdx && audioPlaying) {
      el.pause()
      return
    }
    if (audioCurCh !== chapterIdx) {
      const parts = _audioParts(rawSrc)
      audioPartsRef.current   = parts
      audioPartIdxRef.current = 0
      el.src = parts[0]
      el.load()
      // Restore saved playback position for this chapter
      try {
        const saved = JSON.parse(localStorage.getItem(POS_KEY) || 'null')
        if (saved?.ch === chapterIdx && saved.t > 5) pendingSeekRef.current = saved.t
      } catch {}
      setAudioCurCh(chapterIdx)
      setAudioCurrent(0)
      setAudioDuration(0)
    }
    el.play().catch(() => {})
  }, [audioCurCh, audioPlaying])

  // Switch language while same chapter is loaded
  const switchLang = useCallback((lang: 'pl' | 'en') => {
    setAudioLang(lang)
    if (audioCurCh === null) return
    const el  = audioRef.current
    const ch  = book.chapters[audioCurCh]
    const rawSrc = lang === 'pl'
      ? (ch?.audio_pl || ch?.audio_en)
      : (ch?.audio_en || ch?.audio_pl)
    if (!el || !rawSrc) return
    const parts = _audioParts(rawSrc)
    audioPartsRef.current   = parts
    audioPartIdxRef.current = 0
    const t = el.currentTime
    el.src = parts[0]
    el.load()
    el.currentTime = t
    if (audioPlaying) el.play()
  }, [audioCurCh, audioPlaying])

  const seekAudio = useCallback((pct: number) => {
    const el = audioRef.current
    if (!el || !audioDuration) return
    el.currentTime = pct * audioDuration
  }, [audioDuration])

  // ── Web Speech API — build and start utterance ─────────────────────────────
  const startSpeech = useCallback((chapterIdx: number, lang: 'pl' | 'en') => {
    if (typeof window === 'undefined' || !window.speechSynthesis) return
    window.speechSynthesis.cancel()

    const chapter = book.chapters[chapterIdx]
    const { text, words } = buildSpeechContent(chapter, lang)
    speechWordMapRef.current = words

    const utterance = new SpeechSynthesisUtterance(text)
    utterance.lang  = lang === 'pl' ? 'pl-PL' : 'en-US'

    const assignVoice = () => {
      const voices = window.speechSynthesis.getVoices()
      const voice  = voices.find(v => v.lang.startsWith(lang === 'pl' ? 'pl' : 'en'))
      if (voice) utterance.voice = voice
    }
    assignVoice()
    if (!utterance.voice) {
      window.speechSynthesis.onvoiceschanged = () => {
        assignVoice()
        window.speechSynthesis.onvoiceschanged = null
      }
    }

    utterance.onstart  = () => setSpeechPlaying(true)
    utterance.onpause  = () => setSpeechPlaying(false)
    utterance.onresume = () => setSpeechPlaying(true)
    utterance.onend    = () => {
      setSpeechPlaying(false)
      if (prevHlEl.current) { prevHlEl.current.classList.remove('word-hi'); prevHlEl.current = null }
    }
    utterance.onerror = () => setSpeechPlaying(false)

    utterance.onboundary = (event) => {
      if (event.name !== 'word') return
      const ci  = event.charIndex
      const wds = speechWordMapRef.current

      // Binary search: last word whose pos <= ci
      let lo = 0, hi = wds.length - 1, idx = -1
      while (lo <= hi) {
        const mid = (lo + hi) >> 1
        if (wds[mid].pos <= ci) { idx = mid; lo = mid + 1 }
        else hi = mid - 1
      }
      const sw = idx >= 0 ? wds[idx] : null

      if (prevHlEl.current) prevHlEl.current.classList.remove('word-hi')
      if (sw?.type === 'block' && sw.part === 'text' && sw.bi !== undefined && sw.wi !== undefined) {
        const el = document.querySelector<HTMLElement>(`[data-bi="${sw.bi}"][data-wi="${sw.wi}"]`)
        if (el) { el.classList.add('word-hi'); prevHlEl.current = el }
        else prevHlEl.current = null
      } else {
        prevHlEl.current = null
      }
    }

    utteranceRef.current = utterance
    setAudioCurCh(chapterIdx)
    window.speechSynthesis.speak(utterance)
  }, [])

  // Cancel speech on unmount
  useEffect(() => {
    return () => { window.speechSynthesis?.cancel() }
  }, [])

  // Keep stable refs in sync
  useEffect(() => { audioCurChRef.current = audioCurCh }, [audioCurCh])

  // ── Sync timing data ref when chapter / language changes ──────────────────
  useEffect(() => {
    if (audioCurCh === null) {
      timingWordsRef.current = []
      return
    }
    const ch = book.chapters[audioCurCh]
    timingWordsRef.current = (audioLang === 'pl' ? ch?.timing_pl : ch?.timing_en) ?? []
  }, [audioCurCh, audioLang])


  // ── RAF loop — direct DOM highlighting (no React re-render per frame) ──────
  useEffect(() => {
    const clearHL = () => {
      if (prevHlEl.current) { prevHlEl.current.classList.remove('word-hi'); prevHlEl.current = null }
    }

    if (!highlightMode || !audioPlaying) {
      clearHL()
      if (rafRef.current) { cancelAnimationFrame(rafRef.current); rafRef.current = null }
      return
    }

    const tick = () => {
      const t     = audioRef.current?.currentTime ?? 0
      const words = timingWordsRef.current

      // Binary search: last word with s <= t
      let lo = 0, hi = words.length - 1, idx = -1
      while (lo <= hi) {
        const mid = (lo + hi) >> 1
        if (words[mid].s <= t) { idx = mid; lo = mid + 1 }
        else hi = mid - 1
      }

      const tw = idx >= 0 ? words[idx] : null
      clearHL()

      if (tw?.type === 'block' && tw.part === 'text' && tw.bi !== undefined && tw.wi !== undefined) {
        const el = document.querySelector<HTMLElement>(`[data-bi="${tw.bi}"][data-wi="${tw.wi}"]`)
        if (el) { el.classList.add('word-hi'); prevHlEl.current = el }
      }

      rafRef.current = requestAnimationFrame(tick)
    }

    rafRef.current = requestAnimationFrame(tick)
    return () => {
      if (rafRef.current) { cancelAnimationFrame(rafRef.current); rafRef.current = null }
      clearHL()
    }
  }, [highlightMode, audioPlaying])

  // ── Clear DOM highlight on page navigation ────────────────────────────────
  useEffect(() => {
    if (prevHlEl.current) { prevHlEl.current.classList.remove('word-hi'); prevHlEl.current = null }
  }, [pageIdx])

  // ── Render ─────────────────────────────────────────────────────────────────
  const curSpec  = pages[pageIdx]
  const exitSpec = exitPageIdx !== null ? pages[exitPageIdx] : null
  const curChapter = curSpec?.kind === 'content' ? book.chapters[curSpec.chapterIdx] : null
  const progress = pages.length > 1 ? (pageIdx / (pages.length - 1)) * 100 : 0
  const bookHasAudio = book.chapters.some(ch => ch.audio_pl || ch.audio_en)

  // Bookmarks for current chapter (for inline highlight rendering)
  const chapterBookmarks = curSpec?.kind === 'content'
    ? bookmarks.filter(bm => bm.chapterIdx === curSpec.chapterIdx)
    : []

  // Search results
  const searchResults = (() => {
    const q = searchQuery.trim().toLowerCase()
    if (q.length < 2) return []
    const results: Array<{
      chapterIdx: number; pageIdx: number; snippet: string; chapterTitle: string
    }> = []
    for (let ci = 0; ci < book.chapters.length; ci++) {
      const ch = book.chapters[ci]
      for (let bi = 0; bi < ch.blocks.length; bi++) {
        const text = ch.blocks[bi].text ?? ''
        const lower = text.toLowerCase()
        const idx   = lower.indexOf(q)
        if (idx < 0) continue
        const pg = pages.findIndex(
          p => p.kind === 'content' && p.chapterIdx === ci && p.blockStart <= bi && bi < p.blockEnd
        )
        const from = Math.max(0, idx - 35)
        const to   = Math.min(text.length, idx + q.length + 35)
        results.push({
          chapterIdx: ci, pageIdx: pg,
          snippet: (from > 0 ? '…' : '') + text.slice(from, to) + (to < text.length ? '…' : ''),
          chapterTitle: ch.title,
        })
        if (results.length >= 60) break
      }
      if (results.length >= 60) break
    }
    return results
  })()

  const handleAudioToggle = () => {
    if (audioEnabled) {
      audioRef.current?.pause()
      window.speechSynthesis?.cancel()
      setSpeechPlaying(false)
      setHighlightMode(false)
    } else {
      // When enabling audio on a book with no MP3 files, switch to Web Speech mode
      if (!bookHasAudio && audioMode === 'mp3') setAudioMode('speech')
    }
    setAudioEnabled(e => !e)
  }

  const switchAudioMode = (mode: 'mp3' | 'speech') => {
    if (mode === audioMode) return
    if (mode === 'speech') {
      audioRef.current?.pause()
    } else {
      window.speechSynthesis?.cancel()
      setSpeechPlaying(false)
    }
    if (prevHlEl.current) { prevHlEl.current.classList.remove('word-hi'); prevHlEl.current = null }
    setAudioMode(mode)
  }

  // ── Sync bmComment when active bookmark changes ────────────────────────────
  useEffect(() => {
    if (!bmActiveId) { setBmComment(''); return }
    const bm = bookmarks.find(b => b.id === bmActiveId)
    setBmComment(bm?.comment ?? '')
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bmActiveId])

  // ── Clear inline popover and selection toolbar on page navigation ──────────
  useEffect(() => {
    setBmActiveId(null)
    setSelInfo(null)
  }, [pageIdx])

  // ── Bookmark helpers ───────────────────────────────────────────────────────
  const addBookmark = useCallback(() => {
    if (!selInfo || !curSpec || curSpec.kind !== 'content') return
    const ch = book.chapters[curSpec.chapterIdx]
    const bm: Bookmark = {
      id:           `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
      pageIdx,
      chapterIdx:   curSpec.chapterIdx,
      subPage:      curSpec.subPage,
      blockIdx:     selInfo.blockIdx,
      selectedText: selInfo.selectedText,
      chapterTitle: ch.title,
      chapterNum:   ch.number,
      comment:      '',
      createdAt:    new Date().toISOString(),
    }
    setBookmarks(prev => [...prev, bm])
    setSelInfo(null)
    window.getSelection()?.removeAllRanges()
  }, [selInfo, curSpec, pageIdx])

  const removeBookmark = useCallback((id: string) => {
    setBookmarks(prev => prev.filter(b => b.id !== id))
    if (bmActiveId === id) setBmActiveId(null)
  }, [bmActiveId])

  const updateComment = useCallback((id: string, text: string) => {
    setBookmarks(prev => prev.map(b => b.id === id ? { ...b, comment: text } : b))
  }, [])

  // Called when user clicks ⊙ inline marker in the text
  const handleBookmarkClick = useCallback((id: string, rect: DOMRect) => {
    setBmActiveId(prev => {
      if (prev === id) { setBmPopoverPos(null); return null }
      setBmPopoverPos({ x: rect.right + 4, y: rect.top - 10 })
      return id
    })
  }, [])

  // Detect text selection in content area
  const handleMouseUp = useCallback((e: React.MouseEvent) => {
    if ((e.target as HTMLElement).closest('.bm-marker-btn,.bm-popover,.sf-sel-toolbar')) return
    const sel = window.getSelection()
    if (!sel || sel.isCollapsed) { setSelInfo(null); return }
    const text = sel.toString().trim()
    if (!text || text.length < 2) { setSelInfo(null); return }
    const range = sel.getRangeAt(0)
    const startNode = range.startContainer
    const el = startNode.nodeType === Node.TEXT_NODE
      ? startNode.parentElement
      : startNode as HTMLElement
    const blockEl = el?.closest?.('[data-block-idx]') as HTMLElement | null
    if (!blockEl) { setSelInfo(null); return }
    const bi = parseInt((blockEl as HTMLElement).dataset.blockIdx || '0', 10)
    const selRect = range.getBoundingClientRect()
    setSelInfo({
      blockIdx:     bi,
      selectedText: text,
      toolbarX:     selRect.left + selRect.width / 2,
      toolbarY:     selRect.top,
    })
  }, [])

  // Audio ±5s skip
  const skipAudio = useCallback((secs: number) => {
    const el = audioRef.current
    if (!el) return
    el.currentTime = Math.max(0, Math.min(el.currentTime + secs, audioDuration))
  }, [audioDuration])

  const toggleSpeech = (chapterIdx: number, lang: 'pl' | 'en') => {
    if (!window.speechSynthesis) return
    if (speechPlaying) {
      window.speechSynthesis.pause()
    } else if (audioCurCh === chapterIdx && utteranceRef.current) {
      window.speechSynthesis.resume()
    } else {
      startSpeech(chapterIdx, lang)
    }
  }

  // Word spans: rendered when highlight mode on OR speech mode active (onboundary needs them)
  const wordMode = highlightMode || (audioEnabled && audioMode === 'speech')

  return (
    <div
      className="sf-root"
      data-theme={theme}
      onTouchStart={onTouchStart}
      onTouchEnd={onTouchEnd}
      onMouseUp={handleMouseUp}
      onClick={e => {
        // Close inline bookmark popover when clicking outside it
        const t = e.target as HTMLElement
        if (!t.closest('.bm-popover') && !t.closest('.bm-marker-btn')) {
          setBmActiveId(null)
          setBmPopoverPos(null)
        }
      }}
    >
      {/* ── Hidden measurement area ──────────────────────────────────────── */}
      <div className="sf-measurements">
        {/* Gauge 0: available content height on sub-page 0 (with chapter header) */}
        <div style={{ width: 'var(--page-w)', height: 'var(--page-h)', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
          <div className="page-trim"><span className="page-trim-text">x</span></div>
          <header className="page-chapter-header">
            <span className="page-chapter-num">Chapter 1</span>
            <h1 className="page-chapter-title">Sample Title</h1>
          </header>
          <div className="page-chapter-rule" />
          <div style={{ flex: 1, paddingTop: '0.75rem', overflow: 'hidden' }}>
            <div style={{ height: '100%' }} ref={gaugeRef} />
          </div>
          <div className="page-footer"><span className="page-footer-num">x</span></div>
        </div>

        {/* Gauge N: available content height on sub-pages 1+ (no header) */}
        <div style={{ width: 'var(--page-w)', height: 'var(--page-h)', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
          <div className="page-trim"><span className="page-trim-text">x</span></div>
          <div style={{ flex: 1, paddingTop: '0.75rem', overflow: 'hidden' }}>
            <div style={{ height: '100%' }} ref={gaugeRestRef} />
          </div>
          <div className="page-footer"><span className="page-footer-num">x</span></div>
        </div>

        {/* Per-chapter content width reference */}
        {book.chapters.map((chapter, ci) => (
          <div
            key={chapter.id}
            className="sf-measure-inner"
            ref={el => { measureRefs.current[ci] = el }}
          />
        ))}
      </div>

      {/* ── Top bar ──────────────────────────────────────────────────────── */}
      <div className="sf-topbar">
        <span className="sf-topbar-title">{book.title}</span>
        <span className="sf-topbar-chapter">
          {curChapter
            ? `${typeof curChapter.number === 'string' ? curChapter.number : `Ch. ${curChapter.number}`} · ${curChapter.title}`
            : ''}
        </span>
        <div className="sf-topbar-actions">
          {/* Size preset buttons */}
          <div className="sf-size-btns">
            {PRESETS.map((p, i) => (
              <button
                key={p.label}
                className={`sf-size-btn ${i === sizePreset ? 'active' : ''}`}
                onClick={() => setSizePreset(i)}
                title={`Font size: ${p.fontSize}px${!p.paginate ? ' (scroll)' : ''}`}
              >
                {p.label}
              </button>
            ))}
          </div>
          <button
            className={`sf-icon-btn${audioEnabled ? ' sf-audio-active' : ''}`}
            onClick={handleAudioToggle}
            title={audioEnabled ? 'Wyłącz audio' : 'Włącz audio'}
          >
            <Icon.Headphones />
          </button>
          <div style={{ position: 'relative' }}>
            <button
              className={`sf-icon-btn${exportOpen ? ' sf-audio-active' : ''}`}
              onClick={() => setExportOpen(o => !o)}
              title="Pobierz (EPUB / PDF)"
            >
              <Icon.Download />
            </button>
          </div>
          <button className="sf-icon-btn" onClick={() => setToc(true)} title="Table of contents">
            <Icon.List />
          </button>
          <button
            className={`sf-icon-btn${bookmarks.length > 0 ? ' sf-audio-active' : ''}`}
            onClick={() => setBmOpen(o => !o)}
            title={`Bookmarks (${bookmarks.length})`}
          >
            <Icon.Bookmark active={bookmarks.length > 0} />
          </button>
          <button
            className={`sf-icon-btn${searchOpen ? ' sf-audio-active' : ''}`}
            onClick={() => { setSearchOpen(o => !o); setTimeout(() => searchInputRef.current?.focus(), 50) }}
            title="Search"
          >
            <Icon.Search />
          </button>
          <button
            className="sf-icon-btn"
            onClick={() => setTheme(t => t === 'light' ? 'dark' : 'light')}
            title="Toggle theme"
          >
            {theme === 'light' ? <Icon.Moon /> : <Icon.Sun />}
          </button>
        </div>
      </div>

      {/* ── Export panel ─────────────────────────────────────────────────── */}
      {exportOpen && (() => {
        const attr    = book.attribution
        const epubUrl = attr?.epub_url
        const pdfUrl  = attr?.pdf_url
        const sourceUrl = attr?.book_url || attr?.source_url
        return (
          <>
            <div className="sf-export-overlay" onClick={() => setExportOpen(false)} />
            <div className="sf-export-panel">
              <div className="sf-export-title">Pobierz książkę</div>
              {epubUrl && (
                <a
                  className="sf-export-link"
                  href={epubUrl}
                  target="_blank"
                  rel="noreferrer"
                  onClick={() => setExportOpen(false)}
                >
                  <span className="sf-export-fmt">EPUB</span>
                  <span className="sf-export-desc">E-reader · Kindle, Kobo, iBooks</span>
                </a>
              )}
              {pdfUrl && (
                <a
                  className="sf-export-link"
                  href={pdfUrl}
                  target="_blank"
                  rel="noreferrer"
                  onClick={() => setExportOpen(false)}
                >
                  <span className="sf-export-fmt">PDF</span>
                  <span className="sf-export-desc">Dokument · do druku lub archiwum</span>
                </a>
              )}
              {sourceUrl && (
                <a
                  className="sf-export-link"
                  href={sourceUrl}
                  target="_blank"
                  rel="noreferrer"
                  onClick={() => setExportOpen(false)}
                >
                  <span className="sf-export-fmt">{attr?.source || 'Źródło'}</span>
                  <span className="sf-export-desc">Strona książki</span>
                </a>
              )}
            </div>
          </>
        )
      })()}

      {/* ── Viewport ─────────────────────────────────────────────────────── */}
      <div className="sf-viewport">
        <div className="sf-stage">
          {/* Exiting page (animates out) */}
          {exitSpec && animDir && (
            <div key={`exit-${exitPageIdx}`}>
              {exitSpec.kind === 'cover' ? (
                <div className={`sf-page cover-page page-exit-${animDir}`} style={{ animationDuration: '0.38s' }}>
                  <CoverPage onStart={() => {}} />
                </div>
              ) : (
                <div className={`sf-page content-page page-exit-${animDir}`} style={{ animationDuration: '0.38s' }}>
                  <ContentPageView spec={exitSpec} />
                </div>
              )}
            </div>
          )}

          {/* Current page */}
          {curSpec && (
            <div key={`page-${pageIdx}`}>
              {curSpec.kind === 'cover' ? (
                <div className={`sf-page cover-page ${animDir ? `page-enter-${animDir}` : ''}`}>
                  <CoverPage onStart={() => navigate('next')} />
                </div>
              ) : (
                <div className={`sf-page content-page ${animDir ? `page-enter-${animDir}` : ''}`}>
                  <ContentPageView
                    spec={curSpec}
                    highlightMode={wordMode}
                    bookmarksForChapter={chapterBookmarks}
                    onBookmarkClick={handleBookmarkClick}
                  />
                </div>
              )}
            </div>
          )}
        </div>

        {/* Nav arrows */}
        <button className="sf-nav prev" onClick={() => navigate('prev')} disabled={pageIdx === 0} aria-label="Previous page">
          <Icon.Left />
        </button>
        <button className="sf-nav next" onClick={() => navigate('next')} disabled={pageIdx >= pages.length - 1} aria-label="Next page">
          <Icon.Right />
        </button>
      </div>

      {/* ── Hidden audio element ─────────────────────────────────────────── */}
      <audio
        ref={audioRef}
        onTimeUpdate={() => {
          const el = audioRef.current
          if (el) {
            setAudioCurrent(el.currentTime)
            // Save position (debounced, every ~2 s)
            if (audioCurChRef.current !== null) {
              if (posTimerRef.current) clearTimeout(posTimerRef.current)
              posTimerRef.current = setTimeout(() => {
                localStorage.setItem(POS_KEY, JSON.stringify({
                  ch: audioCurChRef.current,
                  t:  el.currentTime,
                }))
              }, 2000)
            }
          }
        }}
        onLoadedMetadata={() => {
          setAudioDuration(audioRef.current?.duration ?? 0)
          // Seek to restored position after metadata is available
          if (pendingSeekRef.current !== null && audioRef.current) {
            audioRef.current.currentTime = pendingSeekRef.current
            pendingSeekRef.current = null
          }
        }}
        onPlay={() => setAudioPlaying(true)}
        onPause={() => setAudioPlaying(false)}
        onEnded={() => {
          const el = audioRef.current
          // Part advance: next split-part of same chapter
          const nextPartIdx = audioPartIdxRef.current + 1
          if (el && nextPartIdx < audioPartsRef.current.length) {
            audioPartIdxRef.current = nextPartIdx
            el.src = audioPartsRef.current[nextPartIdx]
            el.load()
            el.play().catch(() => {})
            return
          }
          // Chapter advance: next chapter with audio — NO page navigation
          audioPartIdxRef.current = 0
          const curCh = audioCurChRef.current
          if (curCh !== null && el) {
            let nextCh: number | null = null
            for (let i = curCh + 1; i < book.chapters.length; i++) {
              if (book.chapters[i].audio_pl || book.chapters[i].audio_en) { nextCh = i; break }
            }
            if (nextCh !== null) {
              const ch     = book.chapters[nextCh]
              const rawSrc = audioLang === 'pl'
                ? (ch.audio_pl || ch.audio_en)
                : (ch.audio_en || ch.audio_pl)
              const parts  = _audioParts(rawSrc)
              if (parts.length) {
                audioPartsRef.current = parts
                el.src = parts[0]
                el.load()
                el.play().catch(() => {})
                setAudioCurCh(nextCh)
                return
              }
            }
          }
          // End of playlist
          setAudioPlaying(false)
          setAudioCurrent(0)
        }}
      />

      {/* ── Audio bar (3-state: MP3 / Web Speech API) ───────────────────── */}
      {(() => {
        // Show the bar whenever audio is enabled (Web Speech works for any book)
        if (!audioEnabled) return null

        // First chapter with audio in the book
        const firstAudioChIdx: number | null = (() => {
          for (let i = 0; i < book.chapters.length; i++) {
            if (book.chapters[i].audio_pl || book.chapters[i].audio_en) return i
          }
          return null
        })()

        // Hide bar if book has no audio (MP3 mode) and speech isn't available
        if (firstAudioChIdx === null && audioMode !== 'speech') return null

        // Active chapter: what's loaded in the audio element, or first chapter (before first play)
        // — independent of which page the reader is on
        const activeChIdx   = audioCurCh ?? firstAudioChIdx
        const activeCh      = activeChIdx !== null ? book.chapters[activeChIdx] : null
        const playHasPl     = !!activeCh?.audio_pl
        const playHasEn     = !!activeCh?.audio_en

        const isLoaded      = audioCurCh !== null
        const isPlayingMp3  = audioMode === 'mp3' && audioPlaying && isLoaded
        const isPlayingSp   = audioMode === 'speech' && speechPlaying
        const isPlaying     = isPlayingMp3 || isPlayingSp
        const audioProgress = (isLoaded && audioDuration > 0)
          ? (audioCurrent / audioDuration) * 100 : 0
        const hasTiming     = !!(audioLang === 'pl' ? activeCh?.timing_pl?.length : activeCh?.timing_en?.length)

        // Speech mode: use current page's chapter
        const pageChIdx = curSpec?.kind === 'content' ? curSpec.chapterIdx : null

        // Play handler — global playlist, no navigation
        const handlePlay = () => {
          if (audioMode === 'mp3') {
            // On first play check saved position to resume from correct chapter
            let startCh = activeChIdx
            if (audioCurCh === null) {
              try {
                const saved = JSON.parse(localStorage.getItem(POS_KEY) || 'null')
                if (saved?.ch != null) startCh = saved.ch as number
              } catch {}
            }
            if (startCh !== null) toggleAudio(startCh, audioLang)
          } else {
            toggleSpeech(pageChIdx ?? activeChIdx ?? 0, audioLang)
          }
        }

        return (
          <div className="sf-audiobar">
            {/* Language selector */}
            {(audioMode === 'speech' || (playHasPl && playHasEn)) && (
              <div className="sf-audio-langs">
                {(['pl', 'en'] as const).map(l => (
                  <button
                    key={l}
                    className={`sf-audio-lang-btn${audioLang === l ? ' active' : ''}`}
                    onClick={() => audioMode === 'mp3' ? switchLang(l) : setAudioLang(l)}
                  >
                    {l.toUpperCase()}
                  </button>
                ))}
              </div>
            )}

            {/* Mode toggle: MP3 | 🎤 Web Speech */}
            <div className="sf-audio-mode">
              {(playHasPl || playHasEn) && (
                <button
                  className={`sf-audio-mode-btn${audioMode === 'mp3' ? ' active' : ''}`}
                  onClick={() => switchAudioMode('mp3')}
                  title="Tryb MP3 (plik audio)"
                >MP3</button>
              )}
              <button
                className={`sf-audio-mode-btn${audioMode === 'speech' ? ' active' : ''}`}
                onClick={() => switchAudioMode('speech')}
                title="Tryb głosu (Web Speech API)"
              >
                <Icon.Mic />
              </button>
            </div>

            {/* Skip -5s / +5s — MP3 only, LEFT of play */}
            {audioMode === 'mp3' && (
              <button
                className="sf-audio-skip-btn"
                onClick={() => skipAudio(-5)}
                title="Back 5 seconds"
              ><Icon.SkipBack /></button>
            )}
            {audioMode === 'mp3' && (
              <button
                className="sf-audio-skip-btn"
                onClick={() => skipAudio(5)}
                title="Forward 5 seconds"
              ><Icon.SkipForward /></button>
            )}

            {/* Play / pause */}
            <button
              className="sf-audio-play-btn"
              onClick={handlePlay}
              title={isPlaying ? 'Pause' : 'Play'}
            >
              {isPlaying ? <Icon.Pause /> : <Icon.Play />}
            </button>

            {/* Word-highlight toggle */}
            {(hasTiming || audioMode === 'speech') && (
              <button
                className={`sf-audio-hl-btn${highlightMode ? ' active' : ''}`}
                onClick={() => setHighlightMode(m => !m)}
                title={highlightMode ? 'Wyłącz śledzenie tekstu' : 'Śledź tekst'}
              >
                <Icon.Highlight />
              </button>
            )}

            {/* Seek bar — MP3 only */}
            {audioMode === 'mp3' && (
              <div
                className="sf-audio-track"
                onClick={e => {
                  const rect = (e.currentTarget as HTMLDivElement).getBoundingClientRect()
                  seekAudio((e.clientX - rect.left) / rect.width)
                }}
              >
                <div className="sf-audio-fill" style={{ width: `${audioProgress}%` }} />
              </div>
            )}

            {/* Speech mode: animated wave bar */}
            {audioMode === 'speech' && (
              <div className="sf-audio-track sf-audio-track--speech">
                <div
                  className={`sf-audio-fill${speechPlaying ? ' sf-audio-fill--pulse' : ''}`}
                  style={{ width: speechPlaying ? '100%' : '0%' }}
                />
              </div>
            )}

            {/* Time — MP3 only */}
            {audioMode === 'mp3' && (
              <span className="sf-audio-time">
                {isLoaded
                  ? `${formatTime(audioCurrent)} / ${formatTime(audioDuration)}`
                  : `0:00 / 0:00`}
              </span>
            )}

            {/* Audio source — bottom-right, music icon + label, MP3 only, no emoji */}
            {audioMode === 'mp3' && (book.audio_label || book.audio_source) && (
              <span style={{
                marginLeft: 'auto',
                display: 'flex', alignItems: 'center', gap: '3px',
                fontFamily: 'var(--font-ui)', fontSize: '7px', letterSpacing: '0.1em',
                textTransform: 'uppercase', color: 'var(--text-dim)',
                whiteSpace: 'nowrap', opacity: 0.75,
              }}>
                <span style={{ width: '10px', height: '10px', display: 'inline-flex', flexShrink: 0 }}>
                  <Icon.Music />
                </span>
                {(() => {
                  if (book.audio_label) return book.audio_label
                  const src = book.audio_source!
                  const url = src === 'wolnelektury'
                    ? book.attribution?.source_url || 'https://wolnelektury.pl'
                    : null
                  const text = src === 'wolnelektury' ? 'Wolne Lektury'
                             : src === 'generated'    ? 'AI'
                             : src
                  return url
                    ? <a href={url} target="_blank" rel="noreferrer" style={{ color: 'inherit', textDecoration: 'none' }}>{text}</a>
                    : text
                })()}
              </span>
            )}
          </div>
        )
      })()}

      {/* ── Bottom bar ───────────────────────────────────────────────────── */}
      <div className="sf-bottombar">
        <span className="sf-page-label">{pageIdx} / {pages.length - 1}</span>
        <div className="sf-progress-track">
          <div className="sf-progress-fill" style={{ width: `${progress}%` }} />
        </div>
        <span className="sf-page-label">{Math.round(progress)}%</span>
      </div>

      {/* ── TOC overlay ──────────────────────────────────────────────────── */}
      {toc && (
        <TocOverlay
          pages={pages}
          currentIdx={pageIdx}
          onSelect={setPageIdx}
          onClose={() => setToc(false)}
        />
      )}

      {/* ── Bookmark list panel ──────────────────────────────────────────── */}
      {bmOpen && (
        <div style={{
          position: 'fixed', inset: 0, zIndex: 100,
          background: 'var(--page-bg)', display: 'flex', flexDirection: 'column',
        }}>
          <div style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            padding: '1rem 1.5rem', borderBottom: '1px solid var(--border)',
          }}>
            <span style={{ fontFamily: 'var(--font-head)', fontSize: '8px', letterSpacing: '0.35em', textTransform: 'uppercase', color: 'var(--text-dim)' }}>
              Bookmarks ({bookmarks.length})
            </span>
            <button className="sf-icon-btn" onClick={() => setBmOpen(false)}
              style={{ width: 'auto', padding: '4px 12px', fontSize: '10px', fontFamily: 'var(--font-head)', letterSpacing: '0.2em', textTransform: 'uppercase' }}>
              Close
            </button>
          </div>
          <div style={{ flex: 1, overflowY: 'auto', padding: '1.5rem' }}>
            {bookmarks.length === 0 && (
              <p style={{ fontFamily: 'var(--font-ui)', fontSize: '11px', color: 'var(--text-dim)', letterSpacing: '0.05em', textAlign: 'center', marginTop: '3rem' }}>
                Select text on a page, then click the bookmark button that appears to add a bookmark.
              </p>
            )}
            {bookmarks.map(bm => (
              <div key={bm.id} style={{ padding: '0.9rem 0', borderBottom: '1px solid var(--border)', display: 'flex', gap: '1rem', alignItems: 'flex-start' }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', gap: '1rem', alignItems: 'baseline', flexWrap: 'wrap' }}>
                    <span style={{ fontFamily: 'var(--font-head)', fontSize: '7.5px', letterSpacing: '0.25em', color: 'var(--gold)', flexShrink: 0 }}>
                      {typeof bm.chapterNum === 'string' ? bm.chapterNum : `Ch. ${bm.chapterNum}`}
                    </span>
                    <span style={{ fontFamily: 'var(--font-body)', fontSize: '1rem', fontStyle: 'italic', color: 'var(--text)' }}>
                      {bm.chapterTitle}
                    </span>
                  </div>
                  {bm.selectedText && (
                    <p style={{ fontFamily: 'var(--font-body)', fontSize: '0.85rem', color: 'var(--text)', margin: '0.3rem 0 0', lineHeight: 1.55,
                      borderLeft: '2px solid var(--gold)', paddingLeft: '0.6rem', fontStyle: 'italic',
                    }}>
                      "{bm.selectedText.length > 120 ? bm.selectedText.slice(0, 120) + '…' : bm.selectedText}"
                    </p>
                  )}
                  {bm.comment && (
                    <p style={{ fontFamily: 'var(--font-body)', fontSize: '0.82rem', color: 'var(--text-dim)', margin: '0.3rem 0 0', lineHeight: 1.6 }}>
                      {bm.comment}
                    </p>
                  )}
                  <span style={{ fontFamily: 'var(--font-ui)', fontSize: '9px', color: 'var(--text-dim)', letterSpacing: '0.05em', display: 'block', marginTop: '0.3rem' }}>
                    {new Date(bm.createdAt).toLocaleDateString()}
                  </span>
                </div>
                <div style={{ display: 'flex', gap: '6px', flexShrink: 0, paddingTop: '2px' }}>
                  <button onClick={() => { setPageIdx(bm.pageIdx); setBmOpen(false) }}
                    style={{ background: 'none', border: '1px solid var(--border)', borderRadius: '3px', cursor: 'pointer', padding: '3px 8px', fontFamily: 'var(--font-head)', fontSize: '7.5px', letterSpacing: '0.15em', textTransform: 'uppercase', color: 'var(--text-dim)' }}>
                    Go
                  </button>
                  <button onClick={() => removeBookmark(bm.id)}
                    style={{ background: 'none', border: '1px solid var(--border)', borderRadius: '3px', cursor: 'pointer', padding: '3px 8px', fontFamily: 'var(--font-head)', fontSize: '7.5px', letterSpacing: '0.15em', textTransform: 'uppercase', color: 'var(--text-dim)' }}>
                    ✕
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Floating text-selection toolbar ──────────────────────────────── */}
      {selInfo && curSpec?.kind === 'content' && (
        <div
          className="sf-sel-toolbar"
          style={{
            position: 'fixed',
            left: Math.max(8, Math.min(selInfo.toolbarX - 56, window.innerWidth - 120)),
            top:  Math.max(8, selInfo.toolbarY - 42),
            zIndex: 150,
            display: 'flex', alignItems: 'center', gap: '4px',
            background: 'var(--page-bg)',
            border: '1px solid var(--gold)',
            borderRadius: '4px',
            padding: '4px 8px',
            boxShadow: '0 4px 20px rgba(0,0,0,0.18)',
            pointerEvents: 'all',
          }}
          onMouseUp={e => e.stopPropagation()}
        >
          <button
            onClick={e => { e.stopPropagation(); addBookmark() }}
            style={{
              background: 'none', border: 'none', cursor: 'pointer',
              color: 'var(--gold)', fontFamily: 'var(--font-head)',
              fontSize: '8px', letterSpacing: '0.2em', textTransform: 'uppercase',
              display: 'flex', alignItems: 'center', gap: '5px', padding: '2px 4px',
            }}
          >
            <span style={{ width: '13px', height: '13px', display: 'inline-flex' }}><Icon.Bookmark /></span>
            Bookmark
          </button>
          <button
            onClick={e => { e.stopPropagation(); setSelInfo(null); window.getSelection()?.removeAllRanges() }}
            style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-dim)', fontSize: '11px', padding: '2px 2px', lineHeight: 1 }}
          >✕</button>
        </div>
      )}

      {/* ── Inline bookmark comment popover ──────────────────────────────── */}
      {bmActiveId && bmPopoverPos && (() => {
        const bm = bookmarks.find(b => b.id === bmActiveId)
        if (!bm) return null
        const posX = Math.min(bmPopoverPos.x, window.innerWidth - 216)
        const posY = Math.max(8, Math.min(bmPopoverPos.y, window.innerHeight - 220))
        return (
          <div
            className="bm-popover"
            style={{
              position: 'fixed', left: posX, top: posY,
              zIndex: 200, width: '210px',
              background: 'var(--page-bg)',
              border: '1px solid var(--border)',
              borderRadius: '4px', padding: '0.75rem',
              boxShadow: '0 4px 24px rgba(0,0,0,0.18)',
            }}
            onMouseUp={e => e.stopPropagation()}
            onClick={e => e.stopPropagation()}
          >
            <p style={{ fontFamily: 'var(--font-body)', fontSize: '0.75rem', fontStyle: 'italic',
              color: 'var(--text-dim)', margin: '0 0 0.5rem', lineHeight: 1.4,
              overflow: 'hidden', textOverflow: 'ellipsis', display: '-webkit-box',
              WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' }}>
              "{bm.selectedText}"
            </p>
            <textarea
              // eslint-disable-next-line jsx-a11y/no-autofocus
              autoFocus
              value={bmComment}
              onChange={e => { setBmComment(e.target.value); updateComment(bmActiveId, e.target.value) }}
              placeholder="Add note…"
              rows={3}
              style={{
                width: '100%', boxSizing: 'border-box',
                fontFamily: 'var(--font-body)', fontSize: '0.85rem',
                background: 'var(--page-bg)', color: 'var(--text)',
                border: '1px solid var(--border)', borderRadius: '3px',
                padding: '0.4rem 0.5rem', resize: 'none', lineHeight: 1.5,
              }}
            />
            <div style={{ display: 'flex', gap: '6px', marginTop: '6px', justifyContent: 'flex-end' }}>
              <button onClick={() => removeBookmark(bmActiveId)}
                style={{ background: 'none', border: '1px solid var(--border)', borderRadius: '3px', cursor: 'pointer', padding: '3px 8px', fontFamily: 'var(--font-head)', fontSize: '7.5px', letterSpacing: '0.15em', textTransform: 'uppercase', color: 'var(--text-dim)' }}>
                Delete
              </button>
              <button onClick={() => setBmActiveId(null)}
                style={{ background: 'none', border: '1px solid var(--border)', borderRadius: '3px', cursor: 'pointer', padding: '3px 8px', fontFamily: 'var(--font-head)', fontSize: '7.5px', letterSpacing: '0.15em', textTransform: 'uppercase', color: 'var(--text-dim)' }}>
                Close
              </button>
            </div>
          </div>
        )
      })()}

      {/* ── Search panel ─────────────────────────────────────────────────── */}
      {searchOpen && (
        <div style={{
          position: 'fixed', inset: 0, zIndex: 100,
          background: 'var(--page-bg)', display: 'flex', flexDirection: 'column',
        }}>
          <div style={{
            display: 'flex', alignItems: 'center', gap: '1rem',
            padding: '1rem 1.5rem', borderBottom: '1px solid var(--border)',
          }}>
            <span style={{ width: '18px', height: '18px', color: 'var(--text-dim)', flexShrink: 0 }}><Icon.Search /></span>
            <input
              ref={searchInputRef}
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
              placeholder="Search in book…"
              style={{
                flex: 1, fontFamily: 'var(--font-body)', fontSize: '1.05rem',
                background: 'none', border: 'none', outline: 'none',
                color: 'var(--text)',
              }}
            />
            {searchQuery && (
              <button onClick={() => setSearchQuery('')}
                style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-dim)', fontSize: '14px', padding: '0 4px' }}>
                ✕
              </button>
            )}
            <button className="sf-icon-btn" onClick={() => { setSearchOpen(false); setSearchQuery('') }}
              style={{ width: 'auto', padding: '4px 12px', fontSize: '10px', fontFamily: 'var(--font-head)', letterSpacing: '0.2em', textTransform: 'uppercase' }}>
              Close
            </button>
          </div>
          <div style={{ flex: 1, overflowY: 'auto', padding: '0.5rem 1.5rem 2rem' }}>
            {searchQuery.trim().length < 2 && (
              <p style={{ fontFamily: 'var(--font-ui)', fontSize: '11px', color: 'var(--text-dim)', letterSpacing: '0.05em', textAlign: 'center', marginTop: '3rem' }}>
                Type at least 2 characters to search
              </p>
            )}
            {searchQuery.trim().length >= 2 && searchResults.length === 0 && (
              <p style={{ fontFamily: 'var(--font-ui)', fontSize: '11px', color: 'var(--text-dim)', letterSpacing: '0.05em', textAlign: 'center', marginTop: '3rem' }}>
                No results for "{searchQuery}"
              </p>
            )}
            {searchResults.map((r, i) => {
              const q = searchQuery.trim()
              const lo = r.snippet.toLowerCase().indexOf(q.toLowerCase())
              return (
                <button
                  key={i}
                  onClick={() => { if (r.pageIdx >= 0) setPageIdx(r.pageIdx); setSearchOpen(false) }}
                  style={{
                    display: 'block', width: '100%', textAlign: 'left',
                    background: 'none', border: 'none', cursor: 'pointer',
                    padding: '0.85rem 0', borderBottom: '1px solid var(--border)',
                  }}
                >
                  <div style={{ fontFamily: 'var(--font-head)', fontSize: '7.5px', letterSpacing: '0.25em', color: 'var(--gold)', marginBottom: '0.3rem' }}>
                    {r.chapterTitle}
                  </div>
                  <div style={{ fontFamily: 'var(--font-body)', fontSize: '0.95rem', color: 'var(--text)', lineHeight: 1.55 }}>
                    {lo < 0 ? r.snippet : (
                      <>
                        {r.snippet.slice(0, lo)}
                        <mark style={{ background: 'rgba(184,146,60,0.28)', color: 'inherit', borderRadius: '2px' }}>
                          {r.snippet.slice(lo, lo + q.length)}
                        </mark>
                        {r.snippet.slice(lo + q.length)}
                      </>
                    )}
                  </div>
                </button>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
