"""정본 섹션 — 표가 아니라 **산문**을 결정론으로 집는다."""
import re
from collections import namedtuple
from functools import lru_cache

from agent2.data import source, store
from agent2.tools import doctables as _doctables

#: 정본 섹션 1종.
#:   titles   `<TITLE>` 본문에 이 문자열이 들어가면 그 섹션(부분일치, 앞엣것 우선)
#:   triggers 질의에 이 말이 있으면 이 섹션을 붙인다
DocSection = namedtuple("DocSection", "id name titles triggers")

SECTIONS = (
    DocSection("overview", "사업의 개요",
               ("사업의 개요",),
               ("주요사업", "주요 사업", "사업을 정리", "사업 정리", "사업의 개요",
                "무슨 사업", "어떤 사업", "사업 범위", "사업범위", "산업 동향",
                "산업의 동향", "섹터의 동향", "업계 동향", "산업 특징", "사업 구성")),

    DocSection("products", "주요 제품 및 서비스",
               ("주요 제품 및 서비스", "영업의 현황"),
               ("주요 제품", "주요제품", "제품", "서비스", "매출 상품", "매출상품",
                "영업의 현황", "취급 상품")),

    DocSection("production", "원재료 및 생산설비",
               ("원재료 및 생산설비", "생산설비의 현황"),
               ("공장", "사업장", "생산거점", "생산 거점", "생산기지",
                "제조 거점", "공장위치", "공장 위치", "생산 지역", "어디서 생산",
                "원재료", "원자재", "매입처", "조달", "외주",
                "가동률", "가동율", "조업도", "생산능력", "생산실적")),

    DocSection("misc", "기타 참고사항",
               ("기타 참고사항", "재무건전성 등 기타 참고사항"),
               ("시장여건", "시장 여건", "경쟁 현황", "경쟁현황", "업황", "전방산업",
                "산업의 성장성", "시장점유율", "규제 환경")),
)

BY_ID = {s.id: s for s in SECTIONS}

_TITLE = re.compile(r"<TITLE\b[^>]*>([^<]*)</TITLE>")
_TABLE = re.compile(r"<TABLE(?![A-Za-z0-9_-]).*?</TABLE>", re.S)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _norm(s):
    return _WS.sub(" ", s or "").strip()


@lru_cache(maxsize=64)


def _titles_of(corp, year=None):
    """(제목, 시작, 끝) 목록 + 원문. 최신 사업보고서 기준(정정본 제외)."""
    year = year or store.latest_fiscal_year(corp)
    rows = list(store.docs(corp=corp, doc_subtype="annual", base_year=year))
    if not rows:
        return (), "", {}
    rows.sort(key=lambda r: (r.get("rcept_dt") or "", bool(r.get("is_correction"))),
              reverse=True)
    text, doc = "", {}
    for row in rows:
        cand = source.text(row)
        if _TITLE.search(cand or ""):
            text, doc = cand, {"rcept_no": row.get("rcept_no"),
                               "report_nm": row.get("report_nm"),
                               "is_correction": bool(row.get("is_correction"))}
            break
    if not text:
        return (), "", {}
    ms = list(_TITLE.finditer(text))
    out = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        out.append((_norm(m.group(1)), m.end(), end))
    return tuple(out), text, doc


def prose_of(raw):
    """섹션 원문 → **산문만**. 표는 통째로 뺀다(표는 정본표가 담당한다)."""
    return _norm(_TAG.sub(" ", _TABLE.sub(" ", raw)))

#: 실질 본문으로 인정할 최소 길이. 목차 항목은 본문이 거의 없다.
_MIN_PROSE = 40


def parts(corp, section_id, year=None):
    """정본 섹션 → [(제목, 산문)] — **해당하는 절을 전부** 돌려준다."""
    spec = BY_ID.get(section_id)
    if spec is None:
        return []
    titles, text, _ = _titles_of(corp, year)
    for want in spec.titles:              # 라벨 후보 순서 = 우선순위
        got, seen = [], set()
        for t, s, e in titles:
            if want not in t or t in seen:
                continue
            p = prose_of(text[s:e])
            if len(p) >= _MIN_PROSE:
                seen.add(t)
                got.append((t, p))
        if got:
            return got
    return []


def find(corp, section_id, year=None):
    """대표 1건 — (제목, 산문). 없으면 (None, None). 여러 절이면 가장 긴 것."""
    got = parts(corp, section_id, year)
    if not got:
        return None, None
    return max(got, key=lambda x: len(x[1]))


def match(query):
    """질의 → 붙일 정본 섹션 id 목록. 결정론 키워드 매칭(LLM 분류 없음)."""
    q = query or ""
    return [s.id for s in SECTIONS if any(t in q for t in s.triggers)]


def values(corp, section_id, year=None, limit=None):
    """구조화 dict. `find_sections`가 정본표와 나란히 실어 보낸다."""
    got = parts(corp, section_id, year)
    if not got:
        return None
    spec = BY_ID[section_id]
    each = None if limit is None else max(600, limit // len(got))
    body, cut_any = [], False
    for title, prose in got:
        p = prose if each is None or len(prose) <= each else prose[:each]
        cut_any = cut_any or len(p) < len(prose)
        body.append({"절": title, "본문": p,
                     **({"생략됨": f"{len(prose):,}자 중 {len(p):,}자"}
                        if len(p) < len(prose) else {})})
    doc = _titles_of(corp, year)[2]
    return {"section": spec.name, "id": spec.id,
            "source": "정본섹션",
            **({"출처": _doctables._cite(doc)} if doc.get("rcept_no") else {}),
            "절수": len(body), "본문": body,
            "truncated": cut_any,
            "note": "사업보고서 해당 절의 **본문(산문)**입니다. 표는 제외돼 있으니 "
                    "표 수치가 필요하면 정본표를 함께 보십시오. "
                    "회사가 제조/금융으로 절을 나눠 쓰면 절마다 따로 실었습니다."}

if __name__ == "__main__":
    import sys
    corp = sys.argv[1] if len(sys.argv) > 1 else "삼성전자"
    for spec in SECTIONS:
        title, prose = find(corp, spec.id)
        head = (prose or "")[:120].replace("\n", " ")
        print(f"[{spec.id:12s}] {spec.name:18s} "
              f"{('%6d자' % len(prose)) if prose else '  없음 '}  {title or ''}")
        if prose:
            print(f"     {head}…")
