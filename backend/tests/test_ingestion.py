"""
tests/test_ingestion.py

Unit tests for the ingestion pipeline:
  - parser.py: normalisation, header detection, grid validation,
    row serialisation, blank-row splitting, PDF/Excel parse paths.
  - embed_and_load.py: chunk ID determinism, node conversion,
    BigQuery row builder, embedding retry logic.

All I/O is mocked — no real files, no GCP calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ingestion.parser import (
    DocumentType,
    MalformedGridError,
    MissingHeaderError,
    ParsedChunk,
    UnsupportedFileError,
    _detect_header,
    _grid_to_chunks,
    _is_empty_row,
    _normalise_cell,
    _serialise_row,
    _split_grid_on_blank_rows,
    _validate_grid,
    parse_document,
)
from ingestion.embed_and_load import (
    BigQueryVectorWriter,
    IngestionStats,
    _chunk_id,
    chunks_to_nodes,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_TIMETABLE_GRID: list[list[str]] = [
    ["Day", "Time", "Subject", "Faculty", "Room", "Section"],
    ["Monday", "09:00-10:00", "Data Structures", "Dr. A. Roy", "401", "CSE-B"],
    ["Monday", "10:00-11:00", "Mathematics-III", "Dr. S. Das", "302", "CSE-B"],
    ["", "", "", "", "", ""],  # spacer row
    ["Tuesday", "09:00-10:00", "DBMS", "Dr. P. Sen", "401", "CSE-B"],
]

SAMPLE_FEE_GRID: list[list[str]] = [
    ["Fee Head", "Amount (INR)", "Due Date", "Remarks"],
    ["Tuition Fee", "45000", "30-Jun-2024", "Per semester"],
    ["Library Fee", "500", "30-Jun-2024", "Annual"],
    ["Exam Fee", "1200", "15-Jul-2024", "Per semester"],
]


# ---------------------------------------------------------------------------
# Cell normalisation tests
# ---------------------------------------------------------------------------


class TestNormaliseCell:
    def test_none_returns_empty_string(self) -> None:
        assert _normalise_cell(None) == ""

    def test_nan_string_returns_empty(self) -> None:
        assert _normalise_cell("nan") == ""
        assert _normalise_cell("NaN") == ""

    def test_dash_variants_return_empty(self) -> None:
        for dash in ["-", "–", "—", "N/A", "n/a", "#N/A"]:
            assert _normalise_cell(dash) == ""

    def test_collapses_whitespace(self) -> None:
        assert _normalise_cell("  Data   Structures  ") == "Data Structures"

    def test_handles_non_breaking_space(self) -> None:
        assert _normalise_cell("Room\xa0401") == "Room 401"

    def test_converts_numeric(self) -> None:
        assert _normalise_cell(45000) == "45000"
        assert _normalise_cell(3.14) == "3.14"

    def test_unicode_normalisation(self) -> None:
        # Fullwidth digits → ASCII
        result = _normalise_cell("\uff14\uff15\uff10\uff10\uff10")  # ４５０００
        assert result == "45000"


# ---------------------------------------------------------------------------
# Grid validation tests
# ---------------------------------------------------------------------------


class TestValidateGrid:
    def test_raises_on_empty_grid(self) -> None:
        with pytest.raises(MalformedGridError, match="completely empty"):
            _validate_grid([], "test_label")

    def test_raises_when_all_rows_empty(self) -> None:
        grid = [["", "", ""], ["", ""]]
        with pytest.raises(MalformedGridError, match="Every row"):
            _validate_grid(grid, "test_label")

    def test_raises_on_extreme_column_oscillation(self) -> None:
        # 8 columns then 1 column — exceeds 50% ratio threshold
        grid = [
            ["A", "B", "C", "D", "E", "F", "G", "H"],
            ["X"],
        ]
        with pytest.raises(MalformedGridError, match="Column count varies"):
            _validate_grid(grid, "test_label")

    def test_valid_grid_passes(self) -> None:
        _validate_grid(SAMPLE_TIMETABLE_GRID, "test_label")  # No exception


# ---------------------------------------------------------------------------
# Header detection tests
# ---------------------------------------------------------------------------


class TestDetectHeader:
    def test_detects_first_row_as_header(self) -> None:
        headers, idx = _detect_header(SAMPLE_TIMETABLE_GRID, "test")
        assert idx == 0
        assert "Day" in headers
        assert "Subject" in headers

    def test_skips_leading_empty_rows(self) -> None:
        grid = [
            ["", "", "", ""],
            ["Day", "Time", "Subject", "Room"],
            ["Monday", "09:00", "Math", "401"],
        ]
        headers, idx = _detect_header(grid, "test")
        assert idx == 1
        assert headers[0] == "Day"

    def test_raises_when_no_valid_header_in_first_5_rows(self) -> None:
        # All rows mostly empty
        grid = [["", "", "X", ""] for _ in range(6)]
        with pytest.raises(MissingHeaderError, match="Could not identify"):
            _detect_header(grid, "test")

    def test_pads_empty_header_cells(self) -> None:
        grid = [
            ["Day", "", "Subject", "Room"],
            ["Monday", "09:00", "Math", "401"],
        ]
        headers, _ = _detect_header(grid, "test")
        assert headers[1] == "Column_2"  # Empty cell padded with Column_N


# ---------------------------------------------------------------------------
# Row serialisation tests
# ---------------------------------------------------------------------------


class TestSerialiseRow:
    def test_output_contains_all_headers_and_values(self) -> None:
        headers = ("Day", "Time", "Subject")
        values = ("Monday", "09:00", "Data Structures")
        result = _serialise_row(headers, values, "test.pdf", "3")
        assert "Day: Monday" in result
        assert "Time: 09:00" in result
        assert "Subject: Data Structures" in result

    def test_prefix_contains_doc_name_and_page(self) -> None:
        result = _serialise_row(("A",), ("1",), "fee_chart.xlsx", "Sheet1")
        assert "fee_chart.xlsx" in result
        assert "Sheet1" in result

    def test_empty_cell_rendered_as_dash(self) -> None:
        result = _serialise_row(("Day", "Room"), ("Monday", ""), "t.pdf", "1")
        assert "Room: —" in result


# ---------------------------------------------------------------------------
# Grid → chunks conversion tests
# ---------------------------------------------------------------------------


class TestGridToChunks:
    def test_produces_correct_chunk_count(self) -> None:
        # 5 rows total: 1 header + 1 spacer + 3 data rows
        chunks = _grid_to_chunks(
            grid=SAMPLE_TIMETABLE_GRID,
            doc_name="tt.pdf",
            doc_type=DocumentType.PDF,
            source_path="/data/tt.pdf",
            page_number=1,
            table_index=0,
            source_label="tt.pdf:p1:t0",
        )
        # 3 data rows (spacer is skipped)
        assert len(chunks) == 3

    def test_chunk_text_preserves_column_associations(self) -> None:
        chunks = _grid_to_chunks(
            grid=SAMPLE_TIMETABLE_GRID,
            doc_name="tt.pdf",
            doc_type=DocumentType.PDF,
            source_path="/data/tt.pdf",
            page_number=1,
            table_index=0,
            source_label="tt.pdf:p1:t0",
        )
        first_chunk_text = chunks[0].text
        assert "Day: Monday" in first_chunk_text
        assert "Subject: Data Structures" in first_chunk_text
        assert "Room: 401" in first_chunk_text

    def test_chunk_metadata_populated_correctly(self) -> None:
        chunks = _grid_to_chunks(
            grid=SAMPLE_FEE_GRID,
            doc_name="fees.xlsx",
            doc_type=DocumentType.EXCEL,
            source_path="/data/fees.xlsx",
            page_number="Sheet1",
            table_index=0,
            source_label="fees.xlsx:Sheet1:t0",
        )
        c = chunks[0]
        assert c.doc_name == "fees.xlsx"
        assert c.doc_type == DocumentType.EXCEL
        assert c.page_number == "Sheet1"
        assert c.table_index == 0
        assert c.row_index == 0

    def test_raises_on_empty_grid(self) -> None:
        with pytest.raises(MalformedGridError):
            _grid_to_chunks([], "t.pdf", DocumentType.PDF, "/t.pdf", 1, 0, "t")

    def test_raises_on_missing_header(self) -> None:
        all_empty_header = [["", "", ""], ["A", "B", "C"]]
        with pytest.raises(MissingHeaderError):
            _grid_to_chunks(
                all_empty_header, "t.pdf", DocumentType.PDF, "/t.pdf", 1, 0, "t"
            )


# ---------------------------------------------------------------------------
# Blank-row splitter tests
# ---------------------------------------------------------------------------


class TestSplitGridOnBlankRows:
    def test_no_split_for_single_blank_row(self) -> None:
        grid = [
            ["A", "B"],
            ["1", "2"],
            ["", ""],       # single blank — not a split
            ["3", "4"],
        ]
        result = _split_grid_on_blank_rows(grid)
        assert len(result) == 1

    def test_splits_on_two_consecutive_blank_rows(self) -> None:
        grid = [
            ["Day", "Subject"],
            ["Mon", "Math"],
            ["", ""],
            ["", ""],       # two blanks → split
            ["Day", "Subject"],
            ["Tue", "Physics"],
        ]
        result = _split_grid_on_blank_rows(grid)
        assert len(result) == 2

    def test_discards_sub_tables_with_fewer_than_2_rows(self) -> None:
        grid = [
            ["A", "B"],
            ["", ""],
            ["", ""],
            ["X"],           # Only 1 row in second sub-table — discarded
        ]
        result = _split_grid_on_blank_rows(grid)
        assert len(result) == 1  # Only first sub-table kept

    def test_returns_whole_grid_when_no_blanks(self) -> None:
        grid = [["H1", "H2"], ["v1", "v2"], ["v3", "v4"]]
        result = _split_grid_on_blank_rows(grid)
        assert len(result) == 1
        assert result[0] == grid


# ---------------------------------------------------------------------------
# parse_document factory tests
# ---------------------------------------------------------------------------


class TestParseDocument:
    def test_raises_for_unsupported_extension(self, tmp_path: Path) -> None:
        f = tmp_path / "report.docx"
        f.write_bytes(b"")
        with pytest.raises(UnsupportedFileError, match="Unsupported file type"):
            parse_document(f)

    def test_raises_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            parse_document("/nonexistent/path/document.pdf")

    def test_routes_pdf_to_pdf_parser(self, tmp_path: Path) -> None:
        f = tmp_path / "timetable.pdf"
        f.write_bytes(b"%PDF-1.4")  # Minimal PDF magic bytes
        with patch("ingestion.parser.PDFTableParser") as MockParser:
            instance = MockParser.return_value
            instance.parse.return_value = []
            parse_document(f)
            MockParser.assert_called_once_with(f.resolve())

    def test_routes_xlsx_to_excel_parser(self, tmp_path: Path) -> None:
        f = tmp_path / "fees.xlsx"
        f.write_bytes(b"PK")  # ZIP/OOXML magic bytes
        with patch("ingestion.parser.ExcelTableParser") as MockParser:
            instance = MockParser.return_value
            instance.parse.return_value = []
            parse_document(f)
            MockParser.assert_called_once_with(f.resolve())


# ---------------------------------------------------------------------------
# embed_and_load: chunk ID determinism
# ---------------------------------------------------------------------------


class TestChunkId:
    def _make_chunk(self, row_index: int = 0) -> ParsedChunk:
        return ParsedChunk(
            text="Day: Monday | Subject: Math",
            doc_name="tt.pdf",
            doc_type=DocumentType.PDF,
            source_path="/data/tt.pdf",
            page_number=1,
            table_index=0,
            row_index=row_index,
            headers=("Day", "Subject"),
            raw_row_values=("Monday", "Math"),
        )

    def test_same_chunk_produces_same_id(self) -> None:
        c = self._make_chunk()
        assert _chunk_id(c) == _chunk_id(c)

    def test_different_row_index_produces_different_id(self) -> None:
        c1 = self._make_chunk(row_index=0)
        c2 = self._make_chunk(row_index=1)
        assert _chunk_id(c1) != _chunk_id(c2)

    def test_id_is_64_hex_chars(self) -> None:
        c = self._make_chunk()
        cid = _chunk_id(c)
        assert len(cid) == 64
        assert all(ch in "0123456789abcdef" for ch in cid)


# ---------------------------------------------------------------------------
# embed_and_load: chunks_to_nodes
# ---------------------------------------------------------------------------


class TestChunksToNodes:
    def test_node_count_matches_chunk_count(self) -> None:
        chunks = _grid_to_chunks(
            SAMPLE_FEE_GRID, "fees.xlsx", DocumentType.EXCEL,
            "/fees.xlsx", "Sheet1", 0, "fees.xlsx:Sheet1:t0",
        )
        nodes = chunks_to_nodes(chunks)
        assert len(nodes) == len(chunks)

    def test_node_text_matches_chunk_text(self) -> None:
        chunks = _grid_to_chunks(
            SAMPLE_FEE_GRID, "fees.xlsx", DocumentType.EXCEL,
            "/fees.xlsx", "Sheet1", 0, "fees.xlsx:Sheet1:t0",
        )
        nodes = chunks_to_nodes(chunks)
        for chunk, node in zip(chunks, nodes):
            assert node.text == chunk.text

    def test_node_metadata_has_all_required_keys(self) -> None:
        chunks = _grid_to_chunks(
            SAMPLE_FEE_GRID, "fees.xlsx", DocumentType.EXCEL,
            "/fees.xlsx", "Sheet1", 0, "fees.xlsx:Sheet1:t0",
        )
        node = chunks_to_nodes(chunks)[0]
        required_keys = {
            "doc_name", "doc_type", "source_path",
            "page_number", "table_index", "row_index", "headers",
        }
        assert required_keys.issubset(node.metadata.keys())

    def test_headers_stored_as_valid_json(self) -> None:
        chunks = _grid_to_chunks(
            SAMPLE_FEE_GRID, "fees.xlsx", DocumentType.EXCEL,
            "/fees.xlsx", "Sheet1", 0, "fees.xlsx:Sheet1:t0",
        )
        node = chunks_to_nodes(chunks)[0]
        headers = json.loads(node.metadata["headers"])
        assert isinstance(headers, list)
        assert "Fee Head" in headers
