"""경로·상수. `agent/`에 의존하지 않는다(2호기는 1호기를 참조하지 않는다).

CORPUS_DIR은 환경변수로 덮을 수 있다. 기본값은 저장소 루트의 `3.공시/corpus`.
"""
import os
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS_DIR = os.environ.get("CORPUS_DIR") or os.path.join(BASE_DIR, "3.공시", "corpus")
AGENT_DIR = os.path.dirname(os.path.abspath(__file__))

RAW_DIR = os.path.join(CORPUS_DIR, "raw")
UNIVERSE_CSV = os.path.join(CORPUS_DIR, "universe.csv")
MANIFEST_JSONL = os.path.join(CORPUS_DIR, "manifest.jsonl")

#: 주최 명세(README.md·data_filter.md)에 기재된 값 — 로딩 시 실측과 대조한다.
EXPECT = {
    "corps": 70,
    "docs": 4204,
    "catalog": 22980,        # list_*.json 합계(실측). 원문 보유는 docs 4,204건뿐이다.
    "doc_groups": ("periodic", "major", "exchange", "holding"),
    "periodic_years": (2023, 2024, 2025, 2026),
    "rcept_range": ("20230101", "20260630"),
}

#: doc_group ↔ list_*.json 파일명 (DART 공시유형 코드)
LIST_FILE = {"periodic": "list_A.json", "major": "list_B001.json",
             "exchange": "list_I.json", "holding": "list_D.json"}


#: 도구 관측 1건을 프롬프트에 넣을 때의 상한(자).
#: `loop`과 `tools`가 함께 봐야 해서 여기 둔다(서로 import하면 순환이 된다).
#:
#: **실측으로 정했다**(2026-08-02). 27문항 각각에서 "정본표 핵심행을 하나도 자르지 않고
#: 담으려면 몇 자가 필요한가"를 재보니:
#:     중앙값 816 · p90 **4,503** · 최대 15,813(1-10 발행일자별 내역 54행)
#: p90을 덮는 5,000자로 잡는다. 최대값(15,813)은 54행 이력이라 전량이 애초에 불필요하고,
#: 넘치면 몇 행을 생략했는지 고지한다(조용한 손실 금지).
#:
#: ★ 다만 **2,500 → 5,000의 이득은 측정되지 않았다**(A/B 각 2회: 둘 다 완전통과 21.5±0.5·
#:   ACC 88.8%, 토큰도 비슷). 근거(p90 4,503)는 맞았지만 종단 점수는 안 움직였다.
#:   큰 표가 필요한 문항(1-10 발행일자별 내역)을 위해 5,000을 유지하되,
#:   "제약이 없다"와 "올리면 이득이다"는 **별개**라는 것을 기록해 둔다.
#:
#: ★ 이전에는 2,500이었고 그 근거는 "주최가 동기 GET이라 타임아웃이 곧 0점"이었다.
#:   **주최 명세를 전수 확인하니 응답시간 제한이 없다** — `타임아웃`·`timeout` 0회이고
#:   주최 예시 코드가 `requests.get()`에 timeout 인자 없이(=무제한 대기) 호출한다.
#:   없는 제약 때문에 답에 필요한 값을 잘라내고 있었다(1-10·1-16·1-8).
#:   근거: research/evaluation.md
#:
#: ★★ 2026-08-06 재정의 — **더 이상 내가 고른 숫자가 아니다.**
#:   사용자 지시: "토큰은 제약이 없다. 정확도를 깎는 토큰 정량은 없애라."
#:   그래서 유일한 실제 한계인 **모델 컨텍스트 창**에서 역산한다.
#:
#:     컨텍스트     HCX-007 128,000토큰(입력+출력 합), 최대 출력 32,768
#:                  — NAVER CLOVA Studio 모델 사양 문서. 개발 프록시 gpt-4o-mini도 128k.
#:     문자/토큰    **2.0** (실측 2026-08-06: 공시 표 JSON 2,000자→1,004토큰,
#:                  6,000자→2,987토큰. 한국어+숫자 혼합 기준)
#:     고정 비용    SYSTEM 1,248자 + 도구 스키마 4,821자 = 약 3,034토큰(실측)
#:     출력 여유    8,000토큰(우리 답변은 실측 500~1,500자지만 넉넉히 잡는다)
#:     관측 개수    최악 `hard_steps`(5) × 스텝당 도구호출 2 = 10건
#:
#:   → (128,000 − 3,034 − 8,000) × 2.0 / 10 ≈ **23,000자**
#:
#:   실측 뒷받침: 현재 문항당 누적 사용량은 6,600~16,000토큰으로 128k의 5~13%다.
#:   즉 컨텍스트는 병목이 아니었고, 5,000자는 남는 여유를 안 쓰고 값을 자르고 있었다.
_CTX_TOKENS = 128_000          # HCX-007 / gpt-4o-mini 공통(문서 확인)
_CHARS_PER_TOKEN = 2.0         # 한국어 실측
_FIXED_TOKENS = 3_034          # SYSTEM + 도구 스키마(실측)
_OUTPUT_RESERVE = 8_000        # 출력 여유
#: 한 문항이 쌓는 관측 개수. **가정하지 않고 실측했다**(270문항 전수):
#:   중앙값 2 · p90 4 · p99 8 · **최대 16**
#: 최대값을 쓴다 — p90으로 잡으면 상위 10%에서 컨텍스트를 넘긴다.
_MAX_OBS = 16

OBS_LIMIT = int((_CTX_TOKENS - _FIXED_TOKENS - _OUTPUT_RESERVE)
                * _CHARS_PER_TOKEN / _MAX_OBS)


def load_env(path=None):
    """`.env`를 환경변수로 읽는다(이미 있는 값은 덮지 않는다).

    찾는 순서: `agent2/.env` → 저장소 루트 `.env`. **먼저 찾은 하나만** 읽는다.
    agent2 것을 먼저 보는 이유: 2호기는 1호기 설정에 의존하지 않는다(참조 0 원칙).

    python-dotenv를 쓰지 않는 이유: 줄당 `KEY=VALUE` 파싱에 의존성을 더할 이유가 없다.
    `.env`는 .gitignore 대상 — 커밋 금지.
    """
    paths = [path] if path else [os.path.join(AGENT_DIR, ".env"),
                                 os.path.join(BASE_DIR, ".env")]
    got = {}
    for p in paths:
        if not p or not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                # ★ **인라인 주석을 자른다**(2026-08-10). `.env` 관례상 `KEY=VALUE # 설명`이
                #   흔한데 종전 파서는 `#` 이후를 값에 통째로 넣었다. 실제 피해:
                #     `LLM_MODEL=HCX-005   # 비우면 제공자 기본값…`
                #     → 모델명이 `HCX-005   # 비우면…`이 되어 **270×2 측정이 통째로 죽었다**
                #       (`HTTP 400 40080 model not found`, 512건 llm_error, 약 35분 낭비).
                #   따옴표로 감싼 값은 자르지 않는다 — 값 자체에 `#`이 들어갈 수 있고
                #   따옴표가 "여기까지가 값"이라는 명시적 표시다(python-dotenv와 같은 규칙).
                if v[:1] in ("'", '"') and len(v) > 1 and v[-1:] == v[:1]:
                    v = v[1:-1]
                else:
                    v = re.split(r"\s#", v, maxsplit=1)[0].strip().strip('"').strip("'")
                got[k] = v
                os.environ.setdefault(k, v)
        break
    return got
