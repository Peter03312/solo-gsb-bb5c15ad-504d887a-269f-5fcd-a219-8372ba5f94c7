"""Database engine, startup readiness wait and schema initialisation."""
from __future__ import annotations

import logging
import time

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from .config import settings

log = logging.getLogger("radio.db")

engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)

# Scope-snapshot columns added to approval_events together with the
# adjustment feature.  Existing databases (created before adjustments) are
# brought forward idempotently at startup: missing columns are added and
# back-filled from the immutable application parameters as approved.
_APPROVAL_SCOPE_COLUMNS: dict[str, str] = {
    "cx": "FLOAT",
    "cy": "FLOAT",
    "radius": "FLOAT",
    "freq_low": "FLOAT",
    "freq_high": "FLOAT",
    "start_time": "TIMESTAMP",
    "end_time": "TIMESTAMP",
}


def _upgrade_schema() -> None:
    """Idempotent lightweight migration for pre-adjustment databases.

    Adds the approval scope-snapshot columns when absent and back-fills them
    from ``applications``.  An application's scope only changes through an
    adjustment (which is only possible after this migration), so every
    historical approval's scope is exactly the current row parameters at the
    moment of the upgrade.
    """
    inspector = inspect(engine)
    table_names = inspector.get_table_names()
    if "approval_events" not in table_names:
        # create_all already built the table (with every column) or there is
        # nothing to upgrade yet.
        return
    existing = {col["name"] for col in inspector.get_columns("approval_events")}
    missing = [name for name in _APPROVAL_SCOPE_COLUMNS if name not in existing]
    if not missing:
        return
    with engine.begin() as conn:
        for name in missing:
            conn.execute(
                text(
                    f"ALTER TABLE approval_events ADD COLUMN {name} "
                    f"{_APPROVAL_SCOPE_COLUMNS[name]}"
                )
            )
        conn.execute(
            text(
                """
                UPDATE approval_events
                   SET cx = a.cx,
                       cy = a.cy,
                       radius = a.radius,
                       freq_low = a.freq_low,
                       freq_high = a.freq_high,
                       start_time = a.start_time,
                       end_time = a.end_time
                  FROM applications a
                 WHERE a.id = approval_events.application_id
                """
            )
        )
    log.info("back-filled approval scope snapshots into %d new column(s)", len(missing))


def wait_for_database(timeout_seconds: float = 60.0) -> None:
    """Block until the database accepts connections (Compose startup race)."""
    deadline = time.monotonic() + timeout_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            log.info("database is ready after %d attempt(s)", attempt)
            return
        except OperationalError:
            if time.monotonic() >= deadline:
                raise
            log.info("waiting for database (attempt %d) ...", attempt)
            time.sleep(1.0)


def init_db() -> None:
    """Create tables, upgrade older schemas and seed revision 0."""
    # Imported here so models register on Base.metadata before create_all().
    from . import models  # noqa: F401
    from .models import Base, RevisionMeta

    Base.metadata.create_all(bind=engine)
    _upgrade_schema()
    with SessionLocal() as session, session.begin():
        if session.get(RevisionMeta, 1) is None:
            session.add(RevisionMeta(id=1, revision=0))
