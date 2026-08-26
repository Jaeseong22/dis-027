"""XBRL 사실 추출 — 공시 XML 표 셀에 박힌 표준 태그를 그대로 읽는다.

## 왜 이게 필요한가 (2026-07-31에 뒤집힌 전제)

종전에는 "로컬 XML엔 XBRL account_id 태그가 없다"고 알고 있었고, 그래서
한글 계정 라벨을 후보 리스트로 훑는 방식으로 우회했다. **그 전제가 틀렸다.**
FY2025 사업보고서 보유 **70/70 기업 전부**가 재무제표 표 셀에 XBRL 속성을 갖고 있다
(전수 실측: 연결 컨텍스트 439,995셀 · 별도 324,546셀 · 고유 코드 17,559종).

라벨 파싱의 대가는 실제로 치렀다 — 삼성전자 판매비와관리비를
**별도 48,445,100**으로 답했다. 연결은 **87,769,374**이고, 둘은 코퍼스에 나란히 있으며
`ACONTEXT`로만 구분된다. 라벨은 둘 다 "판매비와관리비"다.

## 셀에 실려 있는 것 (실측)

    <TE ACODE="dart_TotalSellingGeneralAdministrativeExpenses"
        ACONTEXT="CFY2025dFY_..._ifrs-full_ConsolidatedMember"
        ADECIMAL="-6" ANEGATED="N">87,769,374</TE>

  · `ACODE`     계정 요소명. `ifrs-full_`(IFRS 표준) · `dart_`(금감원 확장) · `entity…`(회사 확장)
  · `ACONTEXT`  **기간 + 연결/별도 축**
                  `CFY2025` 당기 · `PFY2024` 전기 · `BPFY2023` 전전기
                  `d` 기간(손익·현금흐름) · `e` 시점(재무상태표)
                  `ifrs-full_ConsolidatedMember` 연결 / `ifrs-full_SeparateMember` 별도
  · `ADECIMAL`  자릿수. `-6` = 백만원 단위 표시 · `-3` = 천원 · `INF` = 정확값(주당이익·비율)
  · `ANEGATED`  표시부호 반전 여부

즉 **3개년 × 연결/별도 × 단위**가 한 문서 안에 구조화돼 있다. 추측할 것이 없다.

## 개념 → 코드 배열 (EdgarTools 방식)

SEC 태그 ~18,000종을 정규 개념 84~95개로 줄일 때 쓰는 방식이 **개념마다 코드 배열을 두고
배열 순서를 우선순위로 삼는 것**이다. 우리도 같다 — 우리 코퍼스는 고유 코드 17,559종
(`dart_` 1,124 + `ifrs-full_` 등 1,689 + 회사 확장 14,746)으로 규모까지 비슷하다.

배열 순서가 실제로 필요한 이유(실측): `ifrs-full_SellingGeneralAndAdministrativeExpense`가
어떤 회사에서는 라벨이 **"재산관리비"**다. 이름이 비슷하다고 먼저 잡으면 틀린다.
그래서 `dart_TotalSellingGeneralAdministrativeExpenses`를 앞에 둔다.

## 한계 (정직하게)

  · 태그가 **없는 기업 2곳**: 레인보우로보틱스 · 디앤디파마텍 → 기존 라벨 파서로 폴백해야 한다.
  · 회사 확장 코드(`entity…`)는 회사마다 뜻이 달라 **개념 매핑에 쓰지 않는다.**
  · 여기는 **사실을 읽기만** 한다. 어떤 값을 답으로 쓸지는 부르는 쪽이 정한다.

실행: python3 -m agent2.tools.xbrl 삼성전자
"""
import os
import re
from collections import namedtuple

from agent2.data import source, store

#: 표 셀 + 속성. 값은 셀 텍스트.
_CELL = re.compile(r'<T[EDU]\s([^>]*?ACODE="[^"]*"[^>]*?)>(.*?)</T[EDU]>', re.S)
_ATTR = re.compile(r'(\w+)="([^"]*)"')
_TAG = re.compile(r"<[^>]+>")
#: 행 — 라벨(행의 첫 텍스트 셀)을 붙이기 위해 행 단위로도 훑는다.
_ROW = re.compile(r"<TR[^>]*>(.*?)</TR>", re.S)
_TEXT = re.compile(r"<T[EDU][^>]*>(.*?)</T[EDU]>", re.S)

#: 기간 접두사. 실측 6종: CFY2025dFY · PFY2024dFY · BPFY2023dFY · 각 eFY.
_CTX = re.compile(r"^(C|P|BP)FY(\d{4})([de])")

CONSOLIDATED = "연결"
SEPARATE = "별도"
UNSPECIFIED = "미표기"

Fact = namedtuple("Fact", "code label scope year kind value krw decimals negated raw ctx",
                  defaults=("",))


# ---------------------------------------------------------------- 개념 사전
#: 개념 → 코드 **배열**(우선순위 순). 앞엣것이 먼저 채택된다.
#: 코드명은 추측하지 않고 6개사(제조·은행·보험·증권) 표본에서 라벨과 함께 실측해 확정했다.
CONCEPTS = {
    # 재무상태표 (시점, kind="e")
    "assets_total":      ["ifrs-full_Assets"],
    "liabilities_total": ["ifrs-full_Liabilities"],
    "equity_total":      ["ifrs-full_Equity"],
    # 자본금 ≠ 자본총계. IAS 1.78(e)가 자본을 **납입자본·적립금·이익잉여금으로 분해해
    # 공시**하도록 요구하고, 그 납입자본에 해당하는 IFRS 택소노미 요소가 IssuedCapital이다.
    # 실측: 8개 평가대상사 **전부** 이 코드를 갖고 있고 값이 도메인 검수자 정답과 일치한다
    # (삼성전자 897,514 · SK하이닉스 3,657,652 · 기아 2,139,317 · KB금융 2,090,558 백만원).
    # ★ 이 개념이 없어서 "자본금"이 `_ALIAS`의 "자본"으로 흘러 **자본총계**를 답했다
    #   — 삼성전자 897,514백만원을 436,320,337백만원이라고 답했다(486배). 1-5·1-6 전멸(20문항).
    "issued_capital":    ["ifrs-full_IssuedCapital"],
    "inventories":       ["ifrs-full_Inventories"],
    # ★ 2026-08-23 추가 — `concepts.ALL`에 선언돼 있는데 **코드 배열이 없어 KeyError**였다.
    #   그래서 `finance.extract`가 `ppe` 2/70 · `intangibles` 2/70 · `retained_earnings`
    #   **0/70**으로 사실상 못 닿았다. `ppe`의 note 는 "IAS 1.54(a). 실측 70사"라고
    #   적혀 있었다 — 주석이 코드보다 앞서 있었다(§6-18).
    #   코드는 지어내지 않고 **연결·2025 사실의 라벨이 정확히 `유형자산`/`무형자산`/
    #   `이익잉여금`인 것을 70사 전수로 세어** 최빈 코드를 골랐다:
    #       유형자산     ifrs-full_PropertyPlantAndEquipment       2,209건 · 58사
    #       무형자산     ifrs-full_IntangibleAssetsAndGoodwill       565건 · 32사
    #                   ifrs-full_IntangibleAssetsOtherThanGoodwill  193건 · 10사
    #       이익잉여금   ifrs-full_RetainedEarnings                   274건 · 42사
    #   값 표본 대조: 삼성전자 유형자산 215.3조 · 이익잉여금 402.1조 · 현대차 유형자산 48.7조.
    "ppe":               ["ifrs-full_PropertyPlantAndEquipment"],
    "intangibles":       ["ifrs-full_IntangibleAssetsAndGoodwill",
                          "ifrs-full_IntangibleAssetsOtherThanGoodwill"],
    "retained_earnings": ["ifrs-full_RetainedEarnings"],

    # ★ 유동/비유동 구분 — **IAS 1.60**이 원칙으로 요구하는 표시다("entity shall present
    #   current and non-current assets, and current and non-current liabilities, as separate
    #   classifications"). 도메인 검수자 1차 질문셋 재무 5·7번이 이걸 묻는데 개념이 없어
    #   70개사 전부 답하지 못했다. 코드는 추측이 아니라 표본 6사 실측이다:
    #       ifrs-full_CurrentAssets       50건(라벨 `유동자산`)
    #       ifrs-full_CurrentLiabilities  50건(라벨 `유동부채`)
    "current_assets":      ["ifrs-full_CurrentAssets"],
    "current_liabilities": ["ifrs-full_CurrentLiabilities"],
    # **IAS 1.54(m)** 재무상태표 최소 표시 항목의 금융부채.
    # ★ 코드가 **둘로 갈린다** — 회사가 `단기차입금` 행을 어느 요소로 태깅했나에 따라 다르다.
    #   `CurrentLoansReceivedAndCurrentPortionOfNoncurrentLoansReceived`는
    #   '단기차입금 및 **유동성장기차입금**'이라 범위가 더 넓다(현대글로비스 실측:
    #   전자 7,035억 · 후자 8,007억 · 차이 972억이 유동성장기차입금).
    #
    #   순서는 전수 실측으로 정했다. 70개사에서 **재무상태표 본표 라벨 값과 일치하는
    #   코드**를 세었다(라벨 경로는 XBRL과 독립이라 심판으로 쓸 수 있다):
    #       ShorttermBorrowings          18사 일치
    #       CurrentLoansReceived…         3사 일치(에스엠·우리기술·현대글로비스)
    #       어느 쪽도 아님                 2사(이마트·알테오젠)
    #   그래서 앞엣것을 먼저 본다. 뒤엣것을 넣으면 **그 코드만 가진 7사**가 새로 열린다
    #   (HD현대중공업·HMM·세아베스틸지주·알테오젠·우리기술·한화솔루션·한화오션).
    #
    #   ★ **거래(trade-off)를 정직하게 적는다.** 뒤엣것을 넣은 전후 전수 감사:
    #       XBRL없음  31건 → **27건**   커버리지가 늘었다
    #       불일치     4건 → **7건**    알테오젠·파마리서치(본표 0인데 값이 나옴)·한미약품(135억 차)
    #   즉 커버리지와 정확도가 맞바뀐다. **커버리지를 택했다** — 못 뽑는 것보다
    #   뽑고 감사에서 드러나는 편이 낫다는 판단이다(`scale_audit`이 매번 이 3건을 잡는다).
    #   본표에 `단기차입금` 행이 아예 없는 회사(HD현대중공업 `차입금(유동)` ·
    #   한화오션 `단기차입금 합계`)는 라벨 경로가 없어 **검증 자체가 불가**하다.
    "short_term_borrowings": [
        "ifrs-full_ShorttermBorrowings",
        "ifrs-full_CurrentLoansReceivedAndCurrentPortionOfNoncurrentLoansReceived"],

    # 손익계산서 (기간, kind="d")
    "revenue":           ["ifrs-full_Revenue"],
    "cost_of_sales":     ["ifrs-full_CostOfSales"],
    "gross_profit":      ["ifrs-full_GrossProfit"],
    # dart_Total…을 앞에 둔다 — ifrs-full_Selling…은 라벨이 "재산관리비"인 사례가 있다(실측).
    "sga":               ["dart_TotalSellingGeneralAdministrativeExpenses",
                          "ifrs-full_SellingGeneralAndAdministrativeExpense"],
    "operating_income":  ["dart_OperatingIncomeLoss",
                          "ifrs-full_ProfitLossFromOperatingActivities",
                          "dart_OperatingIncomeInsurance"],      # 보험·은행 업종 코드(실측)
    "pretax_income":     ["ifrs-full_ProfitLossBeforeTax"],
    "net_income":        ["ifrs-full_ProfitLoss"],
    "net_income_owners": ["ifrs-full_ProfitLossAttributableToOwnersOfParent"],
    "eps_basic":         ["ifrs-full_BasicEarningsLossPerShare"],
    "eps_diluted":       ["ifrs-full_DilutedEarningsLossPerShare"],
    "tax_expense":       ["ifrs-full_IncomeTaxExpenseContinuingOperations"],
    # ★ **IAS 1.82(c)** — 손익계산서 최소 표시 항목: "지분법으로 회계처리하는 관계기업과
    #   공동기업의 당기순손익에 대한 지분". 실측 37건(라벨이 `지분법손실`로 오는 회사가 많다 —
    #   손실 표시가 기본 라벨이고 값의 부호가 손익을 가른다).
    #   포괄손익 쪽(`ShareOfOtherComprehensiveIncome…` 55건)은 **다른 항목**이라 넣지 않는다.
    "equity_method_income": [
        "ifrs-full_ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod"],

    # 금융업 top-line. 도메인 확인(2026-07-31): 금융 8개사 중 **5개사는 ifrs-full_Revenue가
    # 그대로 있다** — 라벨만 "영업수익"·"수익(매출액)"이다(신한지주 64.7조 · 하나금융지주 68.9조
    # · 삼성생명 37.4조 · 삼성화재 23.9조 · 미래에셋증권 29.3조).
    # 진짜로 없는 곳은 **KB금융·우리금융지주·메리츠금융지주 3곳뿐**이고, 그때만 아래를 쓴다.
    "net_interest_income": ["ifrs-full_InterestRevenueExpense"],
    "net_fee_income":      ["ifrs-full_FeeAndCommissionIncomeExpense"],
    "insurance_revenue":   ["dart_OperatingIncomeInsurance", "ifrs-full_InsuranceRevenue"],
    # ★ IFRS 17 — 표준 태그를 그대로 쓴다(2026-08-26 전수 확인). 정본표로는 못 집는
    #   값이다(그 표는 행 라벨이 전부 공백 · `concepts.csm` 주석 참조).
    "surrender_reserve":   ["dart_SurrenderValueReserve",
                            "dart_SurrenderValueReserveToBeAdded"],
    "cash":                ["ifrs-full_CashAndCashEquivalents"],
    "nci":                 ["ifrs-full_NoncontrollingInterests"],

    # 현금흐름표 (기간, kind="d")
    "cf_operating":      ["ifrs-full_CashFlowsFromUsedInOperatingActivities"],
    "cf_investing":      ["ifrs-full_CashFlowsFromUsedInInvestingActivities"],
    "cf_financing":      ["ifrs-full_CashFlowsFromUsedInFinancingActivities"],

    # ★ **IAS 1.106** 자본변동표 — 기초 자본총계. DART 확장 태그다(표준 IFRS에는 없다).
    #   실측 298건인데 그 대부분은 **자본 구성항목별 기초잔액**이다(자본금·이익잉여금…).
    #   우리가 원하는 것은 **자본총계의 기초잔액** 하나뿐이라 `_LABEL_MUST`로 가른다.
    "equity_begin":      ["dart_EquityAtBeginningOfPeriod"],
}

#: 재무상태표 개념은 시점(e), 손익·현금흐름은 기간(d).
_INSTANT = {"assets_total", "liabilities_total", "equity_total", "issued_capital",
            "inventories", "cash", "nci",
            # 2026-08-26 — 둘 다 재무상태표 항목이라 시점(e)이다.
            "surrender_reserve",
            # 재무상태표 항목이므로 시점(instant)이다.
            "current_assets", "current_liabilities", "short_term_borrowings",
            # 2026-08-23 추가. 셋 다 재무상태표 항목이라 시점(e)이다 — 실측으로 확인했다.
            "ppe", "intangibles", "retained_earnings"}
#: ★ `equity_begin`은 시점 잔액처럼 보이지만 **기간(d)으로 태깅된다**(실측: 36건 전부 `d`).
#:   자본변동표가 '기초→변동→기말'의 기간 표라서다. `_INSTANT`에 넣었다가 못 찾았다.

#: top-line 후보 — 앞엣것부터 본다. 도메인 확인 결과 대부분 `revenue` 하나로 끝난다.
TOP_LINE = ("revenue", "insurance_revenue", "net_interest_income")


# ---------------------------------------------------------------- 파싱
def _num(s):
    """표시 숫자 → float. 콤마·괄호음수·△·공백 처리. 숫자가 아니면 None."""
    s = _TAG.sub("", s or "").replace("　", " ").strip()
    if not s or s in ("-", "–", "—"):
        return None
    neg = s.startswith("(") and s.endswith(")") or s.startswith(("△", "▲", "−", "-"))
    t = re.sub(r"[(),\s△▲−]", "", s).lstrip("-")
    if not re.fullmatch(r"\d+(\.\d+)?", t):
        return None
    v = float(t)
    return -v if neg else v


def _scale(decimals):
    """ADECIMAL → 원 단위 배수. `-6`이면 표시값이 백만원 단위라는 뜻(실측 확인).

    XBRL의 decimals는 '10^n 자리까지 정확'을 뜻하고, 공시 표는 그 단위로 나눠 표시한다.
    실측 대조: 삼성전자 자산 표시 566,942,110 · ADECIMAL=-6 → 566.9조원. 맞다.
    """
    if decimals in (None, "", "INF"):
        return 1.0
    try:
        d = int(decimals)
    except ValueError:
        return 1.0
    return 10.0 ** (-d) if d < 0 else 1.0


def _scope_of(ctx):
    if "ifrs-full_ConsolidatedMember" in ctx:
        return CONSOLIDATED
    if "ifrs-full_SeparateMember" in ctx:
        return SEPARATE
    return UNSPECIFIED


def _period_of(ctx):
    m = _CTX.match(ctx or "")
    return (int(m.group(2)), m.group(3)) if m else (None, None)


def facts(corp, year=None, doc_subtype="annual"):
    """기업의 해당 보고서에서 XBRL 사실을 전부 뽑는다. 빈 튜플이면 태그가 없는 문서다."""
    docs = list(store.docs(corp=corp, doc_subtype=doc_subtype, base_year=year)) \
        if year else list(store.docs(corp=corp, doc_subtype=doc_subtype))
    if not docs:
        return ()
    text = "\n".join(source.text(d) for d in docs)
    return facts_of_text(text)


def facts_of_text(text):
    out = []
    for rowhtml in _ROW.findall(text):
        if "ACODE=" not in rowhtml:
            continue
        # 행의 첫 텍스트 셀 = 계정 라벨. 감사 로그에 남겨 추적 가능하게 한다.
        label = ""
        for raw in _TEXT.findall(rowhtml):
            t = _TAG.sub("", raw).replace("　", " ").strip()
            if t and _num(t) is None:
                label = t
                break
        for attrs, val in _CELL.findall(rowhtml):
            a = dict(_ATTR.findall(attrs))
            code = a.get("ACODE") or ""
            ctx = a.get("ACONTEXT") or ""
            v = _num(val)
            if v is None:
                continue
            yr, kind = _period_of(ctx)
            sc = _scale(a.get("ADECIMAL"))
            out.append(Fact(code=code, label=label, scope=_scope_of(ctx), year=yr,
                            kind=kind, value=v, krw=v * sc, decimals=a.get("ADECIMAL"),
                            negated=a.get("ANEGATED") == "Y",
                            raw=_TAG.sub("", val).strip(), ctx=ctx))
    return tuple(out)


# ---------------------------------------------------------------- 조회
def pick(fs, concept, scope=CONSOLIDATED, year=None):
    """개념 1건 → Fact. 코드 배열 순서대로 찾고, 먼저 걸리는 코드를 쓴다.

    같은 코드·같은 컨텍스트가 여러 번 나오면(요약표와 본표에 중복 게재) 값이 같으므로
    첫 건을 쓴다. 값이 갈리면 그건 호출자가 알아야 할 신호라 `candidates()`로 노출한다.

    ★ **연도를 안 주면 최신 사업연도를 쓴다.** 한 보고서에는 당기·전기·전전기가 함께
      실려 있어서, 종전처럼 문서 등장 순 첫 건을 쓰면 **전기 값이 나올 수 있다.**
      실측: HD현대중공업 자본금이 2024년 443,865,580천원으로 나왔다
      (2025년은 524,806,125천원이고 도메인 검수자 정답도 이 값이다). 등장 순은
      2024·2023·2022·2025 순이라 최신이 네 번째였다. 자본금만의 문제가 아니라
      **연도를 넘기지 않는 모든 호출**에 걸려 있던 결함이다.
    """
    cs = candidates(fs, concept, scope, year)
    if not cs:
        return None
    if year is None:
        ys = [c.year for c in cs if c.year is not None]
        if ys:
            newest = max(ys)
            return next(c for c in cs if c.year == newest)
    return cs[0]


#: 개념 → 인정할 라벨. **같은 코드가 다른 뜻으로도 쓰이는 경우에만** 건다.
#: 실측(현대자동차): `ifrs-full_IssuedCapital`이 자본변동표의 **열 축**으로도 붙어
#: 라벨이 `지배기업소유주지분`인 셀이 셋 더 나온다(1,144,158 · 3,773,429 · 19,183,623).
#: 라벨을 안 보면 그중 아무거나 자본금이라고 답하게 된다 — 정답은 `자본금` 1,488,993이다.
#: 개념별 **차원(축) 필터** — 이 정규식에 걸리는 컨텍스트는 버린다.
#:
#: ## 근거: XBRL Dimensions — 축이 없는 사실이 합계다
#: 자본변동표는 자본을 구성요소로 쪼개 표시하므로(IAS 1.106) 같은 코드가
#: `…ComponentsOfEquityAxis_…`를 달고 **자본금·주식발행초과금·이익잉여금·비지배지분**
#: 등으로 여러 번 나온다. 실측(삼성전자 2025 연결) 7건이 전부 같은 라벨
#: `2025.01.01 (기초자본)`이라 **라벨로는 못 가른다**:
#:     897,514      …ConsolidatedMember_ifrs-full_ComponentsOfEquityAxis_…   자본금
#:     354,749,604  …ConsolidatedMember                                      **자본총계**
#: 차원이 적용되지 않은 사실이 그 축 전체의 집계값이라는 것은 XBRL 차원 모델의 정의다.
#:
#: ★ **이 필터를 전 개념에 걸지 않는다.** 70개사 전수 시뮬레이션 결과 기존 개념
#:   1,536건 중 1,499건은 그대로지만 **20건이 값이 바뀌고 17건이 사라졌다**
#:   (미래에셋증권 영업이익 11,958억→19,151억 · POSCO홀딩스 단기차입금 74,320억→없음).
#:   어느 쪽이 맞는지 검증하지 못했으므로 적용하지 않는다 — 검증하지 못한 원리는 쓰지 않는다.
_AXIS_EXCLUDE = {
    "equity_begin": re.compile(r"Axis_.*Axis_"),
}

#: **코드 단위** 라벨 조건 — 그 코드가 붙은 셀의 라벨이 이 정규식에 맞아야 받는다.
#: (`_LABEL_MUST`는 개념 단위라 같은 개념의 다른 코드까지 걸러 버린다. 이건 코드 하나만 본다.)
#:
#: ## 왜 필요한가 (2026-08-12 전수 실측)
#: `ifrs-full_CurrentLoansReceivedAndCurrentPortionOfNoncurrentLoansReceived`는
#: 회사마다 **다른 행**에 붙는다. 축 없는 연결 당기 사실의 라벨을 전수로 뽑아 보면 갈린다:
#:     현대글로비스 800,732,226,936  `단기차입금`                        ← 우리가 원하는 것
#:     에스엠         5,166,699,000  `단기차입금`
#:     우리기술      33,818,571,027  `단기차입금`
#:     알테오젠         972,248,000  `유동성 금융기관 차입금(사채 제외)`   ← **유동성장기차입금**이다
#:     LG유플러스   342,500,000,000  `유동성 장기차입금`
#:     한화솔루션 6,320,150,000,000  `유동성 금융기관차입금`
#:     세아베스틸지주 256,326,878,029  `유동 차입금`
#:     HMM         68,592,000,000  `차입금`
#: 알테오젠 연결 재무상태표는 `단기차입금 0 / 유동성장기차입금 972,248,000`이다.
#: 코드만 믿고 받으면 **0이어야 할 답에 유동성장기차입금을 낸다**(파마리서치도 같다).
#: 라벨이 `단기차입금`으로 시작할 때만 받으면 정확히 갈린다.
_CODE_LABEL_MUST = {}

_LABEL_MUST = {
    "issued_capital": re.compile(r"자본금|납입자본"),
    # ★ **라벨이 `단기차입금`인 행만 받는다** (2026-08-12 전수 실측).
    #   코드만 믿으면 **다른 계정을 단기차입금으로 답한다.** 축 없는 연결 당기 사실의
    #   라벨을 70개사에서 전수로 뽑아 보면 두 코드 모두 갈린다:
    #       `ShorttermBorrowings` 30사 중 23사가 `단기차입금`, 7사는 다르다 —
    #           HD현대일렉트릭 `유동성 장기차입금` 18,836원 · 셀트리온 `차입금명칭 합계`
    #           현대글로비스 `유동 금융기관 차입금 및 비유동…`(주석 명세, 7,035억)
    #       `CurrentLoansReceived…` 11사 중 3사가 `단기차입금`, 나머지는
    #           알테오젠 `유동성 금융기관 차입금(사채 제외)` 등 **유동성장기차입금**이다.
    #   실제 피해: 알테오젠·파마리서치는 연결 재무상태표 `단기차입금`이 **0**인데
    #   유동성장기차입금(972,248,000 · 42,439,992)을 답했다.
    #   라벨 조건을 걸면 현대글로비스가 주석값 7,035억 대신 본표값 8,007억으로 바로잡힌다.
    "short_term_borrowings": re.compile(r"^\s*단기차입금"),
    # `dart_EquityAtBeginningOfPeriod`는 **자본 구성항목마다** 붙는다(실측 298건).
    # 우리가 원하는 것은 자본총계의 기초잔액이라 라벨에 `기초`가 있는 것만 받는다.
    # (라벨 실측: `2025.01.01 (기초자본)`)
    "equity_begin": re.compile(r"기초"),
}


#: 연결/별도 축. 모든 컨텍스트가 이 축 **하나는** 갖는다(실측: 축 1개짜리 3,794건이
#: 전부 이 축이고, 축 0개인 컨텍스트는 없다). 따라서 **이 축 말고 다른 축이 붙어 있으면
#: 그 사실은 합계가 아니라 차원 값**이다.
_SCOPE_AXIS = "ConsolidatedAndSeparateFinancialStatementsAxis"
_AXIS = re.compile(r"[A-Za-z0-9\-]+_[A-Za-z]+Axis")


#: ★ 귀속 실험용 스위치(2026-08-13). step13에서 검색(k·절단)과 이 축 정렬을 한 번에
#:   바꿔 `-1.7%p`의 원인을 가릴 수 없었다. `A2_NO_DIM_SORT=1`이면 정렬만 끄고 잰다.
_NO_DIM_SORT = bool(os.environ.get("A2_NO_DIM_SORT"))


def dim_count(ctx):
    """연결/별도 축을 뺀 **추가 차원 수**. 0이면 합계 사실이다."""
    return sum(1 for a in _AXIS.findall(ctx or "") if _SCOPE_AXIS not in a)


def candidates(fs, concept, scope=CONSOLIDATED, year=None):
    """해당 개념에 걸리는 Fact 전부 — 코드 우선순위 순, **합계 사실이 먼저** 온다.

    ## ★ 차원(축) 있는 사실을 총계로 답하던 오추출 (2026-08-12 실측으로 발견·수정)

    XBRL은 같은 코드를 **부문별·지역별·만기별**로 여러 번 태깅한다. 축이 붙지 않은
    사실이 그 축 전체의 집계값이라는 것은 XBRL 차원 모델의 정의다. 종전에는 등장 순으로
    첫 건을 집었고, 차원 값이 본표보다 앞에 나오는 회사에서 **부문 하나를 총계로 답했다.**
    산술로 확인된 두 건:

        미래에셋증권 영업이익(백만원)
            축3  1,195,803   부문별            ← 종전에 이걸 집었다
            축2  2,415,293   부문 소계
            축2   (500,189)  조정
            축1  1,915,104   **총계**           2,415,293 − 500,189 = 1,915,104 ✓

        삼성생명 매출액(백만원)
            축2  37,393,629  지역별(국내)       ← 종전에 이걸 집었다
            축2     202,300  지역별(해외)
            축1  37,595,929  **총계**           37,393,629 + 202,300 = 37,595,929 ✓

    두 경우 다 구성요소의 합이 정확히 축 없는 값과 일치한다 — 추측이 아니라 검산이다.

    ## 왜 '제외'가 아니라 '정렬'인가

    차원 값을 **버리면** 본표에 없고 주석에만 있는 회사에서 값이 통째로 사라진다
    (전수 시뮬레이션: 17건 소실 — POSCO홀딩스 단기차입금 등 만기별 주석만 있는 경우).
    그래서 합계를 **우선**하고, 없을 때만 차원 값으로 물러난다. 정직한 폴백이다.
    """
    codes = CONCEPTS.get(concept)
    if not codes:
        raise KeyError(f"미등록 개념: {concept} — CONCEPTS에 코드 배열을 먼저 정의할 것")
    kind = "e" if concept in _INSTANT else "d"
    must = _LABEL_MUST.get(concept)
    axis_bad = _AXIS_EXCLUDE.get(concept)
    out = []
    for code in codes:
        for f in fs:
            if f.code != code or f.kind != kind:
                continue
            if scope and f.scope != scope:
                continue
            if year and f.year != year:
                continue
            if must and not must.search(f.label or ""):
                continue
            cmust = _CODE_LABEL_MUST.get(f.code)
            if cmust and not cmust.search(f.label or ""):
                continue                  # 같은 코드가 다른 행에 붙는 경우를 가른다
            if axis_bad and axis_bad.search(f.ctx or ""):
                continue                      # 차원이 붙은 사실 = 구성요소. 합계가 아니다.
            out.append(f)
        if out:
            break            # 앞선 코드가 잡히면 뒤 코드는 보지 않는다(우선순위)
    # ★ **스케일 신뢰도가 차원보다 먼저다** (2026-08-12, 축 정렬만 넣었다가 잡은 버그).
    #
    #   `ADECIMAL=0`은 "원 단위"가 아니라 **스케일 미표기**다. 같은 값이 두 번 실리는데
    #   한쪽만 스케일이 붙는 경우가 있다 — 이마트 단기차입금 실측:
    #       3,111,389  ADECIMAL=-6 (추가축 1)  → 3.11조원   ← 재무상태표 본표와 일치
    #       3,111,389  ADECIMAL=0  (추가축 0)  → 311만원    ← 스케일이 날아갔다
    #   본표 원문이 `단기차입금 3,111,389,126,654원`이므로 -6 쪽이 맞다.
    #   축만 보고 정렬하면 축 없는 `ADECIMAL=0`을 집어 **100만분의 1**로 답한다
    #   (셀트리온 3.24조→32억 · 이마트 3.11조→0억 등 6건에서 실제로 그랬다).
    #
    #   축 없는 사실 782건 중 **325건(42%)**이 `ADECIMAL=0`이라 무시할 수 없는 규모다.
    #   그래서 스케일이 명시된 사실을 먼저 쓰고, 그 안에서 합계(추가 차원 0)를 먼저 쓴다.
    #   안정 정렬이라 두 키가 같으면 종전 등장 순이 유지된다.
    #   ★ **`ADECIMAL=0`을 후순위로 두는 규칙은 시도했다가 폐기했다**(2026-08-12).
    #     그렇게 정렬하니 `자산=부채+자본`이 **68사 → 45사**로 무너지고 테스트 2개가 깨졌다.
    #     `ADECIMAL=0`이 진짜 원 단위인 회사가 많다는 뜻이다 — 이마트처럼 스케일이
    #     날아간 경우와 구분할 방법을 아직 못 찾았다. 검증 못 한 규칙은 쓰지 않는다.
    #     남은 오염(축 없는 사실 782건 중 `ADECIMAL=0` 325건)은 아래 `scale_conflict()`로
    #     **드러내기만** 한다. 조용히 틀린 값을 주는 것보다 낫다.
    out = _drop_lost_scale(out)
    if not _NO_DIM_SORT:
        out.sort(key=lambda f: dim_count(f.ctx))
    return out


def _drop_lost_scale(facts_):
    """배율 표기가 빠진 합계 사실을 **구성요소와 대조해** 바로잡는다.

    ## 두 가지 증거로만 고친다 — 둘 다 '판단'이 아니라 '사실'이다

    (1) **같은 표시값이 서로 다른 `ADECIMAL`로 실렸다.**
        같은 숫자를 두 번 적은 것이므로 단위가 다를 수 없다.
            이마트 단기차입금  3,111,389 (ADECIMAL=-6, 축1)  -> 3.11조
                             3,111,389 (ADECIMAL= 0, 축0)  -> 311만원
            본표 `3,111,389,126,654원`이므로 -6이 맞다.

    (2) **구성요소 표시값의 합 == 합계 표시값인데 배율만 다르다.**
        같은 단위로 적어야 합이 맞아떨어지므로, 합계의 `0`은 표기 누락이다.
            에스엠 단기차입금  3,000,000 + 0 + 2,166,699 = 5,166,699 (ADECIMAL=-3, 축2)
                             합계                        5,166,699 (ADECIMAL= 0, 축0)
            본표 `5,166,699,000원`이므로 -3이 맞다.

    ## 왜 이 조건이 안전한가

    합계가 **진짜 원 단위**(`ADECIMAL=0`이 옳은 경우)라면 표시 숫자가 구성요소보다
    훨씬 크다(원 vs 백만원). 그러면 (1)의 값 일치도, (2)의 합 일치도 성립하지 않는다.
    즉 조건이 성립하는 순간 이미 "같은 단위로 적혔다"는 뜻이다.

    느슨한 규칙은 두 번 시도했다 폐기했다 — 되살리지 말 것:
        `ADECIMAL=0`을 일괄 후순위 정렬   `자산=부채+자본` 68사 -> 45사
        같은 개념의 비-0 배율을 무조건 물려받기  68사 -> 54사 · `기초자본` 68사 -> 37사
    """
    zeros = [f for f in facts_ if str(f.decimals) == "0"]
    if not zeros:
        return facts_
    # 구성요소(추가 차원이 있는 사실)의 배율 — 한 종류로 일관될 때만 근거가 된다.
    #
    # ★ **연도별로 나눈다** (2026-08-15 전수 감사로 잡았다). `pick()`이 `year=None`으로
    #   부르므로 `facts_`에는 당기·전기가 섞여 있다. 종전에는 전 연도 구성요소를
    #   한꺼번에 더해서 합이 어느 해와도 안 맞았다 — 이 함수 docstring이 예로 든
    #   **에스엠 단기차입금이 실제로는 안 고쳐지고 있었다**:
    #       2025 구성요소 3,000,000 + 0 + 2,166,699 = 5,166,699  (합계와 일치)
    #       전 연도 합산                            = 10,573,695 (어느 합계와도 불일치)
    #   그래서 `5,166,699`(=51.7억)가 **516만원**으로 나갔다.
    comps = {}
    for f in facts_:
        if dim_count(f.ctx) > 0 and str(f.decimals) not in ("0", "None", "INF"):
            comps.setdefault(f.year, []).append(f)
    per_year = {}
    for y, cs in comps.items():
        decs = {str(x.decimals) for x in cs}
        if len(decs) == 1:
            per_year[y] = (decs.pop(), round(sum(x.value for x in cs), 6))

    same_val = {round(f.value, 6) for f in facts_ if str(f.decimals) not in ("0", "None")}
    out = []
    for f in facts_:
        if str(f.decimals) != "0":
            out.append(f)
            continue
        v = round(f.value, 6)
        # (1) 같은 표시값이 배율과 함께 존재한다 -> 이 셀은 버린다(그쪽을 쓰면 된다).
        if v in same_val:
            continue
        # (2) **같은 해** 구성요소 합과 표시값이 같다 -> 배율을 물려받는다.
        dec, comp_sum = per_year.get(f.year, (None, None))
        if dec and comp_sum is not None and v and abs(comp_sum - v) < 1e-6:
            out.append(f._replace(krw=f.value * _scale(dec), decimals=dec))
            continue
        out.append(f)
    return out or facts_


def scale_conflict(fs, concept, scope=CONSOLIDATED, year=None):
    """같은 개념·같은 값인데 **ADECIMAL이 갈리는** 사실이 있나 — (있음, 배수) 또는 (False, None).

    이마트 단기차입금이 그 예다:
        3,111,389  ADECIMAL=-6  → 3.11조원   ← 재무상태표 본표와 일치
        3,111,389  ADECIMAL=0   → 311만원
    같은 표시 숫자에 스케일만 다르면 **한쪽은 반드시 틀렸다.** 판별 규칙을 아직
    세우지 못했으므로(위 주석 참조) 값을 바꾸지 않고 신호만 돌려준다.
    호출자가 이 신호를 보고 사람에게 확인을 요청하거나 값을 유보할 수 있다.
    """
    cs = candidates(fs, concept, scope, year)
    by_val = {}
    for f in cs:
        by_val.setdefault(round(f.value, 6), set()).add(str(f.decimals))
    for v, decs in by_val.items():
        if len(decs) > 1:
            return True, sorted(decs)
    return False, None


def series(fs, concept, scope=CONSOLIDATED):
    """{연도: 값} — 한 보고서에 당기·전기·전전기가 함께 실려 있어 3개년이 한 번에 나온다."""
    out = {}
    for f in candidates(fs, concept, scope, year=None):
        if f.year is not None and f.year not in out:
            out[f.year] = f.value
    return dict(sorted(out.items()))


def scopes(fs):
    """이 문서가 연결·별도를 각각 갖고 있는가."""
    return {s for s in (f.scope for f in fs)}


def check_identity(fs, scope=CONSOLIDATED, year=None):
    """IAS 1.54 항등식 `자산 = 부채 + 자본` 확인. (오차, 자산, 부채, 자본) 또는 None.

    라벨 파싱 경로는 이 항등식에 1% 허용오차를 뒀지만, XBRL 경로는 실측상
    **오차 0**으로 맞는다(삼성전자 566,942,110 = 130,621,773 + 436,320,337).
    """
    a = pick(fs, "assets_total", scope, year)
    l = pick(fs, "liabilities_total", scope, year)
    e = pick(fs, "equity_total", scope, year)
    if not (a and l and e):
        return None
    return (abs(l.value + e.value - a.value), a.value, l.value, e.value)


if __name__ == "__main__":
    import sys

    corp = sys.argv[1] if len(sys.argv) > 1 else "삼성전자"
    fs = facts(corp, 2025)
    print(f"{corp} FY2025 · XBRL 사실 {len(fs):,}건 · 범위 {sorted(scopes(fs))}")
    if not fs:
        print("  XBRL 태그 없음 — 라벨 파서로 폴백해야 하는 기업")
        raise SystemExit
    for scope in (CONSOLIDATED, SEPARATE):
        idn = check_identity(fs, scope, 2025)
        print(f"\n── {scope} ──  IAS 1 항등식 오차 "
              + (f"{idn[0]:,.0f}" if idn else "확인 불가"))
        for cid in ("revenue", "cost_of_sales", "gross_profit", "sga", "operating_income",
                    "net_income", "eps_basic", "cf_operating", "cf_investing",
                    "cf_financing", "assets_total", "inventories"):
            s = series(fs, cid, scope)
            f = pick(fs, cid, scope, 2025)
            if not s:
                print(f"   {cid:18s} 없음")
                continue
            yrs = " · ".join(f"{y} {v:,.0f}" for y, v in s.items())
            print(f"   {cid:18s} {yrs}    ← {f.label[:22] if f else ''}")
