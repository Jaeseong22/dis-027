"""개념 레지스트리 — 재무 항목의 **표준화 계층**.

## 이 파일의 모든 기준은 표준에서 왔다 (CLAUDE.md '근거 원칙')

  · 개념 목록      **IAS 1.54**(재무상태표 최소 표시 항목) · **IAS 1.82**(손익 최소 표시 항목)
  · 업종별 차이    **IAS 1.55/57/86** — "기업의 재무상태 이해에 목적적합하면 항목 추가·재배열 허용".
                  은행에 `매출액` 행이 없는 것은 **표준이 허용한 정상 상태**이지 결측이 아니다.
                  XBRL 산업 택소노미(`us-gaap-ci`/`basi`/`ins`)가 같은 이유로 분리돼 있다.
  · 매핑 형식      **EdgarTools** — SEC GAAP 태그 약 18,000개를 정규 개념 84~95개로,
                  `개념 → 라벨 배열` 형태로 매핑. 단일 라벨로 찾지 않는다.
  · 결측 구분      **Compustat** — `.0001` 없음 / `.0004` 다른 항목에 합산 / `.0008` 미미.
                  한 종류로 뭉개지 않는다.
  · line_item vs calculable  **Compustat** — 직접 보고된 항목과 계산된 금액을 구분한다.
                  은행 영업수익(순이자손익+순수수료손익)은 **calculable**이므로 그렇게 표시한다.
  · as-reported 링크  **Compustat** — 표준화 값에서 원문 라벨로 되짚을 수 있어야 한다(`Pick`·`Audit`).

## 라벨은 코퍼스 전수에서 도출했다

`python3 -m agent2.tools.derive_labels` — 70개사 FY2025 정식 재무제표 표의 행 라벨 전수 집계.
고유 라벨은 BS 1,428종 · IS 944종이었고, 아래 `labels`는 그중 **2개사 이상이 쓰는 것**만 담았다.
괄호 안 숫자는 사용 기업 수(실측).

## `note`의 `실측 N사`가 무슨 축인가 (2026-08-16 전수 재측정)

세 축이 섞여 있다. **주석 문구가 축을 말한다**:

    "실측 N사"          그 라벨이 재무제표 어디든 등장하는 **기업 수**(연결·별도 합침)
    "XBRL 실측 N/70사"  `xbrl.pick`이 그 개념을 해소하는 기업 수 — 라벨이 아니라 XBRL 축
    revenue만            **연결 손익계산서 첫 행**이 그 라벨인 기업 수(top-line 축)

★ 축을 확인하지 않고 대조하면 없는 결함이 생긴다 — 2026-08-16에 `실측 N사`를
  XBRL 해소 수와 비교해 **14건을 전부 오탐**으로 만들었다.

★ **숫자는 파싱이 좋아질 때마다 위로 움직인다.** 같은 날 재측정에서 12건이 어긋났고
  (`sga` 29→47 · `cost_of_sales` 35→53 · `ppe` 54→70 · 매출액 28→40 …),
  12건은 **정확히 일치**했다(`operating_income` 47+27 · `eps_basic` 27+8+52 ·
  `gross_profit` 52 · `total_ci` 32 · `nci` 66 · 업종 고유 6건 전부).
  일치하는 것이 이만큼이면 축은 맞고 **드리프트는 파싱 커버리지 증가**다.
  라벨 **선택**은 다시 확인했고 바뀌지 않았다 — 숫자만 갱신했다.
"""
from collections import namedtuple

# ----------------------------------------------------------------- 상태 코드
#: **결측을 한 종류로 뭉개지 않는다.** 근거는 두 표준이 같은 방향을 가리킨다:
#:
#:  XBRL 결측 규칙
#:    `xsi:nil="true"`  정보가 **알려지지 않았거나 해당되지 않음**을 명시적으로 보고
#:    fact 생략          *"Unknown or not applicable information should not be included
#:                       in an instance. The according fact should be left out."*
#:    required value not reported  필수 값이 없으면 **오류**
#:  Compustat 결측 코드
#:    `.0001` Not Available · `.0004` Combined Figure · `.0008` Insignificant
#:
#: 그래서 "해당 없음"과 "못 찾음"은 **다른 상태**이고, 후자만 조사 대상이다.
NOT_APPLICABLE = None               # 이 회사가 그 항목을 표시하지 않음 — 정상(IAS 1.55 · XBRL 생략)
NOT_FOUND = "NOT_FOUND"             # **필수 개념인데** 못 찾음 → 오류. 조사 대상
COMBINED = "COMBINED"               # 다른 항목에 합산·계산됨 (Compustat .0004)
UNIT_UNKNOWN = "UNIT_UNKNOWN"       # 찾았으나 단위 미상 → 값 배제
INSIGNIFICANT = "INSIGNIFICANT"     # 회사가 미미하다고 보고 (Compustat .0008)

#: **필수 개념** — IAS 1.54/82가 전 업종에 요구하는 것. 이것만 못 찾으면 `NOT_FOUND`(오류)다.
#: 나머지는 회사가 표시하지 않았을 뿐이므로 `NOT_APPLICABLE`(None)로 둔다.
REQUIRED_ALL = ("assets_total", "liabilities_total", "equity_total",
                "operating_income", "net_income")

#: 보고 프로파일. XBRL 산업 택소노미 구분을 따른다.
PROFILES = ("ci", "basi", "ins", "sec")

Concept = namedtuple("Concept", "id statement kind labels applies derive note max_depth")
Concept.__new__.__defaults__ = (None, "", None)


def C(cid, statement, kind, labels, applies, derive=None, note="", max_depth=None):
    """`max_depth` — 원문 들여쓰기 계층 상한. None이면 제한 없음.

    왜 개념별인가: 계층 위치가 개념마다 다르다(실측).
      손익  `순이자손익`·`매출액`은 **L0**(소계 층). 하위 세부를 잡으면 오판한다 —
            KB금융에서 `보험수익`(L1)을 잡아 은행지주를 보험사로 판정했다.
      재무  `자산총계`는 `자산`(L0) 아래 **L1**, `부채총계`·`자본총계`는 **L2**다.
            IAS 1.54의 총계는 IAS 1.85A의 소계와 층위가 다르다.
    일괄 상한을 걸었다가 재무상태표가 통째로 빠진 사고가 있었다.
    """
    return Concept(cid, statement, kind, tuple(labels), applies, derive, note, max_depth)


# ----------------------------------------------------------------- 재무상태표
#: IAS 1.54 — 재무상태표 최소 표시 항목. 자산·부채·자본 구조는 **업종 무관**하다
#: (IAS 1.54가 금융기관에 예외를 두지 않는다). 실측도 자산총계 69사·자본총계 70사로 일치.
BALANCE_SHEET = [
    C("assets_total", "BS", "line_item",
      ["자산총계", "자산 총계", "자산"],
      {p: "required" for p in PROFILES},
      note="IAS 1.54 — 전 업종 공통. 실측 69/70사"),
    C("liabilities_total", "BS", "line_item",
      ["부채총계", "부채 총계", "부채"],
      {p: "required" for p in PROFILES},
      note="IAS 1.54. 실측 69/70사"),
    C("equity_total", "BS", "line_item",
      ["자본총계", "자본 총계", "자본"],
      {p: "required" for p in PROFILES},
      note="IAS 1.54. 실측 70/70사"),
    C("inventories", "BS", "line_item",
      ["재고자산"],
      {"ci": "required", "sec": "n/a", "basi": "n/a", "ins": "n/a"},
      note="IAS 1.54(g). 실측 47사 — 금융업엔 재고 개념이 없다(IAS 1.55)"),
    C("ppe", "BS", "line_item",
      ["유형자산"],
      {p: "optional" for p in PROFILES}, note="IAS 1.54(a). 실측 70사"),
    C("intangibles", "BS", "line_item",
      ["무형자산"],
      {p: "optional" for p in PROFILES}, note="IAS 1.54(c). 실측 67사"),
    C("cash", "BS", "line_item",
      ["현금및현금성자산", "현금 및 현금성자산"],
      {p: "optional" for p in PROFILES}, note="IAS 1.54(i)"),
    C("nci", "BS", "line_item",
      ["비지배지분"],
      {p: "optional" for p in PROFILES}, note="IAS 1.54(q). 실측 66사"),
    C("retained_earnings", "BS", "line_item",
      ["이익잉여금"],
      {p: "optional" for p in PROFILES}, note="실측 58사"),
    # ── 유동/비유동 구분 (IAS 1.60) ──────────────────────────────────
    # "기업은 유동자산과 비유동자산, 유동부채와 비유동부채를 **구분 표시**한다."
    # 금융업은 IAS 1.60 단서(유동성 순서 표시가 더 목적적합한 경우)에 따라 구분하지 않으므로
    # `basi`/`ins`는 n/a다 — 실측도 금융 10사에서 이 행이 없다.
    # ★ 이 개념들이 `xbrl.py`에만 있고 여기 없어서 **라벨 경로가 통째로 없었다.**
    #   그러면 XBRL 값을 교차검증할 방법이 없다(`scale_audit`이 '라벨없음'으로 세었다).
    C("current_assets", "BS", "line_item",
      ["유동자산", "유동자산계", "Ⅰ.유동자산"],
      {"ci": "required", "sec": "n/a", "basi": "n/a", "ins": "n/a"},
      note="IAS 1.60. XBRL 실측 62/70사", max_depth=1),
    C("current_liabilities", "BS", "line_item",
      ["유동부채", "유동부채계", "Ⅰ.유동부채"],
      {"ci": "required", "sec": "n/a", "basi": "n/a", "ins": "n/a"},
      note="IAS 1.60. XBRL 실측 62/70사", max_depth=1),
    # IAS 1.54(m) 재무상태표 최소 표시 항목의 금융부채.
    C("short_term_borrowings", "BS", "line_item",
      ["단기차입금", "단기차입부채"],
      {"ci": "optional", "sec": "optional", "basi": "optional", "ins": "optional"},
      note="IAS 1.54(m). XBRL 실측 35/70사", max_depth=2),
]

# ----------------------------------------------------------------- 손익
#: IAS 1.82 — 손익 최소 표시 항목. **revenue는 IAS 1.82(a)로 요구되지만**,
#: IAS 1.55/57이 업종별 재배열·대체 표시를 허용하므로 금융업은 다른 항목으로 표시한다.
#: 실측이 이를 확인한다 — 금융 8개사의 연결 손익 본표에 `매출액` 행은 **0회**다.
INCOME_STATEMENT = [
    C("revenue", "IS", "line_item",
      ["매출액", "수익(매출액)", "매출", "영업수익", "매출액및지분법손익"],
      {"ci": "required", "sec": "required", "basi": "n/a", "ins": "n/a"},
      note="IAS 1.82(a). 실측(연결 손익계산서 **첫 행**) 매출액 40사 · 영업수익 11사 · 수익(매출액) 9사. "
           "금융지주·보험은 IAS 1.55에 따라 다른 항목으로 표시 → NOT_APPLICABLE",
      max_depth=0),
    C("cost_of_sales", "IS", "line_item",
      ["매출원가"],
      {"ci": "required", "sec": "n/a", "basi": "n/a", "ins": "n/a"},
      note="실측 53사", max_depth=0),
    C("gross_profit", "IS", "calculable",
      ["매출총이익"],
      {"ci": "optional", "sec": "n/a", "basi": "n/a", "ins": "n/a"},
      derive=("subtract", "revenue", "cost_of_sales"),
      note="실측 52사. 보고되면 line_item, 없으면 매출액-매출원가로 계산", max_depth=0),
    C("sga", "IS", "line_item",
      ["판매비와관리비", "판매비와 관리비"],
      {"ci": "optional", "sec": "optional", "basi": "n/a", "ins": "n/a"},
      note="실측 47사", max_depth=0),
    # IAS 1.82(c) — "지분법으로 회계처리하는 관계기업과 공동기업의 당기순손익에 대한 지분".
    # 라벨이 `지분법손실`로 오는 회사가 많다(손실 표시가 기본이고 부호가 손익을 가른다).
    C("equity_method_income", "IS", "line_item",
      ["지분법손익", "지분법이익", "지분법손실", "관계기업투자손익",
       "지분법적용투자손익", "관계기업및공동기업투자손익"],
      {p: "optional" for p in PROFILES},
      note="IAS 1.82(c). XBRL 실측 45/70사", max_depth=1),
    C("operating_income", "IS", "line_item",
      ["영업이익", "영업이익(손실)", "영업손익"],
      {p: "required" for p in PROFILES},
      note="한국 관행상 전 업종 표시. 실측 영업이익 47사 + 영업이익(손실) 27사", max_depth=0),
    C("pretax_income", "IS", "line_item",
      ["법인세비용차감전순이익", "법인세비용차감전순이익(손실)", "법인세차감전순이익"],
      {p: "optional" for p in PROFILES}, note="실측 34+25+10사", max_depth=0),
    C("tax_expense", "IS", "line_item",
      ["법인세비용", "법인세비용(수익)"],
      {p: "optional" for p in PROFILES}, note="IAS 1.82(d). 실측 45+29사", max_depth=0),
    C("net_income", "IS", "line_item",
      ["당기순이익", "당기순이익(손실)", "연결당기순이익", "연결당기순이익(손실)",
       "당기순손익", "연결당기순손익", "계속영업당기순이익", "계속영업당기순손익",
       "당기순손실"],
      {p: "required" for p in PROFILES},
      note="IAS 1.82(f) 'profit or loss'. 실측 당기순이익 42사 · 당기순이익(손실) 34사 · "
           "연결당기순이익 6사 · 당기순손익 3사 · 연결당기순이익(손실) 2사 · 계속영업당기순이익 2사. "
           "'손익' 표기와 '연결' 접두를 빠뜨려 5개사(삼성SDI·NC·한화솔루션 등)를 놓쳤었다",
      max_depth=0),
    C("eps_basic", "IS", "line_item",
      ["기본주당이익", "기본주당순이익", "주당이익", "주당순이익"],
      {p: "optional" for p in PROFILES}, note="IAS 33. 실측 27+8+52사"),
    C("oci", "IS", "line_item",
      ["기타포괄손익"],
      {p: "optional" for p in PROFILES}, note="IAS 1.82A. 실측 57사", max_depth=0),
    C("total_ci", "IS", "line_item",
      ["총포괄손익", "총포괄이익"],
      {p: "optional" for p in PROFILES}, note="IAS 1.82(i). 실측 32사", max_depth=0),
]

# ----------------------------------------------------------------- 업종 고유
#: IAS 1.55가 허용한 업종별 대체 표시. XBRL `us-gaap-basi`/`us-gaap-ins`에 대응한다.
SECTOR_SPECIFIC = [
    # 은행지주 — 이자 스프레드 구조
    C("net_interest_income", "IS", "line_item",
      ["순이자손익", "순이자이익"],
      {"basi": "required", "sec": "optional", "ci": "n/a", "ins": "n/a"},
      note="실측 순이자손익 4사 + 순이자이익 3사", max_depth=0),
    C("net_fee_income", "IS", "line_item",
      ["순수수료손익", "순수수료이익"],
      {"basi": "required", "sec": "optional", "ci": "n/a", "ins": "n/a"},
      note="실측 5사", max_depth=0),
    C("net_insurance_income", "IS", "line_item",
      ["순보험손익"],
      {"basi": "optional", "ins": "optional", "ci": "n/a", "sec": "n/a"},
      note="실측 3사 — 보험 자회사를 둔 지주만", max_depth=0),
    C("bank_operating_revenue", "IS", "calculable",
      [],
      {"basi": "required", "ci": "n/a", "sec": "n/a", "ins": "n/a"},
      derive=("sum_", "net_interest_income", "net_fee_income", "net_insurance_income"),
      note="**계산값**(Compustat calculable). 은행지주엔 단일 top-line 행이 없다. "
           "순보험손익은 있는 회사만 더한다 — 신한지주는 있고 KB금융은 없다(실측)"),
    # 보험 — 보험료·손해액 구조
    C("insurance_revenue", "IS", "line_item",
      ["보험영업수익", "보험수익"],
      {"ins": "required", "basi": "optional", "ci": "n/a", "sec": "n/a"},
      note="실측 보험수익 6사 · 보험영업수익(삼성생명·삼성화재 본표)", max_depth=0),
    C("insurance_expense", "IS", "line_item",
      ["보험영업비용", "보험서비스비용"],
      {"ins": "required", "basi": "optional", "ci": "n/a", "sec": "n/a"},
      note="실측 보험서비스비용 6사", max_depth=0),
    C("insurance_result", "IS", "line_item",
      ["보험손익", "보험서비스결과"],
      {"ins": "required", "basi": "optional", "ci": "n/a", "sec": "n/a"},
      derive=("subtract", "insurance_revenue", "insurance_expense"),
      note="보고되면 line_item, 없으면 수익-비용. 실측 보험서비스결과 2사", max_depth=0),
    C("investment_result", "IS", "line_item",
      ["투자손익"],
      {"ins": "optional", "basi": "optional", "ci": "n/a", "sec": "n/a"},
      note="보험사 손익의 두 축 중 하나(보험손익 + 투자손익)", max_depth=0),
    # ★★ 2026-08-26 추가 — **표가 아니라 XBRL 코드로 집는다**(§6-36).
    #   `insurance_contract` **정본표는 2026-08-23에 실측으로 기각됐다** — 그 표는 행 라벨이
    #   전부 공백이라 `rows_of`가 7행 → 1행으로 접는다. 즉 **표 시그니처로는 못 집는다.**
    #   그런데 원문 셀에는 IFRS 17 표준 태그가 그대로 붙어 있다(전수):
    #       ifrs-full_ContractualServiceMargin   삼성생명 2,140회 · 삼성화재 616회
    #       dart_SurrenderValueReserve(ToBeAdded) 삼성화재 각 4회
    #   §6-36이 적어 둔 그대로다 — *"원문 XML은 셀마다 ACODE를 달고 있어 **열 이름 매칭과
    #   무관하게** 값을 집을 수 있다."* 라벨이 비어 있어도 코드는 살아 있다.
    # ★★★ **`csm`(보험계약마진)은 등록하려다 실측으로 기각했다**(2026-08-26).
    #   태그는 있다 — `ifrs-full_ContractualServiceMargin`이 삼성생명 2,140회·삼성화재 616회.
    #   그런데 **총계가 없다.** 그 코드가 붙은 셀을 ACONTEXT별로 전수로 세니 전부
    #   포트폴리오별 구성요소다(CFY2025: 4,298,393 · 6,043,267 · 7,950,196 · 789,359 …).
    #   더해서 총계를 만들면 그건 **우리가 지어낸 값**이고, 어느 층을 더해야 하는지
    #   결정할 근거가 없다(중복 합산 위험 — `_capex_total`이 HMM에서 겪은 것과 같다).
    #   ★ 정답셋도 어긋난다 — 검색-19 정답 `13,232,616`의 실제 태그는
    #     **`ifrs-full_InsuranceContractsLiabilityAsset`(보험계약부채)**이지 CSM이 아니다.
    #     정답 쪽 축을 먼저 확정해야 한다.
    #   되살리려면: (1) CSM 총계 행의 판정 근거를 원문에서 찾고 (2) 검색-19 정답의
    #   축(CSM인가 보험계약부채인가)을 도메인 검수로 확정한 뒤에 하라.
    C("surrender_reserve", "BS", "line_item",
      ["해약환급금준비금"],
      {"ins": "required", "basi": "n/a", "ci": "n/a", "sec": "n/a"},
      note="보험업감독규정상 이익잉여금 내 법정준비금. DART 확장 태그 "
           "`dart_SurrenderValueReserve`(잔액)·`…ToBeAdded`(적립·환입 예정액)", max_depth=0),
]

ALL = BALANCE_SHEET + INCOME_STATEMENT + SECTOR_SPECIFIC
BY_ID = {c.id: c for c in ALL}

#: ★ 프로파일 **추론은 폐기했다**. 근거: IFRS 개념체계 '충실한 표현' —
#: *"free from error means there are no errors or omissions ... and the **process** used to
#: produce the reported information has been selected and applied with no errors."*
#: 업종을 추측해 개념을 강제하면 그 추측이 곧 산출 과정의 오류가 된다.
#: 실제로 반복해서 틀렸다 — 셀트리온(제조) → sec · KB금융(은행) → ins · 삼성생명 → None.
#:
#: 대신 **기업이 표시한 개념을 그대로 찾는다**(as-reported 우선).
#: `applies`는 이제 "찾아야 하는가"가 아니라 **"없을 때 어떻게 설명하는가"** 에만 쓴다.
PROFILE_ANCHORS = ()

#: 개념 그룹 — 비교 질의(섹터 순위 등)에서 "같은 역할을 하는 개념"을 묶을 때만 쓴다.
#: 보고 질의에서는 쓰지 않는다(원문 라벨 그대로 답한다).
TOP_LINE_GROUP = ("revenue", "bank_operating_revenue", "insurance_revenue")


def concepts_for(profile=None, statement=None):
    """개념 목록. **profile로 거르지 않는다**(추론을 폐기했으므로).

    전부 시도하고 찾은 것만 값이 된다. 못 찾은 개념은 `explain_absent()`가
    표준 근거와 함께 설명한다.
    """
    return [c for c in ALL if not statement or c.statement == statement]


def observed_profile(found_ids):
    """**발견된 개념으로부터** 사후에 붙이는 라벨. 추론이 아니라 관찰이다.

    추출 결과를 사람이 읽기 쉽게 하는 용도이고, 추출 자체에는 쓰지 않는다.
    """
    f = set(found_ids)
    tags = []
    if {"net_interest_income", "net_fee_income"} & f:
        tags.append("이자·수수료 구조")
    if {"insurance_revenue", "insurance_result"} & f:
        tags.append("보험 구조")
    if {"revenue", "cost_of_sales"} <= f:
        tags.append("매출·원가 구조")
    elif "revenue" in f:
        tags.append("수익 구조")
    return " + ".join(tags) if tags else "구조 미상"


def explain_absent(concept_id, found_ids):
    """개념이 없을 때의 설명 — 대체 항목이 발견됐으면 그것을 가리킨다.

    업종을 단정하지 않는다. **관찰된 것으로만** 말한다.
    """
    c = BY_ID.get(concept_id)
    if c is None:
        return f"'{concept_id}'는 등록되지 않은 개념입니다."
    f = set(found_ids)
    alt = [x for x in TOP_LINE_GROUP if x != concept_id and x in f]
    label = c.labels[0] if c.labels else c.id
    if concept_id == "revenue" and alt:
        names = {"bank_operating_revenue": "순이자손익·순수수료손익",
                 "insurance_revenue": "보험영업수익"}
        got = " / ".join(names.get(x, x) for x in alt)
        return (f"이 회사의 손익계산서에는 '{label}' 항목이 없습니다. 대신 {got}을(를) "
                f"표시합니다(K-IFRS 1001 문단 55 — 목적적합하면 항목을 달리 표시할 수 있습니다).")
    return f"공시된 재무제표에서 '{label}' 항목을 찾지 못했습니다."


def status_of(concept_id, profile, found, unit_known):
    """개념의 결측 상태. Compustat이 코드를 나눠 쓰는 이유를 그대로 따른다."""
    c = BY_ID.get(concept_id)
    if c is None:
        return NOT_FOUND
    if c.applies.get(profile, "n/a") == "n/a":
        return NOT_APPLICABLE          # 은행의 매출액 — 결측이 아니라 개념 부재
    if not found:
        return NOT_FOUND
    if not unit_known:
        return UNIT_UNKNOWN
    return None                        # 정상


def explain(concept_id, profile):
    """사용자에게 보일 설명 — 왜 값이 없는지를 표준 근거와 함께."""
    c = BY_ID.get(concept_id)
    if c is None:
        return f"'{concept_id}'는 등록되지 않은 개념입니다."
    if c.applies.get(profile, "n/a") == "n/a":
        names = {"basi": "은행지주", "ins": "보험", "sec": "증권", "ci": "일반기업"}
        return (f"{names.get(profile, profile)}는 '{c.labels[0] if c.labels else c.id}' 항목을 "
                f"표시하지 않습니다(K-IFRS 1001 문단 55 — 업종에 맞는 항목으로 대체 표시).")
    return ""


if __name__ == "__main__":
    print(f"개념 {len(ALL)}개 · BS {len(BALANCE_SHEET)} · IS {len(INCOME_STATEMENT)} "
          f"· 업종고유 {len(SECTOR_SPECIFIC)}\n")
    for p in PROFILES:
        cs = concepts_for(p)
        req = [c.id for c in cs if c.applies[p] == "required"]
        calc = [c.id for c in cs if c.kind == "calculable"]
        print(f"[{p}] 적용 {len(cs)}개 · 필수 {len(req)}")
        print(f"      필수: {', '.join(req)}")
        if calc:
            print(f"      계산값: {', '.join(calc)}")
    print("\n결측 설명 예:")
    for p in ("basi", "ins", "ci"):
        print(f"  revenue/{p}: {explain('revenue', p) or '(정상 적용)'}")
    print(f"  inventories/basi: {explain('inventories', 'basi')}")
