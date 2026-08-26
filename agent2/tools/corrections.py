"""정정공시 체인 — **원공시와 정정본을 잇는다.**"""
import re
from functools import lru_cache

from agent2.data import source, store

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

_D = r"(\d{4})\s*[-.년]\s*(\d{1,2})\s*[-.월]\s*(\d{1,2})"

_ORIG_DT = re.compile(
    r"(?:정정관련\s*공시서류\s*제출일|정정대상\s*공시서류의?\s*최초제출일)"
    r"[^0-9]{0,80}" + _D)
#: 정정 자체의 일자(보조 — 링크에는 쓰지 않는다).
_FIX_DT = re.compile(r"정정일자[^0-9]{0,80}" + _D)


def _flat(row):
    return _WS.sub(" ", _TAG.sub(" ", source.text(row) or ""))


@lru_cache(maxsize=512)


def origin_rcept_dt(doc_id):
    """정정 문서 → **원공시 접수일**(YYYYMMDD). 못 읽으면 None."""
    row = store.by_doc_id(doc_id) if hasattr(store, "by_doc_id") else None
    if row is None:
        row = next((r for r in store.manifest() if r["doc_id"] == doc_id), None)
    if row is None or not row.get("is_correction"):
        return None
    m = _ORIG_DT.search(_flat(row))
    if not m:
        return None
    y, mo, d = m.groups()          # 한 자리 월·일이 있어 0을 채운다(`2024년 1월 5일`)
    return f"{y}{int(mo):02d}{int(d):02d}"


def chain(row):
    """그 문서가 속한 정정 체인 — 접수일 오름차순 [원본 … 최신정정]."""
    sibs = [r for r in store.manifest()
            if r["corp_name"] == row["corp_name"]
            and r["doc_subtype"] == row["doc_subtype"]
            and r["doc_group"] == row["doc_group"]]
    by_dt = {}
    for r in sibs:
        by_dt.setdefault(r["rcept_dt"], []).append(r)

    # 시작점(원본) 찾기 — 자기 자신부터 원공시 지목을 거슬러 올라간다.
    seen, cur = set(), row
    while cur.get("is_correction"):
        dt = origin_rcept_dt(cur["doc_id"])
        if not dt or dt in seen or dt not in by_dt:
            break
        seen.add(dt)
        nxt = next((r for r in by_dt[dt] if not r.get("is_correction")), by_dt[dt][0])
        if nxt["doc_id"] == cur["doc_id"]:
            break
        cur = nxt
    root = cur

    # 그 원본을 지목하는 정정본들을 모은다(정정본이 또 정정될 수 있어 반복한다).
    members, frontier = [root], {root["rcept_dt"]}
    changed = True
    while changed:
        changed = False
        for r in sibs:
            if not r.get("is_correction") or r in members:
                continue
            if origin_rcept_dt(r["doc_id"]) in frontier:
                members.append(r)
                frontier.add(r["rcept_dt"])
                changed = True
    return sorted(members, key=lambda r: r["rcept_dt"])


def effective(row):
    """그 문서의 **유효본** — 체인에서 가장 늦은 것. 정정이 없으면 자기 자신."""
    return chain(row)[-1]

_DIFF_ROW = re.compile(r"<TR\b.*?</TR>", re.S | re.I)
_DIFF_CELL = re.compile(r"<TD\b[^>]*>(.*?)</TD>", re.S | re.I)
#: 5열 서식에서 버릴 열(정정 내용이 아니라 서식 상용구).
_DIFF_SKIP = ("정정요구", "명령관련")

#: 헤더 판정 — TD와 TH를 함께 본다(헤더 행을 TH로 쓴 문서가 많다).
_HDR_CELL = re.compile(r"<T[DH]\b[^>]*>(.*?)</T[DH]>", re.S | re.I)
#: 사유 자리에 온 수치·대시. 정정사유는 문장이지 숫자나 `-`가 아니다.
_NUMISH = re.compile(r"[-+(]?[\d,]+(?:\.\d+)?\)?%?")
_DASH_ONLY = re.compile(r"[-\u2013\u2014\u2010\u2015\s]+")


def _norm(t):
    """공백을 지운 비교용 문자열. `항 목`처럼 자간을 벌린 헤더가 많다."""
    return _WS.sub("", str(t or ""))


def _header_cols(body):
    """정정표 헤더 행의 열 수. 못 찾으면 0.

    서식이 셋이고 사유 열 위치가 서로 다르다(전수 실측 · 정정표 1,045개):
        3열 635표  `정정항목 | 정정전 | 정정후`
        4열 182표  `항목 | 정정사유 | 정정전 | 정정후`
        5열 220표  `항목 | 정정요구ㆍ명령관련여부 | 정정사유 | 정정전 | 정정후`
    데이터 행의 TD 수만으로는 못 가른다 — 4열 서식은 정정전/후 칸에 중첩표가 있어
    안쪽 TD가 잡힌다. 헤더를 봐야 한다.
    """
    for m in _DIFF_ROW.finditer(body):
        cs = [_norm(c) for c in _HDR_CELL.findall(m.group())]
        joined = "".join(cs)
        if "정정전" in joined and "정정후" in joined:
            return len(cs)
    return 0


def diff(row):
    """정정 문서가 **스스로 밝힌** 정정항목 [{항목, 정정사유, 정정전, 정정후}]."""
    if not row.get("is_correction"):
        return []
    raw = source.text(row) or ""
    out = []
    for tb in _TABLE.finditer(raw):
        body = tb.group()
        flat = _WS.sub("", _TAG.sub("", body))
        if "정정전" not in flat or "정정후" not in flat:
            continue
        n_hdr = _header_cols(body)
        for m in _DIFF_ROW.finditer(body):
            cells = [_WS.sub(" ", _TAG.sub(" ", c)).strip()
                     for c in _DIFF_CELL.findall(m.group())]
            k = len(cells)
            # 서식별로 읽는다 — **TD 수 == 헤더 열 수인 행만 진짜다**.
            #   4열 서식에서 진짜 행(TD=4) 487개가 버려지고 중첩표 조각(TD=3) 245개가
            #   정정항목처럼 나가던 것을 고친다. 5열 서식에서는 사유를 3번째 열로
            #   못 박는다 — 2번째는 `정정요구ㆍ명령관련 여부`라 `-`·`N`·`아니요`가
            #   사유로 새어 나갔다(986행 중 145행 오염).
            reason = ""
            if n_hdr == 5 and k == 5:
                item, reason, before, after = cells[0], cells[2], cells[3], cells[4]
            elif n_hdr == 4 and k == 4:
                item, reason, before, after = cells[0], cells[1], cells[2], cells[3]
            elif n_hdr == 4 and k == 5:
                # 정정후 칸에 중첩표가 있는 진짜 행. 그 자리 값은 정정 전후가 아니라
                # 중첩표의 세부항목 라벨과 값이라 주지 않는다.
                item, reason, before, after = cells[0], cells[1], "", ""
            elif n_hdr in (0, 3) and k == 3:
                item, before, after = cells[0], cells[1], cells[2]
            else:
                continue
            if not item or _norm(item) in ("정정항목", "항목") or _norm(before) == "정정전":
                continue
            if (_norm(reason) == "정정사유" or _NUMISH.fullmatch(reason)
                    or _DASH_ONLY.fullmatch(reason)):
                reason = ""
            out.append({"항목": item,
                        **({"정정사유": reason} if reason else {}),
                        **({"정정전": before, "정정후": after} if before or after else {})})
    return out

_TABLE = re.compile(r"<TABLE(?![A-Za-z0-9_-]).*?</TABLE>", re.S | re.I)

if __name__ == "__main__":
    import sys
    corp = sys.argv[1] if len(sys.argv) > 1 else "삼성전자"
    rows = [r for r in store.manifest()
            if r["corp_name"] == corp and r["is_correction"]]
    print(f"{corp} 정정공시 {len(rows)}건")
    for r in rows[:6]:
        ch = chain(r)
        print(f"\n  [{r['doc_group']}] {r['report_nm'][:44]}")
        print(f"    체인 {' → '.join(x['rcept_dt'] + ('(정정)' if x['is_correction'] else '(원본)') for x in ch)}")
        for d in diff(r)[:3]:
            why = f" [{d['정정사유']}]" if d.get("정정사유") else ""
            print(f"    · {d['항목'][:24]}{why}: "
                  f"{d['정정전'][:30]!r} → {d['정정후'][:30]!r}")
