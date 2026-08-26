"""원문 → 구조(섹션 트리 · 표 · 텍스트) 파서.

설계 근거(전부 원문 확인한 논문 또는 4,204건 전수 실측):

  ① 표는 **HTML로 직렬화**해서 LLM에 준다.
     TabVerse(arXiv 2606.09578): 같은 표를 HTML/Markdown/LaTeX/이미지로 정렬해 비교 —
     텍스트 파이프라인에서 HTML이 가장 안전하고, 구조화 텍스트가 렌더 이미지보다 낫다.

  ② **행 선택은 결정론 코드가 한다. LLM에 시키지 않는다.**
     같은 논문: 열 단위 작업은 강한 모델 전부 85% 초과인데
     **행 검색(row retrieval)은 오픈 모델 전부 12% 미만**이고 형식을 바꿔도 안 낫는다.
     우리 질의는 `재고자산 장부금액`처럼 행 지정이 핵심이라 이 축을 LLM에 넘기면 안 된다.

  ③ 섹션 트리는 **3단**까지.
     HiChunk(arXiv 2509.11552): L1→L3에서 Evidence Recall이 오르고 그 이상은 변화 없음.
     근거 밀집 문서에서만 계층 청킹이 이긴다(HiCBench 81.03 vs 고정크기 74.06) —
     우리는 한 표 안에 답이 몰려 있는 밀집 쪽이다.

  ④ 청크에 **섹션 경로를 메타로 붙인다**.
     FinGEAR(arXiv 2509.12042): 10-K의 Item 정렬 계층 인덱싱으로 F1 0.30→0.68(+127%).
     우리 대응물은 정기공시의 로마숫자 최상위 섹션(I~XII)이다.

전수 실측이 강제하는 것:

  ⑤ 셀 태그는 **TD·TE·TU 3종 전부**. 주력이 문서군마다 뒤집힌다 —
     holding은 TE 1,741 vs TD 181, periodic은 TD 22,650 vs TE 673.
  ⑥ 형식은 **3계열**. exchange 1,469건은 확장자가 .xml이지만 내용은 HTML(euc-kr)이고
     값이 `<span class="xforms_input">`에만 있다. DOCUMENT/SECTION/TE/TU가 0건이다.
  ⑦ 단위 caption의 **공백 변형 3종**을 흡수한다 —
     `(단위 : 백만원)` 157,095 · `(단위: 백만원)` 43,636 · `(단위:백만원)` 12,269.
     한 caption에 단위가 둘인 경우도 있다(`(단위 : 주, 원)` 1,099) → 표 단위 일괄 환산 금지.
  ⑧ 음수는 **괄호와 △ 둘 다**. periodic 전건이 괄호, 29%가 △를 함께 쓴다.

정규식으로 파싱하는 이유: 원문이 최대 32MB이고 DART XML은 미선언 엔티티를 포함해
ElementTree가 중간에 죽는다. 스트리밍 정규식이 관용적이고 빠르다.
"""
import html as _html
import os
import re
from collections import OrderedDict, namedtuple

#: 값 + **선택 근거**. 열을 조용히 고르지 않기 위해 provenance를 함께 돌려준다.
#:   reason: explicit_col | latest_period | only_column | leftmost_fallback | no_numeric_cell
#:   n_label_hits: 같은 라벨이 여러 행에 있으면 >1 (첫 행만 썼다는 신호)
Pick = namedtuple("Pick", "value col period label reason n_label_hits")

#: 기간 헤더 — `제57기` · `2025년`/`2025.12` · `당기/전기/전전기`
_PERIOD_GI = re.compile(r"제\s*(\d+)\s*기")
_PERIOD_YEAR = re.compile(r"((?:19|20)\d{2})\s*(?:년|\.|$|\s)")
_PERIOD_REL = re.compile(r"(전전기|전기|당기|당분기|전분기)")
_REL_ORDER = {"전전기": 0, "전기": 1, "전분기": 1, "당기": 2, "당분기": 2}

# ----------------------------------------------------------------- 정규식
_TAG = re.compile(r"<[^>]+>")
#: ★ `<TABLE\b` 는 **`<TABLE-GROUP` 도 매치한다**(`\b`가 `E`와 `-` 사이 경계다).
#:   그러면 매치가 감싸는 태그부터 시작해 `section_path`를 **TITLE 앞** 오프셋으로 계산한다.
#:   DART는 `<TABLE-GROUP>` **안에** 그 절의 `<TITLE>`을 넣는 경우가 있다:
#:       <TABLE-GROUP ACLASS="{XBRL}NT_S_D818005">
#:       <TITLE>27. 특수관계자와의 거래</TITLE>
#:       <TABLE …>
#:   그래서 특수관계자 표가 **직전 절(26. 부문별 보고)** 로 라벨됐다.
#:   전수 실측(2026-08-23): 표 958,429개 중 `<TABLE-GROUP`부터 시작한 것 69,827개,
#:   그중 **섹션 경로가 틀렸던 것 4,777개**(전부 periodic). 표 개수는 안 바뀐다.
_TABLE = re.compile(r"<TABLE(?![A-Za-z0-9_-]).*?</TABLE>", re.I | re.S)
_TR = re.compile(r"<TR\b.*?</TR>", re.I | re.S)
_CELL = re.compile(r"<(TD|TE|TU|TH)\b([^>]*)>(.*?)</\1>", re.I | re.S)
_ATTR = re.compile(r"(\w+)\s*=\s*\"([^\"]*)\"|(\w+)\s*=\s*'([^']*)'|(\w+)\s*=\s*(\S+)", re.I)
_TITLE = re.compile(r"<TITLE\b[^>]*>(.*?)</TITLE>", re.I | re.S)
_THEAD = re.compile(r"<THEAD\b.*?</THEAD>", re.I | re.S)
_XFORMS = re.compile(r"<span\s+class=\"xforms_input\"[^>]*>(.*?)</span>", re.I | re.S)

#: 단위 caption — 콜론 앞뒤 공백 변형 3종을 \s* 로 흡수(⑦)
_UNIT_CAPTION = re.compile(r"\(\s*단위\s*[::]\s*([^)]{1,60})\)")
#: 라벨에 붙은 단위 — `배당받을 주식(단위: 주)` · `주당배당금(원)` 처럼 **행마다 단위가 다르다**
_UNIT_INLINE = re.compile(r"\(\s*(?:단위\s*[::]\s*)?([^()]{1,20}?)\s*\)\s*$")

#: 원화 배율. 최장 일치로 골라야 '백만원'이 '원'에 먹히지 않는다.
_KRW = OrderedDict((("조원", 1e12), ("억원", 1e8), ("백만원", 1e6),
                    ("천원", 1e3), ("원", 1.0)))
#: 원화가 아닌 통화 — 배율은 없지만 **통화는 안다**. None으로 뭉개면 이 정보를 잃는다.
_FX = ("USD", "JPY", "EUR", "CNY", "HKD", "GBP", "달러", "엔", "유로", "위안")
#: 금액이 아닌 단위
_NONMONEY = OrderedDict((("주", "shares"), ("%", "percent"), ("명", "count"),
                         ("건", "count"), ("개", "count"), ("년", "period"),
                         ("개월", "period"), ("배", "ratio"), ("포인트", "point")))

#: 최상위 섹션(로마숫자) — 정기공시 1,051건 전건이 이 형태
_L1 = re.compile(r"^\s*([IVXⅠ-Ⅻ]+)\s*[.．]\s*(.+)$")
#: 2단 — `1.` `2-1.` `제1부`
_L2 = re.compile(r"^\s*(제\s*\d+\s*부|\d+(?:-\d+)?)\s*[.．]?\s*(.+)$")
#: 3단 — `가.` `(1)` `①`
_L3 = re.compile(r"^\s*([가-힣]\.|\(\d+\)|[①-⑮])\s*(.+)$")

#: 라벨 정규화에서 떼는 것 — 항번호·주석기호·괄호단위·공백·가운뎃점
_LBL_STRIP = re.compile(r"^\s*(?:[IVXⅠ-Ⅻ]+[.．]|\d+(?:[-.]\d+)*[.．]?|[가-힣][.)]|\(\d+\)|[①-⑮])\s*")
#: 주석 표기 — `(주30)` · `(주8,28,31,42)` · `(주2,4,34)` 처럼 **번호가 여러 개**일 수 있다.
#: 실측: `당기순이익 (주8,28,31,42)` · `영업이익 (주21,22)` · `매출액 (주2,4,34)`.
#: 콤마를 안 받으면 `당기순이익(주8283142)`가 되어 개념 매칭이 통째로 실패한다.
_LBL_NOTE = re.compile(r"\(\s*(?:주\s*[\d,\s]*|\*+|단위[^)]*)\s*\)")
_LBL_WS = re.compile(r"[\s ㆍ·・,]+")


# ----------------------------------------------------------------- 텍스트
def unescape(s):
    """HTML 엔티티 해제 + NBSP 정리. DART XML은 &nbsp;·&amp; 를 섞어 쓴다."""
    return _html.unescape(s or "").replace(" ", " ")


def strip_tags(s):
    return _TAG.sub(" ", s or "")


def clean(s):
    """태그 제거 → 엔티티 해제 → 공백 정규화."""
    return re.sub(r"[ \t ]+", " ", unescape(strip_tags(s))).strip()


#: 표 뒤 각주의 시작 표지. 공시가 실제로 쓰는 형태만 넣었다(70개사 전수 훑기).
_NOTE_HEAD = re.compile(r"^(※|주\s*\)|주\s*\d+\s*\)|\*)")
#: 각주로 인정할 최대 길이. 넘어가면 다음 문단을 끌고 오는 것이라 자른다.
_NOTE_MAX = 400


def footnote_after(text, end):
    """`</TABLE>` **바로 뒤**의 `※`·`주)` 각주를 돌려준다. 없으면 빈 문자열.

    ## 왜 필요한가 (2026-08-13 실측)

    정본표는 표를 정확히 옮기지만 **표가 불완전하다는 사실을 표 자신이 말하지 않는다.**
    현대자동차 1-2가 10회 실행 전부 △였다 — 정답이 `차량 78% · 금융 16% · 기타 6%`인데
    정본표에는 차량·기타만 있다. 그게 오추출이 아니라 공시 표가 `(제조서비스업)` 절에
    있어서인데, 공시는 그 사실을 표 바로 아래 각주로 적어 뒀다:

        ※ 금융부문 주요 재무정보는 '(금융업)사업의 개요'에 별도 기재함.

    우리는 이 문장을 버리고 있었다. 각주는 **회사가 직접 쓴 표의 한계·산출식**이라
    지어낸 규칙이 아니다. 삼성전자 가동률 산출식·원재료 매입비중 정의도 전부 여기 있다.

    전수 실측(2026-08-16 재측정): FY2025 사업보고서 **최신본만** 표 110,551개 중
    **3,583개(3.2%)**가 각주를 갖는다(정정 포함 시 138,453개 중 5,099개=3.7%).
    ★ 종전 주석은 `표 28,000개 · 1,763개(6.3%)`였다. 파싱 커버리지가 커지며
      분모가 4배가 됐고 비율은 내려갔다 — **모집단을 함께 적지 않으면 비율이 거짓말을 한다.**
    """
    seg = text[end:end + 2000]
    cut = seg.find("<TABLE")
    if cut == -1:
        cut = len(seg)
    for tag in ("<TITLE", "</SECTION"):
        i = seg.find(tag)
        if 0 <= i < cut:
            cut = i
    note = clean(seg[:cut])
    if not _NOTE_HEAD.match(note):
        return ""
    # 각주 뒤에 이어지는 **본문 문단**까지 끌고 오면 안 된다(현대차 실측: 각주 4줄 뒤에
    # "연결실체의 제조서비스 사업은 …" 문단이 붙어 왔다). 표지(`※`)로 나눈 뒤 각 조각을
    # **첫 문장까지만** 남긴다 — 공시 각주는 한 문장으로 끝난다(`…임.` `…함.` `…입니다.`).
    parts = [p for p in note.split("※") if p.strip()]
    if len(parts) > 1 or note.startswith("※"):
        return " ".join("※ " + _one_sentence(p) for p in parts)[:_NOTE_MAX]
    return _one_sentence(note)[:_NOTE_MAX]


#: 문장 끝 마침표 — **한글 바로 뒤**의 것만 인정한다.
#: 그냥 `.`를 찾으면 각주가 다른 목차를 인용할 때 거기서 잘린다(삼성전기 실측:
#: `…매출액은 'Ⅱ. 사업의 내용 4. 매출 및 수주상황'을 참조…` → `Ⅱ.`·`4.`에서 절단).
#: 숫자·로마숫자 뒤 마침표는 **항번호**이고, 한국어 문장은 한글로 끝난다(…음. …다. …함.).
_SENT_END = re.compile(r"[가-힣]\s*\.")


def _one_sentence(s):
    """각주 한 조각 → 첫 문장까지. 줄바꿈이 먼저 오면 거기서 끊는다."""
    s = s.strip()
    nl = s.find("\n")
    if nl != -1:
        s = s[:nl].strip()
    m = _SENT_END.search(s)
    return (s[:m.end()] if m else s).strip()


def indent_level(label):
    """라벨 앞 전각 공백 수 = 계층 깊이."""
    n = 0
    for ch in str(label or ""):
        if ch == "\u3000":
            n += 1
        else:
            break
    return n


def norm_label(s):
    """계정 라벨 정규화 — 항번호·주석·단위괄호·공백·가운뎃점 제거 후 소문자화.

    `Ⅱ. 매출원가(주3)` · `매출 원가` · `매출원가` 가 같은 키가 되도록.
    """
    s = clean(s).lstrip("\u3000")
    s = _LBL_NOTE.sub("", s)
    s = _LBL_STRIP.sub("", s)
    return _LBL_WS.sub("", s).lower()


def num(s):
    """표 셀 → 수치. 실패하면 None(억지로 0을 만들지 않는다).

    처리: 천단위 콤마 · 괄호 음수 · △/▲ 음수 · 퍼센트 기호 · 빈칸/`-`
    """
    if s is None:
        return None
    t = clean(str(s))
    if not t or t in {"-", "–", "—", "―", "N/A", "n/a", "해당사항없음"}:
        return None
    neg = False
    if t.startswith(("△", "▲", "-", "-", "−")):
        neg, t = True, t[1:]
    if t.startswith("(") and t.endswith(")"):
        neg, t = True, t[1:-1]
    t = t.replace(",", "").replace("%", "").replace("원", "").strip()
    if not re.fullmatch(r"\d+(\.\d+)?", t):
        return None
    v = float(t)
    return -v if neg else v


class Unit:
    """단위 판정 결과.

    **`None` 하나로 뭉개지 않는다.** 세 경우는 전부 다르고, 호출부의 대응도 다르다.

        (단위 : 백만원)         확정   scale=1e6, currency='KRW'      → 환산한다
        (단위: USD)            확정   scale=None, currency='USD'     → 환산 불가, **통화는 안다**
        (단위 : 주, 원)         모호   ambiguous=True                 → **행마다 라벨에서 다시 본다**

    실측 근거: 삼성전자 FY2025 `19. 연결이익잉여금` 표는 캡션이 `백만원`인데
    `배당받을 주식(단위: 주)` 행만 주식 수다. 표 배율로 일괄 환산하면 그 행이 틀린다.
    """

    __slots__ = ("tokens", "scale", "currency", "kind", "raw", "ambiguous")

    def __init__(self, tokens=(), scale=None, currency=None, kind="unknown",
                 raw="", ambiguous=False):
        self.tokens, self.scale, self.currency = tuple(tokens), scale, currency
        self.kind, self.raw, self.ambiguous = kind, raw, ambiguous

    @property
    def known(self):
        """단위를 **특정**했는가(환산 가능 여부와 별개).

        `kind == "unknown"`은 특정 실패다 — `(액면가 기준)` 같은 괄호 한정어를
        단위로 착각하면 안 된다.
        """
        return bool(self.tokens) and not self.ambiguous and self.kind != "unknown"

    @property
    def convertible(self):
        """원화로 환산할 수 있는가."""
        return self.scale is not None

    def __bool__(self):
        return bool(self.tokens)

    def __repr__(self):
        if self.ambiguous:
            return f"<Unit 모호 {self.tokens} raw={self.raw!r}>"
        return (f"<Unit {self.kind} {self.tokens}"
                f"{' x%g' % self.scale if self.scale else ''}"
                f"{' ' + self.currency if self.currency else ''}>")


def _classify(tok):
    """단위 토큰 1개 → (scale, currency, kind)."""
    t = tok.strip()
    if not t:
        return None, None, "unknown"
    hits = [w for w in _KRW if w in t]
    hits = [w for w in hits if not any(w != o and w in o for o in hits)]   # 최장 일치
    if len(hits) == 1 and not any(f in t for f in _FX):
        return _KRW[hits[0]], "KRW", "money"
    for f in _FX:
        if f in t:
            return None, ("USD" if f in ("USD", "달러") else f), "money"
    for w, kind in _NONMONEY.items():
        if t == w or t.endswith(w):
            return None, None, kind
    return None, None, "unknown"


def parse_unit(raw):
    """단위 문자열 → Unit. 토큰이 둘 이상이면 ambiguous."""
    raw = (raw or "").strip()
    if not raw:
        return Unit()
    toks = [t.strip() for t in re.split(r"[,/·ㆍ・]", raw) if t.strip()]
    if len(toks) != 1:
        return Unit(toks, raw=raw, ambiguous=True, kind="mixed")
    scale, cur, kind = _classify(toks[0])
    return Unit(toks, scale, cur, kind, raw)


def caption_unit(text):
    """`(단위 : …)` 캡션에서 Unit을 뽑는다. 없으면 빈 Unit."""
    m = _UNIT_CAPTION.search(text or "")
    return parse_unit(m.group(1)) if m else Unit()


#: 주석 표기 — 번호가 여러 개일 수 있다(`(주8,28,31,42)`). `_LBL_NOTE`와 같은 규칙을 쓴다.
#: 한쪽만 고치면 라벨 정규화는 되는데 단위 판정이 실패한다(미래에셋증권 당기순이익 사고).
_NOTE_PAREN = re.compile(r"주\s*[\d,\s]*\d|\*+|주석.*|참고.*|계속.*")


def _label_paren(label):
    """라벨 꼬리 `(...)`의 내용. 주석 표기(`(주1)`·`(*)`)면 None."""
    m = _UNIT_INLINE.search(clean(label))
    if not m:
        return None
    inner = m.group(1).strip()
    return None if _NOTE_PAREN.fullmatch(inner) else inner


def label_unit(label):
    """라벨 꼬리의 `(…)`에서 Unit을 뽑는다 — `배당받을 주식(단위: 주)` · `주당배당금(원)`."""
    inner = _label_paren(label)
    if inner is None:
        return Unit()
    u = parse_unit(inner)
    return u if u.known or u.ambiguous else Unit()


#: **범위** 한정어 — 단위를 바꾸지 않는다. 표 캡션 단위를 그대로 상속해야 한다.
#: `자산총계(연결)`은 여전히 표의 금액 단위다. 이걸 막으면 정상 케이스가 환산 불가가 된다.
_SCOPE_PAREN = re.compile(
    r"연결|별도|개별|당기|전기|전전기|누적|보통주|우선주|종류주|제\s*\d+\s*기|"
    r"\d+\s*개월|\d+\s*분기|기말|기초|당분기|전분기")

#: **반대 부호 병기** — IFRS 항목명 자체가 양방향이다. 한정어가 아니라 **항목명의 일부**다.
#:   IAS 1.82(f)  "profit or loss"        → `당기순이익(손실)`
#:   IAS 12/1.82(d) "tax expense (income)" → `법인세비용(수익)`
#: 실측: 이 형태의 라벨이 IS에서 67종, `당기순이익(손실)` 34사 · `법인세비용(수익)` 29사 ·
#: `영업이익(손실)` 27사. 한정어로 오인하면 이 계정 전부가 단위 미상이 되어 값이 버려진다
#: (삼성생명 당기순이익이 실제로 그렇게 사라졌다).
_SIGN_PAREN = re.compile(r"^(손실|수익|이익|비용|차손|차익|환입|전입)$")


def _has_qualifier(label):
    """**단위 판정을 막아야 하는** 괄호 한정어가 붙어 있는가.

    `(액면가 기준)`처럼 그 행이 표 기본에서 벗어남을 시사하면 True(상속 금지).
    `(연결)`·`(별도)`·`(제57기)` 같은 범위 한정어는 False(상속 허용).
    """
    inner = _label_paren(label)
    if inner is None or parse_unit(inner).known:
        return False
    if _SIGN_PAREN.match(inner.strip()):
        return False              # IFRS 항목명의 반대 부호 병기 — 단위를 바꾸지 않는다
    return not _SCOPE_PAREN.search(inner)


def unit_scale(text):
    """구버전 호환 — (배율, 원문). 새 코드는 `caption_unit()`을 쓴다."""
    u = caption_unit(text)
    return u.scale, u.raw or None


# ----------------------------------------------------------------- 표
class Table:
    """COLSPAN/ROWSPAN이 전개된 2D 그리드 + 다단 헤더 경로.

    `grid[r][c]`는 항상 값이 있다(병합 셀은 덮인 칸에 같은 값을 복제).
    이렇게 해야 행·열 인덱싱이 실제 표와 일치한다.
    """

    def __init__(self, grid, header_rows=0, caption="", section_path=(), raw="",
                 footnote=""):
        self.grid = grid
        self.header_rows = header_rows
        self.caption = caption
        self.section_path = tuple(section_path)
        self.raw = raw
        #: 표 **바로 뒤**의 `※`·`주)` 각주. 회사가 직접 쓴 표의 한계·산출식이다.
        #: 전수 실측(2026-08-16): FY2025 최신본 표 110,551개 중 **3,583개(3.2%)**가 갖고 있다.
        self.footnote = footnote
        self.unit = caption_unit(caption or self.text_head())
        # 구버전 호환 속성
        self.unit_scale, self.unit_raw = self.unit.scale, (self.unit.raw or None)

    def unit_for(self, label):
        """**행 단위** 판정 — 라벨에 붙은 단위가 표 캡션을 이긴다.

        세 갈래로 갈린다:
          ① 라벨에 단위가 있음        → 그것을 쓴다 (`배당받을 주식(단위: 주)` → 주)
          ② 라벨에 **단위 아닌 괄호 한정어**가 있음 → **빈 Unit**.
             `보통주 주당 배당률 (액면가 기준)`은 백만원이 아니다. 한정어가 붙었다는 것은
             그 행이 표 기본에서 벗어난다는 신호이므로 상속하지 않는다.
          ③ 괄호가 없음              → 표 캡션 상속(캡션이 모호하면 빈 Unit)
        """
        u = label_unit(label)
        if u.known:
            return u
        if _has_qualifier(label):
            return Unit()
        return Unit() if self.unit.ambiguous else self.unit

    def value_krw(self, *labels, col=None, exact=True):
        """원 단위로 환산한 값. 환산할 수 없으면 None.

        `value()`는 표에 적힌 **원값**을, 이 함수는 **환산값**을 준다.
        USD 표·모호 단위 표에서 None이 나오는 것은 정직한 실패다(0을 만들지 않는다).
        """
        hits = self.find_rows(*labels, exact=exact)
        if not hits:
            return None
        label, _ = hits[0]
        u = self.unit_for(label)
        if not u.convertible:
            return None
        v = self.value(*labels, col=col, exact=exact)
        return None if v is None else v * u.scale

    # ---------- 기본 ----------
    @property
    def nrows(self):
        return len(self.grid)

    @property
    def ncols(self):
        return max((len(r) for r in self.grid), default=0)

    def text_head(self, n=2):
        return " ".join(" ".join(r) for r in self.grid[:n])

    def cell(self, r, c):
        try:
            return self.grid[r][c]
        except IndexError:
            return ""

    # ---------- 헤더 ----------
    def header_path(self, col):
        """열 `col`의 다단 헤더 경로. 연속 중복은 접는다.

        `['2025년', '연결', '금액']` 처럼 상위→하위 순서.
        다단 헤더를 한 문자열로 뭉개면 `2025 연결` vs `2025 별도`를 구분 못 한다.
        """
        out = []
        for r in range(self.header_rows):
            v = self.cell(r, col).strip()
            if v and (not out or out[-1] != v):
                out.append(v)
        return out

    def headers(self):
        return [self.header_path(c) for c in range(self.ncols)]

    # ---------- 기간 열 ----------
    def period_map(self):
        """{열: 기간키} — 헤더에서 `제57기`·`2025년`·`당기`를 읽는다.

        **왜 필요한가(실측)**: 기간 열이 2개 이상인 표에서 열 순서가
        내림차순 426 · 오름차순 57 · 뒤섞임 445 다. "가장 왼쪽이 당기"는 46%에서만 맞다.
        차입금 만기표는 `2026 2027 2028 2029 2030`으로 오름차순이다.
        """
        out = {}
        for c in range(self.ncols):
            path = " ".join(self.header_path(c))
            m = _PERIOD_GI.search(path)
            if m:
                out[c] = ("기", int(m.group(1)))
                continue
            m = _PERIOD_YEAR.search(path)
            if m:
                out[c] = ("년", int(m.group(1)))
                continue
            m = _PERIOD_REL.search(path)
            if m:
                out[c] = ("상대", _REL_ORDER[m.group(1)])
        return out

    def latest_period_col(self):
        """가장 최근 기간의 열. 판정 불가면 None."""
        pm = self.period_map()
        if not pm:
            return None
        kinds = {k for k, _ in pm.values()}
        if len(kinds) != 1:
            return None                      # 기수와 연도가 섞이면 비교하지 않는다
        return max(pm, key=lambda c: pm[c][1])

    # ---------- 행 계층 ----------
    def row_levels(self, col=None):
        """{행인덱스: 깊이} — **원문 들여쓰기**(전각 공백)로 계층을 읽는다.

        근거는 `research/parsing.md`의 '행 계층' 항목:
          · DART XML이 계층을 U+3000으로 인코딩한다(실측). 발행사가 명시한 것이다.
          · OHD(arXiv 2602.01969) — 계층 탐지는 **공간 제약**이 기본이고, 단일 제약으로는
            거짓 부모-자식이 생긴다.

        **산술로 계층을 정하지 않는다.** 시도했다가 틀렸다 —
        삼성전자 `매출액 = 매출원가 + 매출총이익`은 산술은 맞지만 계층은 반대다.
        IAS 1.85A의 '소계 = 하위 합'은 `verify_subtotals()`에서 **검증용으로만** 쓴다.
        """
        return {r: indent_level(l) for l, r in self.rows()}

    def verify_subtotals(self, col=None, tol=0.005):
        """IAS 1.85A 검증 — 각 소계가 직속 하위 항목의 합과 맞는가.

        [(라벨, 소계값, 하위합, 일치여부)]. 계층 판정이 아니라 **품질 진단**이다.
        불일치는 곧 오추출이 아닐 수 있다(중간 생략·반올림).
        """
        if col is None:
            col = self.latest_period_col()
        rows = self.rows()
        lv = self.row_levels()
        out = []
        for i, (label, r) in enumerate(rows):
            v = num(self.cell(r, col)) if col is not None else self.value(label)
            if v is None:
                continue
            depth = lv.get(r, 0)
            acc, found = 0.0, False
            for label2, r2 in rows[i + 1:]:
                d2 = lv.get(r2, 0)
                if d2 <= depth:
                    break
                if d2 == depth + 1:
                    cv = num(self.cell(r2, col)) if col is not None else self.value(label2)
                    if cv is None:
                        continue
                    acc += cv
                    found = True
            if found:
                ok = abs(acc - v) <= tol * max(1.0, abs(v))
                out.append((clean(label).lstrip("\u3000"), v, acc, ok))
        return out

    def top_rows(self, col=None):
        """[(라벨, 행인덱스)] — **최상위 층(소계 층)만**. 개념 매핑은 이 층에서 한다.

        하위 세부를 개념으로 잡으면 안 된다 — KB금융에서 `보험수익`(하위)을 잡아
        은행지주를 보험사로 오판한 사고가 있었다. 소계는 `보험서비스결과`다.
        """
        lv = self.row_levels(col)
        if not lv:
            return self.rows()
        return [(l, r) for l, r in self.rows() if lv.get(r, 0) == 0]

    # ---------- 행 조회 (결정론 — ②) ----------
    def label_col(self):
        """라벨 열 = 데이터 행에서 비수치 비율이 가장 높은 왼쪽 열."""
        best, best_score = 0, -1.0
        for c in range(min(self.ncols, 3)):
            vals = [self.cell(r, c) for r in range(self.header_rows, self.nrows)]
            vals = [v for v in vals if v.strip()]
            if not vals:
                continue
            score = sum(1 for v in vals if num(v) is None) / len(vals)
            if score > best_score:
                best, best_score = c, score
        return best

    def rows(self):
        """[(라벨, 행인덱스)] — 헤더를 제외한 데이터 행."""
        lc = self.label_col()
        return [(self.cell(r, lc), r) for r in range(self.header_rows, self.nrows)]

    def find_rows(self, *labels, exact=True, top_only=False):
        """라벨로 행을 찾는다. 정규화 후 비교하며, `exact=False`면 부분일치.

        **부분일치는 기본값이 아니다** — `순익`이 `순익분기점`에 걸리는 식의
        오염이 한국어에서 흔하고, `\\b` 단어경계는 한글에 무용하기 때문.
        """
        keys = [norm_label(x) for x in labels if x]
        rows = self.top_rows() if top_only else self.rows()
        # ★ **라벨 배열은 우선순위다.** EdgarTools가 `개념 → 태그 배열`을 우선순위로 두는 것과
        #   같다. 표 등장 순서로 돌려주면 `자산`(구역 머리글)이 `자산총계`(총계)를 이긴다 —
        #   KB금융 연결재무상태표에서 실제로 그렇게 틀렸다(자산총계 797.9조 대신 머리글 행).
        buckets = {i: [] for i in range(len(keys))}
        extra = []
        for label, r in rows:
            k = norm_label(label)
            if not k:
                continue
            for i, q in enumerate(keys):
                if k == q:
                    buckets[i].append((label, r))
                    break
            else:
                if not exact and any(q and q in k for q in keys):
                    extra.append((label, r))
        out = [x for i in range(len(keys)) for x in buckets[i]]
        return out + extra

    def pick(self, *labels, col=None, exact=True, top_only=False):
        """라벨 → `Pick`(값 + **왜 그 셀을 골랐는지**). 못 찾으면 None.

        열을 조용히 고르지 않는다. 선택 근거가 `Pick.reason`에 남으므로
        호출부가 `leftmost_fallback`(근거 약함)을 거부할 수 있다.
        """
        hits = self.find_rows(*labels, exact=exact, top_only=top_only)
        if not hits:
            return None
        label, r = hits[0]
        nrows = len(hits)
        if col is not None:
            return Pick(num(self.cell(r, col)), col, self.period_map().get(col),
                        label, "explicit_col", nrows)

        # 값이 있는 행만 '같은 라벨 후보'로 센다. 값 없는 구역 머리글(`자산`)까지 세면
        # 정상 표에서도 모호 경고가 뜬다 — 실측상 69/70개사가 전부 그 오탐이었다.
        hits = [(l, rr) for l, rr in hits
                if any(num(self.cell(rr, c)) is not None
                       for c in range(self.label_col() + 1, self.ncols))] or hits
        label, r = hits[0]
        nrows = len(hits)
        numeric = [c for c in range(self.label_col() + 1, self.ncols)
                   if num(self.cell(r, c)) is not None]
        if not numeric:
            return Pick(None, None, None, label, "no_numeric_cell", nrows)

        lp = self.latest_period_col()
        if lp in numeric:
            return Pick(num(self.cell(r, lp)), lp, self.period_map().get(lp),
                        label, "latest_period", nrows)
        if len(numeric) == 1:
            c = numeric[0]
            return Pick(num(self.cell(r, c)), c, self.period_map().get(c),
                        label, "only_column", nrows)
        c = numeric[0]
        return Pick(num(self.cell(r, c)), c, self.period_map().get(c),
                    label, "leftmost_fallback", nrows)

    def value(self, *labels, col=None, exact=True):
        """라벨 행 × 열 → 수치. 근거가 필요하면 `pick()`을 쓴다."""
        p = self.pick(*labels, col=col, exact=exact)
        return p.value if p else None

    def value_at(self, *labels, period, exact=True):
        """기간을 지정해 값을 읽는다 — `period=2025` 또는 `period=57`(제57기)."""
        pm = self.period_map()
        cols = [c for c, (_, n) in pm.items() if n == period]
        if not cols:
            return None
        return self.value(*labels, col=cols[0], exact=exact)

    def series(self, *labels, exact=True):
        """{기간: 값} — 다년도 비교(OPM 3개년 등)에 쓴다."""
        hits = self.find_rows(*labels, exact=exact)
        if not hits:
            return {}
        _, r = hits[0]
        return {n: num(self.cell(r, c)) for c, (_, n) in sorted(self.period_map().items())}

    # ---------- 직렬화 (① HTML) ----------
    def to_html(self, max_rows=None):
        """LLM 주입용 HTML. TabVerse 권고에 따라 기본 형식은 HTML이다."""
        rows = self.grid if max_rows is None else self.grid[:max_rows]
        out = ["<table>"]
        if self.caption:
            out.append(f"  <caption>{_html.escape(self.caption)}</caption>")
        for i, row in enumerate(rows):
            tag = "th" if i < self.header_rows else "td"
            cells = "".join(f"<{tag}>{_html.escape(c)}</{tag}>" for c in row)
            out.append(f"  <tr>{cells}</tr>")
        if max_rows is not None and len(self.grid) > max_rows:
            out.append(f"  <!-- {len(self.grid) - max_rows}행 생략 -->")
        out.append("</table>")
        return "\n".join(out)

    def to_markdown(self, max_rows=None):
        """보조 형식. 사람이 읽는 로그·리포트용."""
        rows = self.grid if max_rows is None else self.grid[:max_rows]
        if not rows:
            return ""
        w = self.ncols
        def line(r):
            return "| " + " | ".join((r + [""] * w)[:w]) + " |"
        head = rows[:self.header_rows] or [rows[0]]
        body = rows[self.header_rows:] if self.header_rows else rows[1:]
        out = [line(h) for h in head] + ["|" + "---|" * w] + [line(b) for b in body]
        return "\n".join(out)

    def __repr__(self):
        return (f"<Table {self.nrows}x{self.ncols} hdr={self.header_rows} "
                f"unit={self.unit_raw!r} sec={'>'.join(self.section_path[-2:])}>")


# ----------------------------------------------------------------- 셀 전개
#: 들여쓰기 — DART XML은 계층을 **전각 공백(U+3000)** 으로 인코딩한다(실측).
#:   '순이자손익' / '　이자수익' / '　　기타포괄손익-…이자수익'
#: OHD(arXiv 2602.01969)가 말하는 **공간 제약**이 이것이다. 발행사가 명시한 계층이므로
#: 산술로 추측할 필요가 없다(산술만 쓰면 `매출액 = 매출원가 + 매출총이익` 같은 우연한
#: 항등식에 걸려 계층이 뒤집힌다 — 실제로 삼성전자에서 그렇게 틀렸다).
_INDENT_CHARS = "\u3000\u00a0 \t"


def _indent_of(body):
    """셀 본문 앞의 들여쓰기 깊이. 전각 공백 1개 = 1단."""
    txt = unescape(strip_tags(body or ""))
    n = 0
    for ch in txt:
        if ch == "\u3000":
            n += 1
        elif ch in " \t\u00a0\r\n":
            continue
        else:
            break
    return n


def _attrs(s):
    out = {}
    for m in _ATTR.finditer(s or ""):
        k = m.group(1) or m.group(3) or m.group(5)
        v = m.group(2) or m.group(4) or m.group(6) or ""
        out[k.upper()] = v
    return out


def _span(a, key):
    try:
        n = int(re.sub(r"\D", "", a.get(key, "1")) or 1)
    except ValueError:
        n = 1
    return max(1, min(n, 200))          # 비정상 스팬으로 메모리가 터지지 않게 상한


def expand_grid(table_html, with_origins=False):
    """`<TABLE>` 원문 → (그리드, 헤더행수) [, 원본마스크].

    COLSPAN/ROWSPAN을 전개해 병합 셀 값을 덮인 칸에 복제한다.
    TD·TE·TU·TH를 **모두** 셀로 취급한다(⑤).

    `with_origins=True`면 **원본 셀만 True**인 같은 모양의 마스크를 함께 준다.
    복제는 문헌 표준이라 유지하되(InstructTable 2604.02880 — "merged cells can be
    replicated across their ranges"), 복제하면 원본과 연장분 구분이 사라져 **행 트리**를
    잃는다. OHD(arXiv 2602.01969)는 그 손실 때문에 평면 직렬화가 계층 표에서
    Markdown −16.14%p·HTML −19.24%p로 진다고 보고하고, LLM 의미판단으로 트리를 만든다.
    우리는 XML이 이미 ROWSPAN을 갖고 있으므로 **LLM 없이 결정론으로** 같은 구조를 얻는다.
    (코퍼스 실측: FY2025 사업보고서 표 189,591개 중 14.4%가 세로병합 — 예외가 아니다.)
    """
    trs = _TR.findall(table_html)
    thead_rows = set()
    if _THEAD.search(table_html):
        head_html = _THEAD.search(table_html).group(0)
        thead_rows = {i for i, tr in enumerate(trs) if tr in head_html}

    grid, orig, occupied = [], [], {}
    for ri, tr in enumerate(trs):
        row, org, ci = [], [], 0
        for m in _CELL.finditer(tr):
            tag, attr, body = m.group(1).upper(), m.group(2), m.group(3)
            while (ri, ci) in occupied:
                row.append(occupied.pop((ri, ci)))
                org.append(False)                         # 위 행 ROWSPAN의 연장분
                ci += 1
            a = _attrs(attr)
            cs, rs = _span(a, "COLSPAN"), _span(a, "ROWSPAN")
            val = clean(body)
            if k_indent := _indent_of(body):
                val = "\u3000" * k_indent + val      # 들여쓰기를 라벨 앞에 보존
            for k in range(cs):
                row.append(val)
                org.append(k == 0)                        # 가로 연장분도 원본이 아니다
                for j in range(1, rs):
                    occupied[(ri + j, ci + k)] = val
            ci += cs
        while (ri, ci) in occupied:                       # 행 끝에 걸린 rowspan
            row.append(occupied.pop((ri, ci)))
            org.append(False)
            ci += 1
        grid.append(row)
        orig.append(org)

    keep = [i for i, r in enumerate(grid) if any(c.strip() for c in r)]
    grid = [grid[i] for i in keep]
    if not with_origins:
        return grid, _header_rows(grid, thead_rows, trs)
    # 마스크는 그리드와 **같은 필터**를 거쳐야 인덱스가 어긋나지 않는다
    orig = [(orig[i] + [False] * len(grid[j]))[:len(grid[j])]
            for j, i in enumerate(keep)]
    return grid, _header_rows(grid, thead_rows, trs), orig


def _header_rows(grid, thead_rows, trs):
    """헤더 행 수 판정 — THEAD 우선, 없으면 '수치가 없는 선두 행'."""
    if thead_rows:
        return max(thead_rows) + 1
    n = 0
    for r in grid[:4]:
        vals = [c for c in r[1:] if c.strip()]
        if vals and all(num(v) is None for v in vals):
            n += 1
        else:
            break
    return n or (1 if grid else 0)


# ----------------------------------------------------------------- 섹션
class Section:
    """섹션 노드. 3단까지만 유지한다(③)."""

    __slots__ = ("title", "level", "start", "end", "children", "parent")

    def __init__(self, title, level, start, parent=None):
        self.title, self.level, self.start = title, level, start
        self.end, self.children, self.parent = None, [], parent

    @property
    def path(self):
        out, n = [], self
        while n is not None and n.title:
            out.append(n.title)
            n = n.parent
        return tuple(reversed(out))

    def __repr__(self):
        return f"<S{self.level} {self.title[:28]!r}>"


def _level_of(title):
    """제목 문자열 → 계층(1~3). 어느 패턴에도 안 맞으면 3단으로 둔다."""
    t = title.strip()
    if _L1.match(t):
        return 1
    if _L2.match(t):
        return 2
    if _L3.match(t):
        return 3
    return 3


def build_sections(text):
    """`<TITLE>` 위치로 섹션 트리를 만든다. 루트는 title=''."""
    root = Section("", 0, 0)
    stack = [root]
    marks = [(m.start(), clean(m.group(1))) for m in _TITLE.finditer(text)]
    marks = [(p, t) for p, t in marks if t]
    for i, (pos, title) in enumerate(marks):
        lv = min(_level_of(title), 3)
        while len(stack) > 1 and stack[-1].level >= lv:
            stack[-1].end = pos
            stack.pop()
        node = Section(title, lv, pos, stack[-1])
        stack[-1].children.append(node)
        stack.append(node)
    end = len(text)
    for n in stack[1:]:
        n.end = end
    root.end = end
    return root


def _section_at(root, pos):
    """위치 `pos`를 담는 가장 깊은 섹션 노드."""
    node, cur = root, root
    while True:
        nxt = next((c for c in cur.children
                    if c.start <= pos and (c.end or 1 << 62) > pos), None)
        if nxt is None:
            return cur if cur is not root else node
        cur = nxt


# ----------------------------------------------------------------- 형식 판별
def detect_format(text):
    """DART XML · HTML(xforms) · PDF 텍스트 3계열 판별(⑥)."""
    head = (text or "")[:600].lstrip()
    if head.startswith("<?xml") or "<DOCUMENT" in head.upper():
        return "dart_xml"
    if "<html" in head.lower() or "xforms_input" in (text or "")[:4000]:
        return "html"
    return "pdf_text"


# ----------------------------------------------------------------- 문서
class Document:
    def __init__(self, text, fmt, sections, tables, meta=None):
        self.text, self.fmt = text, fmt
        self.sections, self.tables = sections, tables
        self.meta = meta or {}

    def section_titles(self, level=None):
        out = []
        def walk(n):
            for c in n.children:
                if level is None or c.level == level:
                    out.append(c)
                walk(c)
        walk(self.sections)
        return out

    def section_text(self, title, exact=False):
        """제목으로 섹션 본문을 잘라 온다. FinGEAR식 Item 스코프(④)."""
        key = norm_label(title)
        for n in self.section_titles():
            k = norm_label(n.title)
            if k == key or (not exact and key and key in k):
                return clean(self.text[n.start:n.end or len(self.text)])
        return ""

    def tables_in(self, title, exact=False):
        key = norm_label(title)
        out = []
        for t in self.tables:
            if any(key == norm_label(p) or (not exact and key and key in norm_label(p))
                   for p in t.section_path):
                out.append(t)
        return out

    def find_tables(self, *row_labels, exact=True):
        """지정한 행 라벨을 **모두** 가진 표. 단일 표 앵커링용."""
        out = []
        for t in self.tables:
            if all(t.find_rows(l, exact=exact) for l in row_labels):
                out.append(t)
        return out

    def __repr__(self):
        return (f"<Document {self.fmt} {len(self.text):,}자 "
                f"섹션 {len(self.section_titles())} 표 {len(self.tables)}>")


# ----------------------------------------------------------------- 파서
def parse_dart_xml(text):
    """DART XML → Document.

    ★ **1열 표는 데이터 표가 아니라 제목 블록이다.** DART는 재무제표 앞에 이런 걸 둔다:

        연결 포괄손익계산서
        제 57 기 2025.01.01 부터 2025.12.31 까지
        제 56 기 …
        (단위 : 백만원)

    실측: 재무제표 섹션의 1열 표 141건 중 **130건이 2열 이상 표 바로 앞**에 있다.
    ★ 이 규칙이 동작한 뒤 남는 1열 표는 **전 70사에서 1건**이다(2026-08-16 확인).
      141은 **병합 전** 수치다 — 두 숫자를 같은 것으로 읽지 말 것.
    이걸 데이터 표로 세면 `제52기2025.01.01부터…`가 행 라벨로 잡혀 개념 사전이 오염된다
    (실제로 손익계산서 '첫 행' 집계에 이 문자열이 9회 올라왔다).
    그래서 **제외하고, 다음 표의 제목·단위·기간 정보로 넘긴다.**
    """
    root = build_sections(text)
    tables = []
    pending = ""                      # 직전 1열 제목 블록의 텍스트
    for m in _TABLE.finditer(text):
        grid, hdr = expand_grid(m.group(0))
        if not grid:
            continue
        ncols = max(len(r) for r in grid)
        if ncols < 2:
            pending = " ".join(c for r in grid for c in r if c.strip())
            continue
        sec = _section_at(root, m.start())
        cap = _caption_near(text, m.start()) or ""
        if pending:
            cap = (cap + " " + pending).strip() if "단위" not in cap else cap
            path = sec.path + (pending[:80],)
        else:
            path = sec.path
        tables.append(Table(grid, hdr, cap, path, m.group(0),
                            footnote_after(text, m.end())))
        pending = ""
    return Document(text, "dart_xml", root, tables)


def parse_html(text):
    """거래소공시 — 값이 `<span class="xforms_input">`에만 있다(⑥).

    섹션 트리가 없으므로 표 그리드 + xforms 값 목록을 함께 제공한다.
    """
    root = build_sections(text)
    tables = []
    for m in _TABLE.finditer(text):
        grid, hdr = expand_grid(m.group(0))
        if grid:
            tables.append(Table(grid, hdr, "", _section_at(root, m.start()).path, m.group(0)))
    values = [clean(v) for v in _XFORMS.findall(text)]
    doc = Document(text, "html", root, tables, {"xforms_values": values})
    return doc


def pdf_cache_path(path):
    """PDF 표 캐시 파일 경로. `warmup`이 '무엇이 찬지'를 이 함수로 판단한다 —
    캐시 규칙이 두 군데로 갈라지지 않게 한 곳에서만 정한다.
    """
    import hashlib
    from agent2 import config
    # ★ 캐시는 **주최 제공 폴더 밖**에 쓴다. 문서 폴더에 파일을 만들면
    #   manifest의 `n_files`와 어긋나 무결성 검사가 깨진다(실제로 깨뜨렸다).
    cdir = os.path.join(config.AGENT_DIR, ".cache", "pdf_tables")
    # 방어: 캐시 경로가 코퍼스 안으로 들어가면 즉시 실패시킨다.
    # 실제로 한 번 오염시켰고 `corpus_audit`의 n_files 검사가 3건을 잡아냈다.
    if os.path.commonpath([os.path.abspath(cdir),
                           os.path.abspath(config.CORPUS_DIR)]) == os.path.abspath(config.CORPUS_DIR):
        raise RuntimeError(f"캐시가 코퍼스 안을 가리킨다: {cdir}")
    os.makedirs(cdir, exist_ok=True)
    key = hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:16]
    return os.path.join(cdir, key + ".json")


def pdf_tables(path, cache=True):
    """PDF에서 표를 추출한다 — **pdfplumber**(native PDF 표준 도구).

    ★ PDF를 반드시 써야 하는 이유(실측): 대체수집 3건은 전부 **최신본**이다.
        KB금융   PDF 2026-06-19 **2차 정정** vs XML 03-13 원본·03-24 1차 정정
        한화오션  PDF 2024-05-14 **정정본**   vs XML 05-13 원본
        한화에어로 PDF 2026-05-13 **유일본**
      '최신 정정본이 유효본'이므로 PDF를 버리면 **낡은 값을 답하게 된다.**

    직접 만든 공백 정렬 휴리스틱은 4MB 문서에서 1,881개(대부분 연혁 목록 오탐)를 냈다.
    조사 결과 native PDF의 정공법은 pdfplumber(문자 좌표 기반)이고,
    Docling/TableFormer(93.6%)는 transformer 의존이라 이 규모엔 과하다.

    추출이 비싸서(수백 페이지) 결과를 JSON으로 디스크 캐시한다.
    """
    import json as _json
    cpath = pdf_cache_path(path)
    if cache and os.path.exists(cpath) and os.path.getmtime(cpath) >= os.path.getmtime(path):
        with open(cpath, encoding="utf-8") as fh:
            return _json.load(fh)
    try:
        import pdfplumber
    except ImportError:
        return []
    out = []
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            found = page.find_tables()
            if not found:
                continue
            for tb in found:
                grid = [[(c or "").strip() for c in row] for row in (tb.extract() or [])]
                grid = [r for r in grid if any(r)]
                if len(grid) < 2:
                    continue
                # ★ 표 **바로 위** 텍스트를 제목으로 잡는다. PDF에는 XML 같은 섹션 트리가
                #   없어서 이게 없으면 `연결재무상태표`와 `재무상태표`(별도)를 구분할 수 없고
                #   단위도 못 읽는다. 실측: 제목 줄에 `<재무상태표> (단위:백만원)`이 함께 있다.
                try:
                    top = tb.bbox[1]
                    head = page.crop((0, max(0, top - 70), page.width, top)).extract_text() or ""
                except Exception:
                    head = ""
                out.append({"page": pno, "grid": grid,
                            "heading": " ".join(head.split())[-160:]})
    if cache:
        try:
            with open(cpath, "w", encoding="utf-8") as fh:
                _json.dump(out, fh, ensure_ascii=False)
        except OSError:
            pass
    return out


def _page_offsets(text):
    """pdftotext 텍스트의 **페이지 시작 문자 오프셋**. 1-based 페이지 → `out[page-1]`.

    pdftotext는 페이지 경계에 폼피드(`\\f`)를 남긴다. 실측(한화에어로스페이스
    분기보고서 2026.03): 폼피드 447개 ↔ pdfplumber 최대 페이지 447로 정확히 맞는다.
    """
    out, i = [0], 0
    while True:
        i = text.find("\f", i)
        if i < 0:
            return out
        out.append(i + 1)
        i += 1


def parse_pdf(path, text=None):
    """PDF 파일 → Document. 표는 pdfplumber, 본문 텍스트는 pdftotext.

    ★ 표의 섹션 경로는 **페이지 번호가 아니라 섹션 트리**로 붙인다.
      섹션은 pdftotext 텍스트의 문자 오프셋으로, 표는 pdfplumber의 페이지 번호로
      나와서 좌표계가 달랐다. 폼피드로 페이지→오프셋을 만들면 XML 경로와 같은
      `_section_at()`을 쓸 수 있다.
      그러지 않으면 경로가 `p452 >` 꼴이라 `V. 회계감사인의 감사의견 등`으로
      조회할 수 없다 — 내용은 다 있는데 **주소가 없는** 상태가 된다.
      실측: 한화에어로스페이스 표 873개가 873개 전부 섹션에 배정된다(미배정 0).
    """
    from agent2.data import source
    if text is None:
        text = source._read_pdf(path)
    doc = parse_pdf_text(text)
    offs = _page_offsets(text)
    tables = []
    for item in pdf_tables(path):
        g = item["grid"]
        width = max(len(r) for r in g)
        g = [r + [""] * (width - len(r)) for r in g]
        head = item.get("heading", "") or item.get("context", "")
        cap = next((c for r in g[:2] for c in r if "단위" in c), "") or head
        page = item["page"]
        # 페이지가 폼피드 범위를 벗어나면(추출 불일치) 종전대로 페이지 번호를 남긴다.
        if 1 <= page <= len(offs):
            path_ = _section_at(doc.sections, offs[page - 1]).path + (head,)
        else:
            path_ = (f"p{page}", head)
        tables.append(Table(g, _header_rows(g, set(), []), cap, path_, ""))
    doc.tables = tables
    doc.meta["note"] = f"pdfplumber 표 {len(tables)}개 · 본문은 pdftotext"
    return doc


def parse_pdf_text(text):
    """PDF 추출 텍스트 → 섹션 트리. **표는 `parse_pdf()`가 pdfplumber로 따로 붙인다.**

    표 안의 `1. …` 같은 번호 항목이 전부 섹션으로 잡히면(실측 4MB에서 2,877개) 트리가
    무의미해진다. 그래서 PDF에서는 **로마숫자 최상위와 `제N부`만** 섹션으로 인정한다.
    """
    root = Section("", 0, 0)
    stack = [root]
    pos = 0
    for line in text.splitlines(keepends=True):
        t = line.strip()
        if t and len(t) <= 60 and (_L1.match(t) or re.match(r"^제\s*\d+\s*부", t)):
            lv = min(_level_of(t), 3)
            while len(stack) > 1 and stack[-1].level >= lv:
                stack[-1].end = pos
                stack.pop()
            node = Section(t, lv, pos, stack[-1])
            stack[-1].children.append(node)
            stack.append(node)
        pos += len(line)
    for n in stack[1:]:
        n.end = len(text)
    root.end = len(text)
    return Document(text, "pdf_text", root, [], {"note": "표는 parse_pdf()가 붙인다"})


def _caption_near(text, pos, back=260):
    """표 바로 앞의 단위 caption을 찾는다. 없으면 빈 문자열."""
    seg = clean(text[max(0, pos - back):pos])
    m = _UNIT_CAPTION.search(seg)
    return m.group(0) if m else ""


def parse(text):
    """형식 자동 판별 후 파싱."""
    fmt = detect_format(text)
    if fmt == "dart_xml":
        return parse_dart_xml(text)
    if fmt == "html":
        return parse_html(text)
    return parse_pdf_text(text)


def parse_doc(row):
    """manifest 1행 → [Document]. 폴더 안 **모든 파일**을 각각 파싱한다."""
    from agent2.data import source
    out = []
    for fname, kind, path in source.files(row):
        d = parse_pdf(path) if kind == "pdf" else parse(
            source._read_text(path))
        if not d.text:
            continue
        d.meta.update({"file": fname, "kind": kind, "doc_id": row.get("doc_id")})
        out.append(d)
    return out


if __name__ == "__main__":
    from agent2.data import store
    r = store.docs(corp="삼성전자", doc_subtype="annual", base_year=2025)[0]
    docs = parse_doc(r)
    print(r["report_nm"], "→ 파일", len(docs), "개\n")
    for d in docs:
        print(" ", d, d.meta["file"])
    main = docs[0]
    print("\n최상위 섹션:", [s.title for s in main.section_titles(level=1)][:6])
    cands = main.find_tables("자산총계", "부채총계", "자본총계")
    print(f"\n자산·부채·자본이 한 표에 있는 표 {len(cands)}개")
    for t in cands[:2]:
        print("  ", t)
        for lbl in ("자산총계", "부채총계", "자본총계"):
            print(f"     {lbl:8} {t.value(lbl)}")
