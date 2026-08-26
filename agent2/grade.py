"""채점기 — 27문항을 6축으로 잰다. 한 덩어리 점수를 내지 않는다.

## 축을 나누는 이유

처음 27문항을 돌렸을 때 `계약 27/27 · answered 21`이 나왔지만 실제 정답은 11건이었다.
"답이 나왔는가"와 "값이 맞는가"는 다른 축이고, 섞으면 조용한 오답을 놓친다.

축 구성은 FinanceComplexQA(arXiv 2607.19238, 금융문서 에이전트 벤치)의 6축을 가져왔다.

    정확성 ACC   최종 결론이 정답과 일치 (루브릭 가중 통과율)
    수치정합 NUM  **공식·단위·기간·부호** 보존. 값만 맞고 단위가 틀리면 실패.
    근거확보 COV  필요한 근거를 실제로 가져왔는가 (도구 호출 + retrieved_context)
    충실성 FS    답변의 수치가 근거 안에 있는가 (근거 밖 수치 = 환각)
    금지         대표 오답을 명시적으로 잡는다 (별도값·코퍼스 밖 지식 등)
    비용         토큰·시간. **참고 지표**다 — 주최 명세에 응답시간 제한이 없다
                 (전수 확인). 느려도 정확한 답이 낫다는 것이 사용자 지시(2026-08-06).

## 단위 환산이 채점의 핵심이다

에이전트가 시가총액 **14,586,465억원**을 "14,586,465 **백만 원**"이라 답한 적이 있다.
숫자만 보면 통과한다. 그래서 통화 단위는 전부 **원으로 환산해서** 비교한다
(억원 ↔ 백만원은 100배 차이라 환산하면 바로 걸린다).

## 임계값에 대한 정직한 고지

`완전통과 / 부분통과 / 실패`의 경계(1.0 / 0.5)는 **표준이 아니라 보고용 구간**이다.
근거가 없으므로 단일 합격선을 만들지 않고 분포를 함께 낸다.
수치 허용오차 1%는 표시 반올림을 흡수하기 위한 값이며 역시 우리 선택이다.

실행:
    python3 -m agent2.grade                 # 27문항 실행 + 채점
    python3 -m agent2.grade --from FILE     # 이전 실행 결과(JSON)를 채점만
    python3 -m agent2.grade 1-15 2-2        # 일부만
"""
import json
import re
import sys

from agent2.tools import claims
from agent2 import golden, loop, scenarios

#: 통화 단위 → 원 배수. 표기 흔들림(백만 원/백만원/백만)을 전부 흡수한다.
_CUR = {"조원": 1e12, "조": 1e12, "억원": 1e8, "억": 1e8,
        "백만원": 1e6, "백만": 1e6, "천원": 1e3, "천": 1e3, "원": 1.0}
#: 통화가 아닌 단위 — 숫자를 그대로 비교한다.
_COUNT = {"%", "주", "명", "사", "개사", "개", "배", "포인트"}

_UNIT_RE = "|".join(sorted(set(_CUR) | _COUNT, key=len, reverse=True))
#: 숫자 + (선택)단위. 괄호·△·마이너스를 음수로 읽는다.
_NUM = re.compile(
    r"(?P<open>[(（])?\s*(?P<sign>[-−△▲])?\s*(?P<n>\d[\d,]*(?:\.\d+)?)\s*(?P<close>[)）])?"
    r"\s*(?P<u>" + _UNIT_RE + r")?")

#: 허용오차는 **답변이 표시한 정밀도의 절반**(half-ulp, 반올림 관례)으로 잡는다.
#: 임의의 상대오차(%)를 쓰면 큰 수에서 구멍이 난다 — 실제로 1%를 썼다가
#: 별도 영업활동현금흐름 68,733,956이 연결 투자활동 68,512,206과 겹쳐 오탐이 났다
#: (1%면 685,122까지 허용된다). "52.7조원"은 0.05조까지, "87,769,374백만원"은
#: 0.5백만원까지만 허용된다 — 표기가 정밀할수록 자동으로 엄격해진다.
_FLOOR = 1e-9         # 부동소수 잡음 방지용 상대 하한
_NEG_HINT = re.compile(r"감소|유출|마이너스|음수|순유출")

# ── 부호 판정 (2026-08-06 사용자 지시: "부호도 반드시 맞아야 한다") ────────────
#: 방향을 말로 표현한 것. 공시 답변은 `-19.5%`라고 안 쓰고 `19.47% 감소`라고 쓴다.
_NEG_WORD = re.compile(r"감소|유출|마이너스|음수|순유출|적자|손실|하락|축소")
_POS_WORD = re.compile(r"증가|유입|순유입|흑자|상승|확대|성장")

#: 항목 경계. **창(window)으로 잡으면 옆 항목의 방향어를 끌어온다** —
#: 실측 버그: 정답 `-19.5%`인데 답변이 `매출 19.47% 증가, 부채 5% 감소`일 때,
#: `19.47` 주변 40자 안에 뒤쪽 `감소`가 들어와 **오답을 통과**시켰다.
#: 그래서 창이 아니라 **줄·불릿·쉼표로 끊은 같은 항목 안에서만** 방향어를 본다.
#: ★ 콤마는 **숫자 사이가 아닐 때만** 구분자다. 그냥 `,`로 끊었더니
#:   `68,512,206백만원 유출`이 `68`에서 잘려 방향어 `유출`을 못 봤다(테스트가 잡았다).
_ITEM_SEP = re.compile(r"[\n;·•]|(?<!\d),(?!\d)|(?:\s-\s)|\*\*")


def _item_of(text, pos):
    """`pos`가 속한 **항목**(줄·불릿·쉼표로 끊은 조각)을 돌려준다."""
    left = 0
    for m in _ITEM_SEP.finditer(text, 0, pos):
        left = m.end()
    m = _ITEM_SEP.search(text, pos)
    return text[left:m.start() if m else len(text)]


#: 방향이 의미 있는 단위 = **부호를 가질 수 있는 양**. 통화와 비율뿐이다.
#: 개수(개사·명·주)는 음수가 없으므로 방향 검사를 하지 않는다.
#: ★ 실측 오탐: 정답 `3개`(3개년)를 답변
#:   "영업활동현금흐름은 최근 **3개년** 연속 **마이너스**입니다"에서 찾았는데,
#:   같은 문장의 `마이너스` 때문에 **개수를 부호 오답으로 처리**했다(No.23·No.39).
_SIGNED = frozenset(_CUR) | {"%", "포인트"}


def sign_ok(gold, text, pos, explicit_neg, unit=""):
    """답변이 표시한 **방향**이 정답의 부호와 맞는가.

    규칙 — 정직하게 엄격하다:
      정답 음수  답변이 부호문자(`-`·`△`·괄호)로 음수이거나, **같은 항목 안에**
                감소 계열 방향어가 있어야 한다. 증가 계열이 있으면 실패.
      정답 양수  답변이 음수 부호도, 감소 계열 방향어도 없어야 한다.
                단, **개수 단위는 검사하지 않는다**(음수가 없는 양이다).

    ★ 왜 필요한가(실측 버그 2건):
      · 정답 `+6.3%`인데 답변 `6.29% 감소`가 **통과**했다 — 값만 봤기 때문이다.
      · 목록 답변에서 옆 항목의 `감소`를 끌어와 부호 오답이 통과했다(위 `_ITEM_SEP`).
    """
    if gold >= 0 and unit not in _SIGNED:
        return True                      # 개수에는 방향이 없다
    item = _item_of(text, pos)
    neg_word, pos_word = bool(_NEG_WORD.search(item)), bool(_POS_WORD.search(item))
    if gold < 0:
        if pos_word and not neg_word:
            return False
        return explicit_neg or neg_word
    return not explicit_neg and not neg_word


def _clean(text):
    """'백만 원' 같은 띄어쓰기와 전각 공백을 붙여 단위 인식이 새지 않게 한다."""
    t = (text or "").replace("　", " ")
    t = re.sub(r"(조|억|백만|천)\s+원", r"\1원", t)
    return t


def numbers(text):
    """[(값, 단위, 시작위치, 표시정밀도)] — 답변에서 수치를 전부 뽑는다.

    표시정밀도 = 마지막 자리의 크기. `52.7`이면 0.1, `87,769,374`면 1.
    """
    out = []
    for m in _NUM.finditer(_clean(text)):
        raw = m.group("n")
        try:
            v = float(raw.replace(",", ""))
        except ValueError:
            continue
        dec = len(raw.split(".")[1]) if "." in raw else 0
        neg = bool(m.group("sign")) or (m.group("open") and m.group("close"))
        out.append((-v if neg else v, m.group("u") or "", m.start(), 10.0 ** (-dec)))
    return out


def _krw(value, unit):
    """통화면 원으로 환산, 아니면 None(숫자 그대로 비교)."""
    return value * _CUR[unit] if unit in _CUR else None


def _tol(step, scale, gold):
    """half-ulp 허용오차 — 답변 표기 정밀도의 절반."""
    return max(step * scale / 2.0, abs(gold) * _FLOOR)


def _match_num(chk, found, text):
    """수치 체크 1건 판정 → (통과, 사유)."""
    gold = chk.value
    g_krw = _krw(gold, chk.unit)
    for v, u, pos, step in found:
        if g_krw is not None:
            # 통화 — 단위를 붙여 쓴 것만 인정한다. 단위 없는 맨숫자는 판정 불가로 본다.
            if u not in _CUR:
                continue
            c, tol = _krw(v, u), _tol(step, _CUR[u], g_krw)
            if abs(c - g_krw) <= tol:
                return True, f"{v:,.0f}{u}"
            # 크기는 맞는데 부호 표기만 없는 경우 — 바로 옆에 감소·유출 표현이 있으면 인정
            if g_krw < 0 and abs(abs(c) - abs(g_krw)) <= tol \
                    and _NEG_HINT.search(_clean(text)[max(0, pos - 15):pos + 25]):
                return True, f"{v:,.0f}{u}(문맥상 음수)"
        else:
            if chk.unit and u and u != chk.unit:
                continue
            if abs(v - gold) <= _tol(step, 1.0, gold):
                return True, f"{v:,.4g}{u or ''}"
    # 값은 있는데 단위가 틀린 경우를 따로 알려준다 — 가장 흔한 실패다
    if g_krw is not None:
        for v, u, _, step in found:
            if abs(v - gold) <= _tol(step, 1.0, gold) and u != chk.unit:
                return False, f"값은 맞으나 단위 {u or '없음'} (정답 {chk.unit})"
    return False, "없음"


#: 부정문 표지. 커버 체크가 **거부 문장 안의 단어**를 잡아 통과하는 것을 막는다.
#: 실제로 2-8이 "가동률에 대한 정보는 공시에서 확인되지 않습니다"로 답했는데
#: '가동률'이라는 단어 때문에 커버 체크를 통과했다 — 채점이 무의미해진다.
#: 정보한계 고지 어휘 — **`claims.DENIAL` 단일 진실.** 사전을 따로 두면 어긋난다:
#: 이 세션에서 같은 실수를 세 번 했다(거부 문장의 금지어를 위반으로 셈).
_DENY = claims.DENIAL


def _sentences(text):
    """커버 판정 단위 = **블록**. 줄 단위로 쪼개면 마크다운이 부서진다.

    실측 실패: 답변이
        1. **유상증자**
           - **일자**: 1989.08.25
           - **수량**: 3,400,000 주
    처럼 **라벨과 값을 다른 줄**에 쓰는데, 줄로 쪼개면 `**유상증자**` 한 줄만 남아
    "질문 되뇜"으로 판정돼 실패했다. 값이 바로 아래 있는데도 못 본 것이다.
    (`1.` 같은 번호도 별도 문장으로 잘려 나갔다.)

    분해 규칙을 마크다운에 맞추려다 하위 불릿까지 끊는 문제가 있어(들여쓰기 유무가
    모델마다 다르다) **분해는 단순하게 두고, 판정을 창(window)으로** 한다 —
    `_match_text`가 라벨 줄과 **뒤따르는 몇 줄**을 함께 보고 내용 유무를 정한다.
    """
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", _clean(text)) if s.strip()]

#: 라벨 줄 다음 몇 줄까지를 같은 항목으로 볼 것인가.
#: 값이 라벨 바로 아래 1~3줄에 오는 형태(일자·수량·금액)를 덮는다.
_WINDOW = 3


#: 도입부·요건 라벨 복사 — 내용이 없는 문장. 여기서 매칭되면 **채점이 부풀려진다.**
#: 실측: "삼성전자의 유상증자, 무상증자, … 이력은 다음과 같습니다:" 뒤에 전 항목을
#: "확인되지 않습니다"로 답했는데도 커버 체크를 통과했다. 도입부가 질문을 되뇐 것뿐이다.
#: 근거: 루브릭 기반 평가에서 presence 기준은 **표면 형태를 보상**해 완결성은 오르고
#: 사실 정확성은 떨어지는 것으로 보고된다(arXiv 2606.08625·2607.15092).
_LEADIN = re.compile(r"(다음과\s*같|아래와\s*같|정리하면|요약하면|에\s*대한\s*정보는|"
                     r"에\s*대한\s*이력은|에\s*대해\s*(알려|정리))")
_LABEL_ECHO = re.compile(r"^[\s\-*·•#>]*\**\s*\[[^\]]+\]")   # "- **[증자이력] …**", "### [지분구조] …"
#: (문장 종결 유무로 제목을 가리려 했으나 **불릿 목록까지 막았다** —
#:  "주요 제품: TV, 모니터, 냉장고…"는 종결이 없어도 내용이다. 라벨 패턴만 쓴다.)


def _is_substantive(sent, question):
    """이 문장이 **새 정보를 담고 있는가.**

    판정 기준은 "도입부처럼 보이는가"가 아니라 **질문에 없던 내용이 있는가**다.
    `"가동률은 78.8%이며 … 다음과 같습니다"`는 도입부 표현이 있어도 데이터가 있으니
    내용이 있는 문장이다(이걸 막았다가 정답을 오답 처리했다).
    """
    t = sent.strip()
    if not t:
        return False
    # 질문에 없던 **수치**가 있으면 무조건 내용이 있다. 연도 같은 되뇜은 제외한다.
    qnums = {n for n, _u, _p, _s in numbers(question or "")}
    if any(n not in qnums and abs(n) >= 10 for n, _u, _p, _s in numbers(t)):
        return True
    if _LABEL_ECHO.match(t):
        return False                        # 요건 라벨을 그대로 옮긴 줄
    if _LEADIN.search(t) or t.endswith((":", "：")):
        return False
    # 수치도 없고 질문 어절만 옮겨 놓은 문장(되뇜)은 내용이 아니다.
    q = set(re.findall(r"[가-힣A-Za-z]{2,}", question or ""))
    w = re.findall(r"[가-힣A-Za-z]{2,}", t)
    if q and w and sum(1 for x in w if x in q) / len(w) >= 0.7:
        return False
    return True


def _match_text(chk, text, question=""):
    """커버 포인트 — **내용 있는 긍정 문장 안에서** 발견돼야 통과.

    내용 유무는 그 줄만이 아니라 **뒤따르는 몇 줄까지 함께** 본다.
    마크다운 목록은 라벨과 값을 다른 줄에 쓴다:
        1. **유상증자**
           - 일자: 1989.08.25 / 수량: 3,400,000주
    라벨 줄만 보면 "질문 되뇜"이라 정답을 오답 처리한다(실제로 그랬다).
    """
    sents = _sentences(text)
    denied = echoed = None
    for i, s in enumerate(sents):
        for a in chk.any_of:
            if a not in s:
                continue
            if _DENY.search(s):
                denied = a
                continue
            window = " ".join(sents[i:i + 1 + _WINDOW])
            if not _is_substantive(s, question) and not _is_substantive(window, question):
                echoed = a
                continue
            return True, a
    if echoed:
        return False, f"'{echoed}'가 도입부·질문 되뇜에만 있음"
    if denied:
        return False, f"'{denied}'가 부정문 안에만 있음"
    return False, "없음"


def _match_absent(chk, text):
    """부재 선언 체크 — 대상 용어가 **부정문과 함께** 나와야 통과."""
    for s in _sentences(text):
        if _DENY.search(s) and any(a in s for a in chk.any_of):
            return True, "부재 선언"
    return False, "부재를 밝히지 않음"


def _match_forbid(chk, found, text):
    """금지 위반이면 (True, 사유)."""
    t = _clean(text)
    for a in chk.any_of:
        if a in t:
            return True, f"문구 '{a}'"
    if chk.value is not None:
        g = _krw(chk.value, chk.unit) if chk.unit else None
        for v, u, pos, step in found:
            hit = ((g is not None and u in _CUR
                    and abs(_krw(v, u) - g) <= _tol(step, _CUR[u], g))
                   or (g is None and abs(v - chk.value) <= _tol(step, 1.0, chk.value)))
            if not hit:
                continue
            # ★ 별도 값을 **별도라고 밝히고** 함께 제시한 것은 오답이 아니다.
            #   금지 규칙의 취지는 "별도를 연결로 답하지 마라"이지 "별도를 언급하지 마라"가
            #   아니다(도메인: "별도 재무제표를 유저가 원하면 답변 가능해야 함").
            #   실제로 연결·별도를 나눠 제시한 정답을 이 규칙이 오답 처리했다.
            if "별도" in chk.label and "별도" in _clean(text)[max(0, pos - 90):pos + 30]:
                continue
            return True, f"{v:,.0f}{u or ''}"
    return False, ""


def _faithfulness(answer, context):
    """답변 수치 중 근거에 존재하는 비율. 근거 밖 수치는 환각 후보다."""
    ctx = re.sub(r"[,\s]", "", _clean(context))
    nums = [v for v, _u, _p, _s in numbers(answer) if abs(v) >= 100]   # 작은 수는 서수·연도 노이즈
    if not nums:
        return None, 0, 0
    ok = 0
    for v in nums:
        s = f"{abs(v):.0f}" if float(v).is_integer() else f"{abs(v)}"
        if s in ctx:
            ok += 1
    return ok / len(nums), ok, len(nums)


def grade_one(sid, answer, context, tools, tokens, seconds, question=""):
    it = golden.item(sid)
    question = question or (scenarios.BY_ID[sid].q if sid in scenarios.BY_ID else "")
    if it is None:
        return None
    found = numbers(answer)
    rows, got, tot = [], 0.0, 0.0
    violations = []
    for c in it.checks:
        if c.kind == "forbid":
            bad, why = _match_forbid(c, found, answer)
            if bad:
                violations.append(f"{c.label} ({why})")
            rows.append(("금지", c.label, not bad, why or "-"))
            continue
        if c.kind == "num":
            ok, why = _match_num(c, found, answer)
        elif c.kind == "absent":
            ok, why = _match_absent(c, answer)
        else:
            ok, why = _match_text(c, answer, question)
        tot += c.weight
        got += c.weight if ok else 0
        rows.append(({"num": "수치", "absent": "부재"}.get(c.kind, "커버"),
                     c.label, ok, why))
    acc = got / tot if tot else 0.0
    nums = [r for r in rows if r[0] == "수치"]
    num_rate = sum(1 for r in nums if r[2]) / len(nums) if nums else None
    fs, fs_ok, fs_n = _faithfulness(answer, context)
    return {"id": sid, "acc": acc, "num": num_rate, "cov": bool(tools),
            "fs": fs, "fs_ok": fs_ok, "fs_n": fs_n,
            "violations": violations, "rows": rows,
            "tokens": tokens, "seconds": seconds,
            "verdict": ("완전통과" if acc >= 1.0 and not violations
                        else "부분" if acc >= 0.5 and not violations
                        else "실패")}


def run(ids=None, save=None):
    """27문항 실행 + 채점. `save`를 주면 답변·근거를 JSON으로 남겨 **재채점이 공짜**가 된다.

    채점 규칙을 고칠 때마다 LLM을 다시 호출하면 비용도 들고 결과가 흔들려
    '채점기가 좋아진 것'과 '답이 달라진 것'이 섞인다.
    """
    out, raw = [], []
    for s in scenarios.SCENARIOS:
        if ids and s.id not in ids:
            continue
        res = loop.run(s.q, question_id=s.id)
        rec = {"id": s.id, "answer": res.get("answer", ""),
               "ctx": res.get("retrieved_context", ""), "tools": res.get("_tools", []),
               "tokens": res.get("_tokens", 0), "sec": res.get("_seconds", 0),
               "stop": res.get("_stop", "")}
        raw.append(rec)
        g = grade_one(s.id, rec["answer"], rec["ctx"], rec["tools"],
                      rec["tokens"], rec["sec"])
        g["stop"] = rec["stop"]
        g["answer"] = rec["answer"]
        out.append(g)
        _print_one(g)
    if save:
        with open(save, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, ensure_ascii=False, indent=1)
        print(f"(실행 기록 저장: {save} — `--from {save}`로 재채점 가능)\n")
    _summary(out)
    return out


def _print_one(g):
    mark = {"완전통과": "●", "부분": "◐", "실패": "○"}[g["verdict"]]
    fs = "-" if g["fs"] is None else "%.0f%%" % (g["fs"] * 100)
    nm = "-" if g["num"] is None else "%.0f%%" % (g["num"] * 100)
    print(f"{mark} [{g['id']:4s}] {g['verdict']:5s} ACC {g['acc']*100:5.1f}% · "
          f"수치 {nm:>4s} · 근거 {'O' if g['cov'] else 'X'} · "
          f"충실 {fs:>4s} ({g['fs_ok']}/{g['fs_n']}) · "
          f"{g['tokens']:,}tok {g['seconds']:.1f}s")
    for kind, label, ok, why in g["rows"]:
        if ok and kind != "금지":
            print(f"        ✓ {kind} {label}: {why}")
        elif not ok:
            print(f"        ✗ {kind} {label}: {why}")
    for v in g["violations"]:
        print(f"        ⛔ 금지 위반 — {v}")
    print()


def _summary(rows):
    n = len(rows)
    if not n:
        return
    full = [r for r in rows if r["verdict"] == "완전통과"]
    part = [r for r in rows if r["verdict"] == "부분"]
    fail = [r for r in rows if r["verdict"] == "실패"]
    viol = [r for r in rows if r["violations"]]
    nocov = [r for r in rows if not r["cov"]]
    fs_vals = [r["fs"] for r in rows if r["fs"] is not None]
    fs_avg = "-" if not fs_vals else "%.1f%%" % (sum(fs_vals) / len(fs_vals) * 100)
    print("=" * 78)
    print(f"문항 {n} · 완전통과 {len(full)} · 부분 {len(part)} · 실패 {len(fail)}")
    print(f"평균 ACC {sum(r['acc'] for r in rows)/n*100:.1f}% · "
          f"근거확보 {n-len(nocov)}/{n} · 충실성 평균 {fs_avg}")
    print(f"금지 위반 {len(viol)}건" + (f" — {[r['id'] for r in viol]}" if viol else ""))
    print(f"토큰 {sum(r['tokens'] for r in rows):,} · 총 {sum(r['seconds'] for r in rows):.1f}초 "
          f"· 최장 {max(r['seconds'] for r in rows):.1f}초(참고)")
    if fail:
        print("\n실패 문항:")
        for r in fail:
            miss = [f"{l}" for k, l, ok, _ in r["rows"] if not ok and k != "금지"]
            print(f"  [{r['id']}] ACC {r['acc']*100:.0f}% — 미달 {miss[:4]}")


if __name__ == "__main__":
    #: `--save`/`--from` 뒤의 파일 경로는 시나리오 ID가 아니다(한 번 섞여서 0문항이 돌았다).
    _argv, args, _opts = sys.argv[1:], [], {}
    _i = 0
    while _i < len(_argv):
        a = _argv[_i]
        if a in ("--save", "--from") and _i + 1 < len(_argv):
            _opts[a] = _argv[_i + 1]
            _i += 2
            continue
        if not a.startswith("--"):
            args.append(a)
        _i += 1

    if "--from" in _opts:
        path = _opts["--from"]
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        rows = []
        for r in saved:
            g = grade_one(r["id"], r["answer"], r.get("ctx", ""), r.get("tools", []),
                          r.get("tokens", 0), r.get("sec", 0))
            if g:
                rows.append(g)
                _print_one(g)
        _summary(rows)
    else:
        run(args or None, save=_opts.get("--save"))
