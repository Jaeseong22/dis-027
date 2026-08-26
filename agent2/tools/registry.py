"""도구 등록기 — 스키마와 구현을 한 곳에서 정의한다.

설계 근거:

  ① **도구는 12개 이내.** 선택 정확도가 개수에 민감하다 —
     50개에서 프런티어 84~95%, 200개에서 41~83%.
     BFCL 실측은 4개→51개에서 43%→2%로 무너졌고, 실무 관측도 10~15개가 한계다.

  ② **스키마 품질이 모델 선택보다 영향이 크다.** 잘 설계된 스키마가 전 모델에서 +10~20%,
     Claude는 상세 설명+예시에서 +5~8%. 설명은 200자 이내·능동태·필요하면 예시 1개.

  ③ **액션은 코드가 JSON보다 낫다**(CodeAct, ICML 2024: 성공률 최대 +20%, 행동 수 -30%).
     다만 같은 논문이 오픈소스 모델의 큰 열세(13.4% vs GPT-4 74.4%)를 보고했고
     결선 런타임이 HCX라 **JSON 스펙을 함께 낸다**. 두 형식이 같은 등록부에서 나온다.

도구는 `@tool` 로 등록하고, 함수 시그니처와 docstring이 그대로 스키마가 된다.
설명을 딴 데 또 쓰지 않는다 — 갈라지기 때문이다.
"""
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
        # ★ `지침:`으로 시작하는 블록은 **스키마 설명에 실어 보낸다.**
        #   종전에는 첫 문단만 description으로 갔고 나머지는 `detail`로 빠져
        #   **모델에 도달하지 않았다**(실측: sector_peers에 쓴 "섹터 질의면 먼저 이 도구로
        #   구성원을 확인하라"가 버려지고 있었다. 그래서 모델이 구성원을 추측해
        #   코퍼스에 없는 `LG전자`를 끌어들였다).
        #   근거: 도구 설명 품질이 모델 선택보다 영향이 크다(잘 설계된 스키마 +10~20%).
        #   길이는 200자 이내로 유지한다 — "토큰 효율적으로 돌려주라"는 지침과 상충하면 안 된다.
        #
        #   ★★ 2026-08-07 — **이 규칙을 모르면 도구가 통째로 안 불린다.** HCX-005는
        #   맞는 도구를 못 찾으면 **없는 함수명을 지어내고**, CLOVA 서버는 그 응답을
        #   `HTTP 400 {"code":"40009","message":"Unsupported function"}`로 통째로 버린다
        #   (모델 텍스트와 정상 호출까지 함께 사라진다). 270문항 실측 3.7%가 이걸로 죽었다.
        #     실측 — 재고자산·매출원가·판관비 질의 9건 × 3회:
        #       현행(개념 목록이 detail로 빠짐)  실패 27/27 · 호출 성공분은 get_financials 4회
        #       `지침:`으로 개념 한글명 노출     실패  6/27 · 호출 21회 **전부 financial_series**
        #     모델이 지어낸 이름: get_inventories · get_cost_of_sales · get_sga ·
        #     get_balance_sheet — 전부 financial_series가 이미 커버하는 개념이다.
        #   ※ 재시도·프롬프트 경고("목록 밖 도구 금지")는 **둘 다 0/27**로 무효였다.
        #     도구 이름을 get_→read_로 바꾸는 것도 실패율은 비슷했으나(5/27) 모델이
        #     맞는 도구로 수렴하지 않아 채택하지 않았다 — 원인이 아니라 섭동이다.
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
    """시그니처 + docstring `인자:` 블록 → 파라미터 명세.

    형식: 한 줄에 `이름: 설명` 하나. 여러 개면 줄을 나눈다.
    (한때 `·`를 구분자로 썼는데 설명 안에도 `·`를 쓰다 보니 앞쪽 파라미터가 잘려나갔다 —
     구분자는 설명에 안 나오는 문자여야 한다. 줄바꿈이 가장 안전하다.)
    """
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
    """도구 등록 데코레이터. 인자는 이 도구가 답하는 **시나리오 번호**다.

    시나리오 번호를 붙이는 이유: 어떤 실무 질문에 쓰이는지가 곧 도구 설명의 근거이고,
    커버리지 검사(어떤 시나리오에 도구가 없는지)를 자동으로 할 수 있다.
    """
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
