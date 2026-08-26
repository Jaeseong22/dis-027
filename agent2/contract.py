"""평가 API 계약 — 5필드 단일 진실."""

CONTRACT = ("question_id", "question", "retrieved_context", "think_trace", "answer")


def shape(result: dict, question: str = "", question_id=None) -> dict:
    """임의의 내부 결과 dict → 계약 5필드. 누락은 빈 문자열, 값은 전부 str로 강제."""
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
