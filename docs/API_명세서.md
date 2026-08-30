# 평가용 API 명세서 — 공시 Agent (dis-027)

제출 항목 3 · **End-point URL + 요청/응답 JSON 스키마**.
아래 값은 전부 **실제로 서버를 띄워 측정한 것**이다(2026-08-31).

---

## 1. End-point

```
http://101.79.19.164/answer
```

| | |
|---|---|
| 프로토콜 | HTTP (포트 80). HTTPS·도메인 없음 — 주최 확인 사항에 따라 공인 IP로 제출 |
| 경로 | `/answer` 고정 |
| 메서드 | `GET` (평가 경로) · `POST`도 지원 |
| 인증·헤더 | 없음 |
| 문자셋 | UTF-8 (`Content-Type: application/json; charset=utf-8`) |
| 헬스체크 | `GET /health` → `{"status":"ok","agent":"dis-027"}` |

★ **헬스체크는 반드시 `GET`.** `HEAD /health`는 `501`을 낸다(§4). 모니터를 `HEAD`로 걸면
멀쩡한 서버가 죽은 것으로 판정된다.

---

## 2. 요청

### 2.1 GET — 평가 경로

```
GET /answer?question_id={id}&question={평가 질의}
```

| 파라미터 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `question_id` | string | 아니오 | 응답에 그대로 되돌려준다. 생략하면 빈 문자열 |
| `question` | string | 아니오 | 평가 질의 원문(UTF-8, URL 인코딩). **생략해도 오류가 아니다** — `200` + `"질문이 비어 있습니다."` |

```bash
curl -G "http://101.79.19.164/answer" \
     --data-urlencode "question_id=Q-001" \
     --data-urlencode "question=삼성전자의 2025년 연결기준 매출액은?"
```

```python
import requests
resp = requests.get(
    "http://101.79.19.164/answer",
    params={"question_id": "Q-001", "question": "삼성전자의 2025년 연결기준 매출액은?"},
    timeout=300,
)
result = resp.json()          # 아래 3장 스키마
```

### 2.2 POST

```
POST /answer
Content-Type: application/json

{"question_id": "Q-001", "question": "평가 질의"}
```

본문은 **JSON 객체**여야 한다. 배열·스칼라·비JSON은 `400`.

---

## 3. 응답 스키마

**5필드 고정. 값은 전부 문자열이다.** 필드가 늘거나 줄지 않는다 —
`agent2/contract.py`의 `shape()`가 API 경계에서 강제하고 `validate()`가 위반을 검출한다.

```json
{
  "question_id":       "Q-001",
  "question":          "평가 질의 원문",
  "retrieved_context": "답변 생성에 참고한 검색 문서",
  "think_trace":       "사고 · 추론 · 도구 사용 과정",
  "answer":            "최종 생성 답변"
}
```

| 필드 | 타입 | 내용 |
|---|---|---|
| `question_id` | string | 요청의 `question_id`를 그대로 반환 |
| `question` | string | 요청의 `question`을 그대로 반환 |
| `retrieved_context` | string | **답 도출에 쓰인 근거.** 도구별로 출처(공시명·공시일·접수번호)·기준·값을 담는다 |
| `think_trace` | string | 질의 해석 → 근거 수집 → 검증 → 종료 → 감사 로그. 첫 줄이 경로·정지사유·소요를 요약 |
| `answer` | string | 최종 답변. 끝에 `(근거: 공시명 (연월), 접수번호 …)`가 붙는다 |

### 3.1 실제 응답 (측정값)

요청 `question=삼성전자의 2025년 연결기준 매출액은?`

```
필드별 길이   question_id 5 · question 22 · retrieved_context 460 ·
             think_trace 919 · answer 90   (자)
```

`answer`
```
삼성전자의 2025년 연결 기준 매출액은 **333,605,938 백만원**입니다.

(근거: 사업보고서 (2025.12), 접수번호 20260310002820)
```

`retrieved_context`
```
[1] get_financials(consolidated=True, corp='삼성전자', year=2025)
    출처   사업보고서 (2025.12), 접수번호 20260310002820
    기준   삼성전자 2025 · 연결 · 사업보고서 (2025.12) · 단위 백만원
    항목 (단위 백만원)         2023         2024         2025
    ─────────────────────────────────────────────────────────
    · 손익계산서
      매출액 (주30)    258,935,494  300,870,903  333,605,938
    (30개 항목 중 답변이 인용한 1개만 표시 — 나머지는 think_trace의 도구 관측에 있습니다)
    XBRL 태그 (ifrs-full 접두 생략)
      매출액 (주30)=Revenue
```

`think_trace` (발췌)
```
[route=pao_loop stop=answered(정상) steps=2 llm=2 tokens=6938 7.9s]
── 질의 해석 ─────
질의: 삼성전자의 2025년 연결기준 매출액은?
요건: 단위 표기, 연결/별도 명시
── 근거 수집 ─────
스텝 1: get_financials(consolidated=True, corp='삼성전자', year=2025)
        출처 사업보고서 (2025.12), 접수번호 20260310002820
        범위 연결
        획득 … (총 30개 항목)
── 검증 ─────
근거가드: 통과 (클레임 3건 전부 지지)
── 종료 ─────
정지[answered] 모델이 근거를 갖추고 답변함 · 스텝 2/3 · LLM 2/24회 · 7.9/240.0초
── 감사 로그 ─────
  연산 llm.tools(HCX-005, 1252tok) → tool_calls
  연산 tool.get_financials(…) → dict
```

★ `retrieved_context`는 공백으로 표를 맞춘 텍스트다. 그대로 보려면 **고정폭 글꼴**로 렌더한다.
★ `think_trace` 첫 줄의 `llm.tools(HCX-005, …)`로 **어떤 LLM이 붙었는지** 사후 확인할 수 있다.

### 3.2 근거가 없을 때도 스키마는 같다

부재·거절·코퍼스 밖 질의도 **`200`이고 5필드를 그대로 채운다.** 오류로 만들지 않는다 —
평가 대상은 답변 내용이기 때문이다.

```json
{ "question_id": "Q-009", "question": "…",
  "retrieved_context": "…", "think_trace": "…",
  "answer": "공시에서 확인되지 않습니다. …" }
```

---

## 4. 상태 코드 — 전 경로 실측

| 코드 | 언제 | 본문 |
|---|---|---|
| `200` | 정상 · 부재 · 거절 · **빈 질의** | 계약 5필드 |
| `400` | POST 본문이 JSON이 아님 | `{"error": "invalid JSON body"}` |
| `400` | POST 본문이 객체가 아님(배열·스칼라) | `{"error": "body must be a JSON object"}` |
| `400` | `question`이 문자열이 아님 | `{"error": "question must be a string"}` |
| `404` | `/answer`·`/health` 외 경로 | `{"error": "not found"}` |
| `414` | URL 과다 길이 | `{"error": "HTTP 414", "code": 414}` |
| `501` | `GET`·`POST` 외 메서드(**`HEAD` 포함**) | `{"error": "Unsupported method ('PUT')", "code": 501}` |
| `500` | 처리 중 예외 | `{"error": "internal error: <타입>", "code": 500, "question_id": …}` |

오류 응답은 5필드가 아니라 `error` 키를 가진다. **`answer`가 없으면 오류로 판별하면 된다.**

★ 빈 질의는 오류가 아니다 — `200` · `answer: "질문이 비어 있습니다."` ·
  `think_trace: "[route=empty_question]"`.

---

## 5. 운영 조건

| | |
|---|---|
| 응답 시간 | 중앙 7~20초 · 최대 110초(실측). **스트리밍 없음** |
| 권장 타임아웃 | **300초 이상.** 60초로 잡으면 정상 응답이 잘린다 |
| 동시성 | 주최 평가는 순차 1건. 서버는 `ThreadingHTTPServer`로 동시 요청도 받는다 |
| 멀티턴 | 없음. 매 요청이 독립이며 이전 대화를 참조하지 않는다 |
| 재기동 | systemd `Restart=always`. 기동 시 캐시를 예열한 **뒤** 포트를 연다 |

★ 첫 질의가 찬 캐시를 물면 느리다. 그래서 기동 순서를 *예열 → 포트 개방*으로 두었고
  `TimeoutStartSec=1800`을 잡았다. 배포 구성은 `README.md` 0장에 있다.

---

## 6. 스키마 준수를 무엇이 보장하는가

```
agent2/contract.py   CONTRACT 5필드 · shape()가 단일 관문 · validate()가 위반 검출
agent2/server.py     핸들러 예외를 500 JSON으로 흡수(커넥션을 끊지 않는다)
                     http.server 기본 HTML 오류(414 등)도 send_error 오버라이드로 JSON화
```

`shape()`는 누락 필드를 빈 문자열로, 비문자열을 `str`로 강제한다. 계약 외 필드는 나가지 않는다.
어떤 입력에도 **JSON**을 반환하는 것이 서버의 불변식이다.
