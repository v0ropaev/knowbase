-- knowbase — initial schema (MVP first vertical slice)
-- Target: PostgreSQL 17.  See DESIGN.md §5 (span identity vs location),
-- §6 (content-addressing), §8 (MVP scope).
--
-- SCOPE NOTE: this is the *trimmed* first migration. Deliberately ABSENT (deferred per §8):
--   artifact_depends_on + recursive-CTE invalidation, embeddings/pgvector/HNSW, tsvector /
--   pg_search BM25, snapshot Merkle-root. They arrive with the layers that need them.
--   The invalidation DAG in this slice is ONE HOP (span -> artifact), handled by a plain join.
--
-- This DDL will be wrapped into an Alembic migration when the Python project is scaffolded;
-- it is kept as reviewable SQL first (per the agreed "DESIGN.md + schema, then code" path).

BEGIN;

-- ---------------------------------------------------------------------------
-- Git anchoring
-- ---------------------------------------------------------------------------

-- A commit we have ingested. Source of truth for "knowledge AT a SHA".
CREATE TABLE commit_ref (
    sha          text        PRIMARY KEY,            -- 40-char hex (full SHA)
    parent_shas  text[]      NOT NULL DEFAULT '{}',
    committed_at timestamptz,
    ingested_at  timestamptz NOT NULL DEFAULT now()
);

-- Mutable branch pointer. Snapshots (below) are immutable; branches just move.
CREATE TABLE branch_ref (
    name       text        PRIMARY KEY,
    head_sha   text        NOT NULL REFERENCES commit_ref(sha),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Spans:  IDENTITY (content-addressed, location-free)  vs  LOCATION (per-SHA)
-- DESIGN.md §5 [LOCKED D6].
-- ---------------------------------------------------------------------------

-- The content-addressed IDENTITY of a code span. Carries NO file path and NO byte offsets.
-- span_id = sha256(normalization_version || lang || span_kind || fq_symbol_path
--                  || structural_fingerprint).  See DESIGN.md §5.
CREATE TABLE code_span (
    span_id                bytea   PRIMARY KEY,          -- sha256 digest (32 bytes)
    lang                   text    NOT NULL DEFAULT 'python',
    span_kind              text    NOT NULL,             -- module|class|function|method|import
    fq_symbol_path         text    NOT NULL,             -- fully-qualified from package root
    structural_fingerprint bytea   NOT NULL,             -- normalized tree-sitter node hash
    normalization_version  int     NOT NULL,             -- lets the identity rule evolve safely
    symbol_id              text                          -- RESERVED for the deferred SCIP upgrade
);

-- The per-SHA LOCATION of a span. Offsets/text live here because they shift between commits
-- even when identity is unchanged. Re-resolved on every commit. (DESIGN.md §5.)
CREATE TABLE span_occurrence (
    span_id    bytea NOT NULL REFERENCES code_span(span_id),
    sha        text  NOT NULL REFERENCES commit_ref(sha),
    file_path  text  NOT NULL,
    start_byte int   NOT NULL,
    end_byte   int   NOT NULL,
    start_line int   NOT NULL,
    end_line   int   NOT NULL,
    raw_text   text  NOT NULL,                           -- exact bytes at this SHA (provenance display)
    PRIMARY KEY (span_id, sha)
);
CREATE INDEX span_occurrence_sha_idx       ON span_occurrence (sha);
CREATE INDEX span_occurrence_file_sha_idx  ON span_occurrence (file_path, sha);  -- find_provenance(file@sha)

-- ---------------------------------------------------------------------------
-- Knowledge artifacts (content-addressed, immutable)
-- DESIGN.md §6.
-- ---------------------------------------------------------------------------

-- artifact_id = sha256(canonical(sorted derived_from span_ids) || extractor_id
--               || extractor_version || prompt_version || model_id || framework_versions_subset)
-- prompt_version / model_id are '' for deterministic extractors.
-- framework_versions participates in the key ONLY for extractors whose output depends on it
-- (API/entity) — NOT for the import graph.  (DESIGN.md §6, review fix: model_id MUST be folded
-- into the key for llm_grounded artifacts.)
CREATE TABLE artifact (
    artifact_id        bytea       PRIMARY KEY,          -- sha256 digest (32 bytes)
    kind               text        NOT NULL,             -- import_edge|api_route|lib_symbol|...
    extractor_id       text        NOT NULL,
    extractor_version  text        NOT NULL,
    prompt_version     text        NOT NULL DEFAULT '',
    model_id           text        NOT NULL DEFAULT '',
    framework_versions jsonb       NOT NULL DEFAULT '{}',
    is_deterministic   boolean     NOT NULL,
    confidence         real        NOT NULL DEFAULT 1.0,
    logical_key        text        NOT NULL,             -- stable human identity (also in snapshot_entry)
    payload            jsonb       NOT NULL,             -- the actual knowledge (route, edge, signature, ...)
    created_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT artifact_confidence_range
        CHECK (confidence >= 0.0 AND confidence <= 1.0),
    -- Deterministic artifacts must not carry prompt/model identity (keeps the key honest).
    CONSTRAINT artifact_deterministic_no_llm_keys
        CHECK (NOT is_deterministic OR (prompt_version = '' AND model_id = ''))
);
CREATE INDEX artifact_kind_idx        ON artifact (kind);
CREATE INDEX artifact_logical_key_idx ON artifact (logical_key);

-- PROVENANCE edge: artifact DERIVED_FROM span. The anti-hallucination grounding edge.
-- Every artifact MUST have >= 1 of these (enforced by the constraint trigger below).
CREATE TABLE artifact_derived_from (
    artifact_id bytea NOT NULL REFERENCES artifact(artifact_id) ON DELETE CASCADE,
    span_id     bytea NOT NULL REFERENCES code_span(span_id),
    role        text,                                    -- e.g. 'decorator','signature','response_model'
    PRIMARY KEY (artifact_id, span_id)
);
CREATE INDEX artifact_derived_from_span_idx ON artifact_derived_from (span_id);  -- reverse: invalidation seed

-- ---------------------------------------------------------------------------
-- Snapshot = git-tree-like manifest: knowledge AT a commit
-- DESIGN.md §6.
-- ---------------------------------------------------------------------------
CREATE TABLE snapshot_entry (
    sha         text  NOT NULL REFERENCES commit_ref(sha),
    logical_key text  NOT NULL,                          -- 'api:GET /orders', 'import:a->b', ...
    artifact_id bytea NOT NULL REFERENCES artifact(artifact_id),
    PRIMARY KEY (sha, logical_key)
);
CREATE INDEX snapshot_entry_artifact_idx ON snapshot_entry (artifact_id);

-- ---------------------------------------------------------------------------
-- Anti-hallucination invariant (DESIGN.md §3, §5 [LOCKED D5]):
-- every artifact has >= 1 derived_from row. Cannot be a plain FK, so a DEFERRED constraint
-- trigger checks at COMMIT time (lets a transaction insert artifact + its edges together).
-- ---------------------------------------------------------------------------
CREATE FUNCTION assert_artifact_is_grounded() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM artifact_derived_from df WHERE df.artifact_id = NEW.artifact_id
    ) THEN
        RAISE EXCEPTION
            'ungrounded artifact %: no derived_from span (anti-hallucination invariant, DESIGN.md §3)',
            encode(NEW.artifact_id, 'hex');
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER artifact_grounded_check
    AFTER INSERT ON artifact
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_artifact_is_grounded();

COMMIT;
