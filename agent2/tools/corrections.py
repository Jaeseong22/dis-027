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
        for m in _DIFF_ROW.finditer(body):
            cells = [_WS.sub(" ", _TAG.sub(" ", c)).strip()
                     for c in _DIFF_CELL.findall(m.group())]
            if len(cells) == 3:
                item, before, after, reason = cells[0], cells[1], cells[2], ""
            elif len(cells) == 5:
                # 가운데 두 열이 `정정요구ㆍ명령관련 여부`와 `정정사유`다.
                item, before, after = cells[0], cells[3], cells[4]
                reason = next((c for c in cells[1:3]
                               if c and not any(s in c for s in _DIFF_SKIP)
                               and c not in ("예", "아니오")), "")
            else:
                continue
            if not item or item in ("정정항목", "항 목", "항목") or before == "정정전":
                continue
            out.append({"항목": item, **({"정정사유": reason} if reason else {}),
                        "정정전": before, "정정후": after})
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
