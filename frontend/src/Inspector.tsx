/** Agent Inspector — 근거 · 추론 과정 · 원본 JSON 화면. 표시 전용이고 API 를 부르지 않는다. */
import { useEffect, useMemo, useRef, useState } from 'react'
import type { AgentAnswer } from './api/agent'
import type { Session } from './sessions'
import { jsonTokens, parseEvidence, parseTrace } from './inspect'
import type { EvidenceBlock, TraceSection } from './inspect'
import { CheckIcon, CodeIcon, CopyIcon, DocIcon, FlowIcon } from './icons'

const CIRCLED = ['①', '②', '③', '④', '⑤', '⑥', '⑦', '⑧', '⑨', '⑩']

/**
 * 구식 복사 경로. 운영은 `http://<IP>` 로 서비스되므로 **보안 컨텍스트가 아니고**
 * 거기서는 `navigator.clipboard` 자체가 없다 — 심사자가 쓰는 바로 그 환경이다.
 * `execCommand` 는 폐기 예정이지만 이 조건에서 동작하는 유일한 방법이다.
 */
function legacyCopy(text: string): boolean {
  const ta = document.createElement('textarea')
  ta.value = text
  ta.setAttribute('readonly', '')
  ta.style.cssText = 'position:fixed;top:-1000px;opacity:0'
  document.body.appendChild(ta)
  ta.select()
  ta.setSelectionRange(0, text.length) // iOS Safari 는 select() 만으로는 안 잡는다
  let ok = false
  try {
    ok = document.execCommand('copy')
  } catch {
    ok = false
  }
  ta.remove()
  return ok
}

/** 클립보드 복사 + "복사됨" 짧은 피드백. */
function useCopy(): [boolean, (text: string) => void] {
  const [done, setDone] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => () => clearTimeout(timer.current), [])

  const flash = () => {
    setDone(true)
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setDone(false), 1500)
  }

  const copy = (text: string) => {
    const p = navigator.clipboard?.writeText(text)
    if (p) {
      // 권한 거부·포커스 없음으로 거절되면 구식 경로로 한 번 더 시도한다.
      p.then(flash, () => legacyCopy(text) && flash())
      return
    }
    if (legacyCopy(text)) flash()
  }

  return [done, copy]
}

function CopyButton({ text, label = 'JSON 복사' }: { text: string; label?: string }) {
  const [done, copy] = useCopy()
  return (
    <button type="button" className={`copy ${done ? 'done' : ''}`} onClick={() => copy(text)}>
      {done ? <CheckIcon /> : <CopyIcon />}
      {done ? '복사됨' : label}
    </button>
  )
}

// ── 원본 JSON ─────────────────────────────────────────
export function JsonView({ value }: { value: AgentAnswer }) {
  const text = useMemo(() => JSON.stringify(value, null, 2), [value])
  const tokens = useMemo(() => jsonTokens(text), [text])
  return (
    <div className="pane">
      <div className="pane-bar">
        <span className="pane-title">
          <CodeIcon /> Agent Response
        </span>
        <CopyButton text={text} />
      </div>
      <pre className="code">
        {tokens.map((t, i) => (t.kind === 'plain' ? t.text : <span key={i} className={`j-${t.kind}`}>{t.text}</span>))}
      </pre>
    </div>
  )
}

// ── 근거 ──────────────────────────────────────────────
function Chip({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <div className={`chip ${accent ? 'accent' : ''}`}>
      <span className="chip-label">{label}</span>
      <span className="chip-value">{value}</span>
    </div>
  )
}

function EvidenceCard({ block }: { block: EvidenceBlock }) {
  return (
    <section className="ev-card">
      <header className="ev-head">
        <span className="ev-no">{block.no}</span>
        <code className="ev-tool">{block.tool}</code>
        {block.args && <span className="ev-args">{block.args}</span>}
      </header>

      {(block.source || block.receipt || block.basis) && (
        <div className="chips">
          {block.source && <Chip label="출처" value={block.source} accent />}
          {block.receipt && <Chip label="접수번호" value={block.receipt} />}
          {block.basis && <Chip label="기준" value={block.basis} />}
        </div>
      )}

      {block.data && (
        <div className="ev-data">
          <h4>사용된 데이터</h4>
          <pre className="mono">{block.data}</pre>
        </div>
      )}

      {block.note && <p className="ev-note">{block.note}</p>}

      {block.xbrl && (
        <div className="ev-data">
          <h4>XBRL 태그</h4>
          <pre className="mono">{block.xbrl}</pre>
        </div>
      )}
    </section>
  )
}

export function EvidenceView({ text }: { text: string }) {
  const blocks = useMemo(() => parseEvidence(text), [text])
  const [raw, setRaw] = useState(false)

  if (!blocks) return <pre className="mono plain-fallback">{text}</pre>

  return (
    <>
      <div className="view-bar">
        <span className="view-count">근거 {blocks.length}건</span>
        <button type="button" className="link-btn" onClick={() => setRaw((v) => !v)}>
          {raw ? '구조화 보기' : '원본 보기'}
        </button>
      </div>
      {raw ? (
        <pre className="mono plain-fallback">{text}</pre>
      ) : (
        blocks.map((b) => <EvidenceCard key={b.no} block={b} />)
      )}
    </>
  )
}

// ── 추론 과정 ─────────────────────────────────────────
function TraceBlock({ section, no, collapsed }: { section: TraceSection; no: number; collapsed: boolean }) {
  return (
    <details className="tr-sec" open={!collapsed}>
      <summary>
        <span className="tr-no">{CIRCLED[no] ?? no + 1}</span>
        {section.name}
      </summary>
      {section.items.length ? (
        <ol className="tr-items">
          {section.items.map((item, i) => (
            <li key={i} className={item.mark ? `mark-${item.mark}` : ''}>
              <div className="tr-head">
                {item.step && <span className="tr-step">step {item.step}</span>}
                {item.mark && <span className="tr-mark">{item.mark === 'ok' ? '✓' : '✕'}</span>}
                <span className="tr-text">{item.head}</span>
              </div>
              {item.detail && <pre className="mono">{item.detail}</pre>}
            </li>
          ))}
        </ol>
      ) : (
        <pre className="mono">{section.raw}</pre>
      )}
    </details>
  )
}

export function TraceView({ text }: { text: string }) {
  const trace = useMemo(() => parseTrace(text), [text])
  const [raw, setRaw] = useState(false)

  if (!trace) return <pre className="mono plain-fallback">{text}</pre>

  const m = trace.meta
  return (
    <>
      <div className="view-bar">
        <span className="view-count">Agent 실행</span>
        <button type="button" className="link-btn" onClick={() => setRaw((v) => !v)}>
          {raw ? '구조화 보기' : '원본 보기'}
        </button>
      </div>

      {m && (
        <div className="badges">
          {m.stop && (
            <span className={`badge ${m.stop === 'answered' ? 'ok' : 'warn'}`}>
              {m.stop === 'answered' ? '✓ ' : '· '}
              {m.stop}
              {m.stopNote ? ` (${m.stopNote})` : ''}
            </span>
          )}
          {m.route && <span className="badge">route {m.route}</span>}
          {m.steps && <span className="badge">{m.steps} steps</span>}
          {m.llm && <span className="badge">LLM {m.llm}회</span>}
          {m.tokens && <span className="badge">{m.tokens} tokens</span>}
          {m.seconds && <span className="badge">{m.seconds}s</span>}
        </div>
      )}

      {raw ? (
        <pre className="mono plain-fallback">{text}</pre>
      ) : (
        <>
          {trace.preamble && <pre className="mono">{trace.preamble}</pre>}
          {trace.sections.map((s, i) => (
            // 감사 로그는 길다 — 접힌 채로 둔다.
            <TraceBlock key={`${s.name}-${i}`} section={s} no={i} collapsed={s.name.includes('감사')} />
          ))}
        </>
      )}
    </>
  )
}

// ── 답변 아래 탭 ──────────────────────────────────────
export function ResponseTabs({ result }: { result: AgentAnswer }) {
  const [tab, setTab] = useState<'evidence' | 'trace' | null>(null)
  const has = { evidence: !!result.retrieved_context, trace: !!result.think_trace }
  if (!has.evidence && !has.trace) return null

  const toggle = (t: 'evidence' | 'trace') => setTab((cur) => (cur === t ? null : t))

  return (
    <div className={`tabs-box ${tab ? 'open' : ''}`}>
      <div className="tabs-row" role="tablist">
        {has.evidence && (
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'evidence'}
            className={`tab ${tab === 'evidence' ? 'active' : ''}`}
            onClick={() => toggle('evidence')}
          >
            <DocIcon /> 근거
          </button>
        )}
        {has.trace && (
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'trace'}
            className={`tab ${tab === 'trace' ? 'active' : ''}`}
            onClick={() => toggle('trace')}
          >
            <FlowIcon /> 추론 과정
          </button>
        )}
      </div>
      {tab && (
        <div className="tab-body">
          {tab === 'evidence' ? (
            <EvidenceView text={result.retrieved_context} />
          ) : (
            <TraceView text={result.think_trace} />
          )}
        </div>
      )}
    </div>
  )
}

// ── /json 화면 ────────────────────────────────────────
interface Entry {
  key: string
  question: string
  at: number
  ms?: number
  result: AgentAnswer
}

function entriesOf(sessions: Session[]): Entry[] {
  const out: Entry[] = []
  for (const s of sessions) {
    for (const t of s.turns) {
      if (t.state === 'done' && t.result) {
        out.push({ key: `${s.id}/${t.id}`, question: t.question, at: t.started, ms: t.ms, result: t.result })
      }
    }
  }
  return out.sort((a, b) => b.at - a.at)
}

const time = (t: number) =>
  new Date(t).toLocaleString('ko-KR', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })

export function JsonPage({ sessions, onGoChat }: { sessions: Session[]; onGoChat: () => void }) {
  const entries = useMemo(() => entriesOf(sessions), [sessions])
  const [picked, setPicked] = useState<string | null>(null)
  const current = entries.find((e) => e.key === picked) ?? entries[0]

  return (
    <div className="json-page">
      <aside className="json-list" aria-label="최근 질의">
        <h2>최근 질의</h2>
        {entries.length === 0 ? (
          <p className="json-list-empty">기록 없음</p>
        ) : (
          entries.map((e) => (
            <button
              key={e.key}
              type="button"
              className={`json-item ${e.key === current?.key ? 'active' : ''}`}
              onClick={() => setPicked(e.key)}
              title={e.question}
            >
              <span className="json-item-q">{e.question}</span>
              <span className="json-item-t">{time(e.at)}</span>
            </button>
          ))
        )}
      </aside>

      <main className="json-main">
        <header className="json-intro">
          <h1>Agent 응답 JSON</h1>
          <p>선택한 질의의 원본 API 응답을 확인할 수 있습니다.</p>
        </header>

        {current ? (
          <>
            <div className="json-meta">
              <span>{Object.keys(current.result).length} fields</span>
              {current.ms !== undefined && <span>응답시간 {(current.ms / 1000).toFixed(1)}s</span>}
              <span className="json-qid">question_id: {current.result.question_id || '(없음)'}</span>
            </div>
            <JsonView value={current.result} />
          </>
        ) : (
          <div className="json-empty">
            <p>아직 확인할 응답이 없습니다.</p>
            <button type="button" className="primary-btn" onClick={onGoChat}>
              채팅에서 질문하기
            </button>
          </div>
        )}
      </main>
    </div>
  )
}
