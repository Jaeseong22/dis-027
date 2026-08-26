"""사전 등록 연산자 — **LLM에게 산술을 시키지 않는다.**"""
from agent2.tools.audit import Audit

_OPS = {}


def _op(name, arity=2):
    def deco(fn):
        fn.op_name, fn.arity = name, arity
        _OPS[name] = fn
        return fn
    return deco


def _ok(*vals):
    return all(v is not None for v in vals)

# ----------------------------------------------------------------- 기본 산술


@_op("add")


def add(a, b):
    return a + b if _ok(a, b) else None


@_op("subtract")


def subtract(a, b):
    return a - b if _ok(a, b) else None


@_op("multiply")


def multiply(a, b):
    return a * b if _ok(a, b) else None


@_op("divide")


def divide(a, b):
    return a / b if _ok(a, b) and b != 0 else None


@_op("sum_", 1)


def sum_(values):
    """합계. **하나라도 None이면 None** — 빠진 값을 0으로 세면 합이 조용히 작아진다."""
    if values is None or not list(values) or any(v is None for v in values):
        return None
    return sum(values)

# ----------------------------------------------------------------- 금융 파생


@_op("growth")


def growth(current, previous):
    """증감률(%) = (당기 − 전기) / |전기| × 100."""
    if not _ok(current, previous) or previous == 0:
        return None
    return (current - previous) / abs(previous) * 100.0


@_op("ratio")


def ratio(part, whole):
    """비중(%) = 부분 / 전체 × 100."""
    return None if not _ok(part, whole) or whole == 0 else part / whole * 100.0


@_op("margin")


def margin(profit, revenue):
    """이익률(%) = 이익 / 매출 × 100. OPM·GPM·순이익률 공통."""
    return ratio(profit, revenue)


@_op("cagr")


def cagr(last, first, years):
    """연평균성장률(%). 부호가 다르거나 0이면 정의되지 않으므로 None."""
    if not _ok(last, first, years) or years <= 0 or first <= 0 or last <= 0:
        return None
    return ((last / first) ** (1.0 / years) - 1.0) * 100.0


@_op("scale", 2)


def scale(value, factor):
    """단위 환산. 배율은 파서가 판정한 것만 넘어온다(추측 금지)."""
    return value * factor if _ok(value, factor) else None

# ----------------------------------------------------------------- 실행기


def run(op, *args, audit=None):
    """등록된 연산자만 실행한다. 미등록 연산은 예외 — 임의 수식을 못 만들게 한다."""
    fn = _OPS.get(op)
    if fn is None:
        raise ValueError(f"미등록 연산자: {op} · 사용 가능: {sorted(_OPS)}")
    result = fn(*args)
    if audit is not None:
        audit.compute(op, args, result)
    return result


def ops():
    return sorted(_OPS)


def verify(name, computed, expected, tol=0.01, audit=None):
    """불변식 검증 — 상대오차 `tol` 이내인지. 검증 결과도 감사에 남긴다."""
    if not _ok(computed, expected) or expected == 0:
        ok = None
    else:
        ok = abs(computed - expected) / abs(expected) <= tol
    if audit is not None:
        audit.compute(f"verify:{name}", (computed, expected), ok)
    return ok

if __name__ == "__main__":
    a = Audit("compute", "스모크")
    print("증감률", run("growth", 3336059, 3008709, audit=a))
    print("적자→흑자", run("growth", 100, -200, audit=a))
    print("OPM", run("margin", 436011, 3336059, audit=a))
    print("None 전파", run("add", None, 5, audit=a))
    print("0 나눗셈", run("divide", 5, 0, audit=a))
    print("불변식", verify("자산=부채+자본", 130621773 + 436320337, 566942110, audit=a))
    print()
    print(a.render())
