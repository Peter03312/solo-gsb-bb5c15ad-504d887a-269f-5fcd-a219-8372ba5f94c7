"""FastAPI application: temporary-event radio spectrum requests.

Endpoints
---------
POST   /api/applications                          submit (whole-request validation)
GET    /api/applications                          list submissions
GET    /api/applications/{id}                    fetch one submission
POST   /api/applications/{id}/preview            conflicts + current revision
POST   /api/applications/{id}/approve            conditional approval (expected rev.)
POST   /api/applications/batch-approve           conditional whole-group approval
POST   /api/applications/{id}/revoke             revoke (changes future effective set)
POST   /api/applications/{id}/amendments         amend an effective license's scope
GET    /api/revision/current                     current revision + effective set
GET    /api/revision/{revision}                  as-of snapshot (approvals/revocations)
GET    /health                                   liveness probe

Concurrency
-----------
Every mutation (single approval, group approval, revocation and scope
amendment) locks the singleton ``revision_meta`` row (``SELECT ... FOR
UPDATE``) for the duration of the transaction.  The four operations
therefore share one revision adjudication and serialise: a group approval
either commits all its approval events at once with consecutive revisions or
commits nothing, and a loser transaction is refused with ``stale_revision``
/ ``interference`` (or the group-level ``batch_rejected`` envelope carrying
every failure category).

Amendments
----------
An amendment replaces the *effective scope* (circle/band/window) of one
already approved, not revoked license at one new revision; the license id
and approved status never change.  Under the lock the license's old scope is
excluded from the effective set and the new scope is adjudicated against the
remaining effective licenses with the ordinary rules.  The append-only
``amendment_events`` table keeps every scope (plus the previous scope), so
an as-of query at an old revision still renders the parameters in force then.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from . import adjudication
from .adjudication import Circle, Conflict, License
from .config import settings
from .db import SessionLocal, engine, init_db, wait_for_database
from .errors import APIError, api_error_handler
from .models import (
    AmendmentEvent,
    Application,
    ApprovalEvent,
    RevisionMeta,
    RevocationEvent,
)
from .schemas import (
    AmendmentRequest,
    ApplicationCreate,
    ApprovalRequest,
    BatchApprovalRequest,
    RevocationRequest,
)
from .semantic_validation import (
    validate_amendment,
    validate_application,
    validate_batch_application_ids,
)

log = logging.getLogger("radio.api")

# Error codes
E_VALIDATION = "validation_failed"
E_NOT_FOUND = "not_found"
E_NOT_PENDING = "not_pending"
E_ALREADY_REVOKED = "already_revoked"
E_REVISION_OUT_OF_RANGE = "revision_out_of_range"
E_STALE_REVISION = "stale_revision"
E_INTERFERENCE = "interference"
# Whole-group refusal; details carries per-failure-category application lists.
E_BATCH_REJECTED = "batch_rejected"


# --------------------------------------------------------------------------- #
# Serialisation helpers
# --------------------------------------------------------------------------- #
def _utc(value: datetime) -> datetime:
    """Coerce a possibly naive timestamp (e.g. under SQLite) to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def serialize_application(
    app: Application,
    latest_amendment: AmendmentEvent | None = None,
) -> dict[str, Any]:
    scope = latest_amendment if latest_amendment is not None else app
    return {
        "id": app.id,
        "name": app.name,
        "circle": {"cx": scope.cx, "cy": scope.cy, "radius": scope.radius},
        "frequency": {"low": scope.freq_low, "high": scope.freq_high},
        "time_window": {
            "start": iso(scope.start_time),
            "end": iso(scope.end_time),
        },
        "status": app.status,
        "approval_revision": app.approval_revision,
        # Revision at which the current scope was last replaced (null when
        # the effective scope is still the original approved scope).
        "amendment_revision": (
            latest_amendment.revision if latest_amendment is not None else None
        ),
        "created_at": iso(app.created_at),
    }


def serialize_conflict(conflict: Conflict) -> dict[str, Any]:
    """One conflicting effective grant with the trigger conditions."""
    other = conflict.other
    return {
        "application": {
            "id": other.application_id,
            "circle": {
                "cx": other.circle.cx,
                "cy": other.circle.cy,
                "radius": other.circle.radius,
            },
            "frequency": {"low": other.freq_low, "high": other.freq_high},
            "time_window": {
                "start": iso(other.start_time),
                "end": iso(other.end_time),
            },
        },
        "trigger": {
            "time_intersect": conflict.time_overlap,
            "area_intersect": conflict.area_overlap,
            "frequency_intersect": conflict.frequency_intersects,
            "minimum_frequency_gap": conflict.minimum_frequency_gap,
            "within_guard_band": (
                conflict.minimum_frequency_gap < settings.guard_band
            ),
        },
        "reason": conflict.reason,
    }


def serialize_batch_conflict(
    conflict: Conflict, scope: str, peer_index: int | None = None
) -> dict[str, Any]:
    """A conflict found during a group adjudication.

    ``scope`` identifies the other side: ``"effective"`` for a currently
    effective license, ``"batch"`` for an earlier application in the same
    request (whose 0-based input position is ``peer_index``).
    """
    result = serialize_conflict(conflict)
    result["scope"] = scope
    if scope == "batch":
        result["batch_index"] = peer_index
    return result


def to_license(app: Application) -> License:
    return to_license_from(app)


def to_license_from(row, application_id: int | None = None) -> License:
    """Build an adjudicator license from a scope-bearing row.

    Accepts an ``Application`` (scope columns are the original parameters,
    id column is ``id``) or an ``AmendmentEvent`` (scope columns are the
    replacement parameters, id column is ``application_id``).
    """
    if application_id is None:
        application_id = getattr(row, "application_id", None) or row.id
    return License(
        application_id=application_id,
        circle=Circle(row.cx, row.cy, row.radius),
        freq_low=row.freq_low,
        freq_high=row.freq_high,
        start_time=_utc(row.start_time),
        end_time=_utc(row.end_time),
    )


def load_latest_amendments(
    session, at_revision: int | None = None
) -> dict[int, AmendmentEvent]:
    """Latest amendment event *at* ``at_revision`` per application.

    When ``at_revision`` is None every amendment is eligible (current
    world); otherwise only amendments with revision <= ``at_revision`` count,
    which is how as-of snapshots restore the scope in force at the time.
    Applications that were never amended are simply absent from the map.
    """
    query = session.query(AmendmentEvent)
    if at_revision is not None:
        query = query.filter(AmendmentEvent.revision <= at_revision)
    latest: dict[int, AmendmentEvent] = {}
    for ev in query.order_by(AmendmentEvent.revision).all():
        latest[ev.application_id] = ev
    return latest


def load_effective_licenses(
    session,
    at_revision: int | None = None,
    amendments: dict[int, AmendmentEvent] | None = None,
) -> list[License]:
    """Approved and not (yet) revoked = the effective set for adjudication.

    Each license carries the scope in force at ``at_revision``: its latest
    amendment not later than that revision, or the original parameters when
    it was never amended by then.
    """
    rows = (
        session.query(Application)
        .filter(Application.status == "approved")
        .order_by(Application.id)
        .all()
    )
    if amendments is None:
        amendments = load_latest_amendments(session, at_revision)
    licenses = []
    for row in rows:
        scope = amendments.get(row.id, row)
        licenses.append(to_license_from(scope, application_id=row.id))
    return licenses


def serialize_amendment(ev: AmendmentEvent) -> dict[str, Any]:
    """One scope-replacement event with the new and the previous scope."""
    return {
        "revision": ev.revision,
        "application_id": ev.application_id,
        "scope": {
            "circle": {
                "cx": ev.cx,
                "cy": ev.cy,
                "radius": ev.radius,
            },
            "frequency": {"low": ev.freq_low, "high": ev.freq_high},
            "time_window": {
                "start": iso(ev.start_time),
                "end": iso(ev.end_time),
            },
        },
        "previous_scope": {
            "circle": {
                "cx": ev.previous_cx,
                "cy": ev.previous_cy,
                "radius": ev.previous_radius,
            },
            "frequency": {
                "low": ev.previous_freq_low,
                "high": ev.previous_freq_high,
            },
            "time_window": {
                "start": iso(ev.previous_start_time),
                "end": iso(ev.previous_end_time),
            },
        },
        "reason": ev.reason,
        "created_at": iso(ev.created_at),
    }


# --------------------------------------------------------------------------- #
# Revision snapshots
# --------------------------------------------------------------------------- #
def build_snapshot(session, revision: int) -> dict[str, Any]:
    """Reconstruct the state of the world *at* ``revision``.

    Returns every approval event with revision <= the target and the
    application snapshot, flags those later revoked by the target revision,
    and includes all revocation and amendment events of the period.  The
    application embedded in every approval record is rendered with the scope
    in force at the target revision (its latest amendment <= the revision),
    so querying an old revision still shows the parameters effective then.
    """
    approvals = (
        session.query(ApprovalEvent)
        .filter(ApprovalEvent.revision <= revision)
        .order_by(ApprovalEvent.revision)
        .all()
    )
    revocations = (
        session.query(RevocationEvent)
        .filter(RevocationEvent.revision <= revision)
        .order_by(RevocationEvent.revision)
        .all()
    )
    amendments_by_app = load_latest_amendments(session, at_revision=revision)
    amendments = (
        session.query(AmendmentEvent)
        .filter(AmendmentEvent.revision <= revision)
        .order_by(AmendmentEvent.revision)
        .all()
    )
    revoked_at_or_before = {ev.application_id: ev for ev in revocations}

    approval_records: list[dict[str, Any]] = []
    effective_ids: list[int] = []
    for ev in approvals:
        app = session.get(Application, ev.application_id)
        rev = revoked_at_or_before.get(ev.application_id)
        was_revoked_by_then = rev is not None and rev.revision <= revision
        record = {
            "revision": ev.revision,
            "application_id": ev.application_id,
            "guard_band": ev.guard_band,
            "created_at": iso(ev.created_at),
            # Status of this grant *as of the queried revision*.
            "status_as_of": "revoked" if was_revoked_by_then else "approved",
            "application": (
                serialize_application(
                    app, amendments_by_app.get(ev.application_id)
                )
                if app
                else None
            ),
        }
        if was_revoked_by_then:
            record["revoked_at_revision"] = rev.revision
        else:
            effective_ids.append(ev.application_id)
        approval_records.append(record)

    revocation_records = [
        {
            "revision": ev.revision,
            "application_id": ev.application_id,
            "reason": ev.reason,
            "created_at": iso(ev.created_at),
        }
        for ev in revocations
    ]

    return {
        "revision": revision,
        "effective_application_ids": effective_ids,
        "approvals": approval_records,
        "revocations": revocation_records,
        "amendments": [serialize_amendment(ev) for ev in amendments],
    }


# --------------------------------------------------------------------------- #
# Application factory / lifespan
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    wait_for_database()
    init_db()
    log.info(
        "startup complete; guard_band=%s listening on %s:%s",
        settings.guard_band,
        settings.app_host,
        settings.app_port,
    )
    yield
    engine.dispose()


app = FastAPI(
    title="Temporary Event Radio Spectrum API",
    version="1.1.0",
    description=(
        "Submit, preview, approve and revoke temporary radio applications. "
        "Frequencies are closed intervals, UTC times are half-open intervals, "
        "coverage is a planar circle."
    ),
    lifespan=lifespan,
)
app.add_exception_handler(APIError, api_error_handler)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Normalise FastAPI structural errors into the standard error envelope."""
    details = [
        {
            "field": ".".join(str(p) for p in err["loc"] if p != "body"),
            "code": err["type"],
            "message": err["msg"],
        }
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": E_VALIDATION,
                "message": "request validation failed; the whole request was rejected",
                "details": details,
            }
        },
    )


# --------------------------------------------------------------------------- #
# Health / revisions
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/revision/current", tags=["revision"])
def current_revision() -> dict[str, Any]:
    with SessionLocal() as session:
        meta = session.get(RevisionMeta, 1)
        revision = meta.revision if meta else 0
        snapshot = build_snapshot(session, revision)
        amendments = load_latest_amendments(session, revision)
        effective_applications = [
            serialize_application(app, amendments.get(app_id))
            for app_id in snapshot["effective_application_ids"]
            if (app := session.get(Application, app_id)) is not None
        ]
    return {
        "current_revision": revision,
        "guard_band": settings.guard_band,
        "effective_application_ids": snapshot["effective_application_ids"],
        "effective_applications": effective_applications,
        "approvals": snapshot["approvals"],
        "revocations": snapshot["revocations"],
        "amendments": snapshot["amendments"],
    }


@app.get("/api/revision/{revision}", tags=["revision"])
def revision_snapshot(revision: int) -> dict[str, Any]:
    if revision < 0:
        raise APIError(
            E_REVISION_OUT_OF_RANGE,
            "revision must be >= 0",
            status_code=422,
        )
    with SessionLocal() as session:
        current = session.get(RevisionMeta, 1).revision
        if revision > current:
            raise APIError(
                E_REVISION_OUT_OF_RANGE,
                f"revision {revision} is in the future; current revision is {current}",
                status_code=404,
                details={"current_revision": current},
            )
        return build_snapshot(session, revision)


# --------------------------------------------------------------------------- #
# Applications: create / list / get
# --------------------------------------------------------------------------- #
@app.post("/api/applications", status_code=201, tags=["applications"])
def create_application(payload: ApplicationCreate) -> dict[str, Any]:
    errors = validate_application(payload)
    if errors:
        raise APIError(
            E_VALIDATION,
            "the whole application was rejected due to invalid fields",
            status_code=422,
            details=errors,
        )

    app_row = Application(
        name=payload.name,
        cx=payload.circle.cx,
        cy=payload.circle.cy,
        radius=payload.circle.radius,
        freq_low=payload.frequency.low,
        freq_high=payload.frequency.high,
        start_time=payload.time_window.start.astimezone(timezone.utc),
        end_time=payload.time_window.end.astimezone(timezone.utc),
        status="pending",
    )
    with SessionLocal() as session, session.begin():
        session.add(app_row)
        session.flush()
        result = serialize_application(app_row)
    return result


@app.get("/api/applications", tags=["applications"])
def list_applications(status: str | None = None) -> dict[str, Any]:
    with SessionLocal() as session:
        query = session.query(Application).order_by(Application.id)
        if status is not None:
            if status not in {"pending", "approved", "revoked"}:
                raise APIError(
                    E_VALIDATION,
                    "status filter must be one of pending, approved, revoked",
                    status_code=422,
                )
            query = query.filter(Application.status == status)
        rows = query.all()
        # Render every effective license with its current (possibly amended)
        # scope; pending applications never have amendment events.
        amendments = load_latest_amendments(session)
        return {
            "applications": [
                serialize_application(row, amendments.get(row.id)) for row in rows
            ]
        }


@app.get("/api/applications/{application_id}", tags=["applications"])
def get_application(application_id: int) -> dict[str, Any]:
    with SessionLocal() as session:
        row = session.get(Application, application_id)
        if row is None:
            raise APIError(
                E_NOT_FOUND,
                f"application {application_id} does not exist",
                status_code=404,
            )
        amendments = load_latest_amendments(session)
        return serialize_application(row, amendments.get(row.id))


# --------------------------------------------------------------------------- #
# Preview (non-mutating)
# --------------------------------------------------------------------------- #
@app.post("/api/applications/{application_id}/preview", tags=["applications"])
def preview_application(application_id: int) -> dict[str, Any]:
    with SessionLocal() as session:
        row = session.get(Application, application_id)
        if row is None:
            raise APIError(
                E_NOT_FOUND,
                f"application {application_id} does not exist",
                status_code=404,
            )
        if row.status != "pending":
            raise APIError(
                E_NOT_PENDING,
                f"application {application_id} is {row.status}, only pending "
                "applications can be previewed",
                status_code=409,
                details={"status": row.status},
            )

        current = session.get(RevisionMeta, 1).revision
        effective = load_effective_licenses(session, current)
        conflicts = adjudication.evaluate(to_license(row), effective, settings.guard_band)

    return {
        "application_id": application_id,
        "current_revision": current,
        "guard_band": settings.guard_band,
        "has_conflicts": bool(conflicts),
        "conflicts": [serialize_conflict(c) for c in conflicts],
    }


# --------------------------------------------------------------------------- #
# Approve (conditional on expected revision, row-lock serialised)
# --------------------------------------------------------------------------- #
@app.post("/api/applications/{application_id}/approve", tags=["applications"])
def approve_application(
    application_id: int, payload: ApprovalRequest
) -> dict[str, Any]:
    with SessionLocal() as session:
        try:
            with session.begin():
                # Serialise against every other approve/revoke on the
                # singleton row (a real row lock on PostgreSQL).
                meta = session.get(RevisionMeta, 1, with_for_update=True)
                current = meta.revision

                row = session.get(Application, application_id)
                if row is None:
                    raise APIError(
                        E_NOT_FOUND,
                        f"application {application_id} does not exist",
                        status_code=404,
                    )
                if row.status == "revoked":
                    raise APIError(
                        E_ALREADY_REVOKED,
                        f"application {application_id} has been revoked",
                        status_code=409,
                    )
                if row.status == "approved":
                    raise APIError(
                        E_NOT_PENDING,
                        f"application {application_id} is already approved at "
                        f"revision {row.approval_revision}",
                        status_code=409,
                        details={"approval_revision": row.approval_revision},
                    )

                # Optimistic revision check against the locked, latest revision.
                if payload.expected_revision != current:
                    raise APIError(
                        E_STALE_REVISION,
                        f"expected revision {payload.expected_revision} but "
                        f"current revision is {current}; re-preview and re-decide",
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                        },
                    )

                # Re-adjudicate under the lock against the latest effective set.
                effective = load_effective_licenses(session, current)
                conflicts = adjudication.evaluate(
                    to_license(row), effective, settings.guard_band
                )
                if conflicts:
                    raise APIError(
                        E_INTERFERENCE,
                        f"application {application_id} interferes with "
                        f"{len(conflicts)} effective grant(s); frequency not granted",
                        status_code=409,
                        details={
                            "current_revision": current,
                            "conflicts": [
                                serialize_conflict(c) for c in conflicts
                            ],
                        },
                    )

                meta.revision = current + 1
                session.add(
                    ApprovalEvent(
                        revision=meta.revision,
                        application_id=application_id,
                        guard_band=settings.guard_band,
                    )
                )
                row.status = "approved"
                row.approval_revision = meta.revision
                session.flush()
                response = {
                    "approved": True,
                    "application_id": application_id,
                    "revision": meta.revision,
                    "guard_band": settings.guard_band,
                    "application": serialize_application(row),
                }
        except IntegrityError:
            # Last line of defence (e.g. backends without row locks such as
            # SQLite's tests): the revision slot was taken concurrently.
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); "
                "re-preview and re-decide",
                status_code=409,
                details={"current_revision": latest},
            )
    return response


# --------------------------------------------------------------------------- #
# Group approval (all-or-nothing, consecutive revisions, one shared lock)
# --------------------------------------------------------------------------- #
@app.post("/api/applications/batch-approve", tags=["applications"])
def approve_applications_batch(
    payload: BatchApprovalRequest,
) -> dict[str, Any]:
    semantic_errors = validate_batch_application_ids(payload.application_ids)
    if semantic_errors:
        raise APIError(
            E_VALIDATION,
            "the whole batch request was rejected due to invalid application_ids",
            status_code=422,
            details=semantic_errors,
        )

    with SessionLocal() as session:
        try:
            with session.begin():
                # Same singleton lock as single approve/revoke: all mutators
                # share one revision adjudication and fully serialise.
                meta = session.get(RevisionMeta, 1, with_for_update=True)
                current = meta.revision

                # Load every referenced application in input order. Unknown
                # ids, wrong statuses and conflicts are all collected so the
                # operator gets the complete failure picture in one response.
                rows: list[Application | None] = [
                    session.get(Application, app_id)
                    for app_id in payload.application_ids
                ]

                missing: list[dict[str, Any]] = []
                not_pending: list[dict[str, Any]] = []
                interference: list[dict[str, Any]] = []

                for index, (app_id, row) in enumerate(
                    zip(payload.application_ids, rows)
                ):
                    if row is None:
                        missing.append(
                            {"application_id": app_id, "batch_index": index}
                        )
                        continue
                    if row.status != "pending":
                        entry: dict[str, Any] = {
                            "application_id": app_id,
                            "batch_index": index,
                            "status": row.status,
                        }
                        if row.approval_revision is not None:
                            entry["approval_revision"] = row.approval_revision
                        not_pending.append(entry)

                # Adjudicate in input order: every still-pending candidate is
                # checked against (a) the effective set and (b) ALL earlier
                # members of THIS batch (an earlier member that itself fails
                # still participates, so the operator sees every pairwise
                # conflict). Nothing here is committed when the group fails.
                granted_licenses: list[License] = []
                effective = load_effective_licenses(session, current)
                id_to_index = {
                    app_id: i for i, app_id in enumerate(payload.application_ids)
                }
                for index, row in enumerate(rows):
                    if row is None or row.status != "pending":
                        continue
                    candidate = to_license(row)

                    effective_conflicts = adjudication.evaluate(
                        candidate, effective, settings.guard_band
                    )
                    peer_conflicts = adjudication.evaluate(
                        candidate, granted_licenses, settings.guard_band
                    )

                    if effective_conflicts or peer_conflicts:
                        interference.append(
                            {
                                "application_id": row.id,
                                "batch_index": index,
                                "conflicts": [
                                    serialize_batch_conflict(c, "effective")
                                    for c in effective_conflicts
                                ]
                                + [
                                    serialize_batch_conflict(
                                        c,
                                        "batch",
                                        peer_index=id_to_index[
                                            c.other.application_id
                                        ],
                                    )
                                    for c in peer_conflicts
                                ],
                            }
                        )
                    # Earlier members participate in later checks even when
                    # they themselves conflict (the group is rejected anyway;
                    # the report stays pairwise complete).
                    granted_licenses.append(candidate)

                stale = payload.expected_revision != current
                if missing or not_pending or interference or stale:
                    details: dict[str, Any] = {
                        "expected_revision": payload.expected_revision,
                        "current_revision": current,
                        "revised": False,
                    }
                    categories: list[str] = []
                    if stale:
                        details["stale_revision"] = {
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                        }
                        categories.append("stale_revision")
                    if missing:
                        details["applications_not_found"] = missing
                        categories.append("not_found")
                    if not_pending:
                        details["applications_not_pending"] = not_pending
                        categories.append("not_pending")
                    if interference:
                        details["interference"] = interference
                        categories.append("interference")

                    raise APIError(
                        E_BATCH_REJECTED,
                        "the whole batch was not approved "
                        f"({', '.join(categories)}); no revision was consumed",
                        status_code=409,
                        details=details,
                    )

                # All checks passed: commit the group atomically, writing one
                # approval event per item with consecutive revisions in the
                # exact input order.
                results: list[dict[str, Any]] = []
                for row in rows:
                    assert row is not None  # missing ids were rejected above
                    meta.revision = current + 1 + len(results)
                    session.add(
                        ApprovalEvent(
                            revision=meta.revision,
                            application_id=row.id,
                            guard_band=settings.guard_band,
                        )
                    )
                    row.status = "approved"
                    row.approval_revision = meta.revision
                    results.append(
                        {
                            "application_id": row.id,
                            "revision": meta.revision,
                            "application": serialize_application(row),
                        }
                    )
                session.flush()
                response = {
                    "approved": True,
                    "expected_revision": payload.expected_revision,
                    "from_revision": current,
                    "revision": meta.revision,
                    "revisions": [item["revision"] for item in results],
                    "count": len(results),
                    "guard_band": settings.guard_band,
                    "results": results,
                }
        except IntegrityError:
            # Last line of defence (e.g. backends without row locks such as
            # SQLite's tests): a revision slot was taken concurrently.
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); "
                "re-preview and re-decide the batch",
                status_code=409,
                details={"current_revision": latest},
            )
    return response


# --------------------------------------------------------------------------- #
# Revoke (only affects the effective set used by later adjudications)
# --------------------------------------------------------------------------- #
@app.post("/api/applications/{application_id}/revoke", tags=["applications"])
def revoke_application(
    application_id: int, payload: RevocationRequest
) -> dict[str, Any]:
    with SessionLocal() as session:
        try:
            with session.begin():
                meta = session.get(RevisionMeta, 1, with_for_update=True)

                row = session.get(Application, application_id)
                if row is None:
                    raise APIError(
                        E_NOT_FOUND,
                        f"application {application_id} does not exist",
                        status_code=404,
                    )
                if row.status == "revoked":
                    raise APIError(
                        E_ALREADY_REVOKED,
                        f"application {application_id} is already revoked",
                        status_code=409,
                    )
                if row.status != "approved":
                    raise APIError(
                        E_NOT_PENDING,
                        f"application {application_id} is pending and cannot "
                        "be revoked",
                        status_code=409,
                        details={"status": row.status},
                    )

                meta.revision += 1
                session.add(
                    RevocationEvent(
                        revision=meta.revision,
                        application_id=application_id,
                        reason=payload.reason,
                    )
                )
                row.status = "revoked"
                session.flush()
                response = {
                    "revoked": True,
                    "application_id": application_id,
                    "revision": meta.revision,
                    "previous_approval_revision": row.approval_revision,
                    "reason": payload.reason,
                }
        except IntegrityError:
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); retry",
                status_code=409,
                details={"current_revision": latest},
            )
    return response


# --------------------------------------------------------------------------- #
# Amend (atomically replace an effective license's scope with a new revision)
# --------------------------------------------------------------------------- #
@app.post("/api/applications/{application_id}/amendments", tags=["applications"])
def amend_application(
    application_id: int, payload: AmendmentRequest
) -> dict[str, Any]:
    """Replace the effective scope of an approved-and-not-revoked license.

    Rules (mirroring single approval):

    * the whole new scope is semantically validated exactly like a new
      application (``validation_failed`` 422 listing every violation);
    * the license must exist (404 ``not_found``), be approved and still
      effective (409 ``not_pending`` when pending, ``already_revoked`` when
      revoked);
    * ``expected_revision`` must equal the current revision (409
      ``stale_revision``), all under the shared row lock;
    * inside the lock the license's *old* scope is removed from the
      effective set and the *new* scope is adjudicated against every other
      effective license with the ordinary interference rules (409
      ``interference``);
    * on success exactly one revision is consumed, one ``AmendmentEvent`` is
      appended (new + previous scope), and the id/approved status are
      untouched.  Old revisions still reconstruct with the old parameters.
    """
    errors = validate_amendment(payload)
    if errors:
        raise APIError(
            E_VALIDATION,
            "the whole amendment was rejected due to invalid fields",
            status_code=422,
            details=errors,
        )

    new_start = payload.time_window.start.astimezone(timezone.utc)
    new_end = payload.time_window.end.astimezone(timezone.utc)

    with SessionLocal() as session:
        try:
            with session.begin():
                # Same singleton lock as approve/batch/revoke.
                meta = session.get(RevisionMeta, 1, with_for_update=True)
                current = meta.revision

                row = session.get(Application, application_id)
                if row is None:
                    raise APIError(
                        E_NOT_FOUND,
                        f"application {application_id} does not exist",
                        status_code=404,
                    )
                if row.status == "revoked":
                    raise APIError(
                        E_ALREADY_REVOKED,
                        f"application {application_id} has been revoked and "
                        "cannot be amended",
                        status_code=409,
                    )
                if row.status != "approved":
                    raise APIError(
                        E_NOT_PENDING,
                        f"application {application_id} is {row.status}; only "
                        "approved, effective licenses can be amended",
                        status_code=409,
                        details={"status": row.status},
                    )

                if payload.expected_revision != current:
                    raise APIError(
                        E_STALE_REVISION,
                        f"expected revision {payload.expected_revision} but "
                        f"current revision is {current}; re-check and retry "
                        "the amendment",
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                        },
                    )

                amendments = load_latest_amendments(session, current)
                previous = amendments.get(application_id, row)

                # Exclude this license's OWN old scope, then adjudicate the
                # new scope against every remaining effective license.
                other_licenses = [
                    lic
                    for lic in load_effective_licenses(
                        session, current, amendments
                    )
                    if lic.application_id != application_id
                ]

                candidate = License(
                    application_id=application_id,
                    circle=Circle(
                        payload.circle.cx,
                        payload.circle.cy,
                        payload.circle.radius,
                    ),
                    freq_low=payload.frequency.low,
                    freq_high=payload.frequency.high,
                    start_time=new_start,
                    end_time=new_end,
                )
                conflicts = adjudication.evaluate(
                    candidate, other_licenses, settings.guard_band
                )
                if conflicts:
                    raise APIError(
                        E_INTERFERENCE,
                        f"the amended scope of application {application_id} "
                        f"interferes with {len(conflicts)} effective "
                        "grant(s); the old scope stays in effect",
                        status_code=409,
                        details={
                            "current_revision": current,
                            "revised": False,
                            "conflicts": [
                                serialize_conflict(c) for c in conflicts
                            ],
                        },
                    )

                # Atomic scope replacement: one append-only event + one
                # revision. The applications row keeps its id and status; the
                # new scope is served by the amendment event from now on.
                meta.revision = current + 1
                event = AmendmentEvent(
                    revision=meta.revision,
                    application_id=application_id,
                    cx=payload.circle.cx,
                    cy=payload.circle.cy,
                    radius=payload.circle.radius,
                    freq_low=payload.frequency.low,
                    freq_high=payload.frequency.high,
                    start_time=new_start,
                    end_time=new_end,
                    previous_cx=previous.cx,
                    previous_cy=previous.cy,
                    previous_radius=previous.radius,
                    previous_freq_low=previous.freq_low,
                    previous_freq_high=previous.freq_high,
                    previous_start_time=_utc(previous.start_time),
                    previous_end_time=_utc(previous.end_time),
                    reason=payload.reason,
                )
                session.add(event)
                session.flush()
                response = {
                    "amended": True,
                    "application_id": application_id,
                    "expected_revision": payload.expected_revision,
                    "revision": meta.revision,
                    "guard_band": settings.guard_band,
                    "scope": {
                        "circle": {
                            "cx": event.cx,
                            "cy": event.cy,
                            "radius": event.radius,
                        },
                        "frequency": {
                            "low": event.freq_low,
                            "high": event.freq_high,
                        },
                        "time_window": {
                            "start": iso(event.start_time),
                            "end": iso(event.end_time),
                        },
                    },
                    "previous_scope": {
                        "circle": {
                            "cx": event.previous_cx,
                            "cy": event.previous_cy,
                            "radius": event.previous_radius,
                        },
                        "frequency": {
                            "low": event.previous_freq_low,
                            "high": event.previous_freq_high,
                        },
                        "time_window": {
                            "start": iso(event.previous_start_time),
                            "end": iso(event.previous_end_time),
                        },
                    },
                    "reason": payload.reason,
                    "application": serialize_application(row, event),
                }
        except IntegrityError:
            # Last line of defence (e.g. backends without row locks such as
            # SQLite's tests): the revision slot was taken concurrently.
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); "
                "re-check and retry the amendment",
                status_code=409,
                details={"current_revision": latest},
            )
    return response
