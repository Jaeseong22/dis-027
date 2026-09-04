"""경로·상수."""
import os
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS_DIR = os.environ.get("CORPUS_DIR") or os.path.join(BASE_DIR, "3.공시", "corpus")
AGENT_DIR = os.path.dirname(os.path.abspath(__file__))

RAW_DIR = os.path.join(CORPUS_DIR, "raw")
UNIVERSE_CSV = os.path.join(CORPUS_DIR, "universe.csv")
MANIFEST_JSONL = os.path.join(CORPUS_DIR, "manifest.jsonl")

#: 사람이 확인한 기업 별칭. 코퍼스가 아니라 **저장소** 자산이다(주최 제공물이 아니다).
ALIASES_CSV = os.path.join(AGENT_DIR, "data", "aliases.csv")

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
_CTX_TOKENS = 128_000          # CLOVA Studio 모델 사양
_CHARS_PER_TOKEN = 2.0         # 한국어 실측
_FIXED_TOKENS = 3_034          # SYSTEM + 도구 스키마(실측)
_OUTPUT_RESERVE = 8_000        # 출력 여유
_MAX_OBS = 16

OBS_LIMIT = int((_CTX_TOKENS - _FIXED_TOKENS - _OUTPUT_RESERVE)
                * _CHARS_PER_TOKEN / _MAX_OBS)


def load_env(path=None):
    """`.env`를 환경변수로 읽는다(이미 있는 값은 덮지 않는다)."""
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
                if v[:1] in ("'", '"') and len(v) > 1 and v[-1:] == v[:1]:
                    v = v[1:-1]
                else:
                    v = re.split(r"\s#", v, maxsplit=1)[0].strip().strip('"').strip("'")
                got[k] = v
                os.environ.setdefault(k, v)
        break
    return got
