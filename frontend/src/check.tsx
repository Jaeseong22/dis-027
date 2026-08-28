/** 자체 점검: `npm run check` — Markdown 렌더러와 세션 그룹화. */
import assert from 'node:assert'
import { renderToStaticMarkup } from 'react-dom/server'
import { Markdown } from './Markdown'
import { groupSessions } from './sessions'
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

console.log('Markdown ok · sessions ok')
