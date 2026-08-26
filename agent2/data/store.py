"""표 3종 로딩 — 기업 마스터 · 원문 문서 · **공시 목록 전체**.

주최가 준 표는 셋인데 지금까지 둘만 썼다. 세 번째(`raw/*/*/list_*.json`)가 가장 크다.

    universe.csv     70행 × 17열      기업 마스터
    manifest.jsonl    4,204행 × 19필드  **원문이 수집된** 문서
    list_*.json ×280 22,980건 × 9필드  DART 공시 목록 **전체**

  → 22,980 − 4,204 = **18,776건은 목록에만 있고 원문이 없다.**
    원문이 없어도 "언제 무슨 공시를 냈다"는 사실은 확실하므로,
    존재·시점 질의에 답할 수 있고 "원문은 코퍼스에 없음"을 정직하게 고지할 수 있다.
    (예: 현금·현물배당결정 377 · 매출액또는손익구조30%변동 293 · 최대주주등소유주식변동 777)

`list_*.json`에만 있는 필드 2개:
    corp_cls  법인구분 Y(유가) 21,717 / K(코스닥) 1,263
    rm        DART 비고 — '정'(정정) 555 · '연'(연결) 279 · '유정' 1,225 …

주의: `corp_code`(8)·`stock_code`(6)는 문자열이다(선행 0 보존).
      macOS 파일시스템은 한글을 NFD로 담으므로 경로 비교 전 NFC 정규화한다.
"""
import csv
import glob
import json
import os
import re
import unicodedata
from functools import lru_cache

from agent2 import config


def nfc(s):
    return unicodedata.normalize("NFC", s) if isinstance(s, str) else s


@lru_cache(maxsize=1)
def universe():
    """기업 마스터 70행. dict 리스트, 전 값 문자열(선행 0 보존)."""
    with open(config.UNIVERSE_CSV, encoding="utf-8-sig", newline="") as fh:
        rows = [{k: (v or "").strip() for k, v in r.items()}
                for r in csv.DictReader(fh)]
    for r in rows:
        r["corp_name"] = nfc(r["corp_name"])
    return tuple(rows)


@lru_cache(maxsize=1)
def manifest():
    """원문이 수집된 문서 4,204행."""
    out = []
    with open(config.MANIFEST_JSONL, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            r["corp_name"] = nfc(r["corp_name"])
            r["file_path"] = nfc(r["file_path"])
            out.append(r)
    return tuple(out)


@lru_cache(maxsize=1)
def catalog():
    """DART 공시 목록 **전체** 22,980건 — 원문 유무와 무관.

    각 행에 `doc_group`(파일명에서 도출)과 `has_source`(원문 보유 여부)를 붙인다.
    `has_source=False`가 18,776건이다.
    """
    have = {r["rcept_no"] for r in manifest()}
    rev = {v: k for k, v in config.LIST_FILE.items()}
    out = []
    for p in sorted(glob.glob(os.path.join(config.RAW_DIR, "*", "*", "list_*.json"))):
        group = rev.get(os.path.basename(p))
        try:
            with open(p, encoding="utf-8") as fh:
                items = json.load(fh)
        except Exception:
            continue
        for it in items:
            # ★ `report_nm` 에 **오른쪽 공백 패딩**이 붙어 온다(전수 7,546/22,980 = 32.8%).
            #   예) "정기주주총회결과              "
            #   `filingtypes._norm` 은 이미 벗기지만 `facts.timeline` 은 그대로 내보내
            #   **모델 관측에 공백이 딸려 나갔다**(실측: 3/3행). 원천에서 벗긴다.
            #   전수 확인: 벗겨도 `filingtypes` 계수는 1,610쌍 전부 불변이다.
            it = {k: (nfc(v).strip() if isinstance(v, str) else v) for k, v in it.items()}
            it["doc_group"] = group
            it["has_source"] = it.get("rcept_no") in have
            out.append(it)
    return tuple(out)


# ----------------------------------------------------------------- 조회
def _norm(s):
    # ★ 마침표·콤마도 지운다(2026-08-26) — `JYP Ent.`(질의) ↔ `JYP Ent`(폴더명 제약으로
    #   마침표를 뗀 값 · `universe.note`가 그 사실을 적어 두었다)가 안 걸렸다.
    return (nfc(str(s or "")).replace(" ", "").replace("·", "").replace("ㆍ", "")
            .replace(".", "").replace(",", "").lower())


#: 영문 법인 접미사. **떼고도 색인한다**(2026-08-26).
#:
#:   `universe.corp_eng_name`을 통째로만 색인해서 `HYBE`가 안 걸렸다 —
#:   저장된 값이 `HYBE Co., Ltd.`이기 때문이다. 실측(통용명 21종): 이 하나로
#:   **HYBE · JYP Ent. · POSCO · SK TELECOM · KOREA ZINC 5건**이 새로 해소된다.
#:   ★ 매핑을 지어내지 않는다 — 주최가 준 `corp_eng_name`에서 **상용구만** 떼는 것이다.
#:   ★ 낱말 경계를 반드시 건다. 안 걸면 `KOREA ZINC INC` → **`KOREA Z`**가 된다
#:     (`inc`가 `ZINC`의 꼬리에 걸린다 · 실측으로 잡았다).
#:   ★ 전수 확인: 접미사를 뗀 키까지 넣어도 **두 회사가 같은 키를 갖는 경우 0건**이다.
_ENG_SUFFIX = re.compile(r"(?:[,\s]|\b)(?:co\.?,?\s*ltd\.?|corporation|corp\.?|inc\.?|"
                         r"company|limited|holdings?|group)\.?\s*$", re.I)


def _strip_eng_suffix(s):
    prev, t = None, str(s or "").strip()
    while t != prev and len(t) > 2:
        prev = t
        t = _ENG_SUFFIX.sub("", t).strip().strip(",").strip()
    return t


@lru_cache(maxsize=1)
def _corp_index():
    """법인명·통용명·영문명(+접미사 제거)·종목코드·법인코드 → 마스터 행."""
    idx = {}
    for r in universe():
        for key in (r["corp_name"], r["listed_name"], r["corp_eng_name"],
                    _strip_eng_suffix(r["corp_eng_name"]),
                    r["stock_code"], r["corp_code"]):
            if key and len(_norm(key)) >= 2:
                idx.setdefault(_norm(key), r)
    return idx


def resolve_corp(q):
    """기업 해소. 못 찾으면 None(억지로 채우지 않는다)."""
    return _corp_index().get(_norm(q))


def docs(corp=None, doc_group=None, doc_subtype=None, base_year=None,
         base_month=None, is_correction=None, source_only=True):
    """문서 조회. `source_only=False`면 원문 없는 목록(catalog)까지 포함한다."""
    rows = list(manifest()) if source_only else list(manifest()) + [
        r for r in catalog() if not r["has_source"]]
    if corp is not None:
        c = resolve_corp(corp)
        name = c["corp_name"] if c else nfc(str(corp))
        rows = [r for r in rows if r.get("corp_name") == name]
    for k, v in (("doc_group", doc_group), ("doc_subtype", doc_subtype),
                 ("base_year", base_year), ("base_month", base_month)):
        if v is not None:
            vs = v if isinstance(v, (list, tuple, set)) else (v,)
            rows = [r for r in rows if r.get(k) in vs]
    if is_correction is not None:
        rows = [r for r in rows if bool(r.get("is_correction")) == bool(is_correction)]
    return rows


def latest_fiscal_year(corp=None):
    """코퍼스가 보유한 최신 사업연도(annual). 상수로 박지 않고 데이터에서 얻는다."""
    ys = [r.get("base_year") for r in docs(corp=corp, doc_subtype="annual")
          if r.get("base_year")]
    return max(ys) if ys else None


def latest_base_year(corp=None, subtype="annual"):
    """그 **보고서 종류**가 보유한 최신 base_year.

    ★ `latest_fiscal_year`는 `annual`만 본다. 분기·반기는 사업보고서보다 **앞선다** —
      2026-08-23 전수: 정기공시 1,054건 중 annual 최신 **2025**, quarter 최신 **2026**
      (70사 전부 2026.03 분기보고서를 갖는다). 분기 질의에 annual 최신을 쓰면
      2026년 1분기를 물었는데 **2025년 1분기 표**를 준다.
    """
    ys = [r.get("base_year") for r in docs(corp=corp, doc_subtype=subtype)
          if r.get("base_year")]
    return max(ys) if ys else None


if __name__ == "__main__":
    u, m, c = universe(), manifest(), catalog()
    ns = sum(1 for r in c if not r["has_source"])
    print(f"universe {len(u)}행 × {len(u[0])}열")
    print(f"manifest {len(m)}건 (원문 보유)")
    print(f"catalog  {len(c)}건 (공시 목록 전체) · 원문 없음 {ns}건")
    for name, got, want in (("기업", len(u), config.EXPECT["corps"]),
                            ("원문", len(m), config.EXPECT["docs"]),
                            ("목록", len(c), config.EXPECT["catalog"])):
        print(f"  {name} {got} / 기대 {want}  {'OK' if got == want else '★불일치'}")
    print("resolve('현대차') →", (resolve_corp("현대차") or {}).get("corp_name"))
    print("resolve('005930') →", (resolve_corp("005930") or {}).get("corp_name"))
    print("최신 사업연도 →", latest_fiscal_year(), "· 삼성전자", latest_fiscal_year("삼성전자"))
