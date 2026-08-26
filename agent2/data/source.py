"""문서 본문 로딩 — 폴더 안 **모든 파일**을 읽는다. 고르지 않는다.

왜 전부인가(실측):
  · 정기공시 205건이 파일 3개 구조다. 본문 XML 외 2개가
    `(첨부)재무제표+주석` · `(첨부)연결재무제표+주석` 이고, **재고자산·판매비와관리비·
    주당이익 주석이 거기 있다.** 본문만 읽으면 이 항목들을 놓친다.
  · 대체수집 3건은 PDF에 내용이 있다. KB금융 [기재정정]사업보고서(2025.12)는
    viewer.html 117,858자 vs **PDF 4,175,609자(35배)** 다.

형식은 그룹마다 다르다(4,204건 전수 실측):
  periodic  DART XML 1,051 (dart4 823 / dart3 228) + HTML 3   utf-8
  major     DART XML 598                                       utf-8
  exchange  **HTML 1,469 전건 · euc-kr · xforms_input**        확장자만 .xml
  holding   DART XML 1,083                                     utf-8

그래서 인코딩은 선언이 아니라 **시도 순서**(utf-8 → euc-kr → cp949)로 정한다.
"""
import os
import subprocess
import unicodedata

from agent2 import config
from agent2.data.store import nfc

TEXT_ENCODINGS = ("utf-8", "euc-kr", "cp949")


def _read_text(path):
    for enc in TEXT_ENCODINGS:
        try:
            with open(path, encoding=enc) as fh:
                return fh.read()
        except (UnicodeDecodeError, LookupError):
            continue
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


class PdfExtractionError(RuntimeError):
    """PDF 추출이 전부 실패했다. **빈 문자열로 감추지 않는다** —
    '내용이 없는 문서'와 '추출에 실패한 문서'는 완전히 다르고,
    후자를 조용히 빈 값으로 넘기면 그 문서만 답이 없는 이유를 영영 모른다."""


def _read_pdf(path, strict=True):
    """pdftotext(-layout, 표 정렬 보존) 우선 → pypdf 폴백.

    둘 다 실패하면 `strict=True`에서 예외를 던진다(기본).
    """
    errs = []
    try:
        r = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", path, "-"],
                           capture_output=True, timeout=300)
        if r.returncode == 0 and r.stdout:
            return r.stdout.decode("utf-8", "replace")
        errs.append(f"pdftotext rc={r.returncode} {r.stderr[:120]!r}")
    except FileNotFoundError:
        errs.append("pdftotext 미설치")
    except subprocess.TimeoutExpired:
        errs.append("pdftotext 타임아웃(300s)")
    except OSError as e:
        errs.append(f"pdftotext {type(e).__name__}")
    try:
        import pypdf
        txt = "\n".join((p.extract_text() or "") for p in pypdf.PdfReader(path).pages)
        if txt.strip():
            return txt
        errs.append("pypdf 결과 비어 있음")
    except ImportError:
        errs.append("pypdf 미설치")
    except Exception as e:
        errs.append(f"pypdf {type(e).__name__}: {e}")
    if strict:
        raise PdfExtractionError(f"{os.path.basename(path)} 추출 실패 — " + " / ".join(errs))
    return ""


def kind_of(fname):
    low = fname.lower()
    if low.endswith(".xml"):
        return "xml"
    if low.endswith((".html", ".htm")):
        return "html"
    if low.endswith(".pdf"):
        return "pdf"
    if low.endswith(".json"):
        return "list"          # list_*.json = 공시 목록. store.catalog()가 다룬다
    return "other"


def doc_dir(row):
    """manifest의 `file_path` → 실제 디스크 경로. **유니코드 정규화 형태를 맞춘다.**

    ★ 주최 안내(2026-08-04)와 전수 실측이 일치한다:
        디스크 `raw/` 하위 한글 폴더명 = **NFD**(자모 분해형)
        manifest.jsonl `file_path` · universe.csv `corp_name` = **NFC**(완성형)

    macOS(APFS)는 정규화를 무시해 어느 형태로도 열리므로 **개발 중에는 안 드러난다.**
    리눅스는 경로를 바이트 그대로 비교하므로 NFC 경로로 NFD 폴더를 못 찾는다.
    실측: 리눅스 규칙(바이트 정확 일치)으로 맞춰보면 **70개사 중 4개만** 열린다
    — 그대로 배포하면 66개사가 오류 없이 **조용히 빈 결과**를 낸다.

    그래서 있는 그대로 → NFD → NFC 순으로 시도한다. 셋 다 없으면 원래 경로를 돌려주고
    상위(`files`·`has_source`)의 `isdir` 검사가 정상적으로 False를 낸다.
    """
    fp = row.get("file_path")
    if not fp:
        return None
    base = os.path.join(config.CORPUS_DIR, fp)
    if os.path.exists(base):
        return base
    for form in ("NFD", "NFC"):
        cand = unicodedata.normalize(form, base)
        if os.path.exists(cand):
            return cand
    return base


def files(row):
    """[(파일명, 종류, 절대경로)] — 본문 파일 목록. 내용은 읽지 않는다.

    PDF 표 추출은 파일 경로가 필요해서(pdfplumber) 경로를 노출하는 진입점을 따로 둔다.
    """
    d = doc_dir(row)
    if not d or not os.path.isdir(d):
        return []
    out = []
    for f in sorted(os.listdir(d)):
        if f.startswith("."):
            continue
        p = os.path.join(d, f)
        if os.path.isfile(p) and kind_of(f) in ("xml", "html", "pdf"):
            out.append((nfc(f), kind_of(f), p))
    return out


def parts(row):
    """[(파일명, 종류, 본문)] — 폴더 안 모든 본문 파일. 목록 json은 제외."""
    out = []
    for f, k, p in files(row):
        out.append((f, k, _read_pdf(p) if k == "pdf" else _read_text(p)))
    return out


def text(row):
    """폴더 안 모든 본문을 이어 붙인 전체 텍스트."""
    return "\n".join(t for _, _, t in parts(row) if t)


def has_source(row):
    """원문 파일이 실제로 있는가. catalog의 목록-only 행은 False."""
    d = doc_dir(row)
    return bool(d) and os.path.isdir(d) and any(
        kind_of(f) in ("xml", "html", "pdf") for f in os.listdir(d) if not f.startswith("."))


if __name__ == "__main__":
    from agent2.data import store
    print("폴더 안 모든 파일을 읽는지 확인\n")
    for corp in ("삼성전자", "KB금융"):
        for r in store.docs(corp=corp, doc_subtype="annual", base_year=2025):
            ps = parts(r)
            print(f"{corp} · {r['report_nm']} [{r['file_format']}]")
            for f, k, t in ps:
                print(f"    {k:5} {len(t):>11,}자  {f}")
            print(f"    합계 {sum(len(t) for _, _, t in ps):,}자\n")
