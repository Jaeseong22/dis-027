"""평가용 API 서버 — stdlib `http.server`(무의존).

계약은 `agent2.contract` 하나가 소유한다. 서버·테스트가 전부 그 모듈만 본다 —
계약을 선언만 하면 경로마다 어긋난다(실제로 그 사고를 겪었다: 경로별 5/6필드 비일관 ·
HTML 오류 페이지 · 커넥션 끊김).

견고성 — **어떤 입력에도 JSON으로 답한다**:
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
            return self._send({"status": "ok", "agent": "dis-027"})
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
    print(f"공시 Q&A API — http://0.0.0.0:{port}/answer (GET·POST) · /health")
    print(f"  LLM: {cur.profile.name} / {cur.model} — "
          f"{'실호출 가능' if cur.available() else '키 없음(확인 불가 응답만)'}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    import sys
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    serve(int(args[0]) if args else DEFAULT_PORT, warm="--no-warm" not in sys.argv)
