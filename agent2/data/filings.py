"""정형공시 정규화 배치 — 주요사항·거래소·지분 3,150건을 레코드로 접는다.

## 왜 필요한가

정본표 16종은 전부 사업보고서에서 나온다. `doctables.FILING_SOURCE`가 빈 사전이라
**코퍼스의 74.9%(3,150건)에 추출기가 하나도 없다.** 계측으로 확인했다 —
대표 도구를 3개사에 돌렸을 때 실제로 읽힌 원문이 정기공시 6건뿐이었다.

정형공시는 사업보고서와 달리 **DART 서식이 고정**이라 스키마 정규화가 성립한다.
사업보고서는 서식이 고정이 아니라 이 방식을 쓰지 않는다(정본표가 그쪽을 맡는다).

## 스키마를 손으로 적지 않는다

서식이 154종이고 필드가 수십 개씩이다. 손으로 적으면 고칠 곳이 154군데가 되고
새 서식에서 깨진다. 대신 **코퍼스에서 유도**한다 — 각 서식의 전 문서를 훑어
라벨 등장률을 세고, 그 분포 자체를 스키마로 남긴다.

★ 임계값을 두지 않는다(사용자 결정 2026-08-14). 전부 저장하고 **등장률을 함께** 기록한다.
  단일판매를 예로 들면 100%면 필드 4개인데 그러면 `계약금액`이 빠지고(1,022/1,106=92.4%),
  50%면 잡음이 섞인다. 임계값은 결국 고르는 값이고, 이 프로젝트에서 고른 값들이 계속 틀렸다.
  등장률을 함께 주면 판단이 소비 시점으로 미뤄지고 나중에 근거를 갖고 자를 수 있다.

## 필드 추출 규칙 (전수 검증됨)

    라벨   rowspan 전개된 행의 **선행 셀들**을 ` > `로 결합. 중복은 접는다.
           `['2. 계약내역','계약금액(원)','22,764,…']` → `2. 계약내역 > 계약금액(원)`
    값     라벨과 다른 **마지막 셀**. 좌표를 고정하지 않는다 —
           실측에서 자기주식 처분예정금액 좌표가 5(113건)·3(3건)로 갈렸다.

★ 명부 표(특별관계자 목록 등)는 첫 열이 라벨이 아니라 **데이터**다. 이 규칙을 그대로
  적용하면 사람 이름이 필드 키가 된다(`경계현 > 개인(국내) > 계열회사 임원`).
  따로 거르지 않는다 — **회사별 등장률이 곧 필터**이기 때문이다. 임원 이름은 그 회사
  문서에만 나타나므로 등장률이 낮게 잡히고, 소비자가 그 수치를 보고 배제할 수 있다.
  (프로토타입에서 한 서식의 앞 25건을 뽑았더니 대부분 같은 회사라 임원 이름이
   '전건 필드'로 보였다. 회사를 섞으니 사라졌다.)

## 정정 반영 (규칙 2)

정정본의 `정정후` 값을 원공시 레코드에 덮고 이력을 남긴다. 링크 필드는 **둘**이고
**어디에 적히는지도 문서군마다 다르다**(2026-08-15 전수 실측):

    거래소공시   `정정관련 공시서류제출일`         631/631   키-값 **표**에 있다
    주요사항     `정정대상 공시서류의 최초제출일`   172/173   **산문**에 있다
    지분공시     `정정대상 공시서류의 최초제출일`    41/ 41   **산문**에 있다
    ─────────────────────────────────────────────────────
    합계 844/845 = 99.9%

★ 종전 코드는 `fields`(표에서 유도한 라벨)만 봐서 **주요사항·지분이 0/214**였다
  (전체 631/845 = 74.7%). 필드가 없는 게 아니라 **표가 아니라 산문**이었다.
  `link_date`에 본문 폴백을 붙여 회복했다. 이 문서 머리말에는 두 필드가 적혀
  있었는데 코드가 한쪽 경로만 탔다 — **주석과 코드가 어긋나 있었다.**

못 잇는 1건(미래에셋증권 자기주식취득결정 정정)은 추정으로 잇지 않는다.

실행:
    python3 -m agent2.data.filings              # 배치 생성
    python3 -m agent2.data.filings --check      # 상태만 확인
"""
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

#: 정정본이 원공시를 가리키는 필드. 전수로 확인한 두 가지뿐이다.
_LINK_LABELS = ("정정관련공시서류제출일", "정정대상공시서류의최초제출일", "최초제출일")
_DATE = re.compile(r"(\d{4})\D{0,3}(\d{1,2})\D{0,3}(\d{1,2})")


def form_of(row):
    """서식명 정규화 — `[기재정정]`·`(자율공시)` 등 접두·접미를 떼고 **종류**만 남긴다.

    `투자판단관련주요경영사항`은 제목이 자유서술이라(예: "ALT-B4 관련 마일스톤 기술료 수령")
    괄호 뒤를 통째로 버린다. 그러지 않으면 서식이 154종으로 흩어진다.
    """
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
    """행을 `라벨런 → 값런` 구간으로 쪼개 [(라벨, 값)]을 낸다.

    ★ 한 행에 라벨-값 쌍이 **여럿** 온다. 전수 측정으로 확인했다
      (대량보유(일반) 40개사 표본, 행 패턴):
          VVVVLVVV  2,304   명부 데이터 행
          LVVVLVV     890   쌍 둘   ┐
          LVLV        498   쌍 둘   ├ 다중 쌍 1,388건
          LV          491   쌍 하나
          LLLLLLLL    481   헤더 행
      "라벨과 다른 마지막 셀" 하나만 취하면 앞쪽 쌍을 통째로 잃는다 —
      `['성명(명칭)','한글','삼성물산주식회사','한자(영문)','三星物産(株)…']`에서
      한글명 대신 한자명을 집었다(D-1 4개사 전부 오답).

    ★ **구간을 넘어 라벨을 물려주지 않는다.** `['자산총액', 61,990,427,
      '부채총액', 24,731,887]`에서 `자산총액 > 부채총액`이라는 없는 계층이 생긴다.
      행 구조가 보여주는 것 이상을 추론하지 않는다.

    검증: 이 규칙으로 `성명(명칭) > 한글`이 40/40 문서에서 정확히 추출된다.

    ★★ **`vals`(그 문서의 값 셀 집합)를 주면 빈도 어휘를 보완한다**(2026-08-17).
      어휘는 "그 서식 문서의 90% 이상에 나타나면 라벨"이라는 **빈도 휴리스틱**이라,
      **서식 변형에만 있는 라벨**을 값으로 오분류한다. 그러면 그 라벨과 값이 통째로
      삼켜진다(`vals[0]`만 취하므로).
        레인보우로보틱스 단일판매: `확정 계약금액`·`조건부 계약금액`·`계약금액 총액(원)`
        3행이 사라져 I-11·12·13이 전부 추출실패였다. 원문 격자는 멀쩡했다:
          ['2. 계약내역', '확정 계약금액', '7,830,000,000', '7,830,000,000']
      거래소공시(html/xforms)는 **원문이 값 셀을 `class="xforms_input"`로 표시**하고
      `parse`가 그것을 `meta['xforms_values']`에 모아 둔다. 빈도가 아니라 **그 문서
      자신의 마크업**이므로 판별이 정확하다(§7 "판별은 값의 동일성이 아니라 마크업").
    ★★ **`vals`를 그냥 OR로 넣으면 순증이 아니다** — 처음에 그렇게 했다가 전수 대조에서
      **790개 키가 사라졌다**(`※ 관련공시`·`유보사유`·`유보기한`). 값 셀이 `xforms_input`이
      아니라 **평문**인 항목은 라벨로 승격돼 짝이 통째로 없어진다.
      그래서 `fields_of`가 **두 규칙을 따로 돌리고 원래 규칙을 먼저 넣는다**(`setdefault`가
      먼저 넣은 것을 지킨다). 구조 규칙은 **새 키만 더한다** — 이제 진짜 순증이다.
      §6-34가 없었으면 이 손실을 못 봤다.
    """
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
#: ★ 전수 측정으로 골랐다(대량보유(일반) 40개사). 셀 등장률이 **이봉분포**다:
#:     100%    136개      ← 서식이 고정이라 구조 셀은 전건에 나타난다
#:     90~99%   +5개
#:     50~89%  +20개      ← 여기부터 값이 섞이기 시작한다
#:   100%와 90%는 D-1 추출 결과가 40/40으로 같다. 더 보수적인 90%를 쓴다 —
#:   라벨이 한 문서에서 누락돼도(파싱 변형·서식 개정) 라벨로 유지된다.
_LABEL_THRESHOLD = 0.9


def cell_sets(rows, verbose=False):
    """문서별 셀 문자열 집합. 라벨 어휘 계산의 입력이다.

    ★ **표본을 쓰지 않는다.** 60건 표본으로 등장률을 재다가 경계 라벨을 잃었다 —
      단일판매 `- 체결계약명`은 전수 1,022/1,106 = **92.4%**로 임계값 90%를 넘는데,
      표본에서는 흔들려 90% 아래로 떨어졌고 그래서 값으로 분류돼
      I-11(체결계약명)이 3개사 전부 실패했다.
      경계에 있는 라벨은 표본으로 판정할 수 없다. 전 문서로 센다.
    """
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
#: ★ 빈도 기반 어휘의 구조적 허점이다. `-`는 "값이 없음"을 뜻하는 자리표시자라
#:   **모든 문서에 나타나고**, 그래서 등장률 100%로 라벨로 승격된다.
#:   그러면 `_segments`가 값으로 안 잡고 라벨로 흡수해 **대시 값이 통째로 사라진다**
#:   (실측: 자기주식처분 20건에서 추출된 549쌍 중 대시 값 **0쌍**).
#:   그 탓에 `6. 처분방법 > 시장을 통한 매도` 필드가 아예 없어져,
#:   확정 규칙 7(b)("`-`는 0주")를 적용할 근거가 관측에 안 들어갔다.
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
            # ★ **원래 규칙 먼저, 구조 규칙은 새 키만.** `setdefault`가 먼저 넣은 것을
            #   지키므로 종전 필드는 하나도 안 사라진다(전수 대조: 사라짐 0).
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
    """명부 표를 **행 구조 그대로** 보존한다 → [{headers, rows}].

    ★ 키-값 평탄화로는 명부를 담을 수 없다. `보고자|삼성물산|298,818,100`처럼
      **행 자체가 레코드**인 표는 라벨-값으로 접으면 관계(보고자/특별관계자)가
      사라진다. 실측: D-2(보고자 의결권주식)·D-3(보고자+특별관계자 합산)이
      평탄화 레코드에서 **구분되지 않아 4개사 전부 오답**이었다.

    판별: 헤더 행(셀이 전부 라벨 어휘) + 그 아래 2행 이상.
    ★ 행을 어휘로 분류하려 들면 안 된다 — `보고자`·`특별관계자`는 모든 문서에
      나타나 **어휘에 포함**되므로, "선행 셀이 어휘가 아닌 행"을 데이터로 보면
      진짜 데이터 행이 걸러진다(실제로 그렇게 만들었다가 빈 표를 얻었다).
      헤더만 찾고 **그 아래는 그대로 담는다.**
    """
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
            # ★ 열 합계는 **자르기 전 전 행**으로 낸다. 잘린 행으로 더하면 부분합이
            #   되고, 그게 답으로 나간다 — 실측: 셀트리온 특별관계자가 105명인데
            #   40행으로 잘려 D-3(보고자+특별관계자 총수)이 틀렸다.
            col_sums = {}
            for j, head in enumerate(gs[h][:12]):
                vals = []
                for g in body:
                    if j < len(g):
                        v = str(g[j]).replace(",", "").strip()
                        if re.fullmatch(r"-?\d+", v):
                            vals.append(int(v))
                # ★ **수치 행이 1개여도 합계를 낸다**(2026-08-17). `>= 2`였을 때
                #   **보고자가 `-`(=0주)이고 특별관계자가 한 명뿐인** 명부표에서 합계가
                #   통째로 사라졌다. 그 한 줄이 곧 전체 합계인데도 빠진 것이다.
                #   실측(D-2 한화오션 약식): `의결권있는주식` 열의 수치 행이
                #   특별관계자 1행(19,834,812)뿐이라 `전 행 합계` 키가 안 생겼고,
                #   모델이 요약에서 그 수치의 이름을 못 찾아 **`필드` 블록의
                #   `보유주식등의 수 > 이번 보고서`를 골라** 보고자 본인 보유량으로 답했다
                #   (r19·r20 2회 모두 X. 같은 회차에 요약 키를 가진 3사는 전부 O).
                if vals:
                    col_sums[str(head)[:30]] = {"sum": sum(vals), "n": len(vals)}
            out.append({"headers": gs[h][:12],
                        "rows": [g[:12] for g in body[:_ROSTER_MAX_ROWS]],
                        "n_rows": len(body), "col_sums": col_sums})
    return out


#: 링크 필드를 **본문에서** 찾는 정규식. `fields`는 표에서만 나오는데
#: 주요사항·지분·정기 정정본은 이 줄이 **표가 아니라 산문**이다:
#:     `2. 정정대상 공시서류의 최초제출일 : 2024년 11월 18일`
#: 날짜는 `2025-07-28`과 `2024년 11월 18일` 두 형식이 섞여 있다.
_LINK_TEXT = re.compile(
    r"(?:정정관련\s*공시서류\s*제출일|정정대상\s*공시서류의?\s*최초제출일)"
    r"[^0-9]{0,80}(\d{4})\s*[-.년]\s*(\d{1,2})\s*[-.월]\s*(\d{1,2})")
_TAGS = re.compile(r"<[^>]+>")


def link_date(fields, row=None):
    """정정본이 가리키는 원공시 제출일 `YYYYMMDD`. 못 찾으면 None.

    ★ **표 → 본문 순으로 본다**(2026-08-15 전수 실측으로 폴백 추가).
      종전에는 `fields`(표에서 유도한 라벨)만 봐서 문서군이 통째로 빠졌다:

          exchange  631/631  ← 링크 줄이 키-값 **표**에 있다
          major       0/173  ┐
          holding     0/ 41  ├ 링크 줄이 **산문**이라 `fields`에 안 들어온다
          periodic     —     ┘

      본문 폴백을 붙이면 **1,003/1,004(99.9%)**가 된다. 정정을 못 이으면 확정 규칙 2
      (정정을 원공시에 반영)가 그 문서군에서 통째로 동작하지 않는다.
    """
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
    """정정본의 정정 표 → {정정항목: (정정전, 정정후)}.

    ★ 헤더가 **5번째 행**에 오는 서식이 있다(거래소공시). 앞 3행만 보면 472건을
      '정정 표 없음'으로 오판한다 — 실제로 한 번 오판했다. 전 행을 본다.
    """
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
    # ★ 정정본은 원본에 **정정신고 표가 추가된 다른 구조**다. 한데 묶으면 정정본에만
    #   있는 라벨(`2. 정정관련 공시서류제출일` 등)이 등장률에서 희석돼 값으로 분류되고,
    #   그러면 원공시 링크를 못 찾아 **정정 반영이 통째로 0건이 된다**(실제로 그랬다).
    #   실측(단일판매 1,106건): 원본 543 · 정정본 563이라 정정 라벨 등장률이 50.9%다.
    #     전체 기준 어휘 16개 → 링크 추출 실패
    #     정정본만 기준 어휘 21개 → 링크 추출 성공
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
            # ★ 원공시가 코퍼스에 없다. 거래소공시는 **정정본이 원본보다 많다**
            #   (실측: 단일판매 원본 360 vs 정정본 398). 이 경우 정정본이 유일한
            #   원문이므로 데이터 손실은 없다 — 다만 "정정을 반영한 것"과
            #   "정정본이 유일본인 것"은 다르므로 표시해 둔다.
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
