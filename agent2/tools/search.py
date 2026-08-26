"""검색 — BM25 + 섹션 스코프."""
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
    "수주": ("수주총액", "계약잔액"),

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
    """어절 + 한글 문자 bigram."""
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
        """상위 k개. `allow(i)`로 **랭킹 전에** 후보를 거른다."""
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
    """문서 1건 → 검색 단위 목록(섹션 텍스트 + 표)."""
    out = []
    for doc in P.parse_doc(row):
        for node in doc.section_titles():
            if node.children:
                continue                      # 잎 섹션만(상위는 하위에 포함된다)
            text = P.clean(doc.text[node.start:node.end or len(doc.text)])
            if len(text) < 30:
                continue
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
RRF_K = 60
#: 융합 전 각 랭커에서 가져올 후보 수.
RRF_POOL = 40


def _rrf(rankings, k=None):
    """Reciprocal Rank Fusion — 점수가 아니라 **순위**를 합친다."""
    k = RRF_K if k is None else k
    score = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking, 1):
            score[idx] = score.get(idx, 0.0) + 1.0 / (k + rank)
    return sorted(score, key=lambda i: -score[i])


def _coverage_rank(units, cand, q_tokens):
    """질의 어절이 본문에 몇 개나 들어 있는가로 매긴 순위."""
    words = {w for w in q_tokens if len(w) >= 2}
    if not words:
        return []
    scored = [(sum(1 for w in words if w in units[i].text) / len(words), i)
              for i in cand]
    return [i for s, i in sorted(scored, key=lambda x: -x[0]) if s > 0]


def search(query, corp=None, doc_group="periodic", subtype="annual",
           year=None, section=None, kind=None, k=8, rerank=False, synonyms=True):
    """질의 → 상위 k개 단위."""
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
