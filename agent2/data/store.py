"""표 3종 로딩 — 기업 마스터 · 원문 문서 · **공시 목록 전체**."""
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
    """DART 공시 목록 **전체** 22,980건 — 원문 유무와 무관."""
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
            it = {k: (nfc(v).strip() if isinstance(v, str) else v) for k, v in it.items()}
            it["doc_group"] = group
            it["has_source"] = it.get("rcept_no") in have
            out.append(it)
    return tuple(out)

# ----------------------------------------------------------------- 조회


def _norm(s):
    return (nfc(str(s or "")).replace(" ", "").replace("·", "").replace("ㆍ", "")
            .replace(".", "").replace(",", "").lower())

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
    """그 **보고서 종류**가 보유한 최신 base_year."""
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
        print(f"  {name} {got} / 기대 {want}  {'OK' if got == want else '불일치'}")
    print("resolve('현대차') →", (resolve_corp("현대차") or {}).get("corp_name"))
    print("resolve('005930') →", (resolve_corp("005930") or {}).get("corp_name"))
    print("최신 사업연도 →", latest_fiscal_year(), "· 삼성전자", latest_fiscal_year("삼성전자"))
