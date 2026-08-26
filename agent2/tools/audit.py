"""감사 로그 — 무엇을 **채택했고 무엇을 왜 버렸는지** 사유 코드로 남긴다.

근거: 온라인 금융 QA에서 3대 문제를 `노이즈 민감 · 계산 취약 · 감사 불가`로 규정하고,
채택/거부 근거와 연산 논리를 구조화 로그로 남기는 방식이 실측으로 효과를 보였다
(arXiv 2605.31064 — 환각률 12.44% → 6.82%, -45.2% · 정확도 85.26% → 91.48%).

    Accepted evidence: 59.1 [Exact_Match], 98.0 [Exact_Match]
    Rejected evidence: 8.2 [Concept_Shift]
    Computation logic: divide(59.1, 98.0)  →  0.60306

우리는 여기서 한 발 더 간다 — 파서의 `Pick.reason`(어느 열을 왜 골랐는지)까지 같은 로그에
들어간다. 값 하나가 나오기까지의 **문서 → 표 → 행 → 열 → 연산**이 전부 추적된다.
"""
from collections import namedtuple

#: 거부 사유 코드 — 2605.31064의 분류를 우리 코퍼스에 맞게 확장
REASONS = {
    "Exact_Match":     "라벨이 정확히 일치",
    "Time_Mismatch":   "기간이 질의와 다름",
    "Entity_Mismatch": "기업이 질의와 다름",
    "Concept_Shift":   "비슷하지만 다른 개념(주석·부문·임원보수 등)",
    "Unit_Error":      "단위를 특정할 수 없거나 원화가 아님",
    "Scope_Mismatch":  "연결/별도 범위가 질의와 다름",
    "Weak_Provenance": "열 선택 근거가 약함(leftmost_fallback)",
    "Ambiguous_Row":   "같은 라벨이 여러 행에 있음",
    "No_Source":       "원문이 코퍼스에 없음(목록에만 존재)",
    "Superseded":      "더 최신 정정본이 있음",
}

Entry = namedtuple("Entry", "kind value reason detail source")


class Audit:
    """도구 한 번 호출의 감사 기록. 값과 함께 반환해 답변 단계까지 따라간다."""

    def __init__(self, tool="", query=""):
        self.tool, self.query = tool, query
        self.entries = []
        self.computation = []

    # ---------- 기록 ----------
    def accept(self, value, reason="Exact_Match", detail="", source=""):
        self._add("accepted", value, reason, detail, source)
        return value

    def reject(self, value, reason, detail="", source=""):
        self._add("rejected", value, reason, detail, source)
        return None

    def _add(self, kind, value, reason, detail, source):
        if reason not in REASONS:
            raise ValueError(f"미등록 사유 코드: {reason} — REASONS에 먼저 정의할 것")
        self.entries.append(Entry(kind, value, reason, detail, source))

    def compute(self, op, args, result):
        """연산 기록. **LLM이 아니라 코드가 계산했다**는 증거가 된다."""
        self.computation.append((op, tuple(args), result))
        return result

    def note_pick(self, pick, source=""):
        """파서 `Pick`을 감사 항목으로 옮긴다 — 근거가 약하면 그 사실이 남는다."""
        if pick is None:
            return None
        if pick.reason == "leftmost_fallback":
            self._add("accepted", pick.value, "Weak_Provenance",
                      f"열 {pick.col} · 기간 미상", source)
        elif pick.n_label_hits > 1:
            self._add("accepted", pick.value, "Ambiguous_Row",
                      f"같은 라벨 {pick.n_label_hits}행 중 첫 행", source)
        else:
            self._add("accepted", pick.value, "Exact_Match",
                      f"{pick.label} · 열 {pick.col} · {pick.reason}", source)
        return pick.value

    # ---------- 조회 ----------
    @property
    def accepted(self):
        return [e for e in self.entries if e.kind == "accepted"]

    @property
    def rejected(self):
        return [e for e in self.entries if e.kind == "rejected"]

    @property
    def weak(self):
        """근거가 약한 채택 — 답변 단계에서 고지 대상."""
        return [e for e in self.accepted
                if e.reason in ("Weak_Provenance", "Ambiguous_Row", "Unit_Error")]

    def render(self):
        """사람이 읽는 감사 로그. think_trace에 그대로 넣는다."""
        out = [f"[{self.tool}] {self.query}".rstrip()]
        for e in self.accepted:
            out.append(f"  채택 {e.value} [{e.reason}] {e.detail}"
                       + (f" ← {e.source}" if e.source else ""))
        for e in self.rejected:
            out.append(f"  거부 {e.value} [{e.reason}] {e.detail}"
                       + (f" ← {e.source}" if e.source else ""))
        for op, args, res in self.computation:
            out.append(f"  연산 {op}({', '.join(map(str, args))}) → {res}")
        return "\n".join(out)

    def to_dict(self):
        return {"tool": self.tool, "query": self.query,
                "accepted": [e._asdict() for e in self.accepted],
                "rejected": [e._asdict() for e in self.rejected],
                "computation": [{"op": o, "args": list(a), "result": r}
                                for o, a, r in self.computation]}

    def __repr__(self):
        return (f"<Audit {self.tool} 채택 {len(self.accepted)} 거부 {len(self.rejected)} "
                f"연산 {len(self.computation)} 약함 {len(self.weak)}>")
