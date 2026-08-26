"""정본 섹션 — 표가 아니라 **산문**을 결정론으로 집는다.

## 왜 만드나 (실측된 실패)

정본표 13종은 표를 정확히 뽑지만, 답이 **표 밖 문단**에 있는 질의를 통째로 놓쳤다.
2026-08-06 평가데이터셋 270문항에서 서술형 35건 중 25건이 이 경로다.

    1-13 공장위치   정답 `DX-구미·베트남·인도·브라질, DS-화성·평택·중국`
                    → 이 문장은 「3. 원재료 및 생산설비」의 **산문**에 있다.
                      우리는 같은 섹션의 *가동률 표*만 읽고 산문을 안 봤다.
    2-4  주요사업   정답 `차량부문/금융부문/기타부문` → 「1. 사업의 개요」 산문
    2-2  섹터동향   정답 `HBM 수요 급증`·`IFRS17` → 산문(단, 하위절 — 아래 한계 참조)

검색(BM25)으로는 못 집는다. 실측:
  · 삼성전자 "공장 생산거점 위치" → 1위가 **가동률 표** 텍스트
  · 삼성생명 "IFRS17 시장여건"    → 1위가 **태국 자회사** 섹션
    (CLAUDE.md가 경고한 그 사고 유형 — 바트 금액을 보험수익으로 답할 뻔했다)

그래서 정본표와 **같은 방식**을 쓴다. 질의어로 섹션을 지정하고 **산문 전문을 그대로**
준다. 검색이 개입하지 않으므로 오검색 위험이 0이다.

## 크기가 맞는다 (실측)

섹션 산문(표 제거 후) 길이 — 8개 평가대상사 기준:
    1. 사업의 개요         519 ~ 7,275자
    2. 주요 제품 및 서비스   219 ~   923자
    3. 원재료 및 생산설비    590 ~ 2,472자
`config.OBS_LIMIT`(컨텍스트 128k에서 역산한 14,620자) 안에 통째로 들어간다.

## 라벨은 **70개사 전수 실측**으로 정했다

    1. 사업의 개요            67사   + (제조서비스업) 3사 + (금융업) 3사
    2. 주요 제품 및 서비스      59사   + (제조서비스업) 3사
    2. 영업의 현황             8사   + (금융업) 3사      ← 금융업은 제품 대신 이것
    3. 원재료 및 생산설비       59사   + (제조서비스업) 3사  (금융업엔 없음 = 정상)
    7. 기타 참고사항           59사   / 5. 재무건전성 등 기타 참고사항 8사(금융)

`(제조서비스업)`·`(금융업)` 접두는 부분문자열 매칭으로 흡수된다.

## 한계 — 하위절은 아직 못 집는다

섹션 트리가 `<TITLE>` 태그만 노드로 쓴다. 실측(삼성생명): TITLE 200개인데 본문
하위절 라벨(`가.`/`나.`/`(1)`)이 73개 더 있고 트리에 없다. 그 결과
「5. 재무건전성 등 기타 참고사항」 하나가 **57,353자**짜리 덩어리가 되고,
2-2가 필요로 하는 `나. 국내외 시장여건 및 경쟁상황`이 그 안에 묻힌다.
→ 하위절 승격(HiChunk L1→L3, arXiv 2509.11552)은 **다음 단계**다. 이 모듈은 L1만 한다.

실행: python3 -m agent2.tools.docsections 삼성전자
"""
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
               # 금융업은 「영업의 현황」이 같은 자리를 차지한다(실측 11사).
               ("주요 제품 및 서비스", "영업의 현황"),
               ("주요 제품", "주요제품", "제품", "서비스", "매출 상품", "매출상품",
                "영업의 현황", "취급 상품")),

    # ★ 트리거를 **위치 질의로 좁혔다**(2026-08-06 실측 회귀).
    #   처음엔 `원재료`·`가동률`·`생산능력`·`생산실적`까지 넣었는데, 그 질의들의 답은
    #   **표**에 있다(매입 비중 91.4%, 가동률 78.8%). 산문을 붙였더니 모델이 표 대신
    #   산문을 옮겨 적어 2-5가 3/8 → 0/8로 무너졌다. 산문이 표를 대체하면 안 된다.
    #   이 섹션이 유일하게 표보다 나은 것은 **생산거점 위치**다(1-13).
    # ★ 트리거에 `원재료`·`가동률` 계열을 더했다(2026-08-09 실측).
    #   **섹션 이름이 `원재료 및 생산설비`인데 `원재료`가 트리거에 없었다.**
    #   그래서 `2-5 원재료 현황` **10문항 전부**가 이 섹션에 못 닿았고
    #   (`match()`가 빈 리스트), `2-8` 가동률 단독 질의 5건도 못 닿았다.
    #   근거: 도원님 `공시 별 알 수 있는 확인 내용 정리`(2026-08-08)가
    #   `II >3. 원재료 및 생산설비`의 확인 가능 내용으로 "원재료·외주비 매입 현황,
    #   생산설비"를 명시한다. 평가셋 `근거(출처)` 열도 2-5·2-6을 같은 섹션으로 지목한다.
    #   가동률을 넣는 이유: 조선사는 산출식이 **표가 아니라 산문**에 있다
    #   (한화오션 `③ 평균가동률 = 100.5%〔② ÷ ①〕` — 이 문장이 이 섹션 본문에 있고,
    #    `docsections`가 이미 갖고 있는데 트리거가 없어 안 불렸다).
    DocSection("production", "원재료 및 생산설비",
               ("원재료 및 생산설비", "생산설비의 현황"),
               ("공장", "사업장", "생산거점", "생산 거점", "생산기지",
                "제조 거점", "공장위치", "공장 위치", "생산 지역", "어디서 생산",
                "원재료", "원자재", "매입처", "조달", "외주",
                "가동률", "가동율", "조업도", "생산능력", "생산실적")),

    # ★ **`II. 사업의 내용` 도입부 등록은 시도했다가 폐기했다**(2026-08-13).
    #   현대자동차 1-2 정답(차량 78% · 금융 16% · 기타 6%)이 그 대섹션 도입부 산문에
    #   한 문장으로 있어서 정본섹션으로 등록했다. 관측에 그 문장이 실리는 것까지
    #   확인했는데(`금융부문이 약 16` 포함 True) **10회 실행이 0/10 → 0/10**으로
    #   전혀 움직이지 않았다. 모델이 정본표 두 줄만 옮기고 끝내기 때문이다 —
    #   근거를 넣는 문제가 아니라 **표를 답의 경계로 보는 문제**라 여기서는 못 고친다.
    #   게다가 이 대섹션에 본문 40자 이상이 있는 회사는 70사 중 **3사**뿐이다
    #   (현대차 223자·한화솔루션 446자·OCI홀딩스 69자). 다시 등록하지 말 것.
    DocSection("misc", "기타 참고사항",
               ("기타 참고사항", "재무건전성 등 기타 참고사항"),
               ("시장여건", "시장 여건", "경쟁 현황", "경쟁현황", "업황", "전방산업",
                "산업의 성장성", "시장점유율", "규제 환경")),
)

BY_ID = {s.id: s for s in SECTIONS}

_TITLE = re.compile(r"<TITLE\b[^>]*>([^<]*)</TITLE>")
#: ★ `<TABLE\b` 는 `<TABLE-GROUP` 도 매치한다(`\b`가 `E`와 `-` 사이 경계다).
#:   여기서는 결과가 안 바뀌지만(전수 확인) 같은 함정을 남기지 않는다 — `parse.py` §_TABLE 참조.
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
    # ★ **정정본이 유효본이다.** 처음에 `is_correction`을 제외했는데 그건 거꾸로였다 —
    #   원본을 읽고 정정된 내용을 버리게 된다. 주최 테크세션 자료도
    #   "정정공시 발생 시 원 공시를 역추적하여 정정된 내용 반영"이라 명시한다.
    #   8개 평가대상사 중 **3사**(한화오션·삼성생명·KB금융)가 FY2025 정정본을 갖고 있다.
    #
    #   ★★ 다만 **"최신이 이긴다"는 순진한 규칙이었다**(실측):
    #     KB금융  20260313 원본(TITLE 202) · 20260324 정정(184) · **20260619 정정(0)**
    #   마지막 정정본은 4.3M자인데 목차 구조가 **아예 없다** — 바뀐 부분만 담은
    #   부분 정정이다. 그걸 집으면 섹션이 통째로 사라진다(실제로 KB금융이 0자가 됐다).
    #   그래서 **접수일 역순으로 훑어 목차를 가진 최신본**을 쓴다.
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
    """정본 섹션 → [(제목, 산문)] — **해당하는 절을 전부** 돌려준다.

    ★ 하나만 고르면 안 된다(실측): 현대자동차는 사업보고서에
      `1. (제조서비스업)사업의 개요`와 `1. (금융업)사업의 개요`를 **둘 다** 싣는다.
      긴 쪽만 집었더니 금융업 절이 뽑혔는데, 정답(2-4)은 `차량부문/금융부문/기타부문`
      셋을 요구한다. 회사가 나눠 쓴 절은 나눠 쓴 대로 준다.

    목차에도 같은 문자열이 있어 껍데기 항목이 걸리므로 본문 길이로 거른다.
    """
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
    """구조화 dict. `find_sections`가 정본표와 나란히 실어 보낸다.

    절이 여럿이면(제조서비스업/금융업) 예산을 **절 수로 나눠** 고르게 담는다 —
    앞엣것이 예산을 다 먹으면 뒤엣것이 통째로 사라진다.
    """
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
