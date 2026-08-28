"""액션 스페이스 — 도구 12개."""
import json as _json
import os as _os
import re as _re

from agent2 import config as _config

from agent2.tools import compute as _compute
from agent2.tools import corrections as _corrections
from agent2.tools import doctables as _doctables
from agent2.tools import docsections as _docsections
from agent2.tools import concepts as _K
from agent2.tools import facts as _facts
from agent2.tools import finance as _finance
from agent2.data import store as _store
from agent2.data.parse import num as _cellnum
from agent2.tools import search as _search
from agent2.tools.registry import (MAX_TOOLS, all_tools, code_prompt,  # noqa: F401
                                   get, json_schemas, scenario_coverage, tool)

#: 정본 섹션 1종에 줄 크기(자). 관측 상한의 **절반**까지만 산문에 준다 —
#: 나머지 절반은 정본표·재무표준값 몫이다(둘이 함께 실리는 질의가 흔하다).
_SNIPPET = int(_os.environ.get("A2_SNIPPET") or 3000)
_KMAX = int(_os.environ.get("A2_K") or 4)

_SEC_LIMIT = _config.OBS_LIMIT // 2

SCENARIOS = tuple(f"1-{i}" for i in range(1, 19)) + tuple(f"2-{i}" for i in range(1, 10))


@tool("1-5", "1-8")


def resolve_company(name: str) -> dict:
    """기업명·종목코드·영문명을 코퍼스 70개사 중 하나로 해소하고 기본 정보를 돌려준다.

    코퍼스 밖이면 None이다. 비슷한 회사를 억지로 고르지 않는다.
    예: "현대차"→현대자동차 · "005930"→삼성전자 · "현대차증권"→None

    인자:
        name: 기업명·통용명·영문명·종목코드 중 아무거나

    반환:
        법인명·종목코드·시장·업종·섹터·상장일·시가총액(억원)·문서 건수. 못 찾으면 None
    """
    return _facts.company(name)


@tool("1-5", "1-6")


def sector_ranking(sector: str = None,
                   industry: "IT|건강관리|경기관련소비재|금융|산업재|소재|"
                             "커뮤니케이션서비스|필수소비재" = None,
                   top: int = 10) -> list:
    """섹터·업종 안에서 시가총액 순위를 매긴다. 섹터를 안 주면 70개사 전체를 매긴다.

    시가총액은 주최가 준 universe.csv(2026-07-24 조회) 값이다.

    인자:
        sector: 섹터명 일부. 예 "반도체" "금융"
        industry: 업종 대분류 8종 중 하나
        top: 상위 몇 개까지

    반환:
        [{rank, corp_name, sector, market_cap_eok}] 시가총액 내림차순
    """
    return _facts.sector_ranking(sector=sector, industry=industry, top=top)


@tool("2-1", "2-8")


def sector_peers(corp: str) -> list:
    """같은 섹터의 기업 목록을 돌려준다. 경쟁사·섹터 비교의 대상을 정할 때 쓴다.

    인자:
        corp: 기준 기업명 **또는 섹터명**(부분일치 — "반도체" → "반도체·전자부품")

    반환:
        섹터 기업명 리스트(기업명을 준 경우 자기 자신 제외)
    """
    return _facts.peers(corp)

_CANON_FALLBACK = {"capex": "신규시설투자등"}

#: 자금조달 질의 — 유형이 여럿이라 한 번에 세어 준다.
_FUNDING_Q = _re.compile(r"자금조달|유형별")

_FUNDING_TYPES = ("유상증자결정", "무상증자결정", "전환사채발행결정",
                  "신주인수권부사채발행결정", "교환사채발행결정")


def _no_corp(corp):
    """코퍼스 70사로 해소되면 (법인명, None), 아니면 (None, 알림 dict)."""
    c = _facts.company(corp)
    if c:
        return c["corp_name"], None
    return None, {"status": "코퍼스에 없는 기업", "corp": corp,
                  "note": f"'{corp}'는 제공 코퍼스(70개사)의 기업명·통용명·영문명·종목코드 "
                          "어느 것으로도 해소되지 않습니다. **이 회사가 공시를 내지 않았다는 "
                          "뜻이 아닙니다** — 이름이 다를 수 있습니다. `resolve_company`로 "
                          "정확한 이름을 확인한 뒤 다시 부르십시오. 코퍼스 밖 회사라면 "
                          "'제공된 공시 코퍼스에 없는 기업입니다'라고 답하십시오."}


def _funding_block(corp, period):
    """자금조달 유형별 건수. **0건도 그대로 준다** — 없다는 것이 답이기 때문이다."""
    from agent2.tools import filingtypes as _ft
    out = {}
    for tid in _FUNDING_TYPES:
        c = _ft.count(corp, tid, period)
        out[c["유형"]] = (f"{c['원본']}건" if c["원본"] else
                         "0건 (이 기간에 해당 공시가 없습니다)")
    out["기간"] = f"{period[0]}~{period[1]}"
    out["note"] = (f"이 계수는 **{period[0]}~{period[1]}** 기간의 자금조달 유형별 "
                   "**원본** 건수입니다(정정본은 같은 사건의 재공시). "
                   "질문이 다른 기간을 말하면 start·end를 주고 다시 부르십시오. "
                   "0건인 유형은 '없다'가 답입니다 — 다른 유형으로 대신 답하지 마십시오.")
    return out


@tool("1-10", "1-11")


def list_filings(corp: str, keyword: str, year: int = None,
                 start: str = None, end: str = None, limit: int = 20) -> dict:
    """그 회사의 공시 이력을 찾는다. 원문이 수집되지 않은 공시까지 포함한다.

    주최 공시 목록 22,980건 중 원문이 있는 건 4,204건뿐이다. 나머지도 언제 무슨 공시를
    냈는지는 확실하다. has_source가 False면 원문이 없다는 뜻이다.
    예: keyword="배당" → 현금·현물배당결정 13건(전부 원문 없음)

    ★ **"몇 번 있었어?"는 이 도구가 세어 줍니다.** keyword를 주면 `계수` 필드에
      원본·정정·전체 건수가 함께 옵니다. 답은 **원본** 건수입니다(정정본은 같은 사건의
      재공시라 함께 세면 부풀어 오릅니다). 직접 세지 마십시오.

    ★ **keyword는 필수입니다.** 없으면 계수도 정형공시 필드도 붙지 않아 목록만 나갑니다.
      실측(2026-08-14, 평가셋 r3·r4 = 230건):
          keyword 있음  O 106 · X 48  (69%)
          keyword 없음  O   7 · X 20  (26%)
      `start`/`end`를 노출한 뒤 모델이 기간에 주의를 쏟아 keyword를 빠뜨리는 비율이
      늘었고(90% → 84%), 그 호출이 26%로 떨어졌다. 그래서 필수로 바꿨다.

    ★ **기간이 여러 해에 걸치면 start·end를 쓰십시오.** year는 **한 해만** 거릅니다.
      "2024년~2026년 3월"이면 start="2024-01-01", end="2026-03-31"입니다.
      year=2024를 주면 2024년만 세어 답이 작아집니다.

    ★ **자금조달을 유형별로 물으면 keyword="자금조달"을 쓰십시오.** 유상증자·무상증자·
      전환사채(CB)·신주인수권부사채(BW)·교환사채(EB) **5종을 한 번에** 세어 줍니다.
      유형마다 따로 부르지 마십시오. 0건인 유형은 '없다'가 답입니다.

    ★ **계약 해지는 keyword="계약 해지"**로 찾습니다. 해지 공시의 `관련공시` 필드가
      **원계약을 가리키고**, 그 원계약이 코퍼스에 있으면 접수번호까지 함께 옵니다.

    인자:
        corp: 기업명
        keyword: **필수.** 보고서명에 포함될 문자열. 예 "배당" "합병" "자기주식처분"
                 (증자·전환사채 **이력**은 find_tables로 주식의 총수 표를 보십시오)
        year: 접수 연도 **한 해**만 볼 때
        start: 기간 시작일 "YYYY-MM-DD". 여러 해에 걸친 질의에 쓰십시오
        end: 기간 종료일 "YYYY-MM-DD"
        limit: 최대 건수

    반환:
        {계수, 정형공시…, rows, total, without_source}
        계수: {유형, 기간, 원본, 정정, 전체, 최근} — "몇 번"의 답은 **원본**
    """
    name, _miss = _no_corp(corp)
    if _miss:
        return _miss
    out = _facts.timeline(corp, keyword=keyword, year=year, limit=limit,
                          start=start, end=end)
    if keyword:
        from agent2.tools import filingtypes as _ft
        _per = None
        if start or end:
            _qp = _ft.QUESTION_PERIOD
            _per = (_re.sub(r"[^0-9]", "", str(start or _qp[0]))[:8].ljust(8, "0"),
                    _re.sub(r"[^0-9]", "", str(end or _qp[1]))[:8].ljust(8, "9"))
        def _norm(x):
            return _re.sub(r"[\s·ㆍ()\-]", "", str(x))

        def _hit(qs, name_):
            pos = 0
            for tok in qs:
                i = name_.find(tok, pos)
                if i < 0:
                    return False
                pos = i + len(tok)
            return True

        _toks = [_norm(t) for t in _re.split(r"[\s·ㆍ]+", keyword) if _norm(t)]
        cnt = ([] if _norm(keyword) in ("정정신고", "정정공시", "정정")
               else [c for c in ((_ft.count(name, t.id, _per) if _per else _ft.count(name, t.id))
                                 for t in _ft.TYPES)
                     if _toks and _hit(_toks, _norm(c["유형"]))])
        if cnt:
            c0 = max(cnt, key=lambda c: c["전체"])
            _rows = out.get("rows") or []
            _forms, _seen = [], set()
            for _r in _rows:
                _nm = str(_r.get("report_nm") or "").strip()
                if _nm and _nm not in _seen:
                    _seen.add(_nm)
                    _forms.append(_nm)
            _note = ("'몇 번'의 답은 위 `답`입니다(원본 건수). "
                     f"아래 `rows` 목록은 {len(_rows)}건이지만 **세지 마십시오** — "
                     "정정본과 다른 서식(종속회사·자회사 공시 등)이 섞여 있어 답과 다릅니다.")
            if c0["원본"] == 0:
                _note = (f"이 기간에 해당하는 공시는 **0건**입니다 — **답은 0입니다.** "
                         f"아래 `rows` {len(_rows)}건은 서식이 다르므로 세지 마십시오"
                         + (f" (목록 서식: {' · '.join(_forms[:3])})." if _forms else "."))
            elif _forms and len(_rows) != c0["원본"]:
                _note += f" 목록 서식: {' · '.join(_forms[:3])}."
            out = {"계수": {"답": c0["원본"],
                          "유형": c0["유형"], "기간": c0["기간"],
                          "note": _note,
                          "참고": {"원본": c0["원본"], "정정": c0["정정"],
                                 "전체": c0["전체"], "최근": c0["최근"]}}, **out}
        elif _FUNDING_Q.search(keyword):
            out = {"자금조달 유형별": _funding_block(name, _per or _ft.PERIOD), **out}
        elif "정정" in keyword:
            n = sum(1 for m in _store.manifest()
                    if m["corp_name"] == name and m["is_correction"]
                    and "20240101" <= str(m["rcept_dt"]) <= "20260331")
            out = {"계수": {"답": n, "유형": "정정신고(전 유형)",
                          "기간": "20240101~20260331",
                          "note": "제목에 [기재정정]이 붙은 공시 전 유형 합계입니다."},
                   **out}
    # 증자·감자 같은 **이력**은 공시 목록이 아니라 사업보고서 표에 있다.
    _rr = (out.get("rows") or [])
    if _rr and "출처" not in out:
        _latest = max(_rr, key=lambda x: str(x.get("rcept_dt") or ""))
        out = {**out, "출처": _doctables._cite(_latest),
               "출처_note": "위 `출처`는 **목록에서 가장 최근 건**입니다. 다른 행을 근거로 "
                          "쓰면 그 행의 `rcept_no`를 `접수번호 <14자리>` 형식으로 적으십시오."}

    head = {}
    if not out.get("계수") or out["계수"].get("답") == 0:
        for tid in _doctables.match(keyword or "")[:2]:
            v = _doctables.values(name, tid, year)
            if v:
                head[_doctables.BY_ID[tid].name] = _canon(v, query=keyword)
    if keyword:
        for rec in _filing_records(name, keyword, limit=2):
            head[f"정형공시 · {rec['서식'][:28]}"] = rec
    if out.get("계수"):
        cnt = out.pop("계수")
        return {"계수": cnt, **head, **out}
    if not out.get("total") and "계수" not in (head or {}):
        out = {**out,
               "※": f"'{keyword}'가 **공시 목록의 서식명**에 걸리지 않았습니다. 공시 목록은 "
                    "'언제 무슨 서식을 냈나'만 담고 본문 값은 없습니다. 찾는 내용이 "
                    "사업보고서 본문·주석에 있을 수 있으니 "
                    f"`find_tables(corp='{corp}', query='...')` 또는 "
                    f"`find_sections(corp='{corp}', query='...')`를 **먼저 확인하십시오.** "
                    "거기에도 없으면 '공시에 기재되지 않음'이라고 답하는 것이 정답입니다."}
    return {**head, **out} if head else out

#: 한글 큰 수 단위 — **4자리씩** 끊는다. 표기의 3자리 콤마와 주기가 어긋나는 것이
#: 오독의 원인이다(588,700,000,000 = 5,887억).
_KOR_UNITS = ((10 ** 12, "조"), (10 ** 8, "억"), (10 ** 4, "만"))


def _won_scale(key, val):
    """`(원)` 금액 필드에 **한글 단위를 병기**한다. 원 단위 표기가 앞이다."""
    s = str(val).strip()
    if "(원)" not in key or not _re.fullmatch(r"-?[\d,]+", s):
        return val
    try:
        n = int(s.replace(",", ""))
    except ValueError:
        return val
    if abs(n) < 10 ** 8:               # 1억 미만은 그대로 읽힌다
        return val
    neg, n2, out = n < 0, abs(n), []
    for unit, name in _KOR_UNITS:
        q, n2 = divmod(n2, unit)
        if q:
            out.append(f"{q:,}{name}")
    if n2:
        out.append(f"{n2:,}")
    return f"{s}원 (= {'-' if neg else ''}{' '.join(out)}원)"

#: 표 단위 문자열 → 원 단위 배수. `백만원, %` 처럼 복합 표기라 **접두로** 찾는다.
_AMT_MULT = (("조원", 10 ** 12), ("십억원", 10 ** 9), ("억원", 10 ** 8),
             ("백만원", 10 ** 6), ("천원", 10 ** 3), ("원", 1))


def _amt_mult(unit):
    """그 표 단위의 원 배수. 금액 단위가 아니면 None."""
    u = str(unit or "").replace(" ", "")
    for name, mult in _AMT_MULT:
        if u.startswith(name):
            return name, mult
    return None


def _kor_amount(n):
    """정수 원 금액 → `52조 6,511억원`. 반올림하지 않는다(`_won_scale`과 같은 규율)."""
    neg, n2, out = n < 0, abs(int(n)), []
    for unit, name in _KOR_UNITS:
        q, n2 = divmod(n2, unit)
        if q:
            out.append(f"{q:,}{name}")
    if n2:
        out.append(f"{n2:,}")
    return ("-" if neg else "") + (" ".join(out) or "0") + "원"


def _canon_norm(v, rows):
    """비교 관측에 원 단위 환산을 병기한다 — 회사마다 표 단위가 다르다."""
    got = _amt_mult(v.get("unit"))
    if not got:
        return None
    _name, mult = got
    out = {}
    for r in rows[:8]:
        for k, x in (r.get("values") or {}).items():
            t = str(x or "").strip().replace(",", "")
            if not _re.fullmatch(r"-?\d+(?:\.\d+)?", t):
                continue
            won = int(round(float(t) * mult))
            if abs(won) < 10 ** 8:          # 1억 미만은 그대로 읽힌다
                continue
            out[f"{r['label']} · {k}"] = _kor_amount(won)
            break                            # 행마다 첫 금액 열 하나면 충분하다
    return out or None

#: 정형공시 레코드에서 잘라낼 필드 — 답과 무관한 서식 상용구.
#: 전건에 있어 라벨 어휘로는 안 걸러지고, 관측만 먹는다(문서당 10~15개).
_FILING_DROP = ("금융위원회", "한국거래소", "회 사 명", "회사명", "대 표 이 사",
                "대표이사", "본 점 소 재 지", "본점소재지", "작 성 책 임 자",
                "작성책임자", "전  화", "전화", "홈페이지")


def _filing_records(corp, keyword, limit=2, budget=1600):
    """`keyword`에 걸리는 **서식마다 최신 1건**. 최대 `limit`종. **접수일 최신순**."""
    out, seen = [], set()
    for form in _filing_forms(corp, keyword):
        if len(out) >= limit:
            break
        rec = _filing_record(corp, keyword, form=form, budget=budget)
        if rec and rec["출처"] not in seen:
            seen.add(rec["출처"])
            out.append(rec)
    # 접수일은 `출처`의 `(YYYYMMDD)`에 있다. 못 읽으면 뒤로 보낸다(순서를 지어내지 않는다).
    def _dt(rec):
        m = _re.search(r"\((\d{8})\)", rec.get("출처") or "")
        return m.group(1) if m else "00000000"
    out.sort(key=_dt, reverse=True)
    if len(out) > 1:
        out[0] = {"※": f"이 서식이 가장 최근입니다(접수일 {_dt(out[0])}). "
                       "'가장 최근'을 묻는 질의는 이 레코드로 답하십시오.", **out[0]}
    return out


def _filing_forms(corp, keyword):
    """`keyword`에 걸리는 서식명. **질의어와 길이가 가까운 순** — `상장`을 물으면
    `…상장`이 `…상장폐지결정`보다 앞선다."""
    try:
        from agent2.data import filings as _filings
    except Exception:
        return []
    kw = _re.sub(r"[\s·ㆍ()]", "", keyword)
    forms = {r["form"] for r in _filings.load()
             if r["corp_name"] == corp
             and kw in _re.sub(r"[\s·ㆍ()]", "", r["form"])}
    return sorted(forms, key=lambda f: (len(_re.sub(r"[\s·ㆍ()]", "", f)), f))

#: **경쟁 필드에 그게 무엇인지 붙인다.** 질의 어휘와 겹치는데 답이 아닌 필드가 있다 —
_LINK_DATE = re_link = _re.compile(r"(\d{4})[-.년]\s*(\d{1,2})[-.월]\s*(\d{1,2})")


def _linked_filings(corp, text):
    """`관련공시` 문자열이 가리키는 공시를 **접수번호까지** 해소한다."""
    hits, gone = [], []
    rows = None
    for y, mo, d in _LINK_DATE.findall(str(text or ""))[:3]:
        key = f"{y}{int(mo):02d}{int(d):02d}"
        if rows is None:
            rows = _facts.filings(corp, with_listed_only=True)
        same = [r for r in rows if str(r.get("rcept_dt") or "") == key]
        if same:
            r = same[0]
            hits.append(f"{r['report_nm']} ({key}), 접수번호 {r['rcept_no']}")
        else:
            gone.append(key)
    out = ""
    if hits:
        out += " ※ 가리키는 공시가 코퍼스에 있습니다 — " + " · ".join(hits) + "."
    if gone:
        out += (f" ※ {', '.join(gone)} 공시는 **코퍼스 기간 밖**이라 원문이 없습니다. "
                "확인된 범위까지만 답하고 그 사실을 밝히십시오.")
    return out

_FIELD_NOTES = (
    (("보유주식등의 수", "이번"),
     " ※ 보고자+특별관계자 **합산**입니다. '보고자 본인'을 묻는 질의의 답이 아닙니다"
     "(명부표의 `보고자 보유주식등의 의결권있는주식`을 쓰십시오)."),
)


def _field_note(k, v, corp=None):
    """필드 값에 **무엇인지** 주석을 붙인다. 해당 없으면 그대로 돌려준다."""
    for needles, note in _FIELD_NOTES:
        if all(n in k for n in needles):
            return f"{v}{note}"
    if corp and ("관련공시" in k or "기타 투자판단" in k):
        return f"{v}{_linked_filings(corp, v)}"
    return v


def _filing_record(corp, keyword, form=None, budget=1600):
    """그 회사의 `keyword`(또는 지정 `form`) 서식 **최신 1건**을 필드 dict로."""
    try:
        from agent2.data import filings as _filings
    except Exception:
        return None
    recs = _filings.load()
    if not recs:
        return None
    kw = _re.sub(r"[\s·ㆍ]", "", keyword)
    hit = [r for r in recs
           if r["corp_name"] == corp
           and (r["form"] == form if form else kw in _re.sub(r"[\s·ㆍ]", "", r["form"]))]
    hit = [r for r in hit if not r["is_correction"] or r.get("orphan")]
    if not hit:
        return None
    def _event_dt(rec):
        for k, v in rec["fields"].items():
            if "계약" in k and "일자" in k:
                d = _re.sub(r"[^0-9]", "", str(v))[:8]
                if len(d) == 8:
                    return d
        return rec["rcept_dt"]
    r = max(hit, key=_event_dt)
    fields, used = {}, 0
    for k, v in r["fields"].items():
        if any(d in k for d in _FILING_DROP):
            continue
        if str(v).strip() in ("-", "–", "—"):
            v = "0 (원문 '-' — 해당 항목 없음)"
        size = len(k) + len(str(v)) + 4
        if used + size > budget:
            break
        fields[k] = _field_note(k, _won_scale(k, v), corp)
        used += size
    out = {"source": "정형공시", "서식": r["form"],
           "출처": f"{r['form']} ({r['rcept_dt']}), 접수번호 {r['rcept_no']}"}
    _D2_KEY = "보고자 보유주식등의 의결권있는주식(보고자 본인 · 특별관계자 제외)"
    _CAT = ("관계", "관 계", "구분")
    picked = [t for t in (r.get("rosters") or [])
              if str(t["headers"][0]).strip() in _CAT][:2]
    _is_roster = bool(picked)
    if not picked and r.get("rosters"):
        picked = r["rosters"][:1]
    if picked:
        t = picked[0]
        rows12 = [g[:8] for g in t["rows"][:12]]
        headers8 = t["headers"][:8]
        block = {}
        _kor = fields.get("성명(명칭) > 한글")
        if _kor:
            block["보고자 정식명칭"] = (
                f"{_kor} — '보고자 성명(명칭)'을 묻는 질의는 이 표기로 답하십시오"
                "(아래 표의 성명은 약칭·영문일 수 있습니다).")
        try:
            _ci = [str(h).strip() for h in headers8].index("의결권있는주식")
            _rep = next(g for g in rows12 if g and str(g[0]).strip() == "보고자")
            _v = str(_rep[_ci]).strip() if _ci < len(_rep) else ""
            if _v in ("-", ""):
                _rel = []              # 값이 있는 특별관계자 (이름, 수치)
                for _g in rows12:
                    if not _g or str(_g[0]).strip() == "보고자" or _ci >= len(_g):
                        continue
                    _c = str(_g[_ci]).strip()
                    if _re.fullmatch(r"[\d,]{2,}", _c):
                        _rel.append((str(_g[1]).strip() if len(_g) > 1 else "", _c))
                if len(_rel) == 1:
                    _who = f"특별관계자 {_rel[0][0]}의 보유량({_rel[0][1]}주)"
                elif _rel:
                    _who = f"특별관계자 {len(_rel)}인의 보유량"
                else:
                    _who = "특별관계자의 보유량"
                block[_D2_KEY] = (
                    f"0주 — 원문이 '-'입니다(직접 보유 없음). 이 표에 보이는 수치는 "
                    f"{_who}이고 **보고자 본인의 것이 아닙니다.** "
                    "'보고자의 의결권있는주식'을 묻는 질의의 답은 0주입니다.")
            else:
                block[_D2_KEY] = (
                    f"{_v} — 보고자 **본인만**의 수치입니다(특별관계자 제외). "
                    "`보유주식등의 수 및 보유비율` 필드는 **합산**이라 이 질의의 답이 아닙니다.")
        except (ValueError, StopIteration, IndexError):
            pass                       # 헤더·보고자 행이 없으면 아무 말도 하지 않는다
        sums = {k: f"{v['sum']:,} ({v['n']}행 합산)"
                for k, v in (t.get("col_sums") or {}).items()}
        if sums and _is_roster:            # 명부표일 때만
            block["전 행 합계(보고자 + 특별관계자 전원)"] = sums
        block["헤더"] = headers8
        block["행"] = rows12
        block["전체행수"] = t["n_rows"]
        out["명부표" if _is_roster else "표"] = block
    out["필드"] = fields
    if r.get("amended"):
        # 정정을 반영했으면 **무엇이 바뀌었는지** 남긴다. 값만 바꾸면 추적이 끊긴다.
        out["정정반영"] = {k: f"{a['from']} → {a['to']}" for k, a in
                       list(r["amended"].items())[:4]}
    if r.get("orphan"):
        out["비고"] = "원공시가 코퍼스에 없어 이 정정본이 유일본입니다."
    return out


@tool("1-1", "1-4", "2-9")


def get_financials(corp: str, year: int = None, month: int = 12,
                   consolidated: bool = True) -> dict:
    """재무제표 수치를 뽑는다. 재무상태표·손익계산서·현금흐름표·주당이익을 모두 포함한다.

    지침: (개념 목록은 모듈 끝 `_sync_concept_guide()`가 `_ALIAS`에서 생성해 채운다)

    연결 기준이 기본이고 3개년(당기·전기·전전기)이 series로 함께 온다.
    values의 단위는 unit 필드에 있다. 답변에 쓸 때 unit을 그대로 붙여야 한다.
    업종마다 표시 항목이 다르다. 은행지주 3곳(KB금융·우리금융지주·메리츠금융지주)에는
    매출액 항목이 없다. 없는 개념은 status에 이유가 온다.

    ★ 이 도구는 **회사 전체 합계**만 준다. 부문별·제품별·지역별 금액이나 **비중**은
      들어 있지 않다. 그런 질의는 find_tables(corp, "부문별 매출 현황")을 부르십시오.
      이 도구를 여러 번 부른다고 부문이 나오지 않는다.

    인자:
        corp: 기업명
        year: 사업연도. 안 주면 최신
        month: 12=사업보고서 6=반기 3·9=분기
        consolidated: 연결이면 True, 별도 재무제표를 원하면 False

    반환:
        {scope, scope_note, unit, values, series, sources, status, period, audit}.
        series는 {개념: {연도: 값}} 3개년. sources는 개념별 근거(XBRL 코드·계정 라벨)
    """
    r = _finance.extract(corp, year=year, month=month, consolidated=consolidated)
    vals = r.get("values", {})
    krw = r.get("krw", {})
    # 표시 단위를 **하나의 문자열**로 확정해 돌려준다. 단위를 개념마다 흩어 두면
    unit = _unit_label(vals, krw) or _legacy_unit(r)
    cunits = {k: (_SCALE_LABEL.get(round(krw[k] / vals[k]))
                  if vals.get(k) and krw.get(k) else unit)
              for k in vals}
    shown = {k: _money_str(v, cunits.get(k)) for k, v in vals.items()}
    shown_series = {k: {y: _money_str(x, cunits.get(k)) for y, x in (s or {}).items()}
                    for k, s in (r.get("series") or {}).items()}
    scope = r.get("scope") or ("연결" if consolidated else "별도")
    # 매출액 대비 비율은 **코드가 계산해 함께 준다.** `_fin_block`에만 붙였더니
    ratios = _sales_ratios(vals, vals)
    struct = _struct_ratios(vals)
    # 접수번호를 함께 준다 — 주최 답변 예시가 전부 접수번호를 인용한다.
    cite = _doctables._cite({"report_nm": r.get("period"), "rcept_no": r.get("rcept_no"),
                             "is_correction": r.get("is_correction")}) \
        if r.get("rcept_no") else None
    # 분기·반기는 손익이 3개월과 누적 두 벌로 실린다(IAS 34.20은 누적이 필수).
    # 질문이 "3분기 매출"이라고만 하면 둘 중 무엇인지 갈리므로 둘 다 주고 밝히게 한다.
    # ★ 지시문은 SYSTEM 이 아니라 도구 note 에 둔다 — SYSTEM 은 지시가 지시를 밀어낸다.
    spans, span_note = {}, None
    _m = r.get("month") or month
    if r.get("spans") and _m in (6, 9):     # 1분기는 3개월 ≡ 누적이라 갈릴 것이 없다
        _q = {6: ("4~6월", "1~6월"), 9: ("7~9월", "1~9월")}[_m]
        spans = {k: {f"해당 분기(3개월 · {_q[0]})": _money_str(v.get("3개월"), cunits.get(k)),
                     f"누적({_q[1]})": _money_str(v.get("누적"), cunits.get(k))}
                 for k, v in r["spans"].items()}
        span_note = (f"※ 위 values 는 **누적({_q[1]})** 기준입니다. 이 보고서는 손익·"
                     f"현금흐름을 **해당 분기(3개월 · {_q[0]})**와 **누적** 두 가지로 "
                     "싣고, 위 `기간구분`에 둘 다 담았습니다. **답변에 어느 기준인지 "
                     "반드시 밝히고**, 끝에 '해당 분기(3개월) 기준과 누적 기준 중 어느 "
                     "쪽을 원하시는지 알려주시면 그 기준으로 다시 답변드리겠습니다'라고 "
                     "확인 질문을 덧붙이십시오.")
    return {"corp": r.get("corp"), "year": r.get("year"), "period": r.get("period"),
            **({"출처": cite} if cite else {}),
            "scope": scope,
            # 연결/별도를 질문이 안 밝히면 우리가 정한 것이다 — 정했으면 밝히고 되묻는다.
            "scope_note": f"※ {scope} 기준입니다. 질문이 연결/별도를 밝히지 않았다면 "
                          f"답변에 '{scope} 기준'임을 적고, 끝에 '별도(개별) 기준이 "
                          "필요하시면 말씀해 주십시오'라고 덧붙이십시오."
                          if scope == "연결" else f"※ {scope} 기준입니다.",
            **({"기간구분": spans, "기간구분_note": span_note} if spans else {}),
            # 사업연도를 질문이 안 밝혀 우리가 최신으로 정한 경우.
            **({"기간_note": f"※ 질문이 사업연도를 지정하지 않아 코퍼스의 최신 보고서"
                            f"({r.get('period')})로 답했습니다. 답변에 그 사실을 적고, "
                            "끝에 '다른 사업연도를 원하시면 말씀해 주십시오'라고 "
                            "덧붙이십시오."}
               if r.get("year_inferred") else {}),
            # 4분기 단독 보고서는 코퍼스에 없다(정기공시 base_month 는 3·6·9·12뿐).
            **({"4분기_note": "※ 4분기만의 수치를 담은 보고서는 공시에 없습니다"
                             "(정기공시는 1분기·반기·3분기·사업보고서 넷뿐입니다). "
                             "사업보고서 값은 **연간 누적**입니다. 4분기 단독을 물어오면 "
                             "그 사실을 고지하십시오."}
               if _m == 12 else {}),
            "unit": unit,
            "values": shown,
            **({"재무구조_비율": struct,
                "struct_note": "위 비율은 **코드가 계산한 값**입니다"
                               "(부채비율=부채총계÷자본총계 · 자기자본비율=자본총계÷자산총계 · "
                               "유동비율=유동자산÷유동부채, 각 ×100). 직접 계산하지 마십시오."}
               if struct else {}),
            **({"매출액_대비_비율": ratios,
                "ratio_note": "코드가 계산한 값입니다(항목÷매출액×100). 손익 항목의 규모를 "
                              "물으면 금액과 함께 이 비율도 밝히십시오. 직접 계산하지 마십시오."}
               if ratios else {}),
            "units": cunits,
            "series": shown_series,
            "sources": {k: f"{r.get('labels', {}).get(k, '')} [{r.get('codes', {}).get(k, '')}]"
                        for k in vals} if r.get("via") == "xbrl" else {},
            "status": {k: v for k, v in r["status"].items() if v is not None},
            "structure": r.get("profile"),
            "audit": r["audit"].render()}


def _size(obj):
    """`loop._render`와 **같은 직렬화**로 잰 크기. 측정과 렌더가 어긋나면 예산이 헛돈다."""
    return len(_json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def _fit(out, rest):
    """조립 결과가 관측 상한에 **실제로** 들어가게 맞춘다."""
    def total():
        return _size(out + rest)

    while total() > _config.OBS_LIMIT:
        big = max((o for o in out if len(o.get("핵심행") or []) > 1),
                  key=lambda o: _size(o), default=None)
        if big is None:
            break
        big["핵심행"] = big["핵심행"][:-1]
        n = big.get("_dropped", 0) + 1
        big["_dropped"] = n
        big["생략"] = f"핵심행 {n}행 생략(크기 제한). 필요하면 다시 물으십시오"

    while total() > _config.OBS_LIMIT:
        sec = max((o for o in out if o.get("source") == "정본섹션"),
                  key=_size, default=None)
        if sec is None:
            break
        cut = False
        for b in sec.get("본문", []):
            if len(b.get("본문", "")) > 400:
                full = b.get("_full", len(b["본문"]))
                b["_full"] = full
                b["본문"] = b["본문"][: len(b["본문"]) // 2]
                b["생략됨"] = f"{full:,}자 중 {len(b['본문']):,}자(크기 제한)"
                cut = True
        if not cut:
            break
    for o in out:
        o.pop("_dropped", None)
        for b in (o.get("본문") or []) if isinstance(o.get("본문"), list) else []:
            b.pop("_full", None)
    return out + rest


def _canon_budget(rest, n_tables):
    """정본표 1개에 줄 크기(자). **고정 상수가 아니라 남는 공간을 실측해 배분**한다."""
    return max(300, (_config.OBS_LIMIT - _size(rest) - 60) // max(1, n_tables))


def _drop_empty(vals):
    """의미 없는 열을 뺀다 — 값이 빈 열(`-`), `비고`, `(단시간근로자)` 같은 부속 열."""
    return {k: x for k, x in vals.items()
            if x and x not in ("-", "–", "—")
            and "비고" not in k and "단시간" not in k}

#: 정본표 직렬화 형식. "json" | "md"
#:
CANON_FORMAT = _os.environ.get("CANON_FORMAT") or "md"


def _md_table(rows, unit_note):
    """정본표 → 마크다운 표. 열 이름을 헤더로 한 번만 쓰므로 JSON보다 훨씬 짧다."""
    cols = []
    for r in rows:
        for k in r["values"]:
            if k not in cols:
                cols.append(k)
    out = [unit_note, "| 구분 | " + " | ".join(cols) + " |",
           "|" + "---|" * (len(cols) + 1)]
    for r in rows:
        out.append("| " + r["label"] + " | "
                   + " | ".join(r["values"].get(c, "") for c in cols) + " |")
    return "\n".join(out)

FIN_BLOCK = True


def _fin_concepts(query):
    """질의에 들어 있는 재무 개념 id. `_ALIAS`(한글→개념)를 **최장일치**로 고른다."""
    if not FIN_BLOCK:
        return []
    q = (query or "")
    hits = [w for w in _ALIAS if w in q]
    kept = [w for w in hits if not any(w != o and w in o for o in hits)]
    return list(dict.fromkeys(_ALIAS[w] for w in kept))


def _fin_block(corp, query, year=None):
    """재무 개념 질의면 **연결 기준 값**을 붙여 준다. 아니면 None."""
    cids = _fin_concepts(query)
    if not cids:
        return None
    r = _finance.extract(corp, year=year)
    vals = {c: r["values"][c] for c in cids if c in r.get("values", {})}
    if not vals:
        return None
    krw = r.get("krw", {})
    unit = _unit_label(r.get("values", {}), krw)
    cunits = {c: (_SCALE_LABEL.get(round(krw[c] / vals[c]))
                  if vals.get(c) and krw.get(c) else unit)
              for c in vals}
    mixed = len(set(cunits.values())) > 1
    out = {"source": "재무표준값", "scope": r.get("scope", "연결"),
           "unit": unit,
           "unit_note": ("연결 기준. 단위가 항목마다 다르므로 **각 값에 붙은 단위를 그대로**"
                         " 쓰십시오(환산하지 마십시오)." if mixed else
                         f"연결 기준, 단위 {unit}. 이 값을 그대로 쓰십시오."),
           "values": {c: _money_str(v, cunits.get(c)) for c, v in vals.items()},
           "units": cunits,
           "series": {c: {y: _money_str(x, cunits.get(c))
                          for y, x in (r.get("series", {}).get(c) or {}).items()}
                      for c in vals if r.get("series")},
           "note": "재무제표 표준 수치는 이 값이 정본입니다. 검색 결과의 표는 "
                   "연결·별도가 섞여 있으니 이 값을 우선하십시오."}
    struct = _struct_ratios(r.get("values", {}))
    if struct:
        out["재무구조_비율"] = struct
        out["struct_note"] = ("위 비율은 **코드가 계산한 값**입니다"
                              "(부채비율=부채총계÷자본총계 · 자기자본비율=자본총계÷자산총계 · "
                              "유동비율=유동자산÷유동부채, 각 ×100). 직접 계산하지 마십시오.")
    ratios = _sales_ratios(vals, r.get("values", {}))
    if ratios:
        out["매출액_대비_비율"] = ratios
        out["ratio_note"] = ("위 비율은 **코드가 계산한 값**입니다(항목÷매출액×100). "
                             "손익 항목의 규모를 물으면 금액과 함께 이 비율도 밝히십시오. "
                             "직접 계산하지 마십시오.")
    return out

#: 매출액 대비로 읽는 것이 표준인 손익 항목.
#: 근거: 이 비율들은 공통형 손익계산서(common-size income statement)의 기본 표시로,
#: 매출액을 100%로 두고 각 손익 항목을 그 비율로 나타내는 재무분석 표준 관행이다.
#: `_RATIO`의 OPM·GPM·NPM(영업이익률·매출총이익률·순이익률)과 같은 계열이며,
#: 매출원가율 = 1 − GPM으로 서로 정합한다.
_FIN_NOT_ENOUGH = _re.compile(r"비중|구성비|부문별|제품별|사업부문별|지역별|품목별|"
                              r"부문 별|제품 별|지역 별|매출 구성|매출구성")

_SALES_RATIO = {"cost_of_sales": "매출원가율", "sga": "판관비율",
                "gross_profit": "매출총이익률", "operating_income": "영업이익률",
                "net_income": "순이익률"}

_STRUCT_RATIO = (
    ("부채비율", "liabilities_total", "equity_total"),
    ("자기자본비율", "equity_total", "assets_total"),
    ("유동비율", "current_assets", "current_liabilities"),
)


def _struct_ratios(allvals):
    """재무구조 비율(%). 필요한 두 항목이 다 있을 때만 낸다."""
    out = {}
    for name, num, den in _STRUCT_RATIO:
        a, b = allvals.get(num), allvals.get(den)
        if a is None or not b:
            continue
        try:
            out[name] = f"{a / b * 100:.1f}%"
        except (TypeError, ZeroDivisionError):
            continue
    return out


def _sales_ratios(asked, allvals):
    """질의에 걸린 손익 항목의 **매출액 대비 비율(%)** — 코드가 계산한다."""
    top = allvals.get("revenue")
    if not top or top <= 0:
        return {}
    out = {}
    for cid, name in _SALES_RATIO.items():
        v = asked.get(cid)
        if v is None:
            continue
        try:
            out[name] = f"{v / top * 100:.1f}%"
        except (TypeError, ZeroDivisionError):
            continue
    return out

#: 표 단위에서 금액 단위만 뽑는다(`억원, %` → `억원`).
#: **금액 단위만**. 고치려는 혼동은 `억원 vs 백만원`이라는 배수 문제이고,
_MONEY_UNIT = _re.compile(r"(조원|십억원|억원|백만원|천원|천만원|원)")
_NUMCELL = _re.compile(r"^[\d,]+(\.\d+)?$|^[(△▲-][\d,]+(\.\d+)?\)?$")

#: 단위 인라인 on/off. A/B용.
UNIT_INLINE = True

#: 열 이름이 **금액임을 적극적으로 가리킬 때만** 단위를 붙인다.
_MONEY_COL = _re.compile(r"액|금액|매출|투자|급여|자산|부채|자본|수익|비용|원가|대금|가액")

#: **표 전체가 하나의 금액 단위인 정본표.** 여기 등록된 표는 열 이름과 무관하게
#: 모든 수치 셀에 단위를 붙인다.
#:
_MONEY_TABLE = {"cashflow", "inventory"}


def _with_unit(values, unit, table_id=None):
    """**금액 열의** 수치 셀에 단위를 붙여 준다. 값과 단위를 떼어 놓으면 모델이 섞는다."""
    if not UNIT_INLINE:
        return values
    m = _MONEY_UNIT.search(unit or "")
    if not m:
        return values
    u = m.group(1)
    all_money = table_id in _MONEY_TABLE
    return {k: (f"{x} {u}"
                if (all_money or _MONEY_COL.search(k or ""))
                and _NUMCELL.match((x or "").strip())
                else x)
            for k, x in values.items()}

ROWS_ALL = {"capex", "raw_materials", "auditor", "rnd", "employees",
            "regional_sales", "equity_method",
            "ppe_movement",
            "segment_sales",
            "largest", "dividend", "shares", "cashflow", "related_party",
            "asset_quality", "solvency", "affiliates",
            "equity_invest", "contingent",
            "rnd_pipeline", "license_out"}

ROWS_ALL_CAP = {"equity_invest": 3500, "contingent": 3500, "cashflow": 4500}


def _col_score(col, qn):
    """열 이름이 질문 어휘와 얼마나 맞나. 0=무관 · 1=2자 겹침 · 2=3자 이상 · 3=이름 전체."""
    c = _re.sub(r"\s+", "", str(col or ""))
    if not c or not qn:
        return 0
    if c in qn:
        return 3
    for L in range(len(c), 1, -1):
        for i in range(len(c) - L + 1):
            if c[i:i + L] in qn:
                return 2 if L >= 3 else 1
    return 0


def _num_like(x):
    """그 셀이 **수치 셀**인가. `_with_unit`이 붙인 단위 접미까지 받는다."""
    t = str(x or "").strip()
    m = _re.match(r"^\(?-?[\d,]+(?:\.\d+)?\)?", t)
    if not m:
        return False
    return len(t[m.end():].strip()) <= 6      # `백만원`·`%`·`주`·`명` 등


def _order_cols(vals, query):
    """질문이 부른 이름의 열을 맨 앞으로 올린다. 버리지 않고 순서만 바꾼다."""
    if not query or len(vals) < 2:
        return vals
    qn = _re.sub(r"\s+", "", str(query))
    items = list(vals.items())
    nidx = [i for i, (_k, x) in enumerate(items) if _num_like(x)]
    if len(nidx) < 2:
        return vals
    order = sorted(nidx, key=lambda i: (-_col_score(items[i][0], qn), i))
    it = iter(order)
    out = {}
    for i, (k, x) in enumerate(items):
        if i in nidx:
            j = next(it)
            out[items[j][0]] = items[j][1]
        else:
            out[k] = x
    return out


def _canon(v, year=None, budget=None, query=None):
    """정본표 → 도구 응답. **핵심행은 전문, 나머지는 라벨만** 준다."""
    keys = [r for r in v["rows"] if r.get("key")] or v["rows"]
    out = {"source": "정본표", "table": v["table"], "section": v["section"],
           "unit": v["unit"], "unit_note": v["unit_note"]}
    if v.get("출처"):
        out["출처"] = v["출처"]
    if v.get("표_각주"):
        out["표_각주"] = v["표_각주"]
    for k in v:
        if k.startswith("표_공통값") or k.startswith("표_계산합계"):
            out[k] = v[k]
    if year:
        out["year"] = year
    budget = budget or _canon_budget(0, 1)   # 인자 2개다(종전 `(1)`은 호출 시 TypeError)
    cap = ROWS_ALL_CAP.get(v.get("id"))
    if cap:
        budget = min(budget, cap)
    rest_rows = ([r for r in v["rows"] if r not in keys]
                 if (v.get("id") in ROWS_ALL
                     or any(r.get("key_arith") for r in v["rows"])) else [])
    n_key = len(keys)
    used, shown = _size(out), []
    for r in keys + rest_rows:
        vals = r["values"]
        if v.get("id") in _TABLE_ZERO:
            vals = {k: ("0" if (x or "").strip() in _DASH else x) for k, x in vals.items()}
        item = {"label": r["label"],
                "values": _order_cols(
                    _with_unit(_drop_empty(vals), v["unit"], v.get("id")), query)}
        size = (len(r["label"]) + sum(len(x) + 3 for x in item["values"].values())
                if CANON_FORMAT == "md" else _size(item))
        if shown and used + size > budget:
            break
        shown.append(item)
        used += size
    if CANON_FORMAT == "md" and shown:
        out["핵심행"] = _md_table(shown[:n_key], v["unit_note"])
        if shown[n_key:]:
            out["그 밖의 행(값 포함)"] = _md_table(shown[n_key:], "")
        out.pop("unit_note", None)
    else:
        out["핵심행"] = shown[:n_key]
        if shown[n_key:]:
            out["그 밖의 행(값 포함)"] = shown[n_key:]
    skipped = n_key - min(len(shown), n_key)
    shown_labels = {i["label"] for i in shown}
    others = [r["label"] for r in v["rows"]
              if r["label"] and r["label"] not in shown_labels]
    if skipped:
        out["생략"] = f"핵심행 {skipped}행 생략(크기 제한). 필요하면 다시 물으십시오"
    elif others:
        out["그 밖의 행"] = others[:10]
    if v.get("id") == "capex":
        tot = _capex_total(v)
        if tot:
            out["합계(코드가 계산)"] = tot
    note = (_capex_note(v) if v.get("id") == "capex" else
            _agm_note(query) if v.get("id") == "dividend" else
            _TABLE_NOTE.get(v.get("id")))
    if note:
        out["표_주의"] = note
    return out

_TOTAL_LABEL = _re.compile(r"^\s*(합\s*계|계|소\s*계|total)\s*$", _re.I)


def _capex_total(v):
    """합계 행이 없는 표의 합을 **결정론 코드가** 계산한다. 못 믿을 땐 계산하지 않는다."""
    rows = v.get("rows") or []
    labs = [r["label"] for r in rows if not _TOTAL_LABEL.match(r["label"] or "")]
    if len(labs) < 2 or any(_TOTAL_LABEL.match(r["label"] or "") for r in rows):
        return None                       # 합계 행이 이미 있으면 계산하지 않는다
    if len(set(labs)) != len(labs):
        return None                       # 라벨 중복 = 소계 섞임 의심 → 기권
    body = [r for r in rows if r["label"] in labs]
    sums = {}
    for col in dict.fromkeys(k for r in body for k in r["values"]):
        vals = [_cellnum(r["values"].get(col)) for r in body]
        got = [x for x in vals if x is not None]
        if len(got) < 2 or len(got) != len(body):
            continue                      # 일부 행만 값이 있으면 합이 뜻을 잃는다
        sums[col] = sum(got)
    if not sums:
        return None
    unit = v.get("unit") or ""
    return {"값": {c: f"{s:,.0f}{(' ' + unit) if unit else ''}" for c, s in sums.items()},
            "계산근거": f"{len(body)}개 행({', '.join(labs[:6])})의 단순합. "
                        f"공시 표에 합계 행이 없어 코드가 더한 값입니다 — "
                        f"합계를 물으면 **이 값을 그대로** 쓰고 직접 더하지 마십시오."}

#: 정기주주총회를 지목하는 질의.
_AGM_Q = _re.compile(r"정기\s*주주총회|정기주총|주주총회\s*(결과|결의|안건|승인)|주총\s*(결과|결의|승인)")


def _agm_note(query):
    """**정기주주총회 결의를 묻는 질의**에만 붙는 주의. 아니면 None."""
    if not query or not _AGM_Q.search(str(query)):
        return None
    return ("**정기주주총회가 승인하는 것은 직전 사업연도 배당입니다**(상법 §449·§462). "
            "'2025년 정기주주총회'의 결의 결과를 물으면 이 표의 **전기 열**"
            "(예: `전기 2024년`)을 읽으십시오. 당기 열은 아직 총회 승인 전이거나 "
            "다음 총회의 안건입니다. 표의 열 이름을 그대로 밝혀 적으십시오.")


def _capex_note(v):
    """`capex` 표가 **향후 계획인지 당해 실적인지** 밝힌다 — 열 이름으로 가린다."""
    # `향후`도 센다 — 현대자동차는 **한 표에 실적과 계획을 같이** 적는다:
    #   `사업부문 | 구분 | 2025년실적 | 2024년실적 | 2023년실적 | 향후 투자계획(2026년)`
    # 그 표를 두고 "실적뿐"이라 고지하면 있는 계획을 없다고 말하게 된다.
    cols = " ".join(k for r in v.get("rows", []) for k in (r.get("values") or {}))
    if any(t in cols for t in ("예상투자", "예정투자", "계획투자", "향후")):
        return None
    return ("표의 금액은 **하나도 빠뜨리지 말고 그대로 제시하십시오.** "
            "그 위에 한 줄만 덧붙이면 됩니다 — 이 표는 당해 집행 실적이며, "
            "이 회사 공시에는 향후 투자계획 표가 따로 없습니다.")

#: 표별 **읽는 법** 주의사항. 값이 아니라 표기 규약을 알려 준다.
#:
#: ## subsidiary — `-`는 "확인 불가"가 아니라 **0**이다
_TABLE_NOTE = {
    # ## rnd — **직접 빼지 마십시오.** 차감 후 값이 표에 이미 있습니다
    "rnd": "행 라벨을 그대로 읽으십시오. `연구개발비용 총계`는 정부보조금 차감 **전**, "
           "`연구개발비용 계`(또는 `정부보조금 차감후 연구개발비용 계`)는 차감 **후**입니다. "
           "**직접 빼서 계산하지 마십시오** — 회사에 따라 `계`만 있고 그것이 이미 차감 후입니다. "
           "정부보조금 자체를 물으면 `(정부보조금)` 행 값을 부호까지 그대로 쓰십시오.",
    "asset_quality": "이 표의 지표가 **그룹 기준인지 은행 단독 기준인지 표에 적혀 있지 "
                     "않을 수 있습니다.** 지주사 본문에 `NPL 비율은 그룹 X%, 은행 Y%`처럼 "
                     "병기된 경우가 있으니, 그룹 기준을 묻는 질의면 "
                     "`find_sections(corp, '재무건전성')`으로 본문을 함께 확인하고 "
                     "**어느 기준의 값인지 밝혀** 답하십시오.",
    "subsidiary": "이 표에서 `-`는 **해당 없음(0개사)**입니다. "
                  "'확인되지 않음'이 아닙니다 — 상장 종속회사가 없으면 그 행이 `-`로 표기됩니다"
                  "(합계 = 상장 + 비상장로 검증됩니다). 0개사라고 답하십시오.",
}

#: 빈칸 표기. 공시 표는 "해당 없음"을 이 문자들로 적는다.
_DASH = ("-", "–", "—", "‐", "―")

#: `-`를 **0으로 정규화**할 표. 값을 바꾸는 것이므로 항등식으로 검증된 표에만 쓴다.
#:
_TABLE_ZERO = {"subsidiary"}

#: 원 배수 → 단위 이름. `_unit_label`과 `_concept_unit`이 같은 사전을 쓴다.
_SCALE_LABEL = {1: "원", 1000: "천원", 1000000: "백만원",
                100000000: "억원", 1000000000000: "조원"}


def _concept_unit(corp, cid):
    """**개념 하나**의 표시 단위. 없으면 None."""
    c = _facts.company(corp)
    if c is None:
        return None
    y = _store.latest_fiscal_year(c["corp_name"])
    if not y:
        return None
    try:
        r = _finance.extract(c["corp_name"], y)
    except Exception:                          # 추출 실패는 단위 없음으로 본다(정직한 None)
        return None
    v = (r.get("values") or {}).get(cid)
    w = (r.get("krw") or {}).get(cid)
    if not v or not w:
        return None
    return _SCALE_LABEL.get(round(w / v))


def _money_str(v, unit):
    """`114140919.0` → `"114,140,919 백만원"`."""
    if v is None:
        return None
    s = f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"
    return f"{s} {unit}" if unit else s


def _pct_str(v):
    """증감률 → `"+6.2%"`. 부호를 붙여 방향을 문자열이 스스로 말하게 한다."""
    if v is None:
        return None
    return f"{v:+.1f}%"


def _unit_label(values, krw):
    """표시값 → 원 배수를 역산해 단위 문자열을 만든다. 값이 없으면 None."""
    for k, v in values.items():
        w = krw.get(k)
        if not v or not w:
            continue
        ratio = round(w / v)
        return {1: "원", 1000: "천원", 1000000: "백만원",
                100000000: "억원", 1000000000000: "조원"}.get(ratio)
    return None


def _legacy_unit(r):
    """라벨 파서 폴백 경로의 단위(개념별 Unit에서 가장 흔한 것)."""
    from collections import Counter
    c = Counter(u.raw for u in (r.get("units") or {}).values() if getattr(u, "raw", None))
    return c.most_common(1)[0][0] if c else None

#: 한글·통용어 → 개념 id. **지식은 프롬프트가 아니라 데이터로 둔다.**
_ALIAS = {
    "매출": "revenue", "매출액": "revenue", "수익": "revenue", "영업수익": "revenue",
    "매출원가": "cost_of_sales", "매출총이익": "gross_profit",
    "판관비": "sga", "판매비와관리비": "sga", "판매비와 관리비": "sga",
    "영업이익": "operating_income", "당기순이익": "net_income", "순이익": "net_income",
    "주당순이익": "eps_basic", "주당이익": "eps_basic", "기본주당이익": "eps_basic",
    "eps": "eps_basic", "희석주당이익": "eps_diluted",
    "자산": "assets_total", "자산총계": "assets_total",
    "부채": "liabilities_total", "부채총계": "liabilities_total",
    "자본": "equity_total", "자본총계": "equity_total",
    # 자본금은 **자본총계가 아니다**(IAS 1.78(e) 납입자본). 이 항목이 없어서
    # "자본금"이 "자본"으로 흘러 자본총계를 답했다 — 삼성전자 897,514백만원을
    # 436,320,337백만원이라고 답했다. `_fin_concepts`의 최장일치 규칙과 함께 봐야 한다.
    "자본금": "issued_capital", "납입자본금": "issued_capital", "액면총액": "issued_capital",
    "재고자산": "inventories", "현금": "cash", "법인세비용": "tax_expense",
    "영업활동현금흐름": "cf_operating", "투자활동현금흐름": "cf_investing",
    "재무활동현금흐름": "cf_financing",
    "순이자손익": "net_interest_income", "순수수료손익": "net_fee_income",
    "보험수익": "insurance_revenue",
    "해약환급금준비금": "surrender_reserve",
}

#: 파생 비율 — **코드가 계산한다**. 분자/분모 개념만 등록하고 산술은 compute가 한다.
#: 정의는 표준 재무비율이다: OPM=영업이익/매출, GPM=매출총이익/매출, NPM=순이익/매출.
_RATIO = {"opm": ("operating_income", "revenue"), "영업이익률": ("operating_income", "revenue"),
          "gpm": ("gross_profit", "revenue"), "매출총이익률": ("gross_profit", "revenue"),
          "npm": ("net_income", "revenue"), "순이익률": ("net_income", "revenue"),
          "매출원가율": ("cost_of_sales", "revenue"),
          "판관비율": ("sga", "revenue"),
          "판매비와관리비율": ("sga", "revenue")}

#: 증가율 접미사. **연도를 LLM이 고르게 하면 틀린다** — 실제로 "전년도 대비"를 물었는데
#: 2025년이 아니라 2024년 증가율(13.94%)을 답했다. 코드가 연도별로 계산해 키로 붙인다.
_GROWTH_SUFFIX = ("증가율", "성장률", "증감률", "growth", "yoy")


def _cid(concept):
    c = (concept or "").strip()
    return _ALIAS.get(c, _ALIAS.get(c.lower(), c))


def _growth_base(concept):
    """'매출증가율' → 'revenue'. 증가율 질의가 아니면 None."""
    c = (concept or "").strip()
    for suf in _GROWTH_SUFFIX:
        if c.lower().endswith(suf):
            base = c[: len(c) - len(suf)].strip(" _-")
            cid = _cid(base)
            if cid:
                return cid
    return None


#: 정본표 경로에도 **증감률을 코드가 계산해** 붙일지. `A2_CANON_GROWTH=0`이면 끈다.
#:
#: 왜 — `financial_series`는 표준 개념(`revenue` 등)에는 `전년대비증감률`을 이미 주는데
#: **정본표 개념(`수주잔고` 등)에는 원값만** 줬다. 그러면 `SYSTEM` 규칙 5가
#: *"수치는 도구가 계산합니다"*라고 지시해도 모델은 줄 게 없어 직접 나눈다 —
#: §6-31(금지만 적지 말고 대체 지시를 함께 적어라)의 값 버전이다.
#:
#: 실측된 피해(r53 다중-18/HD현대일렉트릭):
#:     원값만 줌 → 모델이 (9,423,400−7,646,580)÷7,646,580 을 직접 계산
#:     모델 답변 **23.32%** · 정확한 값 **23.2368%** · 엘에스일렉트릭도 45.76% vs 45.4709%
#:     판정은 둘 다 O였다(텍스트형이라 기업명만 맞으면 통과) — 주최 평가지표 1
#:     `수치·비교(증감)가 정확한가`에는 그대로 걸린다.
#:
#: 실현 가능성(전수: 기업 70 × 정본표 34 = 2,380쌍):
#:     두 해 연속 확보 1,549쌍 · 그중 행·열이 맞아 계산 가능 **1,166쌍(75.3%)**
#:     계산 가능한 셀 47,860개 · 표당 추가 문자 중앙 272자(상위 8행 상한)
_CANON_GROWTH = _os.environ.get("A2_CANON_GROWTH", "1") != "0"

#: 증감률을 몇 행까지 실을지. 관측 예산을 먹지 않도록 상한을 건다(§6-14).
_CANON_GROWTH_MAX = 8

_CANON_NUM = _re.compile(r"^\(?-?[\d,]+(?:\.\d+)?\)?$")

#: 열 이름에 박힌 기준일·기간. 연도 간 열을 맞추려면 떼야 한다.
#:   `당기말 수주잔(2025.12.31)` ↔ `당기말 수주잔(2024.12.31)` 은 같은 열이다.
#:   떼지 않으면 열이 해마다 달라져 증감률을 하나도 못 만든다(효성중공업 실측).
_CANON_COLDATE = _re.compile(r"\s*[(（][^()（）]*(?:19|20)\d\d[^()（）]*[)）]")


def _canon_col(name):
    """연도 간 비교용 열 키. 기준일 괄호와 공백을 지운다."""
    return _CANON_COLDATE.sub("", str(name)).strip()


def _canon_num(x):
    """정본표 셀 → 수치. 괄호는 음수(회계 표기). 수치가 아니면 None."""
    if not isinstance(x, str):
        return None
    t = x.strip()
    if not _CANON_NUM.match(t):
        return None
    body = t.replace(",", "")
    neg = body.startswith("(") and body.endswith(")")
    body = body.strip("()")
    try:
        v = float(body)
    except ValueError:
        return None
    return -v if neg else v


#: 열 이름의 기간 표지. `전기말 수주잔` ↔ `당기말 수주잔` 을 같은 항목으로 묶는다.
_CANON_PRIOR = ("전전기말", "전전기", "전기말", "전기", "기초잔액", "기초", "전년말", "전년", "직전")
_CANON_CUR   = ("당기말", "당기", "기말잔액", "기말", "금기말", "금기", "당분기말", "당분기")


def _canon_period(name):
    """열 이름 → (기간표지, 항목명). 표지가 없으면 (None, 원래이름)."""
    t = _canon_col(name)
    for m in _CANON_PRIOR:
        if t.startswith(m):
            return "prior", t[len(m):].strip(" ·-")
    for m in _CANON_CUR:
        if t.startswith(m):
            return "cur", t[len(m):].strip(" ·-")
    return None, t


def _canon_growth(got):
    """정본표 → {`행 · 항목`: 전년대비 증감률}. **최신 보고서 한 장 안에서만** 계산한다.

    ★★ 보고서를 가로질러 비교하면 틀린다(2026-08-28 실측). 최신 보고서는 전기 수치를
    **재작성**하는 경우가 있어 같은 날짜의 값이 보고서마다 다르다:

        2024년 사업보고서  중공업·효성중공업(주) 당기말 수주잔(2024.12.31) =  5,680,878
        2025년 사업보고서  중공업·효성중공업(주) 전기말 수주잔(2024.12.31) = 10,711,892

    두 보고서를 가로질러 계산하면 +170.0%가 나오는데 실제는 +43.2%다. 그래서 최신
    보고서가 **자기 안에** 담은 전기/당기 열끼리만 비교한다. 그 표를 만든 회사가
    같은 기준으로 나란히 적어 둔 값이므로 재작성 위험이 없다.

    검산(효성중공업 2025 사업보고서): 전기말 중공업 10,711,892 + 건설 5,679,766 =
    16,391,658 · 당기말 15,340,242 + 5,518,255 = 20,858,497 — 사람이 확정한 정답과 일치한다.

    전기 열이 없는 표는 증감률을 만들지 않는다. 지어내지 않는 편이 낫다.
    """
    if not _CANON_GROWTH or not got:
        return {}
    cur = got[-1]
    out, seen = {}, set()
    for row in cur.get("rows") or []:
        lb = row.get("label")
        if not lb:
            continue
        pairs = {}
        for col, val in (row.get("values") or {}).items():
            kind, base = _canon_period(col)
            if not kind:
                continue
            # 열 이름이 정확히 `기초`/`기말`이면 항목명이 빈 문자열이 된다.
            # 흔한 형태라(유형자산 증감표 등) 건너뛰면 통째로 못 잡는다.
            pairs.setdefault(base or "잔액", {})[kind] = val
        for base, kv in pairs.items():
            b, a = _canon_num(kv.get("prior")), _canon_num(kv.get("cur"))
            if b in (None, 0) or a is None:
                continue
            # 원문의 병합 셀(rowspan)로 같은 부문 합계가 여러 행에 퍼진다 —
            # 같은 (항목, 전기, 당기)는 한 번만 싣는다(상한을 헛되이 쓰지 않는다).
            sig = (base, b, a)
            if sig in seen:
                continue
            seen.add(sig)
            out[f"{lb} · {base}"] = _pct_str(_compute.run("growth", a, b))
            if len(out) >= _CANON_GROWTH_MAX:
                return out
    return out


@tool("1-4", "1-18", "2-6")


def financial_series(corp: str, concept: str, years: int = 3) -> dict:
    """한 개념의 3개년 값을 모은다. 추이·증감·비율 질의에 쓴다. 한글 개념명도 받는다.

    지침: (개념 목록은 모듈 끝 `_sync_concept_guide()`가 `_ALIAS`에서 생성해 채운다)

    "매출증가율"처럼 뒤에 증가율·성장률을 붙이면 **전년 대비 증감률(%)을 코드가 계산**해
    연도별로 돌려줍니다. 2025 키의 값이 2025년의 전년 대비 증가율입니다.

    개념 id: revenue cost_of_sales gross_profit sga operating_income net_income
    eps_basic eps_diluted assets_total liabilities_total equity_total inventories cash
    tax_expense cf_operating cf_investing cf_financing net_interest_income insurance_revenue.
    비율은 opm gpm npm(%)로 부르면 코드가 계산한다.

    인자:
        corp: 기업명
        concept: 개념 id 또는 한글명. 예 "operating_income" "주당순이익" "opm" "매출증가율"
        years: 최근 몇 개년. 기본 3(당기·전기·전전기)

    반환:
        {연도별, 전년대비증감률, 최신연도, 개념}. 증감률은 코드가 계산한 %이므로
        직접 나누지 마십시오. 최신연도가 당기입니다
    """
    key = (concept or "").strip().lower()
    if concept and concept not in _ALIAS and _cid(concept) not in _RATIO:
        tids = _doctables.match(concept)
        if tids:
            c = _facts.company(corp)
            if c is None:
                return {"corp": corp, "status": "코퍼스에 없는 기업"}
            nm = c["corp_name"]
            base = _store.latest_fiscal_year(nm) or 0
            got = []
            for y in range(base - years + 1, base + 1):
                v = _doctables.values(nm, tids[0], y)
                if not v:
                    continue
                keys = [r for r in v["rows"] if r.get("key")] or v["rows"]
                got.append({"year": y, "table": v["table"], "unit": v["unit"],
                            "unit_note": v["unit_note"], "rows": keys[:8]})
            if got:
                # 키 순서는 표준 개념 경로와 같게 둔다 — 삽입 순서가 곧 관측 순서다.
                _g = _canon_growth(got)
                # 관측 상한을 넘길 자리면 붙이지 않는다. 무조건 붙였더니 이미 잘리고 있던
                # 관측 6건에서 절단 경계가 밀려 수치가 하나씩 사라졌다(전수 A/B).
                _base = {"개념": concept, "표": tids[0], "연도별": got,
                         "최신연도": got[-1]["year"]}
                if _g and _size(_base) + _size(_g) + 120 > _config.OBS_LIMIT:
                    _g = {}
                # 표준 개념 분기는 출처를 주는데 이 분기만 안 줬다 — 값을 얻고 근거를
                # 잃는 교환은 하지 않는다(실측 A/B에서 접수번호를 잃었다).
                _fr0 = _finance.extract(nm, year=None)
                _c0 = (_doctables._cite({"report_nm": _fr0.get("period"),
                                         "rcept_no": _fr0.get("rcept_no"),
                                         "is_correction": _fr0.get("is_correction")})
                       if _fr0.get("rcept_no") else None)
                return {"개념": concept, "표": tids[0],
                        **({"출처": _c0} if _c0 else {}),
                        "연도별": got,
                        **({"전년대비증감률": _g,
                            "증감률_note": "위 증감률은 **코드가 계산한 값**입니다"
                                          f"({got[-2]['year']}년 → {got[-1]['year']}년, "
                                          "(당기−전기)÷전기×100). 직접 계산하지 마십시오."}
                           if _g else {}),
                        "최신연도": got[-1]["year"],
                        "note": "각 연도의 사업보고서에서 같은 표를 읽은 것입니다. "
                                "열 이름에 역년이 함께 있으니 그대로 쓰십시오."}
    gb = _growth_base(concept)
    if gb:                                   # "매출증가율"처럼 증감률을 직접 물은 경우
        v = _finance.series(corp, gb, years=years + 1)
        g = {y: _compute.run("growth", v[y], v[y - 1]) for y in sorted(v) if (y - 1) in v}
        last = max(g) if g else None
        return {"개념": gb, "전년대비증감률": {y: _pct_str(x) for y, x in g.items()},
                "최신연도": last, "최신증감률": _pct_str(g.get(last))}
    if key in _RATIO:
        num_id, den_id = _RATIO[key]
        num = _finance.series(corp, num_id, years=years)
        den = _finance.series(corp, den_id, years=years)
        r = {y: _compute.run("margin", num[y], den[y])
             for y in sorted(set(num) & set(den)) if den.get(y)}
        last = max(r) if r else None
        return {"개념": key, "연도별": {y: _pct_str(x) for y, x in r.items()},
                "최신값": _pct_str(r.get(last)), "최신연도": last}
    cid = _cid(concept)
    v = _finance.series(corp, cid, years=years + 1)
    ys = sorted(v)
    g = {y: _compute.run("growth", v[y], v[y - 1]) for y in ys if (y - 1) in v}
    last = ys[-1] if ys else None
    unit = _concept_unit(corp, cid)
    _fr = _finance.extract(corp, year=None)
    _cite2 = (_doctables._cite({"report_nm": _fr.get("period"), "rcept_no": _fr.get("rcept_no"),
                                "is_correction": _fr.get("is_correction")})
              if _fr.get("rcept_no") else None)
    out = {"개념": cid, "단위": unit,
           **({"출처": _cite2} if _cite2 else {}),
           "연도별": {y: _money_str(v[y], unit) for y in ys[-years:]},
           "전년대비증감률": {y: _pct_str(x) for y, x in g.items()},
           "최신값": _money_str(v.get(last), unit),
           "최신연도": last,
           "최신증감률": _pct_str(g.get(last))}
    if cid in _CF_CONCEPTS:
        cf = _cashflow_canon(corp)
        if cf:
            out["현금흐름표"] = cf
    return out

#: 현금흐름 3대 활동 개념. 이 개념을 물으면 **합계만으로는 답이 안 되는 질의**가 섞여 있다.
_CF_CONCEPTS = frozenset(("cf_operating", "cf_investing", "cf_financing"))


def _cashflow_canon(corp):
    """현금흐름 개념 질의에 **연결 현금흐름표 정본표**를 함께 붙인다. 없으면 None."""
    try:
        c = _facts.company(corp)
        if c is None:
            return None
        v = _doctables.values(c["corp_name"], "cashflow")
        return _canon(v) if v else None
    except Exception:                      # 보조 근거다. 실패해도 본 결과를 막지 않는다
        return None


@tool("1-3", "1-7", "1-8", "1-9", "1-12", "1-14", "1-15", "1-16", "1-17", "1-18", "2-3")


def find_tables(corp: str, query: str, year: int = None, k: int = 4,
                years: int = 1) -> list:
    """공시에서 표를 찾는다. 자주 쓰는 표는 라벨·값·단위로 파싱해서 함께 돌려준다.

    주식의 총수 · 직원 등의 현황 · 연결대상 종속회사 · 최대주주 · 시설투자 · 가동률은
    정본 표를 결정론으로 찾아 rows(라벨→값)와 unit을 줍니다. 이 값을 그대로 쓰십시오.

    ★ 재무제표 표준 수치(매출·영업이익·순이익·자산·부채·자본·판관비·현금흐름·주당이익)는
    get_financials나 financial_series를 쓰십시오.

    예: "주식의 총수" "최대주주 현황" "직원 현황" "1인평균 급여액" "우선주 발행"
    "생산능력 가동률" "시설투자" "유상증자 무상증자 이력" "재고자산" "현금흐름표"

    인자:
        corp: 기업명
        query: 찾을 내용을 한국어로. 공시 용어에 가까울수록 정확하다
        year: 사업연도. 안 주면 최신
        k: 표 몇 개까지
        years: 최근 몇 개년의 표를 모을지. 3개년 추이 질의는 3으로 주십시오

    반환:
        [{section, table, unit, unit_note, key_rows, rows, year}] 정본표 +
        [{section, html, score}] 검색 결과
    """
    k = min(k or _KMAX, _KMAX)
    # ① 어떤 정본표가 걸리는지 먼저 정한다
    name, _miss = _no_corp(corp)
    if _miss:
        return [_miss]
    fin = _fin_block(name, query, year)
    base = year or _store.latest_fiscal_year(name) or 0
    sub, mon = _periodic_of(query)
    if not year and sub != "annual":
        base = _store.latest_base_year(name, sub) or base
    matched = _doctables.match(query)
    found = [(y, v) for tid in matched
             for y in range(base - years + 1, base + 1)
             if (v := _doctables.values(name, tid, y, subtype=sub, month=mon))]

    alt = []
    for tid, ftype in _CANON_FALLBACK.items():
        if tid in matched and not any(True for _y, v in found if v.get("id") == tid):
            rec = _filing_record(name, ftype)
            if rec:
                rec = {"※": f"`{_doctables.BY_ID[tid].name}` 정본표가 이 회사에는 "
                            f"없습니다. 같은 사실을 담은 **{ftype}** 공시로 대체합니다.",
                       **rec}
                alt.append(rec)

    # ② 검색 결과를 먼저 만든다 — 정본표가 있으면 HTML 없이 목록만
    hits = _search.search(query, corp=corp, year=year, kind="table", k=k)
    has_canon = bool(found)
    rest = []
    for i, h in enumerate(hits[:3 if has_canon else k]):
        sec = " > ".join(h.unit.section_path[-2:])
        body = (not has_canon) or (KEEP_TOP_SNIPPET and i == 0)
        rest.append({"section": sec, "score": round(h.score, 2), "source": "검색",
                     "html": h.unit.table.to_html(max_rows=25) if h.unit.table else ""}
                    if body else
                    {"section": sec, "score": round(h.score, 2), "source": "검색(목록)"})
    if fin:
        rest.append(fin)
    rest = alt + rest

    bud = _canon_budget(rest, len(found))
    out = [_canon(v, year=y, budget=bud, query=query) for y, v in found]
    if fin:
        out.append(fin)
        rest = [x for x in rest if x is not fin]
    return _fit(out, rest)

KEEP_TOP_SNIPPET = (_os.environ.get("A2_KEEP_TOP") or "0") != "0"

_PERIODIC_Q = (
    (_re.compile(r"1\s*분기|첫\s*분기"), ("quarter", 3)),
    (_re.compile(r"3\s*분기"), ("quarter", 9)),
    (_re.compile(r"2\s*분기"), ("half", 6)),
    (_re.compile(r"4\s*분기"), ("annual", 12)),
    (_re.compile(r"반기|상반기"), ("half", 6)),
    (_re.compile(r"분기보고서|분기\s*기준"), ("quarter", None)),
)


def _periodic_of(query):
    """질의가 지목한 정기공시 종류. 없으면 사업보고서(`annual`)다."""
    q = str(query or "")
    for pat, (sub, mon) in _PERIODIC_Q:
        if pat.search(q):
            return sub, mon
    return "annual", None


@tool("1-2", "1-13", "2-2", "2-3", "2-4", "2-5", "2-6", "2-7")


def find_sections(corp: str, query: str, year: int = None, k: int = 4) -> list:  # noqa: D401
    """공시의 서술 섹션을 찾아 본문을 돌려준다. 설명이나 현황을 묻는 질의에 쓴다.

    ★ 재무제표 수치(매출·영업이익·순이익·자산·부채·자본·판관비·현금흐름·주당이익)는
    get_financials를 쓰십시오. 이 도구가 돌려주는 본문에는 연결과 별도 재무제표가
    함께 들어 있어 별도 값을 연결로 잘못 읽기 쉽습니다.

    예: "주요 제품 및 서비스" "원재료" "생산설비 가동률" "설비의 신설 계획"
    "사업의 개요" "경쟁 현황" "사업장 현황" "직원 등의 현황"

    인자:
        corp: 기업명
        query: 찾을 내용을 한국어로
        year: 사업연도. 안 주면 최신
        k: 섹션 몇 개까지

    반환:
        [{section, text, score}] — text는 섹션 본문
    """
    k = min(k or _KMAX, _KMAX)
    # 정본 표가 걸리면 함께 준다 — 표 질의를 find_sections로 물어오는 경우가 실제로 있다
    name, _miss = _no_corp(corp)
    if _miss:
        return [_miss]
    fin = _fin_block(name, query, year)
    found = [v for tid in _doctables.match(query)
             if (v := _doctables.values(name, tid, year))]
    secs = [v for sid in _docsections.match(query)
            if (v := _docsections.values(name, sid, year, limit=_SEC_LIMIT))]
    hits = _search.search(query, corp=corp, year=year, kind="section", k=k)
    has_canon = bool(found or (fin and not _FIN_NOT_ENOUGH.search(query or "")))
    rest = []
    for i, h in enumerate(hits[:3] if has_canon else hits):
        sec = " > ".join(h.unit.section_path[-2:])
        body = (not has_canon) or (KEEP_TOP_SNIPPET and i == 0)
        rest.append({"section": sec, "score": round(h.score, 2), "source": "검색",
                     "text": h.unit.text[:_SNIPPET]} if body else
                    {"section": sec, "score": round(h.score, 2), "source": "검색(목록)"})
    if fin:
        rest.append(fin)
    # 섹션이 차지할 자리를 **미리 반영해** 표 예산을 잡는다. 안 그러면 나중에
    bud = _canon_budget(rest + secs, len(found))
    out = [_canon(v, budget=bud, query=query) for v in found]
    out.extend(secs)                      # 정본 섹션은 이미 잘라 왔다(_SEC_LIMIT)
    if fin:
        out.append(fin)
        rest = [x for x in rest if x is not fin]
    return _fit(out, rest)


@tool("2-2", "2-4")


def list_sections(corp: str, year: int = None) -> list:
    """그 회사 사업보고서의 최상위 목차를 돌려준다. 어디를 볼지 먼저 정할 때 쓴다.

    정기공시는 I. 회사의 개요 / II. 사업의 내용 / III. 재무에 관한 사항 순이다.
    섹션을 정한 뒤 find_sections·find_tables로 그 안을 뒤지면 정확도가 높다.

    인자:
        corp: 기업명
        year: 사업연도. 안 주면 최신

    반환:
        최상위 섹션 제목 리스트
    """
    return _search.sections(corp, year=year, level=1)


@tool("1-1", "1-2", "1-4")


def calculate(op: "add|subtract|multiply|divide|sum_|growth|ratio|margin|cagr|scale",
              values: list) -> dict:
    """등록된 연산자로 계산한다. 직접 산술하지 말고 이 도구를 쓴다.

    입력에 값이 하나라도 없으면 결과는 None이다. 0으로 채우지 않는다.
    예: op="growth", values=[333605938, 300870903] → 10.88 (증감률 %)

    인자:
        op: add·subtract·multiply·divide·sum_·growth·ratio·margin·cagr·scale 중 하나
        values: 연산자 인자 리스트. growth는 당기·전기 순서

    반환:
        {op, values, result}. result가 None이면 계산 불가
    """
    if op not in _compute.ops():
        return {"op": op, "result": None, "error": f"미등록 연산자. 사용 가능: {_compute.ops()}"}
    try:
        result = _compute.run("sum_", values) if op == "sum_" else _compute.run(op, *values)
    except TypeError as e:
        return {"op": op, "values": values, "result": None, "error": str(e)}
    return {"op": op, "values": values, "result": result}


@tool("1-6", "2-1", "2-8")


def compare_companies(corps: list, concept: str, year: int = None,
                      years: int = 1) -> list:
    """여러 기업의 같은 항목을 나란히 뽑는다. 섹터 비교·다년도 비교를 한 번에 한다.

    concept에 재무 개념(revenue 등)이나 공시 표 이름(가동률·시설투자·주식의 총수·
    직원·종속회사·최대주주)을 줄 수 있다. years를 2 이상으로 주면 연도별로 모은다.
    5개사 3개년도 한 번의 호출로 끝나므로 도구를 여러 번 부르지 마십시오.

    업종이 다르면 값의 성격도 다르다. 결과의 label을 그대로 표시해야 오해가 없다.

    인자:
        corps: 기업명 리스트
        concept: 개념 id 또는 표 이름. 예 "revenue" "가동률" "시설투자"
        year: 기준 사업연도. 안 주면 각 회사의 최신
        years: 최근 몇 개년을 모을지. 기본 1

    반환:
        재무 개념이면 [{corp, value_krw, value_raw, unit, status, label}] —
        value_krw는 원 단위로 환산한 비교 가능한 값이다.
        표 이름이면 [{corp, year, table, unit, rows}] — 표의 핵심 행을 연도별로 모은다
    """
    # ── 공시 표 배치 조회 ──
    # 5개사×3개년 가동률처럼 **여러 기업×여러 해**를 물으면 스텝 예산(3)에 안 맞는다.
    # 멀티에이전트로 가지 않고(arXiv 2604.02460: 토큰을 같게 두면 단일 에이전트가 대등·우위)
    # **한 번의 도구 호출**로 팬아웃을 결정론으로 처리한다.
    tids = _doctables.match(concept) if concept else []
    if tids and concept not in _ALIAS and _cid(concept) not in _K.BY_ID:
        tid = tids[0]
        out = []
        for name in list(corps)[:10]:
            c = _facts.company(name)
            if c is None:
                out.append({"corp": name, "status": "코퍼스에 없는 기업",
                            "note": "이 회사는 제공 코퍼스(70개사)에 없습니다. "
                                    "답변에서 언급하지 마십시오."})
                continue
            nm = c["corp_name"]
            base = year or _store.latest_fiscal_year(nm) or 0
            got = False
            for y in range(base - years + 1, base + 1):
                v = _doctables.values(nm, tid, y)
                if not v:
                    continue
                got = True
                keys = [r for r in v["rows"] if r.get("key")] or v["rows"]
                rec = {"corp": nm, "year": y, "table": v["table"],
                       "unit": v["unit"], "unit_note": v["unit_note"],
                       "rows": keys[:8]}
                nz = _canon_norm(v, keys)
                if nz:
                    rec["원단위_환산"] = nz
                    rec["환산_note"] = ("회사마다 표 단위가 다릅니다. **비교할 때는 위 "
                                        "`원단위_환산`을 쓰십시오.** 답변에는 원문 단위 "
                                        "표기를 그대로 적되 자릿수를 이 환산으로 확인하십시오.")
                out.append(rec)
            if not got:
                out.append({"corp": nm, "status": "해당 표 없음",
                            "note": f"이 회사의 사업보고서에 '{concept}' 표가 없습니다."})
        return out
    cid = _cid(concept)
    if years > 1:
        # 섹터 3개년 비교 — 기업마다 시계열을 통째로 준다. 한 번의 호출로 끝난다.
        out = []
        for name in list(corps)[:10]:
            c = _facts.company(name)
            nm = c["corp_name"] if c else name
            out.append({"corp": nm, "concept": cid,
                        "series": _finance.series(nm, cid, years=years),
                        "unit": (_finance.extract(nm, year=year) or {}).get("units", {})
                                and _unit_label(_finance.extract(nm, year=year)["values"],
                                                _finance.extract(nm, year=year).get("krw", {}))})
        return out
    out = []
    for name in list(corps)[:10]:
        r = _finance.extract(name, year=year)
        v = r["values"].get(concept)
        u = (r.get("units") or {}).get(concept)
        # XBRL 경로엔 units가 없고 krw가 있다. 이걸 안 보면 백만원 표기 회사가
        # 원 단위 회사와 100만 배 차이로 나란히 서서 비교가 통째로 깨진다.
        kr = (r.get("krw") or {}).get(concept)
        if kr is None and v is not None:
            kr = v * ((u.scale if u else None) or 1)
        _c3 = (_doctables._cite({"report_nm": r.get("period"), "rcept_no": r.get("rcept_no"),
                                 "is_correction": r.get("is_correction")})
               if r.get("rcept_no") else None)
        out.append({"corp": r.get("corp", name),
                    **({"출처": _c3} if _c3 else {}),
                    "value_krw": kr,
                    "value_raw": v,
                    "unit": (u.raw if u else _unit_label(r.get("values", {}),
                                                         r.get("krw", {}))),
                    "status": r["status"].get(concept),
                    "label": (_K.BY_ID[concept].labels[0]
                              if concept in _K.BY_ID and _K.BY_ID[concept].labels
                              else concept)})
    out.sort(key=lambda x: (x["value_krw"] is None, -(x["value_krw"] or 0)))
    return out


@tool("1-10", "1-11")


def correction_history(corp: str,
                       doc_group: "periodic|major|exchange|holding" = None,
                       year: int = None, start: str = None, end: str = None,
                       limit: int = 10) -> list:
    """정정공시 이력 — 원공시와 정정본을 잇고 **무엇이 바뀌었는지**까지 준다.

    정정본이 유효본입니다. 계약·지분 질의는 반드시 이걸 먼저 확인하십시오.

    ★ **"정정신고가 몇 번?"은 첫 항목의 `계수`가 답입니다.** 목록을 세지 마십시오 —
      `limit`에 잘립니다(기본 10건인데 한화오션은 36건입니다).

    ★ **기간이 여러 해에 걸치면 start·end를 쓰십시오.** year는 **한 해만** 거릅니다.
      "2024년~2026년 3월"이면 start="2024-01-01", end="2026-03-31"입니다.

    인자:
        corp: 기업명
        doc_group: periodic·major·exchange·holding. 안 주면 전부
        year: 접수 연도 **한 해**만 볼 때
        start: 기간 시작일 "YYYY-MM-DD". 여러 해에 걸친 질의에 쓰십시오
        end: 기간 종료일 "YYYY-MM-DD"
        limit: 최대 건수(최신순)

    반환:
        [{계수:{답, 유형, 기간, note}}, {정정공시, 접수일, 출처, 체인, 정정항목:[…]}, …]
        정정항목은 **공시가 표로 적어 둔 것**이지 우리가 비교해 만든 것이 아닙니다
    """
    c = _facts.company(corp)
    if c is None:
        return [{"status": "코퍼스에 없는 기업", "corp": corp}]
    _lo = _re.sub(r"[^0-9]", "", str(start))[:8].ljust(8, "0") if start else None
    _hi = _re.sub(r"[^0-9]", "", str(end))[:8].ljust(8, "9") if end else None
    rows = [r for r in _store.manifest()
            if r["corp_name"] == c["corp_name"] and r.get("is_correction")
            and (not doc_group or r["doc_group"] == doc_group)
            and (not year or (r.get("rcept_dt") or "").startswith(str(year)))
            and (not _lo or str(r.get("rcept_dt") or "") >= _lo)
            and (not _hi or str(r.get("rcept_dt") or "") <= _hi)]
    rows.sort(key=lambda r: r.get("rcept_dt") or "", reverse=True)

    # periodic은 manifest 그룹핑이 정답 — 미리 (그룹키 → 판본목록) 색인을 만든다.
    pgroups = {}
    if any(r["doc_group"] == "periodic" for r in rows):
        for grp in _facts.corrections(c["corp_name"], doc_group="periodic"):
            for v in grp.get("versions", []):
                pgroups[v["rcept_no"]] = grp.get("versions", [])

    def _chain_of(r):
        """그룹별로 **되는 방법만** 쓴다. 안 되면 지어내지 않는다."""
        g = r["doc_group"]
        if g == "exchange":
            try:
                ch = _corrections.chain(r)
            except Exception:
                ch = [r]
            return ch, None
        if g == "periodic":
            ch = pgroups.get(r["rcept_no"]) or [r]
            return ch, None
        return [r], ("이 공시 유형은 원공시 연결이 불가능합니다 — 서식에 "
                     "`정정관련 공시서류제출일`이 없고 기준기간도 비어 있습니다. "
                     "정정항목만 확인하십시오.")

    from agent2.tools import filingtypes as _ft2
    _cl = _lo or (f"{year}0101" if year else _ft2.QUESTION_PERIOD[0])
    _ch = _hi or (f"{year}1231" if year else _ft2.QUESTION_PERIOD[1])
    _n = sum(1 for r in rows if _cl <= str(r.get("rcept_dt") or "") <= _ch)
    out = [{"계수": {"답": _n, "유형": "정정신고(전 유형)",
                   "기간": f"{_cl}~{_ch}",
                   "note": "제목에 [기재정정]이 붙은 공시 전 유형 합계입니다. "
                           "'몇 번'의 답은 이 수치이며, 아래 목록은 최신 "
                           f"{min(limit, len(rows))}건만 보여 줍니다."}}]
    for r in rows[:limit]:
        ch, note = _chain_of(r)
        try:
            dif = _corrections.diff(r)
        except Exception:                      # 보조 근거다 — 실패해도 목록은 준다
            dif = []
        item = {
            "정정공시": r.get("report_nm"),
            "접수일": r.get("rcept_dt"),
            "출처": _doctables._cite(r),
            "체인": [f"{x.get('rcept_dt')}({'정정' if x.get('is_correction') else '원본'})"
                    for x in ch],
            "정정항목": dif or "이 문서는 정정항목 표를 싣지 않았습니다",
        }
        if note:
            item["연결_한계"] = note
        out.append(item)
    if not rows:                       # `out`은 계수 때문에 항상 비지 않는다 — rows로 본다
        return [{"status": "정정공시 없음", "corp": c["corp_name"], "계수": {"답": 0},
                 "note": "이 기업·조건에는 정정공시가 없습니다. 원공시가 곧 유효본입니다."}]
    return out


def _sync_concept_guide():
    """재무 도구 설명의 **개념 목록을 `_ALIAS`에서 생성**해 넣는다."""
    seen, names = set(), []
    for kr, cid in _ALIAS.items():          # dict는 삽입 순서 보존
        if cid not in seen:
            seen.add(cid)
            names.append(kr)
    guide = "다루는 개념: " + "·".join(names) + "."
    if len(guide) > registry.GUIDE_MAX:     # 상한을 넘으면 조용히 잘리지 않게 알린다
        raise ValueError(f"개념 지침 {len(guide)}자 > GUIDE_MAX {registry.GUIDE_MAX}자 — "
                         f"_ALIAS가 커졌다. 상한을 올리거나 표기를 줄일 것")
    for name in ("financial_series", "get_financials"):
        registry.get(name).guide = guide
    return guide

_CONCEPT_GUIDE = _sync_concept_guide()
