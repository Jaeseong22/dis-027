import { useEffect, useRef, useState } from 'react'
import { AgentError, fetchAnswer, fetchHealth } from './api/agent'
import type { HealthStatus } from './api/agent'
import { Markdown } from './Markdown'
import { groupSessions, loadSessions, saveSessions } from './sessions'
import type { Session, Turn } from './sessions'
import { ChatIcon, MenuIcon, PlusIcon, SendIcon } from './icons'
import { JsonPage, ResponseTabs } from './Inspector'
import './App.css'

const SUGGESTIONS = [
  '삼성전자의 2025년 연결기준 매출액은 얼마인가?',
  '삼성전자 2025년 연말 기준 직원등의 현황에서 성별 남자의 기간의 정함이 없는 근로자 수는?',
  '삼성전자의 목표주가는 얼마야?',
  '파마리서치의 2025년 특허 만료 일정을 알려줘',
]

type Health = { state: 'checking' | 'up' | 'down'; agent?: string }

function useHealth(): Health {
  const [health, setHealth] = useState<Health>({ state: 'checking' })
  useEffect(() => {
    const check = () =>
      fetchHealth().then(
        (h: HealthStatus) => setHealth({ state: 'up', agent: h.agent }),
        () => setHealth({ state: 'down' }),
      )
    check()
    const t = setInterval(check, 60_000)
    return () => clearInterval(t)
  }, [])
  return health
}

/**
 * 두 화면뿐이다 — 라우터 라이브러리를 넣을 이유가 없다.
 * Nginx 의 SPA fallback 이 `/json` 도 index.html 로 넘겨 준다.
 */
function usePath(): [string, (to: string) => void] {
  const [path, setPath] = useState(() => window.location.pathname)
  useEffect(() => {
    const pop = () => setPath(window.location.pathname)
    window.addEventListener('popstate', pop)
    return () => window.removeEventListener('popstate', pop)
  }, [])
  const go = (to: string) => {
    if (to !== window.location.pathname) window.history.pushState(null, '', to)
    setPath(to)
  }
  return [path, go]
}

function HealthDot({ health, label }: { health: Health; label: string }) {
  return (
    <span className={`health ${health.state}`} title={label}>
      <span className="dot" aria-hidden="true" />
      <span className="health-text">{label}</span>
    </span>
  )
}

function Elapsed({ from }: { from: number }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])
  return <>{Math.floor((now - from) / 1000)}초 경과</>
}

interface ChatProps {
  sessions: Session[]
  setSessions: React.Dispatch<React.SetStateAction<Session[]>>
  health: Health
  navOpen: boolean
  setNavOpen: (v: boolean) => void
  activeId: string | null
  setActiveId: (v: string | null) => void
}

function Chat({ sessions, setSessions, health, navOpen, setNavOpen, activeId, setActiveId }: ChatProps) {
  const [input, setInput] = useState('')
  const bottom = useRef<HTMLDivElement>(null)

  const active = sessions.find((s) => s.id === activeId)
  const turns = active?.turns ?? []
  const busy = turns.some((t) => t.state === 'pending')

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth' })
  }, [turns.length, activeId])

  /** 항상 독립 요청이다 — 이전 질문을 함께 보내지 않는다. */
  const run = async (sid: string, turnId: string, question: string) => {
    const started = Date.now()
    const patch = (t: Partial<Turn>) =>
      setSessions((prev) =>
        prev.map((s) =>
          s.id === sid
            ? { ...s, updatedAt: Date.now(), turns: s.turns.map((x) => (x.id === turnId ? { ...x, ...t } : x)) }
            : s,
        ),
      )
    patch({ state: 'pending', started, error: undefined, result: undefined, ms: undefined })
    try {
      const result = await fetchAnswer(turnId, question)
      patch({ state: 'done', result, ms: Date.now() - started })
    } catch (e) {
      patch({ state: 'error', error: e instanceof AgentError ? e.message : '알 수 없는 오류가 발생했습니다.' })
    }
  }

  const ask = (question: string) => {
    const q = question.trim()
    if (!q || busy) return
    const turn: Turn = { id: `Q-${Date.now().toString(36)}`, question: q, started: Date.now(), state: 'pending' }
    const sid = activeId ?? `S-${Date.now().toString(36)}`
    setSessions((prev) => {
      const existing = prev.find((s) => s.id === sid)
      const next: Session = existing
        ? { ...existing, updatedAt: turn.started, turns: [...existing.turns, turn] }
        : { id: sid, title: q, updatedAt: turn.started, turns: [turn] }
      return [next, ...prev.filter((s) => s.id !== sid)]
    })
    setActiveId(sid)
    setInput('')
    run(sid, turn.id, q)
  }

  const open = (id: string | null) => {
    setActiveId(id)
    setNavOpen(false)
  }

  return (
    <>
      {navOpen && <div className="scrim" onClick={() => setNavOpen(false)} />}

      <nav className={`sidebar ${navOpen ? 'open' : ''}`} aria-label="채팅 기록">
        <button className="new-chat" type="button" onClick={() => open(null)}>
          <PlusIcon /> 새 채팅
        </button>

        <div className="history">
          {groupSessions(sessions).map((group) => (
            <section key={group.label}>
              <h2>{group.label}</h2>
              {group.sessions.map((s) => (
                <button
                  key={s.id}
                  type="button"
                  className={`history-item ${s.id === activeId ? 'active' : ''}`}
                  onClick={() => open(s.id)}
                  title={s.title}
                >
                  <ChatIcon />
                  <span>{s.title}</span>
                </button>
              ))}
            </section>
          ))}
        </div>

        <footer className="sidebar-foot">
          <strong>공시 Agent</strong>
          <HealthDot
            health={health}
            label={health.state === 'up' ? '연결됨' : health.state === 'down' ? '연결 끊김' : '확인 중'}
          />
        </footer>
      </nav>

      <main className="main">
        <div className="stream">
          <div className="col">
            {turns.length === 0 ? (
              <section className="intro">
                <h1>공시에서 필요한 정보를 찾아보세요</h1>
                <p>국내 상장사 공시를 기반으로 답변과 근거를 제공합니다.</p>
                <div className="suggestions">
                  {SUGGESTIONS.map((s) => (
                    <button key={s} type="button" className="suggestion" onClick={() => ask(s)}>
                      {s}
                    </button>
                  ))}
                </div>
              </section>
            ) : (
              turns.map((turn) => (
                <article key={turn.id} className="turn">
                  <div className="question">
                    <span className="avatar" aria-hidden="true">나</span>
                    <p>{turn.question}</p>
                  </div>

                  {turn.state === 'pending' && (
                    <div className="loading" aria-live="polite">
                      <span className="dots" aria-hidden="true"><i /><i /><i /></span>
                      <span>공시를 확인하고 있습니다…</span>
                      <span className="elapsed"><Elapsed from={turn.started} /></span>
                    </div>
                  )}

                  {turn.state === 'error' && (
                    <div className="failed" role="alert">
                      <p>답변을 가져오지 못했습니다. {turn.error}</p>
                      <button type="button" onClick={() => run(active!.id, turn.id, turn.question)}>
                        다시 시도
                      </button>
                    </div>
                  )}

                  {turn.state === 'done' && turn.result && (
                    <div className="response">
                      <div className="answer">
                        <Markdown text={turn.result.answer} />
                      </div>
                      <div className="meta">답변 완료 · {((turn.ms ?? 0) / 1000).toFixed(1)}초</div>
                      <ResponseTabs result={turn.result} />
                    </div>
                  )}
                </article>
              ))
            )}
            <div ref={bottom} />
          </div>
        </div>

        <form
          className="composer"
          onSubmit={(e) => {
            e.preventDefault()
            ask(input)
          }}
        >
          <div className="col field">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  ask(input)
                }
              }}
              placeholder="공시에 대해 궁금한 내용을 질문해보세요"
              rows={1}
              aria-label="질문 입력"
            />
            <button type="submit" className="send" disabled={busy || !input.trim()} aria-label="질문 보내기">
              <SendIcon />
            </button>
          </div>
          <p className="hint">
            {busy ? '답변을 생성하는 동안에는 새 질문을 보낼 수 없습니다.' : 'Enter 전송 · Shift + Enter 줄바꿈'}
          </p>
        </form>
      </main>
    </>
  )
}

export default function App() {
  const [sessions, setSessions] = useState<Session[]>(loadSessions)
  const [navOpen, setNavOpen] = useState(false)
  // 화면을 오가도 열려 있던 대화는 유지한다 — Chat 이 언마운트돼도 남게 위로 올린다.
  const [activeId, setActiveId] = useState<string | null>(null)
  const [path, go] = usePath()
  const health = useHealth()
  const onJson = path === '/json'

  useEffect(() => saveSessions(sessions), [sessions])

  const nav = (to: string, label: string) => (
    <button
      type="button"
      className={`view-tab ${(to === '/json') === onJson ? 'active' : ''}`}
      onClick={() => {
        setNavOpen(false)
        go(to)
      }}
    >
      {label}
    </button>
  )

  return (
    <div className="shell">
      <header className="header">
        {!onJson && (
          <button className="icon-btn only-mobile" type="button" onClick={() => setNavOpen(true)} aria-label="메뉴 열기">
            <MenuIcon />
          </button>
        )}
        <div className="brand">
          <span className="mark" aria-hidden="true" />
          <span className="brand-name">공시 Agent</span>
        </div>
        <nav className="view-tabs" aria-label="화면 전환">
          {nav('/', '채팅')}
          {nav('/json', 'JSON')}
        </nav>
        <HealthDot
          health={health}
          label={
            health.state === 'up'
              ? `서버 정상 · ${health.agent}`
              : health.state === 'down'
                ? '서버 연결 실패'
                : '상태 확인 중'
          }
        />
      </header>

      <div className="body">
        {onJson ? (
          <JsonPage sessions={sessions} onGoChat={() => go('/')} />
        ) : (
          <Chat
            sessions={sessions}
            setSessions={setSessions}
            health={health}
            navOpen={navOpen}
            setNavOpen={setNavOpen}
            activeId={activeId}
            setActiveId={setActiveId}
          />
        )}
      </div>
    </div>
  )
}
