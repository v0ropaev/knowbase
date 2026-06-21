"""Cross-file questions for the knowledge-vs-RAG comparison (DESIGN.md §9, §10).

Two families, both spanning two files — the case RAG-over-chunks fumbles while a single grounded
knowbase artifact already covers both:
  * **API contracts** — `src/app/routes.py` (route/handler) + `src/app/schemas.py` (response model),
    from the Tier-1 FastAPI fixture (`FILES`).
  * **Domain entities** — `src/app/domain/order.py` (the `Order` entity) +
    `src/app/domain/line_item.py` (the `LineItem` it references), from `ENTITY_FILES` below.
Reused by the deterministic Tier-3 gate (PR-3a) and the nightly LLM A/B (PR-3b).
"""

from __future__ import annotations

from dataclasses import dataclass

ROUTES = "src/app/routes.py"
SCHEMAS = "src/app/schemas.py"
CROSS_FILE = frozenset({ROUTES, SCHEMAS})

ORDER_ENTITY = "src/app/domain/order.py"
LINE_ITEM_ENTITY = "src/app/domain/line_item.py"
ENTITY_CROSS_FILE = frozenset({ORDER_ENTITY, LINE_ITEM_ENTITY})

# A two-file entity fixture: Order references LineItem across files (the cross-file link).
ENTITY_FILES = {
    "src/app/domain/__init__.py": "",
    "src/app/domain/line_item.py": (
        "from dataclasses import dataclass\n\n\n"
        "@dataclass\n"
        "class LineItem:\n"
        "    sku: str\n"
        "    qty: int = 1\n"
    ),
    "src/app/domain/order.py": (
        "from dataclasses import dataclass\n"
        "from app.domain.line_item import LineItem\n\n\n"
        "@dataclass\n"
        "class Order:\n"
        "    id: int\n"
        "    items: list[LineItem]\n"
    ),
}


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
    # Domain-entity questions — answered by the cross-file-grounded `entity:...Order` artifact.
    Question("e1", "What does the Order entity contain, including its line items?",
             ENTITY_CROSS_FILE, frozenset({"entity:app.domain.order.Order"})),
    Question("e2", "What fields does the Order domain model have and what type are its items?",
             ENTITY_CROSS_FILE, frozenset({"entity:app.domain.order.Order"})),
    Question("e3", "Which model does the Order entity's items field reference, and where is it?",
             ENTITY_CROSS_FILE, frozenset({"entity:app.domain.order.Order"})),
]

# Hand-written reference answers (the gold oracle for the nightly LLM-judged A/B, PR-3b). Derived
# from the fixtures above (we own them), so accuracy is judged against ground truth — not against
# either arm's own retrieval (no knowbase-favoring bias). Keep in sync with FILES / ENTITY_FILES.
GOLD: dict[str, str] = {
    "q1": "A JSON array of OrderOut objects; each OrderOut has id (int) and total (float).",
    "q2": "The fields of OrderOut: id (int) and total (float).",
    "q3": "Accepts an OrderIn request body with a single field item (str); returns an OrderOut "
          "(id: int, total: float).",
    "q4": "OrderOut, which has id (int) and total (float).",
    "q5": "OrderOut (id: int, total: float).",
    "q6": "201.",
    "q7": "float.",
    "q8": "GET /api/orders (also POST and GET /api/orders/{order_id}) returns OrderOut; "
          "OrderOut is defined in src/app/schemas.py.",
    "e1": "Order (a dataclass) has id (int) and items (a list of LineItem); each LineItem has "
          "sku (str) and qty (int, default 1).",
    "e2": "Order has id (int) and items, where items is list[LineItem]; LineItem has sku (str) and "
          "qty (int).",
    "e3": "It references LineItem, defined in src/app/domain/line_item.py.",
}
