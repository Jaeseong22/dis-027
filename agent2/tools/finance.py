"""재무 추출 — 개념 레지스트리 기반.

**기준은 전부 표준에서 왔다.** 근거는 `agent2/research/` 참조(CLAUDE.md 근거 원칙):
  · 개념 목록·업종 차이   `research/accounting.md`  — IAS 1.54/82, 1.55, XBRL 산업 택소노미
  · 라벨 배열 매핑        `research/data-standards.md` — EdgarTools, XUSSS
  · 결측 4상태·계산값 표시 `research/data-standards.md` — Compustat
  · as-reported 링크      같은 곳 — `Pick`·`Audit`가 그 역할

**즉흥 규칙은 전부 제거했다.** 아래는 만들었다가 폐기한 것들이다. 되살리지 말 것:
  ✗ "손익계산서 맨 윗줄이 top-line"   → 포괄손익계산서는 `당기순이익`으로 시작한다(24건 실측).
                                      IAS 1이 단일/분리 보고를 모두 허용하기 때문.
  ✗ "연결이 개별보다 항상 크다"        → 내부거래 상계로 반대가 될 수 있고 코퍼스로 검증 실패.
  ✗ "표가 클수록 근거가 강하다"        → 표준 어디에도 없다.

## 추출 절차

1. **버전 전체를 본다**(원본·정정본·PDF). 최신본만 보면 손해다 — KB금융 FY2025는
   PDF 2차정정본에 연결재무상태표가 없고 요약별도표만 있다(실측).
2. 각 표에서 개념을 찾는다. 라벨은 레지스트리의 **배열**로 후보를 돌린다.
3. **단위를 특정 못 하면 값을 버린다**(`UNIT_UNKNOWN`). 삼성생명에서 태국 자회사
   `단위 : 백만바트` 표의 2,321을 본사 보험영업수익으로 낼 뻔했다.
4. 재무상태표는 `자산 = 부채 + 자본` 자기정합 표를 고른다(IAS 1의 항등식).
5. 결측은 4상태로 구분해 돌려준다 — 은행의 `revenue`는 결측이 아니라 `NOT_APPLICABLE`이다.
"""
from agent2.data import parse as P
from agent2.data import store
from agent2.tools import compute, concepts as K, xbrl
from agent2.tools.audit import Audit

#: **주 재무제표(primary financial statements)** — 정해진 집합이다. 우리가 정하지 않는다.
#: ESEF(ESMA 보고 매뉴얼): 재무상태표 · 손익계산서 · 현금흐름표 · 자본변동표가
#: 상세 태깅 대상으로 명시적으로 정의돼 있다.
_PRIMARY_STATEMENTS = ("재무상태표", "포괄손익계산서", "손익계산서", "현금흐름표", "자본변동표")

#: 보고실체의 재무제표가 실리는 섹션. **기업공시서식 작성기준**(금융감독원):
#: *"종속회사가 있는 법인은 「재무에 관한 사항과 그 부속 명세」… 을 **연결재무제표 기준**으로
#: 기재하되 별도재무제표를 포함해서 작성"*. 실측도 1,051/1,054건이 이 구조다.
_FINANCIAL_SECTION = "재무에 관한 사항"
_SUMMARY_TITLES = ("요약재무정보",)


def _title_rank(table):
    """근거 강도 — 주 재무제표(3) > 재무섹션 요약(2) > 기타 섹션 재무제표(1) > 그 외(0).

    근거는 두 가지 표준이고 제가 정한 점수가 아니다:
      · ESEF — 주 재무제표 4종이 정의된 집합
      · 기업공시서식 작성기준 — 보고실체 재무제표는 「재무에 관한 사항」에 연결 기준으로 기재
    `II. 사업의 내용 > 2. 영업의 현황`의 요약표는 주 재무제표가 아니다(KB금융에서 이 표를
    잡을 뻔했다). `IV. 경영진단`에는 해외 자회사 표가 섞여 있다(삼성생명 태국법인).
    """
    path = " ".join(table.section_path)
    in_fin = _FINANCIAL_SECTION in path
    is_primary = any(k in path for k in _PRIMARY_STATEMENTS)
    if in_fin and is_primary:
        return 3
    if in_fin and any(k in path for k in _SUMMARY_TITLES):
        return 2
    if is_primary:
        return 1
    return 0


def _scope(table):
    """연결 · 별도 · 불명. 실측 표기 규칙:
    `2-1. 연결 재무상태표`=연결 · `4-1. 재무상태표`=**별도**('별도'라는 단어가 없다).
    """
    path = " ".join(table.section_path)
    if any(k in path for k in ("별도", "개별")):
        return "별도"
    if "연결" in path:
        return "연결"
    if any(k in path for k in _PRIMARY_STATEMENTS):
        return "별도"          # 주 재무제표 제목인데 '연결'이 없으면 별도
    return "불명"


def _find(table, concept):
    """표에서 개념을 찾는다 — 레지스트리의 **라벨 배열**을 순서대로 시도.

    계층 상한은 **개념별**(`Concept.max_depth`)이다. 일괄 적용은 틀린다:
      손익 개념은 L0(소계 층)에서만 — `보험수익`(L1)을 잡으면 은행지주를 보험사로 오판.
      재무상태표는 `자산총계`가 L1, `부채총계`·`자본총계`가 L2다(실측).
    계층은 원문 들여쓰기(U+3000)에서 온다(research/parsing.md).
    """
    if not concept.labels:
        return None
    hits = table.find_rows(*concept.labels)
    if not hits:
        return None
    if concept.max_depth is not None:
        hits = [(l, r) for l, r in hits if P.indent_level(l) <= concept.max_depth]
        if not hits:
            return None
    return table.pick(*concept.labels, col=None)


# ----------------------------------------------------------------- 표 선택
def balance_sheet(doc, consolidated=True):
    """자산 = 부채 + 자본을 만족하는 표(IAS 1 항등식). (표, 값, 오차) 또는 (None, {}, None)."""
    ids = ("assets_total", "liabilities_total", "equity_total")
    cands = []
    for t in doc.tables:
        vals = {}
        for cid in ids:
            p = _find(t, K.BY_ID[cid])
            if p is None or p.value is None:
                break
            vals[cid] = p.value
        else:
            a = vals["assets_total"]
            if not a:
                continue
            err = abs(vals["liabilities_total"] + vals["equity_total"] - a) / abs(a)
            cands.append((err, t, vals))
    if not cands:
        return None, {}, None
    ok = [c for c in cands if c[0] < 0.01]
    pool = ok or cands
    want = "연결" if consolidated else "별도"
    pool.sort(key=lambda c: (-_title_rank(c[1]), _scope(c[1]) != want,
                             _scope(c[1]) == "불명", c[0]))
    err, t, vals = pool[0]
    return t, vals, err


#: 손익 개념 — 어느 표가 손익 본표인지는 **이 개념들이 몇 개 있는가**로 정한다.
#: 업종을 추측하지 않는다(IFRS 개념체계 '충실한 표현' — research/accounting.md).
_IS_IDS = tuple(c.id for c in K.INCOME_STATEMENT + K.SECTOR_SPECIFIC
                if c.statement == "IS" and c.labels)


def income_statement(doc, consolidated=True):
    """손익 본표 — **개념이 가장 많이 발견되는 정식 재무제표 표**.

    업종 프로파일로 앵커를 고르지 않는다. 기업이 무엇을 표시했든 그대로 받는다.
    KB금융처럼 은행 항목과 보험 항목을 함께 표시하는 회사도 있고, 그건 정상이다.
    """
    want = "연결" if consolidated else "별도"
    best = None
    for t in doc.tables:
        if _title_rank(t) == 0:
            continue                       # 주석·경영진단 표는 손익 본표가 아니다
        hits = [cid for cid in _IS_IDS
                if (p := _find(t, K.BY_ID[cid])) is not None and p.value is not None]
        if len(hits) < 2:
            continue
        score = (_title_rank(t), _scope(t) == want, _scope(t) != "불명", len(hits), t.nrows)
        if best is None or score > best[0]:
            best = (score, t)
    return best[1] if best else None


# ----------------------------------------------------------------- XBRL 우선 경로
def _from_xbrl(corp_name, year, consolidated, subtype, a):
    """XBRL 태그에서 뽑는다. 값이 부족하면 None을 돌려 라벨 파서로 넘긴다.

    왜 이 경로가 먼저인가(2026-07-31 도메인 결정 + 전수 실측):
      · 연결/별도가 `ACONTEXT`로 **확정**된다. 라벨은 둘 다 "판매비와관리비"라
        휴리스틱으로는 못 가른다 — 실제로 별도 48,445,100을 연결로 답한 적이 있다.
      · **당기·전기·전전기가 한 보고서에 함께** 실려 있다. 도메인 확인: "3개년은
        최신 보고서의 당기·전기·전전기로 본다"(2026-07-31). 문서 3개를 뒤질 필요가 없다.
      · `ADECIMAL`로 표시 단위가 확정된다. 기업마다 백만원/원이 섞여 있는데
        (메리츠금융지주·삼성화재는 원 단위) 이걸 모르면 비교가 깨진다.
      · IAS 1 항등식이 70개사 중 68개사에서 **오차 0**으로 맞는다(라벨 경로는 1% 필요).

    태그가 없거나 핵심 개념이 안 잡히는 2개사(레인보우로보틱스·디앤디파마텍)는 None이다.
    """
    if subtype != "annual":          # 분기·반기 문서의 컨텍스트 접두사는 미검증 — 넘기지 않는다
        return None
    fs = xbrl.facts(corp_name, year, doc_subtype=subtype)
    if not fs:
        return None
    scope = xbrl.CONSOLIDATED if consolidated else xbrl.SEPARATE
    if scope not in xbrl.scopes(fs):
        return None

    values, krw, labels, series, status, codes, units = {}, {}, {}, {}, {}, {}, {}
    for cid in xbrl.CONCEPTS:
        f = xbrl.pick(fs, cid, scope, year)
        if f is None:
            # 회사가 표시하지 않은 항목은 **생략**이 정상이다(XBRL 결측 규칙).
            status[cid] = (K.NOT_FOUND if cid in K.REQUIRED_ALL else K.NOT_APPLICABLE)
            continue
        values[cid] = f.value
        krw[cid] = f.krw
        labels[cid] = f.label
        codes[cid] = f.code
        # 라벨 파서 경로와 **같은 계약**을 유지한다(`units`가 없으면 하위 호출부와
        # 회귀 테스트가 깨진다). ADECIMAL이 곧 배율이라 여기서는 모호할 일이 없다.
        scale = f.krw / f.value if f.value else 1.0
        raw = {1: "원", 1e3: "천원", 1e6: "백만원",
               1e8: "억원", 1e12: "조원"}.get(round(scale), "원")
        units[cid] = P.Unit(tokens=(raw,), scale=scale, currency="KRW",
                            kind="krw", raw=f"(단위: {raw})")
        series[cid] = xbrl.series(fs, cid, scope)
        a.accept(f.value, "Exact_Match", f"{cid} ← {f.code} [{scope}] {f.label[:20]}",
                 f"XBRL {f.decimals or 'INF'}")

    if not any(k in values for k in K.REQUIRED_ALL):
        return None

    idn = xbrl.check_identity(fs, scope, year)
    if idn is not None:
        compute.verify("자산=부채+자본", idn[2] + idn[3], idn[1], audit=a)
    return {"values": values, "krw": krw, "labels": labels, "codes": codes,
            "units": units, "series": series, "status": status, "scope": scope,
            "via": "xbrl",
            "identity_error": None if idn is None else idn[0]}


# ----------------------------------------------------------------- 추출
#: **비용 개념은 양수로 낸다** — 공시 표의 부호가 아니라 개념의 부호를 쓴다.
#:
#: ## 왜 (2026-08-11 전수 실측)
#: 같은 개념인데 회사마다 부호가 갈렸다. 손익계산서가 비용을 차감 항목으로 적으면
#: 괄호·음수로 표기되는데, 그걸 그대로 가져왔기 때문이다:
#:     sga            양수 50사 · **음수 10사** · 없음 10사
#:                    삼성SDI · POSCO홀딩스 · 현대제철 · KB금융 · 우리금융지주 ·
#:                    메리츠금융지주 · 셀트리온 · LG생활건강 · HMM · 엘에스일렉트릭
#:     cost_of_sales  양수 46사 · **음수 7사** · 없음 17사
#: (2026-08-15 재측정. 종전 주석은 음수를 9사로 적고 "등"으로 흐렸다 —
#:  전수 감사는 이름을 남긴다. §6-6)
#: 실제 피해 — #68 KB금융 1-15에서 값 3개를 **전부 정확히** 뽑고도 오답이 됐다:
#:     관측 `sga: -7,064,573 백만원` → 답변 `-7,064,573 백만원` → 정답 `7,064,573백만원`
#: 모델은 관측을 그대로 옮겼을 뿐이고, 부호를 넣은 것은 우리다.
#:
#: 근거: 비용의 부호는 **표시(presentation) 문제**이지 개념의 속성이 아니다.
#:   IAS 1.99–105는 비용을 성격별·기능별로 표시하라고만 하고 부호 규약을 두지 않는다.
#:   데이터벤더는 양수로 저장한다 — Compustat `XSGA`(SG&A)·`COGS`가 그렇다.
#:   평가셋 정답도 전부 양수로 쓴다.
#:
#: `tax_expense`는 **넣지 않는다.** 법인세수익(환입)이면 진짜 음수이고,
#: 절댓값을 취하면 그 정보가 사라진다(음수 16사 중 어느 쪽인지 구분이 필요하다).
_COST_ABS = ("sga", "cost_of_sales")


def _abs_costs(r):
    """비용 개념을 절댓값으로. `values`·`krw`·`series`를 **함께** 맞춘다(한쪽만 고치면 어긋난다)."""
    if not isinstance(r, dict):
        return r
    for key in ("values", "krw"):
        d = r.get(key)
        if isinstance(d, dict):
            for cid in _COST_ABS:
                v = d.get(cid)
                if isinstance(v, (int, float)) and v < 0:
                    d[cid] = -v
    s = r.get("series")
    if isinstance(s, dict):
        for cid in _COST_ABS:
            ys = s.get(cid)
            if isinstance(ys, dict):
                s[cid] = {y: (-v if isinstance(v, (int, float)) and v < 0 else v)
                          for y, v in ys.items()}
    return r


def extract(corp, year=None, month=12, consolidated=True):
    """기업·기간 → 개념 값 + 상태 + 감사 로그.

    반환:
      corp · year · month · profile · consolidated · period · doc_id
      values  {개념id: 값}            — 단위 확정된 것만
      units   {개념id: Unit}
      status  {개념id: 상태코드}       — NOT_APPLICABLE / NOT_FOUND / UNIT_UNKNOWN
      audit   Audit
    """
    a = Audit("finance.extract", f"{corp} {year or '최신'}")
    c = store.resolve_corp(corp)
    if c is None:
        a.reject(corp, "Entity_Mismatch", "코퍼스 70개사에서 식별 불가")
        return {"corp": corp, "values": {}, "status": {}, "profile": None, "audit": a}

    year = year or store.latest_fiscal_year(c["corp_name"])
    subtype = {12: "annual", 6: "half"}.get(month, "quarter")
    docs = store.docs(corp=c["corp_name"], doc_subtype=subtype,
                      base_year=year, base_month=month)
    if not docs:
        a.reject(f"{year}.{month:02d}", "Time_Mismatch", f"{subtype} 문서 없음")
        return {"corp": c["corp_name"], "year": year, "values": {}, "status": {},
                "profile": None, "audit": a}

    # ⓪ XBRL 태그 우선. 없거나 부족하면 아래 라벨 파서로 폴백한다(교체가 아니라 상위 경로).
    xb = _from_xbrl(c["corp_name"], year, consolidated, subtype, a)
    if xb is not None:
        doc_row = sorted(docs, key=lambda r: r["rcept_dt"])[-1]
        return _abs_costs({"corp": c["corp_name"], "year": year, "month": month,
                "consolidated": consolidated, "scope": xb["scope"], "via": "xbrl",
                "period": doc_row["report_nm"], "doc_id": doc_row["doc_id"],
                "rcept_dt": doc_row["rcept_dt"],
                "rcept_no": doc_row.get("rcept_no"),
                "is_correction": bool(doc_row.get("is_correction")),
                "values": xb["values"], "krw": xb["krw"], "units": xb["units"],
                "labels": xb["labels"],
                "codes": xb["codes"], "series": xb["series"], "status": xb["status"],
                "identity_error": xb["identity_error"],
                "profile": K.observed_profile(set(xb["values"])), "audit": a})

    # ① 버전 전체를 본다 (research/parsing.md — 정정본이 문서 일부만 다시 내는 경우가 있다)
    cands = []
    for row in sorted(docs, key=lambda r: r["rcept_dt"]):
        for doc in P.parse_doc(row):
            bs_t, bs_v, err = balance_sheet(doc, consolidated)
            is_t = income_statement(doc, consolidated)
            if bs_t is None and is_t is None:
                continue
            cands.append({"row": row, "doc": doc,
                          "bs": bs_t, "bs_err": err, "is": is_t})
    if not cands:
        a.reject(docs[-1]["report_nm"], "Concept_Shift", "재무 표를 찾지 못함")
        return {"corp": c["corp_name"], "year": year, "values": {}, "status": {},
                "profile": None, "audit": a}

    def rank(v, key):
        """표 후보 정렬 — **표의 근거 강도가 접수일보다 우선**한다.

        접수일을 앞에 두면 최신 정정본의 약한 표(요약별도표)가 원본의 정식 연결표를 이긴다.
        실제로 KB금융에서 정답(797.9조)이 후보 1위인데 3,292억이 선택됐다.
        """
        t = v[key]
        if t is None:
            return (-1, 0, 0, "")
        want = "연결" if consolidated else "별도"
        return (_title_rank(t), _scope(t) == want, _scope(t) != "불명",
                v["row"]["rcept_dt"])

    bs_pick = max(cands, key=lambda v: rank(v, "bs"))
    is_pick = max(cands, key=lambda v: rank(v, "is"))

    if bs_pick["row"]["doc_id"] != is_pick["row"]["doc_id"]:
        a.accept(bs_pick["row"]["report_nm"], "Exact_Match",
                 "재무상태표는 다른 버전에서 채택", bs_pick["row"]["rcept_dt"])
    if bs_pick["bs_err"] is not None:
        compute.verify("자산=부채+자본", 1 + bs_pick["bs_err"], 1.0, audit=a)

    # ② 개념별 추출 — 적용되는 개념만 돈다
    values, units, status = {}, {}, {}
    for concept in K.concepts_for():
        table = bs_pick["bs"] if concept.statement == "BS" else is_pick["is"]
        if table is None or concept.kind == "calculable" and not concept.labels:
            continue
        pick = _find(table, concept)
        src = f"{concept.id} ← " + ">".join(table.section_path[-1:])[:40]
        if pick is None or pick.value is None:
            # XBRL 결측 규칙 — 표시하지 않은 항목은 **생략**이 정상이고,
            # **필수 값이 없을 때만** 오류다. 여기서 일괄 NOT_FOUND를 넣으면
            # "회사가 안 쓴 항목"까지 오류로 세어 712건이 됐다(실제 오류는 1건).
            status[concept.id] = (K.NOT_FOUND if concept.id in K.REQUIRED_ALL
                                  else K.NOT_APPLICABLE)
            continue
        u = table.unit_for(pick.label)
        if not u.convertible:
            # ③ 단위 미상이면 값을 버린다 (태국 바트 사고 방지)
            status[concept.id] = K.UNIT_UNKNOWN
            a.reject(pick.value, "Unit_Error",
                     f"{concept.id}: {u.raw or '단위 표기 없음'}"
                     + (f" ({u.currency})" if u.currency and u.currency != "KRW" else ""), src)
            continue
        values[concept.id] = pick.value
        units[concept.id] = u
        a.note_pick(pick, src)

    # ④ 계산값(Compustat calculable) — 파생식이 있는 개념
    for concept in K.concepts_for():
        if concept.derive is None or concept.id in values:
            continue
        op, *args = concept.derive
        parts = [values.get(x) for x in args]
        have = [v for v in parts if v is not None]
        if op == "sum_":
            if not have:
                continue
            values[concept.id] = compute.run("sum_", have, audit=a)
        elif len(parts) == 2 and all(v is not None for v in parts):
            values[concept.id] = compute.run(op, *parts, audit=a)
        else:
            continue
        units[concept.id] = next((units[x] for x in args if x in units), None)
        status[concept.id] = K.COMBINED       # as-reported가 아니라 계산값임을 표시

    # ⑤ 못 찾은 개념 — **필수인지에 따라 상태가 다르다**(XBRL 결측 규칙).
    #   필수(IAS 1.54/82 전 업종)인데 없으면 `NOT_FOUND` = 오류, 조사 대상.
    #   그 외는 회사가 표시하지 않았을 뿐 → `NOT_APPLICABLE`(None) = 정상.
    for concept in K.ALL:
        if concept.id in values or concept.id in status:
            continue
        status[concept.id] = (K.NOT_FOUND if concept.id in K.REQUIRED_ALL
                              else K.NOT_APPLICABLE)

    return _abs_costs({"corp": c["corp_name"], "year": year, "month": month,
            "profile": K.observed_profile(values.keys()), "consolidated": consolidated,
            "period": is_pick["row"]["report_nm"], "doc_id": is_pick["row"]["doc_id"],
            "rcept_no": is_pick["row"].get("rcept_no"),
            "is_correction": bool(is_pick["row"].get("is_correction")),
            "values": values, "units": units, "status": status, "audit": a})


def series(corp, concept_id, years=3, month=12, consolidated=True):
    """개념의 다년도 시계열 — {연도: 값}.

    시나리오 1-4(OPM 3개년) · 1-18(EPS 추이) · 2-6(가동률 3개년)용.
    값이 없는 해는 키를 만들지 않는다(0으로 채우지 않는다).

    ★ 도메인 결정(2026-07-31): **3개년은 최신 보고서의 당기·전기·전전기로 본다.**
    XBRL은 한 보고서에 세 기간이 함께 실려 있으므로 문서 3개를 뒤지지 않는다 —
    빠르고, 세 값이 같은 보고서 기준이라 정합이 보장된다.
    보고서를 해마다 따로 읽으면 그 사이 정정·재작성이 섞여 값이 어긋날 수 있다.
    """
    c = store.resolve_corp(corp)
    if c is None:
        return {}
    latest = store.latest_fiscal_year(c["corp_name"])
    if latest is None:
        return {}
    r = extract(c["corp_name"], latest, month, consolidated)
    if r.get("via") == "xbrl":
        s = (r.get("series") or {}).get(concept_id) or {}
        return {y: v for y, v in sorted(s.items())[-years:]}
    out = {}                                   # 라벨 폴백 — 연도별 문서를 각각 읽는다
    for y in range(latest - years + 1, latest + 1):
        rr = extract(c["corp_name"], y, month, consolidated)
        if concept_id in rr["values"]:
            out[y] = rr["values"][concept_id]
    return out


def value_of(result, concept_id):
    """(값, 단위, 상태, 설명) — 없으면 왜 없는지 표준 근거와 함께 돌려준다."""
    v = result["values"].get(concept_id)
    st = result["status"].get(concept_id)
    return (v, result["units"].get(concept_id), st,
            K.explain(concept_id, result.get("profile") or "ci") if st == K.NOT_APPLICABLE else "")


if __name__ == "__main__":
    for name in ("삼성전자", "KB금융", "삼성생명", "미래에셋증권"):
        r = extract(name)
        print(f"--- {name} · {r.get('period')} · 프로파일 {r['profile']} ---")
        for cid, v in r["values"].items():
            u = r["units"].get(cid)
            kind = " [계산값]" if r["status"].get(cid) == K.COMBINED else ""
            print(f"    {cid:24}{v:>18,.0f}  {(u.raw if u else '')}{kind}")
        na = [k for k, s in r["status"].items() if s == K.NOT_APPLICABLE]
        nf = [k for k, s in r["status"].items() if s == K.NOT_FOUND]
        uu = [k for k, s in r["status"].items() if s == K.UNIT_UNKNOWN]
        print(f"    해당없음 {len(na)} · 못찾음 {len(nf)}{' ' + str(nf[:5]) if nf else ''}"
              f" · 단위미상 {len(uu)}{' ' + str(uu[:3]) if uu else ''}")
        print("   ", repr(r["audit"]))
