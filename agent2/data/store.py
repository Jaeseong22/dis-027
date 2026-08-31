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


#: 한국 법인 상용구. 전치(`주식회사 카카오`)·후치(`네이버(주)`) 둘 다 쓰인다.
_KR_FORM = re.compile(r"주식회사|\(주\)|㈜|\(유\)|유한회사")


def _strip_kr_form(k):
    """정규화된 키에서 법인 상용구를 뗀다."""
    return _KR_FORM.sub("", k)


#: DART 서식이 문서 머리에 넣는 **회사가 스스로 밝힌 상호**.
#:     <COMPANY-NAME AREGCIK="00266961">네이버(주)</COMPANY-NAME>
_COMPANY_NAME = re.compile(r"<COMPANY-NAME\b[^>]*>(.*?)</COMPANY-NAME>", re.S | re.I)
_TAG_IN = re.compile(r"<[^>]+>")
_HEAD_BYTES = 4096


@lru_cache(maxsize=1)
def _filed_names():
    """{corp_name: (원문이 쓴 상호, …)} — `<COMPANY-NAME>` 전수.

    `네이버`·`포스코홀딩스`가 해소되지 않아 **코퍼스에 있는 회사를 "없는 기업"으로**
    답했다. DART 서식이 문서마다 구조화 필드로 상호를 싣고 `AREGCIK`(=corp_code)로
    universe 와 조인된다 — 매핑을 지어내지 않는다.

    전수 실측: XML 문서 3,147건 전부가 이 필드를 갖는다(4,616 − 거래소 HTML 1,469).
    조인 70/70사 · 고유 표기 186종 · 새로 붙는 키 31개 · 충돌 4축 전부 0건.
    정정본·과거 문서까지 보므로 구 사명도 잡힌다(삼성엔지니어링→삼성E&A ·
    대우조선해양→한화오션 · 엘아이지넥스원→LIG디펜스… · 현대중공업→HD현대중공업).
    ★ `OCI(주)`는 인적분할 전 법인명이라 `oci`가 OCI홀딩스로 간다. 신설 OCI는
      코퍼스 밖이므로 그쪽을 물으면 이 매핑이 다른 회사를 준다.

    비용: 문서당 앞 4KB만 읽어 전수 0.3초. 그래서 생성 파일로 빼지 않는다.
    """
    from agent2.data import source          # 순환 import 회피 — source 가 store 를 쓴다
    out = {}
    for row in manifest():
        try:
            fs = source.files(row)
        except Exception:
            continue
        for _fn, kind, path in fs:
            if kind != "xml":
                continue
            try:
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    head = fh.read(_HEAD_BYTES)
            except OSError:
                break
            for v in _COMPANY_NAME.findall(head):
                nm = nfc(_TAG_IN.sub("", v)).strip()
                if nm:
                    out.setdefault(row["corp_name"], set()).add(nm)
            break                            # 문서당 본문 한 개면 충분하다
    return {k: tuple(sorted(v)) for k, v in out.items()}


@lru_cache(maxsize=1)
def _corp_index():
    """법인명·통용명·영문명(+접미사 제거)·종목코드·법인코드·**원문 상호** → 마스터 행.

    ★ 순서가 곧 우선순위다 — universe 가 준 이름을 먼저 넣고 원문 상호를 뒤에 붙인다.
    """
    idx = {}
    for r in universe():
        for key in (r["corp_name"], r["listed_name"], r["corp_eng_name"],
                    _strip_eng_suffix(r["corp_eng_name"]),
                    r["stock_code"], r["corp_code"]):
            if not key:
                continue
            for k in (_norm(key), _strip_kr_form(_norm(key))):
                if len(k) >= 2:
                    idx.setdefault(k, r)
    for corp, names in _filed_names().items():
        r = idx.get(_norm(corp))
        if r is None:
            continue
        for nm in names:
            k = _strip_kr_form(_norm(nm))
            if len(k) >= 2:
                idx.setdefault(k, r)
    return idx


#: 접두 해소의 최소 길이. 1자로는 무엇이든 걸린다.
_PREFIX_MIN = 2


def _prefix_corp(k):
    """접두가 **정확히 한 회사**에만 걸릴 때만 해소한다. 둘 이상이면 None.

    모델이 회사명을 줄여 부른다 — `하나금융`·`우리금융`으로 부르면 코퍼스명
    `하나금융지주`·`우리금융지주`에 닿지 못한다. 모호하면 회사를 고르는 것이
    지어내는 것이므로 해소하지 않는다(전수 70사: 법인 접미를 뗀 형태 10개 중 9개가
    유일하고, `삼성`·`현대`·`LG`처럼 여러 개가 걸리는 접두는 되묻기 게이트가 받는다).
    """
    if len(k) < _PREFIX_MIN:
        return None
    hit = [c for c in universe() if _norm(c["corp_name"]).startswith(k)]
    return hit[0] if len(hit) == 1 else None


def resolve_corp(q):
    """기업 해소. 못 찾으면 None(억지로 채우지 않는다).

    질의 쪽에서도 법인 상용구를 뗀다 — `(주)이마트`·`주식회사 카카오`로 물어도 걸린다.
    정확 일치가 없으면 **유일한 접두**까지만 인정한다(`_prefix_corp`).
    """
    idx = _corp_index()
    k = _norm(q)
    return (idx.get(k) or idx.get(_strip_kr_form(k))
            or _prefix_corp(k) or _prefix_corp(_strip_kr_form(k)))


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


def latest_base_year(corp=None, subtype="annual", month=None):
    """그 **보고서 종류·그 분기**가 보유한 최신 base_year.

    ★ `month`를 안 보면 3분기가 죽는다. quarter 최신은 2026인데 코퍼스의 2026
      정기공시는 1분기뿐이라(quarter 9월은 2023~2025) 3분기 질의가 없는
      (2026, 9월)을 찾는다.
    """
    ys = [r.get("base_year")
          for r in docs(corp=corp, doc_subtype=subtype, base_month=month)
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
