"""검색 — BM25 + 섹션 스코프.

## 기준은 전부 표준·논문에서 왔다 

  · **BM25 파라미터**  Lucene 기본값 `k1=1.2` · `b=0.75`. 임의로 정하지 않는다.
    *"BM25 remains highly resilient to overfitting, requires low compute, and is
    infrastructurally efficient — reasons for its prevalence in first-stage retrieval"*
  · **벡터 검색 불필요**  AAAI 2026(Amazon): *"over 90% of the performance metrics ...
    without using a standing vector database"*. 1단 검색은 어휘 기반으로 충분하다.
  · **한국어 토큰화**  BM25 벤치마크에서 **okt(형태소) 최고 · space(공백) 최저**,
    *"character n-grams can enhance BM25 performance"*.
    → **어절 + 문자 bigram**. 형태소 분석기는 의존성·분석오류 위험으로 미채택.
  · **섹션 스코프**  FinGEAR(arXiv 2509.12042) — Item 정렬 계층 인덱싱으로 F1 0.30→0.68.
    "먼저 어느 섹션을 볼지 정하고 그 안에서 탐색".
  · **표는 HTML로**  TabVerse(arXiv 2606.09578) — 텍스트 파이프라인에서 HTML이 가장 안전.

## 검색 단위

문서를 통째로 넣지 않고 **섹션 텍스트**와 **표**를 각각 단위로 둔다.
표는 `Table` 객체를 그대로 들고 있어서 검색 후 **결정론 행 조회**로 넘어갈 수 있다
(TabVerse: 행 검색은 LLM이 12% 미만이므로 코드가 한다).
"""
import math
import re
from collections import Counter, namedtuple
from functools import lru_cache

from agent2.data import parse as P
from agent2.data import store

#: Lucene 기본값. 근거는 모듈 docstring. 평가 세트가 생기기 전엔 튜닝하지 않는다.
K1 = 1.2
B = 0.75

#: 검색 단위. `table`이 있으면 표 단위이고, 값 조회는 `Table.pick()`으로 이어진다.
Unit = namedtuple("Unit", "doc_id corp section_path kind text table")
Hit = namedtuple("Hit", "score unit matched")

_WORD = re.compile(r"[가-힣]+|[A-Za-z]+|\d+(?:[.,]\d+)*")


#: 사용자 어휘 → **코퍼스가 실제로 쓰는 어휘**. 질의에만 더한다(색인은 안 건드린다).
#:
#: 왜 필요한가(전수 실측): 70개사 사업보고서 섹션 제목에 `공장`·`사업장`·`시설투자`는
#: **0회**다. 표준 제목은 `3. 원재료 및 생산설비`(68/70)이고, 사업장·시설투자는 본문에만 있다.
#: 그래서 "공장 위치"·"설비의 신설 매입 계획" 같은 실무 표현이 통째로 빗나갔다.
#: 리랭킹으로는 못 고친다 — 순위 문제가 아니라 **어휘가 안 겹치는** 문제다.
#:
#: 각 쌍은 코퍼스에 그 표현이 실제로 존재하는지 확인하고 넣었다. 추측으로 넣지 않는다.
_SYN = {
    "공장": ("사업장", "생산설비"),
    "설비": ("생산설비", "시설투자"),
    "신설": ("시설투자", "증설"),
    "매입": ("시설투자",),
    "직원수": ("직원", "현황"),
    "종업원": ("직원",),
    "급여": ("급여액", "연간급여"),
    "연봉": ("급여액",),
    "발행주식수": ("발행할", "주식의", "총수"),
    "주식수": ("주식의", "총수"),
    "자회사": ("종속기업", "종속회사"),
    "대주주": ("최대주주",),
    "가동": ("가동률",),
    # ★ 수주 잔고를 코퍼스는 **회사마다 다른 말로** 적는다(70사 사업보고서 2025 전수):
    #     수주잔고 30사 · 수주총액 30사 · 계약잔액 26사 · 수주잔액 3사 · 기납품액 23사
    #   `계약잔액`만 가진 회사가 **12사**(한전기술·효성중공업·대우건설·기아·NAVER…),
    #   `수주총액`만 가진 회사가 5사다. 즉 "수주잔고"로만 물으면 그 회사들은 통째로 빗나간다.
    #   키는 `수주` 하나면 된다 — `수주잔고`가 든 질의도 이 키에 걸린다(중복 확장 방지).
    "수주": ("수주총액", "계약잔액"),

    # ── 금융·보험 지표 (2026-08-19 · 도메인 검수자 용어 정의 + 70사 전수 확인) ──
    #
    # 도메인 검수자: **NPL = 부실채권 = 고정이하여신** ·
    #         **대손충당금적립률 = 대손충당금 적립비율 = 대손충당금 커버리지 비율**
    #
    # 전수(70사 사업보고서 2025)가 그 정의를 뒷받침하고, **왜 빗나갔는지도 보여준다**:
    #     NPL 6사 · 고정이하여신 5사 · 고정이하여신비율 5사  ← 같은 5사를 가리키는데 표기가 갈린다
    #     ★ **메리츠금융지주는 `고정이하여신`은 쓰고 `NPL`은 안 쓴다** —
    #       질의가 "NPL비율"이면 그 회사가 통째로 빗나갔다(r33 오답의 실제 원인).
    #     대손충당금 적립률 5사 · 적립비율 6사 · **커버리지 1사(KB금융뿐)**
    #     CSM 6사 vs 보험계약마진 14사 · K-ICS 7사 vs 지급여력비율 8사
    #
    # 질의에만 더한다(색인·관측은 안 건드린다) — §6-14 위험이 없는 자리다.
    "NPL": ("고정이하여신", "고정이하여신비율", "부실채권"),
    "부실채권": ("고정이하여신", "고정이하여신비율"),
    "커버리지": ("대손충당금적립률", "대손충당금 적립비율"),
    "대손충당금": ("대손충당금적립률", "고정이하여신"),
    "CSM": ("보험계약마진",),
    "보험계약마진": ("CSM",),
    "K-ICS": ("지급여력비율", "신지급여력"),
    "지급여력": ("K-ICS", "지급여력비율"),
}


def expand(query):
    """질의에 코퍼스 어휘를 덧붙인다. 원 질의는 그대로 두고 **더하기만** 한다."""
    q = query or ""
    extra = [w for k, vs in _SYN.items() if k in q for w in vs if w not in q]
    return (q + " " + " ".join(extra)) if extra else q


def tokenize(text, bigram=True):
    """어절 + 한글 문자 bigram.

    공백 토큰화만 쓰면 BM25 벤치마크에서 최저였다. 한국어는 조사가 붙어 어절이
    그대로는 잘 안 맞기 때문이다. 문자 n-gram이 그 형태 변형을 흡수한다.
    """
    toks = []
    for w in _WORD.findall((text or "").lower()):
        toks.append(w)
        if bigram and len(w) > 1 and "가" <= w[0] <= "힣":
            toks.extend(w[i:i + 2] for i in range(len(w) - 1))
    return toks


class BM25:
    """BM25 Okapi. stdlib만 쓴다(의존성 0 원칙)."""

    def __init__(self, docs_tokens):
        self.n = len(docs_tokens)
        self.tf = [Counter(t) for t in docs_tokens]
        self.len = [len(t) for t in docs_tokens]
        self.avg = (sum(self.len) / self.n) if self.n else 0.0
        df = Counter()
        for t in docs_tokens:
            df.update(set(t))
        # Lucene/Robertson IDF. +1 로 음수 방지.
        self.idf = {w: math.log(1 + (self.n - c + 0.5) / (c + 0.5)) for w, c in df.items()}

    def score(self, i, q_tokens):
        tf, dl = self.tf[i], self.len[i]
        s = 0.0
        matched = []
        for w in q_tokens:
            f = tf.get(w)
            if not f:
                continue
            idf = self.idf.get(w, 0.0)
            denom = f + K1 * (1 - B + B * (dl / self.avg if self.avg else 1))
            s += idf * (f * (K1 + 1)) / denom
            matched.append(w)
        return s, matched

    def top(self, q_tokens, k=8, allow=None):
        """상위 k개. `allow(i)`로 **랭킹 전에** 후보를 거른다.

        랭킹 뒤에 거르면 안 된다(실측 버그): 한 문서에 표가 1,400개인데 섹션은 100개라,
        상위 후보를 표가 전부 차지해 `kind="section"` 검색이 **0건**을 냈다.
        '직원 등의 현황'·'주식의 총수'가 그렇게 통째로 사라졌다.
        """
        out = []
        for i in range(self.n):
            if allow is not None and not allow(i):
                continue
            s, m = self.score(i, q_tokens)
            if s > 0:
                out.append((s, i, m))
        out.sort(key=lambda x: -x[0])
        return out[:k]


# ----------------------------------------------------------------- 단위 만들기
def units_of(row, max_chars=4000):
    """문서 1건 → 검색 단위 목록(섹션 텍스트 + 표).

    섹션은 3단 트리의 잎에서 자른다(HiChunk: L1→L3에서 개선, 그 이상은 무변화).

    ★ **섹션 경로를 본문 앞에 붙여 색인한다**(FinGEAR — Item 정렬 계층 인덱싱으로
    F1 0.30→0.68). 원래 표에만 경로를 붙이고 섹션에는 안 붙였는데, 그 비대칭 때문에
    잎 섹션의 제목이 상위 제목을 잃어 검색이 통째로 빗나갔다(실측):

        '직원 등의 현황' → 섹션 검색 결과 **0건** (정답은 `VIII. 임원 및 직원 등에
        관한 사항 > 1. 임원 및 직원 등의 현황`인데, 그 노드에 자식이 있어 잎으로
        내려가면 제목이 `가. 임원 현황` 같은 것만 남는다)
        '주식의 총수'   → 섹션 검색 결과 0건
    """
    out = []
    for doc in P.parse_doc(row):
        for node in doc.section_titles():
            if node.children:
                continue                      # 잎 섹션만(상위는 하위에 포함된다)
            text = P.clean(doc.text[node.start:node.end or len(doc.text)])
            if len(text) < 30:
                continue
            # ★ 긴 잎 섹션은 **창으로 쪼개 전부 색인한다.** 앞부분만 자르면 뒤 내용이
            #   검색에서 통째로 사라진다(실측): `II. 사업의 내용 > 3. 원재료 및 생산설비`가
            #   133,635자인데 4,000자만 색인해서 그 안의 `주요 사업장 현황`·`시설투자 현황`을
            #   영영 못 찾았다 — 1-13·2-7이 그래서 실패했다.
            #   경로는 창마다 앞에 붙인다(FinGEAR — 계층 인덱싱).
            head = " ".join(node.path) + " "
            for off in range(0, len(text), max_chars):
                chunk = text[off:off + max_chars]
                if off and len(chunk) < 30:
                    break
                out.append(Unit(row["doc_id"], row["corp_name"], node.path,
                                "section", head + chunk, None))
        for t in doc.tables:
            head = " ".join(t.section_path) + " " + t.text_head(3)
            body = " ".join(" ".join(r) for r in t.grid[:40])
            out.append(Unit(row["doc_id"], row["corp_name"], t.section_path,
                            "table", (head + " " + body)[:max_chars], t))
    return out


@lru_cache(maxsize=32)
def _index_for(corp, doc_group, subtype, year):
    rows = store.docs(corp=corp, doc_group=doc_group,
                      doc_subtype=subtype, base_year=year)
    units = []
    for r in rows:
        units.extend(units_of(r))
    body = BM25([tokenize(u.text) for u in units])
    # 섹션 경로만 따로 색인한다 — 본문에 묻히는 제목 신호를 살린다(BM25F의 필드 개념).
    path = BM25([tokenize(" ".join(u.section_path)) for u in units])
    return tuple(units), body, path


#: RRF 상수(원논문 Cormack et al., SIGIR 2009 기본값 60).
#:
#: ★ **측정 결과 리랭킹은 기본 비활성이다.** 무엇을 시도해서 안 됐는지 남긴다:
#:
#:     BM25 단일(기준선)        Recall@1 16/22 · MRR 0.770   ← 최고
#:     RRF body+cov            16/22 · 0.765   (차이 없음)
#:     RRF body+path           11/22 · 0.598   (크게 악화)
#:     RRF body+path+cov       12/22 · 0.621
#:     k는 10·20·60·120 전부 동일 — k 문제가 아니다.
#:
#: 왜 경로 랭커가 해로운가: 긴 섹션을 창으로 쪼개 색인하는데 **한 섹션의 모든 창이
#: 같은 경로**라, 경로 BM25는 창을 구분하지 못하고 답이 없는 창까지 똑같이 밀어올린다.
#: 문헌(arXiv 2604.01733)이 리랭킹을 최대 효과 요소로 꼽은 것은 기준선이 약할 때다
#: (Number Match 41%). 우리 기준선은 이미 MRR 0.770이라 남은 실패의 성격이 다르다 —
#: 순위 문제가 아니라 **어휘 불일치**다('공장 위치' vs 원문 '사업장 현황').
RRF_K = 60
#: 융합 전 각 랭커에서 가져올 후보 수.
RRF_POOL = 40


def _rrf(rankings, k=None):
    """Reciprocal Rank Fusion — 점수가 아니라 **순위**를 합친다.

    RRF를 쓰는 이유(우리 상황에 맞는 성질):
      · 랭커마다 점수 스케일이 다른데(BM25 점수 vs 어절 커버리지 비율) **정규화가 필요 없다.**
      · 가중치를 정할 필요가 없다 — 우리가 임의로 정할 값이 하나도 안 생긴다.
        (근거 원칙: 기준을 스스로 만들지 않는다)
      · 고정 RRF가 적응형 라우팅보다 낫다는 실측이 있다(arXiv 2606.21553, +1.8 EM).
    """
    k = RRF_K if k is None else k
    score = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking, 1):
            score[idx] = score.get(idx, 0.0) + 1.0 / (k + rank)
    return sorted(score, key=lambda i: -score[i])


def _coverage_rank(units, cand, q_tokens):
    """질의 어절이 본문에 몇 개나 들어 있는가로 매긴 순위.

    BM25는 긴 창에서 희귀어 하나만 맞아도 높은 점수가 나온다. 커버리지는
    "질의 단어가 골고루 있는" 단위를 올려 **답이 실제로 든 창**을 고르게 돕는다.
    """
    words = {w for w in q_tokens if len(w) >= 2}
    if not words:
        return []
    scored = [(sum(1 for w in words if w in units[i].text) / len(words), i)
              for i in cand]
    return [i for s, i in sorted(scored, key=lambda x: -x[0]) if s > 0]


def search(query, corp=None, doc_group="periodic", subtype="annual",
           year=None, section=None, kind=None, k=8, rerank=False, synonyms=True):
    """질의 → 상위 k개 단위.

    `section`을 주면 **그 섹션 안에서만** 찾는다(FinGEAR식 2단 탐색).
    `kind="table"`이면 표만 — 수치 질의는 표에서 답이 나온다.
    `rerank`는 기본 False(측정 결과 BM25 단일이 최고). "cov"·"path"로 켜면 RRF 융합 —
    A/B 측정용으로 남겨 둔다.
    """
    if corp:
        c = store.resolve_corp(corp)
        if c is None:
            return []
        corp = c["corp_name"]
        year = year or store.latest_fiscal_year(corp)
    units, bm, bm_path = _index_for(corp, doc_group, subtype, year)
    q = tokenize(expand(query) if synonyms else query)

    def allow(i):
        u = units[i]
        if kind and u.kind != kind:
            return False
        if section and not any(P.norm_label(section) in P.norm_label(p)
                               for p in u.section_path):
            return False
        return True

    body_hits = bm.top(q, k=RRF_POOL, allow=allow)
    if not rerank:
        return [Hit(s, units[i], tuple(m)) for s, i, m in body_hits[:k]]

    matched = {i: m for s, i, m in body_hits}
    scores = {i: s for s, i, m in body_hits}
    r_body = [i for _s, i, _m in body_hits]
    lists = [r_body]
    if "path" in rerank:
        lists.append([i for _s, i, _m in bm_path.top(q, k=RRF_POOL, allow=allow)])
    if "cov" in rerank:
        lists.append(_coverage_rank(units, r_body, q))
    order = _rrf([r for r in lists if r])
    return [Hit(scores.get(i, 0.0), units[i], tuple(matched.get(i, ())))
            for i in order[:k]]


def sections(corp, doc_group="periodic", subtype="annual", year=None, level=1):
    """그 회사 문서의 섹션 목록 — 어느 섹션을 볼지 먼저 정하는 데 쓴다(FinGEAR 1단계)."""
    c = store.resolve_corp(corp)
    if c is None:
        return []
    year = year or store.latest_fiscal_year(c["corp_name"])
    rows = store.docs(corp=c["corp_name"], doc_group=doc_group,
                      doc_subtype=subtype, base_year=year)
    out, seen = [], set()
    for r in rows:
        for doc in P.parse_doc(r):
            for n in doc.section_titles(level=level):
                t = P.clean(n.title)
                if t and t not in seen:
                    seen.add(t)
                    out.append(t)
    return out


def render(hits, max_rows=12):
    """LLM 주입용 컨텍스트. 표는 **HTML**로 낸다(TabVerse 권고)."""
    parts = []
    for h in hits:
        u = h.unit
        path = " > ".join(u.section_path[-2:])
        parts.append(f"[{u.corp} · {path} · {u.kind} · score {h.score:.1f}]")
        if u.table is not None:
            parts.append(u.table.to_html(max_rows=max_rows))
        else:
            parts.append(u.text[:1200])
    return "\n".join(parts)


if __name__ == "__main__":
    for q, corp, kind in (("재고자산 장부금액", "삼성전자", "table"),
                          ("주요 제품 및 서비스", "삼성전자", None),
                          ("가동률", "현대자동차", None)):
        hits = search(q, corp=corp, kind=kind, k=3)
        print(f"\n=== {corp} · {q!r} · {len(hits)}건 ===")
        for h in hits:
            print(f"  {h.score:6.2f} [{h.unit.kind:7}] {' > '.join(h.unit.section_path[-2:])[:56]}")
            print(f"          매칭 {list(h.matched)[:6]}")
