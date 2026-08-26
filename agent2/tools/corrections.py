"""정정공시 체인 — **원공시와 정정본을 잇는다.**

## 왜 필요한가

주최 테크세션(2026-08-06)이 별도 항목으로 못 박았다:
  *"원공시 이후 정정공시가 발생하면 변경된 내용을 원공시와 연결하여 업데이트 필요"*
  *"정정공시 관리 테이블을 별도 구성하여 에이전트가 정정 여부를 확인하도록 설계"*
자료의 사례가 정확히 우리 코퍼스에 있다 — 삼성전자 단일판매·공급계약에서
**계약상대가 `글로벌 대형기업` → `테슬라`로 정정**된 건이다.

규모(코퍼스 전수):
    거래소공시   원본 838 · 정정 **631**  ← 43%가 정정본이다
    주요사항보고 원본 425 · 정정 173
    정기공시     원본 895 · 정정 159
    지분공시     원본 1,042 · 정정 41
정정을 무시하면 계약 질의의 상당수가 **낡은 값**을 답한다.

## 어떻게 잇나 — 추정하지 않는다

`manifest.jsonl`만으로는 못 잇는다. 거래소공시는 `base_year`·`base_month`가 비어 있어
(기업·유형·기준기간) 그룹핑이 성립하지 않는다.

**정정 문서가 원공시를 스스로 지목한다.** 서식에 링크 필드가 있다:

    정정일자                   2025-07-31
    1. 정정관련 공시서류         단일판매·공급계약 체결
    2. 정정관련 공시서류제출일    2025-07-28     ← 이것이 원공시 접수일
    4. 정정사항  정정항목 / 정정전 / 정정후

**전수 실측(정정본 1,004건 전건, 2026-08-15): 1,003건(99.9%)에서 링크를 읽는다.**
필드는 문서군마다 다르다 — 거래소는 `정정관련 공시서류제출일`(631/631),
주요사항·지분·정기는 `정정대상 공시서류의 최초제출일`(372/373).
★ 종전 주석은 **거래소 표본 6건**만 보고 "5건에서 읽었다"고 적었고, 코드도 거래소
  필드 하나만 읽었다. 표본이 문서군을 대표하지 못한 전형적인 사례다(§6-9).

못 읽는 1건(미래에셋증권 자기주식취득결정 정정)은 시간 근접으로 떨어뜨리지 않고
**연결 실패로 남긴다** — 틀린 연결이 연결 없음보다 나쁘다
(계약상대를 엉뚱한 계약에 붙이게 된다).

## 무엇을 돌려주나

    chain(row)      그 문서가 속한 정정 체인 [원본 … 최신정정]
    effective(row)  그 체인의 **유효본**(가장 늦은 정정본)
    diff(row)       정정 문서가 밝힌 정정항목 [(항목, 정정전, 정정후)]

`diff`는 우리가 비교해 만드는 게 아니라 **공시가 표로 적어 둔 것**을 읽는다.
우리가 두 문서를 비교하면 서식 차이까지 변경으로 잡는다.

실행: python3 -m agent2.tools.corrections 삼성전자
"""
import re
from functools import lru_cache

from agent2.data import source, store

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

#: 날짜 표기. 코퍼스에 **두 형식이 섞여 있다** — `2025-07-28`과 `2024년 11월 18일`.
#: 월·일이 한 자리인 경우도 있어 `\d{1,2}`로 받고 뒤에서 0을 채운다.
_D = r"(\d{4})\s*[-.년]\s*(\d{1,2})\s*[-.월]\s*(\d{1,2})"

#: 정정 문서가 지목하는 **원공시 제출일**. 링크 필드가 **둘이다**(전수 실측 2026-08-15):
#:
#:     doc_group   계    정정관련 공시서류제출일   정정대상 공시서류의 최초제출일
#:     exchange   631         631                    0
#:     holding     41           0                   41
#:     major      173           0                  172
#:     periodic   159           0                  159
#:
#: ★ 앞 필드는 **거래소 전용**이다(정기·주요사항·지분에 0건). 종전 코드는 그 하나만
#:   읽어서 거래소 외 373건이 통째로 연결되지 않았다. 게다가 한글 날짜를 안 받아
#:   거래소에서도 15건을 놓쳤다 — **616/1,004(61.4%) → 1,003/1,004(99.9%)**.
#:   `CLAUDE.md` §7은 2026-08-13에 이미 "링크 필드가 둘"로 정정돼 있었다.
#:   **문서만 고치고 코드를 안 고치면 이렇게 된다.**
_ORIG_DT = re.compile(
    r"(?:정정관련\s*공시서류\s*제출일|정정대상\s*공시서류의?\s*최초제출일)"
    r"[^0-9]{0,80}" + _D)
#: 정정 자체의 일자(보조 — 링크에는 쓰지 않는다).
_FIX_DT = re.compile(r"정정일자[^0-9]{0,80}" + _D)


def _flat(row):
    return _WS.sub(" ", _TAG.sub(" ", source.text(row) or ""))


@lru_cache(maxsize=512)
def origin_rcept_dt(doc_id):
    """정정 문서 → **원공시 접수일**(YYYYMMDD). 못 읽으면 None.

    ★ 못 읽었을 때 시간 근접으로 추측하지 않는다. 틀린 연결은 계약상대·금액을
      엉뚱한 계약에 붙이므로, 연결 없음보다 나쁘다.
    """
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
    """그 문서가 속한 정정 체인 — 접수일 오름차순 [원본 … 최신정정].

    같은 (기업·공시유형) 안에서 **정정 문서가 지목한 원공시 접수일**로 잇는다.
    지목을 못 읽은 정정본은 체인에 넣지 않는다(조용한 오연결 금지).
    """
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


#: 정정사항 표. 서식이 **두 가지**다(2026-08-26 전수 확인 · 정정본 1,004건):
#:     3열  `정정항목 | 정정전 | 정정후`                              행 2,559개
#:     5열  `항 목 | 정정요구ㆍ명령관련 여부 | **정정사유** | 정정전 | 정정후`  행 **701개**
#: ★ 모집단을 정확히 적는다(§6-24) — **5열 표를 실제로 갖는 문서는 201/1,004건(20.0%)**이고
#:   그 701행 중 **698행에 사유가 채워져 있다**(빈칸 3). 처음에 "1,001건(99.7%)"이라고
#:   적었던 것은 표 안에 `정정사유`라는 **헤더 문자열**이 있는지만 센 값이라 틀렸다 —
#:   헤더는 있는데 데이터 행이 3열인 문서가 대부분이다.
#: 사유 값 상위: 단순기재오류 97 · 단순 기재 오류 68 · `-` 45 · 단순 기재오류 43 ·
#:              미반영분 포함 30 · 단순 오기재 25 · 재무제표재작성 24
_DIFF_ROW = re.compile(r"<TR\b.*?</TR>", re.S | re.I)
_DIFF_CELL = re.compile(r"<TD\b[^>]*>(.*?)</TD>", re.S | re.I)
#: 5열 서식에서 버릴 열(정정 내용이 아니라 서식 상용구).
_DIFF_SKIP = ("정정요구", "명령관련")


def diff(row):
    """정정 문서가 **스스로 밝힌** 정정항목 [{항목, 정정사유, 정정전, 정정후}].

    우리가 두 문서를 비교해 만들지 않는다 — 서식 차이까지 변경으로 잡힌다.

    ★★★ **5열 서식을 통째로 버리고 있었다**(2026-08-26). 판정이 `len(cells) == 3`이라
      `항 목 | 정정요구ㆍ명령관련 여부 | 정정사유 | 정정전 | 정정후` 5열 표가 전부 탈락했다.
      실측: **정정본 1,004건 중 299건(29.8%)이 `diff()`에서 빈손**으로 나왔고,
      그 문서들은 관측에 `"이 문서는 정정항목 표를 싣지 않았습니다"`라고 나갔다 —
      **표는 있었다.**

      그리고 그 5열에만 있는 것이 **`정정사유`**다. 검색-18(고려아연 "가장 최근
      정정신고 내역과 정정 **사유**")의 정답이 `단위 정정`인데, 원문에 그 문자열이
      그대로 있다:
          `항 목 … | 정정사유 = **단위 정정** | 정정 전 … | 정정 후 …`
      CLAUDE.md는 이 문항을 *"원문에 '정정사유' 문자열이 없다"*는 **코퍼스 한계**로 적어
      두었는데 **틀렸다** — 우리가 안 읽었을 뿐이다.

    ★ 고친 뒤 전수(정정본 1,004건): 빈손 문서 **299 → 112건**(−187) ·
      정정사유를 담은 문서 **0 → 200건** · 총 정정항목 2,956개.

    ★ 반환 형태를 튜플 → dict로 바꾼다. 3열 서식에는 `정정사유`가 없으므로
      키를 빼서 **없는 것을 지어내지 않는다**(호출부는 `.get`으로 읽는다).
    """
    if not row.get("is_correction"):
        return []
    raw = source.text(row) or ""
    # ★ **`정정항목/정정전/정정후` 헤더를 가진 표 안에서만** 읽는다.
    #   문서 전체에서 3열 행을 긁었더니 `작성책임자 (직책) → (성명)` 같은
    #   서식 행이 정정사항으로 잡혔다(실측: 삼성전자 주요사항보고 3건).
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


#: ★ `<TABLE\b` 는 `<TABLE-GROUP` 도 매치한다(`\b`가 `E`와 `-` 사이 경계다).
#:   여기서는 결과가 안 바뀌지만(전수 확인) 같은 함정을 남기지 않는다 — `parse.py` §_TABLE 참조.
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
