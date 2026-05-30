"""SQLAlchemy Core table definitions mirroring db/0001_initial.sql (DESIGN.md §11).

These are used for typed queries and inserts only — the tables are *created* by the migration,
never by this metadata. Keep column names/types in sync with the SQL (the schema is the source of
truth; this is a view onto it).
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

metadata = MetaData()

commit_ref = Table(
    "commit_ref",
    metadata,
    Column("sha", Text, primary_key=True),
    Column("parent_shas", ARRAY(Text), nullable=False),
    Column("committed_at", DateTime(timezone=True)),
    Column("ingested_at", DateTime(timezone=True)),
)

branch_ref = Table(
    "branch_ref",
    metadata,
    Column("name", Text, primary_key=True),
    Column("head_sha", Text, ForeignKey("commit_ref.sha"), nullable=False),
    Column("updated_at", DateTime(timezone=True)),
)

code_span = Table(
    "code_span",
    metadata,
    Column("span_id", LargeBinary, primary_key=True),
    Column("lang", Text, nullable=False),
    Column("span_kind", Text, nullable=False),
    Column("fq_symbol_path", Text, nullable=False),
    Column("structural_fingerprint", LargeBinary, nullable=False),
    Column("normalization_version", Integer, nullable=False),
    Column("symbol_id", Text),
)

span_occurrence = Table(
    "span_occurrence",
    metadata,
    Column("span_id", LargeBinary, ForeignKey("code_span.span_id"), nullable=False),
    Column("sha", Text, ForeignKey("commit_ref.sha"), nullable=False),
    Column("file_path", Text, nullable=False),
    Column("start_byte", Integer, nullable=False),
    Column("end_byte", Integer, nullable=False),
    Column("start_line", Integer, nullable=False),
    Column("end_line", Integer, nullable=False),
    Column("raw_text", Text, nullable=False),
    PrimaryKeyConstraint("span_id", "sha"),
)

artifact = Table(
    "artifact",
    metadata,
    Column("artifact_id", LargeBinary, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("extractor_id", Text, nullable=False),
    Column("extractor_version", Text, nullable=False),
    Column("prompt_version", Text, nullable=False),
    Column("model_id", Text, nullable=False),
    Column("framework_versions", JSONB, nullable=False),
    Column("is_deterministic", Boolean, nullable=False),
    Column("confidence", Float, nullable=False),
    Column("logical_key", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True)),
)

artifact_derived_from = Table(
    "artifact_derived_from",
    metadata,
    Column(
        "artifact_id",
        LargeBinary,
        ForeignKey("artifact.artifact_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("span_id", LargeBinary, ForeignKey("code_span.span_id"), nullable=False),
    Column("role", Text),
    PrimaryKeyConstraint("artifact_id", "span_id"),
)

snapshot_entry = Table(
    "snapshot_entry",
    metadata,
    Column("sha", Text, ForeignKey("commit_ref.sha"), nullable=False),
    Column("logical_key", Text, nullable=False),
    Column("artifact_id", LargeBinary, ForeignKey("artifact.artifact_id"), nullable=False),
    PrimaryKeyConstraint("sha", "logical_key"),
)
