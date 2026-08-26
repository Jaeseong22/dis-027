"""데이터 계층 — 주최가 준 모든 데이터에 접근한다.

    store.universe()   기업 마스터 70
    store.manifest()   원문 보유 문서 4,204
    store.catalog()    공시 목록 전체 22,980 (원문 없는 18,776 포함)
    source.parts()     문서 폴더의 모든 파일(xml 본문+첨부 · html · pdf)
"""
from agent2.data import store, source          # noqa: F401
