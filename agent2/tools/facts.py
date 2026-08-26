"""기업·문서 메타 조회 — 주최가 준 표 3종을 전부 쓴다.

    universe.csv      70개사 × 17컬럼   기업 마스터
    manifest.jsonl    4,204건           **원문이 수집된** 문서
    list_*.json ×280  22,980건          DART 공시 목록 **전체**

★ 세 번째가 가장 크고, 오래 안 쓰고 있었다. **22,980 − 4,204 = 18,776건은 목록에만 있다.**
원문이 없어도 *"언제 무슨 공시를 냈다"* 는 확실하므로, 존재·시점 질의에 답하고
"원문은 코퍼스에 없음"을 정직하게 고지할 수 있다.
목록에만 있는 것 예: 현금·현물배당결정 377 · 매출액또는손익구조30%변동 293 ·
최대주주등소유주식변동신고서 777 · 기업설명회 1,856.

시가총액은 `universe.market_cap`(억원, 2026-07-24 조회)에 있다. 주최 제공 데이터이므로
그대로 쓴다 — 코퍼스 밖에서 가져오는 것이 아니다.
"""
import re
from functools import lru_cache
from collections import Counter

from agent2.data import store
from agent2.tools.audit import Audit

#: 공시 목록의 DART 비고(`rm`) 코드. 실측 분포: '' 11,108 · 유 9,054 · 유정 1,225 ·
#: 코 579 · 정 555 · 연 279 · 정연 116 · 코정 63 · 공 1
RM = {"유": "유가증권시장", "코": "코스닥시장", "정": "정정", "연": "연결",
      "채": "채권", "넥": "코넥스", "공": "공정위"}


def company(name):
    """기업 마스터 1행 + 사람이 읽는 요약. 못 찾으면 None."""
    c = store.resolve_corp(name)
    if c is None:
        return None
    return {
        "corp_name": c["corp_name"], "listed_name": c["listed_name"],
        "corp_code": c["corp_code"], "stock_code": c["stock_code"],
        "market": c["market"], "industry": c["industry"], "sector": c["sector"],
        "listing_date": c["listing_date"], "fiscal_month": c["fiscal_month"],
        "market_cap_eok": int(c["market_cap"] or 0),
        # 단위를 **문장으로 함께** 준다. 필드명(_eok)만으로는 모델이 단위를 옮겨 적다가
        # 틀린다 — 실제로 14,586,465억원을 "14,586,465 백만 원"이라 답한 적이 있다.
        "market_cap_text": fmt_market_cap(c["market_cap"]),
        "note": c.get("note") or "",
        "n_docs": {k: int(c.get(f"n_{k}") or 0)
                   for k in ("periodic", "major", "exchange", "holding")},
    }


def fmt_market_cap(eok):
    """시가총액(억원) → 사람이 읽는 문자열.

    도메인 결정(2026-07-31): "1의 자리까지 데이터화·계산하고, **유저에게 보여줄 때만**
    단위를 반올림해 보여준다." 그래서 정확값(억원)과 읽기 쉬운 조원 표기를 함께 낸다.
    """
    n = int(eok or 0)
    if not n:
        return "확인되지 않음"
    if n >= 10000:
        return f"{n:,}억원 (약 {n/10000:,.1f}조원)"
    return f"{n:,}억원"


def market_cap(name):
    """시가총액(억원). 출처는 `universe.csv`(2026-07-24 조회) — 주최 제공 데이터다."""
    c = store.resolve_corp(name)
    return int(c["market_cap"]) if c and c.get("market_cap") else None


def sector_ranking(sector=None, industry=None, by="market_cap", top=None):
    """섹터·업종 내 순위. 시나리오 1-6(섹터 시총 순위)용.

    `sector`/`industry`가 없으면 전체 70개사.
    """
    rows = list(store.universe())
    if sector:
        rows = [r for r in rows if sector in r["sector"]]
    if industry:
        rows = [r for r in rows if industry in r["industry"]]
    rows.sort(key=lambda r: -int(r["market_cap"] or 0))
    out = [{"rank": i, "corp_name": r["corp_name"], "sector": r["sector"],
            "market_cap_eok": int(r["market_cap"] or 0),
            "market_cap_text": fmt_market_cap(r["market_cap"])}
           for i, r in enumerate(rows, 1)]
    return out[:top] if top else out


_SP = re.compile(r"\s+")


def peers(name, level="sector"):
    """같은 섹터(또는 업종)의 다른 회사들. 시나리오 2-1·2-8(경쟁사 비교)용.

    `name`은 **기업명이거나 섹터명**이다. 섹터명을 받는 이유(실측 2-8):
    "반도체 섹터의 가동률"을 물었을 때 섹터 구성원을 알아낼 길이 없어 모델이
    **직접 추측했고, 코퍼스에 없는 `LG전자`를 끌어들였다.** 섹터명은 코퍼스에서
    `반도체·전자부품`처럼 복합어라 정확히 입력할 수 없으므로 **부분일치**로 받는다.
    """
    c = store.resolve_corp(name)
    key = "sector" if level == "sector" else "industry"
    if c is not None:
        return [r["corp_name"] for r in store.universe()
                if r[key] == c[key] and r["corp_name"] != c["corp_name"]]
    # 기업으로 안 풀리면 섹터명으로 본다. 가장 **짧은** 적중 섹터를 쓴다
    # (`반도체` → `반도체·전자부품`; 여러 섹터가 걸리면 더 좁은 쪽).
    q = _SP.sub("", (name or ""))
    if not q:
        return []
    hits = {r[key] for r in store.universe()
            if r[key] and (q in _SP.sub("", r[key]) or _SP.sub("", r[key]) in q)}
    if not hits:
        return []
    sec = min(hits, key=len)
    return [r["corp_name"] for r in store.universe() if r[key] == sec]


#: 보고서명 매칭용 정규화 — 공백·구분자·괄호를 지운다.
#: 원문 서식명은 `주요사항보고서(자기주식취득결정)`처럼 **붙여 쓰고 괄호가 끼는데**,
#: 모델은 자연어대로 `자기주식 취득 결정`이라 쓴다.
_KW_DROP = re.compile(r"[\s·ㆍ()\-]")


@lru_cache(maxsize=1)
def _TYPE_ALIAS():
    """`filingtypes` 유형 id → 그 유형의 `include` 패턴. 순환 임포트를 피해 지연 로드한다."""
    from agent2.tools import filingtypes as _ft
    return {f.id: f.include for f in _ft.TYPES}


def _kw_hit(keyword, report_nm):
    """보고서명이 keyword에 걸리는가. **평문 부분문자열 OR 정규화 토큰 순서 포함.**

    ★ 왜 (2026-08-17 실측): 종전에는 `keyword in report_nm` 평문 매칭 하나였다.
      원문 서식명에는 공백이 없어 **모델이 띄어 쓰면 목록이 통째로 0건**이 됐다:
          keyword='자기주식취득결정'    → rows 15건 · 관측 6,373자 · B-1 셀트리온 O(14)
          keyword='자기주식 취득 결정'  → rows  0건 · 관측 2,978자 · B-1 X("1번")
      rows가 비면 관측의 91%가 **정형공시 레코드 1건**뿐이라 모델이 그걸 세어
      "1번"이라 답한다. 계수(`답: 14`)는 정확한데 관측 맨 뒤로 밀린다.
      회차 전수(r1~r22 · 도구호출 1,937회): keyword 40종 중 **15종이 공백 포함**이고
      호출의 **15.1%(293회)**가 이 경로였다.
    ★ 같은 함수의 `계수` 경로(`tools/__init__.list_filings`)는 **이미** 이 규칙을
      쓰고 있었다 — 한쪽만 안 맞춰져 있었다(§6-27: 같은 종류의 질의는 체계를 맞춰라).
    ★ 평문 매칭을 **남겨 둔다.** 정규화 매칭은 순증이라 종전에 걸리던 행은 하나도
      사라지지 않는다(전수 대조로 확인: 사라짐 0건).
    """
    if keyword in report_nm:
        return True
    # ★★ **`filingtypes` 유형 id를 별칭으로 인정한다**(2026-08-20).
    #   모델은 유형 id를 keyword로 쓰는 것이 자연스러운데(도구 docstring이 그렇게 안내한다),
    #   id와 실제 서식명이 다른 유형이 **23종 중 9종(39%)**이라 그때 rows가 통째로 0건이 됐다:
    #       임원주요주주소유상황 → `임원ㆍ주요주주특정증권등소유상황보고서`  **8,628건**
    #       최대주주소유주식변동 → `최대주주등소유주식변동신고서`              784건
    #       현금배당결정        → `현금ㆍ현물배당결정`                      408건
    #       손익구조변경        → `매출액또는손익구조30%(대규모법인은15%)이상변경`  310건
    #       타법인취득결정       → `타법인주식및출자증권취득결정`               263건 …
    #   ★ 패턴은 **`filingtypes`가 이미 갖고 있다**(`include`) — 지어내지 않는다.
    #   ★ 전수 대조(keyword 28종 × 서식 587종): **사라짐 0건 · 늘어남 10,560건**.
    #     §6-38이 요구한 "기존 규칙을 남기고 OR로 더해라"를 만족한다.
    _al = _TYPE_ALIAS().get((keyword or "").strip())
    if _al:
        _flat = _KW_DROP.sub("", report_nm)
        if any(_KW_DROP.sub("", p) in _flat for p in _al):
            return True
    nm = _KW_DROP.sub("", report_nm)
    pos = 0
    for tok in (_KW_DROP.sub("", t) for t in re.split(r"[\s·ㆍ]+", keyword)):
        if not tok:
            continue
        i = nm.find(tok, pos)
        if i < 0:
            return False
        pos = i + len(tok)
    return True


def filings(name, doc_group=None, subtype=None, year=None,
            with_listed_only=True, keyword=None, limit=None):
    """공시 목록.

    `with_listed_only=True`(기본) — **목록에만 있는 18,776건까지 포함**한다.
    원문이 없어도 "언제 무슨 공시를 냈다"는 확실하기 때문이다.
    False로 주면 원문이 수집된 4,204건만 본다.

    반환 각 행에 `has_source`가 있다 — False면 목록에만 있고 원문이 없다.
    (이 인자는 한때 `include_listed_only`였는데 뜻과 반대로 동작해 스모크에서 배당 공시가
     0건으로 보였다. 이름은 동작과 일치해야 한다.)
    """
    c = store.resolve_corp(name)
    if c is None:
        return []
    rows = store.docs(corp=c["corp_name"], doc_group=doc_group,
                      doc_subtype=subtype, base_year=year,
                      source_only=not with_listed_only)
    if keyword:
        rows = [r for r in rows if _kw_hit(keyword, r.get("report_nm") or "")]
    have = {r["rcept_no"] for r in store.manifest()}
    out = [{"rcept_no": r["rcept_no"], "rcept_dt": r.get("rcept_dt"),
            "report_nm": r.get("report_nm"), "doc_group": r.get("doc_group"),
            "doc_subtype": r.get("doc_subtype"),
            "is_correction": bool(r.get("is_correction")),
            "base_year": r.get("base_year"), "base_month": r.get("base_month"),
            "has_source": r["rcept_no"] in have,
            "rm": [RM.get(x, x) for x in str(r.get("rm") or "").strip()]}
           for r in rows]
    out.sort(key=lambda r: r["rcept_dt"] or "", reverse=True)
    return out[:limit] if limit else out


def timeline(name, keyword=None, year=None, limit=30, start=None, end=None):
    """공시 이력 — **목록 전체(22,980건)** 기준. 원문 유무를 함께 표시한다.

    시나리오 1-10(증자·전환사채 일자)·1-11 등 '언제 무엇을 냈나' 질의용.

    ★ `start`/`end`로 **기간 범위**를 받는다(`YYYYMMDD` 또는 `YYYY-MM-DD`).
      `year`는 한 해만 거른다 — 범위 질의에 쓰면 답이 작아진다.
      실측(2026-08-14): 평가셋 115문항 중 **103건(90%)이 `year=2024`를 넣었고**,
      질문은 "2024년~2026년 3월"이라 2024년만 세어졌다.
    """
    a = Audit("facts.timeline", f"{name} {keyword or ''}".strip())
    rows = filings(name, with_listed_only=True, keyword=keyword)
    if start or end:
        lo = re.sub(r"[^0-9]", "", str(start or "00000000"))[:8].ljust(8, "0")
        hi = re.sub(r"[^0-9]", "", str(end or "99991231"))[:8].ljust(8, "9")
        rows = [r for r in rows if lo <= str(r["rcept_dt"] or "") <= hi]
    elif year:
        rows = [r for r in rows if str(r["rcept_dt"] or "").startswith(str(year))]
    no_src = sum(1 for r in rows if not r["has_source"])
    if no_src:
        a.reject(f"{no_src}건", "No_Source", "공시 목록에는 있으나 원문이 코퍼스에 없음")
    for r in rows[:limit]:
        if r["has_source"]:
            a.accept(r["report_nm"], "Exact_Match", r["rcept_dt"] or "")
    return {"rows": rows[:limit], "total": len(rows),
            "without_source": no_src, "audit": a}


def corrections(name, doc_group=None, year=None):
    """정정 이력 — (유형·기준기간)별로 원본과 정정본을 묶는다.

    **최신 접수본이 유효본**이다. 대체수집 3건은 PDF가 최신이라 이 규칙이 실제로 중요하다.
    """
    rows = filings(name, doc_group=doc_group, year=year, with_listed_only=True)
    groups = {}
    for r in rows:
        key = (r["doc_group"], r["doc_subtype"], r["base_year"], r["base_month"])
        groups.setdefault(key, []).append(r)
    out = []
    for key, g in groups.items():
        if len(g) < 2 and not any(x["is_correction"] for x in g):
            continue
        g.sort(key=lambda r: r["rcept_dt"] or "")
        out.append({"key": key, "versions": g, "effective": g[-1]})
    return out


def corpus_summary():
    """코퍼스 전체 요약 — 무엇이 얼마나 있는지."""
    m, c = store.manifest(), store.catalog()
    return {
        "corps": len(store.universe()),
        "docs_with_source": len(m),
        "catalog_total": len(c),
        "listed_only": sum(1 for r in c if not r["has_source"]),
        "by_group": dict(Counter(r["doc_group"] for r in m)),
        "corrections": sum(1 for r in m if r.get("is_correction")),
        "periodic_years": dict(sorted(Counter(
            r["base_year"] for r in m if r["doc_group"] == "periodic").items())),
        "latest_fiscal_year": store.latest_fiscal_year(),
    }


if __name__ == "__main__":
    print("코퍼스:", corpus_summary())
    print("\n삼성전자:", company("삼성전자"))
    print("\n시총 상위 5:", [(r["rank"], r["corp_name"], r["market_cap_eok"])
                          for r in sector_ranking(top=5)])
    print("\n금융·보험 섹터:", [(r["rank"], r["corp_name"])
                            for r in sector_ranking(sector="금융", top=4)])
    t = timeline("삼성전자", keyword="배당", limit=5)
    print(f"\n삼성전자 '배당' 공시 {t['total']}건 (원문 없음 {t['without_source']}건)")
    for r in t["rows"]:
        print(f"   {r['rcept_dt']} {r['report_nm'][:38]:40} 원문={'O' if r['has_source'] else 'X'}")
    print("\n한화오션 정정 이력:", len(corrections("한화오션", doc_group="periodic")), "그룹")
