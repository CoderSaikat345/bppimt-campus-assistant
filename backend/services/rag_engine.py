"""
services/rag_engine.py

Core LlamaIndex RAG engine connecting FastAPI to Vertex AI and BigQuery Vector Search.

Security & IAM requirements:
When provisioning GCP service accounts for this component, explicitly use the
"Agent Platform User" role (roles/aiplatform.user) instead of the older
"Vertex AI User" role to grant the necessary permissions for embeddings and text generation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from google.cloud import bigquery
from google.oauth2 import service_account
from llama_index.core import Settings, VectorStoreIndex
from llama_index.embeddings.vertex import VertexTextEmbedding
from llama_index.llms.google_genai import GoogleGenAI
from llama_index.vector_stores.bigquery import BigQueryVectorStore

from config import settings

logger = logging.getLogger(__name__)


_RL_LAST_CALL_TIME: float = 0.0
_RL_LOCK: asyncio.Lock = asyncio.Lock()

class RateLimitedVertexLLM(GoogleGenAI):
    """
    Enterprise rate-limiter for Vertex AI.
    Guarantees a maximum of 5 RPM by enforcing a strict 12.5-second
    delay between consecutive API calls.
    """

    async def _throttle(self) -> None:
        global _RL_LAST_CALL_TIME
        async with _RL_LOCK:
            now = time.monotonic()
            elapsed = now - _RL_LAST_CALL_TIME
            if elapsed < 12.5:
                delay = 12.5 - elapsed
                logger.info(f"RateLimiter: Sleeping for {delay:.1f}s to respect 5 RPM quota...")
                await asyncio.sleep(delay)
            _RL_LAST_CALL_TIME = time.monotonic()

    async def achat(self, *args, **kwargs):
        await self._throttle()
        return await super().achat(*args, **kwargs)

    async def astream_chat(self, *args, **kwargs):
        await self._throttle()
        return await super().astream_chat(*args, **kwargs)


@dataclass
class QueryResult:
    """Standardized output structure for RAG engine queries."""

    answer: str
    sources: list[str]


class RAGEngine:
    """
    Singleton wrapper for the LlamaIndex orchestration logic.
    Maintains persistent connections to Vertex AI models and the BigQuery Vector Store.
    """

    _instance: RAGEngine | None = None

    @classmethod
    async def create(cls) -> RAGEngine:
        """
        Async factory for initializing the RAGEngine.
        Guarantees that network I/O and index loading happen before the app starts serving.
        """
        if cls._instance is None:
            cls._instance = cls()
            await cls._instance._init_engine()
        return cls._instance

    def __init__(self) -> None:
        self.query_engine: Any = None
        self.vector_store: BigQueryVectorStore | None = None
        self.index: VectorStoreIndex | None = None
        self._bq_client: bigquery.Client | None = None

    async def _init_engine(self) -> None:
        """Initialise Vertex AI components and BigQuery connections."""
        logger.info("Initializing RAG Engine with Vertex AI and BigQuery...")

        # Load GCP credentials explicitly if provided
        gcp_credentials = None
        if settings.google_application_credentials:
            gcp_credentials = service_account.Credentials.from_service_account_file(
                settings.google_application_credentials,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )

        # 1. Global LlamaIndex Settings — LLM
        # Using RateLimitedVertexLLM to respect the 5 RPM quota.
        # This replaces the deprecated llama-index-llms-vertex (Vertex class).
        vertex_config = {
            "project": settings.google_cloud_project,
            "location": settings.vertex_ai_location,
        }
        if gcp_credentials:
            vertex_config["credentials"] = gcp_credentials

        Settings.llm = RateLimitedVertexLLM(
            model="gemini-2.5-flash",
            vertexai_config=vertex_config,
            temperature=0.1,
        )

        embed_kwargs = {
            "model_name": "text-embedding-004",
            "project": settings.google_cloud_project,
            "location": settings.vertex_ai_location,
        }
        if gcp_credentials:
            embed_kwargs["credentials"] = gcp_credentials

        # Using text-embedding-004 for optimal retrieval dimensions
        Settings.embed_model = VertexTextEmbedding(**embed_kwargs)

        bq_kwargs = {
            "project_id": settings.google_cloud_project,
            "dataset_id": settings.bigquery_dataset,
            "table_id": settings.bigquery_table,
            "text_field": "text",
        }
        if gcp_credentials:
            bq_kwargs["auth_credentials"] = gcp_credentials

        # 2. BigQuery Vector Store connection
        self.vector_store = BigQueryVectorStore(**bq_kwargs)

        # 3. Create the index directly from the initialized vector store
        self.index = VectorStoreIndex.from_vector_store(
            vector_store=self.vector_store,
        )

        # 4. Construct the query engine
        # top_k=5 is chosen to ensure we pull enough timetable chunks to capture full context
        self.query_engine = self.index.as_query_engine(
            similarity_top_k=5,
        )

        logger.info("RAG engine initialised successfully.")

    async def query(
        self,
        query: str,
        user_email: str | None = None,
        conversation_id: str | None = None,
    ) -> QueryResult:
        """
        Execute a semantic search query against the BigQuery index.

        Args:
            query: The natural language question from the user.
            user_email: Optional context for logging or future row-level security.
            conversation_id: Optional context for threading.

        Returns:
            QueryResult containing the synthesized answer and metadata sources.
        """
        logger.info(
            "Executing RAG query: user=%s conv_id=%s query='%s'",
            user_email,
            conversation_id,
            query,
        )

        # Execute async RAG synthesis
        response = await self.query_engine.aquery(query)

        # Extract structured provenance from the retrieved nodes
        sources: list[str] = []
        if getattr(response, "source_nodes", None):
            for node in response.source_nodes:
                # Fallbacks in case the metadata is missing
                doc_name = node.metadata.get("doc_name", "Unknown Document")
                page_label = node.metadata.get("page_number", "N/A")
                sources.append(f"{doc_name} (Page {page_label})")

        # Deduplicate sources while preserving order
        unique_sources = list(dict.fromkeys(sources))

        return QueryResult(
            answer=str(response),
            sources=unique_sources,
        )

    async def close(self) -> None:
        """
        Gracefully release connections and flush buffers.

        Called during application shutdown via the lifespan handler in main.py.
        Cleans up the BigQuery client connection and resets the singleton.
        """
        logger.info("Closing RAG engine connections...")

        # Close BigQuery client if it was created
        if self._bq_client is not None:
            self._bq_client.close()
            self._bq_client = None

        # Clear references to allow GC
        self.query_engine = None
        self.index = None
        self.vector_store = None

        # Reset singleton so a fresh instance can be created if needed
        RAGEngine._instance = None

        logger.info("RAG engine shut down cleanly.")
