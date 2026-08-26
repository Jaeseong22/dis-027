"""2호기 평가용 API 서버 — stdlib http.server(무의존).

1호기 `agent/server.py`와 **계약·견고성 규칙이 동일**하다. 다른 것은 기본 포트뿐(8001) —
1호기와 동시에 띄워 같은 질의를 양쪽에 쏴야 A/B가 성립하기 때문이다.

의도적 중복: 1호기 서버를 import해 재사용하지 않는다. 재사용하면 2호기 작업이 1호기 파일을
건드리게 되고(설계 규칙: `agent.*`는 읽기만), 한쪽 수정이 다른 쪽 계약을 조용히 바꾼다.
공유하는 것은 계약 정의(`agent2.contract`) 하나뿐이다.

견고성(1호기 R1 #21에서 배운 것 — 어떤 입력에도 JSON):
  · question/question_id 비문자열 → 400 JSON
  · 핸들러 예외 → 500 JSON (커넥션 끊김 금지)
  · http.server 기본 HTML 오류(414 등) → send_error 오버라이드로 JSON화
헬스체크: GET /health   실행: python -m agent2.server [port]
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from agent2 import agent as A2
from agent2.contract import CONTRACT

DEFAULT_PORT = 8001


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        try:
            body = json.dumps({"error": message or f"HTTP {code}", "code": code},
                              ensure_ascii=False).encode("utf-8")
            self.send_response_only(code, message)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def _answer(self, question, question_id):
        if question is not None and not isinstance(question, str):
            return self._send({"error": "question must be a string"}, 400)
        if question_id is not None and not isinstance(question_id, str):
            question_id = str(question_id)
        try:
            r = A2.answer(question or "", question_id)
        except Exception as e:
            return self._send({"error": f"internal error: {type(e).__name__}", "code": 500,
                               "question_id": question_id}, 500)
        self._send({k: r.get(k, "") for k in CONTRACT})

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self._send({"status": "ok", "agent": "2호기"})
        if u.path != "/answer":
            return self._send({"error": "not found"}, 404)
        qs = parse_qs(u.query)
        self._answer(qs.get("question", [""])[0], qs.get("question_id", [None])[0])

    def do_POST(self):
        if urlparse(self.path).path != "/answer":
            return self._send({"error": "not found"}, 404)
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send({"error": "invalid JSON body"}, 400)
        if not isinstance(body, dict):
            return self._send({"error": "body must be a JSON object"}, 400)
        self._answer(body.get("question", ""), body.get("question_id"))

    def log_message(self, *a):
        pass


def serve(port: int = DEFAULT_PORT, warm: bool = True):
    """서빙. 기본으로 **예열하고 나서** 포트를 연다.

    예열을 기동에 붙인 이유(실측): 찬 캐시로 첫 질의를 받으면 KB금융 PDF 표 추출에
    약 116초가 든다. 주최는 동기 GET이라 그 질의는 타임아웃 = 0점이다.
    기동을 한 번 늦추는 편이 첫 질의를 잃는 것보다 낫다(`agent2/warmup.py` 참조).
    """
    if warm:
        from agent2 import warmup
        todo = warmup.cold()
        if todo:
            print(f"예열 중 — 찬 PDF {len(todo)}건 (수 분 걸릴 수 있음)")
        warmup.run(verbose=bool(todo))

    from agent2.core import llm as L
    cur = L.LLM()
    print(f"2호기 공시 Q&A API — http://0.0.0.0:{port}/answer (GET·POST) · /health")
    print(f"  LLM: {cur.profile.name} / {cur.model} — "
          f"{'실호출 가능' if cur.available() else '키 없음(확인 불가 응답만)'}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    import sys
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    serve(int(args[0]) if args else DEFAULT_PORT, warm="--no-warm" not in sys.argv)
