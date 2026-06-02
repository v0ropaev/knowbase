-- knowbase — migration 0002: pgvector embeddings + RAG-over-source baseline (DESIGN.md §10, PR-3).
-- Applied by migrations/versions/0002_embeddings_rag.py (split via sqlparse, run on a raw cursor).
-- Requires the pgvector extension (CI: pgvector/pgvector:pg17; local ephemeral cluster has it).

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

-- Derived acceleration columns on artifact. NOT part of the content-addressed artifact_id (db/0001 §6):
-- the embedding is recomputable from (payload, model). embedding_model_id records which model produced
-- it, so `kb embed` re-embeds exactly the rows whose model changed.
ALTER TABLE artifact ADD COLUMN embedding          vector(384);
ALTER TABLE artifact ADD COLUMN embedding_model_id text;

CREATE INDEX artifact_embedding_hnsw ON artifact USING hnsw (embedding vector_cosine_ops);

-- The RAG-over-source baseline (the "other arm"): raw source windows, NO provenance, NO grounding,
-- NO content-addressing. Deliberately separate from the knowledge model so the A/B is honest.
CREATE TABLE rag_chunk (
    sha        text NOT NULL REFERENCES commit_ref(sha),
    file_path  text NOT NULL,
    chunk_idx  int  NOT NULL,
    start_line int  NOT NULL,
    end_line   int  NOT NULL,
    raw_text   text NOT NULL,
    embedding  vector(384),
    model_id   text,
    PRIMARY KEY (sha, file_path, chunk_idx)
);

CREATE INDEX rag_chunk_embedding_hnsw ON rag_chunk USING hnsw (embedding vector_cosine_ops);

COMMIT;
