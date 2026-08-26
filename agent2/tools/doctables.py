"""정본 표 레지스트리 — 비재무 표를 **결정론으로 골라 파싱**한다."""
import collections
import re
from collections import namedtuple
from functools import lru_cache

from agent2.data import parse as P
from agent2.data import store

#: 정본 표 1종.
#:   cells_all   **반드시** 있어야 하는 문자열(최소 조건)
#:   prefer_any  후보가 여럿일 때 **우선**할 문자열(형제 표를 가른다)
DocTable = namedtuple("DocTable",
                      "id name section_any cells_all prefer_any key_rows triggers")

TABLES = (
    DocTable("related_party", "특수관계자 거래",
             ("특수관계자",), (),
             ("연결", "매출거래", "매입거래", "재화의 매입", "수익", "비용", "채권", "채무"),
             ("합 계", "합계", "계", "총계", "전체 특수관계자 합계"),
             ("특수관계자", "특수관계자 거래", "특수관계자와의 거래", "계열사 거래",
              "내부거래", "관계사 거래")),
    DocTable("provisions", "충당부채",
             ("충당부채",), (),
             ("연결", "기초", "전입액", "당기말", "기말", "환입"),
             ("합 계", "합계", "계", "기타충당부채 합계"),
             ("충당부채", "공사손실충당부채", "하자보수충당부채", "법적소송충당부채",
              "충당부채 전입", "충당부채 환입")),
    DocTable("regional_sales", "지역별 매출",
             ("매출 및 수주", "매출실적", "매출에 관한"), ("국내", "해외"),
             ("연결", "비중", "매출액", "지역"),
             ("계", "합 계", "합계", "총계"),
             ("지역별 매출", "지역별", "국내외 매출", "해외 매출", "국내 매출",
              "지역별 매출 비중", "수출 비중")),
    DocTable("equity_method", "관계기업·공동기업 투자",
             ("관계기업", "공동기업", "지분법"), ("지분법손익",),
             ("연결", "기초", "취득", "당기말", "기말"),
             ("합 계", "합계", "계", "총계"),
             ("지분법", "지분법손익", "지분법 손익", "관계기업", "공동기업",
              "관계기업 투자", "지분법 적용")),
    DocTable("ppe_movement", "유형자산 증감",
             ("유형자산",), ("손상차손", "처분"),
             ("연결", "기초", "취득", "감가상각", "기말"),
             ("합 계", "합계", "계", "유형자산 합계"),
             ("유형자산 증감", "손상차손", "당기 손상", "유형자산 손상",
              "감가상각", "유형자산 변동")),
    DocTable("impairment", "유형자산 손상차손",
             ("유형자산", "손상"), ("손상차손누계액",),
             ("연결", "취득원가", "감가상각누계액", "장부금액"),
             ("합 계", "합계", "계", "장부금액 합계"),
             ("손상차손", "유형자산 손상", "손상차손누계", "자산손상")),
    DocTable("solvency", "지급여력(K-ICS)",
             ("재무건전성",), ("지급여력",),
             ("연결", "지급여력비율", "지급여력금액", "제76기"),
             ("지급여력비율", "위험기준 지급여력비율", "지급여력금액", "합 계", "합계"),
             ("지급여력", "K-ICS", "킥스", "RBC", "지급여력비율", "재무건전성",
              "보험금지급능력", "자본적정성")),
    DocTable("asset_quality", "여신건전성(고정이하여신)",
             ("재무건전성",), ("고정이하여신",),
             ("연결", "총여신", "무수익여신", "대손충당금"),
             ("고정이하여신비율", "고정이하여신 비율", "무수익여신비율",
              "대손충당금적립률", "총여신", "합 계", "합계"),
             ("고정이하여신", "NPL", "무수익여신", "여신건전성", "연체율",
              "대손충당금적립률", "자산건전성", "커버리지비율", "부실채권")),
    DocTable("affiliates", "계열회사 수(요약)",
             ("계열회사",), ("상장", "비상장"),
             ("기업집단", "계열회사의 수", "계"),
             ("계", "합 계", "합계", "총계"),
             ("계열회사", "기업집단", "계열사 수", "계열회사 현황", "계열회사 수",
              "소속 계열회사", "그룹 계열사")),
    DocTable("equity_invest", "타법인출자 현황(상세)",
             ("타법인출자",), ("법인명", "출자목적"),
             ("연결", "지분율", "장부가액", "기말잔액"),
             ("합 계", "합계", "계", "총계"),
             ("타법인출자", "타법인 출자", "출자 현황", "지분 취득", "타법인 주식",
              "출자증권", "피출자법인", "출자목적")),
    DocTable("contingent", "우발부채·약정사항",
             ("우발부채", "약정사항"), ("지급보증",),
             ("관련 기관", "대상 회사", "채권자", "연결", "보증금액"),
             ("합 계", "합계", "계", "총계"),
             ("우발부채", "약정사항", "지급보증", "계약이행보증", "선수금환급보증",
              "보증한도", "담보제공", "RG")),
    DocTable("rnd_pipeline", "연구개발 진행 현황",
             ("연구개발",), ("품목", "적응증"),
             ("개발단계", "임상", "연구시작", "승인일"),
             ("합 계", "합계", "계", "총계"),
             ("파이프라인", "임상", "임상단계", "적응증", "개발단계",
              "연구개발 진행", "신약", "품목(프로젝트)", "연구개발 실적")),
    DocTable("license_out", "라이선스아웃·기술이전 계약",
             ("라이선스", "기술이전"), (),
             ("계약상대", "마일스톤", "계약금", "총괄", "USD"),
             ("합 계", "합계", "계", "총계"),
             ("라이선스", "라이선스아웃", "기술이전", "L/O", "마일스톤",
              "계약금", "업프론트", "upfront", "기술수출")),
    DocTable("interest_income", "이자·비이자 부문 손익",
             ("영업의 현황", "영업부문", "순이자손익"), ("이자부문",),
             ("연결", "비이자부문", "영업이익", "순이자손익"),
             ("이자부문 > 소계", "비이자부문 > 소계", "수수료부문 > 소계",
              "기타영업부문 > 소계", "부문별 이익 합계", "합 계", "합계", "총계"),
             ("이자이익", "비이자이익", "이자부문", "비이자부문", "순이자손익",
              "이자수익 구성")),
    DocTable("unbilled", "미청구공사",
             ("미청구공사", "계약자산", "계약부채", "건설계약"), (),
             ("연결", "채권잔액", "대손충당금", "장부금액", "초과청구공사", "계약자산"),
             ("합 계", "합계", "계", "장부금액 합계"),
             ("미청구공사", "초과청구공사", "계약자산", "계약부채", "진행률 리스크")),
    DocTable("orders", "수주 상황",
             ("매출 및 수주", "기타 재무에 관한 사항"),
             ("수주잔고",),
             ("수주총액", "기납품액", "진행률", "미청구공사", "발주처", "이월 수주잔액",
              "당기 수주금액", "공사기한"),
             ("합 계", "합계", "소 계", "계", "총 계", "총계"),
             ("수주", "수주잔고", "수주총액", "수주잔액", "기납품액", "수주 현황", "신규 수주",
              "수주 상황", "Book-to-Bill", "미청구공사", "진행률", "공사기한", "발주처")),
    DocTable("shares", "주식의 총수",
             ("주식의 총수",),
             ("발행할주식의총수",), ("자기주식수", "유통주식수"),
             # 표에 '발행'이 붙은 행이 셋이다(Ⅱ 현재까지 발행 / Ⅳ 발행주식의 총수 / Ⅵ 유통).
             ("발행주식의 총수", "자기주식수", "유통주식수",
              "현재까지 발행한 주식의 총수", "현재까지 감소한 주식의 총수",
              "감자", "이익소각", "상환주식의 상환"),
             ("발행주식", "주식의 총수", "주식수", "자기주식", "유통주식", "우선주",
              "감자", "이익소각", "주식소각", "소각")),

    DocTable("share_history", "주식 발행일자별 내역",
             ("주식의 총수",),
             ("유상증자",), ("무상증자", "전환권행사", "액면가액"),
             (),
             ("유상증자", "무상증자", "전환사채", "신주인수권", "증자", "발행일자")),

    DocTable("employees", "직원 등의 현황",
             ("직원",),
             ("1인평균급여액",), ("평균근속연수", "연간급여"),
             ("합 계", "성별합계", "DX", "DS", "부문"),
             ("직원", "인원", "종업원", "급여", "연봉", "근속")),

    DocTable("subsidiary", "연결대상 종속회사 현황(요약)",
             ("회사의 개요",),
             ("연결대상회사수",), ("기말", "상장"),
             ("상장", "비상장", "합계"),
             ("종속회사", "종속기업", "연결대상", "자회사")),

    DocTable("inventory", "재고자산 세부내역(연결)",
             ("재고자산",),
             ("충당금",), ("연결",),
             ("재고자산",),
             ("재고자산", "취득원가", "평가충당금", "장부금액")),

    # 현금흐름표 — **3대 활동 합계 + IAS 7이 별도 표시를 요구하는 총액 항목**을 핵심행으로 둔다.
    #
    DocTable("cashflow", "연결 현금흐름표",
             ("현금흐름",),
             ("영업활동",), ("연결", "투자활동", "재무활동"),
             ("영업활동", "투자활동", "재무활동",
              # IAS 7.16 — 투자활동 총액 주요 항목
              "유형자산의 취득", "유형자산 취득", "무형자산의 취득", "무형자산 취득",
              "유형자산의 처분",
              # IAS 7.17 — 재무활동 총액 주요 항목
              # `유상증자`는 IAS 7.17(a) '지분상품의 발행에 따른 현금유입'이다.
              # 평가셋 #15 정답이 이 행을 근거로 삼는다(한화오션 3,497,092백만원).
              "유상증자", "유상증자로 인한 현금유입", "신주발행",
              "자기주식의 취득", "자기주식 취득",
              "사채의 발행", "단기차입금의 차입", "장기차입금의 차입",
              "사채의 상환", "단기차입금의 상환", "장기차입금의 상환", "차입금의 상환",
              # IAS 7.31 — 이자·배당은 각각 별도
              "배당금의 지급", "배당금지급", "배당금 지급",
              "이자의 지급", "이자지급", "이자의 수취", "이자수취",
              # IAS 7.35 — 법인세
              "법인세의 납부", "법인세 납부", "법인세납부"),
             ("현금흐름", "영업활동", "투자활동", "재무활동", "유상증자",
              "설비투자", "CAPEX", "capex", "투자활동현금흐름")),

    DocTable("raw_materials", "원재료 매입 현황",
             ("원재료", "생산설비"),
             ("품목",), ("매입유형", "매입액", "투입액", "구체적용도", "주요매입처", "비율"),
             ("합 계", "합계", "소계", "계"),
             ("원재료", "원자재", "주요 매입처", "매입처", "조달", "매입 현황",
              "원재료 현황", "외주", "부품 조달")),

    # 부문별 매출 — **fail-closed**로만 낸다(아래 `VERIFY`).
    # 라벨 열에 `부문`을 요구해 거래처별 매출표(두산퓨얼셀 `주요 매출처`)를 배제한다.
    DocTable("segment_sales", "부문별 매출 현황",
             ("사업의 내용", "이사의 경영진단"),
             ("부문",), ("비중", "주요 제품", "제품 및 서비스", "품목", "매출액"),
             ("합 계", "합계", "계", "소계"),
             # `상품`·`매출 상품`은 공시서식의 표준 어휘다(제품·상품·용역).
             ("부문별 매출", "매출 비중", "매출비중", "주요 제품", "매출 구성",
              "사업부문", "제품별 매출", "매출액 비중", "매출 상품", "주요 매출",
              "매출상품", "주력 제품", "주요 상품")),

    DocTable("dividend", "배당에 관한 사항",
             ("배당에 관한",),
             ("주당",), ("현금배당", "배당성향", "배당수익률"),
             ("주당 현금배당금(원)", "현금배당금총액(백만원)", "(연결)현금배당성향(%)",
              "현금배당수익률(%)", "(연결)주당순이익(원)"),
             ("배당", "배당금", "주당배당금", "배당성향", "배당수익률", "배당률",
              "현금배당", "배당 정책")),

    DocTable("major_holders", "5% 이상 주주",
             ("주주에 관한",),
             ("5%",), ("지분율", "소유주식수"),
             (),
             ("5% 이상", "주요 주주", "주주 현황", "지분구조", "대주주", "주식 소유")),

    # 섹션 제약을 두지 않는다 — 삼성전자는 `VIII. 임원 및 직원 등에 관한 사항`,
    DocTable("officers", "임원 현황(등기임원)",
             (),
             ("등기임원",), ("직위", "담당업무", "주요경력", "성명", "경력사항"),
             (),
             ("등기임원", "경영진", "임원", "대표이사", "사내이사", "이사회")),

    DocTable("unreg_officers", "미등기임원 보수 현황",
             ("임원 및 직원",),
             ("미등기임원", "연간급여"), ("1인평균급여액", "인원수"),
             ("미등기임원",),
             ("미등기임원", "미등기 임원", "미등기임원 보수", "임원 보수",
              "연간급여 총액", "1인평균 급여액")),

    DocTable("largest", "최대주주 및 특수관계인의 주식소유 현황",
             ("주주에 관한",),
             ("지분율",), ("관계", "최대주주", "특수관계"),
             ("계",),
             ("최대주주", "지분구조", "대주주", "주주 현황", "지분율")),

    DocTable("capex", "시설투자 현황",
             ("생산설비",),
             ("투자",), ("예상투자", "예정투자", "계획투자", "향후투자", "향후 투자"),
             ("합 계", "합계"),
             ("시설투자", "설비투자", "설비의 신설", "신설", "매입 계획",
              "투자 계획", "투자계획", "증설")),

    DocTable("rnd", "연구개발비용",
             ("연구개발활동",),
             ("연구개발비",), ("정부보조금", "매출액 비율", "회계처리", "회계 처리"),
             ("연구개발비용 총계", "연구개발비용 계", "정부보조금", "(정부보조금)"),
             ("연구개발비", "연구개발비용", "정부보조금", "연구개발활동",
              "연구개발 실적", "R&D")),

    DocTable("auditor", "회계감사인의 명칭 및 감사의견",
             ("외부감사",),
             ("감사인", "감사의견"), ("의견변형사유", "핵심감사사항", "강조사항"),
             (),
             ("감사인", "감사의견", "회계법인", "외부감사", "감사보고서",
              "적정의견", "감사받은", "회계감사")),

    DocTable("internal_control", "내부회계관리제도 감사의견",
             ("내부통제",),
             ("내부회계관리제도", "감사의견"), ("지적사항", "회사의대응조치", "사업연도"),
             (),
             ("내부회계관리제도", "내부회계", "내부통제")),

    DocTable("utilization", "생산능력·생산실적·가동률",
             ("생산설비",),
             ("가동률",), ("생산능력", "생산실적", "가동가능"),
             (),
             ("가동률", "생산능력", "생산실적", "공장 가동")),
)

BY_ID = {t.id: t for t in TABLES}

#: 정본표 → **어느 공시에서 찾을 것인가**(`filingtypes.TYPES`의 id).
#: 없으면 사업보고서에서 찾는다(기존 14종이 전부 그렇다).
#:
FILING_SOURCE = {}

_SP = re.compile(r"[\s　]+")
_UNIT = re.compile(r"\(\s*단위\s*[:：]\s*([^)]+)\)")


def _flat(table):
    return _SP.sub("", " ".join(" ".join(r) for r in table.grid))

PERIODIC_SUBTYPES = ("annual", "half", "quarter")


@lru_cache(maxsize=64)


def _tables_of(corp, year=None, filing_type=None, subtype="annual", month=None):
    """표 전체. 기본은 **해당 연도 사업보고서**, `filing_type`을 주면 그 공시."""
    if filing_type:
        from agent2.tools import filingtypes as _ft
        row = _ft.latest(corp, filing_type)
        rows = [row] if row else []
    else:
        year = year or store.latest_fiscal_year(corp)
        rows = store.docs(corp=corp, doc_subtype=subtype, base_year=year)
        if month:
            rows = [r for r in rows if r.get("base_month") == month]
    out = []
    for row in sorted(list(rows), key=lambda r: r["rcept_dt"]):     # 최신 정정본이 뒤에 온다
        res = P.parse_doc(row)
        for doc in (res if isinstance(res, list) else [res]):
            for tb in getattr(doc, "tables", []):
                # 표에 소속 회사를 새긴다 — 검증기가 XBRL과 **교차검증**할 때 필요하다
                # (`_verify_segment_by_ratio`: 비중 역산 총액 == 연결 매출액).
                tb.corp = corp
                # 출처 문서도 새긴다 — **접수번호 인용**에 쓴다. 주최 테크세션 자료의
                # 답변 예시가 전부 `(근거: 사업보고서 2025.12, 접수번호 20260310002820, …)`
                # 형식이고, 아키텍처 슬라이드가 "인용 강제"를 시스템 규칙으로 못 박았다.
                tb.doc = {"rcept_no": row.get("rcept_no"),
                          "report_nm": row.get("report_nm"),
                          "rcept_dt": row.get("rcept_dt"),
                          "is_correction": bool(row.get("is_correction"))}
                out.append(tb)
    return tuple(out)

TABLE_PIN = {
    # find() 가 이미 정확한 표를 고르는데 VERIFY 어휘(매입액|투입액|매입유형|매입처)가
    # 표에 없어서 기권하던 것들. 금액 열이 있고 합계·소계가 맞는 매입 현황 표다.
    ("레인보우로보틱스", "raw_materials"): {},          # 18행 유형|품목|수입여부|3개년 금액
    ("한국항공우주",     "raw_materials"): {},          # 7행 품목|3개년 · 합계 24,105
    ("OCI홀딩스",       "raw_materials"): {},          # 5행 사업부문|품 목|구체적 용도|3개년|주요 구매처

    # cells_all 이 안 맞아 후보에서 아예 빠지거나 엉뚱한 표가 뽑히던 것들.
    ("두산로보틱스",     "raw_materials"): {"cells_all": ("원재료명",), "rows_any": ("계",)},
    #   ↑ 표0(금액·계 9,338,503)과 표1(단가)이 **헤더가 완전히 같다**. `계` 행으로 가른다.
    #     핀이 없으면 `품 목`을 가진 **가동률 표**가 뽑혔다.
    ("디앤디파마텍",     "raw_materials"): {"cells_all": ("매입유형",)},   # 캡션 [매입현황 (연결…)]
    ("LIG디펜스앤에어로스페이스", "raw_materials"): {"cells_all": ("원재료매입액",)},  # 지배회사 2,970,058
    ("현대건설",        "raw_materials"): {"cells_all": ("주요자재", "구매금액")},
    #   ↑ `주요자재` 만으로는 5행 **가격표**(원/톤)가 같이 걸린다. `구매금액`으로 가른다.
    ("대우건설",        "raw_materials"): {"cells_all": ("주요자재",)},     # 매입액|비율|주요 매입처
    ("효성중공업",       "raw_materials"): {"cells_all": ("매입유형",)},
    #   ↑ `주요 원재료` 로 잡으면 **가격표**(품목|기수별 단가)와 겹친다.
    # 한화솔루션은 넣지 않는다 — 그 절에 **가격변동 표만** 있고 매입액 표가 없다(진짜 부재).

    ("SK하이닉스",       "segment_sales"): {"cells_all": ("사업부문", "주요상표")},
    ("LG에너지솔루션",    "segment_sales"): {"cells_all": ("사업부문", "수요변동요인")},
    ("에코프로비엠",      "segment_sales"): {"cells_all": ("품목", "주요상표")},
    ("현대제철",        "segment_sales"): {"cells_all": ("영업유형",)},
    ("삼성화재해상보험",   "segment_sales"): {"cells_all": ("국내사업", "해외사업"),
                                          "rows_any": ("매출액",)},
    #   ↑ 금융지주 6사 중 **삼성화재만** 부문별 *매출* 표를 갖는다
    #     (국내사업 23,947,419 · 해외사업 674,739 · 기타 537,085 · 합계 24,778,520 백만원).
    #     신한·하나·우리·메리츠·삼성생명은 부문표가 **당기순이익/영업손익** 기준이라
    #     `segment_sales`(부문별 매출)가 아니다 — 은행지주는 매출 개념으로 공시하지 않는다.
    ("LG씨엔에스",       "segment_sales"): {"cells_all": ("구분", "품목", "매출액", "비율"),
                                          "rows_any": ("클라우드",)},
    ("알테오젠",        "segment_sales"): {"cells_all": ("품목/프로젝트", "제품설명")},
    ("한미약품",        "segment_sales"): {"cells_all": ("부문", "구분"), "rows_any": ("의약품",)},
    ("디앤디파마텍",      "segment_sales"): {"cells_all": ("매출유형", "건수", "비중")},
    ("우리기술",        "segment_sales"): {"cells_all": ("총매출액", "내부매출액")},
    ("LG생활건강",       "segment_sales"): {"cells_all": ("주요제품", "주요상표")},
    ("삼성E&A",        "segment_sales"): {"cells_all": ("구분", "매출액", "비율"),
                                          "rows_any": ("화공",)},
    #   ↑ `구분`이 없으면 주요고객 표(`주요고객|매출액|비율|영업부문`)가, `비율`이 없으면
    #     부문요약 표(`구분|화공|첨단산업|New Energy|합계`)가 함께 걸린다. 셋을 다 요구해야 갈린다.
    ("현대글로비스",      "segment_sales"): {"cells_all": ("구분", "금액", "비중"),
                                          "rows_any": ("해운",)},
    #   ↑ `사업부문|매출유형|내수/수출` 표는 **별도 기준**이라 합계가 22,531,798백만원이다.
    #     맞는 표는 `구분|금 액|비 중(%)` 쪽으로 합계 **29,566,409백만원 = XBRL 연결매출과 일치**.
    ("하이브",         "segment_sales"): {"cells_all": ("구분", "품목", "매출액", "비중"),
                                          "rows_any": ("음반",)},
    ("와이지엔터테인먼트",  "segment_sales"): {"cells_all": ("구분", "매출액", "비율"),
                                          "rows_any": ("음악서비스",)},

    ("파마리서치",       "rnd"): {"cells_all": ("매출액대비비율",)},

    ("KB금융",           "asset_quality"): {"cells_all": ("고정이하여신비율", "2025년말")},
    ("현대로템",         "contingent"): {"cells_all": ("지급보증", "관련 기관")},
    ("두산에너빌리티",   "orders"): {"rows_any": ("Jawaharpur", "ObraC")},
    #   ↑ `주설비공사`는 두 표에 다 있다. 40행에만 있는 라벨은 `Jawaharpur`·`ObraC` 둘뿐이다
    #     (59행에만 있는 것은 AlKhalij·DohaRO·JafurahCogen 등). 라벨 집합의 차집합으로 확정했다.
    ("한국항공우주",     "orders"): {"cells_all": ("기초", "기말"), "rows_any": ("방산",)},
}


def _pin(corp, table_id):
    """그 (기업, 표)의 핀. 없으면 None."""
    return TABLE_PIN.get((corp, table_id))


def _rows_any_ok(t, want):
    """라벨 열에 `want` 중 하나가 있나."""
    if not want:
        return True
    for row in t.grid:
        lab = " ".join(str(c or "") for c in row[:2]).replace(" ", "")
        if any(w.replace(" ", "") in lab for w in want):
            return True
    return False


def find(corp, table_id, year=None, _tables=None, subtype="annual", month=None):
    """정본 표 1개. 못 찾으면 None(억지로 비슷한 표를 주지 않는다)."""
    spec = BY_ID.get(table_id)
    if spec is None:
        raise KeyError(f"미등록 정본 표: {table_id} — TABLES에 먼저 등록할 것")
    pin = _pin(corp, table_id)
    need_cells = tuple(pin["cells_all"]) if pin and "cells_all" in pin else spec.cells_all
    need_rows = tuple(pin.get("rows_any") or ()) if pin else ()
    cands = []
    src = FILING_SOURCE.get(table_id)
    for t in (_tables if _tables is not None
              else _tables_of(corp, year, src, subtype, month)):
        sp = " ".join(t.section_path)
        if spec.section_any and not any(p in sp for p in spec.section_any):
            continue
        body = _flat(t)
        # `body`는 `_flat()`이 공백을 지운 문자열이므로 원문의 `품 목`·`가동 률`은
        if all(c.replace(" ", "") in body for c in need_cells) and _rows_any_ok(t, need_rows):
            # 우선 토큰은 **본문과 섹션 경로 양쪽**에서 본다. 재고자산 주석은 연결·별도가
            # 같은 형태이고 `III. 재무에 관한 사항 > 8. 재고자산 (연결)`처럼
            pref = sum(1 for pcell in spec.prefer_any if pcell in body or pcell in sp)
            # 우선 토큰이 맞아도 **값이 하나도 없는 표는 우대하지 않는다.**
            if pref and not _has_values(t):
                pref = 0
            doc = getattr(t, "doc", None) or {}
            recency = (doc.get("rcept_dt") or "", 1 if doc.get("is_correction") else 0)
            cands.append((pref, recency, len(t.grid), t))
    if not cands:
        return None
    # 우선순위: 우선토큰 적중 → **최신 판본** → 행 수
    ranked = [x[3] for x in sorted(cands, key=lambda x: (x[0], x[1], x[2]), reverse=True)]
    if pin:                       # 사람이 확인한 핀이면 VERIFY 를 다시 묻지 않는다
        return ranked[0]
    return _first_valid(table_id, ranked) or ranked[0]


def _has_values(t):
    """헤더를 뺀 본문에 **숫자 셀이 하나라도** 있나. 첫 칸(라벨 열)은 세지 않는다."""
    for row in t.grid[_head_rows(t):]:
        for c in row[1:]:
            s = (c or "").strip()
            if s and _NUMISH.match(s) and any(ch.isdigit() for ch in s):
                return True
    return False


def _first_valid(table_id, ranked):
    """순위대로 보면서 **`VERIFY`를 통과하는 첫 후보**를 고른다. 없으면 None."""
    check = VERIFY.get(table_id)
    if check is None:
        return None
    for t in ranked:
        try:
            if check(t):
                return t
        except Exception:
            continue
    return None

_NUMISH = re.compile(r"^[\d,.\-()%~ㆍ·\s△▲]+$")


def _is_header(row):
    """헤더 행인가 — 첫 칸을 뺀 나머지에 숫자 셀이 거의 없으면 헤당더로 본다."""
    rest = [c.strip() for c in row[1:] if c and c.strip()]
    if not rest:
        return False
    numish = sum(1 for c in rest if _NUMISH.match(c))
    return numish <= len(rest) * 0.3


def _head_rows(table_or_grid, grid=None):
    """헤더 행 수 — **마크업을 먼저 믿는다.**"""
    if grid is None:                       # `_head_rows(table)` 형태
        table, grid = table_or_grid, getattr(table_or_grid, "grid", table_or_grid)
        n = getattr(table, "header_rows", 0) or 0
    else:                                  # `_head_rows(None, grid)` 형태
        n = 0
    if n:
        n = min(n, len(grid))
        for i in range(n):
            if any(_MONEYCELL.match((c or "").strip()) for c in grid[i]):
                return i
        return n
    n = 0
    for row in grid[:5]:
        if _is_header(row):
            n += 1
        else:
            break
    return n

#: 콤마 천단위 금액 셀. 뒤에 `(50.2%)`처럼 비중이 붙는 표기까지 받는다.
_MONEYCELL = re.compile(r"^\(?\d{1,3}(,\d{3})+(\.\d+)?\)?(\s*\(\s*[\d.]+\s*%?\s*\))?$")


def column_names(grid, n_head=None):
    """열 번호 → 열 이름. 헤더 행들을 열 단위로 이어 붙인다."""
    heads = grid[:n_head] if n_head is not None else []
    if n_head is None:
        for row in grid[:5]:
            if _is_header(row):
                heads.append(row)
            else:
                break
    if not heads:
        return {}
    width = max(len(r) for r in heads)
    names = {}
    for i in range(width):
        parts = []
        for r in heads:
            c = (r[i] if i < len(r) else "").strip()
            if c and c not in parts:
                parts.append(c)
        # 상위 헤더를 다 이으면 이름이 길어져(`직원 직 원 수 기간의 정함이없는 근로자 전체`)
        # 관측이 2,500자 한도에서 잘린다. **가장 구체적인 뒤 2단**만 쓴다.
        names[i] = _SP.sub(" ", " ".join(parts[-2:])).strip()
    return names


def rows_of(table):
    """표 → [(라벨, {열이름: 값})]. 헤더 행은 제외한다."""
    grid = table.grid
    n_head = _head_rows(table)
    names = column_names(grid, n_head)
    out = []
    for row in grid[n_head:]:
        cells = [(c or "").strip() for c in row]
        if not any(cells):
            continue
        label = cells[0]
        vals, seen_lbl, sub = {}, False, []
        for i, c in enumerate(cells):
            if i == 0:
                continue
            if not seen_lbl and c == label:      # COLSPAN으로 반복된 라벨 칸
                continue
            seen_lbl = True
            nm = names.get(i) or f"열{i}"
            if nm == names.get(0):
                if c and c != label and not _VALUE_CELL.match(c):
                    sub.append(c)
                continue
            if c:
                vals[nm] = c
        if sub:
            label = " > ".join([label] + sub) if label else " > ".join(sub)
        out.append((label, vals))
    return out

#: 값 셀 판정 — 하위 라벨로 접어 넣을지 가른다(숫자는 값이지 라벨이 아니다).
_VALUE_CELL = re.compile(r"^[\s(（△▲+\-−]*[\d,]+(\.\d+)?\s*[)）%]?\s*$")


def unit_of(table):
    """캡션·표 안에서 `(단위 : …)`를 찾는다. 값과 함께 줘야 단위 오답을 막는다."""
    for src in (table.caption or "", " ".join(" ".join(r) for r in table.grid[:3])):
        m = _UNIT.search(src)
        if m:
            return _SP.sub(" ", m.group(1)).strip()
    return None

#: 행 라벨 앞의 항번호. `Ⅱ. 현재까지 발행한 주식의 총수`에서 `Ⅱ.`를 벗긴다.
_ENUM = re.compile(r"^[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩIVX\d가나다라마바사아자차]{1,4}\s*[.)]\s*")


def _key_rank(label, keys):
    """핵심행 우선순위 — 등록 순서를 그대로 쓴다. 매칭 안 되면 큰 수."""
    lab = _SP.sub("", _ENUM.sub("", (label or "").strip()))
    for i, k in enumerate(keys):
        kk = _SP.sub("", k)
        if lab == kk or lab.startswith(kk):
            return i
    return len(keys) + 1


def _is_key(label, keys):
    """핵심 행 판정."""
    lab = _SP.sub("", _ENUM.sub("", (label or "").strip()))
    for k in keys:
        kk = _SP.sub("", k)
        if lab == kk or lab.startswith(kk):
            return True
    return False

# ── fail-closed 검증 ────────────────────────────────────────────────────
#: 표를 내기 **전에** 통과해야 하는 검사. 실패하면 `values()`가 None을 준다
#: (= 검색 경로로 폴백). selective prediction의 reject option에 해당한다.
VERIFY = {}

_RATIO_HDR = re.compile(r"비중|비율|구성비|점유|증감률|%")
_AMT_HDR = re.compile(r"매출액|금액|매출|수익")


def _seg_value_col(grid, n_head):
    """금액 열. **헤더가 답을 써 뒀다** — 숫자 밀도로 고르면 비중 열을 집는다."""
    if not grid:
        return None
    w = max(len(r) for r in grid)
    hdr = ["".join(grid[i][c] if c < len(grid[i]) else "" for i in range(n_head))
           for c in range(w)]
    cands = []
    for c in range(w):
        n = sum(1 for r in grid[n_head:] if _n(r[c] if c < len(r) else "") is not None)
        if n < 3 or _RATIO_HDR.search(hdr[c]):
            continue
        cands.append((0 if _AMT_HDR.search(hdr[c]) else 1, c))
    return min(cands)[1] if cands else None

#: 비중 역산 총액과 연결매출의 허용 오차. 표는 반올림해 싣고(78.2), 회사가 부문
_RATIO_TOL = 0.02


def _verify_segment_by_ratio(t, grid, n_head):
    """비중 열로 역산한 총액이 **연결 매출액(XBRL)**과 맞는가."""
    from agent2.tools import xbrl as _x           # 순환 임포트 회피

    hdr = [" ".join(grid[i][j] for i in range(n_head) if j < len(grid[i]))
           for j in range(len(grid[0]))]
    amt = next((j for j, h in enumerate(hdr) if "금액" in h or "매출액" in h), None)
    rat = next((j for j, h in enumerate(hdr) if "비중" in h), None)
    if amt is None or rat is None:
        return False
    def collect(rows_only_sales):
        got = []
        for row in grid[n_head:]:
            if rows_only_sales and not re.search(r"매출|수익", " ".join(row[:2])):
                continue
            a = _n(row[amt] if amt < len(row) else "")
            b = _n(row[rat] if rat < len(row) else "")
            if a and b and b > 0:
                got.append(a / b * 100.0)
        return got

    # 매출 행만 고르는 게 기본이다 — 같은 표에 영업이익·총자산 행이 섞이면 총액이 다르다.
    implied = collect(True)
    if not implied and re.search(r"매출|수익", hdr[amt]):
        implied = collect(False)
    if not implied:
        return False
    med = sorted(implied)[len(implied) // 2]
    scale = _unit_scale_of(t)
    rev = _x.pick(_x.facts(t.corp), "revenue") if getattr(t, "corp", None) else None
    if rev is None or not rev.krw:
        return False
    return abs(med * scale - rev.krw) <= rev.krw * _RATIO_TOL


def _unit_scale_of(t):
    """표 단위 → 원 배수. 비중 역산 총액을 XBRL(원)과 맞추려면 필요하다."""
    u = unit_of(t) or ""
    for tok, s in (("조원", 1e12), ("억원", 1e8), ("백만원", 1e6), ("천원", 1e3)):
        if tok in u:
            return s
    return 1e6                                     # 공시 표 기본 표시단위(실측 최빈)


def _verify_segment(t):
    """부문합 == 총계인가. **원본 셀만** 더한다(복제분은 세지 않는다)."""
    try:
        grid, n_head, org = P.expand_grid(t.raw or "", with_origins=True)
    except Exception:
        return False
    hdr = " ".join(" ".join(r) for r in grid[:n_head])
    if _POINT_HDR.search(hdr) and not re.search(r"매출|영업수익", hdr):
        return False
    vc = _seg_value_col(grid, n_head)
    if vc is None or len(grid) - n_head < 3:
        return False
    vhdr = " ".join(grid[i][vc] for i in range(n_head) if vc < len(grid[i]))
    if re.search(r"생\s*산|매\s*입|투\s*입", vhdr):
        return False

    def val(i):
        return _n(grid[i][vc]) if vc < len(grid[i]) else None

    def is_origin(i, c):
        return org[i][c] if i < len(org) and c < len(org[i]) else True

    #: 2단 라벨이 이 중 하나면 **매출 행만** 쓴다(요약표 구분 열)
    sales_lv2 = ("매출액", "매출", "수익")
    has_lv2 = any(_SP.sub("", (grid[i][1] if len(grid[i]) > 1 else "")) in sales_lv2
                  for i in range(n_head, len(grid)))

    groups, cur = [], None
    for i in range(n_head, len(grid)):
        lab0 = grid[i][0] if grid[i] else ""
        if is_origin(i, 0) and _SP.sub("", lab0):
            cur = {"label": lab0, "rows": []}
            groups.append(cur)
        if cur is None:
            cur = {"label": lab0, "rows": []}
            groups.append(cur)
        cur["rows"].append(i)

    comps, totals = [], []
    for g in groups:
        if _is_key(g["label"], _SEG_TOT) and len(g["rows"]) == 1:
            v = val(g["rows"][0])
            if v is not None:
                totals.append(v)          # 총계는 여럿일 수 있다
            continue
        rows = g["rows"]
        if has_lv2:                       # 요약표 → 매출 행만
            rows = [i for i in rows
                    if _SP.sub("", grid[i][1] if len(grid[i]) > 1 else "") in sales_lv2]
        sub = [i for i in rows
               if len(grid[i]) > 1 and _is_key(grid[i][1], _SEG_TOT)]
        if sub:                           # 그룹 소계가 있으면 그것만 센다(중복 방지)
            v = val(sub[-1])
            if v is not None:
                comps.append(v)
            continue
        vs = [v for i in rows if is_origin(i, vc) and (v := val(i)) is not None]
        if vs:
            comps.append(sum(vs))
    if totals and comps:
        s = sum(comps)
        if any(t0 and abs(t0 - s) <= abs(t0) * 0.005 for t0 in totals):
            return True
    return (_verify_segment_by_ratio(t, grid, n_head)
            or _verify_segment_by_arith(grid, n_head, vc))

#: 헤더가 **시점(잔액)**을 가리키는 표기. 매출은 기간 개념이라 이것만 있으면 매출표가 아니다.
_POINT_HDR = re.compile(r"당기말|전기말|기말\s*현재|말\s*잔액")

#: `_verify_segment` **전용** 총계 라벨 — 전역 `_TOTAL_LABELS`에 `소계`가 없다.
#:
#: 전역을 안 건드리는 이유: `_computed_totals`는 소계를 **빼야** 하는 쪽이라
#: (삼성전자 `성별합계`를 부문과 같은 층으로 세면 정확히 2배가 된다) 의미가 반대다.
_SEG_TOT = ("합 계", "합계", "총 계", "총계", "계", "소 계", "소계")


def _verify_segment_by_arith(grid, n_head, vc):
    """총계 **라벨**을 못 찾은 표에서, 총계 행을 **산술로** 식별한다."""
    vhdr = " ".join(grid[i][vc] for i in range(n_head) if vc < len(grid[i]))
    if not re.search(r"매출|금\s*액|영업수익|수익|제\s*\d+\s*기|20\d\d", vhdr):
        return False
    vals = [v for i in range(n_head, len(grid))
            if (v := (_n(grid[i][vc]) if vc < len(grid[i]) else None)) is not None]
    if len(vals) < 3:
        return False
    tot = sum(vals)
    # 어떤 행의 값이 **나머지 행 합**과 같으면 그 행이 총계다(부문합 = 총계와 동치).
    return any(v and abs(v - (tot - v)) <= abs(v) * 0.005 for v in vals)

VERIFY["segment_sales"] = _verify_segment


def _verify_raw_materials(t):
    """원재료 매입표인가 — **금액 열(매입/투입)이 있어야 한다.**"""
    f = _flat(t)
    return bool(re.search(r"매입액|투입액|매입\s*유형|매입처", f))

VERIFY["raw_materials"] = _verify_raw_materials

#: 금융지주회사 **산업 전체 통계**의 표지. 한 회사의 여신건전성 표에는 나올 수 없는 라벨이다.
_INDUSTRY_HDR = re.compile(r"소속회사수|연결총자산|당기순이익\(개별합산\)|이중레버리지비율")


def _verify_asset_quality(t):
    """**그 회사 자신의** 여신건전성 표인가 — 산업 통계표를 막는다."""
    return not _INDUSTRY_HDR.search(_flat(t))

VERIFY["asset_quality"] = _verify_asset_quality


def _verify_officers(t):
    """**등기임원** 표인가 — `등기임원 여부` 열에 `미등기`가 아닌 값이 있어야 한다."""
    n_head = _head_rows(t)
    head = t.grid[0] if t.grid else []
    col = next((i for i, x in enumerate(head) if "등기임원" in (x or "")), None)
    if col is None:
        return False
    for r in t.grid[max(n_head, 1):]:
        v = (r[col] if col < len(r) else "").strip()
        if v and "미등기" not in v and "등기임원" not in v and "여부" not in v:
            return True
    return False

VERIFY["officers"] = _verify_officers


def _verify_largest(t):
    """**주식소유 현황** 표인가 — 헤더에 `관계`/`구분` 열이 있어야 한다."""
    n_head = _head_rows(t)
    col = None
    for r in t.grid[:max(n_head, 1)]:
        for i, x in enumerate(r):
            if _SP.sub("", x or "") in ("관계", "구분"):
                col = i
                break
        if col is not None:
            break
    if col is None:
        return False
    # 열만 있고 값이 없는 표를 통과시키지 않는다(유령 열 방지).
    return any((r[col] if col < len(r) else "").strip()
               for r in t.grid[max(n_head, 1):])

VERIFY["largest"] = _verify_largest


def _verify_major_holders(t):
    """**5% 이상 주주 현황** 표인가 — 헤더에 `주주명` 열이 있어야 한다."""
    n_head = _head_rows(t)
    for r in t.grid[:max(n_head, 1)]:
        for x in r:
            if _SP.sub("", x or "") == "주주명":
                return True
    return False

VERIFY["major_holders"] = _verify_major_holders

#: 세로병합 복제 수치를 렌더링에서 **지울** 표. 검증만 마스크를 쓰면 모델은 여전히
#: 복제값을 본다 — 카카오는 부문 매출 4,318,175가 톡비즈·포털비즈·플랫폼기타 3개 행에
#: 반복돼 "톡비즈 매출 4.3조"로 답할 수 있다. 원본 셀에만 수치를 남기고
#: 연장분은 `〃(위 부문 합계)`로 바꿔 **무엇의 값인지**를 명시한다.
#:
SPAN_AWARE = {"segment_sales"}
_DITTO = "〃(위 행과 병합된 부문 합계)"


def _blank_spans(t, rows):
    """복제된 **수치** 셀을 ditto로 바꾼다. 라벨·텍스트 셀은 그대로 둔다."""
    try:
        grid, n_head, org = P.expand_grid(t.raw or "", with_origins=True)
    except Exception:
        return rows
    # `expand_grid`의 `n_head`는 마크업 기준이다 — `column_names`에 그대로 넘겨
    names = column_names(grid, n_head)
    out, ri = [], n_head
    for lab, vals in rows:
        if ri >= len(grid):
            out.append((lab, vals)); continue
        new_vals = {}
        for k, v in vals.items():
            ci = next((i for i, nm in names.items() if nm == k), None)
            dup = (ci is not None and ri < len(org) and ci < len(org[ri])
                   and not org[ri][ci] and _n(v) is not None)
            new_vals[k] = _DITTO if dup else v
        out.append((lab, new_vals)); ri += 1
    return out


def _table_level(t, rows):
    """데이터 행 **전체가 하나의 병합 셀**인 수치 열을 행에서 떼어 낸다.
    `({열이름: 값}, 남은 rows)`."""
    try:
        grid, n_head, org = P.expand_grid(t.raw or "", with_origins=True)
    except Exception:
        return {}, rows
    if len(grid) - n_head < 2:            # 데이터 행이 1개면 '전체 걸침'이 무의미하다
        return {}, rows
    names = column_names(grid, n_head)
    lifted = {}
    for ci, nm in names.items():
        origins = [ri for ri in range(n_head, len(grid))
                   if ci < len(org[ri]) and org[ri][ci]]
        if len(origins) != 1:             # 행마다 자기 셀이 있으면 행 수준 데이터다
            continue
        row = grid[origins[0]]
        if ci < len(row) and _n(row[ci]) is not None:
            lifted[nm] = row[ci].strip()
    if not lifted:
        return {}, rows
    return lifted, [(lab, {k: v for k, v in vals.items() if k not in lifted})
                    for lab, vals in rows]

#: `제57기` 같은 기수 표기. 표의 열 이름·행 라벨에 쓰인다.
_PERIOD = re.compile(r"제\s*(\d+)\s*기")


def _year_labels(rows, base_year):
    """열 이름의 `제N기`에 **역년을 박는다** — `제57기` → `2025년(제57기)`."""
    if not base_year:
        return rows
    ns = set()
    for lab, vals in rows:
        for k in list(vals) + [lab]:
            ns |= {int(m) for m in _PERIOD.findall(k or "")}
    if not ns:
        return rows
    top = max(ns)

    def stamp(s):
        s = s or ""

        def one(m):
            yr = base_year - (top - int(m.group(1)))
            # 이미 그 연도가 적혀 있으면 건드리지 않는다 — 현대자동차 가동률 표는
            # 열 이름이 `2024년 (제57기)`라서, 덧씌우면 `2024년 (2024년(제57기))`가 된다.
            return m.group(0) if str(yr) in s else f"{yr}년(제{m.group(1)}기)"

        return _PERIOD.sub(one, s)

    return [(stamp(lab), {stamp(k): v for k, v in vals.items()}) for lab, vals in rows]


def _cite(doc):
    """근거 표기 문자열. **접수번호를 반드시 포함**한다."""
    nm = doc.get("report_nm") or "공시"
    out = f"{nm}, 접수번호 {doc.get('rcept_no')}"
    return out + "(정정본)" if doc.get("is_correction") else out


def cache_path(corp, table_id, year, subtype="annual", month=None):
    """정본표 결과 캐시 파일 경로. `warmup`이 '무엇이 찬지'를 이 함수로 판단한다 —
    캐시 규칙이 두 군데로 갈라지지 않게 한 곳에서만 정한다(`parse.pdf_cache_path`와 같은 규율).
    """
    import hashlib
    import os
    from agent2 import config
    cdir = os.path.join(config.AGENT_DIR, ".cache", "canon_tables")
    if os.path.commonpath([os.path.abspath(cdir),
                           os.path.abspath(config.CORPUS_DIR)]) == os.path.abspath(config.CORPUS_DIR):
        raise RuntimeError(f"캐시가 코퍼스 안을 가리킨다: {cdir}")
    os.makedirs(cdir, exist_ok=True)
    _sfx = "" if (subtype == "annual" and not month) else f"\x00{subtype}\x00{month}"
    key = hashlib.sha1(
        f"{corp}\x00{table_id}\x00{year}\x00{_code_version()}{_sfx}".encode("utf-8")
    ).hexdigest()[:16]
    return os.path.join(cdir, key + ".json")

#: 캐시 키에 섞을 **코드 지문**. 한 번만 계산한다.
_CODE_VER = None


def _code_version():
    """표 선택·렌더링을 정하는 소스의 해시. **캐시 무효화용**이다."""
    global _CODE_VER
    if _CODE_VER is None:
        import hashlib
        import os
        h = hashlib.sha1()
        here = os.path.dirname(os.path.abspath(__file__))
        # 표의 격자를 만드는 `parse`까지 포함한다 — 둘 중 하나만 바뀌어도 값이 달라진다.
        for p in (os.path.abspath(__file__),
                  os.path.join(os.path.dirname(here), "data", "parse.py")):
            try:
                with open(p, "rb") as f:
                    h.update(f.read())
            except OSError:
                h.update(p.encode("utf-8"))   # 못 읽으면 경로만 — 캐시를 막지는 않는다
        _CODE_VER = h.hexdigest()[:12]
    return _CODE_VER

#: 정본표 결과를 디스크에 캐시할지. 끄려면 `CANON_CACHE=0`.
#:
_DISK_CACHE = __import__("os").environ.get("CANON_CACHE", "1") != "0"

_NUMISH = re.compile(r"^[\d,.\-()%\s]*$")


def _disambiguate(rows):
    """라벨이 중복되는 행에 **구분 열 값을 라벨에 합쳐** 유일하게 만든다."""
    seen = collections.Counter(lab for lab, _ in rows)
    if all(v == 1 for v in seen.values()):
        return rows
    groups = collections.defaultdict(list)
    for i, (lab, vals) in enumerate(rows):
        groups[lab].append(i)
    out = list(rows)
    for lab, idxs in groups.items():
        if len(idxs) < 2:
            continue
        # 구분 열 — 비수치 열 중 그룹 안에서 값이 갈리는 첫 열
        cols = [c for c in rows[idxs[0]][1]
                if not _NUMISH.match(str(rows[idxs[0]][1].get(c) or ""))]
        disc = next((c for c in cols
                     if len({str(rows[i][1].get(c) or "") for i in idxs}) == len(idxs)), None)
        if disc is None:
            continue
        for i in idxs:
            v = str(rows[i][1].get(disc) or "").strip()
            if v:
                out[i] = (f"{lab} · {v}", rows[i][1])
    return out

#: `부문 × 구분` 2차원 표에 **계산된 합계**를 붙일 표. 확정 규칙 4가 "부문별 합산"이라
#: 합계 행이 없는 회사에서는 모델이 직접 더해야 하고, 거기서 틀린다
COMPUTED_TOTALS = {"employees"}

#: **원문 라벨 오타 보정** — `(기업, 표, 틀린 라벨) → 올바른 라벨`.
#:
#: 한화에어로스페이스 직원표는 같은 방산 부문의 남/여 행을 `방산`·`빙산`으로 적었다.
#: **원문 XBRL도 둘 다 `ACODE="EMP_OPR"`(부문명)로 달아** 격자·속성 어느 쪽으로도
LABEL_TYPO = {("한화에어로스페이스", "employees", "빙산"): "방산"}


def _fix_label_typo(corp, table_id, rows):
    """원문 라벨 오타를 `LABEL_TYPO`대로 고친다. 등록이 없으면 그대로 둔다."""
    if not any(k[0] == corp and k[1] == table_id for k in LABEL_TYPO):
        return rows
    out = []
    for lab, vals in rows:
        head, sep, tail = lab.partition(" · ")
        fixed = LABEL_TYPO.get((corp, table_id, head))
        out.append(((fixed + sep + tail) if fixed else lab, vals))
    return out


def _computed_totals(rows):
    """`부문 · 구분` 표의 두 방향 합계를 계산해 `{라벨: {열: 값}}`으로 돌려준다."""
    tot = [(lab, v) for lab, v in rows if _is_key(lab, _TOTAL_LABELS)]
    seg = [(lab, v) for lab, v in rows if not _is_key(lab, _TOTAL_LABELS)]
    if len(tot) != 1 or len(seg) < 2:
        return {}
    split = [(lab.rsplit(" · ", 1), v) for lab, v in seg]
    if any(len(p) != 2 for p, _v in split):
        return {}                       # `_disambiguate`가 안 갈랐다 = 2차원이 아니다
    parts = [(p[0], p[1], v) for p, v in split]

    subtotal = set()
    segs = {d for d, _k, _v in parts}
    for d in segs:
        mine = [v for dd, _k, v in parts if dd == d]
        rest = [v for dd, _k, v in parts if dd != d]
        if not rest:
            continue
        agree = tie = 0
        for c in mine[0]:
            a = [x for x in (_n(v.get(c)) for v in mine) if x is not None]
            b = [x for x in (_n(v.get(c)) for v in rest) if x is not None]
            if not a or not b:
                continue
            tie += 1
            if abs(sum(a) - sum(b)) < 0.5:
                agree += 1
        if tie and agree * 2 > tie:     # 과반 열에서 '나머지의 합'이면 소계다
            subtotal.add(d)
    if subtotal:
        parts = [(d, k, v) for d, k, v in parts if d not in subtotal]

    if len({d for d, _k, _v in parts}) < 2:
        return {}                       # 부문이 1개면 계산값이 합계 행과 같다

    # 가산 가능 열 — 원문 합계 행과 부문 합이 일치하는 열만
    addable = []
    for c in tot[0][1]:
        tv = _n(tot[0][1].get(c))
        svs = [x for x in (_n(v.get(c)) for _d, _k, v in parts) if x is not None]
        if tv is not None and svs and abs(sum(svs) - tv) < 0.5:
            addable.append(c)
    if not addable:
        return {}

    def _sum(sel):
        out = {}
        for c in addable:
            xs = [x for x in (_n(v.get(c)) for v in sel) if x is not None]
            if xs:
                s = sum(xs)
                out[c] = f"{s:,.0f}" if s == int(s) else f"{s:,}"
        return out

    made = {}
    by_kind = collections.defaultdict(list)
    by_seg = collections.defaultdict(list)
    for d, k, v in parts:
        by_kind[k].append(v)
        by_seg[d].append(v)
    for k, sel in by_kind.items():
        if len(sel) > 1:                # 부문이 여럿일 때만 — 1개면 그 행 자체가 답이다
            made[f"전 부문 합계 · {k}"] = _sum(sel)
    for d, sel in by_seg.items():
        if len(sel) > 1:                # 그 부문에 구분이 여럿일 때만
            _ks = "+".join(k for k in dict.fromkeys(k for _d, k, _v in parts if _d == d))
            made[f"{d} · {_ks} 합계"] = _sum(sel)
    return {k: v for k, v in made.items() if v}


def _arith_key(out):
    """핵심행이 **하나도 없을 때**, 산술로 총계인 행을 찾아 핵심행으로 표시한다."""
    if any(r.get("key") for r in out) or len(out) < 3:
        return out
    cols = set()
    for r in out:
        cols |= set(r["values"])
    cols = [c for c in cols
            if sum(1 for r in out if _n(r["values"].get(c)) is not None) >= len(out) - 1]
    if not cols:
        return out
    for i, r in enumerate(out):
        agree = tie = 0
        for c in cols:
            mine = _n(r["values"].get(c))
            rest = [x for x in (_n(y["values"].get(c)) for j, y in enumerate(out) if j != i)
                    if x is not None]
            if mine is None or not rest:
                continue
            tie += 1
            if abs(mine - sum(rest)) < max(1.0, abs(mine) * 0.001):
                agree += 1
        if tie and agree * 2 > tie:
            r["key"] = True
            r["key_arith"] = True
            return out                 # 총계는 하나다 — 첫 번째만 표시하고 멈춘다
    return out


def values(corp, table_id, year=None, _tables=None, subtype="annual", month=None):
    """정본 표 → 구조화 dict. 못 찾거나 **검증에 실패하면** None."""
    import json as _json
    import os as _os
    cp = None
    if _DISK_CACHE and _tables is None:
        year = year or store.latest_fiscal_year(corp)
        cp = cache_path(corp, table_id, year, subtype, month)
        if _os.path.exists(cp):
            try:
                with open(cp, encoding="utf-8") as fh:
                    return _json.load(fh)
            except (ValueError, OSError):
                pass                       # 깨진 캐시는 무시하고 다시 만든다
    out = _values_uncached(corp, table_id, year, _tables, subtype, month)
    if cp:
        try:
            tmp = cp + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                _json.dump(out, fh, ensure_ascii=False)
            _os.replace(tmp, cp)           # 원자적 교체 — 반쯤 쓴 파일을 읽지 않는다
        except OSError:
            pass
    return out


def _values_uncached(corp, table_id, year=None, _tables=None,
                     subtype="annual", month=None):
    t = find(corp, table_id, year, _tables=_tables, subtype=subtype, month=month)
    if t is None:
        return None
    check = VERIFY.get(table_id)
    if check is not None and _pin(corp, table_id) is None and not check(t):
        return None                       # fail-closed — 검색 경로로 폴백
    spec = BY_ID[table_id]
    rows = rows_of(t)
    if table_id in SPAN_AWARE:
        rows = _blank_spans(t, rows)
    common, rows = _table_level(t, rows)
    rows = _year_labels(rows, year or store.latest_fiscal_year(corp))
    unit = unit_of(t)
    rows = _fix_label_typo(corp, table_id, rows)
    rows = _disambiguate(rows)
    computed = _computed_totals(rows) if table_id in COMPUTED_TOTALS else {}
    out = []
    for lab, vals in rows:
        key = _is_key(lab, spec.key_rows)
        out.append({"label": lab, "values": vals, **({"key": True} if key else {})})
    out = _arith_key(out)              # 라벨로 못 잡은 총계 행을 산술로 찾아 표시
    # 핵심행을 앞으로, 그 안에서는 **등록 순서**대로. 절단돼도 중요한 행이 살아남는다.
    out.sort(key=lambda r: (not r.get("key"), _key_rank(r["label"], spec.key_rows)))
    doc = getattr(t, "doc", None) or {}
    return {"table": spec.name, "id": spec.id,
            "section": " > ".join(t.section_path[-2:]),
            **({"출처": _cite(doc)} if doc.get("rcept_no") else {}),
            "unit": unit,
            # 단위를 **문장으로** 함께 준다. unit 필드만 두면 모델이 놓치고 다른 단위로
            "unit_note": (f"이 표의 모든 수치 단위는 {unit}입니다. 답변에 이 단위를 그대로 쓰십시오."
                          if unit else "단위 표기가 없습니다."),
            "key_rows": [r["label"] for r in out if r.get("key")],
            **({"표_각주": getattr(t, "footnote", "")} if getattr(t, "footnote", "") else {}),
            **({"표_공통값(특정 행·부문·성별에 속하지 않음)": common} if common else {}),
            **({"표_계산합계(원문에 없음·아래 행들을 더한 값)": computed} if computed else {}),
            "rows": out,
            "n_rows": len(rows)}

#: 구조적 불변식 — **어느 기업에서나 참이어야 하는 관계**.
#: 기업별 정답을 만들 수 없으므로(70개사 × 표 8종) 이걸로 "맞는 표인가"를 검증한다.
#: 재무에서 `자산 = 부채 + 자본`(IAS 1)을 쓴 것과 같은 방식이다.
#: 깨지면 **그 기업에서 표를 잘못 골랐다는 신호**이고, 값을 몰라도 검출된다.
#: 근거: 템플릿 기반 추출은 기업별 레이아웃 변형에 취약하고 씨앗 과적합 위험이 있다
#: . 탐지율만으로는 "찾았는가"만 알 뿐 "맞는가"를 모른다.
_TOTAL_LABELS = ("합 계", "합계", "총 계", "총계", "계")

INVARIANTS = {
    "subsidiary": ("상장 + 비상장 = 합계", ("상장", "비상장")),
    "capex": ("부문별 합 = 합계", None),
    "segment_sales": ("부문별 합 = 합계", None),
}


def _n(x):
    """표 셀 → 숫자. `△`·`▲`·괄호를 음수로 읽는다."""
    t = (x or "").strip()
    if not re.search(r"\d", t):
        return None
    neg = t.startswith(("△", "▲", "-", "−")) or (t.startswith("(") and t.endswith(")"))
    try:
        v = float(re.sub(r"[^\d.]", "", t))
    except ValueError:
        return None
    return -v if neg else v


def check_invariant(corp, table_id, year=None, _tables=None):
    """(설명, 부분합, 합계, 통과여부) 또는 None(불변식 미등록·표 없음·판정불가)."""
    inv = INVARIANTS.get(table_id)
    if inv is None:
        return None
    desc, parts = inv
    v = values(corp, table_id, year, _tables=_tables)
    if not v:
        return None
    rows = v["rows"]
    tot = next((r for r in rows if _is_key(r["label"], _TOTAL_LABELS)), None)
    if tot is None:
        return None
    cands = {k: _n(x) for k, x in tot["values"].items()}
    cands = {k: x for k, x in cands.items() if x}
    if not cands:
        return None
    col = max(cands, key=lambda k: abs(cands[k]))
    total = cands[col]
    part_rows = [r for r in rows
                 if r is not tot and not _is_key(r["label"], _TOTAL_LABELS)
                 and (parts is None or _is_key(r["label"], parts))
                 and _n(r["values"].get(col)) is not None]
    if not part_rows:
        return None
    ssum = sum(_n(r["values"][col]) for r in part_rows)
    # 허용오차는 **표시 정밀도**에서 나온다 — 행마다 반올림이 half-ulp까지 생기므로
    ok = abs(ssum - total) <= max(len(part_rows) / 2.0, 1)
    return (desc, ssum, total, ok)


def match(query):
    """질의 → 원하는 정본 표 id 목록. 결정론 키워드 매칭."""
    q = (query or "").lower()
    qz = _SP.sub("", q)
    hits = []
    for t in TABLES:
        best = max((len(k) for k in t.triggers if _trigger_hit(t.id, k, q, qz)),
                   default=0)
        if best:
            hits.append((best, t.id))
    hits.sort(key=lambda x: -x[0])
    return [tid for _, tid in hits]

#: 순수 ASCII 트리거(영문·숫자). 이런 것만 낱말 경계를 요구한다.
_ASCII_TRIG = re.compile(r"^[A-Za-z0-9&./-]+$")

_ANTI_TRIGGER = {
    ("raw_materials", "조달"): ("자금조달", "자금 조달"),
}


def _trigger_hit(tid, k, q, qz):
    """트리거 하나가 질의에 걸리는가. 낱말 경계와 부정 문맥을 함께 본다."""
    kl = k.lower()
    if _ASCII_TRIG.match(k):
        # 앞뒤가 영숫자면 더 긴 영단어의 일부다(merger 안의 rg).
        if not re.search(r"(?<![a-z0-9])" + re.escape(kl) + r"(?![a-z0-9])", q):
            return False
    elif kl not in q and _SP.sub("", kl) not in qz:
        return False
    return not any(a.lower() in q or _SP.sub("", a.lower()) in qz
                   for a in _ANTI_TRIGGER.get((tid, k), ()))

if __name__ == "__main__":
    import sys

    corp = sys.argv[1] if len(sys.argv) > 1 else "삼성전자"
    tabs = _tables_of(corp, 2025)
    print(f"{corp} FY2025 · 표 {len(tabs):,}개\n")
    for spec in TABLES:
        v = values(corp, spec.id, 2025, _tables=tabs)
        if v is None:
            print(f"■ {spec.name} — 없음")
            continue
        print(f"■ {spec.name}  [{v['section']}]  단위 {v['unit']}  {v['n_rows']}행")
        for r in v["rows"][:6]:
            print(f"     {r['label'][:26]:28s} {' | '.join(r['values'][:4])[:66]}")
        print()
