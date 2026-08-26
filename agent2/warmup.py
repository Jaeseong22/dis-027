"""서빙 전 예열 — **찬 캐시로 첫 질의를 받지 않는다.**"""
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
    """예열 대상 (기업, 표id, 연도) 전체. 70개사 × 정본표 17종 = 1,190개."""
    from agent2.tools import doctables as D
    return [(r["corp_name"], spec.id, store.latest_fiscal_year(r["corp_name"]))
            for r in store.universe() for spec in D.TABLES]


def canon_cold(keys=None):
    """아직 캐시되지 않은 정본표 키."""
    from agent2.tools import doctables as D
    keys = canon_keys() if keys is None else keys
    return [k for k in keys if not os.path.exists(D.cache_path(*k))]


def canon_warm(verbose=True):
    """정본표 결과를 디스크에 채운다. (새로 만든 수, 이미 있던 수)."""
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
