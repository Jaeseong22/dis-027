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

#: 영문 법인격 표기. ★ 단독형(`CO`·`LTD.`)과 `CO,.LTD`(쉼표가 앞에 오는 오탈자성 표기)를
#: 넣었다 — universe 실측에 그 셋이 다 있는데 종전 대안은 `co+ltd` 붙은 꼴만 받았다.
#: 그래서 `Samsung Electronics`(universe: `SAMSUNG ELECTRONICS CO,.LTD`)·
#: `Hyundai Motor`(`HYUNDAI MOTOR CO`)·`LG Energy Solution`(`LG ENERGY SOLUTION, LTD.`)이
#: 회사가 **스스로 밝힌 영문명**인데도 해소되지 않았다.
_ENG_SUFFIX = re.compile(r"(?:[,\s]|\b)(?:co[.,\s]*,?\s*ltd\.?|corporation|corp\.?|"
                         r"incorporation|inc\.?|company|co\.?|limited|ltd\.?|"
                         r"holdings?|group)\.?\s*$", re.I)


def _strip_eng_suffix(s):
    prev, t = None, str(s or "").strip()
    while t != prev and len(t) > 2:
        prev = t
        t = _ENG_SUFFIX.sub("", t).strip().strip(",").strip(".").strip()
    return t


#: 로마자 한 글자의 한글 음차. 사명 앞토막이 이니셜일 때만 쓴다.
_LETTER = {"a": "에이", "b": "비", "c": "씨", "d": "디", "e": "이", "f": "에프",
           "g": "지", "h": "에이치", "i": "아이", "j": "제이", "k": "케이", "l": "엘",
           "m": "엠", "n": "엔", "o": "오", "p": "피", "q": "큐", "r": "알",
           "s": "에스", "t": "티", "u": "유", "v": "브이", "w": "더블유",
           "x": "엑스", "y": "와이", "z": "지"}

#: 이니셜로 볼 로마자 토막 — 2~3자이고 **바로 뒤가 한글이거나 끝**일 때만.
#: 4자 이상은 이니셜이 아니라 단어다(`NAVER`·`POSCO`는 음차하면 안 된다).
_HEAD_ROMAN = re.compile(r"^([A-Za-z]{2,3})(?=[가-힣]|$)")

#: 영문명 **선두 토큰**이 2~3자 대문자면 그것이 이 회사의 이니셜이다.
_ENG_INITIAL = re.compile(r"^([A-Z]{2,3})(?=[\s.,]|$)")


def _roman_to_kr(name):
    """`LG이노텍` → `엘지이노텍`. 이니셜이 아니면 None."""
    m = _HEAD_ROMAN.match(str(name or ""))
    if not m:
        return None
    return "".join(_LETTER[c] for c in m.group(1).lower()) + name[m.end():]


def _initial_variants(corp_name, eng_name):
    """이니셜 ↔ 음차 상호 표기. **영문명이 밝힌 이니셜만** 쓴다.

    ★ 종전에는 한글 이름 앞 3음절을 음차로 **탐욕적으로** 되읽었는데, 그러면
      `와이지엔터테인먼트`에서 `엔`까지 이니셜로 읽어 `YGN터테인먼트`라는 기형 키가
      나왔고 `에스엠`은 2자 로마자 키 `sm`을 만들어 코퍼스 밖 문장(`SM 6 판매량`)을
      오탐했다. 어디까지가 이니셜인지는 추측할 것이 아니라 영문명이 이미 말해 준다.
    """
    m = _ENG_INITIAL.match(str(eng_name or "").strip())
    if not m:
        return ()
    ini = m.group(1)
    kr = "".join(_LETTER[c] for c in ini.lower())
    name = str(corp_name or "")
    if name.upper().startswith(ini):                 # `LG이노텍` → `엘지이노텍`
        out = kr + name[len(ini):]
    elif _norm(name).startswith(_norm(kr)):          # `엘에스일렉트릭` → `LS일렉트릭`
        out = ini + name[len(kr):]
    else:
        return ()
    # 로마자만 2자인 키는 만들지 않는다 — 영문 낱말 안에 그대로 박힌다(`sm`).
    k = _norm(out)
    if len(k) < 3 or (k.isascii() and len(k) < 3):
        return ()
    return (out,)


@lru_cache(maxsize=1)
def _aliases():
    """`data/aliases.csv` — 사람이 확인한 별칭만. 규칙으로 유도 못 하는 표기가 여기 온다.

    규칙(영문 접미사·음차)으로 만들 수 있는 것은 넣지 않는다. 여기 있는 것은
    구 사명(`기아자동차`→`기아`)·통용 약칭(`하이닉스`→`SK하이닉스`)처럼 **코퍼스
    데이터에서 유도할 수 없는** 매핑뿐이고, 각 행이 `note`에 근거를 달고 있다.

    ★ 종전에는 `_prefix_corp`(유일 접두)가 이 자리를 대신했는데, 같은 규칙이
      `카카`→카카오 · `이마`→이마트 · `셀트리`→셀트리온 같은 **잘린 오표기 14종**을
      함께 통과시켰다(70사 전수 실측). 정상 통용명과 잘림은 꼬리 길이로도
      출현빈도로도 갈리지 않는다 — `미래에셋|증권`(꼬리 1회)을 살리면
      `현대모|비스`(1회)도 살아난다. 그래서 규칙을 버리고 데이터로 옮겼다.
    """
    out = {}
    try:
        with open(config.ALIASES_CSV, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                a, c = nfc((r.get("alias") or "").strip()), nfc((r.get("corp_name") or "").strip())
                if a and c:
                    out[a] = c
    except OSError:
        return {}
    return out


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
def _index_parts():
    """(색인, **별칭에서만 온 키**). 뒤엣것을 따로 주는 이유는 질의 매칭 방식이 달라서다.

    `clarify.corps_in`은 조사가 붙어도 잡으려고 부분문자열로 본다. 그 방식은 정식
    사명에는 안전하지만 **약칭 별칭에는 위험하다** — 별칭이 형제사 이름의 앞토막이라
    다른 회사를 잡는다. 실측:
        포스코퓨처엠 → POSCO홀딩스 · 하나금융투자 → 하나금융지주 ·
        미래에셋생명 → 미래에셋증권 · 에코프로에이치엔 → 에코프로비엠 · KAIST → 한국항공우주
    그래서 별칭 키는 질의 쪽에서 **토큰 전체가 일치할 때만** 인정한다.
    `resolve_corp`(문자열 전체 일치)에는 이 구분이 필요 없다.
    """
    idx, before = _build_index()
    return idx, frozenset(k for k in idx if k not in before)


@lru_cache(maxsize=1)
def _corp_index():
    return _index_parts()[0]


def alias_keys():
    """별칭에서만 생긴 색인 키. 부분문자열로 쓰면 안 되는 것들이다."""
    return _index_parts()[1]


def _build_index():
    """법인명·통용명·영문명(+접미사 제거)·종목코드·법인코드·**원문 상호**·음차·별칭 → 마스터 행.

    ★ 순서가 곧 우선순위다 — universe 가 준 이름을 먼저 넣고 원문 상호·음차를 뒤에 붙인다.
    음차(`_initial_variants`)는 매핑을 지어내는 것이 아니라 **영문명이 밝힌 이니셜**을
    한글로 읽는 규칙이다. 어디까지가 이니셜인지는 영문명 선두 토큰이 결정한다.
    """
    idx = {}

    def put(key, row):
        for k in (_norm(key), _strip_kr_form(_norm(key))):
            if len(k) >= 2:
                idx.setdefault(k, row)

    for r in universe():
        for key in (r["corp_name"], r["listed_name"], r["corp_eng_name"],
                    _strip_eng_suffix(r["corp_eng_name"]),
                    r["stock_code"], r["corp_code"]):
            if key:
                put(key, r)
        for v in _initial_variants(r["corp_name"], r["corp_eng_name"]):
            put(v, r)
    for corp, names in _filed_names().items():
        r = idx.get(_norm(corp))
        if r is None:
            continue
        for nm in names:
            put(nm, r)
            v = _roman_to_kr(nm)                    # 원문 상호가 이니셜로 시작하면 음차도
            if v and len(_norm(v)) >= 3:
                put(v, r)
    before = frozenset(idx)                        # 별칭 이전의 키 — 어느 것이 별칭발인지
    for alias, corp in _aliases().items():          # 사람이 확인한 매핑을 마지막에
        r = idx.get(_norm(corp))
        if r is not None:
            put(alias, r)
    return idx, before


def resolve_corp(q):
    """기업 해소. **정확 일치만** 인정한다. 못 찾으면 None(억지로 채우지 않는다).

    질의 쪽에서도 법인 상용구를 뗀다 — `(주)이마트`·`주식회사 카카오`로 물어도 걸린다.

    ★ 종전에 있던 `_prefix_corp`(유일 접두)를 뺐다. 잘린 이름을 회사로 채워 주는데,
      그것이 곧 **오표기에 답하는 것**이다. 70사 전수 실측으로 `카카`→카카오 ·
      `이마`→이마트 · `셀트리`→셀트리온 · `SK하이닉`→SK하이닉스 등 14종이 통과했다.
      규칙이 살리던 정상 통용명 10종(`하나금융`·`미래에셋`·`삼성바이오`…)은
      `data/aliases.csv`로 옮겼다 — 근거가 행마다 남고 오표기는 따라오지 않는다.
    """
    idx = _corp_index()
    k = _norm(q)
    return idx.get(k) or idx.get(_strip_kr_form(k))


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
