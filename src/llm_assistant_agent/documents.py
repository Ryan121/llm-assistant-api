"""Reading documents that are not source code.

The model cannot be shown a PDF. What it can be shown - and what it actually
needs in order to *write a parser* for one - is the document's shape: how many
pages, where the columns are, what the header row says, and a few rows exactly
as they appear. That is perhaps thirty lines. The extracted text of a
twenty-page statement is several thousand tokens of near-repetition and still
loses the column geometry, which is the part that differs between one bank's
layout and the next.

So the default here is a structural digest, not a dump. ``full=True`` gets the
text when the content is what matters rather than the shape.

The libraries are an optional extra (``pip install '.[documents]'``) because
this package is also the gateway, whose image is deliberately small. Absent
them, every function here returns an instruction for installing them rather
than raising - the message goes to the model as a tool result, and "install the
extra" is something the user can act on.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["MISSING_EXTRA", "SUFFIXES", "describe", "extract_text"]

#: What a document tool can open. Anything else is source code - read_file.
SUFFIXES = (".pdf", ".csv", ".tsv", ".xlsx", ".xlsm")

MISSING_EXTRA = (
    "Reading {kind} files needs an optional dependency that is not installed. "
    "Ask the user to run: pip install 'llm-assistant-api[documents]'"
)

#: Enough pages to see whether the layout repeats, few enough to stay cheap.
_MAX_PAGES = 3
#: Sample rows per table. Three shows the pattern; thirty shows it thirty times.
_MAX_ROWS = 3
_MAX_COLUMNS = 12
_MAX_CELL = 40
_MAX_TEXT = 15_000

#: Word x-positions within this many points count as the same column. Bank
#: statements rarely align to the point, and a column found at 55.9 and 56.1 is
#: one column.
_COLUMN_TOLERANCE = 3.0
#: A column is a position shared by more than one row, not a stray word. Kept
#: low on purpose: a "Credit" column with one entry in the sample is still a
#: column the parser has to handle, and missing it is worse than listing a
#: sparse one - so the occupancy is reported alongside and the caller judges.
_MIN_COLUMN_ROWS = 2


@dataclass
class _MissingExtraError(Exception):
    kind: str

    def message(self) -> str:
        return MISSING_EXTRA.format(kind=self.kind)


def describe(path: Path) -> str:
    """A structural digest of ``path``: what a parser author would need."""
    return _guard(path, _describe)


def extract_text(path: Path) -> str:
    """The document's text, for when the content matters more than the shape."""
    return _guard(path, _extract)


def _guard(path: Path, work: Callable[[Path], str]) -> str:
    """Turn any parser failure into a tool result the model can act on.

    Deliberately broad. These are third-party parsers over files the agent did
    not create - a truncated download, a .pdf that is really HTML, an encrypted
    statement - and each raises its own exception type. Uncaught, they escape
    ``ToolBox.invoke``, which only expects OSError and ValueError, and take the
    whole turn down with a traceback rather than telling the model what is
    wrong with the file.
    """
    try:
        return work(path)
    except _MissingExtraError as missing:
        return missing.message()
    except Exception as exc:  # noqa: BLE001 - see above
        return f"{path.name} could not be read: {type(exc).__name__}: {exc}"


def _describe(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _describe_pdf(path)
    if suffix in {".csv", ".tsv"}:
        return _describe_delimited(path)
    if suffix in {".xlsx", ".xlsm"}:
        return _describe_workbook(path)
    return f"{path.name} is not a document this tool reads. Try read_file."


def _extract(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        pdfplumber = _import("pdfplumber", "PDF")
        with pdfplumber.open(str(path)) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
        return _cap("\n\n".join(pages))
    if suffix in {".csv", ".tsv"}:
        return _cap(path.read_text(encoding="utf-8", errors="replace"))
    if suffix in {".xlsx", ".xlsm"}:
        return _cap(_workbook_text(path))
    return f"{path.name} is not a document this tool reads. Try read_file."


# --- PDF -------------------------------------------------------------------


def _describe_pdf(path: Path) -> str:
    pdfplumber = _import("pdfplumber", "PDF")
    lines: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        total = len(pdf.pages)
        lines.append(f"{path.name} - PDF, {total} page{'s' if total != 1 else ''}")
        for number, page in enumerate(pdf.pages[:_MAX_PAGES], start=1):
            lines.extend(_describe_page(number, page))
        if total > _MAX_PAGES:
            lines.append(f"\n... {total - _MAX_PAGES} further pages not shown.")
    return "\n".join(lines)


def _describe_page(number: int, page: Any) -> list[str]:
    words = page.extract_words() or []
    lines = [f"\npage {number}: {len(words)} words"]

    tables = page.extract_tables() or []
    for index, table in enumerate(tables, start=1):
        lines.extend(_describe_table(index, table))

    # Ruled tables are the easy case. A statement laid out with whitespace has
    # no table at all, and then the column x-positions are the only thing that
    # tells a parser where the fields start.
    columns = _column_positions(words)
    if columns:
        rendered = ", ".join(f"{x:.0f} ({n} rows)" for x, n in columns)
        lines.append(f"  text columns start at x: {rendered}")

    if not tables:
        text = (page.extract_text() or "").splitlines()
        for line in text[: _MAX_ROWS + 2]:
            if line.strip():
                lines.append(f"    {line[:120]}")
    return lines


def _describe_table(index: int, table: list[list[str | None]]) -> list[str]:
    if not table:
        return []
    width = max(len(row) for row in table)
    lines = [f"  table {index}: {width} columns x {len(table)} rows"]
    for label, row in zip(
        ("header", *[f"row {i}" for i in range(1, _MAX_ROWS + 1)]),
        table[: _MAX_ROWS + 1],
        strict=False,
    ):
        lines.append(f"    {label}: {_row(row)}")
    return lines


def _column_positions(words: list[dict[str, object]]) -> list[tuple[float, int]]:
    """x-positions shared by several rows - the columns of a whitespace layout,
    each with how many rows actually reach it."""
    counts: Counter[float] = Counter()
    for word in words:
        try:
            x = float(word["x0"])  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError):
            continue
        counts[round(x / _COLUMN_TOLERANCE) * _COLUMN_TOLERANCE] += 1

    # Selected by how many rows reach them, then sorted by position for
    # reading. Taking the leftmost N instead - the obvious thing - drops the
    # Balance column off the right of a dense statement in favour of a dozen
    # near-identical positions in the left margin, which is what a real
    # seven-page statement does to a page-wide tolerance.
    candidates = [(x, n) for x, n in counts.items() if n >= _MIN_COLUMN_ROWS]
    best = sorted(candidates, key=lambda pair: pair[1], reverse=True)[:_MAX_COLUMNS]
    return sorted(best)


# --- delimited text --------------------------------------------------------


def _describe_delimited(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")
    sample = raw[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample)
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = "\t" if path.suffix.lower() == ".tsv" else ","

    rows = list(csv.reader(io.StringIO(raw), delimiter=delimiter))
    if not rows:
        return f"{path.name} - empty"

    header, *body = rows
    kind = path.suffix.lstrip(".").upper()
    lines = [
        f"{path.name} - {kind}, {len(body)} data rows, {len(header)} columns",
        f"  delimiter: {delimiter!r}",
        "  columns:",
    ]
    for index, name in enumerate(header[:_MAX_COLUMNS]):
        values = [row[index] for row in body[:50] if index < len(row) and row[index].strip()]
        examples = ", ".join(v[:_MAX_CELL] for v in values[:2])
        lines.append(f"    {name[:30]:<30} ({_kind_of(values)})  e.g. {examples}")
    if len(header) > _MAX_COLUMNS:
        lines.append(f"    ... and {len(header) - _MAX_COLUMNS} more columns")

    for number, row in enumerate(body[:_MAX_ROWS], start=1):
        lines.append(f"  row {number}: {_row(row)}")
    return "\n".join(lines)


def _kind_of(values: list[str]) -> str:
    """A guess at the column's type, to save the model a round trip."""
    if not values:
        return "empty"
    if all(_numeric(v) for v in values):
        return "numeric"
    if all(_date_like(v) for v in values):
        return "date-like"
    return "text"


def _numeric(value: str) -> bool:
    try:
        float(value.replace(",", "").replace("£", "").replace("$", "").strip())
    except ValueError:
        return False
    return True


def _date_like(value: str) -> bool:
    digits = sum(c.isdigit() for c in value)
    separators = sum(value.count(c) for c in "/-.")
    return digits >= 4 and separators >= 2


# --- workbooks -------------------------------------------------------------


def _describe_workbook(path: Path) -> str:
    openpyxl = _import("openpyxl", "XLSX")
    book = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        lines = [f"{path.name} - XLSX, {len(book.sheetnames)} sheet(s)"]
        for name in book.sheetnames:
            sheet = book[name]
            rows = list(sheet.iter_rows(max_row=_MAX_ROWS + 1, values_only=True))
            lines.append(f"\n  sheet {name!r} - {sheet.max_row} rows x {sheet.max_column} columns")
            for label, row in zip(
                ("header", *[f"row {i}" for i in range(1, _MAX_ROWS + 1)]), rows, strict=False
            ):
                lines.append(f"    {label}: {_row([_cell(v) for v in row])}")
        return "\n".join(lines)
    finally:
        book.close()


def _workbook_text(path: Path) -> str:
    openpyxl = _import("openpyxl", "XLSX")
    book = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        out: list[str] = []
        for name in book.sheetnames:
            out.append(f"# {name}")
            for row in book[name].iter_rows(values_only=True):
                out.append("\t".join(_cell(v) for v in row))
        return "\n".join(out)
    finally:
        book.close()


# --- shared ----------------------------------------------------------------


def _import(module: str, kind: str) -> Any:
    try:
        return __import__(module)
    except ImportError as exc:
        raise _MissingExtraError(kind) from exc


def _cell(value: object) -> str:
    return "" if value is None else str(value)


def _row(row: list[str | None] | list[str]) -> str:
    cells = [(_cell(c)).replace("\n", " ")[:_MAX_CELL] for c in row[:_MAX_COLUMNS]]
    rendered = " | ".join(cells)
    return rendered + (" | ..." if len(row) > _MAX_COLUMNS else "")


def _cap(text: str) -> str:
    if len(text) <= _MAX_TEXT:
        return text
    return text[:_MAX_TEXT] + f"\n... {len(text) - _MAX_TEXT} more characters not shown."
