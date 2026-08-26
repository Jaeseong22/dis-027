"""공시 Agent — PAO(계획·행동·관찰) 루프.

LLM은 **어떤 도구를 어떤 인자로 부를지만** 정한다. 값 추출과 산술은 `tools/`의
결정론 코드가 한다 — 수치를 LLM에 만들게 하면 환각이 들어오고, 이 과제는
근거기반이 평가 축이다.

    agent.py     진입점 — answer(question, id) → 계약 5필드
    loop.py      PAO 루프 · SYSTEM 프롬프트
    tools/       결정론 추출(도구 12개) · 답변 출력 관문
    data/        원문 접근·정규화
    core/llm.py  LLM 어댑터(프로필 교체만으로 제공자 전환)
"""
