"""SQLAlchemy ORM models.

History is event-sourced:

* ``applications``     - every submitted request, rejected submissions are
                         never persisted (see whole-request validation);
* ``approval_events``  - append-only approval record, one per revision;
* ``revocation_events``- append-only revocation record, one per revision;
* ``amendment_events`` - append-only scope-replacement record for an
                         effective (approved, not revoked) license, one per
                         revision, holding the complete new scope and the
                         previous effective scope;
* ``revision_meta``    - singleton row holding the current revision number.

A revocation never deletes history: it only removes an application from the
effective set used by later adjudications.  An amendment never changes the
license id or approval status: it atomically replaces the effective scope at
a new revision, while every earlier revision still reconstructs with the
parameters in force then.  Snapshots for any past revision are reconstructed
from the three event tables.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Coverage circle in planar coordinates.
    cx: Mapped[float] = mapped_column(Float, nullable=False)
    cy: Mapped[float] = mapped_column(Float, nullable=False)
    radius: Mapped[float] = mapped_column(Float, nullable=False)

    # Closed frequency interval [freq_low, freq_high].
    freq_low: Mapped[float] = mapped_column(Float, nullable=False)
    freq_high: Mapped[float] = mapped_column(Float, nullable=False)

    # UTC half-open interval [start_time, end_time).
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    end_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # pending -> approved -> revoked (irreversible transitions)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    approval_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class ApprovalEvent(Base):
    __tablename__ = "approval_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), nullable=False
    )
    # Snapshot of the guard band in force when the decision was made.
    guard_band: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (Index("ix_approval_events_application", "application_id"),)


class RevocationEvent(Base):
    __tablename__ = "revocation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (Index("ix_revocation_events_application", "application_id"),)


class AmendmentEvent(Base):
    """Atomic replacement of an effective license scope at one revision.

    Stores the complete *new* scope (coverage circle, closed frequency band,
    half-open UTC time window) as well as a snapshot of the *previous* scope
    in force immediately before this revision, so as-of queries can render
    the parameters at any time.  The application id and approved status are
    untouched; an amendment only consumes one new revision.
    """

    __tablename__ = "amendment_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), nullable=False
    )
    # New effective scope.
    cx: Mapped[float] = mapped_column(Float, nullable=False)
    cy: Mapped[float] = mapped_column(Float, nullable=False)
    radius: Mapped[float] = mapped_column(Float, nullable=False)
    freq_low: Mapped[float] = mapped_column(Float, nullable=False)
    freq_high: Mapped[float] = mapped_column(Float, nullable=False)
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    end_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Snapshot of the previous effective scope (original or prior amendment).
    previous_cx: Mapped[float] = mapped_column(Float, nullable=False)
    previous_cy: Mapped[float] = mapped_column(Float, nullable=False)
    previous_radius: Mapped[float] = mapped_column(Float, nullable=False)
    previous_freq_low: Mapped[float] = mapped_column(Float, nullable=False)
    previous_freq_high: Mapped[float] = mapped_column(Float, nullable=False)
    previous_start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    previous_end_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (Index("ix_amendment_events_application", "application_id"),)


class RevisionMeta(Base):
    """Singleton row (id == 1) holding the current revision number."""

    __tablename__ = "revision_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
