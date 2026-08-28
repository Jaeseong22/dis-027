/** 자체 점검: `npm run check` — Markdown 렌더러와 세션 그룹화. */
import assert from 'node:assert'
import { renderToStaticMarkup } from 'react-dom/server'
import { Markdown } from './Markdown'
import { groupSessions } from './sessions'
import { jsonTokens, parseEvidence, parseTrace } from './inspect'
import type { Session } from './sessions'

const html = (text: string) => renderToStaticMarkup(<Markdown text={text} />)

assert.equal(
  html('매출액은 **333,605,938 백만원**입니다.'),
  '<p>매출액은 <strong>333,605,938 백만원</strong>입니다.</p>',
)
assert.equal(html('a\n\nb'), '<p>a</p><p>b</p>')
assert.equal(html('- 하나\n- 둘'), '<ul><li>하나</li><li>둘</li></ul>')
assert.equal(html('앞\n- 항목\n뒤'), '<p>앞</p><ul><li>항목</li></ul><p>뒤</p>')
assert.equal(html('`code` 와 **굵게**'), '<p><code>code</code> 와 <strong>굵게</strong></p>')
assert.equal(html('별표 * 하나는 그대로'), '<p>별표 * 하나는 그대로</p>')
assert.equal(html(''), '')

// ── 세션 그룹화 ──────────────────────────────────────
const now = new Date('2026-08-28T15:00:00+09:00').getTime()
const DAY = 86_400_000
const at = (t: number, id: string): Session => ({ id, title: id, updatedAt: t, turns: [] })

const groups = groupSessions(
  [
    at(now - 30 * DAY, '오래됨'),
    at(now - 3 * DAY, '이번주'),
    at(now - 60_000, '방금'),
    at(now - 20 * 60 * 60 * 1000, '어젯밤'),
    at(now - 120_000, '조금전'),
  ],
  now,
)

assert.deepEqual(
  groups.map((g) => [g.label, g.sessions.map((s) => s.id)]),
  [
    ['오늘', ['방금', '조금전']],
    ['어제', ['어젯밤']],
    ['지난 7일', ['이번주']],
    ['이전', ['오래됨']],
  ],
)
assert.deepEqual(groupSessions([], now), [])


// ── Inspector 파서 ───────────────────────────────────
// 실제 백엔드(2호기) 응답에서 그대로 가져온 문자열이다.
const CTX = [
  "[1] get_financials(consolidated=True, corp='삼성전자', year=2025)",
  '    출처   사업보고서 (2025.12), 접수번호 20260310002820',
  '    기준   삼성전자 2025 · 연결 · 사업보고서 (2025.12) · 단위 백만원',
  '    항목 (단위 백만원)         2023         2024         2025',
  '    ─────────────────────────────────────────────────────────',
  '    · 손익계산서',
  '      매출액 (주30)    258,935,494  300,870,903  333,605,938',
  '    (28개 항목 중 답변이 인용한 1개만 표시 — 나머지는 think_trace의 도구 관측에 있습니다)',
  '    XBRL 태그 (ifrs-full 접두 생략)',
  '      매출액 (주30)=Revenue',
].join('\n')

const ev = parseEvidence(CTX)
assert.equal(ev?.length, 1)
assert.equal(ev![0].no, '1')
assert.equal(ev![0].tool, 'get_financials')
assert.equal(ev![0].args, "consolidated=True, corp='삼성전자', year=2025")
assert.equal(ev![0].source, '사업보고서 (2025.12)')
assert.equal(ev![0].receipt, '20260310002820')
assert.equal(ev![0].basis, '삼성전자 2025 · 연결 · 사업보고서 (2025.12) · 단위 백만원')
assert.equal(ev![0].xbrl, '매출액 (주30)=Revenue')
assert.match(ev![0].note!, /^28개 항목 중/)
// 표는 공백 그대로 — 공통 들여쓰기만 벗겨서 열 정렬이 유지된다.
assert.match(ev![0].data, /^항목 \(단위 백만원\) {9}2023/)
assert.match(ev![0].data, /\n {2}매출액 \(주30\) {4}258,935,494/)

// 모르는 모양 → null → 호출부가 원문을 그대로 보여준다.
assert.equal(parseEvidence('(도구 호출 없음)'), null)
assert.equal(parseEvidence(''), null)

// 두 블록 · 메타 없는 원문 JSON 관측도 통째로 보존한다.
const two = parseEvidence("[1] a(x=1)\n    rows   []\n\n[2] find_tables(q='특허')\n[\n {\"section\": \"II\"}\n]")
assert.equal(two?.length, 2)
assert.equal(two![1].tool, 'find_tables')
assert.equal(two![1].data, '[\n {"section": "II"}\n]')

const TRACE = [
  '[route=pao_loop stop=answered(정상) steps=2 llm=2 tokens=6938 8.94s]',
  '── 질의 해석 ─────────────────────────────────────────',
  '질의: 삼성전자의 2025년 연결기준 매출액은 얼마인가?',
  '요건: 단위 표기, 연결/별도 명시',
  '',
  '── 근거 수집 ─────────────────────────────────────────',
  "스텝 1: get_financials(consolidated=True, corp='삼성전자', year=2025)",
  '        출처 사업보고서 (2025.12), 접수번호 20260310002820',
  '        획득 assets_total 566,942,110 백만원',
  '스텝 2: 도구 호출 없음 → 직접 답변',
  '',
  '── 검증 ────────────────────────────────────────────',
  '근거가드: 통과 (클레임 3건 전부 지지)',
  '',
  '── 감사 로그 ────────────────────────────────────────',
  '[loop] 삼성전자의 2025년 연결기준 매출액은 얼마인가?',
].join('\n')

const tr = parseTrace(TRACE)
assert.deepEqual(tr!.meta, {
  route: 'pao_loop',
  stop: 'answered',
  stopNote: '정상',
  steps: '2',
  llm: '2',
  tokens: '6938',
  seconds: '8.94',
})
assert.deepEqual(tr!.sections.map((s) => s.name), ['질의 해석', '근거 수집', '검증', '감사 로그'])
assert.deepEqual(tr!.sections[0].items.map((i) => i.head), [
  '질의: 삼성전자의 2025년 연결기준 매출액은 얼마인가?',
  '요건: 단위 표기, 연결/별도 명시',
])
// `스텝 N:` 은 번호를 떼고, 들여쓴 줄은 그 스텝의 상세로 붙는다.
assert.equal(tr!.sections[1].items.length, 2)
assert.equal(tr!.sections[1].items[0].step, '1')
assert.equal(tr!.sections[1].items[0].head, "get_financials(consolidated=True, corp='삼성전자', year=2025)")
assert.match(tr!.sections[1].items[0].detail, /^출처 사업보고서/)
assert.equal(tr!.sections[1].items[1].step, '2')
assert.equal(tr!.sections[2].items[0].mark, 'ok')
// 원문은 어느 섹션에서도 그대로 남는다.
assert.equal(tr!.sections[2].raw, '근거가드: 통과 (클레임 3건 전부 지지)')

// 차단 · 괄호 안 한글 stop
const blocked = parseTrace('[route=pao_loop stop=ungrounded(①설계) steps=2 llm=2 tokens=2507 7.56s]\n'
  + '── 검증 ──────\n근거가드: 차단 — 밸류에이션·전망 생성(규정 위반)')
assert.equal(blocked!.meta!.stop, 'ungrounded')
assert.equal(blocked!.meta!.stopNote, '①설계')
assert.equal(blocked!.sections[0].items[0].mark, 'blocked')

// 섹션 없는 짧은 경로도 헤더만 읽어낸다.
assert.deepEqual(parseTrace('[route=empty_question]')!.meta, { route: 'empty_question' })
const err = parseTrace('[route=error] TypeError: boom')
assert.equal(err!.meta!.route, 'error')
assert.equal(err!.preamble, 'TypeError: boom')
assert.equal(parseTrace(''), null)
assert.equal(parseTrace('아무 구조도 없는 텍스트'), null)

// ── JSON 하이라이트 ─────────────────────────────────
const toks = jsonTokens('{\n  "answer": "값 \\"인용\\"",\n  "n": -1.5e3,\n  "ok": true\n}')
assert.deepEqual(
  toks.filter((t) => t.kind !== 'plain').map((t) => [t.kind, t.text]),
  [
    ['key', '"answer"'],
    ['str', '"값 \\"인용\\""'],
    ['key', '"n"'],
    ['num', '-1.5e3'],
    ['key', '"ok"'],
    ['lit', 'true'],
  ],
)
// 붙였다 떼면 원문과 같아야 한다 — 색칠이 글자를 잃지 않는다.
const round = JSON.stringify({ question_id: 'Q-1', answer: '매출액 333,605,938 백만원\n다음 줄' }, null, 2)
assert.equal(jsonTokens(round).map((t) => t.text).join(''), round)
assert.deepEqual(jsonTokens(''), [])

console.log('Markdown ok · sessions ok · inspect ok')
