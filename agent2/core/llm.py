"""LLM 어댑터 — 개발은 OpenAI, 결선은 HyperCLOVA X. 코드는 하나다.

근거 전문은 `research/llm.md`. 설계를 가른 사실 하나만 여기 옮긴다:

  **CLOVA Studio가 OpenAI 호환 엔드포인트를 제공한다.**
  `https://clovastudio.stream.ntruss.com/v1/openai` — chat/completions·embeddings·models.
  `tools`·`tool_choice`·`response_format`이 전부 지원된다(공식 문서 「오픈AI 호환성」).
  즉 전환은 **base_url·키·모델명 교체**이고 코드 변경이 없다.

그래서 이 모듈엔 **provider 분기가 없다.** 와이어 프로토콜 하나에 프로필만 갈아끼운다.
분기가 없으면 갈라질 것도 없다 — 화요일 전환은 `LLM_PROFILE=hcx` 한 줄이다.

다만 "호환"은 chat/tools까지만 성립하고 가장자리에서 샌다. 실제로 다른 지점만 흡수한다:

  - 툴 인자가 JSON **문자열**(OpenAI)일 수도 **객체**(CLOVA 네이티브)일 수도 있다
  - 응답이 `choices[0].message`일 수도 `result.message`일 수도 있다
  - 툴콜 필드명이 `tool_calls`일 수도 `toolCalls`일 수도 있다
  - HCX는 `max_tokens` **하한이 1024**이고 `system` 메시지가 **1개**뿐이다
  - HCX는 `response_format.strict`를 **받되 무시한다** → 스키마 준수를 우리가 검증해야 한다

마지막 항목이 핵심이다. 스키마 제약을 통과해도 의미가 맞다는 보장이 없고
(arXiv 2607.18261·2604.25359), CLOVA는 strict를 무시하므로 `structured()`는 응답을
**믿지 않고 검증한 뒤 1회 복구 재시도**한다.

의존성 0(stdlib urllib). 두 엔드포인트가 같은 JSON을 쓰므로 SDK가 필요 없고,
결선 4시간 환경에서 설치 리스크를 0으로 만든다. 스트리밍은 쓰지 않는다
(계약이 `GET /answer` 단발 JSON이라 이벤트 경계 차이라는 위험을 아예 피한다).

사용:
    python3 -m agent2.core.llm           # 설정 점검(키 없어도 동작)
    python3 -m agent2.core.llm --probe   # 실제 3모드 호출(키 필요) — 화요일 전환 당일 필수
"""
import json
import os
import random
import re
import ssl
import time
import urllib.error
import urllib.request
from collections import namedtuple
from functools import lru_cache

from agent2 import config

# ---------------------------------------------------------------- 프로필

Profile = namedtuple("Profile", "name base_url model key_env min_max_tokens "
                                "max_temperature one_system strict_enforced")

#: 전환 지점. 코드가 아니라 **표**를 고친다.
PROFILES = {
    # 개발용. 키는 OPENAI_API_KEY.
    "openai": Profile(
        name="openai",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",              # LLM_MODEL로 덮을 수 있다(개발 비용 고려 기본값)
        key_env="OPENAI_API_KEY",
        min_max_tokens=None,
        max_temperature=2.0,
        one_system=False,
        strict_enforced=True,             # OpenAI는 strict를 실제로 강제한다
    ),
    # 결선용. 키는 CLOVA_API_KEY. 공식 문서 「오픈AI 호환성」 기준.
    "hcx": Profile(
        name="hcx",
        base_url="https://clovastudio.stream.ntruss.com/v1/openai",
        # ★ 2026-08-07 실측으로 확정. 종전 기본값은 HCX-007이었다.
        #   · HCX-005  chat OK · **tools(function calling) OK** — 도구 호출·인자 파싱 정상
        #   · HCX-007  HTTP 401 "No Service App" — 서비스 앱이 005로만 승인돼 호출 불가
        #   설계상으로도 005가 맞다: 우리 루프는 **전적으로 function calling**이고,
        #   007의 강점인 thinking은 공식 문서상 **function calling과 동시 사용 불가**다.
        #   출력 상한 4,096도 충분하다(우리 답변 실측 500~1,500자 ≈ 750토큰).
        #   ※ **튜닝 모델은 function calling을 못 쓴다**(공식 문서) — 파인튜닝으로 가면
        #     정본표·정본섹션 라우팅을 통째로 버려야 한다. 지금은 튜닝하지 않는다.
        model="HCX-005",
        key_env="CLOVA_API_KEY",
        min_max_tokens=1024,              # 네이티브 문서: maxTokens 하한 1024
        max_temperature=1.0,              # 네이티브 문서: 0.00~1.00
        one_system=True,                  # 네이티브 문서: system은 요청당 1개
        strict_enforced=False,            # 호환 문서: strict를 받되 무시한다
    ),
}

DEFAULT_PROFILE = "openai"

#: 키 환경변수 별칭. CLOVA 키를 `CLOVASTUDIO_API_KEY`로 쓰던 표기가 있어 둘 다 받는다 —
#: 화요일 전환에서 "키를 넣었는데 못 읽는다"로 시간을 버리지 않으려는 장치다.
KEY_ALIASES = {"CLOVA_API_KEY": ("CLOVASTUDIO_API_KEY", "NCP_CLOVASTUDIO_API_KEY"),
               "OPENAI_API_KEY": ("OPENAI_KEY",)}

# 재시도 대상 상태코드. 4xx 중 429/408/409/425만 일시적 오류로 본다.
#: CLOVA 40009 판별. 두 엔드포인트가 형태가 달라 **코드 문자열**로 본다
#: (호환: `{"error":{"code":"40009"}}` · 네이티브: `{"status":{"code":"40009"}}`).
_UNSUPPORTED_TOOL = re.compile(r'"code"\s*:\s*"?40009"?|Unsupported function')

_RETRY_STATUS = (408, 409, 425, 429, 500, 502, 503, 504)
#: HTTP 재시도 횟수. **바운드가 있다는 것 자체가 핵심**이다 —
#: IAL 조사(arXiv 2607.01641, 6,549개 저장소 정적분석)에서 무한 에이전트 루프의
#: **최대 원인이 "바운드 없는 재시도 되먹임" 25.0%**였다(도구호출 반복 23.5%가 2위).
#: 3은 주요 클라우드 SDK의 통용 기본값과 같다.
_MAX_RETRY = 3

#: ★ 429(rate limit)는 **다른 오류와 다른 백오프**를 쓴다(2026-08-10).
#:
#: 종전에는 전부 `0.5·2^n + jitter`(0.5s → 1.0s, 합 1.5초)였다. **429에는 무의미하다** —
#: CLOVA 한도는 **QPM/TPM, 즉 60초 창** 기준이라 1.5초 뒤에 다시 쳐도 같은 창 안이다.
#: 실측 피해: 측정 실행마다 4~13건이 죽었고, 그중 한 번은 `#172·173·174·175`가 **연속**
#: 으로 죽어 시나리오 1-9를 **회귀로 잘못 판정**했다(§25-1). 측정이 거짓말을 했다.
#:
#: 값의 근거 — NCP 공식 쿡북(포럼 topic/488)이 CLOVA 429 대응으로 쓰는 값 그대로다:
#:     `max_retries = 5` · `retry_delay_seconds = 10` · 배치 사이 `time.sleep(30)`
#: 내가 고른 숫자가 아니다.
#:
#: 그리고 **`Retry-After` 헤더를 존중한다**(RFC 9110 §10.2.3). 서버가 대기 시간을
#: 알려주면 그게 추측보다 정확하다. 종전 코드는 헤더를 아예 읽지 않았다.
#: 폭주 방지: 헤더 값이 있어도 `_RATE_WAIT_MAX`로 상한을 건다.
#: ★ **429만** 넣는다. 처음에 503을 같이 넣었다가 뺐다 — topic/488은 429만 다루고,
#:   503(Service Unavailable)이 rate limit이라는 근거가 없다. 근거 없이 넣은 값이었고,
#:   테스트가 잡았다(`test_persistent_5xx_gives_up_with_detail`이 3회를 기대하는데
#:   503이 rate 경로로 가면서 5회가 됐다).
_RATE_STATUS = (429,)
_RATE_RETRY = 5                       # topic/488 `max_retries`
_RATE_DELAY = 10.0                    # topic/488 `retry_delay_seconds`
_RATE_WAIT_MAX = 60.0                 # `Retry-After`가 크게 와도 여기까지만 기다린다

# ---------------------------------------------------------------- 반환 타입

Usage = namedtuple("Usage", "prompt completion total")
ToolCall = namedtuple("ToolCall", "id name arguments")
Reply = namedtuple("Reply", "text tool_calls finish_reason usage model raw")


class LLMError(Exception):
    """어댑터 공통 예외."""


class LLMUnavailable(LLMError):
    """키가 없거나 엔드포인트에 닿지 못함. 루프는 이걸 보고 결정론 답변으로 물러선다."""


class UnsupportedTool(LLMError):
    """CLOVA 40009 — 모델이 **스키마에 없는 함수명을 생성**해 응답이 통째로 버려졌다.

    공식 troubleshoot(40009 두 번째 사례):
        원인 "Unsupported tool request"
        조치 "Select a tool that can process the request, or check whether the tool
              supports the request."

    실측(2026-08-07): HCX-005는 맞는 도구를 못 찾으면 `get_inventories`·`get_capital`·
    `get_shares_outstanding`·`get_balance_sheet` 같은 **없는 이름**을 만든다. 서버는
    400으로 응답 전체를 버리므로 **모델 텍스트도 정상 호출도 함께 사라진다.**
    270문항 각 1회에서 첫 시도 실패 3.7~7.8%.

    **재시도로는 안 풀린다**(같은 입력 → 같은 이름을 또 생성): 그대로 재시도 0/27,
    270 전수에서 1/10만 회복. 반면 `tool_choice="none"`은 **18/18 회피**한다.
    그래서 일반 오류와 구분해 따로 잡고, 루프가 도구를 끈 채 다시 묻게 한다.
    """


class ModeConflict(LLMError):
    """chat/structured/tools 동시 사용. HCX-007이 금지하므로 코드에서 먼저 막는다."""


class SchemaError(LLMError):
    """복구 재시도 뒤에도 스키마를 만족하지 못함."""


# ---------------------------------------------------------------- 스키마 검증

#: CLOVA structured outputs가 지원하는 키워드만 검증한다(문서 기준).
#: `pattern`은 **미지원**이라 스키마에 쓰지 않고, 검증기도 다루지 않는다.
_TYPES = {"string": str, "integer": int, "number": (int, float),
          "boolean": bool, "object": dict, "array": list}


def validate(obj, schema, path="$"):
    """JSON 스키마 부분집합 검증. 위반 목록(문자열)을 반환한다. 빈 리스트 = 통과.

    왜 직접 검증하나: CLOVA가 `strict`를 무시하고(공식 문서), 제약 디코딩을 통과해도
    의미가 어긋날 수 있다(arXiv 2604.25359). 제공자를 믿지 않는다.
    """
    errs = []
    if "anyOf" in schema:
        if not any(not validate(obj, s, path) for s in schema["anyOf"]):
            errs.append(f"{path}: anyOf 어느 분기도 만족하지 않음")
        return errs

    typ = schema.get("type")
    if typ:
        py = _TYPES.get(typ)
        # bool은 int의 하위형이라 숫자로 새어 들어온다 — 명시적으로 막는다.
        bad = py is None or not isinstance(obj, py) or (
            typ in ("integer", "number") and isinstance(obj, bool))
        if bad:
            return [f"{path}: type {typ} 기대, {type(obj).__name__} 받음"]

    if "enum" in schema and obj not in schema["enum"]:
        errs.append(f"{path}: enum {schema['enum']} 밖의 값 {obj!r}")

    if typ in ("integer", "number"):
        if "minimum" in schema and obj < schema["minimum"]:
            errs.append(f"{path}: minimum {schema['minimum']} 미만 ({obj})")
        if "maximum" in schema and obj > schema["maximum"]:
            errs.append(f"{path}: maximum {schema['maximum']} 초과 ({obj})")

    if typ == "array":
        if "minItems" in schema and len(obj) < schema["minItems"]:
            errs.append(f"{path}: minItems {schema['minItems']} 미만 ({len(obj)})")
        if "maxItems" in schema and len(obj) > schema["maxItems"]:
            errs.append(f"{path}: maxItems {schema['maxItems']} 초과 ({len(obj)})")
        if "items" in schema:
            for i, it in enumerate(obj):
                errs += validate(it, schema["items"], f"{path}[{i}]")

    if typ == "object":
        for key in schema.get("required", []):
            if key not in obj:
                errs.append(f"{path}: 필수 필드 누락 '{key}'")
        for key, sub in schema.get("properties", {}).items():
            if key in obj:
                errs += validate(obj[key], sub, f"{path}.{key}")

    return errs


# ---------------------------------------------------------------- 응답 파싱
# 호환 계층이 OpenAI 모양을 내든 CLOVA 네이티브 모양을 내든 둘 다 흡수한다.

def _message_of(body):
    """`choices[0].message`(OpenAI) 또는 `result.message`(CLOVA 네이티브)."""
    if isinstance(body.get("choices"), list) and body["choices"]:
        return body["choices"][0].get("message") or {}, body["choices"][0]
    res = body.get("result")
    if isinstance(res, dict):
        return res.get("message") or {}, res
    return {}, {}


def _finish_of(choice, body):
    for k in ("finish_reason", "finishReason"):
        if choice.get(k):
            return choice[k]
        res = body.get("result")
        if isinstance(res, dict) and res.get(k):
            return res[k]
    return ""


def _usage_of(body):
    u = body.get("usage")
    if not isinstance(u, dict):
        res = body.get("result")
        u = res.get("usage") if isinstance(res, dict) else None
    if not isinstance(u, dict):
        return Usage(0, 0, 0)
    p = u.get("prompt_tokens", u.get("promptTokens", 0)) or 0
    c = u.get("completion_tokens", u.get("completionTokens", 0)) or 0
    t = u.get("total_tokens", u.get("totalTokens", 0)) or (p + c)
    return Usage(int(p), int(c), int(t))


def _tool_calls_of(msg):
    """`tool_calls`(OpenAI) / `toolCalls`(CLOVA). arguments는 문자열일 수도 객체일 수도 있다."""
    raw = msg.get("tool_calls") or msg.get("toolCalls") or []
    out = []
    for tc in raw:
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except ValueError:
                args = {"__unparsed__": args}      # 정직한 실패 — 조용히 버리지 않는다
        elif not isinstance(args, dict):
            args = {}
        out.append(ToolCall(tc.get("id") or "", fn.get("name") or "", args))
    return tuple(out)


# ---------------------------------------------------------------- 어댑터

class LLM:
    """OpenAI 와이어 프로토콜 클라이언트. 프로필만 바꾸면 HCX가 된다.

    3모드를 **분리**해 노출한다 — HCX-007이 structured/tools/thinking을 동시에 못 쓰기 때문.
    동시 사용은 `ModeConflict`로 호출 시점에 막는다(결선에서 500을 보는 대신).
    """

    def __init__(self, profile=None, model=None, api_key=None, temperature=0.0,
                 seed=0, timeout=60, max_tokens=None, audit=None):
        config.load_env()
        name = profile or os.environ.get("LLM_PROFILE") or DEFAULT_PROFILE
        if name not in PROFILES:
            raise LLMError(f"미등록 프로필: {name} — {sorted(PROFILES)} 중 하나")
        self.profile = PROFILES[name]
        self.base_url = (os.environ.get("LLM_BASE_URL") or self.profile.base_url).rstrip("/")
        self.model = model or os.environ.get("LLM_MODEL") or self.profile.model
        # `None`이면 환경변수를 찾고, **빈 문자열이면 '키 없음'을 명시한 것**으로 본다.
        # 둘을 구분하지 않으면 키가 있는 환경에서 '키 없는 경로'를 테스트할 수 없다
        # (실제로 이 구분이 없어서 키 발급 직후 테스트가 깨졌다).
        self.api_key = (_key_from_env(self.profile.key_env) if api_key is None
                        else api_key) or ""
        self.temperature = min(float(temperature), self.profile.max_temperature)
        self.seed = seed
        self.timeout = timeout
        # ★★ 기본을 2048 → **1024**로 낮췄다(2026-08-19 · 주최 안내 반영).
        #
        # 주최 답변: "TPM 한도는 실제 소비 토큰이 아니라 **`input token` + `maxTokens`**로
        # 계산됩니다. maxTokens를 타이트하게 설정하는 것도 에러 발생을 줄이는 방법입니다."
        # 즉 **쓰지도 않는 출력 여유가 한도를 그대로 먹는다.**
        #
        # 실측(과거 답변 **3,036건 전수**): 중앙 154자 · 99% 578자 · **최대 1,163자**.
        # 2048 초과 **0.0%** · 1024 초과 0.1%. 즉 2048은 한 번도 쓰이지 않았다.
        # HCX `maxTokens` 하한이 1024라 그 아래로는 못 내린다(`_floor_tokens`).
        #
        # 효과(문항당 LLM 호출 중앙 2회 · 입력 평균 9,012토큰 기준):
        #     문항당 TPM 소비 13,108 → **11,060**
        #     분당 처리 테스트앱 4 → **5문항** · 서비스앱 13 → **16문항**
        self.max_tokens = self._floor_tokens(max_tokens or 1024)
        self.audit = audit

        # 예산 집행은 loop.py가 한다. 어댑터는 **집계만** 한다.
        self.calls = 0
        self.retries = 0
        self.usage = Usage(0, 0, 0)
        self.log = []          # [{mode, model, ms, usage, finish, error}]

    # ---------- 상태 ----------
    def available(self):
        """키가 있는가. 없으면 루프가 LLM 없는 결정론 경로로 물러선다."""
        return bool(self.api_key)

    def _floor_tokens(self, n):
        lo = self.profile.min_max_tokens
        return max(int(n), lo) if lo else int(n)

    # ---------- 3모드 ----------
    def chat(self, messages, system=None, max_tokens=None, temperature=None,
             reasoning_effort=None):
        """자유 텍스트. 서술·요약 전용 — **수치를 만들게 하지 않는다**(결정론 레인이 계산)."""
        body = self._body(messages, system, max_tokens, temperature)
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        return self._call("chat", body)

    def tools(self, messages, schemas, system=None, tool_choice="auto",
              max_tokens=None, temperature=None):
        """툴 호출. `tools/registry.json_schemas()`를 그대로 받는다.

        `reasoning_effort`를 받지 않는다 — HCX는 thinking과 function calling이 배타다.
        `n`·`parallel_tool_calls`도 보내지 않는다(CLOVA는 1·true 고정이라 보낼 이유가 없다).
        """
        if not schemas:
            raise ModeConflict("tools 모드인데 스키마가 비었다")
        body = self._body(messages, system, max_tokens, temperature)
        body["tools"] = list(schemas)
        body["tool_choice"] = tool_choice
        return self._call("tools", body)

    def structured(self, messages, schema, system=None, name="result",
                   max_tokens=None, temperature=None, repair=True):
        """JSON 스키마 응답 → **검증된 dict**. 실패 시 1회 복구 재시도 후 `SchemaError`.

        CLOVA가 `strict`를 무시하므로(공식 문서) 검증은 선택이 아니라 필수다.
        """
        body = self._body(messages, system, max_tokens, temperature)
        # strict=False로 **고정**한다. 개발(OpenAI)과 결선(HCX)의 동작을 같게 만들기 위해서다.
        #   · HCX는 strict를 받되 **무시**한다(공식 문서) → 결선에선 어차피 자체 검증이 유일한 방어선.
        #   · OpenAI strict=True는 모든 object에 `additionalProperties:false`와
        #     "required에 전 속성 나열"을 강제한다(실측 400). 선택 필드를 필수로 바꿔야 해서
        #     스키마 의미가 달라지고, 그렇게 얻은 안전망은 결선에서 사라진다.
        # 개발에서만 도는 안전망은 결선 사고를 감춘다 — 대신 복구 재시도 경로를 개발에서 실제로 태운다.
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": name, "schema": schema,
                                                   "strict": False}}
        reply = self._call("structured", body)
        obj, errs = _coerce_json(reply.text, schema)
        if not errs:
            return obj

        if not repair:
            raise SchemaError("; ".join(errs))

        # 하이브리드 복구(arXiv 2605.02363) — 무엇이 틀렸는지 되먹인다.
        self._note("structured", "schema_retry", "; ".join(errs[:3]))
        fix = list(messages) + [
            {"role": "assistant", "content": reply.text or ""},
            {"role": "user", "content":
                "위 응답이 스키마를 위반했습니다. 위반 내용:\n- "
                + "\n- ".join(errs[:8])
                + "\n\n설명 없이 스키마를 만족하는 JSON만 다시 출력하세요."}]
        body2 = self._body(fix, system, max_tokens, temperature)
        body2["response_format"] = body["response_format"]
        reply2 = self._call("structured", body2)
        obj2, errs2 = _coerce_json(reply2.text, schema)
        if errs2:
            raise SchemaError("복구 후에도 위반: " + "; ".join(errs2))
        return obj2

    # ---------- 요청 조립 ----------
    def _body(self, messages, system, max_tokens, temperature):
        msgs = _normalize(messages, system, one_system=self.profile.one_system)
        if not msgs:
            raise LLMError("메시지가 비었다")
        body = {"model": self.model,
                "messages": msgs,
                "max_tokens": self._floor_tokens(max_tokens or self.max_tokens),
                "temperature": min(float(self.temperature if temperature is None
                                         else temperature),
                                   self.profile.max_temperature)}
        # ★ `is not None`이어야 한다. `if self.seed:`로 두면 **0이 누락**된다 —
        #   기본값이 0이던 시절 우리는 seed를 **한 번도 안 보냈다**(실측 2026-08-07).
        #   ※ 플레이그라운드 UI는 1 이상만 받지만 **API는 0도 받는다**(실측 2026-08-07).
        #     그래서 값은 0으로 두고, **누락되던 조건만** 고친다.
        #   ※ seed를 보낸다고 완전 재현이 되는 건 아니다(arXiv 2408.04667·2506.09501:
        #     temperature=0도 결정론이 아니다). 안 보내는 것보다 나을 뿐이고,
        #     실제 변동 감소 여부는 **재보고 판단한다**(가정하지 않는다).
        if self.seed is not None:
            body["seed"] = int(self.seed)
        return body

    # ---------- 전송 ----------
    def _call(self, mode, body):
        if not self.available():
            raise LLMUnavailable(
                f"{self.profile.key_env}가 없다 — 프로필 {self.profile.name}. "
                f".env에 넣거나 환경변수로 주입할 것")
        t0 = time.time()
        try:
            raw = self._post("/chat/completions", body)
        except LLMError as e:
            self._note(mode, "error", str(e), ms=(time.time() - t0) * 1000)
            raise
        ms = (time.time() - t0) * 1000

        msg, choice = _message_of(raw)
        usage = _usage_of(raw)
        self.calls += 1
        self.usage = Usage(self.usage.prompt + usage.prompt,
                           self.usage.completion + usage.completion,
                           self.usage.total + usage.total)
        reply = Reply(text=(msg.get("content") or "").strip(),
                      tool_calls=_tool_calls_of(msg),
                      finish_reason=_finish_of(choice, raw),
                      usage=usage, model=raw.get("model") or self.model, raw=raw)
        self.log.append({"mode": mode, "model": reply.model, "ms": round(ms, 1),
                         "usage": usage._asdict(), "finish": reply.finish_reason,
                         "tool_calls": [t.name for t in reply.tool_calls]})
        if self.audit is not None:
            self.audit.compute(f"llm.{mode}",
                               (self.model, f"{usage.total}tok"), reply.finish_reason)
        return reply

    def _post(self, path, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json",
                     "Authorization": "Bearer " + self.api_key})
        last = ""
        wait = None                            # 429일 때 다음 대기 시간(초). None이면 일반 백오프
        attempt = 0
        budget = _MAX_RETRY
        while attempt < budget:
            try:
                with urllib.request.urlopen(req, timeout=self.timeout,
                                            context=_ssl_context()) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode("utf-8", "replace")[:400]
                    ra = e.headers.get("Retry-After") if e.headers else None
                finally:
                    e.close()                 # 오류 응답도 닫아야 커넥션이 반납된다
                last = f"HTTP {e.code}: {detail}"
                if e.code in _RATE_STATUS:
                    # 429/503만 **긴 백오프 + 더 많은 재시도**로 갈아탄다(위 상수 주석 참조).
                    budget = max(budget, _RATE_RETRY)
                    try:                       # `Retry-After`는 초 또는 HTTP-date. 초만 받는다.
                        wait = min(float(ra), _RATE_WAIT_MAX) if ra else _RATE_DELAY
                    except (TypeError, ValueError):
                        wait = _RATE_DELAY
                # ★ 40009는 **재시도 대상이 아니다**(같은 입력이면 같은 이름을 또 만든다 —
                #   실측 0/27). 호출자가 `tool_choice`를 바꿔 다시 물을 수 있게 따로 알린다.
                if _UNSUPPORTED_TOOL.search(detail or ""):
                    raise UnsupportedTool(last)
                if e.code not in _RETRY_STATUS:
                    raise LLMError(last)
            except urllib.error.URLError as e:
                last = f"연결 실패: {e.reason}"
            except (TimeoutError, OSError) as e:
                last = f"전송 오류: {e}"
            attempt += 1
            if attempt < budget:
                self.retries += 1
                time.sleep((wait if wait is not None
                            else 0.5 * (2 ** (attempt - 1))) + random.random() * 0.2)
                wait = None                    # 다음 실패가 429가 아니면 일반 백오프로 돌아간다
        raise LLMError(f"{_MAX_RETRY}회 재시도 실패 — {last}")

    def _note(self, mode, kind, detail, ms=None):
        self.log.append({"mode": mode, "event": kind, "detail": detail,
                         "ms": None if ms is None else round(ms, 1)})

    def __repr__(self):
        return (f"<LLM {self.profile.name} {self.model} 호출 {self.calls} "
                f"토큰 {self.usage.total} 재시도 {self.retries}"
                f"{'' if self.available() else ' (키 없음)'}>")


@lru_cache(maxsize=1)
def _ssl_context():
    """CA 번들이 실린 SSL 컨텍스트.

    왜 필요한가: macOS python.org 프레임워크 설치본은 기본 CA가 비어 있다
    (`ssl.get_default_verify_paths().cafile is None`) — 실측으로 확인했고,
    이 상태에서 모든 HTTPS 호출이 CERTIFICATE_VERIFY_FAILED로 죽는다.
    「Install Certificates.command」를 사람이 돌리는 방식은 배포 환경에서 재현되지 않으므로
    코드가 스스로 확보한다(있으면 certifi, 없으면 시스템 기본).

    **검증을 끄지 않는다.** 인증서 검증 비활성화는 중간자 공격에 그대로 노출된다.
    """
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca", 0) == 0:
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except Exception:                  # certifi가 없으면 시스템 기본으로 간다
            pass
    return ctx


def _key_from_env(name):
    """정식 이름 → 별칭 순으로 찾는다. 빈 문자열은 '없음'으로 본다."""
    for n in (name,) + KEY_ALIASES.get(name, ()):
        v = (os.environ.get(n) or "").strip()
        if v:
            return v
    return ""


def _normalize(messages, system, one_system):
    """메시지 정규화. HCX는 system이 1개뿐이라 여러 개를 **합친다**."""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs += [dict(m) for m in messages]
    if not one_system:
        return msgs
    sys_parts = [m["content"] for m in msgs if m.get("role") == "system"]
    rest = [m for m in msgs if m.get("role") != "system"]
    if not sys_parts:
        return rest
    return [{"role": "system", "content": "\n\n".join(sys_parts)}] + rest


def _coerce_json(text, schema):
    """텍스트 → dict + 위반 목록. 코드펜스로 감싸 오는 경우까지 흡수한다."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("```")[1] if s.count("```") >= 2 else s[3:]
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    s = s.strip()
    if not s:
        return None, ["응답이 비었다"]
    try:
        obj = json.loads(s)
    except ValueError as e:
        return None, [f"JSON 파싱 실패: {e}"]
    return obj, validate(obj, schema)


# ---------------------------------------------------------------- 테스트용 대역

class FakeLLM(LLM):
    """네트워크 없이 도는 대역. 테스트·오프라인 개발용.

    실물과 **같은 클래스 계층**을 쓴다 — 정규화·검증·파싱 경로를 그대로 타야
    테스트가 실제 코드를 검사하는 게 된다(응답 조립만 가로챈다).
    """

    def __init__(self, replies=(), **kw):
        kw.setdefault("api_key", "fake")
        super().__init__(**kw)
        self.replies = list(replies)
        self.sent = []

    def available(self):
        return True

    def _post(self, path, body):
        self.sent.append(body)
        if not self.replies:
            raise LLMError("FakeLLM: 준비된 응답이 없다")
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, dict) and "choices" in r:
            return r
        msg = {"role": "assistant", "content": r if isinstance(r, str) else ""}
        if isinstance(r, list):                      # 툴콜 목록
            msg["content"] = ""
            msg["tool_calls"] = r
        return {"model": self.model,
                "choices": [{"message": msg,
                             "finish_reason": "tool_calls" if isinstance(r, list) else "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


# ---------------------------------------------------------------- 점검

def _probe():
    """실제 호출로 3모드를 확인한다. **화요일 HCX 전환 당일 반드시 돌릴 것.**"""
    llm = LLM()
    print(llm, "\n base_url:", llm.base_url)
    if not llm.available():
        print(f" ✗ {llm.profile.key_env} 없음 — 실호출 불가")
        return
    print(" chat      :", repr(llm.chat([{"role": "user", "content": "한 단어로 답: 공시"}]).text))
    schema = {"type": "object", "properties": {"n": {"type": "integer"}},
              "required": ["n"]}
    print(" structured:", llm.structured(
        [{"role": "user", "content": "n에 7을 넣은 JSON"}], schema))
    tc = llm.tools([{"role": "user", "content": "삼성전자를 찾아줘"}],
                   [{"type": "function",
                     "function": {"name": "resolve_company",
                                  "description": "기업명을 코퍼스 기준으로 해소한다",
                                  "parameters": {"type": "object",
                                                 "properties": {"name": {"type": "string"}},
                                                 "required": ["name"]}}}])
    print(" tools     :", tc.tool_calls or f"(툴콜 없음) {tc.text!r}")
    print(" 누적      :", llm)


if __name__ == "__main__":
    import sys

    if "--probe" in sys.argv:
        _probe()
    else:
        config.load_env()
        for nm, p in PROFILES.items():
            key = _key_from_env(p.key_env)
            mark = "✓" if key else "·"
            print(f"{mark} {nm:7s} {p.model:12s} {p.base_url}")
            print(f"          키 {p.key_env}={'있음' if key else '없음'} · "
                  f"max_tokens 하한 {p.min_max_tokens or '없음'} · "
                  f"temp≤{p.max_temperature} · system {'1개' if p.one_system else '다수'} · "
                  f"strict {'강제' if p.strict_enforced else '무시(자체검증)'}")
        cur = LLM()
        print(f"\n현재 프로필: {cur.profile.name} ({cur.model}) — "
              f"{'실호출 가능' if cur.available() else '키 없음'}")
        print("전환: .env에 LLM_PROFILE=hcx · CLOVA_API_KEY=... 두 줄")
