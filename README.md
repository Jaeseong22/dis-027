# dis-027 — 공시 Agent

**제10회 미래에셋증권 AI Festival · 주제3 · 팀 부산싸나이**

고정 공시 코퍼스(70개사 · 2023-01 ~ 2026-06) 위에서 도는 **자연어 Q&A 에이전트**.
모든 답변에 근거(접수번호)를 붙인다. 리포트 생성기가 아니다.

**수치는 결정론 코드가 뽑고, 서술만 LLM이 한다.** LLM은 *어떤 도구를 어떤 인자로 부를지*만
정하고, 값 추출·산술은 `agent2/tools/`의 결정론 코드가 한다.

---

## 0. 평가용 API End-point

```
http://101.79.19.164/answer
```

```bash
curl -G "http://101.79.19.164/answer" \
     --data-urlencode "question_id=Q-001" \
     --data-urlencode "question=삼성전자의 2025년 연결기준 매출액은?"
```

```json
{
  "question_id":       "Q-001",
  "question":          "삼성전자의 2025년 연결기준 매출액은?",
  "retrieved_context": "답변 생성에 참고한 검색 문서",
  "think_trace":       "사고 · 추론 · 도구 사용 과정",
  "answer":            "최종 생성 답변"
}
```

- 경로 `/answer` 고정 · HTTP 80 · 별도 헤더나 인증 없음
- 5필드 고정이고 값은 전부 **문자열**이다 (`agent2/contract.py`가 강제)
- 헬스체크는 `GET /health` — `HEAD`는 501을 낸다
- 서버: 네이버클라우드 `c2-g3a`(2 vCPU / 4 GB / 20 GB) · Ubuntu 24.04 ·
  systemd 서비스 `gongsi` · 코퍼스는 `/data/corpus`

---

## 1. 환경 구성

```bash
python3 -m pip install -r requirements.txt
```

의존성은 **셋뿐이고 전부 선택적**이다(`certifi` · `pdfplumber` · `pypdf`).
코어는 표준 라이브러리만 쓰므로 설치가 실패해도 에이전트는 동작한다(해당 기능만 비활성).
`pdftotext`(poppler)가 있으면 PDF로만 제공된 정기공시 3건을 더 빨리 읽는다.

### 1.1 코퍼스 연결 (필수)

주최 제공 원문 5.2GB는 **이 저장소에 담지 않았다.** 경로를 환경변수로 가리킨다.

```bash
export CORPUS_DIR=/경로/3.공시/corpus     # universe.csv · manifest.jsonl · raw/ 가 있는 곳
```

확인:
```bash
python3 -c "from agent2.data import store; print(len(store.universe()))"   # → 70
```

### 1.2 LLM 자격증명 (필수)

```bash
cp agent2/.env.example agent2/.env
# LLM_PROFILE=hcx  ·  CLOVA_API_KEY=<CLOVA Studio **서비스** API 키>
```

★ **서비스 키**여야 한다(테스트 키로는 HCX-005가 안 열린다). 서빙 전에 확인한다:

```bash
python3 -m agent2.core.llm
#   ✓ hcx  HCX-005  https://clovastudio.stream.ntruss.com/v1/openai
#   현재 프로필: hcx (HCX-005) — 실호출 가능
```

### 1.3 캐시 예열 (서빙 전 필수)

```bash
python3 -m agent2.warmup          # 정본표 + PDF · 약 40초
python3 -m agent2.data.filings    # 정형공시 3,150건 · 약 30초
python3 -m agent2.warmup --check  # "전부 예열됨 — 바로 서빙 가능"
```

★ 찬 캐시로 첫 질의를 받으면 PDF 표 추출에 약 116초가 든다. 그래서 `serve()`가
기본으로 **예열하고 나서 포트를 연다**.

---

## 2. 실행

```bash
python3 -m agent2.server 8001
```

기동 로그에 `LLM: hcx / HCX-005 — 실호출 가능` 이 나오는지 확인한다.

### 평가용 API

```
GET /answer?question_id=<id>&question=<질의>
→ {question_id, question, retrieved_context, think_trace, answer}   (전부 문자열)
```

```bash
curl -G "http://127.0.0.1:8001/answer" \
  --data-urlencode "question_id=Q-001" \
  --data-urlencode "question=삼성전자의 2025년 연결기준 매출액은?"
```

`POST /answer`(JSON 본문)와 `GET /health`도 지원한다.
★ `HEAD`를 포함한 그 밖의 메서드는 **501**을 낸다 — 헬스체크를 `HEAD /health`로 걸면 안 된다.

### 단일 질의 (서버 없이)

```bash
python3 -m agent2.agent            # 스모크
python3 -m agent2.loop "삼성전자 2025년 매출액은?"
```

---

## 3. 전처리 산출물

`agent2/.cache/`에 들어 있다(저장소에 포함 · 90 MB). 서빙 전에 채워야 하고,
없으면 첫 질의가 느려지거나 정형공시 필드가 붙지 않는다.

```
canon_tables/   정본표 2,380건    python3 -m agent2.warmup
filings/        정형공시 3,150건  python3 -m agent2.data.filings
pdf_tables/     PDF 표 3건        (warmup에 포함)
```

---

## 4. 구조

```
agent2/
├── server.py        평가용 API (stdlib http.server · 무의존)
├── agent.py         진입점 — answer(question, id) → 계약 5필드
├── contract.py      계약 5필드 단일 진실
├── loop.py          PAO 루프(Plan·Act·Observe) · SYSTEM 프롬프트
├── answer_spec.py   질문 유형별 답변 요건
├── config.py        경로·상수 (CORPUS_DIR)
├── core/llm.py      LLM 어댑터 (HyperCLOVA X · 무의존 stdlib)
├── data/            원문 접근·정규화
│   ├── store.py       universe / manifest / 공시목록
│   ├── source.py      파일 읽기(인코딩·PDF)
│   ├── parse.py       원문 → 섹션 트리·표
│   └── filings.py     정형공시 3,150건 배치
└── tools/           **결정론 추출 — 도구 12개**
    ├── doctables.py   정본표 34종 (섹션경로 + 셀시그니처로 표 확정)
    ├── docsections.py 정본섹션 4종 (답이 문단에 있는 질의)
    ├── xbrl.py        개념 → XBRL 코드
    ├── finance.py     재무제표 추출
    ├── filingtypes.py 공시유형 23종 계수
    ├── corrections.py 정정 체인 (원공시 ↔ 정정본)
    ├── claims.py      **답변 출력 단일 관문** — 근거·규정·인젝션 방어
    └── search.py      BM25
```

### 설계 요지

- **도구 12개 상한.** 선택 정확도가 개수에 민감하다(BFCL 4→51개에서 43%→2%).
- **fail-closed.** 표를 확정하지 못하면 값을 내지 않는다 — 오추출보다 정직한 부재가 낫다.
- **근거 가드.** 도구를 안 쓰고 사실을 주장하면 차단한다. 밸류에이션·미래전망은 코드가 막는다.
- **프롬프트 공격.** 질문 쪽에서 탐지해 역할을 고수한다(공격 6/6 차단 · 지시유출 0/40).

---

## 5. 제약

- 제공 코퍼스 밖 데이터를 쓰지 않는다. 외부 API를 호출하지 않는다
  (`requests`·`socket` 의존 0 · `urllib`은 LLM 호출과 서버에만 쓴다).
- 공시 근거 없는 미래예측·투자의견을 생성하지 않는다. 확인 불가는 그렇게 답한다.
- 코퍼스에 없는 기업명은 **"코퍼스에 없는 기업"이라고 밝힌다** — 비슷한 회사를 추측하지 않는다.
