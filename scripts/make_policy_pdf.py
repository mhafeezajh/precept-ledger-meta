"""Write deploy/demo/payments_policy.pdf from deploy/demo/payments_policy.txt (demo beat 1).

A minimal PDF written by hand (PDF 1.4, Helvetica, one text line per source line), so no PDF
writing dependency is needed. Deterministic: no dates or ids, so the same text gives the same
bytes and the same SHA-256. A new page starts at a section heading once a page has 25 lines,
so no clause is split across pages. Run: ``uv run python scripts/make_policy_pdf.py``.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "deploy" / "demo" / "payments_policy.txt"
TARGET = ROOT / "deploy" / "demo" / "payments_policy.pdf"
LINES_BEFORE_BREAK = 25
_HEADING = re.compile(r"^\d+\. ")


def paginate(lines: list[str]) -> list[list[str]]:
    pages: list[list[str]] = [[]]
    for line in lines:
        if _HEADING.match(line) and len(pages[-1]) >= LINES_BEFORE_BREAK:
            pages.append([])
        pages[-1].append(line)
    return pages


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _content(lines: list[str]) -> bytes:
    ops = ["BT", "/F1 10 Tf", "14 TL", "56 770 Td"]
    ops += [f"({_escape(line)}) Tj T*" for line in lines]
    ops.append("ET")
    return "\n".join(ops).encode("ascii")


def build_pdf(text: str) -> bytes:
    pages = paginate(text.rstrip("\n").split("\n"))
    count = len(pages)
    # Objects: 1 catalog, 2 pages, 3 font, then a page and its content stream per page.
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(count))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    for i, lines in enumerate(pages):
        stream = _content(lines)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font"
            f" << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def main() -> int:
    text = SOURCE.read_text("utf-8")
    if not text.isascii():
        print("payments_policy.txt must be ASCII (Helvetica, WinAnsi)")
        return 1
    TARGET.write_bytes(build_pdf(text))
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
