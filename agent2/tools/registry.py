"""도구 등록기 — 스키마와 구현을 한 곳에서 정의한다."""
import inspect
import re
from collections import OrderedDict

_REGISTRY = OrderedDict()
MAX_TOOLS = 12          # ①. 넘으면 등록 시점에 실패시킨다.
MAX_DESC = 200          # ②. 설명 길이 상한.

_PY_JSON = {str: "string", int: "integer", float: "number", bool: "boolean",
            list: "array", dict: "object"}

#: 도구 설명에 실을 지침의 길이 상한. 200자 이내·능동태.
GUIDE_MAX = 200


class Tool:
    __slots__ = ("name", "fn", "summary", "guide", "detail", "params", "returns",
                 "scenarios")

    def __init__(self, name, fn, scenarios=()):
        self.name, self.fn = name, fn
        self.scenarios = tuple(scenarios)
        doc = inspect.getdoc(fn) or ""
        blocks = [b.strip() for b in doc.split("\n\n") if b.strip()]
        self.summary = blocks[0].replace("\n", " ") if blocks else ""
        guides = [b for b in blocks[1:] if b.startswith("지침:")]
        self.guide = guides[0].replace("\n", " ")[:GUIDE_MAX] if guides else ""
        self.detail = "\n\n".join(b for b in blocks[1:] if not b.startswith("지침:"))
        self.params = _params_of(fn, doc)
        self.returns = _section(doc, "반환")

    def json_schema(self):
        """OpenAI/HCX 호환 function 스키마."""
        props, required = {}, []
        for p in self.params:
            props[p["name"]] = {"type": p["type"], "description": p["desc"]}
            if p["enum"]:
                props[p["name"]]["enum"] = p["enum"]
            if p["required"]:
                required.append(p["name"])
        return {"type": "function",
                "function": {"name": self.name,
                             "description": (self.summary + " " + self.guide).strip(),
                             "parameters": {"type": "object", "properties": props,
                                            "required": required}}}

    def signature(self):
        """코드 액션용 파이썬 시그니처 1줄."""
        args = []
        for p in self.params:
            args.append(p["name"] if p["required"] else f"{p['name']}={p['default']!r}")
        return f"{self.name}({', '.join(args)})"

    def __call__(self, *a, **kw):
        return self.fn(*a, **kw)

    def __repr__(self):
        return f"<Tool {self.signature()}>"


def _section(doc, head, keep_lines=False):
    m = re.search(rf"^{head}:\s*\n(.*?)(?=\n\S|\Z)", doc, re.M | re.S)
    if not m:
        return ""
    return m.group(1).rstrip() if keep_lines else re.sub(r"\s+", " ", m.group(1)).strip()


def _params_of(fn, doc):
    """시그니처 + docstring `인자:` 블록 → 파라미터 명세."""
    descs = {}
    blk = _section(doc, "인자", keep_lines=True)
    for line in blk.splitlines():
        m = re.match(r"\s*(\w+)\s*:\s*(.+)", line)
        if m:
            descs[m.group(1)] = m.group(2).strip()
    out = []
    for name, prm in inspect.signature(fn).parameters.items():
        if name.startswith("_"):
            continue
        ann = prm.annotation
        typ = _PY_JSON.get(ann, "string")
        enum = None
        if isinstance(ann, str) and "|" in ann:          # "a|b|c" 형태 주석 → enum
            enum = [x.strip() for x in ann.split("|")]
        out.append({"name": name, "type": typ, "desc": descs.get(name, ""),
                    "required": prm.default is inspect.Parameter.empty,
                    "default": None if prm.default is inspect.Parameter.empty else prm.default,
                    "enum": enum})
    return out


def tool(*scenarios):
    """도구 등록 데코레이터. 인자는 이 도구가 답하는 **시나리오 번호**다."""
    def deco(fn):
        name = fn.__name__
        if name in _REGISTRY:
            raise ValueError(f"도구 이름 중복: {name}")
        t = Tool(name, fn, scenarios)
        if not t.summary:
            raise ValueError(f"{name}: docstring 첫 줄(요약)이 없다 — 스키마가 비게 된다")
        if len(t.summary) > MAX_DESC:
            raise ValueError(f"{name}: 요약 {len(t.summary)}자 > {MAX_DESC}자 상한")
        missing = [p["name"] for p in t.params if not p["desc"]]
        if missing:
            raise ValueError(f"{name}: 인자 설명 없음 {missing} — docstring `인자:` 블록에 적을 것")
        _REGISTRY[name] = t
        if len(_REGISTRY) > MAX_TOOLS:
            raise ValueError(f"도구가 {len(_REGISTRY)}개 — 상한 {MAX_TOOLS}개 초과. "
                             f"선택 정확도가 개수에 민감하다(4→51개에서 43%→2%).")
        return fn
    return deco


def get(name):
    return _REGISTRY.get(name)


def all_tools():
    return tuple(_REGISTRY.values())


def json_schemas():
    """JSON function calling용 — 결선 HCX 경로."""
    return [t.json_schema() for t in _REGISTRY.values()]


def code_prompt():
    """코드 액션용 — 사용 가능한 함수 목록을 파이썬 시그니처로."""
    lines = ["사용 가능한 함수(파이썬으로 호출):"]
    for t in _REGISTRY.values():
        lines.append(f"  {t.signature()}")
        lines.append(f"      {t.summary}")
    return "\n".join(lines)


def scenario_coverage(scenarios):
    """{시나리오: [도구…]} — 도구가 없는 시나리오를 드러낸다."""
    out = {s: [] for s in scenarios}
    for t in _REGISTRY.values():
        for s in t.scenarios:
            out.setdefault(s, []).append(t.name)
    return out
