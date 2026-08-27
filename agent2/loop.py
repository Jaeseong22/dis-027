"""PAO 루프 — Plan · Act · Observe. LLM은 **어떤 도구를 어떤 인자로 부를지만** 정한다."""
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
NO_PROGRESS_LIMIT = 3


class Budget:
    """예산 집행. 초과하면 정지 이유 코드를 돌려준다."""

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
        """**절대 바운드**만 본다 — `max_steps`(계획 예산)는 보지 않는다."""
        if step >= self.hard_steps:
            return "budget_hard"
        if llm.calls >= self.max_llm_calls:
            return "budget_llm"
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


#: 답변에 실린 수치 개수 — 요건 보완이 **정보를 줄이지 않았는지** 보는 단조성 검사에 쓴다.
#: 값이 아니라 개수만 필요하므로 단위 사전은 인식 폭을 넓히는 용도다.
_N_CUR = {"조원", "조", "억원", "억", "백만원", "백만", "천원", "천", "원"}
_N_COUNT = {"%", "주", "명", "사", "개사", "개", "배", "포인트"}
_N_UNIT = "|".join(sorted(_N_CUR | _N_COUNT, key=len, reverse=True))
_N_RE = _re.compile(
    r"(?P<open>[(（])?\s*(?P<sign>[-−△▲])?\s*(?P<n>\d[\d,]*(?:\.\d+)?)\s*(?P<close>[)）])?"
    r"\s*(?P<u>" + _N_UNIT + r")?")


def _numbers(text):
    """답변에서 수치를 전부 뽑는다. '백만 원' 같은 띄어쓰기는 붙여서 본다."""
    t = (text or "").replace("\u3000", " ")
    t = _re.sub(r"(조|억|백만|천)\s+원", r"\1원", t)
    return [m.group("n") for m in _N_RE.finditer(t) if m.group("n")]


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

_ROUTE_TOOLS = ("find_tables", "find_sections")
_Q_PERIODIC = _re.compile(r"(20\d\d\s*년\s*[1-4]\s*분기|[1-4]\s*분기|반기)")


def _question_periodic(question):
    """질문이 지목한 분기·반기 표현. 사업보고서 질의면 None."""
    m = _Q_PERIODIC.search(question or "")
    return m.group(1) if m else None

_ASSIST_TOOLS = ("get_financials", "list_filings", "list_sections", "find_sections")
ASSIST_BUDGET = 2000


def _assist_year(question):
    """질문이 지목한 (연도, subtype, month). 다년 비교면 연도는 None."""
    ys = {int("20" + m) for m in _re.findall(r"20(\d\d)\s*년", question or "")}
    sub, mon = _tools._periodic_of(question or "")
    return (ys.pop() if len(ys) == 1 else None), sub, mon


def _canon_assist(name, args, question, out=None):
    """질문이 정본표 주제면 그 표를 골라 준다. 없으면 None."""
    if name not in _ASSIST_TOOLS:
        return None
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
    out = {}
    for tid in ids[:2]:
        try:
            v = _doctables.values(nm, tid, year, subtype=sub, month=mon)
        except Exception:
            v = None
        if v:
            lab = _doctables.BY_ID[tid].name
            if year:
                lab = f"{lab} · {year}년" + (f" {mon // 3}분기" if sub == "quarter" else "")
            out[lab] = _tools._canon(v, budget=ASSIST_BUDGET, query=question)
    return out or None


def _with_period(name, args, question):
    """도구 인자에 질문 기간·연도를 채운다. 모델이 이미 준 값은 덮지 않는다."""
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
    """N번 샘플해 **액션 다수결**. (선택된 reply, 표수, 후보수)를 준다."""
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
_MULTI_Q = __import__("re").compile(
    r"유형별|비교|더\s*큰|더\s*많|중\s*(어디|어느|누가)|와\s*\S+\s*중|과\s*\S+\s*중|순위|각각")
#: 늘린 스텝 수. 3스텝 초과 사례 8건이 전부 정답이었고 그 최대가 6스텝이라 **5**로 둔다
#: (문헌의 예산 등급도 3→5가 한 칸이다 · arXiv 2603.08877).
_MULTI_STEPS = 5

GATE = True

#: 게이트를 **모델이 정보한계를 말했을 때만** 걸지. A/B용.
GATE_ONLY_ON_DENIAL = True

_DENIAL = claims.DENIAL


def _corp_in(question):
    """자유문장에서 기업을 뽑는다. `resolve_corp`는 **정확한 이름**만 받아 문장엔 못 쓴다."""
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
        from agent2 import tools as _t
        _sub, _mon = _t._periodic_of(question or "")
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
    """질의 1건 → 답변. 계약 필드와 진단을 함께 담은 dict."""
    llm = llm or L.LLM()
    budget = budget or Budget()
    if _MULTI_Q.search(question or "") and budget.max_steps < _MULTI_STEPS:
        budget = _copy.copy(budget)
        budget.max_steps = _MULTI_STEPS
        trace_note = f"다중 조회 질의 — 스텝 예산 {_MULTI_STEPS}"
    else:
        trace_note = ""
    if VOTE_N > 1:
        # 투표 샘플은 **진짜 LLM 호출**이다. 예산을 안 늘리면 투표가 상한을 먹어
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
            need = GATE_ONLY_ON_DENIAL is False or bool(_DENIAL.search(reply.text or ""))
            unread = _unread_canon(question, evidence) if (need and not gated) else []
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
                    evidence.append((tc.name, _a, obs))
                    trace.append(f"스텝 {step}: {tc.name}({_args(_a)})"
                                 + _obs_summary(obs))
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
    if answer and stop not in _DEAD_STOPS:
        hint2 = answer_spec.retry_hint(question, answer)
        room2 = budget.spent(step, llm, reserve) is None
        if hint2 and room2:
            try:
                fixed = llm.chat(messages + [{"role": "assistant", "content": answer},
                                             {"role": "user", "content": hint2}],
                                 system=SYSTEM).text
                if fixed and fixed.strip():
                    names = {c["corp_name"] for c in _store.universe()}
                    had = {c for c in names if c in answer}
                    kept = {c for c in had if c in fixed}
                    keep = (len(_numbers(fixed)) >= len(_numbers(answer))
                            and kept == had)
                    if keep:
                        answer = fixed
                        trace.append("요건 보완: " + hint2.splitlines()[-1].strip())
                    else:
                        trace.append(f"요건 보완 버림 — 수치가 줄었다 "
                                     f"(수치 {len(_numbers(answer))}→{len(_numbers(fixed))} · 기업 {len(had)}→{len(kept)})")
            except L.LLMError as e:            # 보완은 보조다 — 실패해도 원 답변을 지킨다
                trace.append(f"요건 보완 실패(원 답변 유지): {e}")

    # ── 근거 가드 (claims.py) ──
    # 답변을 원자 클레임으로 쪼개 근거와 대조한다. 도구를 하나도 안 쓰고 사실을 주장하면
    # 여기서 막는다 — 실제로 "반도체 섹터 산업이 어떻지?"에 도구 0회로 자체 지식을 답한
    # 규정 위반이 있었다. 과잉 차단이 더 위험하므로 기본은 표시만 하고, 전면 차단은
    # 도구 0회인 경우로 한정한다.
    ctx = _context(evidence)
    computed = [r for _op, _args, r in audit.computation if isinstance(r, (int, float))]
    answer, rep = claims.guard(answer, ctx, tools_used=[n for n, _, _ in evidence],
                               computed=computed, system_text=SYSTEM,
                               question=question)
    trace.append(claims.render(rep))
    if rep.get("blocked"):
        stop = "ungrounded"

    # 근거 공시 표시. 모델이 옮겨 적지 않으면 관측의 `출처`를 그대로 붙인다.
    answer, _n_cite = _cite_fallback(answer, evidence, blocked=bool(rep.get("blocked")))
    if _n_cite:
        trace.append(f"근거표시: 답변에 근거가 없어 관측의 출처 {_n_cite}건을 덧붙였다")

    trace.append(f"정지[{stop}] {STOP.get(stop, ('미상', '미상'))[0]} · {budget.report(step, llm)}")
    return _result(question, question_id, answer, evidence, trace, stop,
                   step, budget, llm, audit)


#: 답변에 이미 근거 공시가 표시됐는지 — 접수번호 14자리로 본다.
#: `\b`를 쓰면 안 된다: 한글도 `\w`라 "…20260318001672입니다"에서 단어경계가 없다.
_HAS_CITE = _re.compile(r"(?<!\d)\d{14}(?!\d)")

#: 관측(JSON 렌더)에서 `출처` 값만 꺼낸다. `출처_note`는 키가 달라 걸리지 않고,
#: 관측이 잘려 닫는 따옴표가 없으면 매치되지 않는다(반쪽 출처를 쓰지 않는다).
_OBS_CITE = _re.compile(r'"출처"\s*:\s*"([^"\\]{4,200})"')

#: 한 답변에 붙일 출처 개수 상한.
_CITE_MAX = 3


def _cite_fallback(answer, evidence, blocked=False):
    """답변에 근거 공시 표시가 없으면 관측의 `출처`를 그대로 덧붙인다.

    요구사항은 "모든 답변에는 근거 공시를 표시할 것"이고 형식은 공시명·공시일이다.
    `SYSTEM` 규칙 10이 도구가 준 `출처`를 그대로 적으라고 지시하지만 지켜지지 않는다 —
    관측이 출처를 주는 비율 80.1%에 대해 답변이 옮겨 적는 비율은 43.3%다(실측 201문항).
    도구 쪽에 출처를 더 실어도 이 비율은 움직이지 않았으므로, 사후 부착으로 보완한다.

    새 조회도 추론도 하지 않는다. `retrieved_context`에 그대로 있는 문자열만 옮기므로
    근거 가드의 대조 대상과 어긋나지 않는다.

    안 붙이는 경우: 답변에 이미 접수번호가 있다 · 가드가 차단했다 · 관측에 출처가 없다.
    """
    if blocked or not (answer or "").strip():
        return answer, 0
    if _HAS_CITE.search(answer):
        return answer, 0
    srcs = []
    for _name, _a, obs in evidence:
        for src in _OBS_CITE.findall(obs if isinstance(obs, str) else str(obs)):
            src = src.strip()
            if src and src not in srcs:
                srcs.append(src)
    if not srcs:
        return answer, 0
    srcs = srcs[:_CITE_MAX]
    return answer.rstrip() + "\n\n(근거: " + " · ".join(srcs) + ")", len(srcs)


#: think_trace 절 구분 — 접두어로 분류한다. 순서는 바꾸지 않고 절이 바뀔 때만 머리말을 넣는다.
#: 기계용 첫 줄(`[route=…]`)과 `스텝 N: 도구(` 접두는 유지한다 — 진입점과 계약 검사가 읽는다.
_TRACE_SECTIONS = (
    ("질의 해석", ("질의:", "요건:")),
    ("근거 수집", ("스텝 ", "마무리", "LLM 오류", "도구오류", "완료게이트")),
    ("검증",      ("근거가드", "근거표시", "요건 보완")),
    ("종료",      ("정지[",)),
)


def _section_of(line):
    for name, prefixes in _TRACE_SECTIONS:
        if line.startswith(prefixes):
            return name
    return None


def _format_trace(trace):
    """평평한 trace 목록 → 절로 묶인 형태. 순서는 바꾸지 않는다."""
    out, cur = [], None
    for line in trace:
        sec = _section_of(line) or cur or "근거 수집"
        if sec != cur:
            out.append(("" if not out else "\n") + f"── {sec} " + "─" * max(0, 46 - len(sec)))
            cur = sec
        out.append(line)
    return "\n".join(out)


#: 관측 요약에 실을 값의 개수.
_OBS_KEYS = 4


def _obs_summary(obs):
    """관측 → "무엇을 얻었는가" 한 줄.

    JSON 앞 80자만 실으면 그 스텝에서 무슨 근거를 얻었는지가 보이지 않는다.
    관측은 `_render`가 만든 JSON 문자열이므로, 파싱되면 출처와 핵심 값을 뽑고
    잘려서 파싱이 안 되면 앞부분을 싣되 잘렸다고 밝힌다(조용한 손실 금지).
    """
    if not isinstance(obs, str):
        obs = str(obs)
    if obs.startswith(("오류:", "None (", "이미 같은 인자로")):
        return " → " + _head(obs, 110)
    try:
        d = json.loads(obs)
    except (ValueError, TypeError):
        return " → " + _head(obs, 110) + "  (관측이 잘려 요약 불가)"
    if not isinstance(d, dict):
        return " → " + _head(obs, 110)

    bits = []
    if d.get("출처"):
        bits.append(f"출처 {d['출처']}")
    scope = d.get("scope") or d.get("표") or d.get("개념") or d.get("table")
    if scope:
        bits.append(f"범위 {scope}")
    got = d.get("values")
    if isinstance(got, dict) and got:
        head = " · ".join(f"{k} {v}" for k, v in list(got.items())[:_OBS_KEYS])
        more = f" … (총 {len(got)}개 항목)" if len(got) > _OBS_KEYS else ""
        bits.append("획득 " + head + more)
    else:
        rows = d.get("rows") or d.get("연도별") or d.get("목록")
        if isinstance(rows, list):
            bits.append(f"획득 {len(rows)}행")
        elif d.get("계수") is not None:
            bits.append(f"획득 계수 {d['계수']}")
        else:
            keys = [k for k in d if not k.startswith("_")][:6]
            bits.append("획득 " + ", ".join(keys) if keys else "획득 없음")
    return "\n        " + "\n        ".join(bits)


def _args(d):
    return ", ".join(f"{k}={v!r}" for k, v in sorted(d.items()))


def _head(s, n=80):
    s = " ".join(s.split())
    return s[:n] + ("…" if len(s) > n else "")


def _context(evidence):
    """근거 묶음 → retrieved_context 문자열. 가드와 결과가 **같은 문자열**을 봐야 한다."""
    return "\n\n".join(f"[{i}] {name}({_args(args)})\n{obs}"
                       for i, (name, args, obs) in enumerate(evidence, 1))


#: `retrieved_context`에서 뺄 키 — **근거가 아닌 것**.
#:
#:   audit    결정론 추출의 감사 로그. 값·라벨·XBRL 태그가 전부 `values`·`sources`와
#:            중복이다(실측: `audit`에만 있는 수치는 XBRL 스케일 코드 `-6` 하나).
#:            성격상 "사고·추론·도구 사용 과정"이므로 think_trace의 몫이다.
#:   units    `values`의 각 문자열이 이미 단위를 달고 있다(실측 28/28 중복).
#:   *_note   **모델에게 주는 지시문**이다("직접 계산하지 마십시오"). 채점자가 읽는
#:            "답변 생성에 참고한 검색 문서"에 지시문이 실릴 이유가 없다.
_PUBLIC_DROP = ("audit", "units")

#: 위에서 따로 렌더하므로 일반 키 순회에서 제외한다.
_PUBLIC_SHOWN = ("출처", "scope", "period", "unit", "values", "series", "sources", "status",
                 "corp", "year", "structure")


def _is_note(k):
    return k.endswith("_note") or k == "note"


def _public_obs(obs):
    """관측 → 사람이 읽는 근거. **값은 하나도 버리지 않는다.**

    `retrieved_context`는 계약상 "답변 생성에 참고한 검색 문서"이고 채점 축은
    근거 완전성(필수 데이터가 검색 근거에 포함됐는가)이다. 그런데 이 필드는
    내부 관측 문자열을 그대로 실어 왔다 — JSON 덤프에 모델용 지시문과 추출
    디버그 로그까지 섞여 나갔다(실측 10,332자 중 근거가 아닌 것 18.6%).

    아는 모양은 표로 펴고, **모르는 키는 그대로 싣는다** — 도구가 12종이고
    관측 모양이 제각각이라 화이트리스트로 만들면 기본값이 '버림'이 된다(§6-2).
    파싱이 안 되면(절단 등) 원문을 그대로 돌려준다.
    """
    if not isinstance(obs, str):
        obs = str(obs)
    try:
        d = json.loads(obs)
    except (ValueError, TypeError):
        return obs
    if not isinstance(d, dict):
        return obs

    out = []
    if d.get("출처"):
        out.append(f"    출처   {d['출처']}")
    unit = f"단위 {d['unit']}" if d.get("unit") else None
    who = " ".join(str(x) for x in (d.get("corp"), d.get("year")) if x)
    scope = " · ".join(x for x in (who or None, d.get("scope"), d.get("period"), unit) if x)
    if scope:
        out.append(f"    기준   {scope}")

    src = d.get("sources") if isinstance(d.get("sources"), dict) else {}
    vals = d.get("values")
    ser = d.get("series")
    out += _value_table(vals, ser, src)

    # 남은 키는 **하나도 버리지 않는다**. 근거가 아닌 것만 위 상수로 명시해 뺀다.
    for k, v in d.items():
        if k in _PUBLIC_DROP or k in _PUBLIC_SHOWN or _is_note(k):
            continue
        if isinstance(v, dict) and v and all(not isinstance(x, (dict, list)) for x in v.values()):
            out.append(f"    {k}   " + " · ".join(f"{a} {b}" for a, b in v.items()))
        else:
            out.append(f"    {k}   " + (json.dumps(v, ensure_ascii=False)
                                        if isinstance(v, (dict, list)) else str(v)))
    if src and not isinstance(vals, dict):
        out.append("    sources   " + json.dumps(src, ensure_ascii=False))
    return "\n".join(out) if out else obs


#: 원문 라벨에서 XBRL 태그 부분을 떼는 정규식. 라벨과 태그를 열로 나눠 싣는다.
_SRC_TAG = _re.compile(r"^(.*?)\s*\[([^\]]+)\]\s*$")

#: 재무 항목을 재무제표별로 묶는다 — 28행을 한 덩어리로 쏟으면 스캔이 안 된다.
#: 여기 없는 개념은 마지막 묶음(`기타`)으로 간다. **버리지 않는다.**
_GROUPS = (
    ("재무상태표", ("assets_total", "liabilities_total", "equity_total", "issued_capital",
                 "inventories", "ppe", "intangibles", "retained_earnings",
                 "current_assets", "current_liabilities", "cash", "nci")),
    ("손익계산서", ("revenue", "cost_of_sales", "gross_profit", "sga", "operating_income",
                 "pretax_income", "net_income", "net_income_owners", "tax_expense",
                 "equity_method_income", "eps_basic", "eps_diluted",
                 "net_interest_income", "insurance_revenue")),
    ("현금흐름표", ("cf_operating", "cf_investing", "cf_financing", "equity_begin")),
)
_GROUP_OF = {cid: name for name, ids in _GROUPS for cid in ids}


def _w(s):
    """표시 폭. 한글·전각은 2칸이다 — `len()`으로 맞추면 표가 어긋난다."""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def _pad(s, n, right=False):
    s = str(s)
    gap = " " * max(0, n - _w(s))
    return (gap + s) if right else (s + gap)


def _grouped(vals):
    """재무제표 묶음 순서로 항목을 낸다. 묶음에 없는 개념은 뒤에 그대로 붙인다."""
    order = {cid: i for i, (_n, ids) in enumerate(_GROUPS) for cid in ids}
    keys = sorted(vals, key=lambda k: (order.get(k, len(_GROUPS)), list(vals).index(k)))
    return [(k, vals[k]) for k in keys]


def _value_table(vals, ser, src):
    """`values`와 `series`를 **한 표로** 합친다.

    합쳐도 되는 근거: `values[k]`가 `series[k]`의 최신연도 값과 같다
    (전수 70사 × 연결/별도 · 대조 3,525건 · 일치 3,525건 = 100%).
    따로 실으면 같은 값이 두 번 나가고 항목 28개가 56행이 된다.

    라벨을 **앞**에 두는 이유: 사람이 표를 읽을 때 눈이 라벨을 먼저 따라간다.
    종전에는 숫자가 앞이고 라벨이 뒤라 스캔이 안 됐다.
    """
    if not isinstance(vals, dict) or not vals:
        out = []
        if vals is not None:
            out.append("    값   " + json.dumps(vals, ensure_ascii=False))
        if isinstance(ser, dict) and ser:
            out.append("    추이")
            for k, v in ser.items():
                line = " → ".join(f"{y} {x}" for y, x in v.items()) if isinstance(v, dict) \
                       else json.dumps(v, ensure_ascii=False)
                out.append(f"      {src.get(k, k)}  {line}")
        elif ser is not None:
            out.append("    추이   " + json.dumps(ser, ensure_ascii=False))
        return out

    ser = ser if isinstance(ser, dict) else {}
    years = sorted({y for v in ser.values() if isinstance(v, dict) for y in v})
    rows, tags = [], []
    for k, cur in vals.items():
        pass
    rows, tags = [], []
    for k, cur in _grouped(vals):
        raw = str(src.get(k, k))
        m = _SRC_TAG.match(raw)
        label, tag = (m.group(1), m.group(2)) if m else (raw, "")
        if tag:
            tags.append(f"{label}={tag}")
        cells = []
        row_ser = ser.get(k) if isinstance(ser.get(k), dict) else {}
        for y in years:
            cells.append(str(row_ser.get(y, "")))
        # 최신연도 열이 비면 `values`를 그 자리에 둔다(추이가 없는 항목).
        if years and not cells[-1]:
            cells[-1] = str(cur)
        if not years:
            cells = [str(cur)]
        rows.append((label, cells, _GROUP_OF.get(k, "기타")))

    # 칸마다 같은 단위를 반복하지 않는다 — 머리말이 이미 말한다. 다른 단위만 칸에 남긴다
    # (주당이익은 `원`이고 나머지는 `백만원`이다. 통째로 떼면 그 행이 틀린 값이 된다).
    _cnt = {}
    for _, cells, _g in rows:
        for c in cells:
            u = c.rsplit(" ", 1)[-1] if " " in c else ""
            if u and not u[-1:].isdigit():
                _cnt[u] = _cnt.get(u, 0) + 1
    common = max(_cnt, key=_cnt.get) if _cnt else ""
    if common:
        rows = [(lb, [c[: -len(common)].rstrip() if c.endswith(" " + common) else c
                      for c in cells], g) for lb, cells, g in rows]

    cols = [str(y) for y in years] or ["값"]
    # ★ 열 폭은 **내용에서** 잡는다. 고정 폭으로 뒀더니 `1,083,335,531,792`(17자)가
    #   옆 칸과 붙어(`…909,8891,083,…`) 값 두 개가 한 수로 읽혔다.
    w = max([_w(r[0]) for r in rows] + [_w("항목")]) + 2
    cw = []
    for i, c in enumerate(cols):
        cw.append(max([_w(c)] + [_w(r[1][i]) for r in rows if i < len(r[1])]) + 2)
    head = "    " + _pad("항목" + (f" (단위 {common})" if common else ""), w) \
           + "".join(_pad(c, cw[i], right=True) for i, c in enumerate(cols))
    out = [head, "    " + "─" * min(_w(head) - 4, 110)]
    cur_g = None
    for label, cells, g in rows:
        if g != cur_g:
            out.append(f"    · {g}")
            cur_g = g
        out.append("      " + _pad(label, w)
                   + "".join(_pad(c, cw[i], right=True) for i, c in enumerate(cells)))
    if tags:
        # 접두사(`ifrs-full_`·`dart_`)는 전 항목이 공유한다 — 라벨당 한 번씩 적을 이유가 없다.
        short = [t.replace("=ifrs-full_", "=").replace("=dart_", "=dart:") for t in tags]
        out.append("    XBRL 태그 (ifrs-full 접두 생략)")
        line = "      "
        for t in short:
            if _w(line) + _w(t) > 104:
                out.append(line.rstrip(" ·")); line = "      "
            line += t + " · "
        if line.strip():
            out.append(line.rstrip(" ·"))
    return out


def _public_context(evidence):
    """`retrieved_context` 필드용. 가드가 보는 `_context`(전체)와 **분리**한다 —
    검증은 관측 전체를 봐야 하고, 채점자는 근거만 보면 된다."""
    return "\n\n".join(f"[{i}] {name}({_args(args)})\n{_public_obs(obs)}"
                       for i, (name, args, obs) in enumerate(evidence, 1))


def _result(question, question_id, answer, evidence, trace, stop, step, budget, llm, audit):
    ctx = _public_context(evidence)
    think = _format_trace(trace)
    if audit.computation or audit.entries:
        think += ("\n\n── 감사 로그 " + "─" * 40 + "\n") + audit.render()
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
