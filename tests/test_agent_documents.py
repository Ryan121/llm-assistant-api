"""Documents the model cannot be shown directly.

What matters is that the digest carries what someone would need to *write a
parser*: where the columns are, what the header says, and a few rows verbatim.
A dump of the extracted text is both larger and less useful, because it loses
the column geometry - which is the part that differs between one bank's layout
and the next.

The PDF and spreadsheet tests need the optional extra and skip without it, so
CI does not have to carry pdfminer and Pillow to check the rest.
"""

from __future__ import annotations

import csv
import zlib
from pathlib import Path

import pytest

from llm_assistant_agent.documents import SUFFIXES, describe, extract_text
from llm_assistant_agent.tools import ToolBox
from llm_assistant_agent.workspace import Workspace


def _has(module: str) -> bool:
    try:
        __import__(module)
    except ImportError:
        return False
    return True


needs_pdf = pytest.mark.skipif(not _has("pdfplumber"), reason="needs the documents extra")
needs_xlsx = pytest.mark.skipif(not _has("openpyxl"), reason="needs the documents extra")


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace.open(tmp_path)


@pytest.fixture
def toolbox(workspace: Workspace) -> ToolBox:
    return ToolBox(workspace, approver=lambda description, detail: True)


_ROWS = [
    ("01/09/2026", "TESCO STORES 3345", "42.10", "", "1203.55"),
    ("02/09/2026", "TFL TRAVEL CHARGE", "8.40", "", "1195.15"),
    ("03/09/2026", "SALARY ACME LTD", "", "2400.00", "3595.15"),
    ("04/09/2026", "BRITISH GAS DD", "88.20", "", "3506.95"),
]


def _statement_pdf(path: Path) -> None:
    """A statement laid out with whitespace and no ruled table - the common
    hard case, and the one where column positions are the only signal."""
    xs = [56, 150, 330, 400, 470]
    ops = ["BT /F1 9 Tf"]
    for index, heading in enumerate(("Date", "Description", "Debit", "Credit", "Balance")):
        ops.append(f"1 0 0 1 {xs[index]} 730 Tm ({heading}) Tj")
    y = 712
    for row in _ROWS:
        for index, cell in enumerate(row):
            if cell:
                ops.append(f"1 0 0 1 {xs[index]} {y} Tm ({cell}) Tj")
        y -= 16
    ops.append("ET")

    stream = zlib.compress("\n".join(ops).encode())
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Filter /FlateDecode /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(start).encode() + b"\n%%EOF\n"
    path.write_bytes(out)


# --- PDF -------------------------------------------------------------------


@needs_pdf
def test_a_pdf_digest_carries_what_a_parser_needs(workspace: Workspace) -> None:
    _statement_pdf(workspace.root / "statement.pdf")

    digest = describe(workspace.root / "statement.pdf")

    assert "PDF, 1 page" in digest
    assert "text columns start at x:" in digest
    # Rows verbatim, so the model can match against real strings.
    assert "TESCO STORES 3345" in digest


@needs_pdf
def test_a_sparse_column_is_still_reported(workspace: Workspace) -> None:
    """The Credit column has one entry in the sample. A parser still has to
    handle it, so it is listed with its occupancy rather than dropped."""
    _statement_pdf(workspace.root / "statement.pdf")

    digest = describe(workspace.root / "statement.pdf")

    positions = digest.split("text columns start at x:")[1].splitlines()[0]
    assert positions.count("(") >= 5  # all five columns, each with a row count


@needs_pdf
def test_busy_columns_win_over_left_margin_noise(workspace: Workspace) -> None:
    """On a dense page nearly every x has two words, so taking the leftmost
    positions drops the Balance column off the right in favour of a dozen
    near-identical positions in the left margin."""
    ops = ["BT /F1 9 Tf"]
    # More lightly-used positions to the left than the cap allows through...
    for column in range(14):
        for row in range(2):
            ops.append(f"1 0 0 1 {40 + column * 30} {760 - row * 12} Tm (noise) Tj")
    # ...and the column that actually matters, furthest right.
    for row in range(40):
        ops.append(f"1 0 0 1 520 {700 - row * 12} Tm (1203.55) Tj")
    ops.append("ET")

    stream = zlib.compress("\n".join(ops).encode())
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Filter /FlateDecode /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(start).encode() + b"\n%%EOF\n"
    (workspace.root / "dense.pdf").write_bytes(out)

    digest = describe(workspace.root / "dense.pdf")
    positions = digest.split("text columns start at x:")[1].splitlines()[0]

    # The busiest column survives the cap; tolerance rounding moves it a little.
    assert "(40 rows)" in positions, positions
    assert any(str(x) in positions for x in (519, 520, 521)), positions


@needs_pdf
def test_the_digest_is_far_smaller_than_the_text(workspace: Workspace) -> None:
    """The whole argument for a digest: it costs a fraction of the tokens."""
    _statement_pdf(workspace.root / "statement.pdf")

    digest = describe(workspace.root / "statement.pdf")
    full = extract_text(workspace.root / "statement.pdf")

    assert len(digest) < len(full) * 4  # a one-page sample; the gap grows with pages
    assert full.strip()


# --- delimited --------------------------------------------------------------


def test_a_csv_digest_names_columns_and_guesses_types(workspace: Workspace) -> None:
    target = workspace.root / "expenses.csv"
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "description", "amount", "currency"])
        for row in _ROWS:
            writer.writerow([row[0], row[1], row[2] or row[3], "GBP"])

    digest = describe(target)

    assert "4 data rows, 4 columns" in digest
    assert "date-like" in digest
    assert "numeric" in digest
    assert "TESCO STORES 3345" in digest


def test_a_big_csv_is_summarised_not_dumped(workspace: Workspace) -> None:
    target = workspace.root / "big.csv"
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["a", "b"])
        for index in range(5000):
            writer.writerow([index, f"row {index}"])

    digest = describe(target)

    assert "5000 data rows" in digest
    assert len(digest) < 1000
    assert "row 4999" not in digest


def test_a_tsv_is_recognised(workspace: Workspace) -> None:
    target = workspace.root / "x.tsv"
    target.write_text("a\tb\n1\t2\n", encoding="utf-8")

    assert "TSV" in describe(target)


# --- spreadsheets -----------------------------------------------------------


@needs_xlsx
def test_a_workbook_digest_covers_every_sheet(workspace: Workspace) -> None:
    from openpyxl import Workbook

    book = Workbook()
    first = book.active
    first.title = "September"
    first.append(["Date", "Description", "Amount"])
    first.append(["01/09/2026", "TESCO STORES 3345", 42.10])
    book.create_sheet("October").append(["Date", "Description", "Amount"])
    book.save(workspace.root / "book.xlsx")

    digest = describe(workspace.root / "book.xlsx")

    assert "2 sheet(s)" in digest
    assert "'September'" in digest
    assert "'October'" in digest
    assert "TESCO STORES 3345" in digest


# --- the tool ---------------------------------------------------------------


def test_read_file_points_at_read_document(toolbox: ToolBox) -> None:
    """The model's instinct on any path is read_file, so the redirect has to
    live there rather than rely on it noticing the other tool."""
    (toolbox.workspace.root / "statement.pdf").write_bytes(b"%PDF-1.4\n")

    outcome = toolbox.invoke("read_file", {"path": "statement.pdf"})

    assert outcome.is_error
    assert "read_document" in outcome.content


def test_a_document_is_not_marked_as_read_for_editing(toolbox: ToolBox) -> None:
    """A digest is not the file's contents; an edit built from one would be
    built from a summary."""
    target = toolbox.workspace.root / "expenses.csv"
    target.write_text("a,b\n1,2\n", encoding="utf-8")

    toolbox.invoke("read_document", {"path": "expenses.csv"})

    assert target.resolve() not in toolbox.workspace.seen


def test_a_missing_document_is_an_error_not_a_crash(toolbox: ToolBox) -> None:
    outcome = toolbox.invoke("read_document", {"path": "nope.pdf"})

    assert outcome.is_error
    assert "does not exist" in outcome.content


def test_documents_cannot_escape_the_workspace(toolbox: ToolBox) -> None:
    outcome = toolbox.invoke("read_document", {"path": "../../etc/passwd"})

    assert outcome.is_error
    assert "outside the workspace" in outcome.content


def test_an_unsupported_suffix_says_to_use_read_file(workspace: Workspace) -> None:
    target = workspace.root / "notes.md"
    target.write_text("# hi\n", encoding="utf-8")

    assert "read_file" in describe(target)


def test_every_declared_suffix_is_routed(workspace: Workspace) -> None:
    """A suffix read_file refuses must be one describe() actually handles, or
    the redirect sends the model somewhere that cannot help it."""
    for suffix in SUFFIXES:
        target = workspace.root / f"sample{suffix}"
        target.write_bytes(b"")
        assert "Try read_file" not in describe(target), suffix


def test_a_corrupt_document_is_reported_not_raised(workspace: Workspace) -> None:
    """Third-party parsers raise their own exception types, which ToolBox.invoke
    does not catch - uncaught, a truncated download takes the turn down with a
    traceback instead of telling the model the file is broken."""
    target = workspace.root / "truncated.pdf"
    target.write_bytes(b"%PDF-1.4\nnot really a pdf")

    message = describe(target)

    assert "could not be read" in message


def test_a_corrupt_document_does_not_escape_the_toolbox(toolbox: ToolBox) -> None:
    (toolbox.workspace.root / "truncated.pdf").write_bytes(b"%PDF-1.4\nnope")

    outcome = toolbox.invoke("read_document", {"path": "truncated.pdf"})

    assert "could not be read" in outcome.content


def test_a_missing_extra_explains_the_install(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reported as a tool result, so it has to tell the model what to ask for
    rather than raise an ImportError mid-turn."""
    import builtins

    real = builtins.__import__

    def refuse(name: str, *args: object, **kwargs: object) -> object:
        if name == "pdfplumber":
            raise ImportError("no pdfplumber")
        return real(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", refuse)
    (workspace.root / "x.pdf").write_bytes(b"%PDF-1.4\n")

    message = describe(workspace.root / "x.pdf")

    assert "documents" in message
    assert "pip install" in message
