import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// 개발 서버에서만 쓰는 프록시. 운영에서는 Nginx 가 같은 경로를 백엔드로 넘긴다.
// 코드는 양쪽 모두 fetch('/answer?...') 로 동일하다.
const backend = {
  target: 'http://101.79.19.164',
  changeOrigin: true,
  // 답변이 최대 110초 걸린다. 기본 타임아웃으로는 끊긴다.
  timeout: 300_000,
  proxyTimeout: 300_000,
}

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/answer': backend,
      '/health': backend,
    },
  },
})
