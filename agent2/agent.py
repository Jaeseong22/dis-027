"""진입점 — `answer(question, question_id) -> 계약 5필드`.

PAO 루프(`agent2.loop`)에 연결돼 있다.

빈 질의는 루프를 태우지 않는다(LLM 호출 낭비). 그 밖의 모든 실패는
루프 안에서 정지 이유 코드로 흡수돼 계약 5필드로 나온다 — 예외가 API 경계를 넘지 않는다.

think_trace 첫 줄에 경로·정지 이유를 남긴다. 채점자와 우리 둘 다 이 한 줄로 판별한다.
"""
import os as _os

from agent2 import contract, loop

_EMPTY = "질문이 비어 있습니다."

#: ★ **응답 데드라인** — 주최가 타임아웃을 300초로 명시했다(2026-08-11 Q&A).
#:
#: 종전에 `loop.Budget`이 `max_seconds=None`(무제한)이었고 그 근거가
#: *"주최 명세에 응답시간 제한이 없다(전수 확인: `타임아웃`·`timeout` 0회)"* 였다.
#: **그 근거가 뒤집혔다.**
#:
#: 240초로 잡는 이유 — 주최가 끊는 것보다 **우리가 먼저 마무리해 부분 답을 내는 편이
#: 낫다.** 300초에 서버가 끊으면 재시도 2회를 하고, 그래도 늦으면 그 문항은 0점이다.
#: `budget_time` 정지 코드는 이미 있어서 그때까지 모은 근거로 답을 만든다.
#:
#: 실측(step9 2회 n=510)은 여유가 크다 — 중앙값 17.4초 · p99 69.6초 · 최대 110.5초.
#: 데드라인은 정상 경로가 아니라 **최악 경로**를 막는 장치다: LLM 호출 타임아웃 60초 ×
#: `max_llm_calls` 24 + 429 재시도 대기(최대 60초)가 겹치면 산술적으로 300초를 넘는다.
#:
#: **평가 하네스는 `loop.run`을 직접 부르므로 이 값의 영향을 받지 않는다** —
#: 측정은 상한 없이 그대로 재고, 제출 서버 경로에만 건다.
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
