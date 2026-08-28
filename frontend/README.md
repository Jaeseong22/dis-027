# frontend — 공시 Q&A 웹 클라이언트

React + TypeScript + Vite. 백엔드(`agent2`)는 건드리지 않는다.

```bash
npm install
npm run dev     # http://localhost:5173 — /answer·/health 는 Vite proxy 가 백엔드로 넘긴다
npm run build   # frontend/dist 생성
npm run check   # Markdown 렌더러 자체 점검
```

API 호출은 항상 상대경로(`/answer`, `/health`)다. 개발은 `vite.config.ts` 의 proxy,
운영은 Nginx 가 같은 경로를 백엔드로 넘긴다 — 코드는 양쪽이 동일하다.

운영 배포는 `dist/` 를 정적 서빙하고 두 경로만 프록시하면 된다.

```nginx
root /opt/gongsi/frontend/dist;
location / { try_files $uri /index.html; }
location ~ ^/(answer|health)$ {
    proxy_pass http://127.0.0.1:80;
    proxy_read_timeout 300s;
}
```
