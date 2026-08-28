/** Markdown 렌더러 자체 점검: `npm run check` */
import assert from 'node:assert'
import { renderToStaticMarkup } from 'react-dom/server'
import { Markdown } from './Markdown'

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

console.log('Markdown ok')
