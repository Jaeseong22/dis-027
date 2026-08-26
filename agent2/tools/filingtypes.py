"""공시 유형 레지스트리 — **"○○ 공시가 몇 번 있었나"를 결정론으로 센다.**"""
import re
import sys
from collections import namedtuple

from agent2.data import store

PERIOD = ("20230101", "20261231")

QUESTION_PERIOD = ("20240101", "20260331")

#: 제목 접두 = 정정 표기. **이 접두가 판별 기준이다**(위 docstring 참조).
_CORR_PREFIX = ("[기재정정]", "[첨부정정]")

#: 보고 주체가 자회사·종속회사임을 나타내는 꼬리표. 본체 계수에서 뺀다.
_SUB_MARK = ("자회사의 주요경영사항", "종속회사의주요경영사항", "종속회사의 주요경영사항")

_PRE = re.compile(r"^\s*\[[^\]]+\]\s*")
_SP = re.compile(r"[\s　]+")


def _norm(name):
    """보고서명 정규화 — 정정 접두를 떼고 공백을 지운다."""
    return _SP.sub("", _PRE.sub("", (name or "").strip()))


def is_correction(row):
    """정정본인가 — **제목 접두로만** 판별한다(`is_correction` 필드는 목록에서 빈다)."""
    return (row.get("report_nm") or "").strip().startswith(_CORR_PREFIX)


def is_subsidiary(row):
    """보고 주체가 자회사·종속회사인가."""
    return any(m in (row.get("report_nm") or "") for m in _SUB_MARK)

#: 공시 유형 1건.
#:   id       질문셋에서 부르는 이름
#:   label    사람이 읽는 이름
#:   include  정규화된 보고서명이 이 중 하나로 **시작**하면 포함
#:   exclude  포함됐더라도 이 조각이 들어 있으면 제외
#:   group    manifest의 doc_group(검증용. 목록에는 없을 수 있어 필수는 아니다)
FilingType = namedtuple("FilingType", "id label include exclude group")


def F(fid, label, include, exclude=(), group=None):
    return FilingType(fid, label, tuple(_SP.sub("", x) for x in include),
                      tuple(_SP.sub("", x) for x in exclude), group)

TYPES = (
    # ── 주요사항보고서 ──────────────────────────────────────────
    F("자기주식취득결정", "자기주식 취득 결정",
      ("주요사항보고서(자기주식취득결정)",),
      # 신탁계약 체결·해지는 **다른 결정**이다(취득 자체가 아니라 위탁 계약).
      ("신탁계약",), "major"),                                    # 71
    F("자기주식처분결정", "자기주식 처분 결정",
      ("주요사항보고서(자기주식처분결정)",), (), "major"),            # 117
    F("유상증자결정", "유상증자 결정",
      ("주요사항보고서(유상증자결정)", "유상증자결정"),
      # 청약·발행 '결과'는 결정이 아니다.
      ("결과",), "major"),                                        # 105 (자회사 68)
    F("무상증자결정", "무상증자 결정",
      ("주요사항보고서(무상증자결정)", "무상증자결정"), ("결과",), "major"),     # 4

    F("전환사채발행결정", "전환사채(CB) 발행 결정",
      ("주요사항보고서(전환사채권발행결정)", "전환사채권발행결정"),
      ("매도", "매수선택권", "취득", "결과"), "major"),                # 23 (8사)
    F("신주인수권부사채발행결정", "신주인수권부사채(BW) 발행 결정",
      ("주요사항보고서(신주인수권부사채권발행결정)", "신주인수권부사채권발행결정",
       "주요사항보고서(신주인수권부사채발행결정)", "신주인수권부사채발행결정"),
      ("취득", "행사", "조정", "결과"), "major"),                     # **0** — 코퍼스에 없다
    F("신규시설투자등", "신규 시설투자 등",
      ("신규시설투자등",), ("결과",), "exchange"),                     # 43 (21사)

    F("단일판매공급계약해지", "단일판매ㆍ공급계약 해지",
      ("단일판매ㆍ공급계약해지", "단일판매공급계약해지"), (), "exchange"),      # 20
    F("투자판단관련주요경영사항", "투자판단 관련 주요경영사항",
      ("투자판단관련주요경영사항",), (), "exchange"),                    # 300 (36사)

    F("상각형조건부자본증권발행결정", "상각형 조건부자본증권 발행 결정",
      ("주요사항보고서(상각형조건부자본증권발행결정)",), (), "major"),                 # 72 (원문 71 · 4사)
    F("자기주식취득신탁계약체결결정", "자기주식 취득 신탁계약 체결 결정",
      ("주요사항보고서(자기주식취득신탁계약체결결정)",), (), "major"),                 # 49 (원문 47 · 13사)
    F("회사합병결정", "회사 합병 결정",
      ("주요사항보고서(회사합병결정)",), (), "major"),                         # 41 (원문 24 · 14사)
    F("회사분할결정", "회사 분할 결정",
      ("주요사항보고서(회사분할결정)",), (), "major"),                         # 18 (원문 12 · 6사)
    F("주식교환ㆍ이전결정", "주식 교환ㆍ이전 결정",
      ("주요사항보고서(주식교환ㆍ이전결정)",), (), "major"),                      # 17 (원문 12 · 5사)
    F("회사분할합병결정", "회사 분할합병 결정",
      ("주요사항보고서(회사분할합병결정)",), (), "major"),                       # 9 (원문 8 · 1사)
    F("타법인주식및출자증권양수결정", "타법인 주식·출자증권 양수 결정",
      ("주요사항보고서(타법인주식및출자증권양수결정)",), (), "major"),                 # 8 (원문 7 · 3사)
    F("감자결정", "감자 결정",
      ("주요사항보고서(감자결정)",), (), "major"),                           # 6 (원문 6 · 3사)
    F("자본으로인정되는채무증권발행결정", "자본인정 채무증권 발행 결정",
      ("주요사항보고서(자본으로인정되는채무증권발행결정)",), (), "major"),               # 6 (원문 6 · 3사)
    F("유형자산양도결정", "유형자산 양도 결정",
      ("주요사항보고서(유형자산양도결정)",), (), "major"),                       # 5 (원문 3 · 2사)
    F("소송등의제기", "소송 등의 제기",
      ("주요사항보고서(소송등의제기)",), (), "major"),                         # 5 (원문 5 · 1사)
    F("해외증권시장주권등상장폐지", "해외 증권시장 주권등 상장폐지",
      ("주요사항보고서(해외증권시장주권등상장폐지)",), (), "major"),                  # 4 (원문 4 · 4사)
    F("해외증권시장주권등상장폐지결정", "해외 증권시장 주권등 상장폐지 결정",
      ("주요사항보고서(해외증권시장주권등상장폐지결정)",), (), "major"),                # 4 (원문 4 · 4사)
    F("유형자산양수결정", "유형자산 양수 결정",
      ("주요사항보고서(유형자산양수결정)",), (), "major"),                       # 3 (원문 2 · 1사)
    F("영업양수결정", "영업 양수 결정",
      ("주요사항보고서(영업양수결정)",), (), "major"),                         # 2 (원문 2 · 1사)
    F("자기전환사채매도결정", "자기 전환사채 매도 결정",
      ("주요사항보고서(자기전환사채매도결정)",), (), "major"),                     # 2 (원문 2 · 1사)
    F("타법인주식및출자증권양도결정", "타법인 주식·출자증권 양도 결정",
      ("주요사항보고서(타법인주식및출자증권양도결정)",), (), "major"),                 # 1 (원문 1 · 1사)
    F("영업정지", "영업 정지",
      ("주요사항보고서(영업정지)",), (), "major"),                           # 1 (원문 1 · 1사)
    F("해외증권시장주권등상장", "해외 증권시장 주권등 상장",
      ("주요사항보고서(해외증권시장주권등상장)",), (), "major"),                    # 1 (원문 1 · 1사)
    F("해외증권시장주권등상장결정", "해외 증권시장 주권등 상장 결정",
      ("주요사항보고서(해외증권시장주권등상장결정)",), (), "major"),                  # 1 (원문 1 · 1사)
    F("제3자의전환사채매수선택권행사", "제3자의 전환사채 매수선택권 행사",
      ("주요사항보고서(제3자의전환사채매수선택권행사)",), (), "major"),                # 1 (원문 1 · 1사)
    F("자기주식신탁해지", "자기주식 취득 신탁계약 해지 결정",
      ("주요사항보고서(자기주식취득신탁계약해지결정)",), (), "major"),         # 43

    F("교환사채발행결정", "교환사채(EB) 발행 결정",
      ("주요사항보고서(교환사채권발행결정)", "교환사채권발행결정"),
      ("취득", "행사", "조정", "결과"), "major"),                     # 4 (2사)

    # ── 거래소공시 ──────────────────────────────────────────────
    F("감사보고서제출", "감사보고서 제출",
      ("감사보고서제출",), (), "exchange"),                          # 279
    F("정기주주총회결과", "정기주주총회 결과",
      ("정기주주총회결과",), (), "exchange"),                        # 212
    F("현금배당결정", "현금ㆍ현물배당 결정",
      ("현금ㆍ현물배당결정",),
      # 주주명부폐쇄(기준일) 결정은 배당 결정 자체가 아니다.
      ("주주명부폐쇄", "기준일",), "exchange"),                       # 318 (자회사 54)
    F("타법인취득결정", "타법인주식및출자증권 취득 결정",
      ("타법인주식및출자증권취득결정",),
      ("처분",), "exchange"),                                       # 172 (자회사 52)
    F("지속가능경영보고서", "지속가능경영보고서 등 관련사항",
      ("지속가능경영보고서등관련사항",), (), "exchange"),               # 131
    F("대표이사변경", "대표이사(대표집행임원) 변경 안내공시",
      ("대표이사(대표집행임원)변경",), (), "exchange"),                # 66
    F("단일판매공급계약체결", "단일판매ㆍ공급계약 체결",
      ("단일판매ㆍ공급계약체결",),
      # 해지는 **반대 사건**이다. 체결 건수에 섞으면 안 된다.
      ("해지",), "exchange"),                                       # 804
    F("최대주주소유주식변동", "최대주주등 소유주식변동신고서",
      ("최대주주등소유주식변동신고서",), (), "exchange"),               # 575
    F("손익구조변경", "매출액 또는 손익구조 30%(대규모법인 15%) 이상 변경",
      # `…이상변경`과 `…이상변동` 두 표기가 함께 쓰인다(158 vs 25). 둘 다 같은 서식이다.
      ("매출액또는손익구조30%(대규모법인은15%)이상",),
      # `미만변경(자율공시)`은 **반대 조건**이다.
      ("미만",), "exchange"),                                       # 237 (자회사 37)
    F("기업설명회", "기업설명회(IR) 개최 안내공시",
      ("기업설명회(IR)개최",), (), "exchange"),                      # 1,364

    # ── 지분공시 ────────────────────────────────────────────────
    F("대량보유상황보고", "주식등의 대량보유상황보고서",
      ("주식등의대량보유상황보고서",), (), "holding"),                  # 767
    F("임원주요주주소유상황", "임원ㆍ주요주주특정증권등 소유상황보고서",
      ("임원ㆍ주요주주특정증권등소유상황보고서",),
      # '거래계획보고서'는 다른 서식이다(사전 공시).
      ("거래계획",), "holding"),                                     # 6,950
    F("의결권대리행사권유", "의결권 대리행사 권유 참고서류",
      ("의결권대리행사권유참고서류",), ("의견표명",), "holding"),        # 365
)

BY_ID = {t.id: t for t in TYPES}


def _match(row, ft):
    n = _norm(row.get("report_nm"))
    if not any(n.startswith(p) for p in ft.include):
        return False
    return not any(x in n for x in ft.exclude)


def rows_of(corp, type_id, period=PERIOD, include_sub=False):
    """그 회사·그 유형의 공시 행 목록(기간 필터 적용)."""
    ft = BY_ID.get(type_id)
    if ft is None:
        raise KeyError(f"미등록 공시 유형: {type_id} — TYPES에 먼저 등록할 것")
    c = store.resolve_corp(corp)
    if c is None:
        return []
    lo, hi = period or ("", "99999999")
    out = []
    for r in store.docs(corp=c["corp_name"], source_only=False):
        dt = str(r.get("rcept_dt") or "")
        if not (lo <= dt <= hi):
            continue
        if not _match(r, ft):
            continue
        if not include_sub and is_subsidiary(r):
            continue
        out.append(r)
    out.sort(key=lambda r: (r.get("rcept_dt") or "", r.get("rcept_no") or ""))
    return out


def count(corp, type_id, period=PERIOD):
    """건수 — **원본/정정/전체를 함께** 준다. 어느 것이 답인지는 질문이 정한다."""
    body = rows_of(corp, type_id, period, include_sub=False)
    sub = rows_of(corp, type_id, period, include_sub=True)
    sub = [r for r in sub if is_subsidiary(r)]
    orig = [r for r in body if not is_correction(r)]
    corr = [r for r in body if is_correction(r)]
    have = {r["rcept_no"] for r in store.manifest()}
    return {
        "corp": corp, "유형": BY_ID[type_id].label, "기간": f"{period[0]}~{period[1]}",
        "원본": len(orig), "정정": len(corr), "전체": len(body),
        "자회사건": len(sub),
        "원문보유": sum(1 for r in body if r["rcept_no"] in have),
        "최근": (body[-1].get("rcept_dt") if body else None),
        "접수번호": [r["rcept_no"] for r in orig[-3:]],
        "note": (f"이 계수는 **{period[0]}~{period[1]}** 기간입니다. "
                 "질문이 다른 기간을 말하면 start·end 를 주고 다시 부르십시오. "
                 "'몇 번'의 답은 **원본** 건수입니다(정정본은 같은 사건의 재공시). "
                 "정정본만 목록에 남은 경우 과소계수가 될 수 있습니다."),
    }


def latest(corp, type_id, period=PERIOD, with_source=True):
    """가장 최근 공시 1건. `with_source`면 **원문이 있는 것** 중 최신을 고른다."""
    rows = rows_of(corp, type_id, period)
    if not rows:
        return None
    if with_source:
        have = {r["rcept_no"] for r in store.manifest()}
        src = [r for r in rows if r["rcept_no"] in have]
        if src:
            return src[-1]
    return rows[-1]


def _smoke(corp=None):
    corps = [corp] if corp else ["삼성전자", "SK하이닉스", "한화오션", "KB금융"]
    for c in corps:
        print(f"\n=== {c}")
        for t in TYPES:
            r = count(c, t.id)
            if r["전체"] or r["자회사건"]:
                print(f"   {t.label[:30]:32s} 원본{r['원본']:4d} 정정{r['정정']:4d} "
                      f"자회사{r['자회사건']:3d} 원문{r['원문보유']:4d}")

if __name__ == "__main__":
    _smoke(sys.argv[1] if len(sys.argv) > 1 else None)
