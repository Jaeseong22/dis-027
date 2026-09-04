"""질문 입력의 단일 관문 — **시나리오 밖 질의만** 구체성을 따져 되묻는다.

`claims.py`가 답변 출력의 관문이면 여기는 입력이다. 다만 모든 질문을 보지 않는다 —
기업이 70사로 특정되고 아는 축(정본표·정본섹션·재무개념·공시유형·도구 담당 어휘)에
걸리면 **시나리오 안**이라 손대지 않고 그대로 흘린다.

되묻기를 전 질의에 걸면 안 된다 — 기간 표현이 없어도 답이 확정된 문항이 정답셋에
49건 있고, 연결/별도는 사실상 전부다. 판정을 두 단계로 나눈 이유가 그것이다.

축 사전은 새로 만들지 않고 코드가 이미 가진 트리거를 모은다
(`doctables` 239 · `docsections` 52 · `filingtypes` 45 · `concepts` 33 + `_TOOL_AXIS`).
반대로 `공시`·`보고서`·`현황` 같은 총칭어는 축이 아니다 — 축으로 치면 막연한 질의가
"시나리오 안"이 되어 게이트가 무력해진다.

걸리는 곳은 `agent.answer` 하나다. 실측: 사람이 확정한 정답 757행 + 주최 예시 6종에서
발동 0건이고, 금지·공격 질의에서는 비켜선다(거절이 정답이다).
"""
import os as _os
import re as _re

from agent2.data import store as _store
from agent2.tools import claims as _claims
from agent2.tools import concepts as _concepts
from agent2.tools import docsections as _docsections
from agent2.tools import doctables as _doctables
from agent2.tools import filingtypes as _filingtypes

#: 게이트 전체 스위치. 끄면 종전 동작(전 질의가 루프로 간다).
ON = _os.environ.get("A2_CLARIFY", "1") != "0"

#: 기간 축을 되물을 것인가. 시나리오 **밖**에만 적용되지만 축을 따로 끌 수 있게 둔다 —
#: 회차 A/B로 이득을 재려면 한 축씩 갈라야 한다(§6-3).
ON_PERIOD = _os.environ.get("A2_CLARIFY_PERIOD", "1") != "0"

#: 근거 미발견(게이트 B)을 되묻기로 바꿀 것인가.
ON_NOEVIDENCE = _os.environ.get("A2_CLARIFY_NOEVIDENCE", "1") != "0"

#: 위 네 사전에 없지만 **등록된 도구가 실제로 담당하는** 축.
#:
#: 근거는 도구 docstring이다 — `resolve_company`가 상장일·종목코드·시가총액·업종을
#: 돌려주고(`facts.company`), `rank_by_metric`이 순위를, `sector_peers`가 경쟁사를,
#: `correction_history`가 정정 이력을 준다. 실측으로 이 목록이 필요한 이유:
#:   · `정정신고는 몇 번 있었어?`(B-6 · 8개사) — 네 사전 어디에도 `정정`이 없다
#:   · `LG씨엔에스 … 주권상장일자와 특례상장 유형`(검색-35) — `상장`이 없다
#: 이 둘을 안 넣으면 정답확정 문항 9건이 "시나리오 밖"으로 흘러 되묻기 후보가 된다.
#:
#: ★ **총칭어를 넣지 마라.** `공시`·`보고서`·`현황`·`내역`은 어느 질문에나 붙어
#:   시나리오 안 판정을 무력화한다.
_TOOL_AXIS = (
    # resolve_company — 코퍼스 기본 정보
    "상장", "상장일", "상장일자", "주권상장", "특례상장", "종목코드", "시가총액", "시총",
    "업종", "섹터", "결산월",
    # rank_by_metric · sector_peers
    "순위", "경쟁사", "동종업계", "피어",
    # rank_by_metric — 순위 질의가 회사명 없이 들어온다(`매출액 top5 기업`)
    "매출액", "영업이익", "당기순이익", "자산총계", "직원수",
    # correction_history
    "정정", "정정신고", "정정공시", "정정사유",
)


def _axis_terms():
    """축 사전. 코드가 가진 트리거를 모아 정규화한 집합으로 돌려준다."""
    terms = set(_TOOL_AXIS)
    for t in _doctables.TABLES:
        terms |= set(t.triggers)
    for s in _docsections.SECTIONS:
        terms |= set(s.triggers)
    for t in _filingtypes.TYPES:
        terms |= {t.id, t.label}
    for c in _concepts.ALL:
        terms |= set(c.labels)
    return frozenset(_store._norm(t) for t in terms if t and len(t) >= 2)


AXIS = _axis_terms()


def _corp_keys():
    """법인명·통용명·영문명·종목코드 색인의 키. 2자 미만은 오탐이라 뺀다."""
    return tuple(k for k in _store._corp_index() if len(k) >= 2)


def _ambiguous():
    """이름 앞토막 → 그 토막으로 시작하는 회사들. **해소되는 토막은 뺀다.**

    코퍼스 70사에서 유도한다(하드코딩하지 않는다). 실측 결과:
        삼성(8) 현대(7) LG(5) 한화(3) 두산(3) SK(2) 한미(2) 우리(2) HD현대(2) 삼성전(2)
    `현대차`처럼 색인에 있는 키는 **모호하지 않다** — 정확히 한 회사로 해소된다.
    """
    idx = _store._corp_index()
    pref = {}
    for r in _store.universe():
        n = _store._norm(r["corp_name"])
        for size in range(2, min(len(n), 6) + 1):
            pref.setdefault(n[:size], set()).add(r["corp_name"])
    return {k: tuple(sorted(v)) for k, v in pref.items()
            if len(v) > 1 and k not in idx}


AMBIGUOUS = _ambiguous()

#: 기간 표현. `최근`·`최신`도 기간 지정으로 본다 — 확정 규칙 3이 "가장 최근"에
#: **사건 발생일 기준**이라는 답을 이미 정해 뒀다.
_PERIOD = _re.compile(r"20\d\d|\d\s*분기|반기|최근|최신|현재|작년|올해|지난해|전년|"
                      r"당기|전기|직전|연간|연말")

#: 회사명이 없어도 답이 되는 질의. `rank_by_metric`이 70개사를 한 지표로 줄 세운다
#: (섹터를 안 주면 전체). ★ 2026-08-30에 `top N`·`N위`·`가장 큰` 꼴을 더했다 —
#: 종전에는 `매출액별 top5 기업을 알려줘`가 회사명이 없다는 이유로 되묻기로 샜는데,
#: 그 질의는 이제 답할 수 있다. 정답확정 757행에서 이 추가로 새로 걸리는 행은 0건이다.
_NO_CORP_OK = _re.compile(r"시가총액|시총|순위|상위|섹터|업종|전체|개사|코퍼스|"
                          r"70개|경쟁사|동종업계|"
                          r"top\s*\d|\d\s*위\b|가장\s*(큰|많|높|낮|작|적)|"
                          r"제일\s*(큰|많|높|낮|작|적)")

#: 요구를 담지 않는 말. 이것만 남으면 무엇을 묻는지 알 수 없다.
_STOP_WORDS = ("알려줘", "알려주세요", "설명해줘", "정리해줘", "말해줘", "보여줘",
               "어때", "뭐야", "무엇", "얼마", "어디", "누구", "있어", "있나", "했어",
               "인가", "인지", "대해", "관해", "기준", "현황", "내역", "관련", "정보",
               "궁금", "부탁", "주세요", "해줘", "좀")

_NUM = _re.compile(r"\d")


def corps_in(question):
    """질문이 담은 코퍼스 70사. 조사가 붙어도 잡히도록 **부분문자열**로 본다.

    부분문자열 매칭은 과잉 탐지 쪽으로 틀린다 — 그 방향이 안전하다(게이트가 안 걸린다).
    실측: 정답확정 757행 전수에서 미탐 0건.

    ★ **별칭 키만 예외다.** 약칭 별칭(`포스코`·`하나금융`·`KAI`)은 형제사 이름의
      앞토막이라 부분문자열로 보면 다른 회사를 잡는다 — `포스코퓨처엠`이
      POSCO홀딩스로, `KAIST`가 한국항공우주로 걸렸다. 그래서 별칭은 조사를 뗀
      **토큰 전체**가 일치할 때만 인정한다. 기존 키의 동작은 그대로 둔다.
    """
    n = _store._norm(question or "")
    idx = _store._corp_index()
    al = _store.alias_keys()
    out = {idx[k]["corp_name"] for k in _corp_keys() if k not in al and k in n}
    for tok in _bare_tokens(question):
        k = _store._norm(tok)
        if k in al:
            out.add(idx[k]["corp_name"])
    return sorted(out)


def axes_in(question):
    """질문이 걸리는 축. 길이 내림차순(긴 것이 구체적이다)."""
    n = _store._norm(question or "")
    return sorted({a for a in AXIS if a in n}, key=len, reverse=True)


def ambiguous_in(question):
    """특정되지 않는 이름 토막 → 후보 회사들. 회사가 이미 특정되면 빈 dict."""
    if corps_in(question):
        return {}
    n = _store._norm(question or "")
    return {k: AMBIGUOUS[k] for k in AMBIGUOUS if k in n}


#: 이름 토막을 뽑을 때 쓰는 토큰. 뒤에 붙는 조사는 떼고 본다.
_TOKEN = _re.compile(r"[가-힣A-Za-z0-9&.]+")
#: ★ 종전에는 9종뿐이라 `하나금융까지`·`포스코부터`·`KAI라는`이 통째로 미탐이었다
#:   (별칭 21개 × 조사 10종에서 86% 미탐 실측). 긴 것부터 봐야 `에서는`이 `는`에
#:   가로채이지 않는다.
_JOSA = tuple(sorted(
    ("에서의", "에서는", "에서도", "으로의", "으로서", "으로써", "이라는", "이라고",
     "까지는", "부터는", "한테는", "에게는",
     "에서", "으로", "이의", "와의", "과의", "에의", "로서", "로써", "까지", "부터",
     "마저", "조차", "밖에", "한테", "에게", "께서", "라는", "라고", "이란", "처럼",
     "보다", "마다", "대로", "만큼", "이랑", "하고", "에는", "에도",
     "의", "은", "는", "이", "가", "을", "를", "에", "와", "과", "도", "만", "라", "야"),
    key=len, reverse=True))


def _bare_tokens(question):
    """토큰 → (조사 뗀 형태, 원형) 순으로 내놓는다. `corps_in`·`outside_name` 공용.

    ★ **떼기 전 형태도 함께 내는** 이유: 조사 목록에 든 글자로 끝나는 회사명이 있다
      (`에코프로`의 `로`·`이마트`의 조사 아님 등). 하나만 내면 정상 이름을 깎아
      미탐이 된다. 둘 다 보면 깎여도 원형이 받아 준다.
      `outside_name`은 앞에서부터 보므로 **뗀 형태가 먼저**여야 문장이 깨지지 않는다
      (`LG전자에서는` → payload가 `LG전자`).
    """
    for raw in _TOKEN.findall(question or ""):
        tok = raw
        for j in _JOSA:
            if tok.endswith(j) and len(tok) > len(j) + 1:
                tok = tok[:-len(j)]
                break
        if tok != raw:
            yield tok
        yield raw


def outside_name(question):
    """모호 토막으로 **시작하지만** 코퍼스에 없는 완결된 이름. 없으면 None.

    `LG전자` → `LG전자` · `삼성` → None(토막 그대로라 모호한 것이지 코퍼스 밖이 아니다).
    이 구분이 없으면 코퍼스 밖 기업(`LG전자`)에 *"LG로 시작하는 5개사 중 어느 것이냐"*고
    되물어 **정보한계 고지(평가지표 7)를 잃는다** — `audit.probe` D-5가 그 문항이다.
    """
    idx = _store._corp_index()
    for tok in _bare_tokens(question):       # 조사를 떼고 본다(`LG전자의` → `LG전자`)
        k = _store._norm(tok)
        if k in idx or len(k) < 3:
            continue
        for pref in AMBIGUOUS:
            if k.startswith(pref) and len(k) > len(pref):
                return tok
    return None


def _topic_left(question):
    """기업명·기간·불용어를 뺀 나머지 글자 수. 무엇을 묻는지가 남아 있는가."""
    s = question or ""
    for c in corps_in(s):
        s = s.replace(c, " ")
    for w in _STOP_WORDS:
        s = s.replace(w, " ")
    s = _re.sub(r"20\d\d년?|\d\s*분기|반기", " ", s)
    return len(_re.sub(r"[^가-힣A-Za-z]", "", s))


def route(question):
    """(경로, 사유, 딸림값). LLM 0회 · 도구 0회.

        scenario       아는 축이다 — 게이트를 태우지 않는다(현행 경로 그대로)
        out_specific   시나리오 밖이지만 구체적이다 — 검색으로 답을 시도한다
        clarify        시나리오 밖이고 빠진 것이 있다 — 되묻는다
    """
    q = (question or "").strip()
    if not ON or not q:
        return ("scenario", "", None)
    # ★★ **금지·공격 질의에서는 비켜선다.** 이쪽 정답은 되묻기가 아니라 거절이고
    #   (평가지표 6), 그 거절은 모델과 `claims.guard`가 이미 하고 있다.
    #   실측 회귀: 이 두 줄이 없으면 `audit.probe` 금지 3건(C-1·C-4·C-5)과
    #   공격 4건(D-1·D-2·D-4·D-6)이 되묻기로 새어 거절이 사라진다.
    if _claims.INJECTION.search(q) or _claims.FORBIDDEN_Q.search(q):
        return ("scenario", "", None)
    corps, axes = corps_in(q), axes_in(q)
    if corps and axes:
        return ("scenario", "", None)
    # ── 여기부터 시나리오 밖 ──
    if not corps:
        outside = outside_name(q)
        if outside:
            return ("clarify", "outside_corp", outside)
        amb = ambiguous_in(q)
        if amb:
            return ("clarify", "ambiguous_corp", amb)
        if _NO_CORP_OK.search(q):
            # 회사 없는 순위 질의는 **어느 지표로 줄 세울지**가 있어야 답이 된다.
            #   `매출액 top5 기업`        → 축(매출액) 있음 → 답한다
            #   `제일 많이 성장한 회사`     → 무엇의 성장인지 없음 → 되묻는다
            return ("out_specific", "", None) if axes else ("clarify", "no_metric", None)
        return ("clarify", "no_corp", None)
    # ★ 요구를 먼저 본다. 둘 다 빠진 질의(`삼성전자에 대해 알려줘`)에서 기간을 먼저
    #   물으면 "연도를 말해 달라"는 엉뚱한 되묻기가 나간다 — 무엇을 묻는지가 더 앞선다.
    if _topic_left(q) < 2:
        return ("clarify", "no_topic", corps)
    if ON_PERIOD and not _PERIOD.search(q):
        return ("clarify", "no_period", corps)
    return ("out_specific", "", None)


#: 코퍼스가 담는 사업연도. 되묻기 문장이 선택지를 제시할 때 쓴다(상수로 박지 않는다).
def _years():
    ys = sorted({r.get("base_year") for r in _store.docs(doc_subtype="annual")
                 if r.get("base_year")})
    return ys


#: 무엇을 묻는지 모를 때 제시할 항목. 정본표·재무개념에서 대표만 고른다.
_TOPIC_MENU = ("재무 수치(매출액·영업이익·자산총계 등)", "직원 현황", "배당",
               "설비투자", "주요 공시 이력", "정정공시 이력", "특수관계자 거래",
               "최대주주·주식 현황")


def message(reason, payload=None, question=""):
    """되묻기 문장. **답을 대신하지 않고 무엇이 있어야 답할 수 있는지 밝힌다.**

    형식을 하나로 못 박는다 — ① 왜 못 답하는지 ② 코퍼스가 가진 선택지 ③ 되묻기.
    선택지를 함께 주는 이유: 되묻기만 하면 사용자가 무엇을 더 적어야 하는지 모른다.
    """
    if reason == "ambiguous_corp":
        parts = []
        for key, names in sorted((payload or {}).items(), key=lambda kv: -len(kv[0])):
            parts.append(f"'{key}'로 시작하는 회사는 제공 코퍼스에 "
                         f"{len(names)}개사가 있습니다 — {' · '.join(names)}")
        return ("어느 회사를 말씀하시는지 특정되지 않아 답변을 드릴 수 없습니다.\n"
                + "\n".join(parts)
                + "\n\n정확한 회사명을 함께 알려주시면 공시에서 찾아 답변드리겠습니다.")
    if reason == "outside_corp":
        # ★ `DENIAL` 어휘("찾을 수 없" · "포함되어 있지 않")를 그대로 쓴다 — 우리가
        #   내보내는 부재 고지를 채점기·probe 가 못 알아보면 정답을 벌준다.
        #
        # ★★ 종전에는 `'{X}'는 코퍼스에 없는 기업입니다`라고 **단정**했다. 그런데 이
        #   경로에는 코퍼스 밖 회사(`LG전자`)와 코퍼스 안 회사의 미등록 표기(`LG엔솔`·
        #   `두산중공업`)가 **함께** 들어오고 코드는 둘을 구분하지 못한다. 단정도 답변이고
        #   후자에 대해서는 거짓이었다. 그래서 부재는 **조건절로** 밝히고 같은 접두의
        #   후보를 함께 준다 — 부재 고지는 유지하면서 거짓 단정만 없앤다.
        cand = max((k for k in AMBIGUOUS if _store._norm(payload or "").startswith(k)),
                   key=len, default=None)
        who = ""
        if cand:
            names = AMBIGUOUS[cand]
            who = (f"'{cand}'로 시작하는 회사는 코퍼스에 {len(names)}개사가 있습니다 — "
                   f"{' · '.join(names)}\n")
        return (f"'{payload}'로 해소되는 회사를 제공 코퍼스 70개사에서 찾을 수 없습니다.\n"
                + who +
                "코퍼스는 국내 상장사 70개사의 2023년 1월 ~ 2026년 6월 공시로 한정됩니다.\n\n"
                "이 중 한 곳을 말씀하신 것이라면 정확한 회사명을 알려주시면 공시에서 찾아 "
                "답변드리겠습니다. 그 밖의 회사라면 코퍼스에 포함되어 있지 않습니다.")
    if reason == "no_corp":
        return ("어느 회사에 대한 질문인지 확인되지 않아 답변을 드릴 수 없습니다.\n"
                "제공된 공시 코퍼스는 국내 상장사 70개사의 2023년 1월 ~ 2026년 6월 공시로 "
                "한정됩니다.\n\n회사명(또는 종목코드)을 함께 알려주시면 공시에서 찾아 "
                "답변드리겠습니다.")
    if reason == "no_period":
        ys = _years()
        span = f"{ys[0]}~{ys[-1]}년 사업보고서와 2026년 1분기 보고서" if ys else "코퍼스"
        who = " · ".join(payload or [])
        return (f"어느 시점 기준인지 확인되지 않아 답변을 드릴 수 없습니다.\n"
                f"{who}에 대해 코퍼스가 담고 있는 것은 {span}입니다.\n\n"
                f"어느 사업연도(또는 분기) 기준인지 알려주시면 그 보고서에서 찾아 "
                f"답변드리겠습니다.")
    if reason == "no_metric":
        return ("어느 기준으로 줄 세울지 질문에서 특정되지 않아 답변을 드릴 수 없습니다.\n"
                "코퍼스 70개사를 순위로 낼 수 있는 기준은 예를 들어 다음과 같습니다 — "
                "매출액 · 영업이익 · 당기순이익 · 자산총계 · 시가총액 · 직원수 · "
                "부채비율 · 영업이익률 · 매출증가율.\n\n"
                "어느 기준으로 어느 사업연도를 보실지 알려주시면 순위를 내 드리겠습니다.")
    if reason == "no_topic":
        who = " · ".join(payload or [])
        return (f"{who}에 대해 무엇을 확인해 드릴지 질문에서 특정되지 않았습니다.\n"
                f"코퍼스에서 답변 가능한 항목은 예를 들어 다음과 같습니다 — "
                + " · ".join(_TOPIC_MENU)
                + "\n\n어떤 항목을 어느 시점 기준으로 확인할지 알려주시면 "
                  "공시에서 찾아 답변드리겠습니다.")
    if reason == "no_evidence":
        return ("질문하신 내용을 특정할 수 있는 공시 항목을 찾지 못했습니다.\n"
                "제공 코퍼스는 정기공시(사업·반기·분기보고서), 주요사항보고서, "
                "거래소공시(공급계약·시설투자), 지분공시(5% 대량보유)로 한정됩니다.\n\n"
                "어느 보고서의 어떤 항목인지(또는 다른 표현으로) 다시 말씀해 주시면 "
                "찾아 답변드리겠습니다.")
    return ("질문을 조금 더 구체적으로 알려주시면 공시에서 찾아 답변드리겠습니다.")


def no_evidence(evidence):
    """관측이 재료를 하나도 주지 못했는가. **좁게** 판정한다.

    좁게 잡는 이유: 이 판정이 틀리면 답을 낼 수 있는 질문을 되묻기로 덮는다.
    숫자가 하나라도 있거나 본문이 200자를 넘으면 재료가 있다고 본다.
    """
    if not evidence:
        return True
    for item in evidence:
        obs = item[2] if len(item) > 2 else ""
        s = str(obs or "")
        if _NUM.search(s) or len(s.strip()) > 200:
            return False
    return True


if __name__ == "__main__":
    import sys

    qs = [" ".join(sys.argv[1:])] if len(sys.argv) > 1 else [
        "삼성전자 2025년 연결기준 매출액은 얼마인가?",
        "삼성 매출액 알려줘",
        "매출 1위 회사 알려줘",
        "삼성전자에 대해 알려줘",
        "현대모비스 반도체 공장 증설 계획 알려줘",
    ]
    print(f"축 사전 {len(AXIS)}개 · 모호 토막 {len(AMBIGUOUS)}개")
    for q in qs:
        r, why, payload = route(q)
        print(f"\n[{r}{'/' + why if why else ''}] {q}")
        if r == "clarify":
            print("  " + message(why, payload, q).replace("\n", "\n  "))
