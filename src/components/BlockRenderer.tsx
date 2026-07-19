import type { BookBlock, BookmarkHighlight } from '../types'

interface Props {
  block: BookBlock
  prevType?: string
  isFirst?: boolean           // true for very first paragraph → drop cap
  highlightMode?: boolean     // when true, text is wrapped in per-word spans
  blockIdx?: number           // original index in chapter.blocks (for data-bi / data-block-idx)
  bookmarkHighlights?: BookmarkHighlight[]
  onBookmarkClick?: (id: string, rect: DOMRect) => void
}

// ── Per-word spans for audio karaoke highlighting ─────────────────────────────
function WordSpans({ text, bi, startWi = 0 }: { text: string; bi: number; startWi?: number }) {
  const tokens = text.split(/(\s+)/)
  let wi = startWi
  return (
    <>
      {tokens.map((tok, i) => {
        if (/^\s+$/.test(tok) || tok === '') return tok
        const wordIdx = wi++
        return (
          <span key={i} data-bi={bi} data-wi={wordIdx}>
            {tok}
          </span>
        )
      })}
    </>
  )
}

// ── Text with bookmark highlights ─────────────────────────────────────────────
// Splits text around highlighted ranges, renders <mark> + ⊙ marker button.
// Maintains correct data-wi indices across parts for karaoke to work.
function TextWithHighlights({
  text, bi, hl, highlights, onBookmarkClick,
}: {
  text: string
  bi:   number
  hl:   boolean
  highlights: BookmarkHighlight[]
  onBookmarkClick?: (id: string, rect: DOMRect) => void
}) {
  if (!highlights.length) {
    return hl ? <WordSpans text={text} bi={bi} /> : <>{text}</>
  }

  // Build parts by splitting text around each highlight (first occurrence wins)
  type Part = { text: string; bmId?: string }
  let parts: Part[] = [{ text }]

  for (const bm of highlights) {
    if (!bm.selectedText) continue
    const next: Part[] = []
    for (const part of parts) {
      if (part.bmId !== undefined) { next.push(part); continue }
      const idx = part.text.indexOf(bm.selectedText)
      if (idx < 0) { next.push(part); continue }
      if (idx > 0) next.push({ text: part.text.slice(0, idx) })
      next.push({ text: bm.selectedText, bmId: bm.id })
      const after = part.text.slice(idx + bm.selectedText.length)
      if (after) next.push({ text: after })
    }
    parts = next
  }

  // Count words globally so data-wi indices stay correct
  let wiCounter = 0

  return (
    <>
      {parts.map((part, i) => {
        const renderInner = () => {
          if (!hl) return <>{part.text}</>
          // count words in this part
          const tokens = part.text.split(/(\s+)/)
          return (
            <>
              {tokens.map((tok, j) => {
                if (/^\s+$/.test(tok) || tok === '') return tok
                const wi = wiCounter++
                return <span key={j} data-bi={bi} data-wi={wi}>{tok}</span>
              })}
            </>
          )
        }

        // Increment wiCounter for non-hl plain parts too (so indices match)
        if (!hl && part.bmId === undefined) {
          // count words without rendering spans
          part.text.split(/(\s+)/).forEach(tok => {
            if (tok && !/^\s+$/.test(tok)) wiCounter++
          })
          return <span key={i}>{part.text}</span>
        }

        if (part.bmId !== undefined) {
          const bmId = part.bmId
          return (
            <span key={i}>
              <mark style={{
                background: 'rgba(184,146,60,0.28)', color: 'inherit',
                borderRadius: '2px', padding: '0 1px',
              }}>
                {renderInner()}
              </mark>
              <button
                className="bm-marker-btn"
                onClick={e => {
                  e.stopPropagation()
                  onBookmarkClick?.(bmId, (e.currentTarget as HTMLElement).getBoundingClientRect())
                }}
                style={{
                  background: 'none', border: 'none', cursor: 'pointer',
                  color: 'var(--gold)', fontSize: '0.65em', padding: '0 2px',
                  verticalAlign: 'super', lineHeight: 1, display: 'inline',
                }}
                title="Bookmark"
              >⊙</button>
            </span>
          )
        }
        return <span key={i}>{renderInner()}</span>
      })}
    </>
  )
}

// ── Main block renderer ───────────────────────────────────────────────────────
export function BlockRenderer({
  block, prevType, isFirst, highlightMode, blockIdx,
  bookmarkHighlights, onBookmarkClick,
}: Props) {
  const hl  = !!(highlightMode && blockIdx !== undefined)
  const bi  = blockIdx ?? 0
  const bdi = blockIdx  // for data-block-idx

  // Only highlights that belong to THIS block
  const highlights = (bookmarkHighlights ?? []).filter(bm => bm.blockIdx === bi)
  const hasHL = highlights.length > 0

  const renderText = (text: string) =>
    hasHL
      ? <TextWithHighlights text={text} bi={bi} hl={hl} highlights={highlights} onBookmarkClick={onBookmarkClick} />
      : hl
        ? <WordSpans text={text} bi={bi} />
        : <>{text}</>

  switch (block.type) {
    case 'paragraph': {
      const isDropCap = isFirst && prevType === undefined
      const isIndent  = prevType === 'paragraph'
      return (
        <p
          className={['block block-paragraph', isDropCap ? 'drop-cap' : '', isIndent ? 'para-indent' : ''].filter(Boolean).join(' ')}
          data-block-idx={bdi}
        >
          {renderText(block.text ?? '')}
        </p>
      )
    }

    case 'heading':
      return (
        <h2 className="block block-heading-2" data-block-idx={bdi}>
          {renderText(block.text ?? '')}
        </h2>
      )

    case 'quote':
      return (
        <blockquote className="block block-quote" data-block-idx={bdi}>
          <p className="block-quote__text">
            {renderText(block.text ?? '')}
          </p>
          {block.attribution && (
            <cite className="block-quote__attribution">— {block.attribution}</cite>
          )}
        </blockquote>
      )

    case 'divider':
      return (
        <div className="block block-divider" data-block-idx={bdi}>
          <span className="block-divider__glyph">✦ ✦ ✦</span>
        </div>
      )

    case 'callout':
      return (
        <div className="block block-callout" data-block-idx={bdi}>
          {block.label && (
            <div className="block-callout__label">{block.label}</div>
          )}
          <p className="block-callout__text">
            {renderText(block.text ?? '')}
          </p>
        </div>
      )

    case 'image':
      return (
        <figure className="block" data-block-idx={bdi} style={{ margin: '1.5rem 0' }}>
          <img
            src={block.url}
            alt={block.alt ?? ''}
            style={{ width: '100%', borderRadius: '2px', border: '1px solid var(--border)', display: 'block' }}
          />
          {block.caption && (
            <figcaption style={{
              marginTop: '0.5rem', fontFamily: 'var(--font-ui)', fontSize: '10px',
              letterSpacing: '0.08em', color: 'var(--text-dim)', textAlign: 'center',
            }}>
              {block.caption}
            </figcaption>
          )}
        </figure>
      )

    case 'verse':
      return (
        <pre
          className="block"
          data-block-idx={bdi}
          style={{
            fontFamily: 'var(--font-body)', fontStyle: 'italic',
            whiteSpace: 'pre-wrap', color: 'var(--text-dim)',
            lineHeight: 1.9, paddingLeft: '1.5rem',
            borderLeft: '1px solid var(--border)',
          }}
        >
          {block.text}
        </pre>
      )

    default:
      return null
  }
}
