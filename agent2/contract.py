"""평가 API 계약 — 5필드 단일 진실.

왜 별도 모듈인가: 계약을 **선언만** 하면 경로마다 어긋난다 — 실제로 그 사고를 겪었다
(경로별 5/6필드 비일관 · HTML 오류 페이지 · 커넥션 끊김). 그래서 `shape()`를 단일 관문으로
두고 `validate()`로 위반을 검출한다. 서버·테스트가 전부 이 모듈만 본다.

계약(주최 배포 PDF):
  GET /answer?question_id=&question=
  → {question_id, question, retrieved_context, think_trace, answer}

내부 결과 dict엔 진단 필드(`_stop`·`_calls` 등)가 더 있으나 **API 경계에서는 5필드만** 낸다.
근거는 `answer` 본문과 `retrieved_context`에 이미 들어 있다.
"""

CONTRACT = ("question_id", "question", "retrieved_context", "think_trace", "answer")


def shape(result: dict, question: str = "", question_id=None) -> dict:
    """임의의 내부 결과 dict → 계약 5필드. 누락은 빈 문자열, 값은 전부 str로 강제.

    질의·id는 인자를 우선한다(핸들러가 받은 원본이 진실이고, 내부에서 정규화된 값이 아니다).
    """
    r = result or {}
    out = {k: r.get(k, "") for k in CONTRACT}
    out["question"] = question if question is not None else ""
    out["question_id"] = "" if question_id is None else str(question_id)
    for k in CONTRACT:
        if out[k] is None:
            out[k] = ""
        elif not isinstance(out[k], str):
            out[k] = str(out[k])
    return out


def validate(payload: dict) -> list:
    """계약 위반 목록을 문자열로 반환(빈 리스트 = 통과). 테스트·런타임 가드 공용."""
    errs = []
    if not isinstance(payload, dict):
        return ["payload가 dict가 아님: %s" % type(payload).__name__]
    missing = [k for k in CONTRACT if k not in payload]
    if missing:
        errs.append("누락 필드: %s" % ", ".join(missing))
    extra = [k for k in payload if k not in CONTRACT]
    if extra:
        errs.append("계약 외 필드 노출: %s" % ", ".join(sorted(extra)))
    for k in CONTRACT:
        if k in payload and not isinstance(payload[k], str):
            errs.append("%s 타입이 str이 아님: %s" % (k, type(payload[k]).__name__))
    return errs
