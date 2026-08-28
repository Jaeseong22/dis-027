import { useEffect, useRef, useState } from 'react'
import { AgentError, fetchAnswer, fetchHealth } from './api/agent'
import type { AgentAnswer, HealthStatus } from './api/agent'
import { Markdown } from './Markdown'
import './App.css'

const SUGGESTIONS = [
  '삼성전자의 2025년 연결기준 매출액은 얼마인가?',
  '삼성전자 2025년 연말 기준 직원등의 현황에서 성별 남자의 기간의 정함이 없는 근로자 수는?',
  '삼성전자의 목표주가는 얼마야?',
  '파마리서치의 2025년 특허 만료 일정을 알려줘',
]

interface Turn {
  id: string
  question: string
  started: number
  state: 'pending' | 'done' | 'error'
  result?: AgentAnswer
  error?: string
  seconds?: number
}

function Elapsed({ from }: { from: number }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])
  return <>{Math.floor((now - from) / 1000)}초</>
}

function HealthBadge() {
  const [health, setHealth] = useState<HealthStatus | null>(null)
  const [down, setDown] = useState(false)

  useEffect(() => {
    const check = () =>
      fetchHealth().then(
        (h) => { setHealth(h); setDown(false) },
        () => setDown(true),
      )
    check()
    const t = setInterval(check, 60_000)
    return () => clearInterval(t)
  }, [])

  const ok = health && !down
  return (
    <span className={`health ${ok ? 'up' : down ? 'down' : ''}`}>
      <span className="dot" aria-hidden="true" />
      {ok ? `정상 · ${health.agent}` : down ? '연결 실패' : '확인 중'}
    </span>
  )
}

function Collapsible({ title, body }: { title: string; body: string }) {
  if (!body) return null
  return (
    <details className="collapsible">
      <summary>{title}</summary>
      <pre>{body}</pre>
    </details>
  )
}

export default function App() {
  const [turns, setTurns] = useState<Turn[]>([])
  const [input, setInput] = useState('')
  const counter = useRef(0)
  const bottom = useRef<HTMLDivElement>(null)
  const busy = turns.some((t) => t.state === 'pending')

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth' })
  }, [turns])

  const run = async (id: string, question: string) => {
    const started = Date.now()
    const base = { id, question, started }
    const patch = (t: Turn) => setTurns((prev) => prev.map((p) => (p.id === id ? t : p)))
    patch({ ...base, state: 'pending' })
    try {
      const result = await fetchAnswer(id, question)
      patch({ ...base, state: 'done', result, seconds: Math.round((Date.now() - started) / 1000) })
    } catch (e) {
      const msg = e instanceof AgentError ? e.message : '알 수 없는 오류가 발생했습니다.'
      patch({ ...base, state: 'error', error: msg })
    }
  }

  const ask = (question: string) => {
    const q = question.trim()
    if (!q || busy) return
    const id = `Q-${String(++counter.current).padStart(3, '0')}`
    setTurns((prev) => [...prev, { id, question: q, started: Date.now(), state: 'pending' }])
    setInput('')
    run(id, q)
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo">공시</span>
          <div>
            <h1>공시 Q&amp;A Agent</h1>
            <p>전자공시 기반 · 근거를 붙여 답합니다</p>
          </div>
        </div>
        <HealthBadge />
      </header>

      <main className="thread">
        {turns.length === 0 && (
          <section className="empty">
            <h2>무엇이 궁금하신가요?</h2>
            <p>기업 공시에서 확인된 사실만 근거와 함께 답합니다.</p>
            <div className="chips">
              {SUGGESTIONS.map((s) => (
                <button key={s} type="button" className="chip" onClick={() => ask(s)}>
                  {s}
                </button>
              ))}
            </div>
          </section>
        )}

        {turns.map((turn) => (
          <article key={turn.id} className="turn">
            <div className="bubble user">{turn.question}</div>

            {turn.state === 'pending' && (
              <div className="bubble agent loading" aria-live="polite">
                <span className="spinner" aria-hidden="true" />
                <span>공시를 조회하고 있습니다… <Elapsed from={turn.started} /></span>
                <small>보통 7~20초, 길게는 110초까지 걸립니다.</small>
              </div>
            )}

            {turn.state === 'error' && (
              <div className="bubble agent error" role="alert">
                <strong>답변을 가져오지 못했습니다.</strong>
                <span>{turn.error}</span>
                <button type="button" onClick={() => run(turn.id, turn.question)}>
                  다시 시도
                </button>
              </div>
            )}

            {turn.state === 'done' && turn.result && (
              <div className="bubble agent">
                <div className="answer">
                  <Markdown text={turn.result.answer} />
                </div>
                <div className="meta">
                  {turn.id} · {turn.seconds}초
                </div>
                <Collapsible title="근거 보기" body={turn.result.retrieved_context} />
                <Collapsible title="추론 과정" body={turn.result.think_trace} />
              </div>
            )}
          </article>
        ))}
        <div ref={bottom} />
      </main>

      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault()
          ask(input)
        }}
      >
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              ask(input)
            }
          }}
          placeholder="예) 삼성전자의 2025년 연결기준 매출액은 얼마인가?"
          rows={1}
          aria-label="질문 입력"
        />
        <button type="submit" disabled={busy || !input.trim()}>
          {busy ? '조회 중' : '질문하기'}
        </button>
      </form>
    </div>
  )
}
