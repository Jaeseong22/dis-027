"""정형공시 정규화 배치 — 주요사항·거래소·지분 3,150건을 레코드로 접는다."""
import collections
import json
import os
import re
import time

from agent2 import config
from agent2.data import parse as P
from agent2.data import source
from agent2.data import store

_SP = re.compile(r"[\s　]+")
N = lambda s: _SP.sub("", str(s or ""))

#: 배치 산출물. 코퍼스 밖에 쓴다(문서 폴더에 파일을 만들면 `n_files` 무결성이 깨진다).
OUT_DIR = os.path.join(config.AGENT_DIR, ".cache", "filings")
RECORDS = os.path.join(OUT_DIR, "records.jsonl")
SCHEMA = os.path.join(OUT_DIR, "schema.json")

#: 정형공시 3종. 정기공시는 정본표(`doctables`)가 맡는다.
GROUPS = ("major", "exchange", "holding")

_LINK_LABELS = ("정정관련공시서류제출일", "정정대상공시서류의최초제출일", "최초제출일")
_DATE = re.compile(r"(\d{4})\D{0,3}(\d{1,2})\D{0,3}(\d{1,2})")


def form_of(row):
    """서식명 정규화 — `[기재정정]`·`(자율공시)` 등 접두·접미를 떼고 **종류**만 남긴다."""
    s = re.sub(r"^\[[^]]+\]", "", row["report_nm"]).strip()
    s = re.sub(r"\((자율공시|공정공시|안내공시)\)", "", s).strip()
    if s.startswith("투자판단관련주요경영사항"):
        return "투자판단관련주요경영사항"
    return s


def _fold(grid_row):
    """rowspan/colspan으로 복제된 연속 중복 셀을 접는다."""
    out = []
    for c in (str(x).strip() for x in grid_row):
        if not out or N(c) != N(out[-1]):
            out.append(c)
    return out


def _segments(folded, vocab, vals=None):
    """행을 `라벨런 → 값런` 구간으로 쪼개 [(라벨, 값)]을 낸다."""
    def _is_label(c):
        return N(c) in vocab or (vals is not None and N(c) not in vals)

    out, labs, run = [], [], []
    for c in folded:
        if not c:
            continue
        if _is_label(c):
            if run:                       # 값런이 끝났다 — 구간 확정
                out.append((" > ".join(labs), run[0]))
                labs, run = [], []
            labs.append(c)
        elif labs:
            run.append(c)
    if labs and run:
        out.append((" > ".join(labs), run[0]))
    return out

#: 라벨 어휘 임계값 — 그 서식의 문서 중 몇 %에 나타나야 라벨로 보는가.
#:
_LABEL_THRESHOLD = 0.9


def cell_sets(rows, verbose=False):
    """문서별 셀 문자열 집합. 라벨 어휘 계산의 입력이다."""
    out = []
    for i, m in enumerate(rows, 1):
        seen = set()
        try:
            for d in P.parse_doc(m):
                for t in d.tables:
                    for g in t.grid:
                        seen.update(N(c) for c in _fold(g) if c)
        except Exception:
            pass
        out.append(seen)
        if verbose and i % 500 == 0:
            print(f"  어휘 스캔 {i}/{len(rows)}", flush=True)
    return out

#: 라벨 어휘에서 **반드시 빼는** 문자열 — 자리표시자.
#:
_PLACEHOLDER = {"-", "–", "—", "", "해당없음", "해당사항없음"}


def vocab_from(sets):
    """셀 집합들 → 라벨 어휘. 등장률 ≥ `_LABEL_THRESHOLD`. 자리표시자는 뺀다."""
    cnt = collections.Counter()
    for s in sets:
        cnt.update(s)
    n = max(1, len(sets))
    return {c for c, v in cnt.items()
            if v >= n * _LABEL_THRESHOLD and c not in _PLACEHOLDER}


def fields_of(row, vocab):
    """문서 → {라벨: 값}. 먼저 나온 라벨을 유지한다."""
    out = {}
    try:
        docs = P.parse_doc(row)
    except Exception:
        return out, 0
    n_tab = 0
    for d in docs:
        # 거래소공시(html/xforms)만 값 집합을 갖는다. 없으면 두 번째 패스가 생략된다.
        xv = (getattr(d, "meta", None) or {}).get("xforms_values")
        vals = {N(x) for x in xv} if xv else None
        for t in d.tables:
            n_tab += 1
            for pass_vals in ((None, vals) if vals is not None else (None,)):
                for g in t.grid:
                    if len(g) < 2:
                        continue
                    for key, val in _segments(_fold(g), vocab, pass_vals):
                        if not key or not val or len(key) > 90 or N(key) == N(val):
                            continue
                        out.setdefault(key, val)
    return out, n_tab

#: 명부 표로 보존할 최대 행 수. 특별관계자가 수백 명인 보고서가 있다(삼성전자 15명,
#: 셀트리온 105명). 전부 담으면 레코드가 커지고 관측 예산을 넘는다.
_ROSTER_MAX_ROWS = 40


def rosters_of(row, vocab):
    """명부 표를 **행 구조 그대로** 보존한다 → [{headers, rows}]."""
    out = []
    try:
        docs = P.parse_doc(row)
    except Exception:
        return out
    for d in docs:
        for t in d.tables:
            gs = [_fold(g) for g in t.grid]
            if len(gs) < 3:
                continue
            hdr = [i for i, g in enumerate(gs[:4])
                   if g and all(N(c) in vocab for c in g if c)]
            if not hdr:
                continue
            h = hdr[-1]
            body = [g for g in gs[h + 1:] if any(str(c).strip() for c in g)]
            if len(body) < 2:
                continue
            col_sums = {}
            for j, head in enumerate(gs[h][:12]):
                vals = []
                for g in body:
                    if j < len(g):
                        v = str(g[j]).replace(",", "").strip()
                        if re.fullmatch(r"-?\d+", v):
                            vals.append(int(v))
                if vals:
                    col_sums[str(head)[:30]] = {"sum": sum(vals), "n": len(vals)}
            out.append({"headers": gs[h][:12],
                        "rows": [g[:12] for g in body[:_ROSTER_MAX_ROWS]],
                        "n_rows": len(body), "col_sums": col_sums})
    return out

#: 링크 필드를 **본문에서** 찾는 정규식. `fields`는 표에서만 나오는데
#: 주요사항·지분·정기 정정본은 이 줄이 **표가 아니라 산문**이다:
#:     `2. 정정대상 공시서류의 최초제출일 : 2024년 11월 18일`
_LINK_TEXT = re.compile(
    r"(?:정정관련\s*공시서류\s*제출일|정정대상\s*공시서류의?\s*최초제출일)"
    r"[^0-9]{0,80}(\d{4})\s*[-.년]\s*(\d{1,2})\s*[-.월]\s*(\d{1,2})")
_TAGS = re.compile(r"<[^>]+>")


def link_date(fields, row=None):
    """정정본이 가리키는 원공시 제출일 `YYYYMMDD`. 못 찾으면 None."""
    for k, v in fields.items():
        if any(lab in N(k) for lab in _LINK_LABELS):
            m = _DATE.search(str(v))
            if m:
                return f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    if row is None:
        return None
    try:
        txt = _TAGS.sub(" ", source.text(row) or "")
    except Exception:
        return None
    m = _LINK_TEXT.search(txt)
    return f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}" if m else None


def amendments(row):
    """정정본의 정정 표 → {정정항목: (정정전, 정정후)}."""
    out = {}
    try:
        docs = P.parse_doc(row)
    except Exception:
        return out
    for d in docs:
        for t in d.tables:
            hi = next((i for i, g in enumerate(t.grid)
                       if "정정전" in N(" ".join(g)) and "정정후" in N(" ".join(g))), None)
            if hi is None:
                continue
            for g in t.grid[hi + 1:]:
                if len(g) < 3:
                    continue
                item = str(g[0]).strip()
                before, after = str(g[-2]).strip(), str(g[-1]).strip()
                if item and after and N(item) not in ("정정항목",):
                    out[item] = (before, after)
    return out


def build(verbose=True):
    """3,150건을 레코드로 만들고 정정을 반영한다. (레코드 수, 소요초)."""
    t0 = time.time()
    rows = [m for m in store.manifest() if m["doc_group"] in GROUPS]

    # ── 라벨 어휘를 **서식 × 정정여부**로 나눠 만든다. 라벨/값 판별이 여기에 달려 있다.
    #
    by_form = collections.defaultdict(list)
    for m in rows:
        by_form[(form_of(m), bool(m["is_correction"]))].append(m)
    vocabs = {}
    for j, (key, ms) in enumerate(sorted(by_form.items(), key=lambda kv: -len(kv[1])), 1):
        vocabs[key] = vocab_from(cell_sets(ms))
        if verbose and j % 10 == 0:
            print(f"  라벨 어휘 {j}/{len(by_form)}조합", flush=True)
    if verbose:
        print(f"  서식×정정 {len(by_form)}조합 · 라벨 어휘 중앙 "
              f"{sorted(len(v) for v in vocabs.values())[len(vocabs)//2]}개 "
              f"({time.time()-t0:.0f}s)", flush=True)

    recs, by_orig = [], {}
    for i, m in enumerate(rows, 1):
        vocab = vocabs[(form_of(m), bool(m["is_correction"]))]
        f, n_tab = fields_of(m, vocab)
        rec = {"doc_id": m["doc_id"], "rcept_no": m["rcept_no"],
               "rcept_dt": str(m["rcept_dt"]), "corp_name": m["corp_name"],
               "corp_code": m["corp_code"], "doc_group": m["doc_group"],
               "form": form_of(m), "is_correction": bool(m["is_correction"]),
               "corrects": link_date(f, m) if m["is_correction"] else None,
               "n_tables": n_tab, "fields": f,
               "rosters": rosters_of(m, vocab)}
        recs.append(rec)
        if not m["is_correction"]:
            by_orig.setdefault((m["corp_name"], rec["form"], rec["rcept_dt"]), rec)
        if verbose and i % 400 == 0:
            el = time.time() - t0
            print(f"  {i}/{len(rows)}  {el:.0f}s  (남은 예상 {el/i*(len(rows)-i):.0f}s)",
                  flush=True)

    # ── 정정 반영 (규칙 2)
    n_applied = n_orphan = 0
    for rec in recs:
        if not rec["is_correction"] or not rec["corrects"]:
            continue
        key = (rec["corp_name"], rec["form"], rec["corrects"])
        orig = by_orig.get(key)
        if orig is None:
            rec["orphan"] = True
            n_orphan += 1
            continue
        row = next(m for m in rows if m["rcept_no"] == rec["rcept_no"])
        for item, (before, after) in amendments(row).items():
            # 정정항목 라벨은 본문 필드명과 표기가 조금씩 다르다 — 정규화해 맞춘다.
            tgt = next((k for k in orig["fields"] if N(item) and N(item) in N(k)), None)
            orig.setdefault("amended", {})[tgt or item] = {
                "from": before, "to": after, "by": rec["rcept_no"]}
            if tgt:
                orig["fields"][tgt] = after
            n_applied += 1

    # ── 서식별 스키마(라벨 등장률)
    schema = {}
    for rec in recs:
        s = schema.setdefault(rec["form"], {"n_docs": 0, "corps": set(), "fields": {}})
        s["n_docs"] += 1
        s["corps"].add(rec["corp_name"])
        for k in rec["fields"]:
            s["fields"][k] = s["fields"].get(k, 0) + 1
    for s in schema.values():
        s["n_corps"] = len(s.pop("corps"))
        s["fields"] = dict(sorted(s["fields"].items(), key=lambda kv: -kv[1]))

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(RECORDS + ".tmp", "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(RECORDS + ".tmp", RECORDS)
    with open(SCHEMA + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(schema, fh, ensure_ascii=False, indent=1)
    os.replace(SCHEMA + ".tmp", SCHEMA)

    el = time.time() - t0
    if verbose:
        size = os.path.getsize(RECORDS) / 1e6
        print(f"레코드 {len(recs)}건 · 서식 {len(schema)}종 · {size:.1f}MB · {el:.0f}초")
        print(f"정정 반영 {n_applied}건 · 원공시 미발견 {n_orphan}건")
    return len(recs), el


def load():
    """레코드를 읽어 리스트로. 배치가 없으면 빈 리스트."""
    if not os.path.exists(RECORDS):
        return []
    with open(RECORDS, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh]


def schema():
    if not os.path.exists(SCHEMA):
        return {}
    with open(SCHEMA, encoding="utf-8") as fh:
        return json.load(fh)

if __name__ == "__main__":
    import sys
    if "--check" in sys.argv:
        recs = load()
        sc = schema()
        print(f"레코드 {len(recs)}건 · 서식 {len(sc)}종")
        if recs:
            n_am = sum(1 for r in recs if r.get("amended"))
            print(f"정정 반영된 원공시 {n_am}건")
            for form, s in sorted(sc.items(), key=lambda kv: -kv[1]["n_docs"])[:8]:
                top = list(s["fields"].items())[:1]
                print(f"  {form[:40]:42s} {s['n_docs']:5d}건 {s['n_corps']:3d}사 "
                      f"필드 {len(s['fields']):4d}종"
                      + (f" · 최다 {top[0][0][:24]}({top[0][1]})" if top else ""))
        else:
            print("배치 없음 — `python3 -m agent2.data.filings` 실행")
    else:
        build()
