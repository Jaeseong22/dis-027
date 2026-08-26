"""기업·문서 메타 조회 — 주최가 준 표 3종을 전부 쓴다."""
import re
from functools import lru_cache
from collections import Counter

from agent2.data import store
from agent2.tools.audit import Audit

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
    """시가총액(억원) → 사람이 읽는 문자열."""
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
    """섹터·업종 내 순위. 시나리오 1-6(섹터 시총 순위)용."""
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
    """같은 섹터(또는 업종)의 다른 회사들. 시나리오 2-1·2-8(경쟁사 비교)용."""
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
    """보고서명이 keyword에 걸리는가. **평문 부분문자열 OR 정규화 토큰 순서 포함.**"""
    if keyword in report_nm:
        return True
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
    """공시 목록."""
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
    """공시 이력 — **목록 전체(22,980건)** 기준. 원문 유무를 함께 표시한다."""
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
    """정정 이력 — (유형·기준기간)별로 원본과 정정본을 묶는다."""
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
