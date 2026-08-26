"""답변 요건 — 질문 유형별로 **답에 반드시 들어가야 할 것**을 데이터로 둔다."""
import re
from collections import namedtuple

#: 요건 1개. `any_of` 중 하나라도 답변에 있으면 충족으로 본다.
Req = namedtuple("Req", "label any_of")
#: 질문 유형. `triggers`가 질의에 있으면 이 유형으로 본다.
Spec = namedtuple("Spec", "id name triggers requires seed")

#: 요건 작성에서 **제외한** 시나리오. 유형 규칙이 일반화되는지 확인하는 대조군이다.
HOLDOUT = ()

SPECS = (
    Spec("shares", "주식수",
         ("발행주식", "주식의 총수", "주식수", "우선주", "유통주식"),
         (Req("발행주식의 총수", ("발행주식의 총수", "발행주식총수", "발행 주식의 총수",
                              "발행주식 총수")),
          Req("자기주식수", ("자기주식",)),
          Req("유통주식수", ("유통주식", "유통 주식"))),
         seed="1-7"),

    Spec("headcount", "인원",
         ("직원수", "직원 수", "인원", "종업원", "직원이 몇"),
         (Req("총 직원수", ("합계", "총", "전체")),
          Req("사업부문별 구분", ("부문", "DX", "DS", "사업부문")),
          Req("소속 외 근로자 구분", ("소속 외", "소속외", "제외"))),
         seed="1-16"),

    Spec("pay", "급여",
         ("급여", "연봉", "보수", "인건비"),
         (Req("1인평균급여액", ("1인평균급여액", "1인평균 급여액", "1인 평균급여액")),
          Req("연간급여총액", ("연간급여총액", "연간급여 총액", "급여총액")),
          Req("근속연수와 구분", ("근속", "년"))),
         seed="1-17"),

    Spec("cost", "원가·비율",
         ("매출원가",),
         (Req("매출원가 금액", ("매출원가",)),
          Req("매출액 대비 비율", ("매출원가율", "매출액 대비", "대비 비율"))),
         seed="2-9"),

    # 가동률만 답하면 실무가 못 쓴다 — 공시 서식이 `생산능력·생산실적·가동률`을 한 표로
    Spec("utilization", "가동률",
         ("가동률", "가동 률", "생산능력", "생산 능력", "생산실적"),
         (Req("가동률", ("가동률", "%")),
          Req("생산능력", ("생산능력", "생산 능력", "가동가능", "생산 가능")),
          Req("생산실적", ("생산실적", "생산 실적", "실제 생산", "실제가동", "실제 가동"))),
         seed="2-6"),

    Spec("subsidiary", "종속회사",
         ("종속회사", "종속기업", "연결대상", "자회사"),
         (Req("합계 종속회사 수", ("합계", "합 계", "전체", "총")),
          Req("상장·비상장 구분", ("상장",)),
          Req("주요종속회사 수", ("주요종속회사", "주요 종속회사", "주요종속")),
          Req("전년 대비 증감", ("증가", "감소", "전년"))),
         seed="1-11"),

    # 트리거에서 맨 "설비"를 뺐다 — "생산설비 가동률" 질의까지 걸려
    Spec("capex", "투자계획",
         ("시설투자", "설비의 신설", "신설", "매입 계획", "투자 계획", "투자계획", "증설"),
         (Req("부문별 투자액", ("부문", "DS", "SDC")),
          Req("합계", ("합계", "총", "합 계")),
          Req("투자 내용·기간", ("신ㆍ증설", "신·증설", "증설", "보완", "투자기간"))),
         seed="2-7"),

    # 맨 "섹터"를 뺐다 — "반도체 섹터의 시가총액 순위" 같은 단순 랭킹 질의에
    Spec("sector", "섹터비교",
         ("동종", "경쟁사", "산업이 어떻", "업계", "섹터 산업", "섹터내", "섹터 내"),
         (Req("대상 기업 목록", ("SK하이닉스", "삼성전기", "한미반도체", "LG이노텍",
                             "SK", "LG", "경쟁사")),
          Req("3개년 재무 추이", ("2023", "2024", "제55기", "제56기", "3개년", "추이")),
          Req("재무 항목", ("매출", "영업이익", "당기순이익")),
          Req("기업별 사업 내용", ("사업", "제품", "산업"))),
         seed="2-2"),

    Spec("product", "제품·매출",
         ("주요사업", "주요 사업", "주요 제품", "주요제품", "사업을 정리", "매출 상품"),
         (Req("부문 구분", ("부문", "DX", "DS", "SDC")),
          Req("주요 제품", ("메모리", "스마트폰", "TV", "디스플레이", "제품")),
          Req("매출액", ("억원", "조원", "백만원", "매출액")),
          Req("매출 비중", ("비중", "%"))),
         seed="2-4"),

    Spec("ownership", "지분구조",
         ("지분구조", "최대주주", "대주주", "주주 현황", "경영진", "임원"),
         (Req("최대주주 및 특수관계인 지분율", ("최대주주", "특수관계")),
          Req("특수관계인 합산 지분율", ("합계", "합 계", "계 ", "합산", "총 지분")),
          Req("5% 이상 주주", ("5%", "주요 주주", "국민연금", "5퍼")),
          Req("등기임원 정보", ("등기임원", "사내이사", "대표이사", "직위"))),
         seed="1-8"),

    Spec("financial", "재무수치",
         ("매출", "영업이익", "순이익", "자산", "부채", "자본", "판관비", "판매비",
          "현금흐름", "재고자산", "주당", "opm", "gpm", "이익률"),
         (Req("단위 표기", ("원", "%", "배")),
          Req("연결/별도 명시", ("연결", "별도"))),
         seed="1-15"),
)

BY_ID = {s.id: s for s in SPECS}

#: **계수 질의** — "몇 번/몇 건 있었어?". 답이 숫자 하나뿐인 질문이다.
#:
_COUNT_Q = re.compile(r"몇\s*(번|건|개|차례)|횟수")


def classify(question):
    """질의 → 해당하는 유형 목록. **결정론 키워드 매칭**이다(LLM 분류 안 씀)."""
    q = (question or "").lower()
    if _COUNT_Q.search(q):
        return []
    return [s for s in SPECS if any(t.lower() in q for t in s.triggers)]


def requirements(question):
    """질의에 걸리는 요건 전부(중복 라벨 제거)."""
    seen, out = set(), []
    for s in classify(question):
        for r in s.requires:
            if r.label not in seen:
                seen.add(r.label)
                out.append((s.name, r))
    return out


def missing(question, answer):
    """답변에서 빠진 요건 목록. 결정론 어휘 검사."""
    a = (answer or "")
    # 정보가 없다고 정직하게 답한 경우는 요건을 강제하지 않는다.
    if re.search(r"확인되지\s*않|확인할\s*수\s*없|찾을\s*수\s*없", a) and len(a) < 200:
        return []
    return [(name, r) for name, r in requirements(question)
            if not any(k in a for k in r.any_of)]


def prompt_hint(question):
    """생성 시점에 프롬프트로 넣을 요건 안내. 없으면 빈 문자열."""
    reqs = requirements(question)
    if not reqs:
        return ""
    lines = ["이 유형의 답변에 반드시 포함해야 할 항목입니다 "
             "(근거에서 확인되는 것만 쓰고, 확인 안 되는 항목은 그렇다고 적으십시오):"]
    for name, r in reqs:
        lines.append(f"  · [{name}] {r.label}")
    return "\n".join(lines)


def retry_hint(question, answer):
    """검증 후 재생성용. 빠진 항목만 짚는다. 없으면 빈 문자열."""
    miss = missing(question, answer)
    if not miss:
        return ""
    lines = ["앞선 답변은 그대로 유지하십시오. 거기에 아래 항목만 덧붙여 "
             "**전체 답변을 다시 한 번 완성해** 주십시오. "
             "이미 적은 수치와 출처는 하나도 빠뜨리지 않고 그대로 포함합니다. "
             "덧붙일 항목이 근거에 없으면 그 항목에만 "
             "'공시에서 확인되지 않습니다'라고 적습니다:"]
    for name, r in miss:
        lines.append(f"  · [{name}] {r.label}")
    return "\n".join(lines)

if __name__ == "__main__":
    from agent2 import scenarios

    print(f"유형 {len(SPECS)}개 · 요건 {sum(len(s.requires) for s in SPECS)}개 · "
          f"홀드아웃 {len(HOLDOUT)}문항 {HOLDOUT}\n")
    for s in scenarios.SCENARIOS:
        types = [t.name for t in classify(s.q)]
        mark = "H" if s.id in HOLDOUT else " "
        print(f" {mark} [{s.id:4s}] {'·'.join(types) or '(유형 없음)':22s} {s.q[:44]}")
