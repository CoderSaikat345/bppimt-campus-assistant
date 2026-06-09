"""
ingestion/parser.py

Specialized tabular document parser for the BPPIMT Campus Resource Assistant.

Design rationale
────────────────
University documents (timetables, fee structures) are almost entirely
grid-based.  Naive PDF text extraction destroys column associations —
"Monday" from column 1 gets concatenated with a random subject from
column 4.  Standard sentence-based chunking then splits mid-row, making
it impossible for a retrieval model to reconstruct "CSE-B has Data
Structures on Monday 9:00–10:00 in Room 401."

This parser solves that by:
  1. Detecting whether a document is a PDF or Excel file.
  2. Extracting every table as a structured grid (list-of-rows).
  3. Serialising each data row into an explicit semantic sentence that
     names every column header alongside its value.
  4. Wrapping each sentence in a ParsedChunk dataclass that carries the
     provenance metadata needed for vector store upsert.

The output is a flat list[ParsedChunk] ready for embedding.

Supported input formats
───────────────────────
  • PDF  — via pdfplumber (handles merged/irregular cell borders)
  • XLSX — via openpyxl (handles merged cells, frozen header rows)

Exception strategy
──────────────────
  MissingHeaderError   — table has no detectable header row
  MalformedGridError   — row/column count mismatch or all-empty grid
  UnsupportedFileError — file extension is not .pdf or .xlsx/.xls

These are raised from within the parser and caught by the caller
(embed_and_load.py) so a single bad file never aborts the full pipeline.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Final, Iterator

import openpyxl
import pdfplumber
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum number of non-empty cells in a row to be considered a real data row.
_MIN_DATA_CELLS: Final[int] = 2

# Maximum fraction of empty cells in a "header" row before we give up.
_MAX_HEADER_EMPTY_RATIO: Final[float] = 0.6

# Column separator used inside serialised sentence segments.
_SEP: Final[str] = " | "

# ---------------------------------------------------------------------------
# Custom exception hierarchy
# ---------------------------------------------------------------------------


class IngestionError(Exception):
    """Base class for all ingestion pipeline errors."""


class MissingHeaderError(IngestionError):
    """
    Raised when a table grid cannot produce a usable header row.

    This occurs when:
      - The first non-empty row has more than MAX_HEADER_EMPTY_RATIO empty cells.
      - The entire table has fewer than 2 non-empty rows.
    """


class MalformedGridError(IngestionError):
    """
    Raised when the extracted grid is structurally invalid.

    This occurs when:
      - All extracted rows are empty.
      - Column count changes by more than 50 % between consecutive rows
        (indicates merged-cell extraction failure).
    """


class UnsupportedFileError(IngestionError):
    """Raised when the input file has an extension we cannot handle."""


# ---------------------------------------------------------------------------
# Document type enum
# ---------------------------------------------------------------------------


class DocumentType(Enum):
    PDF = auto()
    EXCEL = auto()


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedChunk:
    """
    A single semantic text unit ready for embedding.

    Attributes
    ──────────
    text          : The serialised sentence representing one table row.
    doc_name      : Original file basename (e.g. "CSE_Timetable_2024.pdf").
    doc_type      : DocumentType.PDF or DocumentType.EXCEL.
    source_path   : Absolute path to the source file.
    page_number   : PDF page index (1-based) or Excel sheet name.
    table_index   : Index of the table on the page / sheet (0-based).
    row_index     : Row index within the table (0-based, excludes header).
    headers       : Tuple of column header strings for this table.
    raw_row_values: Tuple of raw cell values (pre-serialisation) for audit.
    """

    text: str
    doc_name: str
    doc_type: DocumentType
    source_path: str
    page_number: int | str
    table_index: int
    row_index: int
    headers: tuple[str, ...]
    raw_row_values: tuple[str, ...]


# ---------------------------------------------------------------------------
# Text normalisation helpers
# ---------------------------------------------------------------------------


def _normalise_cell(value: Any) -> str:
    """
    Convert any cell value to a clean, single-line string.

    Steps:
      1. Cast to str, stripping None / float NaN artefacts.
      2. Unicode NFKC normalisation (collapses ligatures, fixes fullwidth chars).
      3. Collapse internal whitespace to single spaces.
      4. Strip leading/trailing whitespace.
    """
    if value is None:
        return ""
    raw = str(value).strip()
    if raw.lower() in {"none", "nan", "#n/a", "n/a", "-", "–", "—"}:
        return ""
    # NFKC: canonical decomposition → compatibility composition
    raw = unicodedata.normalize("NFKC", raw)
    # Collapse whitespace (including non-breaking spaces \xa0)
    raw = re.sub(r"[\s\xa0]+", " ", raw).strip()
    return raw


def _is_empty_row(row: list[str]) -> bool:
    """Return True if every cell in `row` is an empty string."""
    return all(cell == "" for cell in row)


def _non_empty_count(row: list[str]) -> int:
    return sum(1 for cell in row if cell)


# ---------------------------------------------------------------------------
# Row serialiser
# ---------------------------------------------------------------------------


def _serialise_row(
    headers: tuple[str, ...],
    row_values: tuple[str, ...],
    doc_name: str,
    page_label: str,
) -> str:
    """
    Convert one data row into a human-readable, retrieval-friendly sentence.

    Format:
        [<doc_name> | Page/Sheet: <label>]
        <header_1>: <value_1> | <header_2>: <value_2> | ...

    Empty cells are included as "<header>: —" to preserve column alignment
    context for the embedding model.

    Example output:
        [CSE_Timetable_2024.pdf | Page/Sheet: 3]
        Day: Monday | Time: 09:00–10:00 | Subject: Data Structures |
        Faculty: Dr. A. Roy | Room: 401 | Section: CSE-B
    """
    parts: list[str] = []
    for header, value in zip(headers, row_values):
        display_value = value if value else "—"
        parts.append(f"{header}: {display_value}")

    body = _SEP.join(parts)
    prefix = f"[{doc_name} | Page/Sheet: {page_label}]"
    return f"{prefix}\n{body}"


# ---------------------------------------------------------------------------
# Grid validator
# ---------------------------------------------------------------------------


def _validate_grid(grid: list[list[str]], source_label: str) -> None:
    """
    Assert that `grid` satisfies minimum structural requirements.

    Raises:
        MalformedGridError: if the grid is empty or all rows are empty.
    """
    if not grid:
        raise MalformedGridError(
            f"[{source_label}] Extracted grid is completely empty."
        )

    non_empty_rows = [row for row in grid if not _is_empty_row(row)]
    if not non_empty_rows:
        raise MalformedGridError(
            f"[{source_label}] Every row in the extracted grid is empty."
        )

    # Detect wild column-count oscillation (sign of merged-cell extraction failure)
    col_counts = [len(row) for row in non_empty_rows]
    if max(col_counts) > 0 and (min(col_counts) / max(col_counts)) < 0.5:
        raise MalformedGridError(
            f"[{source_label}] Column count varies between {min(col_counts)} and "
            f"{max(col_counts)} — likely a merged-cell extraction failure. "
            f"Review the source document layout."
        )


# ---------------------------------------------------------------------------
# Header detector
# ---------------------------------------------------------------------------


def _detect_header(
    grid: list[list[str]],
    source_label: str,
) -> tuple[tuple[str, ...], int]:
    """
    Identify the first row that qualifies as a header row.

    Strategy:
      - Walk rows from the top.
      - Skip rows that are entirely empty.
      - Accept the first row where > (1 - MAX_HEADER_EMPTY_RATIO) cells are filled.
      - If no such row exists within the first 5 rows, raise MissingHeaderError.

    Returns:
        (header_tuple, header_row_index) — index is used to skip the header
        when iterating data rows.

    Raises:
        MissingHeaderError: if a usable header row cannot be identified.
    """
    search_limit = min(5, len(grid))
    for idx in range(search_limit):
        row = grid[idx]
        if _is_empty_row(row):
            continue
        filled = _non_empty_count(row)
        if filled / len(row) >= (1 - _MAX_HEADER_EMPTY_RATIO):
            # Pad any empty header cells with "Column_N" to keep alignment
            headers = tuple(
                cell if cell else f"Column_{i + 1}"
                for i, cell in enumerate(row)
            )
            logger.debug(
                "[%s] Header detected at row %d: %s", source_label, idx, headers
            )
            return headers, idx

    raise MissingHeaderError(
        f"[{source_label}] Could not identify a header row in the first "
        f"{search_limit} rows. Check that the table has a non-empty first row."
    )


# ---------------------------------------------------------------------------
# Grid → ParsedChunk iterator
# ---------------------------------------------------------------------------


def _grid_to_chunks(
    grid: list[list[str]],
    doc_name: str,
    doc_type: DocumentType,
    source_path: str,
    page_number: int | str,
    table_index: int,
    source_label: str,
) -> list[ParsedChunk]:
    """
    Convert a validated 2-D string grid into a list of ParsedChunk objects.

    Each non-empty data row becomes exactly one chunk.

    Raises:
        MissingHeaderError: propagated from _detect_header.
        MalformedGridError: propagated from _validate_grid.
    """
    _validate_grid(grid, source_label)
    headers, header_row_idx = _detect_header(grid, source_label)
    n_cols = len(headers)

    chunks: list[ParsedChunk] = []
    data_row_counter = 0

    for row_idx, raw_row in enumerate(grid):
        if row_idx <= header_row_idx:
            continue  # Skip header and any leading empty rows above it
        if _is_empty_row(raw_row):
            continue  # Skip spacer rows between sections

        # Normalise row length to match header width
        padded_row = raw_row[:n_cols] + [""] * max(0, n_cols - len(raw_row))
        row_values = tuple(padded_row[:n_cols])

        if _non_empty_count(list(row_values)) < _MIN_DATA_CELLS:
            logger.debug(
                "[%s] Skipping near-empty data row %d: %s",
                source_label,
                row_idx,
                row_values,
            )
            continue

        page_label = (
            str(page_number) if isinstance(page_number, int) else page_number
        )

        text = _serialise_row(
            headers=headers,
            row_values=row_values,
            doc_name=doc_name,
            page_label=page_label,
        )

        chunks.append(
            ParsedChunk(
                text=text,
                doc_name=doc_name,
                doc_type=doc_type,
                source_path=source_path,
                page_number=page_number,
                table_index=table_index,
                row_index=data_row_counter,
                headers=headers,
                raw_row_values=row_values,
            )
        )
        data_row_counter += 1

    logger.info(
        "[%s] Table %d on page/sheet '%s' → %d chunks",
        doc_name,
        table_index,
        page_number,
        len(chunks),
    )
    return chunks


# ---------------------------------------------------------------------------
# PDF parser  (pdfplumber)
# ---------------------------------------------------------------------------


class PDFTableParser:
    """
    Extracts tabular content from PDF files using pdfplumber.

    pdfplumber is chosen over PyPDF2/pypdfium because it reconstructs
    table geometry from the PDF drawing operators (lines, rectangles),
    making it robust against tables that lack explicit border boxes.

    Merge strategy for borderless tables:
      snap_tolerance  — cells within 3pt of each other are merged.
      join_tolerance  — text fragments within 3pt are joined.
    """

    _EXTRACT_SETTINGS: dict[str, Any] = {
        "vertical_strategy": "lines",
        "horizontal_strategy": "lines",
        "snap_tolerance": 3,
        "join_tolerance": 3,
        "edge_min_length": 3,
        "min_words_vertical": 1,
        "min_words_horizontal": 1,
    }

    def __init__(self, file_path: Path) -> None:
        if not file_path.exists():
            raise FileNotFoundError(f"PDF file not found: {file_path}")
        if file_path.suffix.lower() != ".pdf":
            raise UnsupportedFileError(
                f"PDFTableParser requires a .pdf file; got: {file_path.suffix}"
            )
        self._path = file_path
        self._doc_name = file_path.name

    def parse(self) -> list[ParsedChunk]:
        """
        Open the PDF and extract every table from every page.

        Returns:
            Flat list of ParsedChunk objects from all pages and tables.

        Raises:
            MalformedGridError / MissingHeaderError: per-table, logged and
            skipped — does not abort the entire document parse.
        """
        all_chunks: list[ParsedChunk] = []

        logger.info("Parsing PDF: %s", self._path)

        try:
            with pdfplumber.open(self._path) as pdf:
                total_pages = len(pdf.pages)
                logger.info("  Total pages: %d", total_pages)

                for page_idx, page in enumerate(pdf.pages, start=1):
                    page_tables = page.extract_tables(
                        table_settings=self._EXTRACT_SETTINGS
                    )

                    if not page_tables:
                        logger.debug(
                            "  Page %d: no tables detected.", page_idx
                        )
                        continue

                    logger.debug(
                        "  Page %d: %d table(s) found.",
                        page_idx,
                        len(page_tables),
                    )

                    for tbl_idx, raw_table in enumerate(page_tables):
                        source_label = (
                            f"{self._doc_name}:p{page_idx}:t{tbl_idx}"
                        )
                        # Normalise every cell
                        grid: list[list[str]] = [
                            [_normalise_cell(cell) for cell in row]
                            for row in raw_table
                        ]

                        try:
                            chunks = _grid_to_chunks(
                                grid=grid,
                                doc_name=self._doc_name,
                                doc_type=DocumentType.PDF,
                                source_path=str(self._path.resolve()),
                                page_number=page_idx,
                                table_index=tbl_idx,
                                source_label=source_label,
                            )
                            all_chunks.extend(chunks)
                        except (MissingHeaderError, MalformedGridError) as exc:
                            logger.warning(
                                "  Skipping table %d on page %d — %s: %s",
                                tbl_idx,
                                page_idx,
                                type(exc).__name__,
                                exc,
                            )
                            continue

        except Exception as exc:
            raise IngestionError(
                f"Failed to open or parse PDF '{self._path}': {exc}"
            ) from exc

        logger.info(
            "PDF parse complete: %s → %d total chunks", self._doc_name, len(all_chunks)
        )
        return all_chunks


# ---------------------------------------------------------------------------
# Excel parser  (openpyxl)
# ---------------------------------------------------------------------------


class ExcelTableParser:
    """
    Extracts tabular content from Excel files (.xlsx / .xls) using openpyxl.

    Merged-cell handling:
      openpyxl tracks merge ranges in worksheet.merged_cells.  When a merged
      cell is encountered, its value is held only in the top-left cell.  All
      other cells in the range return None.  This parser unmerges the value
      by propagating the top-left content into every covered cell before
      extracting the grid, preserving column alignment.

    Multi-sheet support:
      All non-hidden sheets are parsed.  The sheet name is used as the
      page_number field in ParsedChunk.
    """

    _SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".xlsx", ".xls", ".xlsm"})

    def __init__(self, file_path: Path) -> None:
        if not file_path.exists():
            raise FileNotFoundError(f"Excel file not found: {file_path}")
        if file_path.suffix.lower() not in self._SUPPORTED_EXTENSIONS:
            raise UnsupportedFileError(
                f"ExcelTableParser requires an Excel file; got: {file_path.suffix}"
            )
        self._path = file_path
        self._doc_name = file_path.name

    # ── Merge-cell propagation ─────────────────────────────────────────────

    @staticmethod
    def _expand_merged_cells(ws: Worksheet) -> None:
        """
        Propagate the value of each merge anchor cell to all cells it spans.

        After this call, reading ws.cell(row, col).value for any cell that
        was part of a merge range returns the anchor value instead of None.

        This is done in-place on the worksheet object (non-destructive to the
        file on disk because we open with read_only=False but never save).
        """
        for merge_range in list(ws.merged_cells.ranges):
            # The anchor is always the top-left cell of the merge range
            anchor_value = ws.cell(
                row=merge_range.min_row,
                column=merge_range.min_col,
            ).value

            # Unmerge first so we can write to the covered cells
            ws.unmerge_cells(str(merge_range))

            for row in range(merge_range.min_row, merge_range.max_row + 1):
                for col in range(merge_range.min_col, merge_range.max_col + 1):
                    ws.cell(row=row, column=col).value = anchor_value

    # ── Auto table boundary detection ─────────────────────────────────────

    @staticmethod
    def _find_table_bounds(
        ws: Worksheet,
    ) -> tuple[int, int, int, int]:
        """
        Detect the bounding box of the populated data region.

        Returns (min_row, max_row, min_col, max_col) — all 1-indexed.

        Strategy:
          - Walk rows/columns to find the first and last non-empty cell.
          - If the worksheet is completely empty, raise MalformedGridError.
        """
        min_row = min_col = float("inf")  # type: ignore[assignment]
        max_row = max_col = 0

        for row in ws.iter_rows():
            for cell in row:
                if cell.value is not None and str(cell.value).strip():
                    r, c = cell.row, cell.column
                    min_row = min(min_row, r)
                    max_row = max(max_row, r)
                    min_col = min(min_col, c)
                    max_col = max(max_col, c)

        if max_row == 0:
            raise MalformedGridError(
                f"Worksheet '{ws.title}' appears to be completely empty."
            )

        return (
            int(min_row),
            int(max_row),
            int(min_col),
            int(max_col),
        )

    # ── Sheet → grid extractor ─────────────────────────────────────────────

    def _extract_sheet_grid(
        self,
        ws: Worksheet,
    ) -> list[list[str]]:
        """
        Convert an openpyxl Worksheet into a 2-D list[list[str]] grid.

        Steps:
          1. Expand merged cells so every position has its correct value.
          2. Auto-detect the populated bounding box.
          3. Read every cell in the bounding box and normalise it.
        """
        self._expand_merged_cells(ws)
        min_r, max_r, min_c, max_c = self._find_table_bounds(ws)

        grid: list[list[str]] = []
        for r in range(min_r, max_r + 1):
            row: list[str] = []
            for c in range(min_c, max_c + 1):
                raw = ws.cell(row=r, column=c).value
                row.append(_normalise_cell(raw))
            grid.append(row)

        return grid

    def parse(self) -> list[ParsedChunk]:
        """
        Open the Excel workbook and extract tables from all visible sheets.

        Each sheet is treated as one logical table (table_index=0).
        If a sheet contains multiple visually separated tables (blank-row
        delimited), they are split and each one is treated independently.

        Returns:
            Flat list of ParsedChunk objects from all sheets.
        """
        all_chunks: list[ParsedChunk] = []

        logger.info("Parsing Excel: %s", self._path)

        try:
            wb = openpyxl.load_workbook(
                filename=str(self._path),
                read_only=False,   # Must be False to allow merge expansion
                data_only=True,    # Read computed values, not formulas
            )
        except Exception as exc:
            raise IngestionError(
                f"Failed to open Excel file '{self._path}': {exc}"
            ) from exc

        visible_sheets = [
            ws for ws in wb.worksheets
            if ws.sheet_state == "visible"
        ]

        if not visible_sheets:
            raise MalformedGridError(
                f"Excel file '{self._doc_name}' has no visible sheets."
            )

        for ws in visible_sheets:
            sheet_label = f"sheet:{ws.title}"
            source_label = f"{self._doc_name}:{sheet_label}"

            try:
                grid = self._extract_sheet_grid(ws)
            except MalformedGridError as exc:
                logger.warning(
                    "Skipping sheet '%s' in '%s' — %s",
                    ws.title,
                    self._doc_name,
                    exc,
                )
                continue

            # Split the sheet grid into sub-tables separated by blank rows
            sub_tables = _split_grid_on_blank_rows(grid)

            for tbl_idx, sub_grid in enumerate(sub_tables):
                try:
                    chunks = _grid_to_chunks(
                        grid=sub_grid,
                        doc_name=self._doc_name,
                        doc_type=DocumentType.EXCEL,
                        source_path=str(self._path.resolve()),
                        page_number=ws.title,
                        table_index=tbl_idx,
                        source_label=source_label,
                    )
                    all_chunks.extend(chunks)
                except (MissingHeaderError, MalformedGridError) as exc:
                    logger.warning(
                        "Skipping sub-table %d in sheet '%s' — %s: %s",
                        tbl_idx,
                        ws.title,
                        type(exc).__name__,
                        exc,
                    )
                    continue

        logger.info(
            "Excel parse complete: %s → %d total chunks",
            self._doc_name,
            len(all_chunks),
        )
        return all_chunks


# ---------------------------------------------------------------------------
# Blank-row sub-table splitter
# ---------------------------------------------------------------------------


def _split_grid_on_blank_rows(
    grid: list[list[str]],
) -> list[list[list[str]]]:
    """
    Split a grid into sub-grids wherever two or more consecutive blank rows appear.

    This handles Excel sheets that pack multiple logical tables into one sheet
    (e.g. Monday timetable, then a blank separator, then Tuesday timetable).

    A single blank row is treated as a visual spacer within a table and is
    kept.  Two or more consecutive blank rows trigger a split.

    Returns:
        List of sub-grids, each of which is a list[list[str]].
        Sub-grids with fewer than 2 rows are discarded.
    """
    sub_tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    consecutive_blanks = 0

    for row in grid:
        if _is_empty_row(row):
            consecutive_blanks += 1
            if consecutive_blanks == 1:
                # First blank row — keep as visual spacer
                current.append(row)
            elif consecutive_blanks == 2:
                # Second consecutive blank — commit current sub-table
                if len(current) >= 2:
                    sub_tables.append(current)
                current = []
            # Further consecutive blanks are swallowed
        else:
            consecutive_blanks = 0
            current.append(row)

    if len(current) >= 2:
        sub_tables.append(current)

    if not sub_tables:
        # No splits occurred — the whole grid is one table
        return [grid] if len(grid) >= 2 else []

    return sub_tables


# ---------------------------------------------------------------------------
# Public factory function
# ---------------------------------------------------------------------------


def parse_document(file_path: str | Path) -> list[ParsedChunk]:
    """
    Parse a university document (PDF or Excel) into a flat list of ParsedChunks.

    This is the single public entry point for the ingestion pipeline.
    Callers do not need to know which parser to instantiate.

    Args:
        file_path: Absolute or relative path to the source document.

    Returns:
        list[ParsedChunk] — may be empty if the document contains no tables.

    Raises:
        FileNotFoundError:    File does not exist.
        UnsupportedFileError: File extension is not .pdf / .xlsx / .xls.
        IngestionError:       Any unrecoverable parse failure.
    """
    path = Path(file_path).resolve()

    if not path.exists():
        raise FileNotFoundError(f"Document not found: {path}")

    suffix = path.suffix.lower()

    if suffix == ".pdf":
        return PDFTableParser(path).parse()
    elif suffix in ExcelTableParser._SUPPORTED_EXTENSIONS:
        return ExcelTableParser(path).parse()
    else:
        raise UnsupportedFileError(
            f"Unsupported file type '{suffix}'. "
            f"Supported: .pdf, .xlsx, .xls, .xlsm"
        )
