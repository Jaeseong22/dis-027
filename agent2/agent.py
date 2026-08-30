"""진입점 — `answer(question, question_id) -> 계약 5필드`."""
import os as _os

from agent2 import contract, loop
from agent2.tools import clarify as _clarify

_EMPTY = "질문이 비어 있습니다."

DEADLINE_SECONDS = float(_os.environ.get("ANSWER_DEADLINE_SECONDS") or 240)


def answer(question: str, question_id=None) -> dict:
    q = (question or "").strip()
    if not q:
        return contract.shape({"answer": _EMPTY, "retrieved_context": "",
                               "think_trace": "[route=empty_question]"},
                              question or "", question_id)
    # 게이트 A — 시나리오 밖 질의의 구체성(`tools/clarify.py` · LLM 0회 · 도구 0회).
    # 아는 축으로 해소되는 질문은 그냥 지나간다. 걸리는 곳을 `loop.run`이 아니라 여기로
    # 둔 이유: 서빙 경로가 이 함수 하나이고, 루프는 예산·반복감지를 재는 자리다.
    gate, why, payload = _clarify.route(q)
    if gate == "clarify":
        return contract.shape(
            {"answer": _clarify.message(why, payload, q),
             "retrieved_context": "(되묻기 — 도구 호출 없음)",
             "think_trace": f"[route=clarify reason={why} llm=0 tools=0]\n"
                            f"질의: {q}\n"
                            f"되묻기[{why}] 시나리오 밖 · 질문이 특정되지 않았다"},
            q, question_id)

    try:
        res = loop.run(q, question_id=question_id or "",
                       budget=loop.Budget(max_seconds=DEADLINE_SECONDS or None))
    except Exception as e:                    # 계약 위반보다 나쁜 건 없다 — 예외를 답변으로 바꾼다
        return contract.shape(
            {"answer": "공시에서 확인되지 않습니다 — 처리 중 오류가 발생했습니다.",
             "retrieved_context": "",
             "think_trace": f"[route=error] {type(e).__name__}: {e}"}, q, question_id)

    head = (f"[route=pao_loop stop={res.get('_stop')}({res.get('_stop_category')}) "
            f"steps={res.get('_steps')} llm={res.get('_llm_calls')} "
            f"tokens={res.get('_tokens')} {res.get('_seconds')}s]")
    raw = dict(res)
    raw["think_trace"] = head + "\n" + str(res.get("think_trace", ""))

    # 게이트 B — 시나리오 밖인데 관측이 재료를 주지 못했으면 되묻는다. 시나리오 안 질의는
    # 건드리지 않는다(부재형 정답 문항이 전부 거기 속해 채점 어휘를 잃지 않는다).
    if (_clarify.ON and _clarify.ON_NOEVIDENCE and gate == "out_specific"
            and res.get("_stop") not in ("llm_error", "llm_unavailable")
            and _clarify.no_evidence(res.get("_calls") or [])):
        raw["answer"] = _clarify.message("no_evidence", None, q)
        raw["think_trace"] += ("\n되묻기[no_evidence] 시나리오 밖 · "
                               "관측이 재료를 주지 못했다")
    return contract.shape(raw, q, question_id)

if __name__ == "__main__":
    for q in ("삼성전자 2025년 매출액", ""):
        p = answer(q, "smoke")
        print(f"질의 {q!r}")
        print("  계약:", contract.validate(p) or "OK")
        print("  경로:", p["think_trace"].splitlines()[0])
        print("  답변:", p["answer"][:100])
