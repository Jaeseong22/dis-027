"""서빙 전 예열 — **찬 캐시로 첫 질의를 받지 않는다.**

왜 필요한가(실측으로 발견):
  시나리오를 실제로 돌리다가 "KB금융 2025년 자산총계"가 **120.9초**를 썼다.
  같은 질의를 다시 돌리면 4.4초다. 차이는 전부 PDF 표 추출 캐시였다 —
  KB금융 [기재정정]사업보고서는 PDF가 4.2MB(viewer.html의 35배)이고,
  pdfplumber 첫 파싱이 약 116초 걸린다.

  주최는 `GET /answer`로 **동기 호출**한다. 첫 질의가 이 비용을 물면 타임아웃이고,
  타임아웃은 0점이다. 캐시는 결선 배포에서 항상 차갑다.

그래서 기동할 때 미리 데운다. 대상은 두 가지뿐이고 둘 다 유한하다:
  ① 데이터 캐시 — universe(70) · manifest(4,204) · catalog(22,980)
  ② PDF 표 캐시 — 코퍼스 전체에서 PDF를 가진 문서(소수)

실행:
    python3 -m agent2.warmup          # 예열하고 소요를 보고
    python3 -m agent2.warmup --check  # 데우지 않고 무엇이 찬지만 확인
"""
import os
import time

from agent2.data import parse, source, store


def pdf_paths():
    """코퍼스 안 모든 PDF 경로. manifest 전수를 훑는다(골라내지 않는다)."""
    out = []
    for row in store.manifest():
        for _fname, kind, path in source.files(row):
            if kind == "pdf":
                out.append((row.get("corp_name", ""), path))
    return out


def cold(paths=None):
    """아직 캐시되지 않은 PDF 목록."""
    paths = pdf_paths() if paths is None else paths
    return [(c, p) for c, p in paths if not os.path.exists(parse.pdf_cache_path(p))]


def run(verbose=True):
    """데이터 캐시 + PDF 표 캐시를 채운다. 소요(초)를 돌려준다."""
    t0 = time.time()
    n_corp = len(store.universe())
    n_doc = len(store.manifest())
    n_cat = len(store.catalog())
    t_data = time.time() - t0
    if verbose:
        print(f"데이터 캐시 · 기업 {n_corp} · 문서 {n_doc} · 목록 {n_cat} · {t_data:.1f}초")

    paths = pdf_paths()
    todo = cold(paths)
    if verbose:
        print(f"PDF {len(paths)}건 중 찬 것 {len(todo)}건")
    for corp, path in todo:
        t1 = time.time()
        try:
            tables = parse.pdf_tables(path)
            note = f"표 {len(tables)}개"
        except Exception as e:
            note = f"실패 {type(e).__name__}: {e}"
        if verbose:
            print(f"  {corp:12s} {time.time()-t1:6.1f}초  {note}  "
                  f"{os.path.basename(path)[:50]}")
    n_new, n_hit = canon_warm(verbose=verbose)
    if verbose:
        print(f"정본표 캐시 · 새로 만든 것 {n_new} · 이미 있던 것 {n_hit}")

    total = time.time() - t0
    if verbose:
        print(f"예열 완료 {total:.1f}초 · 남은 찬 PDF {len(cold(paths))}건 "
              f"· 찬 정본표 {len(canon_cold())}건")
    return total


# ─────────────────────────────────────────────── 정본표 캐시
def canon_keys():
    """예열 대상 (기업, 표id, 연도) 전체. 70개사 × 정본표 17종 = 1,190개.

    ★ 연도를 **여기서 해소**한다(2026-08-16). `values()`는 캐시 키를 만들기 전에
      `year or latest_fiscal_year(corp)`로 해소하므로 파일은 `(corp, tid, 2025)`에
      쓰인다. 그런데 `canon_cold`는 `(corp, tid, None)`을 찾고 있어 **항상 미스**였다 —
      캐시가 다 차 있어도 `--check`가 콜드 1,190건이라 했고 `canon_warm`은
      `새로 만든 것 1,190 · 이미 있던 것 0`으로 보고했다(실제로는 0.5초에 끝나는 전건 적중).
      `values()`의 주석이 같은 함정을 이미 기록하고 있었는데 그때 `values()`만 고쳤다.
    """
    from agent2.tools import doctables as D
    return [(r["corp_name"], spec.id, store.latest_fiscal_year(r["corp_name"]))
            for r in store.universe() for spec in D.TABLES]


def canon_cold(keys=None):
    """아직 캐시되지 않은 정본표 키."""
    from agent2.tools import doctables as D
    keys = canon_keys() if keys is None else keys
    return [k for k in keys if not os.path.exists(D.cache_path(*k))]


def canon_warm(verbose=True):
    """정본표 결과를 디스크에 채운다. (새로 만든 수, 이미 있던 수).

    왜 예열하나(전수 실측 2026-08-13): `find_tables` 콜드가 70개사 합계 98.5초인데
    웜은 15.3초다. 그 차이의 약 40%가 이 캐시이고, 나머지는 BM25 색인이다.
    캐시 자체는 **3.36MB**로 작다(중앙 41.6KB · 최대 122.7KB).
    """
    from agent2.tools import doctables as D
    keys = canon_keys()
    todo = canon_cold(keys)
    n_hit = len(keys) - len(todo)
    done = 0
    for i, (corp, tid, year) in enumerate(todo, 1):
        try:
            D.values(corp, tid, year)
            done += 1
        except Exception as e:                      # 한 표가 죽어도 예열은 계속한다
            if verbose:
                print(f"  정본표 실패 {corp}/{tid}: {type(e).__name__}: {e}")
        if verbose and i % 200 == 0:
            print(f"  정본표 {i}/{len(todo)}")
    return done, n_hit


if __name__ == "__main__":
    import sys

    if "--check" in sys.argv:
        paths = pdf_paths()
        todo = cold(paths)
        ckeys = canon_keys()
        ctodo = canon_cold(ckeys)
        print(f"PDF {len(paths)}건 · 찬 것 {len(todo)}건")
        for corp, p in todo:
            print(f"  찬 캐시 {corp} — {os.path.basename(p)[:60]}")
        print(f"정본표 {len(ckeys)}건 · 찬 것 {len(ctodo)}건")
        if not todo and not ctodo:
            print("전부 예열됨 — 바로 서빙 가능")
    else:
        run()
