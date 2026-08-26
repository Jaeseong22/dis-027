"""진입점 — `answer(question, question_id) -> 계약 5필드`."""
import os as _os

from agent2 import contract, loop

_EMPTY = "질문이 비어 있습니다."

DEADLINE_SECONDS = float(_os.environ.get("ANSWER_DEADLINE_SECONDS") or 240)


def answer(question: str, question_id=None) -> dict:
    q = (question or "").strip()
    if not q:
        return contract.shape({"answer": _EMPTY, "retrieved_context": "",
                               "think_trace": "[route=empty_question]"},
                              question or "", question_id)
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
    return contract.shape(raw, q, question_id)

if __name__ == "__main__":
    for q in ("삼성전자 2025년 매출액", ""):
        p = answer(q, "smoke")
        print(f"질의 {q!r}")
        print("  계약:", contract.validate(p) or "OK")
        print("  경로:", p["think_trace"].splitlines()[0])
        print("  답변:", p["answer"][:100])
