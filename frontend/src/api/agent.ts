/** 공시 Agent API 클라이언트. 절대주소를 쓰지 않는다 — 개발은 Vite proxy, 운영은 Nginx. */

export interface AgentAnswer {
  question_id: string
  question: string
  retrieved_context: string
  think_trace: string
  answer: string
}

export interface HealthStatus {
  status: string
  agent: string
}

/** 서버가 최대 110초까지 쓴다. 여유를 두고 300초. */
const TIMEOUT_MS = 300_000

export class AgentError extends Error {}

export async function fetchAnswer(
  questionId: string,
  question: string,
  signal?: AbortSignal,
): Promise<AgentAnswer> {
  const query = new URLSearchParams({ question_id: questionId, question })
  const timeout = AbortSignal.timeout(TIMEOUT_MS)

  let res: Response
  try {
    res = await fetch(`/answer?${query}`, {
      signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
    })
  } catch (e) {
    if (signal?.aborted) throw e
    if (timeout.aborted) throw new AgentError('응답이 300초를 넘었습니다.')
    throw new AgentError('서버에 연결하지 못했습니다.')
  }

  if (!res.ok) throw new AgentError(`서버 오류 (HTTP ${res.status})`)

  let data: Partial<AgentAnswer>
  try {
    data = await res.json()
  } catch {
    throw new AgentError('응답을 해석하지 못했습니다.')
  }

  // answer 없는 200 응답은 오류로 취급한다.
  if (!data.answer) throw new AgentError('답변이 비어 있습니다.')

  return {
    question_id: data.question_id ?? questionId,
    question: data.question ?? question,
    retrieved_context: data.retrieved_context ?? '',
    think_trace: data.think_trace ?? '',
    answer: data.answer,
  }
}

export async function fetchHealth(): Promise<HealthStatus> {
  const res = await fetch('/health', { signal: AbortSignal.timeout(10_000) })
  if (!res.ok) throw new AgentError(`HTTP ${res.status}`)
  return res.json()
}
