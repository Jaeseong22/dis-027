import type { ReactNode } from 'react'

/**
 * 답변에 실제로 오는 마크다운만 처리한다 — **굵게**, `코드`, 줄바꿈, `- ` 불릿.
 * 라이브러리를 하나 더 얹을 만큼의 문법이 아니다.
 */
const INLINE = /\*\*(.+?)\*\*|`(.+?)`/g

function inline(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = []
  let last = 0
  for (const m of text.matchAll(INLINE)) {
    if (m.index > last) out.push(text.slice(last, m.index))
    out.push(
      m[1] !== undefined
        ? <strong key={`${key}-${m.index}`}>{m[1]}</strong>
        : <code key={`${key}-${m.index}`}>{m[2]}</code>,
    )
    last = m.index + m[0].length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

export function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = []
  let bullets: string[] = []

  const flush = () => {
    if (!bullets.length) return
    const items = bullets
    blocks.push(
      <ul key={`ul-${blocks.length}`}>
        {items.map((b, i) => <li key={i}>{inline(b, `${blocks.length}-${i}`)}</li>)}
      </ul>,
    )
    bullets = []
  }

  for (const line of text.split('\n')) {
    const bullet = line.match(/^\s*[-*]\s+(.*)$/)
    if (bullet) {
      bullets.push(bullet[1])
      continue
    }
    flush()
    if (line.trim()) blocks.push(<p key={`p-${blocks.length}`}>{inline(line, `${blocks.length}`)}</p>)
  }
  flush()

  return <>{blocks}</>
}
