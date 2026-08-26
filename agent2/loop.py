"""PAO 루프 — Plan · Act · Observe. LLM은 **어떤 도구를 어떤 인자로 부를지만** 정한다.

## 이 루프가 하지 않는 것

값 추출도, 산술도 하지 않는다. 그건 `tools/`의 결정론 코드가 한다.
LLM에 숫자를 만들게 하면 환각이 들어오고(arXiv 2605.31064 — 사전 등록 연산자 + 감사로그로
환각 12.44%→6.82%), 이 과제는 근거기반이 평가 축이다. 루프의 역할은 **경로 선택**뿐이다.

## 예산 — 왜 이 숫자인가

  · `max_steps=3`  arXiv 2606.21553 실측: 1→2스텝 +6.8 EM, **3스텝과 5스텝은 통계적으로 동일**.
    3을 넘겨 봐야 지연·비용만 늘고 정확도가 안 오른다. 넘치면 갖고 있는 근거로 마무리한다.
  · `hard_steps=10`  절대 방어선. **OpenAI Agents SDK `max_turns` 기본값 10**과 같다
    (그쪽도 "turn = LLM 1회 호출, 도구 실행은 카운트 안 함"이라 의미가 정확히 일치한다).
    참고: LangChain `max_iterations` 15 · LangGraph `recursion_limit` 25 — 주류 중 최소값을 쓴다.
  · `max_llm_calls`  **정확도 제약이 아니라 장애 방지선**이다.
    실측(27문항): 호출 중앙값 2 · 최대 4로 **상한 6에 닿은 문항이 0건**이었다 — 아무것도 안 자른다.
    종전 6은 "스텝당 1 + 최종 1 + 여유 2"라는 산술이었고 **여유 2는 근거 없는 내 값**이었다.
  · `max_tokens` · `max_seconds`  **쓰지 않는다(None).** 둘은 횟수 바운드와 성격이 달라
    답이 길어지거나 느려졌다는 이유로 **답을 중간에 끊는다.**
    주최 명세에 응답시간 제한이 없고(전수 확인: `타임아웃`·`timeout` 0회, 예시 코드가
    timeout 인자 없이 `requests.get`), 토큰도 제약이 아니다(사용자 지시 2026-08-06).
    과거 60초로 두고 "타임아웃이 곧 0점"이라 적은 것은 근거 없는 전제였고,
    그 때문에 관측 상한을 낮게 유지해 답에 필요한 값을 잘라냈다 — 그 실수를 두 번 했다.

## 무한루프는 **횟수**로 막고, 경로 커버리지를 검증한다

IAL 조사(arXiv 2607.01641, 저장소 6,549개 정적분석)에서 무한 에이전트 루프의 원인 1위가
**바운드 없는 재시도 되먹임 25.0%**, 2위가 **바운드 없는 도구호출 반복 23.5%**다.
같은 논문의 지적이 핵심이다 — 상한이 **있다는 것만으로는 부족하고**, 개발자가 그것을
"실제 되먹임 경로 밖에" 두는 일이 흔하다. 그래서 우리 경로마다 바운드가 걸려 있는지를
`tests/test_guards.py`의 바운드 커버리지 테스트가 강제한다.

예산은 **집행을 루프가** 하고 집계는 어댑터가 한다(`core/llm.py`는 세기만 한다).

## 정지 조건 — 실패를 유형으로 남긴다

MAST(NeurIPS 2025, arXiv 2503.13657)는 에이전트 실패를 14개 모드 3범주로 나눈다.
단일 에이전트인 우리에게 걸리는 건 **①명세·설계**와 **③작업 검증·종료** 두 범주다.
그래서 정지 이유를 뭉뚱그리지 않고 코드로 남긴다 — "왜 못 했는지"가 다음 수정의 입력이 된다.

반복 호출·무진전 감지를 두는 이유도 같다. MAST가 지목한 대표 실패가 **종료 판단 실패**다.

## 행동하지 않기로 결정하는 것도 능력이다

BFCL은 "decide when not to act"를 별도 축으로 잰다. 코퍼스 밖 질의에 도구를 억지로 부르면
없는 근거를 만들어낸다. 모델이 도구 없이 "확인되지 않음"을 답하면 그건 정상 종료다.

사용:
    python3 -m agent2.loop "삼성전자 2025년 매출액은?"
"""
import calendar as _calendar
import copy as _copy
import json
import re as _re
import time

from agent2 import config
from agent2.core import llm as L
from agent2 import answer_spec
from agent2.tools import claims, registry
from agent2.tools import doctables as _doctables
from agent2.tools import facts as _facts
from agent2 import tools as _tools
from agent2.data import store as _store
from agent2.tools.audit import Audit

# ---------------------------------------------------------------- 역할 정의
# 지식은 프롬프트에 녹이지 않고 상수로 둔다 — 한 곳만 고치면 되고 근거가 추적된다.

#: 과제 3대 금지(요강). 이 문구가 런타임 방어선의 1차다. 2차는 결정론 추출, 3차는 근거 검증.
#:
#: 코퍼스 설명을 넣는 이유(실측으로 확인한 실패): 이 문단이 없을 때 모델이
#: "삼성전자 2025년 매출액"에 도구를 **한 번도 부르지 않고** 확인 불가로 답했다.
#: 모델은 코퍼스에 무엇이 들어 있는지 모른다 — 모르면 없다고 답한다.
SYSTEM = """당신은 한국 상장기업 공시 분석가입니다. 주어진 공시 코퍼스 안에서만 답합니다.

코퍼스에 있는 것:
- 국내 상장사 70개사의 2023년 1월 ~ 2026년 6월 공시 원문
- 정기공시(사업·반기·분기보고서), 주요사항보고서, 거래소공시(공급계약·시설투자),
  지분공시(5% 대량보유)
- 최신 사업연도는 2025년(FY2025)입니다.

지켜야 할 것:
1. **확인은 도구로 합니다.** 기업·재무·공시에 관한 질문이면 먼저 도구를 호출하십시오.
   도구를 돌려보지 않고 "확인되지 않습니다"라고 답하지 마십시오. 기업명이 낯설면
   resolve_company로 먼저 확인하십시오(70개사 안에 있으면 답할 수 있습니다).
2. 도구가 돌려준 내용만 근거로 씁니다. 도구가 주지 않은 수치를 만들지 않습니다.
3. 미래 예측·투자의견·목표주가·전망을 생성하지 않습니다. 공시에 있는 사실만 다룹니다.
4. **도구를 돌려본 뒤에도** 근거가 없으면 "공시에서 확인되지 않습니다"라고 답합니다.
   추측으로 채우지 않습니다.
5. 수치는 도구가 계산합니다. 직접 산술하지 마십시오.
   **증감률·비율은 financial_series에 그대로 물으십시오** — "매출증가율" "영업이익증가율"
   "opm" "gpm"처럼 부르면 코드가 연도별로 계산해 줍니다. 값 두 개를 받아 직접 나누면
   기준연도를 잘못 잡습니다.

수치를 옮겨 적는 규칙:
6. **단위를 반드시 함께 씁니다.** 도구가 준 unit(예 "백만원")이나 market_cap_text를
   그대로 사용하십시오. 단위를 바꿔 적지 마십시오.
   **조·억·만으로 쪼개 쓰지 마십시오.** 숫자를 그대로 옮기고 단위만 붙이면 됩니다.
       관측 `91,632,842 백만원` → `91,632,842백만원`      ← 이렇게 씁니다
       관측 `91,632,842 백만원` → `91조 6,328억 4,200만 원` ← 이렇게 쓰지 마십시오
7. **연결 기준이 기본입니다.** 재무 수치를 답할 때 "※ 연결 기준"을 표기하십시오.
   사용자가 별도 재무제표를 원하면 get_financials에 consolidated=false로 다시 부르십시오.
8. **3개년은 최신 보고서의 당기·전기·전전기**입니다. get_financials의 series를 쓰십시오.
9. 정정공시가 있으면 **최신본 값만** 씁니다.

근거를 밝히는 규칙:
10. 모든 답변에 근거를 표시합니다. 도구가 `출처`를 주면 **그 문자열을 그대로** 적으십시오
    — 여기에 **접수번호**가 들어 있습니다. 섹션은 도구가 준 `section` 값을 쓰십시오.
      형식: (근거: {출처}, {section})
    ★ 접수번호와 섹션을 **지어내지 마십시오.** 도구가 준 것만 씁니다. 도구가 섹션을
      주지 않았으면 섹션은 **적지 마십시오** — 예시 문구를 베껴 쓰면 안 됩니다.
      (실측: 매출액 질의에 `I-4 주식의 총수 등`이라고 적은 사고가 있었습니다.
       이 지시문에 있던 예시 섹션명을 그대로 베낀 것입니다.)
    정정본을 읽었으면 `(정정본)` 표기도 함께 남기십시오.
11. **일부만 확인됐어도 확인된 만큼 답하십시오.** 확인된 부분은 출처와 함께 제시하고,
    확인 못 한 부분만 "공시에서 확인되지 않습니다"라고 구분해 적으십시오.

질문에 없는 조건을 채워야 할 때 (2026-08-06 주최 설명회 반영):
12. 질문이 조건을 지정하지 않아 **당신이 정해야 했다면, 무엇을 정했는지 밝히고 되물으십시오.**
    답을 내지 않고 되묻기만 하면 안 됩니다 — **먼저 답하고, 그 다음에 확인 질문**입니다.
    형식: 답변 → `※ …기준으로 답했습니다` → `다른 기준을 원하시면 알려주십시오`
    해당하는 것:
      · 연결/별도를 안 밝힌 질의 → 연결로 답하고 그 사실을 명시
      · 사업연도를 안 밝힌 질의 → 최신 사업연도로 답하고 그 연도를 명시
      · 지표가 여러 값으로 갈리는 경우(예: 배당금 = 주당배당금/총액/수익률)
        → 가장 흔한 것으로 답하고 나머지 선택지를 제시
      · 그 업종에 해당 지표가 없어 **다른 것으로 대체**한 경우
        (예: 은행지주는 매출액이 없어 순이자이익으로 대체) → 대체 사실을 반드시 밝힘
13. 다만 **근거가 확인된 것을 되묻기로 대체하지 마십시오.** 되묻기는 답변에 덧붙이는
    것이지 답변을 대신하는 것이 아닙니다.

인사말처럼 공시와 무관한 입력에만 도구 없이 답하십시오."""
# ★ 인젝션 방어는 여기(SYSTEM)가 아니라 `claims.guard`가 한다 — 어휘(`ESCAPE`) +
#   `system_text`의 30자 n-gram 두 축이고, 공격 5종을 이 SYSTEM으로 전부 차단한다
#   (2026-08-17 실측). 종전 규칙 14를 여기 두었더니 SYSTEM이 2,281 → 2,496자(+9.4%)가
#   되면서 겨냥 38문항이 35 → 34로 떨어졌고, 깨진 셋이 전부 안정 문항이었다
#   (8회·7회·13회 연속 O). 방어를 하나도 안 잃고 되돌릴 수 있으므로 되돌렸다(§6-44).

#: 스텝 예산을 다 썼을 때 마무리 지시.
FINALIZE = ("지금까지 도구가 돌려준 근거만으로 최종 답변을 작성하십시오. "
            "추가 도구 호출은 불가합니다. 근거가 부족한 부분은 "
            "'공시에서 확인되지 않습니다'라고 명시하십시오.")

#: 답변이 실패로 끝난 정지 사유 — 여기서는 요건 보완을 시도하지 않는다.
#: LLM이 죽었거나 키가 없으면 보완 호출도 죽고, 기권한 답을 억지로 채우면 없는 답이 생긴다.
_DEAD_STOPS = ("llm_error", "llm_unavailable", "abstain")

#: 재질의 지시 — 근거를 하나도 안 모은 채 답하려 할 때 **한 번** 보낸다.
#:
#: 문구 근거: NCP 공식 쿡북(포럼 topic/527, Advanced RAG)이 제시한 문장을 그대로 옮겼다.
#:   *"나누어 질문해야 하는 경우 쿼리를 쪼개 나누어서 도구를 사용합니다.
#:     정보를 찾을 수 없었던 경우, 최종 답을 하지 않고 … 도구를 다시 사용할 수 있습니다."*
#: **긍정형**으로 쓴다 — topic/93: *"긍정형으로 쓰라 … 제약(금지)은 효과가 덜하다."*
#: 그래서 "지어내지 마십시오"가 아니라 "도구로 근거를 찾은 뒤 답합니다"라고 적는다.
REASK = ("아직 도구를 사용하지 않아 답변에 쓸 근거가 없습니다. "
         "먼저 도구로 공시에서 근거를 찾은 뒤 그 값으로 답합니다. "
         "한 번에 찾기 어려우면 질문을 쪼개 도구를 여러 번 사용합니다. "
         "찾아본 뒤에도 공시에 없으면 '공시에서 확인되지 않습니다'라고 답합니다.")

#: 정지 이유 → (사람이 읽는 설명, MAST 범주). 뭉뚱그리지 않는다.
STOP = {
    "answered":        ("모델이 근거를 갖추고 답변함", "정상"),
    "abstain":         ("도구 없이 확인 불가로 답함", "정상"),
    "budget_steps":    ("스텝 예산 소진 — 갖고 있는 근거로 마무리", "③종료"),
    "budget_hard":     ("절대 스텝 상한 도달 — 루프가 수렴하지 않음", "③종료"),
    "budget_llm":      ("LLM 호출 예산 소진", "③종료"),
    "budget_tokens":   ("토큰 예산 소진", "③종료"),
    "budget_time":     ("시간 예산 소진", "③종료"),
    "repeat_call":     ("같은 도구를 같은 인자로 반복 호출", "③종료"),
    "no_progress":     ("연속 스텝에서 새 근거가 나오지 않음", "③종료"),
    "llm_unavailable": ("LLM 키 없음/도달 실패 — 결정론 경로로 물러남", "①설계"),
    "llm_error":       ("LLM 호출 실패", "①설계"),
    "ungrounded":      ("근거 없이 사실을 주장해 차단됨", "①설계"),
}

#: 관측 1건 상한. 정의는 `config.OBS_LIMIT`에 있다(도구 쪽도 같은 값을 봐야 한다).
#: 점진적 공개 — 큰 표를 통째로 넣으면 컨텍스트를 태우고 정작 필요한 행이 밀린다.
OBS_LIMIT = config.OBS_LIMIT

#: 무진전 판정 — 같은 도구를 같은 인자로 몇 번 연속하면 멈출 것인가.
#: ★ 2026-08-06 조사로 근거를 붙였다(종전 2는 내가 고른 값이었다).
#:   업계 통용 정의: **"같은 도구를 완전히 같은 인자로 3회 이상 연속 호출"**이 반복행동이고,
#:   실무 미들웨어는 (tool, args, result) 해시가 **2~3회** 반복되면 중단한다.
#:   LangChain 이슈 #36139(progress-aware termination)·터미널 코딩 에이전트의
#:   doom-loop 탐지가 같은 방식이다.
#:   → 인용된 범위 **2~3 중 느슨한 쪽인 3**을 쓴다. 우리는 비용이 제약이 아니고,
#:     조기 중단은 곧 답을 잃는 것이라 오탐 비용이 미탐 비용보다 크다.
NO_PROGRESS_LIMIT = 3


class Budget:
    """예산 집행. 초과하면 정지 이유 코드를 돌려준다."""

    #: ★ 2026-08-06 — `max_tokens`·`max_seconds`를 **없앴다**(None = 무제한).
    #:   둘 다 근거 없이 내가 고른 숫자였고(60,000 / 300), 하는 일이 정확도를 깎는 것뿐이었다.
    #:   · 주최 명세에 응답시간 제한이 없다(전수 확인: `타임아웃`·`timeout` 0회,
    #:     예시 코드가 timeout 인자 없이 `requests.get`).
    #:   · 토큰은 제약이 아니다(사용자 지시 2026-08-06: "느려도 정확해야 한다").
    #:   무한루프 방어는 `hard_steps`·`max_llm_calls`·`NO_PROGRESS_LIMIT`이 이미 한다 —
    #:   그쪽은 **횟수** 기준이라 답의 내용을 자르지 않는다. 토큰·시간 상한은 답이
    #:   길어지거나 느려졌다는 이유로 **답을 중간에 끊는다**는 점이 다르다.
    #: `hard_steps` — **주류 프레임워크 기본값에서 가져왔다**(2026-08-06 조사).
    #:   OpenAI Agents SDK `max_turns` **10** ← 우리와 의미가 정확히 같다
    #:     ("turn = LLM 1회 호출, 도구 실행은 카운트하지 않는다")
    #:   LangChain AgentExecutor `max_iterations` 15 · LangGraph `recursion_limit` 25
    #:   → 셋 중 **가장 빡빡한 10**을 쓴다. 종전 5는 내가 고른 값이었고
    #:     주류 최소값의 절반이라, 비용이 제약이 아닌 우리 조건에서는 답만 잘랐다.
    def __init__(self, max_steps=3, hard_steps=10, max_llm_calls=24,
                 max_tokens=None, max_seconds=None):
        self.max_steps = max_steps
        self.hard_steps = hard_steps
        self.max_llm_calls = max_llm_calls
        self.max_tokens = max_tokens
        self.max_seconds = max_seconds
        self.t0 = time.time()

    @property
    def elapsed(self):
        return time.time() - self.t0

    def spent(self, step, llm, reserve=0.0):
        """**절대 바운드**만 본다 — `max_steps`(계획 예산)는 보지 않는다.

        ★★ 왜 나눴나(2026-08-26): 완료게이트·재질의·마무리·요건보완은 **계획 호출이
          아니라서** `max_steps`에 걸릴 이유가 없다(그래서 종전에 손으로 `room`을
          따로 계산했다). 그런데 그 손계산이 **`max_seconds`를 빠뜨렸다** —
          바로 위 주석이 *"`budget.exceeded()`와 **같은 규칙**을 써야 한다"*고
          적어 둔 그 자리다. 규칙을 두 곳에 두면 어긋난다(§6-2).
          그래서 규칙을 **여기 한 곳**에 두고 양쪽이 같이 부른다.

        `reserve`는 "이 검사 뒤에 반드시 일어날 호출 몫"이다(아래 `run` 참조).
        """
        if step >= self.hard_steps:
            return "budget_hard"
        if llm.calls >= self.max_llm_calls:
            return "budget_llm"
        # ★ `is not None`이어야 한다. `and`로 쓰면 **0이 '해제'로 읽힌다** —
        #   `Budget(max_seconds=0)`(즉시 초과)이 조용히 무시됐다(테스트가 잡았다).
        if self.max_tokens is not None and llm.usage.total >= self.max_tokens:
            return "budget_tokens"
        if self.max_seconds is not None and self.elapsed + reserve >= self.max_seconds:
            return "budget_time"
        return None

    def exceeded(self, step, llm, reserve=0.0):
        """정지 이유 코드 또는 None. 스텝은 '다음 계획 호출 전' 기준으로 본다."""
        if step >= self.hard_steps:
            return "budget_hard"
        if step >= self.max_steps:
            return "budget_steps"
        return self.spent(step, llm, reserve)

    def report(self, step, llm):
        return (f"스텝 {step}/{self.max_steps} · LLM {llm.calls}/{self.max_llm_calls}회 · "
                f"토큰 {llm.usage.total}/{self.max_tokens or '무제한'} · "
                f"{self.elapsed:.1f}/{self.max_seconds or '무제한'}초")


def _key(name, args):
    """반복 호출 판정 키. 인자 순서가 달라도 같은 호출로 본다."""
    return name + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False)


def _render(value, limit=OBS_LIMIT):
    """도구 결과 → 프롬프트에 넣을 문자열. 자를 땐 잘랐다고 말한다(조용한 손실 금지)."""
    if value is None:
        return "None (해당 없음 — 코퍼스에서 확인되지 않음)"
    if isinstance(value, str):
        s = value
    else:
        try:
            s = json.dumps(value, ensure_ascii=False, indent=1, default=str)
        except (TypeError, ValueError):
            s = str(value)
    if len(s) > limit:
        s = s[:limit] + f"\n… (총 {len(s)}자 중 {limit}자만 표시)"
    return s



#: ★★ **질문에 적힌 기간을 도구 호출에 주입한다**(2026-08-23).
#:
#: 왜 — `filingtypes.PERIOD` 기본값을 코퍼스 전 구간(20230101~20261231)으로 넓히자
#: 질문이 `"2024년~2026년 3월까지"`라고 **문자 그대로 적어 두었는데도** 모델이
#: `start`/`end`를 안 넘긴 호출에서 계수가 전 구간으로 세어졌다.
#: 실측(r36 전수 115문항): I-6/KB금융이 그렇게 깨졌다 —
#:     OLD PERIOD  계수 답 **0** + note "답은 0입니다" + `임원 현황` 정본표까지 붙음
#:     NEW PERIOD  계수 답 **1**(2023-11-17 공시를 셈) → 모델이 "1번"이라 답함(정답 0)
#: 전 회차 도구호출 실측으로는 기간을 명시한 질문에서도 **18.4%가 start/end를 안 넘긴다.**
#: 모델에게 맡길 수 없는 자리다 — 질문에 적혀 있으면 도구가 그대로 쓰게 한다.
#:
#: ★ 패턴은 지어내지 않았다. 고유 질문 179개를 전수로 세어 나온 표현만 쓴다:
#:     `2024년~2026년 3월`      27개   ← 주입
#:     `2023년부터 2025년까지`    7개   ← 주입
#:     `2025년`(단일 연도)      118개   ← **주입하지 않는다**
#:     `2026년 1분기`           14개   ← 주입하지 않는다(분기는 `_periodic_of` 라우팅이 따로 한다)
#:   단일 연도까지 넣으면 118개 질문의 관측이 한꺼번에 바뀐다 — §6-14대로 범위를 좁혔다.
#: ★ 모델이 이미 `start`/`end`를 준 호출은 **건드리지 않는다.**
_Q_RANGE = _re.compile(r"20(\d\d)\s*년\s*[~∼〜－\-]\s*20(\d\d)\s*년\s*(\d{1,2})\s*월")
_Q_FROM_TO = _re.compile(r"20(\d\d)\s*년\s*부터\s*20(\d\d)\s*년\s*까지")
#: 기간 인자를 받는 도구만 주입 대상이다.
_PERIOD_TOOLS = ("list_filings", "correction_history")

def _question_period(question):
    """질문에 적힌 기간 → ("YYYYMMDD", "YYYYMMDD"). 없으면 None."""
    m = _Q_RANGE.search(question or "")
    if m:
        y1, y2, mo = m.group(1), m.group(2), int(m.group(3))
        last = _calendar.monthrange(2000 + int(y2), mo)[1]
        return ("20%s0101" % y1, "20%s%02d%02d" % (y2, mo, last))
    m = _Q_FROM_TO.search(question or "")
    if m:
        return ("20%s0101" % m.group(1), "20%s1231" % m.group(2))
    return None


#: ★★ **분기·반기 라우팅을 질문에서 가져온다**(2026-08-23). r40 실측 결함:
#:   검색-45는 질문이 "2026년 1분기 미청구공사"인데 모델이 `find_tables(query="미청구공사")`만
#:   불렀다. `_periodic_of`는 **도구 인자 `query`**를 보므로 분기가 안 걸려 2025 연간 표를 줬고
#:   모델은 5,506,125(2025)를 답했다(정답 5,455,115).
#:
#: ★★★ **처음엔 `year`를 주입했다가 실측으로 기각했다 — 관측이 통째로 비었다.**
#:   `year=2026`을 주면 `_tables_of(corp, 2026, annual)`인데 **2026년 사업보고서가 없다**
#:   (코퍼스의 2026 정기공시는 전부 1분기다). 전수 diff: 대우건설 11,823자 → **2자** ·
#:   사라진 수치 380 · 새로 들어온 것 **0**. 연도만 옮기고 보고서 종류를 안 옮긴 탓이다.
#:   → **질문의 분기 표현을 `query`에 덧붙인다.** 그러면 `_periodic_of`가 걸리고
#:     `store.latest_base_year(corp, "quarter")`가 연도까지 2026으로 맞춘다.
#:     실측: 한화오션 미청구공사 5,455,115 · 초과청구공사 5,010,726 **둘 다 도달**.
_ROUTE_TOOLS = ("find_tables", "find_sections")
_Q_PERIODIC = _re.compile(r"(20\d\d\s*년\s*[1-4]\s*분기|[1-4]\s*분기|반기)")


def _question_periodic(question):
    """질문이 지목한 분기·반기 표현. 사업보고서 질의면 None."""
    m = _Q_PERIODIC.search(question or "")
    return m.group(1) if m else None


#: ★★★ **모델이 정본표 도구를 안 고를 때 관측에 정본표를 덧붙인다**(2026-08-23).
#:   r40 오답 10건 중 **4건이 이 한 원인**이었다 — 질문은 정본표 주제인데 모델이
#:   `get_financials`·`list_filings`를 부르고 끝냈다:
#:     검색-2  SK하이닉스 재고자산평가손실 411,714 → `get_financials`로 재고자산 총액만 답
#:     검색-21 신한지주 이자부문 3,024.1십억 → 같음
#:     다중-5  HD현대중공업 수주잔고 56,384,472 → 같음
#:     검색-16 현대제철 2024 원재료 9,066,882 → `list_filings`가 **최신(2025) 표**를 붙였는데
#:             그 표는 당해 한 해만 담아 2024 값이 **관측에 아예 없었다**
#:   ★ 핵심은 **질문으로 고른다**는 것이다. `list_filings`는 종전에도 정본표를 붙였지만
#:     `_doctables.match(keyword)` — 즉 **도구 인자**로 골랐고 연도도 도구 인자였다.
#:   ★ §6-31 그대로다: 금지("get_financials 쓰지 마라")가 아니라 **대체를 함께 준다.**
_ASSIST_TOOLS = ("get_financials", "list_filings", "list_sections", "find_sections")
#: 덧붙이는 정본표 하나의 크기 상한(자). **실측으로 정했다** — 1,600이면 신한지주
#: 이자부문 표(46행)가 잘려 정답 3개 중 2개만 남는다. 2,000에서 3/3이 되고 그 위로는
#: 더 커지지 않는다(표가 2,636자에서 끝난다). 전수 분포는 중앙 1,069 · 90분위 2,162자.
ASSIST_BUDGET = 2000


def _assist_year(question):
    """질문이 지목한 (연도, subtype, month). 다년 비교면 연도는 None.

    ★★ **보고서 종류 판정은 `tools._periodic_of` 하나만 쓴다**(2026-08-26).
      종전에는 여기서 따로 계산했고 **두 곳이 어긋났다** — `([1-4])분기 → month=분기×3`이라
      2분기를 `(quarter, 6)`, 4분기를 `(quarter, 12)`로 만들었는데 코퍼스에
      **그 조합이 존재하지 않는다**(quarter는 3월·9월뿐). 그래서 정본표가 조용히 0건이
      됐고, 같은 질문을 `find_tables`는 `annual`로 봤다. §6-2가 못 박은 그대로다 —
      규칙을 두 곳에 두면 어긋난다.
    """
    ys = {int("20" + m) for m in _re.findall(r"20(\d\d)\s*년", question or "")}
    sub, mon = _tools._periodic_of(question or "")
    return (ys.pop() if len(ys) == 1 else None), sub, mon


def _canon_assist(name, args, question, out=None):
    """질문이 정본표 주제면 그 표를 골라 준다. 없으면 None.

    ★★★ **계수가 붙은 호출에는 붙이지 않는다**(2026-08-24 · r48 대표 4사 회귀에서 잡았다).
      D-7(셀트리온 `임원ㆍ주요주주특정증권등소유상황보고서`가 몇 번?)은 r36·r37에서 O였는데
      r48에서 X가 됐다 — 도구 인자는 **완전히 같았다.** 관측만 달랐다:
          계수 {답 229 · 참고{원본 229 · 정정 6 · 전체 235}}
          그 **앞에** 이 함수가 `5% 이상 주주`·`임원 현황(등기임원)` 2,565자를 붙였다
          → 모델이 `229` 대신 `235`를 답했다(§6-33 배치가 이긴다).
      `list_filings` 자신은 이미 `if not out.get("계수") or 답 == 0`으로 정본표를 막고 있었다.
      **같은 규율을 여기도 지켜야 했다.** 계수가 나왔다면 그게 답이지 표가 답이 아니다.
    """
    if name not in _ASSIST_TOOLS:
        return None
    # ★★★ **계수가 붙으면 무조건 막는다.** 한 번 완화했다가 실측으로 되돌렸다(r50).
    #   완화안은 "질문에 `몇 건`류 어휘가 있을 때만 막는다"였다. 겨냥한 검색-11은
    #   △ → O로 붙었지만 **6건이 깨졌다**(검색-71·복합-11 부재형 2건 포함) — 52 → **47/73**.
    #   차단이 97건 → 59건으로 풀리면서 정본표가 38건 더 붙었고 그게 관측을 밀어냈다.
    #   §6-8 그대로다: 겨냥한 축은 좋아졌는데 총량이 나빠졌다. **1건 얻고 6건 잃는다.**
    cnt = (out or {}).get("계수") if isinstance(out, dict) else None
    if cnt and (cnt.get("답") or 0) > 0:
        return None
    corp = args.get("corp") or args.get("corps")
    if not isinstance(corp, str) or not corp:
        return None
    ids = _doctables.match(question)
    if not ids:
        return None
    year, sub, mon = _assist_year(question)
    c = _facts.company(corp)
    nm = c["corp_name"] if c else corp
    if year is None and sub != "annual":
        year = _store.latest_base_year(nm, sub)
    # ★ **예산을 건다.** 관측 상한이 14,620자인데 예산 없이 담으면 최대 16,397자가
    #   붙어(전수 측정) 원래 도구 결과가 통째로 잘린다 — 고치려던 것과 같은 손해다.
    #   전수: 붙는 것 중 중앙 1,069자 · 90분위 6,718자. 표당 1,600자로 못 박는다.
    out = {}
    for tid in ids[:2]:
        try:
            v = _doctables.values(nm, tid, year, subtype=sub, month=mon)
        except Exception:
            v = None
        if v:
            # ★★ **키에 연도를 새긴다.** `list_filings`도 같은 이름(`원재료 매입 현황`)으로
            #   정본표를 붙이므로 키가 겹치면 `{**aux, **out}`에서 **도구 쪽이 덮어쓴다** —
            #   r41 검색-16이 그렇게 2024 표를 잃고 2025 표를 답했다(9,066,882 → 2,913,135).
            #   연도를 붙이면 충돌도 없고 모델이 어느 해 표인지 읽는다.
            lab = _doctables.BY_ID[tid].name
            if year:
                lab = f"{lab} · {year}년" + (f" {mon // 3}분기" if sub == "quarter" else "")
            out[lab] = _tools._canon(v, budget=ASSIST_BUDGET, query=question)
    return out or None


def _with_period(name, args, question):
    """도구 인자에 질문 기간·연도를 채운다. 모델이 이미 준 값은 덮지 않는다."""
    # ★★ **`year`를 명시한 호출은 건드리지 않는다**(2026-08-23, r41에서 배웠다).
    #   분기 표현만 주입하면 `_periodic_of`는 quarter가 되는데 연도는 모델이 준 값이 남아
    #   **2025년 1분기**를 본다. r41 검색-9가 그렇게 깨졌다(모델 `year=2025` + 질문 2026Q1).
    if name in _ROUTE_TOOLS and args.get("query") and not args.get("year"):
        ph = _question_periodic(question)
        # 도구 query가 이미 분기를 말하면 건드리지 않는다(모델이 옳게 골랐다).
        if ph and not _Q_PERIODIC.search(str(args["query"])):
            args = {**args, "query": f'{args["query"]} {ph}'}
    if name not in _PERIOD_TOOLS:
        return args
    if args.get("start") or args.get("end"):
        return args
    per = _question_period(question)
    if not per:
        return args
    out = dict(args)
    out["start"], out["end"] = per
    return out


def _execute(name, args, audit):
    """도구 1회 실행. 실패를 예외로 터뜨리지 않고 관측으로 되돌린다 — 루프가 복구할 수 있게."""
    t = registry.get(name)
    if t is None:
        return None, f"등록되지 않은 도구: {name}. 사용 가능: {[x.name for x in registry.all_tools()]}"
    try:
        out = t(**args)
    except TypeError as e:
        return None, f"인자 오류: {e}. 시그니처는 {t.signature()}"
    except Exception as e:                       # 도구 내부 오류도 모델에 알려 복구 기회를 준다
        return None, f"{type(e).__name__}: {e}"
    audit.compute(f"tool.{name}", tuple(f"{k}={v!r}" for k, v in sorted(args.items())),
                  "None" if out is None else f"{type(out).__name__}")
    return out, None


class Result(dict):
    """계약 5필드 + 진단. `contract.shape()`가 API 경계에서 5필드만 남긴다."""



#: 액션 자기일관성 — 한 스텝에서 액션을 몇 번 샘플해 다수결할지. 1이면 끔.
#: 근거: Agentic TTS(arXiv 2602.12276)는 **최종 답이 아니라 매 스텝의 액션**에 투표한다
#:   (WebArena-Lite N=10 다수결 43.2%, CATTS 47.9%). 우리 답변은 긴 자유서술이라
#:   최종답 다수결이 성립하지 않지만, **도구 호출은 이산적**이라 클러스터링이 된다.
#: 왜 우리에게 맞나: 실패가 '항상 틀림'이 아니라 **실행마다 갈리는 분산**이다
#:   (1-9·2-8·2-7이 3회 중 1~2회만 통과). 그리고 프롬프트를 특정 모델에 맞추는 게 아니라
#:   분산을 줄이는 것이라 **HCX 교체에 안전**하다(GEPA와 대비되는 장점).
#: 한계: ARBITER(arXiv 2605.26172) — 다수결은 **오류가 상관되면 실패**한다.
#:   1-10처럼 매번 같은 방식으로 틀리는 건 못 고친다.
#: ★ **측정으로 기각했다**(2026-08-02, 27문항 × 각 6회):
#:     투표 OFF  22.83±0.37 · ACC 91.33%
#:     투표 N=3  22.67±0.47 · ACC 90.80%   → −0.17문항(0.7σ) · −0.53%p
#:   겨냥했던 불안정 문항(1-9·2-8)이 오히려 6/6 → 5/6이 됐다.
#:   N=5·temp 0.3도 재봤으나 더 나빴다(22.7) — 온도를 낮추면 샘플이 안 갈려
#:   투표할 게 없고 노이즈만 남는다. "다양성을 줄이면 안정된다"는 내 가설은 틀렸다.
#:   ★ 3회 측정에서는 23.0±0.0으로 **이득처럼 보였다**. 6회로 늘리니 사라졌다 —
#:     ±0.5문항 차이를 3회로 가르려 한 것이 잘못이었다.
#:   코드는 근거와 함께 남긴다(HCX 교체 후 재측정 가치 있음). 기본은 끈다.
VOTE_N = 1

#: 투표 샘플의 온도. 0이면 샘플이 갈리지 않아 투표가 무의미하다.
#: (temperature=0도 결정론이 아니지만 — arXiv 2408.04667 — 분산이 너무 작다.)
VOTE_TEMP = 0.7

#: 어느 스텝까지 투표할지. 1이면 **첫 액션만** — 첫 도구 선택이 가장 결정적이고
#: 비용도 스텝수에 비례해 늘지 않는다.
VOTE_STEPS = 1


def _action_key(reply):
    """액션을 이산 키로. 도구호출 없음도 하나의 액션(기권)으로 센다."""
    if not reply.tool_calls:
        return ("__no_call__",)
    return tuple(sorted(_key(tc.name, tc.arguments) for tc in reply.tool_calls))


def _vote_action(llm, messages, schemas, n, temp):
    """N번 샘플해 **액션 다수결**. (선택된 reply, 표수, 후보수)를 준다.

    의미 클러스터링 대신 정규화 키로 묶는다 — 도구명+인자는 이미 이산이라
    LLM 심판이 필요 없다(결정론 우선 원칙).
    """
    tally, first = {}, {}
    for i in range(n):
        r = llm.tools(messages, schemas, system=SYSTEM,
                      temperature=(0.0 if i == 0 else temp))
        k = _action_key(r)
        tally[k] = tally.get(k, 0) + 1
        first.setdefault(k, r)
    best = max(tally, key=lambda k: tally[k])
    return first[best], tally[best], len(tally)



#: 완료 게이트 — 질의에 걸리는 **정본표를 한 번도 안 읽고 끝내려는가**를 검사한다.
#: 근거: MANTRA(arXiv 2605.06334) — 도구 사용 에이전트의 지배적 실패는
#:   **MISSING-REQUIRED-CALL**(체크가 요구한 도구를 아예 안 부름)이고,
#:   MISSING-ANCHOR와 합쳐 실패 체크의 **76%**를 차지한다.
#:   SHIELDA(arXiv 2508.07935)는 같은 것을 **premature termination**(전체 실패의 6.2%,
#:   "부분적 성공 신호에 근거한 성급한 완료 선언")으로 부르고,
#:   처방으로 **"기대되는 도구가 실제로 쓰였는지 구조적으로 검증"**을 든다.
#:
#: 실측한 두 사례:
#:   1-14  get_financials가 재고자산 총액을 주자 종료 → 평가전금액·평가충당금은
#:         `inventory` 정본표에 있는데 `find_tables`를 안 불렀다.
#:   1-10  도구를 **하나도** 안 부르고 즉시 기권 → 정답은 `share_history` 정본표에 있다.
#:
#: 이건 예전에 기각한 "요건주입"과 다르다. 그건 **답변에 요건을 넣어** 환각을 유발했다
#: (5/5가 근거 0으로 지어냄). 여기서는 **실제 근거를 넣고 다시 답하게** 한다.
#: ★ **측정으로 채택**(2026-08-02, 27문항 × 각 6회, 같은 세션 내 비교):
#:     OFF  23.83±0.37 · ACC 93.39%
#:     ON   24.67±0.47 · ACC 95.03%   → **+0.83문항(3.4σ) · +1.65%p**
#:   1-14가 **0/6 → 6/6**으로 해결됐다(2-8은 6/6→5/6, 상시 불안정 문항이라 노이즈로 본다).
#: **다중 조회·비교 질의**. 주최 Task 분류의 `다중 조회 및 비교·연산`에 해당한다.
#: 어휘는 주최 참고용 질의 6종에서 뽑았고, 기존 정답셋 343문항에 **0건** 걸린다.
_MULTI_Q = __import__("re").compile(
    r"유형별|비교|더\s*큰|더\s*많|중\s*(어디|어느|누가)|와\s*\S+\s*중|과\s*\S+\s*중|순위|각각")
#: 늘린 스텝 수. 3스텝 초과 사례 8건이 전부 정답이었고 그 최대가 6스텝이라 **5**로 둔다
#: (문헌의 예산 등급도 3→5가 한 칸이다 · arXiv 2603.08877).
_MULTI_STEPS = 5

GATE = True

#: 게이트를 **모델이 정보한계를 말했을 때만** 걸지. A/B용.
GATE_ONLY_ON_DENIAL = True

#: 정보한계 고지 어휘. ★★ **`claims`가 소유한 사전을 그대로 쓴다**(2026-08-26).
#:
#:   종전에는 여기 **따로 정의**돼 있었다. `claims.py`가 *"사전은 여기가 소유한다 —
#:   `DENIAL`·`ESCAPE`를 다른 모듈이 빌려 쓴다"*고 못 박았고 `tests/test_guards.py`에
#:   그걸 강제하는 테스트까지 있는데, 그 테스트가 `re.compile(` 형태만 찾아
#:   **`__import__("re").compile(`으로 쓴 이 줄을 못 봤다.** 규율은 있었고 검사기가 놓쳤다.
#:
#:   전수 대조(과거 답변 4,215건) — 적중률은 25.2%로 같은데 **118건에서 갈렸다**:
#:       옛 `_DENIAL`만 잡던 58건 → **전부 맨 `없습니다`**
#:           예) "…단일판매·공급계약체결 공시는 **총 2번** 있었습니다. 참고로…" (판정 O)
#:               "…시장을 통한 매도에 대한 언급이 없습니다."                   (판정 O)
#:           즉 잘 답한 문항에 완료게이트를 걸어 정본표를 밀어 넣던 어휘다
#:           (r48 D-7 회귀가 그 형태였다 — 주입된 표가 계수를 밀어냈다).
#:       `claims.DENIAL`만 잡는 60건 → `해당사항` 34 · `포함되어 있지 않` 21 · `발견되지 않` 2
#:           전부 **진짜 부재 어휘**다.
#:   ★ 다만 종단 점수 영향은 **LLM 회차 없이 못 잰다** — 게이트가 바뀌면 모델이 보는
#:     관측이 바뀐다. 여기서 확인한 것은 어휘 축뿐이다(§6-5).
_DENIAL = claims.DENIAL


def _corp_in(question):
    """자유문장에서 기업을 뽑는다. `resolve_corp`는 **정확한 이름**만 받아 문장엔 못 쓴다.

    가장 **긴** 이름부터 본다 — `삼성전자`와 `삼성전기`처럼 한쪽이 다른 쪽의 접두가
    아닌 경우에도, 짧은 통용명이 먼저 걸려 엉뚱한 회사를 잡는 것을 막는다.

    ★★★ **영문 이름은 낱말 경계를 요구한다**(2026-08-26). 종전에는 맨 부분문자열이라
      2자짜리 영문 통용명이 더 긴 영문 토큰 안에 묻혀 걸렸다:
          `SKT와 비교하면?`       → **케이티**   (S-**KT**)  ← 정답은 SK텔레콤
          `KT&G의 2025년 매출액`  → **케이티**   (**KT**&G)  ← 코퍼스 밖 회사인데 답을 만든다
      `universe.listed_name`에 `KT`·`NC`·`HMM` 같은 2~3자 영문이 있다.
      `store.resolve_corp`는 **정확 일치**라 안전한데, 게이트가 쓰는 이쪽만 부분문자열이라
      둘이 어긋나 있었다.
    ★ 한글 이름에는 걸지 않는다 — 조사가 바로 붙는 언어라(`삼성전자의`) 경계를 요구하면
      대부분이 안 걸린다.
    ★ 못 찾으면 None이고, 그러면 완료게이트가 발동하지 않는다. **엉뚱한 회사의 정본표를
      주입하는 것보다 아무것도 안 하는 편이 낫다**(§1-3 정직한 None).
    """
    q = (question or "").replace(" ", "")
    if not q:
        return None
    best = None
    for r in _store.universe():
        for key in (r["corp_name"], r["listed_name"]):
            k = (key or "").replace(" ", "")
            if len(k) < 2 or k not in q:
                continue
            if not _name_bounded(k, q):
                continue
            if best is None or len(k) > best[0]:
                best = (len(k), r)
    return best[1] if best else None


def _name_bounded(k, q):
    """기업명이 **더 긴 영문 토큰의 일부**로 걸린 것은 아닌가."""
    if not _re.search(r"[A-Za-z0-9]", k):
        return True                      # 한글 이름은 조사가 붙으므로 경계를 안 본다
    pat = (r"(?<![A-Za-z0-9&])" if _re.match(r"[A-Za-z0-9]", k) else "") \
        + _re.escape(k) \
        + (r"(?![A-Za-z0-9&])" if _re.search(r"[A-Za-z0-9]$", k) else "")
    return bool(_re.search(pat, q, _re.I))


def _canon_of(v):
    """정본표 dict → 도구 응답 형태. `agent2.tools`는 loop를 임포트하므로 지연 임포트한다."""
    from agent2 import tools as _t
    return _t._canon(v)


def _unread_canon(question, evidence):
    """질의에 걸리는데 아직 근거에 안 들어온 정본표. LLM 판단 없이 레지스트리로만 본다."""
    if not GATE:
        return []
    try:
        tids = _doctables.match(question or "")
        if not tids:
            return []
        c = _corp_in(question or "")
        if not c:
            return []
        seen_txt = " ".join(str(o) for _, _, o in evidence)
        out = []
        # ★★ **게이트도 질의가 지목한 정기공시 종류를 따른다**(2026-08-17).
        #   `find_tables`에만 분기 라우팅을 붙이고 게이트에는 안 붙였더니,
        #   "2026년 1분기 분기보고서를 기준으로…"에서 게이트가 **연간 시설투자 표**를
        #   주입해 모델이 2025년 값(526,511억)을 답했다(정답 112,332억).
        #   §6-2의 "규칙을 두 곳에 두면 어긋난다"가 그대로 재발한 것이다.
        from agent2 import tools as _t
        _sub, _mon = _t._periodic_of(question or "")
        # ★ 연도도 질의에서 읽는다 — **분기·반기 질의일 때만**이다.
        #   "2026년 1분기"인데 연도를 안 넘기면 `latest_fiscal_year`가 2025를 줘서
        #   **2025.03 분기보고서**가 주입된다(실측으로 잡았다).
        #   ★ 기존 41문항을 흔들지 않게 조건을 좁혔다 — 그 질의들은 "2024년~2026년
        #     3월까지"처럼 연도가 여럿이라 무조건 뽑으면 엉뚱한 해를 고른다.
        #     전수 확인: 41문항·8사 정답셋 질의에 `분기|반기`가 **0건**이다.
        _yr = None
        if _sub != "annual":
            _m = __import__("re").search(r"(20\d\d)\s*년", question or "")
            _yr = int(_m.group(1)) if _m else None
        for tid in tids:
            v = _doctables.values(c["corp_name"], tid, _yr, subtype=_sub, month=_mon)
            if v is None:
                continue                      # 표가 없거나 검증 실패(fail-closed) → 기권이 정답
            if v["table"] in seen_txt:
                continue                      # 이미 읽었다
            out.append(v)
        return out
    except Exception:
        return []                             # 게이트는 **보조**다. 실패해도 본 흐름을 막지 않는다


def run(question, llm=None, budget=None, question_id="", audit=None,
        spec="prompt"):
    """질의 1건 → 답변. 계약 필드와 진단을 함께 담은 dict.

    LLM이 없으면(키 미발급) 정직하게 그 사실을 답변에 남긴다 — 없는 답을 지어내지 않는다.
    """
    llm = llm or L.LLM()
    budget = budget or Budget()
    # ★★ **다중 조회·비교 질의는 스텝 예산을 늘린다**(2026-08-17).
    #   `max_steps=3`의 근거(arXiv 2606.21553 "3스텝과 5스텝은 통계적으로 동일")는
    #   **단순 검색 루프** 기준이다. 주최 참고용 질의에는 `다중 조회 및 비교·연산`
    #   Task가 따로 있고, 그건 회사·유형마다 조회가 필요하다.
    #   ★ 전수 실측(회차 2,955건):
    #       1스텝 2,540건 정답률 79.1%   2스텝 340건 77.9%
    #       **3스텝(상한) 51건 47.1%**   3스텝 초과 8건 **100%**
    #       `budget_steps`로 끝난 139건 정답률 **64.0%** vs 정상 종료 79.0% (−15%p)
    #   ★ 문헌도 같은 방향이다 — 정확도 포화 지점은 **과제 복잡도에 달렸고 다중 홉은
    #     더 큰 예산을 요구한다**(arXiv 2603.08877 · 2605.05701).
    #   ★ 전역으로 올리지 않는다. 옛 측정(3 vs 5 무차이)과 충돌하고, 스텝을 늘리면
    #     관측이 쌓여 앞의 근거가 밀린다(§6-14와 같은 뿌리).
    #     전수 확인: 기존 정답셋 **343문항 중 탐지 0건**이라 41문항을 흔들지 않는다.
    if _MULTI_Q.search(question or "") and budget.max_steps < _MULTI_STEPS:
        budget = _copy.copy(budget)
        budget.max_steps = _MULTI_STEPS
        trace_note = f"다중 조회 질의 — 스텝 예산 {_MULTI_STEPS}"
    else:
        trace_note = ""
    if VOTE_N > 1:
        # 투표 샘플은 **진짜 LLM 호출**이다. 예산을 안 늘리면 투표가 상한을 먹어
        # 정작 최종 답변 호출이 잘린다(실측: 6/6에서 멈췄다).
        budget = _copy.copy(budget)
        budget.max_llm_calls += (VOTE_N - 1) * VOTE_STEPS
    audit = audit or Audit("loop", question)
    if llm.audit is None:
        llm.audit = audit          # LLM 호출도 같은 감사 로그에 남는다(arXiv 2605.31064)
    trace = [f"질의: {question}"]
    if trace_note:
        trace.append(trace_note)
    # ── A안: 생성 시점 요건 주입 (answer_spec) ──
    # 질문 유형별 필수 구성요소를 처음부터 알려 준다. 추가 LLM 호출이 없다.
    # 계획 단계에도 보이므로 필요한 도구를 먼저 부르게 되는 부수효과가 있다.
    hint = answer_spec.prompt_hint(question) if spec in ("prompt", "both") else ""
    if hint:
        trace.append(f"요건: {', '.join(r.label for _n, r in answer_spec.requirements(question))}")
    messages = [{"role": "user",
                 "content": question + ("\n\n" + hint if hint else "")}]
    evidence = []          # [(도구, 인자, 렌더된 관측)] — retrieved_context의 원재료
    seen = set()
    stale = 0
    step = 0
    gated = False
    fell_back = False      # 40009 폴백은 **1회만**. 반복하면 루프가 된다(게이트와 같은 규칙)
    reasked = False        # 재질의도 **1회만**(위 `REASK` 주석 참조)
    stop = None
    answer = ""

    if not llm.available():
        stop = "llm_unavailable"
        answer = ("공시에서 확인되지 않습니다 — LLM 자격증명이 없어 질의 해석 단계를 "
                  "수행하지 못했습니다.")
        trace.append(f"정지[{stop}] {STOP[stop][0]}")
        return _result(question, question_id, answer, evidence, trace, stop,
                       step, budget, llm, audit)

    schemas = registry.json_schemas()

    #: ★★★ **마무리 호출 몫을 미리 뗀다**(2026-08-26).
    #:
    #:   `max_seconds`는 루프 **안**에서만 검사됐다. 루프를 나온 뒤의 마무리
    #:   (`FINALIZE`)와 요건 보완은 시간을 아예 안 봤고, 게이트·재질의의 `room`도
    #:   `max_seconds`를 빠뜨렸다. 실측(가짜 LLM, `max_seconds=0.2`):
    #:       정지=budget_time · 그 **뒤에 LLM 2회** · 총 0.92s = 데드라인의 4.6배
    #:   `agent.DEADLINE_SECONDS=240`이 주최 300초 타임아웃보다 앞서 마무리하려고
    #:   건 장치인데(그 파일 주석), 정작 그 마무리가 데드라인 밖에서 일어났다.
    #:
    #:   고치는 방향이 둘인데 **끊는 쪽이 아니라 예약하는 쪽**을 골랐다 —
    #:   240초에 마무리를 막으면 모은 근거를 통째로 버리게 되고, 그건
    #:   *"빈손으로 돌려주지 않는다"*는 이 루프의 규율을 깬다.
    #:   → 계획·게이트·재질의는 **`reserve`만큼 일찍** 멈추고, 그렇게 비워 둔 자리에서
    #:     마무리를 부른다.
    #: ★ `reserve` 값을 내가 고르지 않았다 — **어댑터가 선언한 호출 1회 상한**
    #:   (`LLM.timeout`, 기본 60초)을 그대로 쓴다. 그 속성이 없는 가짜 LLM은 0이라
    #:   기존 테스트 동작이 그대로다.
    #: ★★ 계획 호출 **앞**에서는 `2 × reserve`를 본다. 산술이 그렇다 —
    #:   지금 하려는 계획 호출이 `timeout`까지 쓸 수 있고, **그 뒤에 마무리가 또 한 번**
    #:   `timeout`까지 쓸 수 있다. 한 칸만 떼면 계획 호출이 그 칸을 먹어 마무리가 넘친다
    #:   (실측으로 확인했다 — 예약 1칸 판에서 마무리가 데드라인을 22% 넘겼다).
    #:   기본값으로 환산하면 계획은 240 − 120 = **120초**까지다. 실측 소요는
    #:   중앙 17.4초 · p99 69.6초 · **최대 110.5초**(step9 n=510)라 최대값도 안 자른다.
    #: ★ 남는 위험은 밝혀 둔다 — 429 백오프(`_RATE_RETRY` 5회 × `_RATE_WAIT_MAX` 60초)는
    #:   이 예약을 넘길 수 있다. 그쪽은 `Retry-After` 존중과 상한이 막는 몫이다.
    reserve = float(getattr(llm, "timeout", 0) or 0)

    while True:
        stop = budget.exceeded(step, llm, reserve * 2)
        if stop:
            break
        step += 1
        try:
            if VOTE_N > 1 and step <= VOTE_STEPS:
                reply, votes, ncand = _vote_action(llm, messages, schemas,
                                                   VOTE_N, VOTE_TEMP)
                if ncand > 1:
                    trace.append(f"스텝 {step}: 액션 투표 {votes}/{VOTE_N}"
                                 f"(후보 {ncand}종)")
            else:
                reply = llm.tools(messages, schemas, system=SYSTEM)
        except L.LLMUnavailable:
            stop = "llm_unavailable"
            break
        except L.UnsupportedTool as e:
            # ★★ CLOVA 40009 — 모델이 **스키마에 없는 함수명을 생성**해 응답이 통째로
            #   버려졌다. 종전에는 여기서 `llm_error`로 끊겨 답변이 "확인되지 않습니다"로
            #   나갔다(270문항 각 1회 실측 첫 시도 3.7~7.8%가 이렇게 죽었다).
            #
            #   재시도는 답이 아니다 — 같은 입력이면 같은 이름을 또 만든다(그대로 재시도
            #   0/27, 270 전수에서 1/10만 회복). 반면 **`tool_choice="none"`은 18/18 회피**한다.
            #   공식 troubleshoot의 조치도 "요청을 처리할 수 있는 도구를 고르라"이고,
            #   Function Calling 문서가 `toolChoice: "none"`(도구 없이 일반 응답)을 준다.
            #
            #   ★ 다만 **근거 없이 도구만 끄면 환각한다.** 그래서 먼저 결정론 라우팅으로
            #     정본표를 넣어 준다 — 완료게이트와 **같은 경로**를 쓴다(규칙을 두 곳에 두면
            #     어긋난다). 근거를 못 넣으면 그대로 끊는다(없는 답을 지어내지 않는다).
            if not fell_back:
                fell_back = True
                inject = _unread_canon(question, evidence)
                for v in inject:
                    obs = _render(_canon_of(v))
                    evidence.append(("도구오류복구", {"표": v["table"]}, obs))
                    messages.append({"role": "user",
                                     "content": f"아래 근거로 답하십시오.\n{obs}"})
                trace.append(f"스텝 {step}: 도구 호출 실패(40009) — "
                             f"정본표 {[v['table'] for v in inject] or '없음'} 주입 후 "
                             f"도구를 끄고 재요청")
                if evidence:
                    try:
                        reply = llm.tools(messages, schemas, system=SYSTEM,
                                          tool_choice="none")
                    except L.LLMError as e2:
                        stop = "llm_error"
                        trace.append(f"LLM 오류(폴백 실패): {e2}")
                        break
                else:
                    stop = "llm_error"
                    trace.append(f"LLM 오류: {e} — 주입할 정본표가 없어 중단")
                    break
            else:
                stop = "llm_error"
                trace.append(f"LLM 오류(폴백 후 재발): {e}")
                break
        except L.LLMError as e:
            stop = "llm_error"
            trace.append(f"LLM 오류: {e}")
            break

        if not reply.tool_calls:
            # 도구를 부르지 않았다 = 답을 냈거나, 확인 불가로 판단했다.
            # 다만 **읽어야 할 정본표를 안 읽고 끝내려는 것**일 수 있다(MISSING-REQUIRED-CALL).
            # ★ **답이 이미 채워졌으면 주입하지 않는다.** 게이트의 목적은 *성급한 종료*를
            #   막는 것이지 근거를 더 얹는 것이 아니다. 스텝 제약을 풀어 게이트가 제대로
            #   작동하게 하자 27문항이 27.00 → 26.00으로 내려갔다 — 이미 잘 답한 질의에도
            #   표를 밀어 넣어 답을 흔든 것이다(부문표·단위인라인에서 겪은 것과 같은 패턴).
            #   그래서 **모델이 "확인되지 않음"을 말했을 때만** 발동한다.
            need = GATE_ONLY_ON_DENIAL is False or bool(_DENIAL.search(reply.text or ""))
            unread = _unread_canon(question, evidence) if (need and not gated) else []
            # ★ **스텝 예산으로 막지 않는다.** 게이트는 계획 호출이 아니라 근거를 넣어주는
            #   것이라 `max_steps`에 걸릴 이유가 없다. 종전에는 `budget.exceeded(step, llm)`을
            #   그대로 봐서 **마지막 스텝에서는 절대 발동하지 않았다** — 모델은 보통 스텝을
            #   다 쓰고 답하므로 게이트가 대부분 죽어 있었다(실측: SK하이닉스 배당성향이
            #   정본표 70/70 존재에도 "확인되지 않음"으로 나갔다).
            #   무한 루프는 `gated` 1회 제한과 hard_steps·호출 상한이 막는다.
            # ★ `budget.exceeded()`와 **같은 규칙**을 써야 한다. 토큰 상한을 None으로
            #   바꿨을 때 여기만 놓쳐 15개 테스트가 TypeError로 죽었다.
            # ★★ 그래서 손계산을 걷어내고 `budget.spent()`를 부른다(2026-08-26).
            #   손으로 옮겨 적은 이 세 줄이 **`max_seconds`를 빠뜨리고 있었다** —
            #   같은 주석이 "같은 규칙을 써야 한다"고 적어 둔 자리에서 규칙이 갈렸다.
            #   `reserve`를 함께 넘긴다: 게이트가 시간을 다 쓰면 마무리가 밀린다.
            #   계획 호출과 같은 자리라 **2칸**이다(게이트 호출 1 + 마무리 1).
            room = budget.spent(step, llm, reserve * 2) is None
            if unread and room:
                gated = True                  # 게이트는 **1회만**. 반복하면 루프가 된다.
                for v in unread:
                    obs = _render(_canon_of(v))
                    evidence.append(("완료게이트", {"표": v["table"]}, obs))
                    messages.append({"role": "user",
                                     "content": f"아래 정본표를 아직 읽지 않았습니다. "
                                                f"이 근거로 답을 보완하십시오.\n{obs}"})
                trace.append(f"스텝 {step}: 완료게이트 — 미열람 정본표 "
                             f"{[v['table'] for v in unread]} 주입")
                continue
            # ★★ 재질의 — **근거를 하나도 안 모은 채 사실을 주장하려 할 때 한 번 더 묻는다.**
            #
            #   실측(step4, 270×2): `ungrounded` 차단 **33건**인데 그중 O는 3건뿐이다.
            #   그리고 도구를 **한 번도 안 부른 문항이 25건**이고 그 정답률은 16%다
            #   (1회 부르면 60%). 즉 "도구 0회 → 자체 지식으로 답 → 환각가드가 차단"이
            #   그대로 손실이었다. 가드는 제대로 동작했지만 **막은 뒤 복구 경로가 없었다.**
            #
            #   근거: NCP 공식 쿡북(포럼 topic/527, Advanced RAG)이 도구 설명에 넣으라고
            #   제시한 문장이 정확히 이 처리다 —
            #     *"정보를 찾을 수 없었던 경우, 최종 답을 하지 않고 … 도구를 다시 사용할 수 있습니다."*
            #   문구는 **긍정형**으로 쓴다(topic/93: "제약(금지)은 효과가 덜하다").
            #
            #   **한 번만** 한다(`reasked`). 무한 되먹임은 IAL 조사(arXiv 2607.01641)에서
            #   에이전트 무한루프의 1위 원인(25.0%)이고, 이 저장소의 다른 되먹임 경로
            #   (`gated`·`fell_back`·재시도)도 전부 1회 바운드다.
            #
            #   **모델이 "확인되지 않음"이라 말한 경우는 건드리지 않는다** — 정직한 기권은
            #   우리 규정이 요구하는 답이고, 그걸 다시 밀면 없는 답을 짜내게 된다.
            if (not evidence and not reasked and room
                    and (reply.text or "").strip()
                    and not _DENIAL.search(reply.text or "")):
                reasked = True
                messages.append({"role": "user", "content": REASK})
                trace.append(f"스텝 {step}: 재질의 — 근거 0건 상태의 주장이라 도구 사용을 요청")
                continue
            answer = reply.text
            stop = "answered" if evidence else "abstain"
            trace.append(f"스텝 {step}: 도구 호출 없음 → 직접 답변")
            break

        messages.append({"role": "assistant", "content": reply.text or "",
                         "tool_calls": [{"id": tc.id, "type": "function",
                                         "function": {"name": tc.name,
                                                      "arguments": json.dumps(
                                                          tc.arguments, ensure_ascii=False)}}
                                        for tc in reply.tool_calls]})

        gained = 0
        for tc in reply.tool_calls:
            k = _key(tc.name, tc.arguments)
            if k in seen:
                obs = "이미 같은 인자로 호출했습니다. 다른 도구나 다른 인자를 쓰거나, 지금 근거로 답하십시오."
                trace.append(f"스텝 {step}: {tc.name} 반복 호출 — 건너뜀")
            else:
                seen.add(k)
                _a = _with_period(tc.name, tc.arguments, question)
                out, err = _execute(tc.name, _a, audit)
                if not err:
                    # 정본표를 **앞에** 붙인다 — 뒤에 두면 모델이 앞의 것을 센다(§6-33).
                    aux = _canon_assist(tc.name, _a, question, out)
                    if aux:
                        out = {**aux, "도구결과": out} if not isinstance(out, dict) \
                              else {**aux, **out}
                if err:
                    obs = "오류: " + err
                    trace.append(f"스텝 {step}: {tc.name} 실패 — {err[:120]}")
                else:
                    obs = _render(out)
                    gained += 1
                    # ★★★ **실제로 실행된 인자(`_a`)를 기록한다**(2026-08-26).
                    #   종전에는 모델이 낸 원본(`tc.arguments`)을 남겼다. 그런데 이 루프는
                    #   `_with_period`로 질문의 기간·분기를 **주입해서** 실행한다:
                    #       _calls    query='미청구공사'
                    #       실제 실행  query='미청구공사 2026년 1분기'
                    #   `_calls`는 *"어느 도구를 어떤 질의로 불렀나"*를 사후에 알려고 2026-08-09에
                    #   추가한 필드인데(그 아래 `_result` 주석), 그 목적을 정확히 못 지켰다 —
                    #   이 기록으로 재현하면 **다른 관측**이 나온다. r43·r45의 오답 전수 분석이
                    #   "모델이 실제로 부른 도구를 재현해 쟀다"고 적은 그 경로다.
                    #   주입 전 인자는 `messages`의 tool_calls에 그대로 남아 있다.
                    evidence.append((tc.name, _a, obs))
                    trace.append(f"스텝 {step}: {tc.name}({_args(_a)}) → {_head(obs)}")
            # 호환 문서가 snake_case를 명시한다. 네이티브 v3는 toolCallId — 전환일 재확인 대상.
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "name": tc.name, "content": obs})

        if gained == 0:
            stale += 1
            if stale >= NO_PROGRESS_LIMIT:
                stop = "repeat_call" if seen else "no_progress"
                break
        else:
            stale = 0

    # 예산으로 끊겼으면 갖고 있는 근거로 마무리한다 — 빈손으로 돌려주지 않는다.
    # ★ 이 호출이 위에서 **자리를 비워 둔 그 호출**이다(`reserve`). 그래서 여기서는
    #   `reserve` 없이 검사한다 — 있으면 부르고, 절대 바운드를 이미 넘겼으면 안 부른다.
    #   종전에는 **아무 검사도 없어서** `budget_time`·`budget_llm`으로 끊긴 뒤에도
    #   무조건 한 번 더 불렀다(§⑤).
    if not answer and stop not in ("llm_unavailable", "llm_error"):
        over = budget.spent(step, llm)
        if over:
            trace.append(f"마무리 생략 — 절대 바운드 초과({over}). "
                         f"모은 근거는 retrieved_context에 그대로 남는다")
        else:
            trace.append(f"마무리: {STOP.get(stop, ('예산 소진', ''))[0]}")
            try:
                answer = llm.chat(messages + [{"role": "user", "content": FINALIZE}],
                                  system=SYSTEM).text
            except L.LLMError as e:
                trace.append(f"마무리 실패: {e}")
                answer = ("공시에서 확인되지 않습니다 — 근거를 모았으나 답변 생성 단계에서 "
                          "실패했습니다.")
    if not answer:
        answer = "공시에서 확인되지 않습니다."

    # ── 요건 보완 재생성 (answer_spec.retry_hint) ──
    #
    # ★★ `retry_hint`는 **정의만 되어 있고 아무도 부르지 않았다**(2026-08-10 발견).
    #   `answer_spec` 모듈 docstring이 설계를 적어 뒀는데 연결이 빠졌다:
    #     *"형식 위반 시 그 항목만 짚어 1회 재생성하는 방식은 금융 단답 QA에서 쓰이는
    #       constrained retry와 같다(FinMMEval 2026)."*
    #   `tools/corrections.py`와 같은 종류의 누락이다.
    #
    #   피해 실측(270×2, AB md 실행): SYSTEM 규칙 12가 "가정을 밝히고 되물으라"고
    #   지시하는데 **10%만 지킨다.**
    #       질문이 연결/별도 미명시 96%  →  답변에 `연결 기준` 명시 **36%**
    #       질문이 연도 미명시     98%  →  답변에 연도 명시        **49%**
    #       확인 질문을 붙인 답변                                  **10%**
    #   주최 설명회가 못 박은 요구인데(§규칙 12·13) 생성 시점 안내(`prompt_hint`)만으로는
    #   안 지켜졌다. step4에서 **요건을 강화하자 +4~6문항**이 움직인 전례가 있다.
    #
    #   **가진 근거 안에서 빠진 항목만** 보완하게 한다 — 새 도구 호출은 하지 않으므로
    #   없는 사실이 들어올 자리가 없다. **1회만** 한다(다른 되먹임 경로와 같은 바운드).
    if answer and stop not in _DEAD_STOPS:
        hint2 = answer_spec.retry_hint(question, answer)
        # ★ 요건 보완은 **선택**이다(답은 이미 있다). 그러니 호출 수뿐 아니라
        #   시간·토큰까지 같은 규칙으로 보고, 다음 호출 몫(`reserve`)도 남아 있어야 한다.
        #   종전에는 `llm.calls`만 봐서 데드라인을 지난 뒤에도 한 번 더 불렀다.
        room2 = budget.spent(step, llm, reserve) is None
        if hint2 and room2:
            try:
                fixed = llm.chat(messages + [{"role": "assistant", "content": answer},
                                             {"role": "user", "content": hint2}],
                                 system=SYSTEM).text
                # ★★ **정보가 줄면 버린다**(2026-08-11). 보완은 더하는 것이지 빼는 것이
                #   아니다 — 임의 임계값이 아니라 단조성 조건이다.
                #   실측(step6): 보완 재생성이 원 답변을 통째로 버리고 "확인할 수 없습니다"만
                #   남겼다(#21 삼성전자: 합계 308·상장 1·비상장 307·주요 158을 전부 잃음).
                #   문구도 고쳤지만(`answer_spec.retry_hint`) 프롬프트만 믿지 않는다.
                from agent2 import grade as _grade    # 지역 import — 순환 참조 방지
                if fixed and fixed.strip():
                    # ★ **기업명에도 같은 단조성**을 건다(2026-08-11).
                    #   수치 개수만 봤더니 **비교 문항에서 한쪽 회사가 통째로 사라졌다**
                    #   (실측 1-6: `삼성전자/SK하이닉스` → SK하이닉스만 · `한화오션/HD현대중공업`
                    #    → HD현대중공업만 · `삼성생명/KB금융` → 삼성생명만).
                    #   빠진 회사의 수치를 다른 회사가 채워 개수는 유지됐다.
                    #   원 답변에 있던 기업이 하나라도 사라지면 보완을 버린다.
                    names = {c["corp_name"] for c in _store.universe()}
                    had = {c for c in names if c in answer}
                    kept = {c for c in had if c in fixed}
                    keep = (len(_grade.numbers(fixed)) >= len(_grade.numbers(answer))
                            and kept == had)
                    if keep:
                        answer = fixed
                        trace.append("요건 보완: " + hint2.splitlines()[-1].strip())
                    else:
                        trace.append(f"요건 보완 버림 — 수치가 줄었다 "
                                     f"(수치 {len(_grade.numbers(answer))}→{len(_grade.numbers(fixed))} · 기업 {len(had)}→{len(kept)})")
            except L.LLMError as e:            # 보완은 보조다 — 실패해도 원 답변을 지킨다
                trace.append(f"요건 보완 실패(원 답변 유지): {e}")

    # ── 근거 가드 (claims.py) ──
    # 답변을 원자 클레임으로 쪼개 근거와 대조한다. 도구를 하나도 안 쓰고 사실을 주장하면
    # 여기서 막는다 — 실제로 "반도체 섹터 산업이 어떻지?"에 도구 0회로 자체 지식을 답한
    # 규정 위반이 있었다. 과잉 차단이 더 위험하므로 기본은 표시만 하고, 전면 차단은
    # 도구 0회인 경우로 한정한다.
    ctx = _context(evidence)
    computed = [r for _op, _args, r in audit.computation if isinstance(r, (int, float))]
    # ★ `system_text=SYSTEM`을 넘겨 **지시문 유출**까지 본다(어휘가 아니라 내용을 본다).
    #   전수 실측: SYSTEM 30자 조각이 정상 답변 3,784건에 **0건** 나온다.
    # ★ `question`을 넘긴다(2026-08-26). 답변만 보는 방어는 **인젝션에 순순히 따르면서
    #   아무것도 유출하지 않는** 공격을 구조적으로 못 잡는다(`claims.INJECTION` 주석).
    answer, rep = claims.guard(answer, ctx, tools_used=[n for n, _, _ in evidence],
                               computed=computed, system_text=SYSTEM,
                               question=question)
    trace.append(claims.render(rep))
    if rep.get("blocked"):
        stop = "ungrounded"

    trace.append(f"정지[{stop}] {STOP.get(stop, ('미상', '미상'))[0]} · {budget.report(step, llm)}")
    return _result(question, question_id, answer, evidence, trace, stop,
                   step, budget, llm, audit)


def _args(d):
    return ", ".join(f"{k}={v!r}" for k, v in sorted(d.items()))


def _head(s, n=80):
    s = " ".join(s.split())
    return s[:n] + ("…" if len(s) > n else "")


def _context(evidence):
    """근거 묶음 → retrieved_context 문자열. 가드와 결과가 **같은 문자열**을 봐야 한다."""
    return "\n\n".join(f"[{i}] {name}({_args(args)})\n{obs}"
                       for i, (name, args, obs) in enumerate(evidence, 1))


def _result(question, question_id, answer, evidence, trace, stop, step, budget, llm, audit):
    ctx = _context(evidence)
    think = "\n".join(trace)
    if audit.computation or audit.entries:
        think += "\n\n--- 감사 ---\n" + audit.render()
    r = Result({
        "question_id": str(question_id or ""),
        "question": question,
        "retrieved_context": ctx or "(도구 호출 없음)",
        "think_trace": think,
        "answer": answer,
    })
    r["_stop"] = stop
    r["_stop_category"] = STOP.get(stop, ("", "미상"))[1]
    r["_steps"] = step
    r["_llm_calls"] = llm.calls
    r["_tokens"] = llm.usage.total
    r["_seconds"] = round(budget.elapsed, 2)
    r["_tools"] = [name for name, _, _ in evidence]
    # ★ 2026-08-09 — **인자와 관측을 함께 내보낸다.** 종전에는 이름만 남겨서,
    #   오답을 사후 분석할 때 "어느 도구를 어떤 질의로 불렀나"를 알 수 없었다.
    #   실측 피해(2026-08-09 회귀 분석): #102가 정본표를 탔는지 검색 폴백을 탔는지
    #   확인하지 못해 원인을 **세 번 연속 틀리게** 짚었다(관측 절단 → 후보 선택 →
    #   커버리지 결손, 전부 오답). 결국 도구 출력을 재현해 바이트 비교하는 우회로 뒤집었다.
    #   `retrieved_context`에 같은 정보가 있지만 그건 답변용 문자열이라 파싱이 불안정하다.
    r["_calls"] = [(name, args, obs) for name, args, obs in evidence]
    r["_audit"] = audit
    return r


if __name__ == "__main__":
    import sys

    from agent2 import contract

    q = " ".join(sys.argv[1:]) or "삼성전자 2025년 매출액은?"
    res = run(q)
    print("=== think_trace ===\n" + res["think_trace"])
    print("\n=== retrieved_context ===\n" + _head(res["retrieved_context"], 600))
    print("\n=== answer ===\n" + res["answer"])
    print(f"\n정지={res['_stop']}({res['_stop_category']}) 스텝={res['_steps']} "
          f"LLM={res['_llm_calls']}회 토큰={res['_tokens']} {res['_seconds']}초")
    print("계약 위반:", contract.validate(contract.shape(res, q)) or "없음")
