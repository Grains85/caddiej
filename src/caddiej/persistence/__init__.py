"""Local history: what was hit, when, and with what.

One SQLite file the user owns and can copy, inspect, or delete. No server, no
ORM, no migration framework — the standard library and an ordered list of
migrations, which is the right amount of machinery for a single-user golf
simulator.

The layers are separate on purpose, and each is testable without the ones above:

* `schema` is the tables and how they move forward;
* `records` is how a shot becomes a row and comes back a shot;
* `repository` is every statement CaddieJ runs, and nothing else;
* `analytics` is pure arithmetic over records, with no database in sight;
* `database` confines SQLite to one thread;
* `service` is the only asynchronous part, and the only one that knows about
  events, queues, or failure.
"""

from caddiej.persistence.analytics import (
    ClubAggregate,
    DispersionSample,
    Metric,
    MetricSummary,
    SessionAnalytics,
    summarise_session,
)
from caddiej.persistence.database import Database
from caddiej.persistence.records import (
    AnalysisReference,
    RawPayload,
    SessionRecord,
    StoredAdvice,
    StoredShot,
)
from caddiej.persistence.repository import HistoryRepository
from caddiej.persistence.schema import SCHEMA_VERSION, SchemaTooNewError
from caddiej.persistence.service import (
    HistoryHealth,
    HistoryService,
    build_history_service,
)

__all__ = [
    "SCHEMA_VERSION",
    "AnalysisReference",
    "ClubAggregate",
    "Database",
    "DispersionSample",
    "HistoryHealth",
    "HistoryRepository",
    "HistoryService",
    "Metric",
    "MetricSummary",
    "RawPayload",
    "SchemaTooNewError",
    "SessionAnalytics",
    "SessionRecord",
    "StoredAdvice",
    "StoredShot",
    "build_history_service",
    "summarise_session",
]
