"""Cross-file-contract questions for the knowledge-vs-RAG comparison (DESIGN.md §9, §10).

Every expected answer spans `src/app/routes.py` (the route/handler) AND `src/app/schemas.py` (the
pydantic model) of the Tier-1 FastAPI fixture — the case RAG-over-chunks fumbles. Reused by the
deterministic gate (PR-3a) and the nightly LLM A/B (PR-3b).
"""

from __future__ import annotations

from dataclasses import dataclass

ROUTES = "src/app/routes.py"
SCHEMAS = "src/app/schemas.py"
CROSS_FILE = frozenset({ROUTES, SCHEMAS})


@dataclass(frozen=True)
class Question:
    id: str
    question: str
    expected_files: frozenset[str]
    expected_logical_keys: frozenset[str]


QUESTIONS: list[Question] = [
    Question("q1", "What does the response of GET /api/orders look like?",
             CROSS_FILE, frozenset({"api:GET /api/orders"})),
    Question("q2", "What fields are returned when listing orders?",
             CROSS_FILE, frozenset({"api:GET /api/orders"})),
    Question("q3", "What request body does POST /api/orders accept and what does it return?",
             CROSS_FILE, frozenset({"api:POST /api/orders"})),
    Question("q4", "Which schema is returned when getting a single order by id?",
             CROSS_FILE, frozenset({"api:GET /api/orders/{order_id}"})),
    Question("q5", "What is the response model for creating an order?",
             CROSS_FILE, frozenset({"api:POST /api/orders"})),
    Question("q6", "What HTTP status code does creating an order return?",
             CROSS_FILE, frozenset({"api:POST /api/orders"})),
    Question("q7", "What is the type of the total field on an order response?",
             CROSS_FILE, frozenset({"api:GET /api/orders"})),
    Question("q8", "Which endpoint returns OrderOut and where is that model defined?",
             CROSS_FILE, frozenset({"api:GET /api/orders"})),
]
