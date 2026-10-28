"""Policy PDF to text with pages, numbered clauses and character spans (ARCHITECTURE flow B step 1).

The PDF is hostile input: size and page limits, encrypted files refused, and any parser failure
is an error, never a partial document (R4). Text is normalized per page (runs of whitespace
become one space; pages are joined by a newline), so a span ``[start, end)`` into
``Document.text`` is stable for the same bytes. A clause is a line that starts with a number
such as ``3.2``, plus the lines after it until the next numbered line or section heading.

Spans are computed here, by code. A drafter only names a clause and quotes it (D68); code
finds the quote inside that clause, so a rule cannot cite text the policy does not contain.
"""

import hashlib
import io
import re
from dataclasses import dataclass

import pypdf

from common.errors import PolicyError

MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_PAGES = 200
_CLAUSE = re.compile(r"^(\d{1,3}\.\d{1,3})\s+(\S.*)$")
_HEADING = re.compile(r"^\d{1,3}\.\s")


@dataclass(frozen=True)
class Clause:
    id: str
    page: int
    text: str  # normalized
    start: int  # [start, end) in Document.text
    end: int


@dataclass(frozen=True)
class Document:
    name: str
    sha256: str
    pages: int
    text: str
    clauses: tuple[Clause, ...]

    def clause(self, clause_id: str) -> Clause | None:
        return next((c for c in self.clauses if c.id == clause_id), None)

    def locate(self, clause_id: str, quote: str) -> tuple[int, int] | None:
        """The span of ``quote`` inside clause ``clause_id``, or None if it is not there."""
        clause = self.clause(clause_id)
        wanted = " ".join(quote.split())
        if clause is None or not wanted:
            return None
        at = clause.text.find(wanted)
        return None if at < 0 else (clause.start + at, clause.start + at + len(wanted))


def _bad(message: str, code: str = "policy.pdf_unreadable") -> PolicyError:
    return PolicyError(message, code=code)


def _page_texts(data: bytes) -> list[str]:
    if len(data) > MAX_PDF_BYTES:
        raise _bad("policy PDF is too large", "policy.pdf_too_large")
    try:
        reader = pypdf.PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise _bad("encrypted PDFs are not accepted", "policy.pdf_encrypted")
        if len(reader.pages) > MAX_PAGES:
            raise _bad("policy PDF has too many pages", "policy.pdf_too_large")
        return [page.extract_text() or "" for page in reader.pages]
    except PolicyError:
        raise
    # pypdf raises many types on malformed input; every one of them means "no document" (R4).
    except Exception as exc:
        raise _bad(f"cannot read the PDF ({type(exc).__name__})") from exc


def _page_clauses(number: int, lines: list[str], page_text: str, offset: int) -> list[Clause]:
    found, cursor = [], 0
    clause_id: str | None = None
    parts: list[str] = []

    def close() -> None:
        nonlocal cursor
        if clause_id is None:
            return
        text = " ".join(parts)
        at = page_text.find(text, cursor)  # lines were joined by single spaces, so it is there
        cursor = at + len(text)
        found.append(Clause(clause_id, number, text, offset + at, offset + cursor))

    for line in lines:
        match = _CLAUSE.match(line)
        if match or _HEADING.match(line):
            close()
            clause_id, parts = (match.group(1), [match.group(2)]) if match else (None, [])
        elif clause_id is not None:
            parts.append(line)
    close()
    return found


def _clauses(pages: list[str]) -> tuple[str, tuple[Clause, ...]]:
    texts: list[str] = []
    found: list[Clause] = []
    offset = 0
    for number, raw in enumerate(pages, start=1):
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        lines = [line for line in lines if line]
        page_text = " ".join(lines)
        found += _page_clauses(number, lines, page_text, offset)
        texts.append(page_text)
        offset += len(page_text) + 1  # pages are joined by "\n"
    return "\n".join(texts), tuple(found)


def read_pdf(data: bytes, name: str) -> Document:
    pages = _page_texts(data)
    text, clauses = _clauses(pages)
    ids = [c.id for c in clauses]
    if not clauses:
        raise _bad("no numbered clauses found", "policy.pdf_no_clauses")
    if len(set(ids)) != len(ids):
        raise _bad("a clause number appears twice", "policy.pdf_duplicate_clause")
    return Document(name, hashlib.sha256(data).hexdigest(), len(pages), text, clauses)
