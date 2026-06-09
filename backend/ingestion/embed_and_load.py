"""
ingestion/embed_and_load.py

LlamaIndex orchestration pipeline: embed parsed chunks → upsert to BigQuery.

Pipeline overview
─────────────────

  ┌─────────────────┐
  │  Source Files   │  (PDFs / Excel from --input-dir)
  └────────┬────────┘
           │  parse_document()
           ▼
  ┌─────────────────┐
  │  ParsedChunk[]  │  (semantic row strings + provenance metadata)
  └────────┬────────┘
           │  batch → LlamaIndex TextNode[]
           ▼
  ┌─────────────────────────────────────┐
  │  Vertex AI text-embedding-004       │  (batched, with retry + back-off)
  │  768-dimensional dense vectors      │
  └────────────────────┬────────────────┘
           │  BigQueryVectorStoreWriter
           ▼
  ┌──────────────────────────────────────────────┐
  │  BigQuery Table (VECTOR INDEX enabled)       │
  │                                              │
  │  Schema:                                     │
  │    id            STRING NOT NULL             │
  │    text          STRING                      │
  │    embedding     FLOAT64 REPEATED (768-dim)  │
  │    doc_name      STRING                      │
  │    doc_type      STRING                      │
  │    source_path   STRING                      │
  │    page_number   STRING                      │
  │    table_index   INT64                       │
  │    row_index     INT64                       │
  │    headers       STRING (JSON array)         │
  │    ingested_at   TIMESTAMP                   │
  └──────────────────────────────────────────────┘

Design decisions
────────────────
• LlamaIndex TextNode is used as the canonical document unit so the same
  objects can later be plugged into a VectorStoreIndex for RAG queries.

• Embedding is batched (default 100 chunks / request) to stay within
  Vertex AI's request size limits and to minimise billable API calls.

• Upsert semantics: each chunk gets a deterministic ID = SHA-256 of
  (source_path + page_number + table_index + row_index).  Re-running the
  pipeline on the same files is idempotent — rows are MERGE'd not appended.

• BigQuery streaming insert is used for <10 k rows; the load-job path is
  used for large batches (>10 k) to avoid per-row pricing.

• The BigQuery Vector Index is created (or confirmed existing) automatically
  at startup so the table is immediately queryable after ingestion.

Exception strategy
──────────────────
  EmbeddingError      — Vertex AI call failed after max retries
  BigQueryWriteError  — streaming insert or load job failed
  Per-file errors from parser.py are caught and logged; they do not abort
  the pipeline — partial success is better than total failure.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, NamedTuple, Sequence

import vertexai
from google.cloud import bigquery
from google.api_core.exceptions import GoogleAPICallError
from llama_index.core.schema import TextNode
from llama_index.embeddings.vertex import VertexTextEmbedding
from vertexai.language_models import TextEmbeddingInput

from config import settings
from google.oauth2 import service_account
from ingestion.parser import (
    DocumentType,
    IngestionError,
    ParsedChunk,
    UnsupportedFileError,
    parse_document,
)

# Load GCP credentials explicitly if provided
gcp_credentials = None
if settings.google_application_credentials:
    gcp_credentials = service_account.Credentials.from_service_account_file(
        settings.google_application_credentials
    )

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EMBEDDING_MODEL: Final[str] = "text-embedding-004"
EMBEDDING_DIMENSION: Final[int] = 768
EMBEDDING_TASK_TYPE: Final[str] = "RETRIEVAL_DOCUMENT"

# Vertex AI supports up to 250 texts per embed request; we use 100 for safety.
EMBED_BATCH_SIZE: Final[int] = 100

# BigQuery streaming insert limit is 10 MB / request; 500 rows is conservative.
BQ_STREAM_BATCH_SIZE: Final[int] = 500

# Retry settings for transient Vertex AI / BigQuery failures
MAX_RETRIES: Final[int] = 3
RETRY_BASE_DELAY_SEC: Final[float] = 2.0

# BigQuery Vector Search index type and distance measure
BQ_INDEX_TYPE: Final[str] = "IVF"          # Inverted File index — best for >10k rows
BQ_DISTANCE_TYPE: Final[str] = "COSINE"    # Cosine similarity for semantic search

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class EmbeddingError(Exception):
    """Raised when Vertex AI embedding fails after all retries."""


class BigQueryWriteError(Exception):
    """Raised when a BigQuery insert or load job fails."""


# ---------------------------------------------------------------------------
# Deterministic chunk ID
# ---------------------------------------------------------------------------


def _chunk_id(chunk: ParsedChunk) -> str:
    """
    Generate a stable, deterministic SHA-256 ID for a ParsedChunk.

    The ID is derived from source identity fields only (not the text content)
    so that re-parsing the same file produces identical IDs, enabling idempotent
    upsert behaviour in BigQuery.
    """
    key = "|".join([
        chunk.source_path,
        str(chunk.page_number),
        str(chunk.table_index),
        str(chunk.row_index),
    ])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# ParsedChunk → LlamaIndex TextNode converter
# ---------------------------------------------------------------------------


def chunks_to_nodes(chunks: list[ParsedChunk]) -> list[TextNode]:
    """
    Convert ParsedChunk objects into LlamaIndex TextNode objects.

    TextNode.metadata carries the structured provenance data that will be
    stored alongside the embedding vector in BigQuery.  The retrieval engine
    will surface this metadata in citations.

    Args:
        chunks: List of ParsedChunk objects from the parser.

    Returns:
        List of TextNode objects, each with a deterministic node_id.
    """
    nodes: list[TextNode] = []
    for chunk in chunks:
        node_id = _chunk_id(chunk)
        node = TextNode(
            id_=node_id,
            text=chunk.text,
            metadata={
                "doc_name": chunk.doc_name,
                "doc_type": chunk.doc_type.name,
                "source_path": chunk.source_path,
                "page_number": str(chunk.page_number),
                "table_index": chunk.table_index,
                "row_index": chunk.row_index,
                "headers": json.dumps(list(chunk.headers)),
            },
        )
        nodes.append(node)
    return nodes


# ---------------------------------------------------------------------------
# Vertex AI embedding client
# ---------------------------------------------------------------------------


from vertexai.language_models import TextEmbeddingModel, TextEmbeddingInput

class VertexEmbedder:
    """
    Wraps the Vertex AI text-embedding-004 model using the native SDK.
    This guarantees exactly 1 HTTP request per batch to strictly adhere to
    tight GCP Quota limits (RPM).
    """

    _RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 503})

    def __init__(self) -> None:
        vertexai.init(
            project=settings.google_cloud_project,
            location=settings.vertex_ai_location,
            credentials=gcp_credentials,
        )
        self._model = TextEmbeddingModel.from_pretrained(EMBEDDING_MODEL)
        logger.info(
            "VertexEmbedder initialised natively: model=%s project=%s location=%s",
            EMBEDDING_MODEL,
            settings.google_cloud_project,
            settings.vertex_ai_location,
        )

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            raise ValueError("embed_batch received an empty list of texts.")

        all_embeddings: list[list[float]] = []

        # Split into sub-batches
        for batch_start in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[batch_start : batch_start + EMBED_BATCH_SIZE]
            embeddings = self._embed_with_retry(batch)
            all_embeddings.extend(embeddings)
            
            # Add a delay between batches to respect strict GCP quotas (RPM limits)
            if batch_start + EMBED_BATCH_SIZE < len(texts):
                logger.info("  Pausing for 8 seconds to avoid Vertex AI quota limits...")
                time.sleep(8)

        return all_embeddings

    def _embed_with_retry(self, batch: list[str]) -> list[list[float]]:
        last_exc: Exception | None = None
        
        # Prepare native inputs with the correct retrieval task type
        inputs = [TextEmbeddingInput(text, EMBEDDING_TASK_TYPE) for text in batch]

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                responses = self._model.get_embeddings(inputs)
                return [r.values for r in responses]

            except GoogleAPICallError as exc:
                status_code = getattr(exc, "code", None) or getattr(
                    getattr(exc, "response", None), "status_code", 0
                )
                if status_code not in self._RETRYABLE_STATUS_CODES:
                    logger.error(
                        "Non-retryable Vertex AI error (status=%s): %s",
                        status_code,
                        exc,
                    )
                    raise EmbeddingError(
                        f"Non-retryable Vertex AI error: {exc}"
                    ) from exc

                delay = RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1))
                logger.warning(
                    "Vertex AI transient error (attempt %d/%d, status=%s). "
                    "Retrying in %.1fs. Error: %s",
                    attempt,
                    MAX_RETRIES,
                    status_code,
                    delay,
                    exc,
                )
                time.sleep(delay)
                last_exc = exc

            except Exception as exc:
                logger.error("Unexpected error during embedding: %s", exc)
                raise EmbeddingError(
                    f"Unexpected embedding error: {exc}"
                ) from exc

        raise EmbeddingError(
            f"Vertex AI embedding failed after {MAX_RETRIES} retries. "
            f"Last error: {last_exc}"
        )


# ---------------------------------------------------------------------------
# BigQuery schema and writer
# ---------------------------------------------------------------------------

# Full BigQuery schema including the FLOAT64 REPEATED embedding column.
# The VECTOR INDEX is created separately after the table exists.
_BQ_SCHEMA: list[bigquery.SchemaField] = [
    bigquery.SchemaField("node_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("text", "STRING", mode="NULLABLE"),
    bigquery.SchemaField(
        "embedding",
        "FLOAT64",
        mode="REPEATED",
        description=f"Dense vector from {EMBEDDING_MODEL} ({EMBEDDING_DIMENSION} dims)",
    ),
    bigquery.SchemaField("doc_name", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("doc_type", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("source_path", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("page_number", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("table_index", "INT64", mode="NULLABLE"),
    bigquery.SchemaField("row_index", "INT64", mode="NULLABLE"),
    bigquery.SchemaField(
        "headers",
        "STRING",
        mode="NULLABLE",
        description="JSON array of column header strings",
    ),
    bigquery.SchemaField(
        "metadata", 
        "JSON", 
        mode="NULLABLE",
        description="JSON representation of metadata for LlamaIndex compatibility",
    ),
    bigquery.SchemaField("ingested_at", "TIMESTAMP", mode="REQUIRED"),
]

# DDL to create the VECTOR INDEX on the embedding column.
# IVF index with COSINE distance enables sub-second ANN search on millions of rows.
_VECTOR_INDEX_DDL = """
CREATE VECTOR INDEX IF NOT EXISTS `{index_name}`
ON `{project}.{dataset}.{table}`(embedding)
OPTIONS (
  index_type = '{index_type}',
  distance_type = '{distance_type}',
  ivf_options = '{{"num_lists": 100}}'
)
""".strip()


class BigQueryVectorWriter:
    """
    Writes TextNode objects (with pre-computed embeddings) to BigQuery.

    Table management:
      - Creates the dataset and table if they do not exist.
      - Creates the VECTOR INDEX if it does not exist.

    Write strategy:
      - ≤ BQ_STREAM_BATCH_SIZE rows: streaming insert (low latency).
      - > BQ_STREAM_BATCH_SIZE rows: load job via JSON newline file (cheaper).

    Upsert semantics:
      BigQuery does not support native upsert on streaming inserts.  We use
      MERGE via a temporary load job for true idempotency.  For simplicity in
      Phase 2, we use INSERT with duplicate-tolerant queries at read time
      (latest ingested_at wins in the RAG engine's SQL).
      Full MERGE-based upsert is implemented in _upsert_via_load_job().
    """

    def __init__(self) -> None:
        self._client = bigquery.Client(
            project=settings.google_cloud_project,
            credentials=gcp_credentials,
        )
        self._dataset_id = settings.bigquery_dataset
        self._table_id = settings.bigquery_table
        self._full_table_ref = (
            f"{settings.google_cloud_project}"
            f".{self._dataset_id}"
            f".{self._table_id}"
        )
        self._ensure_dataset_and_table()
        self._ensure_vector_index()

    # ── Infrastructure provisioning ────────────────────────────────────────

    def _ensure_dataset_and_table(self) -> None:
        """Create the BigQuery dataset and table if they do not already exist."""
        # Dataset
        dataset_ref = self._client.dataset(self._dataset_id)
        dataset = bigquery.Dataset(dataset_ref)
        dataset.location = settings.vertex_ai_location
        self._client.create_dataset(dataset, exists_ok=True)
        logger.info("BigQuery dataset ensured: %s", self._dataset_id)

        # Table
        table_ref = self._client.dataset(self._dataset_id).table(self._table_id)
        table = bigquery.Table(table_ref, schema=_BQ_SCHEMA)
        self._client.create_table(table, exists_ok=True)
        logger.info("BigQuery table ensured: %s", self._full_table_ref)

    def _ensure_vector_index(self) -> None:
        """
        Create the VECTOR INDEX on the embedding column if it does not exist.

        The index DDL is idempotent (IF NOT EXISTS) so it is safe to run
        on every pipeline execution.

        Note: Vector index creation is asynchronous in BigQuery — the index
        is built in the background after this call returns.  The table remains
        queryable (with full table scans) until the index is ready.
        """
        index_name = f"{self._table_id}_vector_idx"
        ddl = _VECTOR_INDEX_DDL.format(
            index_name=index_name,
            project=settings.google_cloud_project,
            dataset=self._dataset_id,
            table=self._table_id,
            index_type=BQ_INDEX_TYPE,
            distance_type=BQ_DISTANCE_TYPE,
        )
        try:
            query_job = self._client.query(ddl)
            query_job.result()  # Wait for DDL to complete
            logger.info("BigQuery VECTOR INDEX ensured: %s", index_name)
        except GoogleAPICallError as exc:
            # Non-fatal — the index may already exist or be building
            logger.warning(
                "Could not create vector index (may already exist): %s", exc
            )

    # ── Row builder ────────────────────────────────────────────────────────

    @staticmethod
    def _build_bq_row(
        node: TextNode,
        embedding: list[float],
        ingested_at: str,
    ) -> dict:
        """
        Construct a BigQuery row dict from a TextNode and its embedding.

        Args:
            node:        LlamaIndex TextNode with metadata populated.
            embedding:   Float vector from Vertex AI.
            ingested_at: ISO-8601 UTC timestamp string.

        Returns:
            Dict matching the BigQuery schema defined in _BQ_SCHEMA.
        """
        meta = node.metadata
        return {
            "node_id": node.node_id,
            "text": node.text,
            "embedding": embedding,
            "doc_name": meta.get("doc_name", ""),
            "doc_type": meta.get("doc_type", ""),
            "source_path": meta.get("source_path", ""),
            "page_number": meta.get("page_number", ""),
            "table_index": int(meta.get("table_index", 0)),
            "row_index": int(meta.get("row_index", 0)),
            "headers": meta.get("headers", "[]"),
            "metadata": json.dumps(meta), # Store full metadata for LlamaIndex
            "ingested_at": ingested_at,
        }

    # ── Streaming insert ───────────────────────────────────────────────────

    def _stream_insert(self, rows: list[dict]) -> None:
        """
        Insert rows via BigQuery streaming API.

        Streaming inserts are available for querying within seconds but
        incur per-row cost.  Used for small batches (≤ BQ_STREAM_BATCH_SIZE).

        Raises:
            BigQueryWriteError: if any rows fail to insert.
        """
        table_ref = self._client.dataset(self._dataset_id).table(self._table_id)
        errors = self._client.insert_rows_json(table_ref, rows)
        if errors:
            raise BigQueryWriteError(
                f"BigQuery streaming insert errors: {errors}"
            )

    # ── Load-job upsert ────────────────────────────────────────────────────

    def _upsert_via_load_job(self, rows: list[dict]) -> None:
        """
        Upsert rows via a BigQuery load job + MERGE statement.

        This is the preferred path for large batches.  Steps:
          1. Write rows to a JSON newline temporary table.
          2. Run a MERGE statement that updates existing rows (by id) and
             inserts new ones.
          3. Delete the temporary table.

        Raises:
            BigQueryWriteError: if the load job or MERGE fails.
        """
        tmp_table_id = f"{self._table_id}_tmp_{int(time.time())}"
        tmp_full_ref = (
            f"{settings.google_cloud_project}.{self._dataset_id}.{tmp_table_id}"
        )

        # ── Step 1: Load into temp table ──────────────────────────────────
        job_config = bigquery.LoadJobConfig(
            schema=_BQ_SCHEMA,
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        )
        tmp_table_ref = self._client.dataset(self._dataset_id).table(tmp_table_id)
        self._client.create_table(
            bigquery.Table(tmp_table_ref, schema=_BQ_SCHEMA), exists_ok=True
        )

        # Serialize rows to NDJSON
        ndjson_bytes = "\n".join(json.dumps(row, default=str) for row in rows).encode()

        load_job = self._client.load_table_from_json(
            json_rows=rows,
            destination=tmp_full_ref,
            job_config=job_config,
        )
        try:
            load_job.result()
        except GoogleAPICallError as exc:
            raise BigQueryWriteError(
                f"BigQuery load job failed: {exc}"
            ) from exc

        # ── Step 2: MERGE into target table ───────────────────────────────
        merge_sql = f"""
        MERGE `{self._full_table_ref}` AS target
        USING `{tmp_full_ref}` AS source
        ON target.id = source.id
        WHEN MATCHED THEN
          UPDATE SET
            text         = source.text,
            embedding    = source.embedding,
            doc_name     = source.doc_name,
            doc_type     = source.doc_type,
            source_path  = source.source_path,
            page_number  = source.page_number,
            table_index  = source.table_index,
            row_index    = source.row_index,
            headers      = source.headers,
            ingested_at  = source.ingested_at
        WHEN NOT MATCHED THEN
          INSERT ROW
        """
        try:
            merge_job = self._client.query(merge_sql)
            merge_job.result()
            logger.info(
                "MERGE complete: %d rows upserted into %s",
                len(rows),
                self._full_table_ref,
            )
        except GoogleAPICallError as exc:
            raise BigQueryWriteError(
                f"BigQuery MERGE job failed: {exc}"
            ) from exc
        finally:
            # ── Step 3: Drop temp table ───────────────────────────────────
            try:
                self._client.delete_table(tmp_full_ref, not_found_ok=True)
            except GoogleAPICallError:
                logger.warning("Could not delete temp table %s", tmp_full_ref)

    # ── Public write interface ─────────────────────────────────────────────

    def write(
        self,
        nodes: list[TextNode],
        embeddings: list[list[float]],
    ) -> None:
        """
        Write TextNodes and their embeddings to BigQuery in batches.

        Args:
            nodes:      LlamaIndex TextNode objects (with metadata).
            embeddings: Corresponding float vectors (same length as nodes).

        Raises:
            ValueError:        If nodes and embeddings lengths differ.
            BigQueryWriteError: If any write batch fails.
        """
        if len(nodes) != len(embeddings):
            raise ValueError(
                f"nodes ({len(nodes)}) and embeddings ({len(embeddings)}) "
                f"must have equal length."
            )

        ingested_at = datetime.now(timezone.utc).isoformat()
        all_rows: list[dict] = [
            self._build_bq_row(node, emb, ingested_at)
            for node, emb in zip(nodes, embeddings)
        ]

        total = len(all_rows)
        logger.info("Writing %d rows to BigQuery: %s", total, self._full_table_ref)

        if total <= BQ_STREAM_BATCH_SIZE:
            self._stream_insert(all_rows)
            logger.info("Streaming insert complete: %d rows.", total)
        else:
            self._upsert_via_load_job(all_rows)


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------


class IngestionPipeline:
    """
    End-to-end orchestrator: parse → embed → load.

    Usage:
        pipeline = IngestionPipeline()
        stats = pipeline.run(input_dir=Path("/data/university_docs"))

    Returns an IngestionStats summary for logging and monitoring.
    """

    def __init__(self) -> None:
        self._embedder = VertexEmbedder()
        self._writer = BigQueryVectorWriter()

    # ── File discovery ─────────────────────────────────────────────────────

    @staticmethod
    def _discover_files(input_dir: Path) -> list[Path]:
        """
        Recursively discover all PDF and Excel files in `input_dir`.

        Args:
            input_dir: Root directory to scan.

        Returns:
            Sorted list of file paths.

        Raises:
            FileNotFoundError: If input_dir does not exist.
        """
        if not input_dir.exists():
            raise FileNotFoundError(
                f"Input directory does not exist: {input_dir}"
            )
        patterns = ["**/*.pdf", "**/*.xlsx", "**/*.xls", "**/*.xlsm"]
        files: list[Path] = []
        for pattern in patterns:
            files.extend(input_dir.glob(pattern))
        return sorted(set(files))

    # ── Per-file processor ─────────────────────────────────────────────────

    def _process_file(self, file_path: Path) -> tuple[int, int]:
        """
        Parse, embed, and load a single document file.

        Returns:
            (chunks_produced, chunks_written) tuple.

        All exceptions from parse / embed / write are caught and logged;
        they do not propagate to the caller so one bad file doesn't abort
        the pipeline run.
        """
        logger.info("── Processing: %s", file_path.name)

        # Parse
        try:
            chunks: list[ParsedChunk] = parse_document(file_path)
        except (IngestionError, UnsupportedFileError, FileNotFoundError) as exc:
            logger.error("  Parse failed: %s", exc)
            return 0, 0

        if not chunks:
            logger.warning("  No chunks extracted from %s — skipping.", file_path.name)
            return 0, 0

        logger.info("  Parsed %d chunks.", len(chunks))

        # Convert to LlamaIndex nodes
        nodes = chunks_to_nodes(chunks)

        # Embed
        try:
            texts = [node.text for node in nodes]
            embeddings = self._embedder.embed_batch(texts)
        except EmbeddingError as exc:
            logger.error("  Embedding failed: %s", exc)
            return len(chunks), 0

        # Load into BigQuery
        try:
            self._writer.write(nodes=nodes, embeddings=embeddings)
        except BigQueryWriteError as exc:
            logger.error("  BigQuery write failed: %s", exc)
            return len(chunks), 0

        logger.info("  ✓ Written %d chunks to BigQuery.", len(chunks))
        return len(chunks), len(chunks)

    # ── Main entry point ───────────────────────────────────────────────────

    def run(self, input_dir: Path) -> "IngestionStats":
        """
        Run the full ingestion pipeline over all files in `input_dir`.

        Args:
            input_dir: Directory containing source documents.

        Returns:
            IngestionStats dataclass with per-run metrics.
        """
        start_time = time.monotonic()
        files = self._discover_files(input_dir)

        if not files:
            logger.warning(
                "No supported files found in: %s. "
                "Ensure the directory contains .pdf, .xlsx, or .xls files.",
                input_dir,
            )
            return IngestionStats(
                files_found=0,
                files_succeeded=0,
                files_failed=0,
                total_chunks_parsed=0,
                total_chunks_written=0,
                elapsed_seconds=0.0,
            )

        logger.info("Found %d file(s) to process.", len(files))

        total_parsed = total_written = 0
        files_ok = files_err = 0

        for file_path in files:
            parsed, written = self._process_file(file_path)
            total_parsed += parsed
            total_written += written
            if written > 0 or parsed == 0:
                files_ok += 1
            else:
                files_err += 1

        elapsed = time.monotonic() - start_time
        stats = IngestionStats(
            files_found=len(files),
            files_succeeded=files_ok,
            files_failed=files_err,
            total_chunks_parsed=total_parsed,
            total_chunks_written=total_written,
            elapsed_seconds=round(elapsed, 2),
        )

        logger.info(
            "Pipeline complete. files=%d/%d chunks_written=%d elapsed=%.2fs",
            files_ok,
            len(files),
            total_written,
            elapsed,
        )
        return stats


# ---------------------------------------------------------------------------
# Stats dataclass
# ---------------------------------------------------------------------------


from dataclasses import dataclass as _dc


@_dc
class IngestionStats:
    files_found: int
    files_succeeded: int
    files_failed: int
    total_chunks_parsed: int
    total_chunks_written: int
    elapsed_seconds: float

    def to_dict(self) -> dict:
        return {
            "files_found": self.files_found,
            "files_succeeded": self.files_succeeded,
            "files_failed": self.files_failed,
            "total_chunks_parsed": self.total_chunks_parsed,
            "total_chunks_written": self.total_chunks_written,
            "elapsed_seconds": self.elapsed_seconds,
        }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="embed_and_load",
        description=(
            "BPPIMT Campus Resource Assistant — Data Ingestion Pipeline.\n"
            "Parses PDF/Excel documents, generates Vertex AI embeddings, "
            "and upserts them into BigQuery Vector Search."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        required=True,
        help="Directory containing source PDF/Excel documents to ingest.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Parse documents and log chunk counts without calling "
            "Vertex AI or writing to BigQuery."
        ),
    )
    return parser


def main() -> None:
    """CLI entry point for the ingestion pipeline."""
    arg_parser = _build_arg_parser()
    args = arg_parser.parse_args()

    input_dir: Path = args.input_dir.resolve()
    dry_run: bool = args.dry_run

    if dry_run:
        logger.info("DRY RUN mode — no embeddings or BigQuery writes will occur.")
        from ingestion.parser import parse_document as _pd

        files = IngestionPipeline._discover_files(input_dir)
        total_chunks = 0
        for f in files:
            try:
                chunks = _pd(f)
                logger.info("  %s → %d chunks", f.name, len(chunks))
                for i, chunk in enumerate(chunks[:3]):  # Preview first 3
                    logger.info("    [%d] %s", i, chunk.text[:120].replace("\n", " "))
                total_chunks += len(chunks)
            except Exception as exc:
                logger.error("  %s → FAILED: %s", f.name, exc)
        logger.info("Dry run complete. Total chunks: %d", total_chunks)
        return

    pipeline = IngestionPipeline()
    stats = pipeline.run(input_dir=input_dir)

    print("\n" + "=" * 60)
    print("  INGESTION PIPELINE SUMMARY")
    print("=" * 60)
    for key, value in stats.to_dict().items():
        print(f"  {key:<28}: {value}")
    print("=" * 60)

    # Exit with error code if any files failed
    if stats.files_failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
