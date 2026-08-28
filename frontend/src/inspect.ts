/**
 * `retrieved_context` · `think_trace` 를 화면용 구조로 나눈다.
 *
 * 원칙: **내용을 바꾸지 않는다.** 확실히 알아본 패턴만 떼어내고,
 * 못 알아본 것은 원문 그대로 남긴다. 모양이 어긋나면 `null` 을 돌려
 * 호출부가 원문 표시로 되돌아가게 한다 — 추측해서 깨지는 편보다 낫다.
 */

// ── retrieved_context ─────────────────────────────────
export interface EvidenceBlock {
  no: string
  tool: string
  args: string
  /** 출처 — 접수번호는 떼어 따로 담는다. */
  source?: string
  receipt?: string
  basis?: string
  /** `(28개 항목 중 …)` 같은 꼬리 주석. */
  note?: string
  /** 알아보지 못한 나머지 전부. 공백 그대로. */
  data: string
  xbrl?: string
}

const HEAD = /^\[(\d+)\]\s+([A-Za-z_][\w.]*)\((.*)\)\s*$/
const META = /^ {2,}(출처|기준) {2,}(.+?)\s*$/
const RECEIPT = /접수번호\s*(\d{6,})/

/** 공통 들여쓰기만 벗긴다 — 표의 열 정렬은 상대 간격이라 보존된다. */
function dedent(lines: string[]): string {
  const filled = lines.filter((l) => l.trim())
  if (!filled.length) return ''
  const cut = Math.min(...filled.map((l) => l.length - l.trimStart().length))
  return lines.map((l) => l.slice(cut)).join('\n').trim()
}

export function parseEvidence(text: string): EvidenceBlock[] | null {
  const body = (text ?? '').replace(/\r/g, '').trim()
  if (!body) return null

  const out: EvidenceBlock[] = []
  for (const chunk of body.split(/\n[ \t]*\n/)) {
    const lines = chunk.split('\n')
    const h = lines[0].match(HEAD)
    if (!h) return null // 모르는 모양이 하나라도 있으면 통째로 원문을 쓴다

    const block: EvidenceBlock = { no: h[1], tool: h[2], args: h[3], data: '' }
    const rest: string[] = []
    const xbrl: string[] = []
    let inXbrl = false

    for (const line of lines.slice(1)) {
      if (inXbrl) {
        xbrl.push(line)
        continue
      }
      const t = line.trim()
      if (t.startsWith('XBRL')) {
        inXbrl = true // 제목 줄은 화면 라벨이 대신한다
        continue
      }
      const m = line.match(META)
      if (m) {
        if (m[1] === '출처') {
          const r = m[2].match(RECEIPT)
          if (r) block.receipt = r[1]
          block.source = m[2].replace(RECEIPT, '').replace(/[,·\s]+$/, '').trim()
        } else {
          block.basis = m[2]
        }
        continue
      }
      if (!block.note && t.length > 2 && t.startsWith('(') && t.endsWith(')')) {
        block.note = t.slice(1, -1)
        continue
      }
      rest.push(line)
    }

    block.data = dedent(rest)
    if (xbrl.length) block.xbrl = dedent(xbrl)
    out.push(block)
  }
  return out.length ? out : null
}

// ── think_trace ───────────────────────────────────────
export interface TraceMeta {
  route?: string
  stop?: string
  stopNote?: string
  steps?: string
  llm?: string
  tokens?: string
  seconds?: string
}

/** 섹션 본문의 한 항목 — 머리줄 하나에 들여쓴 상세 몇 줄. */
export interface TraceItem {
  head: string
  detail: string
  /** 머리줄이 `스텝 N:` 이면 N. */
  step?: string
  mark?: 'ok' | 'blocked'
}

export interface TraceSection {
  name: string
  items: TraceItem[]
  /** 원문 그대로 — 항목으로 못 쪼갠 경우와 원본 보기용. */
  raw: string
}

export interface Trace {
  meta?: TraceMeta
  sections: TraceSection[]
  /** 섹션 머리말보다 앞에 온 줄들. */
  preamble: string
}

const HEADER = /^\[([^\]]*)\]\s*(.*)$/
const SECTION = /^──\s*(.*?)\s*─*$/
const STOP = /^([^(]+)(?:\((.*)\))?$/
const STEP = /^스텝\s*(\d+)\s*:\s*(.*)$/

function itemsOf(lines: string[]): TraceItem[] {
  const items: TraceItem[] = []
  for (const line of lines) {
    if (!line.trim()) continue
    const indented = /^\s/.test(line)
    if (indented && items.length) {
      const last = items[items.length - 1]
      last.detail = last.detail ? `${last.detail}\n${line}` : line
      continue
    }
    const s = line.match(STEP)
    items.push({
      head: s ? s[2] : line.trim(),
      detail: '',
      step: s?.[1],
      // 검증 결과만 표시한다 — 그 외 문장은 해석하지 않는다.
      mark: /통과/.test(line) ? 'ok' : /차단/.test(line) ? 'blocked' : undefined,
    })
  }
  return items.map((i) => ({ ...i, detail: dedent(i.detail.split('\n')) }))
}

export function parseTrace(text: string): Trace | null {
  const lines = (text ?? '').replace(/\r/g, '').split('\n')
  let meta: TraceMeta | undefined

  const h = lines[0]?.match(HEADER)
  if (h && h[1].startsWith('route=')) {
    meta = {}
    for (const m of h[1].matchAll(/(\w+)=(\S+)/g)) {
      if (m[1] === 'stop') {
        const s = m[2].match(STOP)
        meta.stop = s?.[1]
        meta.stopNote = s?.[2]
      } else if (m[1] === 'route' || m[1] === 'steps' || m[1] === 'llm' || m[1] === 'tokens') {
        meta[m[1]] = m[2]
      }
    }
    const sec = h[1].match(/([\d.]+)s\s*$/)
    if (sec) meta.seconds = sec[1]
    lines.shift()
    if (h[2]) lines.unshift(h[2]) // `[route=error] TypeError: …` 의 뒤쪽 설명
  }

  const names: string[] = []
  const bodies: string[][] = []
  const preamble: string[] = []
  for (const line of lines) {
    const m = line.match(SECTION)
    if (m && m[1]) {
      names.push(m[1])
      bodies.push([])
      continue
    }
    ;(bodies.length ? bodies[bodies.length - 1] : preamble).push(line)
  }

  const sections = names.map((name, i) => ({
    name,
    items: itemsOf(bodies[i]),
    raw: bodies[i].join('\n').trim(),
  }))

  if (!meta && !sections.length) return null
  return { meta, sections, preamble: preamble.join('\n').trim() }
}

// ── JSON 하이라이트 ───────────────────────────────────
export type JsonKind = 'key' | 'str' | 'num' | 'lit' | 'plain'
export interface JsonToken {
  text: string
  kind: JsonKind
}

const TOKEN =
  /("(?:\\.|[^"\\])*")(\s*:)|("(?:\\.|[^"\\])*")|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g

/** 문자열/키/숫자/리터럴만 나눈다. 색칠 외에 하는 일이 없다. */
export function jsonTokens(json: string): JsonToken[] {
  const out: JsonToken[] = []
  let last = 0
  for (const m of json.matchAll(TOKEN)) {
    if (m.index > last) out.push({ text: json.slice(last, m.index), kind: 'plain' })
    if (m[1] !== undefined) {
      out.push({ text: m[1], kind: 'key' })
      out.push({ text: m[2], kind: 'plain' })
    } else if (m[3] !== undefined) {
      out.push({ text: m[3], kind: 'str' })
    } else if (m[4] !== undefined) {
      out.push({ text: m[4], kind: 'lit' })
    } else {
      out.push({ text: m[5], kind: 'num' })
    }
    last = m.index + m[0].length
  }
  if (last < json.length) out.push({ text: json.slice(last), kind: 'plain' })
  return out
}
