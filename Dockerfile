# 제10회 미래에셋증권 AI Festival 주제3 — 공시 Agent
#
# 코어는 **표준 라이브러리만** 쓴다. requirements.txt의 셋은 전부 선택적 의존성이라
# 설치가 실패해도 에이전트는 동작한다(해당 기능만 비활성).
FROM python:3.12-slim

# pdftotext(poppler) — 정기공시 3건이 XML 없이 PDF만 있다. 없으면 pypdf로 물러난다.
RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY agent2/ ./agent2/

# 주최 제공 코퍼스는 이미지에 담지 않는다(5.2GB). 실행 시 마운트한다:
#   docker run -v /경로/corpus:/corpus -e CORPUS_DIR=/corpus -e LLM_PROFILE=hcx \
#              -e CLOVA_API_KEY=... -p 8001:8001 dis-027
ENV CORPUS_DIR=/corpus
EXPOSE 8001
CMD ["python3", "-m", "agent2.server", "8001"]
