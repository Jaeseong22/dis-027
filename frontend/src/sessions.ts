/** 채팅 세션 — 화면 기록일 뿐이다. 백엔드는 멀티턴이 아니라서 서버로는 아무것도 안 보낸다. */
import type { AgentAnswer } from './api/agent'

export interface Turn {
  id: string
  question: string
  started: number
  state: 'pending' | 'done' | 'error'
  result?: AgentAnswer
  error?: string
  ms?: number
}

export interface Session {
  id: string
  title: string
  updatedAt: number
  turns: Turn[]
}

const KEY = 'gongsi.sessions.v1'
/** ponytail: 오래된 것부터 버린다. 세션 검색·서버 저장이 필요해지면 그때 백엔드로 옮긴다. */
const LIMIT = 20

export function loadSessions(): Session[] {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? '[]')
    if (!Array.isArray(raw)) return []
    return raw.map((s: Session) => ({
      ...s,
      // 새로고침으로 끊긴 요청은 되살릴 수 없다.
      turns: s.turns.map((t) =>
        t.state === 'pending' ? { ...t, state: 'error' as const, error: '요청이 중단되었습니다.' } : t,
      ),
    }))
  } catch {
    return []
  }
}

export function saveSessions(list: Session[]) {
  const keep = list.filter((s) => s.turns.length).slice(0, LIMIT)
  try {
    localStorage.setItem(KEY, JSON.stringify(keep))
  } catch {
    // 용량 초과 — 최근 5개만 남기고 재시도, 그래도 안 되면 포기한다.
    try {
      localStorage.setItem(KEY, JSON.stringify(keep.slice(0, 5)))
    } catch { /* 기록은 부가 기능이다 */ }
  }
}

const DAY = 86_400_000

function startOfDay(t: number) {
  const d = new Date(t)
  d.setHours(0, 0, 0, 0)
  return d.getTime()
}

export interface SessionGroup {
  label: string
  sessions: Session[]
}

/** 최근 순으로 정렬해 오늘 · 어제 · 지난 7일 · 이전으로 나눈다. 빈 그룹은 뺀다. */
export function groupSessions(list: Session[], now: number = Date.now()): SessionGroup[] {
  const today = startOfDay(now)
  const groups: SessionGroup[] = [
    { label: '오늘', sessions: [] },
    { label: '어제', sessions: [] },
    { label: '지난 7일', sessions: [] },
    { label: '이전', sessions: [] },
  ]
  for (const s of [...list].sort((a, b) => b.updatedAt - a.updatedAt)) {
    const i = s.updatedAt >= today ? 0 : s.updatedAt >= today - DAY ? 1 : s.updatedAt >= today - 6 * DAY ? 2 : 3
    groups[i].sessions.push(s)
  }
  return groups.filter((g) => g.sessions.length)
}
