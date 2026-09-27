"""ORM mapping of the schema created by the Alembic migrations.

The migrations are the source of truth (they are written as SQL so the spatial pieces read exactly
as they run); these models mirror them for the request/response paths. High-volume paths use
set-based Core statements over the same engine instead of the ORM unit of work.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from geoalchemy2 import Geography, Geometry, WKBElement
from sqlalchemy import (
    REAL,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Double,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "%(table_name)s_%(column_0_name)s_idx",
    "uq": "%(table_name)s_%(column_0_name)s_key",
    "ck": "%(table_name)s_%(constraint_name)s_check",
    "fk": "%(table_name)s_%(column_0_name)s_fkey",
    "pk": "%(table_name)s_pkey",
}

POINT = Geography(geometry_type="POINT", srid=4326, spatial_index=False)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))
    username: Mapped[str] = mapped_column(CITEXT, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GeoZone(Base):
    __tablename__ = "geozones"
    __table_args__ = (
        CheckConstraint("radius_m BETWEEN 10 AND 100000", name="radius"),
        CheckConstraint("length(name) BETWEEN 1 AND 80", name="name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text)
    color: Mapped[str] = mapped_column(Text, server_default="#6d5dfc")
    center: Mapped[WKBElement] = mapped_column(POINT)
    radius_m: Mapped[float] = mapped_column(Double)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    notify_enter: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    notify_exit: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    dwell_s: Mapped[int | None] = mapped_column(Integer)
    envelope: Mapped[WKBElement] = mapped_column(
        Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False),
        Computed("perimeter_envelope(center, radius_m)", persisted=True),
    )
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Device(Base):
    __tablename__ = "devices"

    device_id: Mapped[str] = mapped_column(Text, primary_key=True)
    position: Mapped[WKBElement] = mapped_column(POINT)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    speed_mps: Mapped[float | None] = mapped_column(REAL)
    heading_deg: Mapped[float | None] = mapped_column(REAL)
    accuracy_m: Mapped[float | None] = mapped_column(REAL)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DeviceTrack(Base):
    """One applied report; the table is range-partitioned by ``recorded_at`` (10-minute slots)."""

    __tablename__ = "device_tracks"

    device_id: Mapped[str] = mapped_column(Text, primary_key=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    position: Mapped[WKBElement] = mapped_column(POINT)
    speed_mps: Mapped[float | None] = mapped_column(REAL)
    heading_deg: Mapped[float | None] = mapped_column(REAL)


class ZonePresence(Base):
    __tablename__ = "zone_presence"

    device_id: Mapped[str] = mapped_column(Text, primary_key=True)
    zone_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("geozones.id", ondelete="CASCADE"), primary_key=True
    )
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dwell_alerted: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (UniqueConstraint("zone_id", "device_id", "kind", "occurred_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    zone_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("geozones.id", ondelete="SET NULL")
    )
    zone_name: Mapped[str] = mapped_column(Text)
    device_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    position: Mapped[WKBElement] = mapped_column(POINT)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OutboxMessage(Base):
    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subject: Mapped[str] = mapped_column(Text)
    msg_id: Mapped[str] = mapped_column(Text)
    payload: Mapped[bytes] = mapped_column(LargeBinary)
    trace_context: Mapped[dict[str, str] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PartitionEpoch(Base):
    __tablename__ = "partition_epochs"

    partition: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    revision: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    owner: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
