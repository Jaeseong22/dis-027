"""LLM 어댑터 — HyperCLOVA X."""
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
    # 키는 CLOVA_API_KEY. 공식 문서 「오픈AI 호환성」 기준.
    "hcx": Profile(
        name="hcx",
        base_url="https://clovastudio.stream.ntruss.com/v1/openai",
        model="HCX-005",
        key_env="CLOVA_API_KEY",
        min_max_tokens=1024,              # 네이티브 문서: maxTokens 하한 1024
        max_temperature=1.0,              # 네이티브 문서: 0.00~1.00
        one_system=True,                  # 네이티브 문서: system은 요청당 1개
        strict_enforced=False,            # 호환 문서: strict를 받되 무시한다
    ),
}

DEFAULT_PROFILE = "hcx"

#: 키 환경변수 별칭. CLOVA 키를 `CLOVASTUDIO_API_KEY`로 쓰던 표기가 있어 둘 다 받는다 —
#: 화요일 전환에서 "키를 넣었는데 못 읽는다"로 시간을 버리지 않으려는 장치다.
KEY_ALIASES = {"CLOVA_API_KEY": ("CLOVASTUDIO_API_KEY", "NCP_CLOVASTUDIO_API_KEY")}

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
    """CLOVA 40009 — 모델이 **스키마에 없는 함수명을 생성**해 응답이 통째로 버려졌다."""


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
    """JSON 스키마 부분집합 검증. 위반 목록(문자열)을 반환한다. 빈 리스트 = 통과."""
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
    """OpenAI 와이어 프로토콜 클라이언트. 프로필만 바꾸면 HCX가 된다."""

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
        """툴 호출. `tools/registry.json_schemas()`를 그대로 받는다."""
        if not schemas:
            raise ModeConflict("tools 모드인데 스키마가 비었다")
        body = self._body(messages, system, max_tokens, temperature)
        body["tools"] = list(schemas)
        body["tool_choice"] = tool_choice
        return self._call("tools", body)

    def structured(self, messages, schema, system=None, name="result",
                   max_tokens=None, temperature=None, repair=True):
        """JSON 스키마 응답 → **검증된 dict**. 실패 시 1회 복구 재시도 후 `SchemaError`."""
        body = self._body(messages, system, max_tokens, temperature)
        # strict=False로 **고정**한다.
        #   · HCX는 strict를 받되 **무시**한다(공식 문서) → 결선에선 어차피 자체 검증이 유일한 방어선.
        #   · OpenAI strict=True는 모든 object에 `additionalProperties:false`와
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": name, "schema": schema,
                                                   "strict": False}}
        reply = self._call("structured", body)
        obj, errs = _coerce_json(reply.text, schema)
        if not errs:
            return obj

        if not repair:
            raise SchemaError("; ".join(errs))

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
    """CA 번들이 실린 SSL 컨텍스트."""
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
    """네트워크 없이 도는 대역. 테스트·오프라인 개발용."""

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
