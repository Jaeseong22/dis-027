"""근거 가드 — 답변을 원자 클레임으로 쪼개 근거와 대조하고, 지지되지 않으면 막는다."""
import re
from collections import namedtuple

#: `step`은 **답변에 적힌 표시 정밀도**(정수면 1.0 · 소수 한 자리면 0.1)다.
Claim = namedtuple("Claim", "kind text value unit sentence idx step")
Verdict = namedtuple("Verdict", "claim ok reason")

#: 통화·수량 단위 → 원 배수. 단위가 다르면 같은 숫자도 다른 값이다.
_CUR = {"조원": 1e12, "조": 1e12, "억원": 1e8, "억": 1e8,
        "백만원": 1e6, "백만": 1e6, "천원": 1e3, "원": 1.0}
_UNITS = "|".join(sorted(set(_CUR) | {"%", "주", "명", "사", "개사", "개", "배"},
                         key=len, reverse=True))
_NUM = re.compile(r"(?P<o>[(（])?\s*(?P<s>[-−△▲])?\s*(?P<n>\d[\d,]*(?:\.\d+)?)\s*"
                  r"(?P<c>[)）])?\s*(?P<u>" + _UNITS + r")?")

#: 정직한 실패 선언 — 절대 막지 않는다.
_NOINFO = re.compile(r"확인되지\s*않|확인할\s*수\s*없|찾을\s*수\s*없|제공할\s*수\s*없|"
                     r"공시에\s*없|해당\s*사항\s*없")

#: 공시 도메인에 대한 **주장**을 담은 문장인지. 인사·안내와 가르는 데 쓴다.
_ASSERT = ("산업", "시장", "매출", "수익", "사업", "제품", "경쟁", "성장", "수요",
           "기술", "점유", "실적", "투자", "생산", "재무", "부문", "고객", "공급")

#: 연도·서수처럼 근거 대조가 무의미한 수. 1900~2100 정수와 20 이하 정수.


def _skip_num(v, unit):
    if unit:
        return False
    return (float(v).is_integer()
            and (1900 <= abs(v) <= 2100 or abs(v) <= 20))


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def _numbers(text):
    """[(값, 단위, 표시정밀도)]."""
    out = []
    t = re.sub(r"(조|억|백만|천)\s+원", r"\1원", text or "")
    for m in _NUM.finditer(t):
        raw = m.group("n")
        try:
            v = float(raw.replace(",", ""))
        except ValueError:
            continue
        dec = len(raw.split(".")[1]) if "." in raw else 0
        neg = bool(m.group("s")) or (m.group("o") and m.group("c"))
        out.append((-v if neg else v, m.group("u") or "", 10.0 ** (-dec)))
    return out


def decompose(answer, tools_used=()):
    """답변 → 원자 클레임 목록. 문장 단위로 쪼개고 문장 안의 수치를 각각 클레임으로 만든다."""
    out = []
    for i, s in enumerate(sentences(answer)):
        if _NOINFO.search(s):
            out.append(Claim("부재선언", s, None, "", s, i, 1.0))
            continue
        nums = [(v, u, st) for v, u, st in _numbers(s) if not _skip_num(v, u)]
        for v, u, st in nums:
            out.append(Claim("수치", f"{v:,.10g}{u}", v, u, s, i, st))
        if not nums:
            kind = "메타" if _is_meta(s) else ("서술" if any(k in s for k in _ASSERT)
                                             else "메타")
            out.append(Claim(kind, s, None, "", s, i, 1.0))
    return out

#: 사실 주장이 아닌 문장 — 도입부와 출처 표기. 이걸 서술로 세면 오탐이 쏟아진다
_LEADIN = re.compile(r"(다음과\s*같|아래와\s*같|정리하면|요약하면)")
_CITE = re.compile(r"^\(?\s*(출처|근거|자료|source)\s*[:：]")

#: 인사·안내. **도구 0회여도 통과시켜야 하는 문장**이다 — `loop.SYSTEM` 마지막 줄이
#: *"인사말처럼 공시와 무관한 입력에만 도구 없이 답하십시오"*라고 지시한다.
#: 공격 질문에 딸려 오는 인사는 `INJECTION`이 **질문 쪽에서** 이미 막으므로
#: 여기서 인사를 허용해도 방어가 새지 않는다.
_GREETING = re.compile(r"안녕하[세십]|반갑|감사합니다|무엇을\s*도와|도와\s*드리|"
                       r"말씀해\s*주|질문이?\s*있으시|문의(해|하)\s*주")


def _is_meta(s):
    t = s.strip()
    return bool(_CITE.match(t)) or bool(_LEADIN.search(t)) or t.endswith((":", "："))


def _ctx_numbers(context):
    """근거에 등장한 수치를 (원값, 표시값) 두 형태로 모은다."""
    plain, krw = set(), set()
    for v, u, _ in _numbers(context or ""):
        plain.add(round(abs(v), 6))
        if u in _CUR:
            krw.add(round(abs(v) * _CUR[u], 6))
    return plain, krw


def _supported_number(v, unit, step, plain, krw, derived):
    """수치 1건이 근거로 지지되는가."""
    a = abs(v)
    tol = max(step / 2.0, a * 1e-9)
    for pool in (plain, derived):
        for c in pool:
            if abs(c - a) <= tol:
                return True
    if unit in _CUR:
        k = a * _CUR[unit]
        ktol = max(step * _CUR[unit] / 2.0, k * 1e-9)
        for c in krw:
            if abs(c - k) <= ktol:
                return True
        # 근거 표의 숫자는 **배율이 캡션에 있어** 맨숫자로 실린다(`(단위: 백만원)`).
        # 그래서 근거 값이 어떤 표시 배율인지 알 수 없다 — 표준 배율을 전부 시도한다.
        # 333.6조원(답변) ↔ 333,605,938(근거, 실제로는 백만원)이 이 경로로 맞는다.
        for c in plain:
            for sc in _CUR.values():
                if abs(c * sc - k) <= ktol:
                    return True
    return False


def _support_text(sentence, context):
    """서술 문장이 근거 텍스트로 뒷받침되는가 — 명사 어절 겹침으로 본다."""
    toks = {w for w in re.findall(r"[가-힣A-Za-z]{2,}", sentence) if len(w) >= 2}
    if not toks:
        return True
    ctx = context or ""
    hit = sum(1 for w in toks if w in ctx)
    return hit / len(toks) >= 0.3


def verify(claims, context, computed=()):
    """클레임별 판정. `computed`는 감사 로그의 연산 결과(파생 수치 인정용)."""
    plain, krw = _ctx_numbers(context)
    derived = {round(abs(float(c)), 6) for c in computed
               if isinstance(c, (int, float))}
    out = []
    for c in claims:
        if c.kind in ("부재선언", "메타"):
            out.append(Verdict(c, True, c.kind))
        elif c.kind == "수치":
            ok = _supported_number(c.value, c.unit, c.step, plain, krw, derived)
            out.append(Verdict(c, ok, "근거 일치" if ok else "근거에 없는 수치"))
        else:
            ok = _support_text(c.sentence, context)
            out.append(Verdict(c, ok, "근거 겹침" if ok else "근거에 없는 서술"))
    return out

#: **밸류에이션·목표주가 차단** — 코퍼스에 주가가 없으므로 이 지표들은 **산출 자체가 불가**하다.
#: 근거: 주최 제공 `universe.csv`에 `market_cap`은 있으나 주가·거래량 열은 없다(스키마 확인).
DENIAL = re.compile(
    r"확인되지\s*않|확인할\s*수\s*없|제공(되지\s*않|할\s*수\s*없|해\s*드릴\s*수\s*없)|"
    r"산출할\s*수\s*없|계산할\s*수\s*없|생성하지\s*않|다루지\s*않|"
    r"포함되어\s*있지\s*않|코퍼스에\s*(는\s*)?(없|포함)|주가[·, ]*시세가\s*없|"
    r"답변(할|해\s*드릴|\s*드릴)\s*수\s*없|찾을\s*수\s*없|알\s*수\s*없|"
    r"관련(이)?\s*없는\s*질문|"
    r"정보가\s*아[닙니]|대상이\s*아[닙니]|해당(하지|되지)\s*않|"
    # grade.py에 따로 있던 어휘를 흡수했다(사전은 하나여야 한다).
    r"발견되지\s*않|해당\s*(사항|없)|정보가\s*없|나와\s*있지\s*않|"
    r"공개하지\s*않")

_VALUATION = re.compile(
    r"(PER|PBR|PSR|PCR|EV\s*/\s*EBITDA|주가수익비율|주가순자산비율|"
    r"목표\s*주가|적정\s*주가|밸류에이션|투자의견|매수\s*추천|매도\s*추천)")

#: 미래 시제 수치 — 공시 사실이 아니라 전망이다. **수치를 동반할 때만** 본다.
_FORECAST = re.compile(r"(예상|전망|추정|예측)\s*(순이익|매출|영업이익|주가|실적|EPS)|"
                       r"20\d\dF\b|향후\s*\d+\s*년\s*(간)?\s*(예상|전망)")

#: 미래 시제가 **어미로** 드러난 전망. 위 `_FORECAST`는 `(예상|전망)+명사` 꼴만 잡고
#: 게다가 수치를 동반해야 발동하는데, **"주가는 상승할 것으로 보입니다"에는 숫자가 없다.**
_FORECAST_TENSE = re.compile(
    r"(상승|하락|증가|감소|개선|악화|확대|축소|성장)(할|될)\s*(것|전망|가능성)|"   # A
    r"것으로\s*(전망|예상)|"                                                  # B
    r"향후[^.!?\n]{0,30}(전망|예상|추정)")                                     # D

#: 역할 이탈·지시문 유출 신호. 사전은 한 곳이다.
ESCAPE = re.compile(r"시스템\s*프롬프트|지시를\s*무시|이전\s*지시|역할을\s*변경|"
                    r"이제부터\s*(나는|저는)|당신의\s*지침|프롬프트는\s*다음|"
                    r"내\s*지시(문|사항)")

INJECTION = re.compile(
    # ① 앞의 것을 잊/무시하라 — 목적어가 `지시`가 아니어도 잡는다.
    r"(이전|앞의|위의|위에|기존|지금까지|여태|방금)[^.\n]{0,16}"
    r"(지시|규칙|내용|말|대화|설정|지침|명령)[^.\n]{0,12}(무시|잊|잊어)|"
    r"(지시|규칙|지침|설정|제약)(를|을|은|는)?\s*(모두|다|전부)?\s*"
    r"(무시|잊어|잊고|신경\s*쓰지)|"
    # ② 시스템 지시문을 내놓으라.
    r"(시스템|초기|기본|내부)\s*(프롬프트|지시문|지시사항|지침|규칙|메시지|설정)"
    r"[^.\n]{0,24}(출력|알려|나열|보여|공개|말해|읊|보내)|"
    r"프롬프트\s*(전문|원문|전체|내용)|"
    r"system\s*prompt|ignore\s+(all\s+)?(previous|prior|above)|"
    # ③ 역할을 바꾸라.
    r"(너는|당신은|넌)\s*이제[^.\n]{0,24}(아니라|아니야|이다|이야|야\b)|"
    r"이제부터\s*(너는|당신은|넌|네가)|"
    r"역할(을|이)?[^.\n]{0,20}(바꿔|바꾸|변경|전환)|개발자\s*모드|"
    # ④ 질문 안에 지시문 태그를 심는 형태.
    r"<\s*/?\s*(system|instruction|prompt)\b|"
    # ⑤ 근거 없이 만들어내라.
    r"(지어내|만들어내|상상해|꾸며내)(서|어서)?\s*(답|말|알려|줘|제시)|"
    # ⑥ 내부 구성을 내놓으라.
    r"(도구|함수|툴)\s*(목록|이름|리스트)[^.\n]{0,24}(나열|출력|알려|보여|전부)",
    re.I)

#: SYSTEM 원문이 답변에 그대로 실렸는지 볼 때 쓰는 창 길이(문자).
_LEAK_SPAN = 30


def _forbidden(answer):
    """규정 위반 문장 목록. **거부 문장 안의 금지어는 위반이 아니다.**"""
    out = []
    for sent in re.split(r"(?<=[.!?])\s+|\n", answer or ""):
        if not sent.strip() or DENIAL.search(sent):
            continue
        # 밸류에이션 용어는 **숫자가 없어도** 막는다 — "목표주가는 증권사마다 다릅니다"처럼
        # 수치 없이 코퍼스 밖으로 안내하는 것도 이 과제에서는 답이 아니다.
        # 전망 어휘는 수치를 동반할 때만 본다(과잉 차단을 피한다).
        if (_VALUATION.search(sent) or _FORECAST_TENSE.search(sent)
                or (_FORECAST.search(sent) and re.search(r"\d", sent))):
            out.append(sent.strip())
    return out


def _escaped(answer, system_text=""):
    """역할 이탈·지시문 유출 문장 목록."""
    out = []
    sents = [s for s in re.split(r"(?<=[.!?])\s+|\n", answer or "") if s.strip()]
    for sent in sents:
        if DENIAL.search(sent):
            continue
        if ESCAPE.search(sent):
            out.append(sent.strip())
    if system_text:
        flat = " ".join((answer or "").split())
        sysflat = " ".join(system_text.split())
        for i in range(0, len(sysflat) - _LEAK_SPAN + 1):
            frag = sysflat[i:i + _LEAK_SPAN]
            if frag in flat:
                out.append(f"[지시문 유출] …{frag}…")
                break
    return out

#: ── 출력 위생 ─────────────────────────────────────────────────
#: 차단(ROLE_HOLD 전면 대체)이 아니라 **문장 단위로 덜어낸다** — 남은 답변이 정직한
#: 한계 고지인 경우가 있다. 어휘는 `audit.probe` 실측 답변에서 뽑았고, 과거 답변
#: 4,459건 전수에 대고 오탐을 쟀다: 자기정체성 1 · 상용서비스 1 · URL 3 — 전부 진짜 위반.
#: ★ DART·금융감독원 안내는 일부러 막지 않는다 — 우리 코퍼스의 출처이고 정직한 안내다.

#: ① 자기 정체성 오설명 — 우리는 학습 데이터가 아니라 주어진 코퍼스로 답한다.
_SELF_DESC = re.compile(r"(제가|저는|내가)\s*학습(한|된|하)|학습\s*데이터|"
                        r"훈련\s*데이터|지식\s*컷오프")
#: ② 상용 서비스 안내 — 코퍼스 밖으로 보내면 안 된다(제공 코퍼스 외 데이터 금지).
_EXT_SERVICE = re.compile(r"네이버\s*금융|다음\s*금융|증권\s*(관련\s*)?웹\s*?사이트|"
                          r"증권\s*사이트|증권사\s*(앱|HTS|MTS|홈페이지)|"
                          r"한국거래소\s*홈페이지|(야후|구글)\s*파이낸스|"
                          r"인베스팅닷컴|에프앤가이드")
#: ③ URL — 도구는 URL을 준 적이 없다. 나오면 모델이 지어낸 것이다.
#:   문장을 지우지 않고 URL만 뗀다 — 그 문장에 근거가 실려 있을 수 있다.
_MD_LINK = re.compile(r"\[([^\]]{1,120})\]\(\s*https?://[^)\s]+\s*\)")
_BARE_URL = re.compile(r"\(?\s*<?https?://[^\s)\]>]+>?\s*\)?")


#: ④ 지어낸 `section:` — 도구가 준 적 없는 섹션명을 답변이 적는다.
#:   실측(배포 서버 종단 확인): `get_financials` 는 `section` 을 주지 않는데 답변이
#:   `(근거: 분기보고서 (2025.09), section: net income)` 이라고 적었다(평가지표 4 환각).
#:   ★ 판정은 **관측의 `section` 필드 값인가**로 본다. 전 회차 실측(답변 4,459건 ·
#:     section 값 33종 115회) 대부분이 섹션이 아니라 도구 반환 키·계정 라벨이다 —
#:     `계수` 27회 · `재고자산` 17회 · `revenue` 2회 · `없음` 4회. 이 말들은 관측
#:     본문에는 있으므로 "관측 어딘가에 있나"로 보면 안 걸린다.
_ANS_SECTION = re.compile(r"\s*[,·]?\s*(?:section|섹션)\s*[:：]\s*([^),\n]{1,80})")
#: 관측에서 **section 필드의 값**만 꺼낸다.
_OBS_SECTION = re.compile(r"""['"]?(?:section|섹션)['"]?\s*[:：]\s*['"]?([^'"\n,}]{2,200})""")


def drop_fake_section(answer, context):
    """관측의 `section` 값이 아닌 `section: X` 구절을 뗀다. (정리된 답변, 뗀 것)."""
    if not answer or not _ANS_SECTION.search(answer):
        return answer, []
    real = [re.sub(r"\s+", " ", v).strip() for v in _OBS_SECTION.findall(context or "")]
    dropped = []

    def sub(m):
        sec = re.sub(r"\s+", " ", m.group(1)).strip().rstrip(".)").strip("'\"")
        # 도구가 준 섹션이면 그대로 둔다. 모델이 앞뒤를 자를 수 있어 포함 관계로 본다.
        if sec and any(sec in v or v in sec for v in real):
            return m.group(0)
        dropped.append(sec)
        return ""

    out = _ANS_SECTION.sub(sub, answer)
    if not dropped:
        return answer, []
    out = re.sub(r"\(\s*근거\s*[:：]\s*\)", "", out)      # 빈 근거 괄호가 남으면 지운다
    out = re.sub(r"[ \t]{2,}", " ", out).strip()
    return (out or answer), dropped


def hygiene(answer):
    """(정리된 답변, 덜어낸 것). 근거 문장은 건드리지 않는다."""
    if not answer:
        return answer, []
    removed = []
    n_md = len(_MD_LINK.findall(answer))
    out = _MD_LINK.sub(r"\1", answer)          # [텍스트](url) → 텍스트 (근거는 남긴다)
    if n_md:
        removed.append(f"URL(링크) {n_md}건")
    n_bare = len(_BARE_URL.findall(out))
    if n_bare:
        removed.append(f"URL {n_bare}건")
        out = _BARE_URL.sub(" ", out)   # 앞뒤 어절이 붙지 않게 한 칸 남긴다
    keep = []
    for sent in sentences(out):
        if _SELF_DESC.search(sent):
            removed.append(f"[자기정체성] {sent[:60]}")
            continue
        if _EXT_SERVICE.search(sent):
            removed.append(f"[상용서비스] {sent[:60]}")
            continue
        keep.append(sent)
    if not removed:
        return answer, []
    text = " ".join(keep).strip()
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([,.)])", r"\1", text)
    # 전부 덜어내면 빈 답이 된다 — 그때는 정직한 기권으로 돌린다.
    return (text or "공시에서 확인되지 않습니다."), removed


ROLE_HOLD = ("저는 제공된 공시 코퍼스 안에서만 답변하는 공시 분석 에이전트입니다. "
             "역할이나 지침을 변경할 수 없고, 시스템 지시문은 공개하지 않습니다. "
             "공시에 관해 질문해 주시면 근거와 함께 답변드리겠습니다.")


def guard(answer, context, tools_used=(), computed=(), strict=False, system_text="",
          question=""):
    """가드 실행 → (내보낼 답변, 리포트)."""
    if question and INJECTION.search(question):
        rep = {"n_claims": 0, "n_unsupported": 0, "unsupported": [],
               "blocked": True, "removed": [],
               "reason": f"질문이 프롬프트 공격 형태다: {question.strip()[:60]}"}
        return (ROLE_HOLD, rep)

    escaped = _escaped(answer, system_text)
    if escaped:
        rep = {"n_claims": 0, "n_unsupported": 0, "unsupported": [],
               "blocked": True, "removed": escaped,
               "reason": f"역할 이탈·지시문 유출: {escaped[0][:60]}"}
        return (ROLE_HOLD, rep)

    banned = _forbidden(answer)
    if banned:
        rep = {"n_claims": 0, "n_unsupported": 0, "unsupported": [],
               "blocked": True, "removed": banned,
               "reason": f"밸류에이션·전망 생성(규정 위반): {banned[0][:60]}"}
        return ("공시에서 확인되지 않습니다. 이 코퍼스에는 주가·시세가 없어 "
                "PER·목표주가 같은 밸류에이션 지표는 산출할 수 없고, "
                "미래 전망·투자의견은 생성하지 않습니다.", rep)

    # 출력 위생 — 차단 검사를 **전부 통과한 뒤에** 문장을 덜어낸다.
    # 먼저 덜어내면 위반 문장을 지워 놓고 통과시키는 꼴이 된다.
    answer, hyg = hygiene(answer)
    answer, fake_sec = drop_fake_section(answer, context)
    if fake_sec:
        hyg.append(f"[지어낸 섹션] {' · '.join(fake_sec)}")

    claims = decompose(answer, tools_used)
    vs = verify(claims, context, computed)
    bad = [v for v in vs if not v.ok]
    rep = {"n_claims": len(claims), "n_unsupported": len(bad),
           "unsupported": [f"[{v.claim.kind}] {v.claim.text[:60]}" for v in bad],
           "blocked": False, "removed": [], "hygiene": hyg}

    _asserted = [v for v in vs
                 if v.claim.kind != "부재선언"
                 and not _is_meta(v.claim.sentence)
                 and not _GREETING.search(v.claim.sentence)]
    hard = [v for v in _asserted if v.claim.kind == "수치"]
    soft = [v for v in _asserted if v.claim.kind != "수치"]
    said_absent = any(v.claim.kind == "부재선언" for v in vs) or bool(DENIAL.search(answer or ""))
    factual = hard or (soft if not said_absent else [])
    if not tools_used and factual:
        rep["blocked"] = True
        rep["reason"] = "도구 호출 0회 — 코퍼스 근거 없이 사실을 주장함"
        return ("공시에서 확인되지 않습니다. "
                "제공된 공시 코퍼스에서 근거를 찾지 못했습니다.", rep)

    if strict and bad:
        drop = {v.claim.idx for v in bad}
        kept = [s for i, s in enumerate(sentences(answer)) if i not in drop]
        rep["removed"] = [s for i, s in enumerate(sentences(answer)) if i in drop]
        out = " ".join(kept).strip()
        if not out:
            return "공시에서 확인되지 않습니다.", rep
        return out + "\n\n※ 근거가 확인되지 않은 일부 내용은 제외했습니다.", rep

    return answer, rep


def render(rep):
    """think_trace에 넣을 요약."""
    if rep.get("blocked"):
        return f"근거가드: 차단 — {rep.get('reason', '')}"
    if not rep["n_unsupported"]:
        return f"근거가드: 통과 (클레임 {rep['n_claims']}건 전부 지지)"
    lines = [f"근거가드: 클레임 {rep['n_claims']}건 중 미지지 {rep['n_unsupported']}건"]
    lines += [f"   · {x}" for x in rep["unsupported"][:5]]
    if rep["removed"]:
        lines.append(f"   제거 {len(rep['removed'])}문장")
    return "\n".join(lines)

if __name__ == "__main__":
    CTX = "[1] get_financials\n판매비와관리비 87,769,374 (단위: 백만원) 매출액 333,605,938"
    CASES = [
        ("도구 0회 + 산업 서술(2-2 유형)",
         "반도체 섹터는 IoT, 자율주행차 등에서 수요가 증가하고 경쟁이 치열한 산업입니다.", CTX, ()),
        ("인사 — 도구 0회여도 통과",
         "안녕하세요! 공시에 관한 질문이 있으시면 말씀해 주세요.", "", ()),
        ("부재선언 — 통과", "공시에서 확인되지 않습니다.", "", ()),
        ("근거 있는 수치", "판매비와관리비는 87,769,374 백만원입니다.", CTX, ("get_financials",)),
        ("단위 환산 표기", "매출액은 약 333.6조원입니다.", CTX, ("get_financials",)),
        ("근거에 없는 수치", "판관비는 48,445,100 백만원입니다.", CTX, ("get_financials",)),
    ]
    for name, ans, ctx, tools in CASES:
        out, rep = guard(ans, ctx, tools)
        print(f"\n■ {name}")
        print("   ", render(rep))
        if out != ans:
            print("    → 대체:", out[:60])
