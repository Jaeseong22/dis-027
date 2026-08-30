"""70개사 순위 배치 — 질의 시점에 70사를 훑지 않는다.

`get_financials`를 70사에 돌리면 15.6초 · 상주 926MB이고 **두 번째도 빨라지지 않는다**
(이 경로엔 lru_cache가 없어 매번 원문을 다시 판다). 서버 메모리가 4GB라 그 스파이크를
감당할 수 없어 `filings.py`와 같은 방식으로 예열 때 한 번 계산해 디스크에 둔다.

담는 것 — 금액 35종(**원 단위로 정규화**) · 비율 8종 · 증감률 35종 · 직원수 · 시가총액.

순위가 거짓말하는 지점(전부 실측):
  ① 단위가 섞인다(원 42사 · 백만원 20사 · 천원 8사). 환산 없이 정렬하면 1위가 뒤집힌다.
  ② 결측은 지표마다 다르고 뜻도 다르다 — 0으로 채우지 말고 뺀 회사를 이름으로 밝힌다.
  ③ 매출액 순위에 `영업수익`이 섞인다(신한지주·하나금융지주). 행마다 원문 라벨을 담는다.
  ④ 지주회사의 직원수는 그룹 직원수가 아니다(KB금융 144명).

실행:
    python3 -m agent2.data.rankings            # 배치 (약 15초)
    python3 -m agent2.data.rankings --check
"""
import json
import os
import re
import time

from agent2 import config

OUT_DIR = os.path.join(config.AGENT_DIR, ".cache", "rankings")

#: 원 단위 환산 배수. 여기 없는 단위가 나오면 담지 않는다(0을 만들지 않는다).
SCALE = {"원": 1, "천원": 10 ** 3, "백만원": 10 ** 6, "십억원": 10 ** 9, "억원": 10 ** 8}

#: 비율 묶음 — `get_financials`가 이미 계산해 준다. 우리가 다시 나누지 않는다(§7).
RATIO_GROUPS = ("재무구조_비율", "매출액_대비_비율")

#: 금액이 아니라 그 자체로 단위가 있는 값. 원 환산을 하지 않는다.
_PER_SHARE = ("eps_basic", "eps_diluted")

_NUM = re.compile(r"^-?[\d,]+(?:\.\d+)?$")


def path(year):
    return os.path.join(OUT_DIR, f"fy{year}.json")


def _amount(text):
    """`333,605,938 백만원` → (333605938000000, '백만원'). 못 읽으면 (None, None)."""
    s = str(text or "").strip()
    if " " not in s:
        return (None, None)
    num, unit = s.rsplit(" ", 1)
    if unit not in SCALE or not _NUM.match(num.strip()):
        return (None, None)
    body = num.replace(",", "")
    sign = -1 if body.startswith("-") else 1
    body = body.lstrip("-")
    if "." in body:                       # 소수는 이 코퍼스의 금액 표기에 없다 — 그래도 지킨다
        return (int(sign * float(body) * SCALE[unit]), unit)
    return (sign * int(body) * SCALE[unit], unit)


def _percent(text):
    """`29.9%` → 29.9. 못 읽으면 None."""
    s = str(text or "").strip()
    if not s.endswith("%"):
        return None
    try:
        return float(s[:-1].replace(",", ""))
    except ValueError:
        return None


def _headcount(corp, year):
    """직원 등의 현황 `합 계` 행의 직원수. 70/70사에서 열이 유일하게 잡힌다(실측).

    열 이름은 `직 원 수 합 계`·`합 계`로 갈린다(공백을 지우고 본다).
    이 값은 **그 법인**의 직원수다 — 지주회사면 그룹 전체가 아니다.
    """
    from agent2.tools import doctables as _doctables
    v = _doctables.values(corp, "employees", year) or {}
    flat = lambda s: re.sub(r"\s", "", str(s))
    for row in (v.get("rows") or []):
        if flat(row.get("label")) != "합계":
            continue
        cells = row.get("values") or {}
        cols = [c for c in cells if flat(c) in ("직원수합계", "합계", "직원수계")]
        if len(cols) != 1:
            return (None, None)           # 열이 모호하면 담지 않는다
        raw = str(cells[cols[0]]).strip()
        if not _NUM.match(raw):
            return (None, None)
        return (int(raw.replace(",", "")), f"{raw}명")
    return (None, None)


def build(year=2025, verbose=True):
    """70사 전수를 훑어 순위 배치를 만든다. (회사 수, 소요초)."""
    from agent2.data import store
    from agent2.tools import __init__ as _unused          # noqa: F401  (레지스트리 초기화)
    import agent2.tools as T

    t0 = time.time()
    rows = {}
    for r in store.universe():
        corp = r["corp_name"]
        d = T.get_financials(corp=corp, year=year)
        metrics = {}
        srcs = d.get("sources") or {}
        for k, v in (d.get("values") or {}).items():
            if k in _PER_SHARE:
                won, unit = (None, None)
                pct = None
                num = re.sub(r"[^\d.-]", "", str(v).rsplit(" ", 1)[0])
                val = float(num) if num not in ("", "-", ".") else None
                if val is None:
                    continue
                metrics[k] = {"값": val, "표기": str(v), "종류": "주당",
                              "라벨": srcs.get(k, "")}
                continue
            won, unit = _amount(v)
            if won is None:
                continue
            metrics[k] = {"원": won, "표기": str(v), "단위": unit, "종류": "금액",
                          "라벨": srcs.get(k, "")}
        for group in RATIO_GROUPS:
            for k, v in (d.get(group) or {}).items():
                pct = _percent(v)
                if pct is None:
                    continue
                metrics[k] = {"값": pct, "표기": str(v), "종류": "비율", "라벨": k}
        # ── 전년 대비 증감률 — `series`에서 코드가 계산한다(모델에 나눗셈을 시키지 않는다).
        #    ★ **기준연도 값이 0 이하면 내지 않는다.** 적자→흑자 전환의 "증가율"은
        #      뜻이 없다(부호가 뒤집혀 −300% 같은 값이 나온다). 정직한 부재가 낫다.
        for k, ser in (d.get("series") or {}).items():
            ys = sorted(y for y in ser if str(y).isdigit())
            if len(ys) < 2:
                continue
            cur, prev = _amount(ser[ys[-1]])[0], _amount(ser[ys[-2]])[0]
            if cur is None or prev is None or prev <= 0:
                continue
            metrics[f"{k}_growth"] = {
                "값": round((cur - prev) / prev * 100, 1),
                "표기": f"{(cur - prev) / prev * 100:+.1f}%",
                "종류": "비율",
                "라벨": f"{ys[-2]}→{ys[-1]} 전년 대비 증감률"}

        head, head_text = _headcount(corp, year)
        if head is not None:
            metrics["employees"] = {"값": head, "표기": head_text, "종류": "인원",
                                    "라벨": "직원 등의 현황 · 합 계"}
        cap = r.get("market_cap")           # universe.csv 열 이름 · 단위 억원
        if cap not in (None, ""):
            try:
                metrics["market_cap"] = {"원": int(str(cap).replace(",", "")) * 10 ** 8,
                                         "표기": f"{int(str(cap).replace(',', '')):,}억원",
                                         "단위": "억원", "종류": "금액",
                                         "라벨": "universe.csv 시가총액(2026-07-24 조회)"}
            except ValueError:
                pass
        rows[corp] = {"출처": d.get("출처", ""), "구조": d.get("structure", ""),
                      "섹터": r.get("sector", ""), "업종": r.get("industry", ""),
                      "지표": metrics}

    payload = {"year": year, "scope": "연결", "built": time.strftime("%Y-%m-%d %H:%M:%S"),
               "corps": len(rows), "rows": rows}
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = path(year) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(tmp, path(year))
    el = time.time() - t0
    if verbose:
        n = len({k for v in rows.values() for k in v["지표"]})
        size = os.path.getsize(path(year)) / 1000
        print(f"기업 {len(rows)}사 · 지표 {n}종 · {size:.0f}KB · {el:.0f}초")
    return len(rows), el


def load(year=2025):
    """배치를 읽는다. 없으면 None — **여기서 즉석 계산하지 않는다**(15초·900MB)."""
    p = path(year)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def years():
    """배치가 있는 연도."""
    if not os.path.isdir(OUT_DIR):
        return []
    return sorted(int(m.group(1)) for m in
                  (re.match(r"fy(\d{4})\.json$", f) for f in os.listdir(OUT_DIR)) if m)


if __name__ == "__main__":
    import sys

    if "--check" in sys.argv:
        for y in years() or [None]:
            d = load(y) if y else None
            print(f"fy{y}: 기업 {d['corps']}사 · 생성 {d['built']}" if d else "배치 없음")
        raise SystemExit(0)
    yr = next((int(a) for a in sys.argv[1:] if a.isdigit()), 2025)
    build(yr)
