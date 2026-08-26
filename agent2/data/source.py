"""문서 본문 로딩 — 폴더 안 **모든 파일**을 읽는다. 고르지 않는다."""
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
    """pdftotext(-layout, 표 정렬 보존) 우선 → pypdf 폴백."""
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
    """manifest의 `file_path` → 실제 디스크 경로. **유니코드 정규화 형태를 맞춘다.**"""
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
    """[(파일명, 종류, 절대경로)] — 본문 파일 목록. 내용은 읽지 않는다."""
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
