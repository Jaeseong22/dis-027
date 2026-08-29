"""XBRL 사실 추출 — 공시 XML 표 셀에 박힌 표준 태그를 그대로 읽는다."""
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

_CTX = re.compile(r"^(C|P|BP)FY(\d{4})([de])([A-Z]*)")

#: ACONTEXT 기간 접두사 → 기간 구분. 전 코퍼스 실측(periodic 1,466파일 · 예외 0건):
#:   base_month=12 → FY / 6 → HY·HYA·HYQ / 3 → FQ·FQA·FQQ / 9 → TQ·TQA·TQQ
#: 분기·반기는 같은 행에 3개월(…Q)과 누적(…A)이 나란히 실린다. 1분기는 둘이 같다.
CUMULATIVE = "누적"
THREE_MONTH = "3개월"
INSTANT = "시점"
ANNUAL = "연간"
_SPAN = {"FY": ANNUAL, "": None,
         "HY": INSTANT, "HYA": CUMULATIVE, "HYQ": THREE_MONTH,
         "FQ": INSTANT, "FQA": CUMULATIVE, "FQQ": THREE_MONTH,
         "TQ": INSTANT, "TQA": CUMULATIVE, "TQQ": THREE_MONTH}

CONSOLIDATED = "연결"
SEPARATE = "별도"
UNSPECIFIED = "미표기"

Fact = namedtuple("Fact",
                  "code label scope year kind value krw decimals negated raw ctx span",
                  defaults=("", None))

# ---------------------------------------------------------------- 개념 사전
#: 개념 → 코드 **배열**(우선순위 순). 앞엣것이 먼저 채택된다.
CONCEPTS = {
    # 재무상태표 (시점, kind="e")
    "assets_total":      ["ifrs-full_Assets"],
    "liabilities_total": ["ifrs-full_Liabilities"],
    "equity_total":      ["ifrs-full_Equity"],
    # 자본금 ≠ 자본총계. IAS 1.78(e)가 자본을 **납입자본·적립금·이익잉여금으로 분해해
    # 공시**하도록 요구하고, 그 납입자본에 해당하는 IFRS 택소노미 요소가 IssuedCapital이다.
    "issued_capital":    ["ifrs-full_IssuedCapital"],
    "inventories":       ["ifrs-full_Inventories"],
    "ppe":               ["ifrs-full_PropertyPlantAndEquipment"],
    "intangibles":       ["ifrs-full_IntangibleAssetsAndGoodwill",
                          "ifrs-full_IntangibleAssetsOtherThanGoodwill"],
    "retained_earnings": ["ifrs-full_RetainedEarnings"],

    "current_assets":      ["ifrs-full_CurrentAssets"],
    "current_liabilities": ["ifrs-full_CurrentLiabilities"],
    # **IAS 1.54(m)** 재무상태표 최소 표시 항목의 금융부채.
    "short_term_borrowings": [
        "ifrs-full_ShorttermBorrowings",
        "ifrs-full_CurrentLoansReceivedAndCurrentPortionOfNoncurrentLoansReceived",
        # 위 둘이 없는 회사가 있다(현대제철·크래프톤·HD현대중공업 등).
        # 라벨 게이트(`_LABEL_MUST` `^단기차입금`)가 유동성장기차입금을 막아 안전하다.
        # 전수 A/B: 새로 4건 · 사라짐 0 · 바뀜 0.
        "ifrs-full_OtherCurrentBorrowingsAndCurrentPortionOfOtherNoncurrentBorrowings"],

    # 손익계산서 (기간, kind="d")
    "revenue":           ["ifrs-full_Revenue"],
    "cost_of_sales":     ["ifrs-full_CostOfSales"],
    "gross_profit":      ["ifrs-full_GrossProfit"],
    "sga":               ["dart_TotalSellingGeneralAdministrativeExpenses",
                          "ifrs-full_SellingGeneralAndAdministrativeExpense"],
    "operating_income":  ["dart_OperatingIncomeLoss",
                          "ifrs-full_ProfitLossFromOperatingActivities",
                          "dart_OperatingIncomeInsurance"],      # 보험·은행 업종 코드(실측)
    "pretax_income":     ["ifrs-full_ProfitLossBeforeTax"],
    "net_income":        ["ifrs-full_ProfitLoss"],
    "net_income_owners": ["ifrs-full_ProfitLossAttributableToOwnersOfParent"],
    # ★ `oci`·`total_ci` 에 코드가 없어 XBRL 경로가 이기면 값이 사라졌다.
    #   연간에서도 68/70사가 이 구멍에 걸려 있었다(oci 2사 · total_ci 1사만 나왔다).
    #   전 코퍼스 실측: `기타포괄손익` 1,316건 · `총포괄손익` 1,310 + `총포괄이익` 334.
    "oci":               ["ifrs-full_OtherComprehensiveIncome"],
    "total_ci":          ["ifrs-full_ComprehensiveIncome"],
    "eps_basic":         ["ifrs-full_BasicEarningsLossPerShare"],
    "eps_diluted":       ["ifrs-full_DilutedEarningsLossPerShare"],
    "tax_expense":       ["ifrs-full_IncomeTaxExpenseContinuingOperations"],
    "equity_method_income": [
        "ifrs-full_ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod"],
    # ★ `…OfAssociatesAccountedForUsingEquityMethod`(관계기업 전용)을 더하면 값 11건이
    #   새로 생기지만 **효성중공업 2건이 뒤집힌다**. 두 코드가 같은 금액을 다른 부호로
    #   담는다 — 본표 `지분법손실 2,148,885,480`(양수 표기) ↔ 주석 `-2,149백만원`.
    #   부호 규약을 확정하기 전에는 더하지 않는다.

    "net_interest_income": ["ifrs-full_InterestRevenueExpense"],
    "net_fee_income":      ["ifrs-full_FeeAndCommissionIncomeExpense"],
    "insurance_revenue":   ["dart_OperatingIncomeInsurance", "ifrs-full_InsuranceRevenue"],
    "surrender_reserve":   ["dart_SurrenderValueReserve",
                            "dart_SurrenderValueReserveToBeAdded"],
    "cash":                ["ifrs-full_CashAndCashEquivalents"],
    "nci":                 ["ifrs-full_NoncontrollingInterests"],

    # 현금흐름표 (기간, kind="d")
    "cf_operating":      ["ifrs-full_CashFlowsFromUsedInOperatingActivities"],
    "cf_investing":      ["ifrs-full_CashFlowsFromUsedInInvestingActivities"],
    "cf_financing":      ["ifrs-full_CashFlowsFromUsedInFinancingActivities"],

    "equity_begin":      ["dart_EquityAtBeginningOfPeriod"],
}

#: 재무상태표 개념은 시점(e), 손익·현금흐름은 기간(d).
_INSTANT = {"assets_total", "liabilities_total", "equity_total", "issued_capital",
            "inventories", "cash", "nci",
            "surrender_reserve",
            # 재무상태표 항목이므로 시점(instant)이다.
            "current_assets", "current_liabilities", "short_term_borrowings",
            "ppe", "intangibles", "retained_earnings"}

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
    """ADECIMAL → 원 단위 배수. `-6`이면 표시값이 백만원 단위라는 뜻(실측 확인)."""
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
    """ACONTEXT → (연도, 기간/시점, 기간구분)."""
    m = _CTX.match(ctx or "")
    if not m:
        return (None, None, None)
    return (int(m.group(2)), m.group(3), _SPAN.get(m.group(4)))


def facts(corp, year=None, doc_subtype="annual", base_month=None):
    """기업의 해당 보고서에서 XBRL 사실을 전부 뽑는다. 빈 튜플이면 태그가 없는 문서다.

    ★ `base_month`를 넘겨라. `doc_subtype="quarter"`는 1분기와 3분기를 둘 다 뜻해서,
      월을 안 주면 두 문서의 사실이 섞여 `pick`이 1분기 값을 3분기 답으로 돌려준다.
    """
    kw = {"corp": corp, "doc_subtype": doc_subtype}
    if year:
        kw["base_year"] = year
    if base_month is not None:
        kw["base_month"] = base_month
    docs = list(store.docs(**kw))
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
            yr, kind, span = _period_of(ctx)
            sc = _scale(a.get("ADECIMAL"))
            out.append(Fact(code=code, label=label, scope=_scope_of(ctx), year=yr,
                            kind=kind, value=v, krw=v * sc, decimals=a.get("ADECIMAL"),
                            negated=a.get("ANEGATED") == "Y",
                            raw=_TAG.sub("", val).strip(), ctx=ctx, span=span))
    return tuple(out)

# ---------------------------------------------------------------- 조회


def pick(fs, concept, scope=CONSOLIDATED, year=None, span=None):
    """개념 1건 → Fact. 코드 배열 순서대로 찾고, 먼저 걸리는 코드를 쓴다."""
    cs = candidates(fs, concept, scope, year, span)
    if not cs:
        return None
    if year is None:
        ys = [c.year for c in cs if c.year is not None]
        if ys:
            newest = max(ys)
            return next(c for c in cs if c.year == newest)
    return cs[0]

#: 개념 → 인정할 라벨. **같은 코드가 다른 뜻으로도 쓰이는 경우에만** 건다.
_AXIS_EXCLUDE = {
    "equity_begin": re.compile(r"Axis_.*Axis_"),
}

#: **코드 단위** 라벨 조건 — 그 코드가 붙은 셀의 라벨이 이 정규식에 맞아야 받는다.
#: (`_LABEL_MUST`는 개념 단위라 같은 개념의 다른 코드까지 걸러 버린다. 이건 코드 하나만 본다.)
#:
_CODE_LABEL_MUST = {}

_LABEL_MUST = {
    "issued_capital": re.compile(r"자본금|납입자본"),
    "short_term_borrowings": re.compile(r"^\s*단기차입금"),
    "equity_begin": re.compile(r"기초"),
}

_SCOPE_AXIS = "ConsolidatedAndSeparateFinancialStatementsAxis"
_AXIS = re.compile(r"[A-Za-z0-9\-]+_[A-Za-z]+Axis")

_NO_DIM_SORT = bool(os.environ.get("A2_NO_DIM_SORT"))


def dim_count(ctx):
    """연결/별도 축을 뺀 **추가 차원 수**. 0이면 합계 사실이다."""
    return sum(1 for a in _AXIS.findall(ctx or "") if _SCOPE_AXIS not in a)


def candidates(fs, concept, scope=CONSOLIDATED, year=None, span=None):
    """해당 개념에 걸리는 Fact 전부 — 코드 우선순위 순, **합계 사실이 먼저** 온다.

    `span`을 주면 그 기간 구분만 남긴다(분기·반기의 3개월/누적을 가른다).
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
            if span and f.span != span:
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
    out = _drop_lost_scale(out)
    if not _NO_DIM_SORT:
        out.sort(key=lambda f: dim_count(f.ctx))
    return out


def _drop_lost_scale(facts_):
    """배율 표기가 빠진 합계 사실을 **구성요소와 대조해** 바로잡는다."""
    zeros = [f for f in facts_ if str(f.decimals) == "0"]
    if not zeros:
        return facts_
    # 구성요소(추가 차원이 있는 사실)의 배율 — 한 종류로 일관될 때만 근거가 된다.
    #
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
    """같은 개념·같은 값인데 **ADECIMAL이 갈리는** 사실이 있나 — (있음, 배수) 또는 (False, None)."""
    cs = candidates(fs, concept, scope, year)
    by_val = {}
    for f in cs:
        by_val.setdefault(round(f.value, 6), set()).add(str(f.decimals))
    for v, decs in by_val.items():
        if len(decs) > 1:
            return True, sorted(decs)
    return False, None


def series(fs, concept, scope=CONSOLIDATED, span=None):
    """{연도: 값} — 한 보고서에 당기·전기·전전기가 함께 실려 있어 3개년이 한 번에 나온다.

    ★ 분기·반기에서는 `span`을 줘야 연도끼리 기준이 어긋나지 않는다.
    """
    out = {}
    for f in candidates(fs, concept, scope, year=None, span=span):
        if f.year is not None and f.year not in out:
            out[f.year] = f.value
    return dict(sorted(out.items()))


def span_for(concept, flow_span):
    """개념 하나에 적용할 기간 구분. 재무상태표 개념은 **시점**이라 흐름 구분이 없다."""
    if flow_span is None:
        return None
    return INSTANT if concept in _INSTANT else flow_span


def scopes(fs):
    """이 문서가 연결·별도를 각각 갖고 있는가."""
    return {s for s in (f.scope for f in fs)}


def check_identity(fs, scope=CONSOLIDATED, year=None):
    """IAS 1.54 항등식 `자산 = 부채 + 자본` 확인. (오차, 자산, 부채, 자본) 또는 None."""
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
