"""Initial schema: users, geozones with an indexable envelope, devices, presence, alerts, outbox.

Revision ID: 0001
Revises:
Create Date: 2026-09-26
"""

import re
from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A lon/lat box that provably contains the geodesic circle (centre, radius_m) on WGS84.
# Latitude span:  r / (a(1-e^2) * pi/180)       -- meridian degree is shortest at the equator
# Longitude span: r / (a cos(phi*) * pi/180)    -- parallel radius N cos(phi) >= a cos(phi),
#                 phi* = highest |latitude| the circle can reach
# Near a pole the box spans all longitudes; across the antimeridian it is split in two.
ENVELOPE_FUNCTION = r"""
CREATE FUNCTION perimeter_envelope(center geography, radius_m double precision)
RETURNS geometry
LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE
AS $$
DECLARE
    lon     double precision := ST_X(center::geometry);
    lat     double precision := ST_Y(center::geometry);
    dlat    double precision := radius_m / (6378137.0 * (1.0 - 0.00669437999014) * pi() / 180.0) + 1e-9;
    south   double precision := greatest(lat - dlat, -90.0);
    north   double precision := least(lat + dlat, 90.0);
    widest  double precision := greatest(abs(south), abs(north));
    dlon    double precision;
BEGIN
    IF widest >= 89.999999 THEN
        RETURN ST_MakeEnvelope(-180.0, south, 180.0, north, 4326);
    END IF;
    dlon := radius_m / (6378137.0 * pi() / 180.0 * cos(radians(widest))) + 1e-9;
    IF dlon >= 180.0 THEN
        RETURN ST_MakeEnvelope(-180.0, south, 180.0, north, 4326);
    ELSIF lon - dlon < -180.0 THEN
        RETURN ST_Multi(ST_Collect(
            ST_MakeEnvelope(-180.0, south, lon + dlon, north, 4326),
            ST_MakeEnvelope(lon - dlon + 360.0, south, 180.0, north, 4326)));
    ELSIF lon + dlon > 180.0 THEN
        RETURN ST_Multi(ST_Collect(
            ST_MakeEnvelope(lon - dlon, south, 180.0, north, 4326),
            ST_MakeEnvelope(-180.0, south, lon + dlon - 360.0, north, 4326)));
    END IF;
    RETURN ST_MakeEnvelope(lon - dlon, south, lon + dlon, north, 4326);
END
$$
"""

ENVELOPE_COMMENT = """
COMMENT ON FUNCTION perimeter_envelope(geography, double precision) IS
    'Conservative lon/lat box of a geodesic circle; GiST prefilter for zone matching.'
"""

# Device tracks live in 10-minute range partitions; this function keeps the window rolling: it
# drops the slots that ended before the retention window (a DROP, not millions of DELETEs) and
# attaches the slots from `retention` ago to `ahead` from now, moving in any rows the default
# partition took while a slot was missing. It runs as the schema owner (SECURITY DEFINER), so the
# services can keep the partitions current without any DDL rights of their own.
TRACKS_FUNCTION = r"""
CREATE FUNCTION perimeter_maintain_tracks(
    retention interval, ahead interval DEFAULT interval '20 minutes'
)
RETURNS TABLE (created integer, dropped integer, moved bigint, purged bigint, failed integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET timezone = 'UTC'
-- A step that cannot get its lock soon gives up until the next round, rather than queueing the
-- engines' inserts behind it (a long backup holds locks on every table it reads, for example).
SET lock_timeout = '500ms'
AS $$
DECLARE
    slot     CONSTANT interval := interval '10 minutes';
    origin   CONSTANT timestamptz := timestamptz '2000-01-01 00:00:00+00';
    first_at CONSTANT timestamptz := date_bin(slot, now() - retention, origin);
    last_at  CONSTANT timestamptz := date_bin(slot, now() + ahead, origin);
    at       timestamptz;
    name     text;
    n        bigint;
    part     record;
BEGIN
    created := 0;
    dropped := 0;
    moved := 0;
    purged := 0;
    failed := 0;
    -- One maintainer at a time: whoever does not get the lock has nothing to do this round.
    IF NOT pg_try_advisory_xact_lock(hashtext('perimeter_maintain_tracks')) THEN
        RETURN NEXT;
        RETURN;
    END IF;

    -- Expired slots go whole. Every step below is its own subtransaction, so one that cannot get
    -- its lock is retried next round without holding up the others.
    FOR part IN
        SELECT c.relname
        FROM pg_inherits AS i
        JOIN pg_class AS c ON c.oid = i.inhrelid
        WHERE i.inhparent = 'public.device_tracks'::regclass
          AND c.relname ~ '^device_tracks_[0-9]{12}$'
          AND to_timestamp(substr(c.relname, 15), 'YYYYMMDDHH24MI') + slot <= now() - retention
    LOOP
        BEGIN
            EXECUTE format('DROP TABLE public.%I', part.relname);
            dropped := dropped + 1;
        EXCEPTION WHEN lock_not_available THEN
            failed := failed + 1;
        END;
    END LOOP;

    -- The default partition holds only reports no slot covered: those past retention, purged
    -- here, and those that arrived while maintenance could not run, moved into their slot below.
    BEGIN
        DELETE FROM public.device_tracks_default WHERE recorded_at < first_at;
        GET DIAGNOSTICS n = ROW_COUNT;
        purged := n;
    EXCEPTION WHEN lock_not_available THEN
        failed := failed + 1;
    END;

    at := first_at;
    WHILE at <= last_at LOOP
        name := 'device_tracks_' || to_char(at, 'YYYYMMDDHH24MI');
        IF to_regclass('public.' || name) IS NULL THEN
            BEGIN
                -- Built apart and attached: ATTACH locks the parent only against schema changes,
                -- so reports keep flowing (CREATE ... PARTITION OF would stop them). A slot can
                -- only be attached once the default partition holds none of its rows, so those
                -- move in first, with the default locked against new ones meanwhile.
                LOCK TABLE public.device_tracks_default IN ACCESS EXCLUSIVE MODE;
                EXECUTE format(
                    'CREATE TABLE public.%I (LIKE public.device_tracks INCLUDING ALL, '
                    'CONSTRAINT %I CHECK (recorded_at >= %L AND recorded_at < %L))',
                    name, name || '_range', at, at + slot
                );
                EXECUTE format(
                    'WITH taken AS (DELETE FROM public.device_tracks_default '
                    'WHERE recorded_at >= %L AND recorded_at < %L RETURNING *) '
                    'INSERT INTO public.%I SELECT * FROM taken',
                    at, at + slot, name
                );
                GET DIAGNOSTICS n = ROW_COUNT;
                EXECUTE format(
                    'ALTER TABLE public.device_tracks ATTACH PARTITION public.%I '
                    'FOR VALUES FROM (%L) TO (%L)',
                    name, at, at + slot
                );
                -- The check spared ATTACH a scan of the slot; the partition bound replaces it.
                EXECUTE format('ALTER TABLE public.%I DROP CONSTRAINT %I', name, name || '_range');
                created := created + 1;
                moved := moved + n;
            EXCEPTION WHEN lock_not_available THEN
                failed := failed + 1;
            END;
        END IF;
        at := at + slot;
    END LOOP;
    RETURN NEXT;
END
$$
"""

TRACKS_GRANTS = r"""
DO $$
BEGIN
    REVOKE ALL ON FUNCTION perimeter_maintain_tracks(interval, interval) FROM PUBLIC;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'perimeter_app') THEN
        GRANT EXECUTE ON FUNCTION perimeter_maintain_tracks(interval, interval) TO perimeter_app;
    END IF;
END
$$
"""

SCHEMA = r"""
CREATE TABLE users (
    id          uuid PRIMARY KEY DEFAULT uuidv7(),
    username    citext NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT users_username_key UNIQUE (username),
    CONSTRAINT users_username_check CHECK (username ~ '^[a-z0-9][a-z0-9_.-]{1,31}$')
);

CREATE TABLE geozones (
    id            uuid PRIMARY KEY DEFAULT uuidv7(),
    owner_id      uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    name          text NOT NULL,
    color         text NOT NULL DEFAULT '#6d5dfc',
    center        geography(Point, 4326) NOT NULL,
    radius_m      double precision NOT NULL,
    is_active     boolean NOT NULL DEFAULT true,
    notify_enter  boolean NOT NULL DEFAULT true,
    notify_exit   boolean NOT NULL DEFAULT true,
    dwell_s       integer,
    envelope      geometry(Geometry, 4326)
                  GENERATED ALWAYS AS (perimeter_envelope(center, radius_m)) STORED,
    version       integer NOT NULL DEFAULT 1,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT geozones_name_check CHECK (length(name) BETWEEN 1 AND 80),
    CONSTRAINT geozones_color_check CHECK (color ~ '^#[0-9a-f]{6}$'),
    CONSTRAINT geozones_radius_check CHECK (radius_m BETWEEN 10 AND 100000),
    CONSTRAINT geozones_dwell_check CHECK (dwell_s IS NULL OR dwell_s BETWEEN 10 AND 86400)
);
-- Only active zones take part in matching, so only they are indexed for it.
CREATE INDEX geozones_active_envelope_gix ON geozones USING gist (envelope) WHERE is_active;
CREATE INDEX geozones_owner_idx ON geozones (owner_id, created_at DESC, id DESC);

CREATE TABLE devices (
    device_id    text PRIMARY KEY,
    position     geography(Point, 4326) NOT NULL,
    recorded_at  timestamptz NOT NULL,
    received_at  timestamptz NOT NULL,
    speed_mps    real,
    heading_deg  real,
    accuracy_m   real,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT devices_device_id_check CHECK (device_id ~ '^[A-Za-z0-9_-]{1,64}$')
) WITH (fillfactor = 70);
-- Distance questions (constant radius) use the geography index; viewport questions are lon/lat
-- rectangles and use the planar one, where a box edge is a parallel, not a great circle.
CREATE INDEX devices_position_gix ON devices USING gist (position);
CREATE INDEX devices_lonlat_gix ON devices USING gist ((position::geometry));
CREATE INDEX devices_recorded_at_brin ON devices USING brin (recorded_at);

CREATE TABLE zone_presence (
    device_id      text NOT NULL,
    zone_id        uuid NOT NULL REFERENCES geozones (id) ON DELETE CASCADE,
    entered_at     timestamptz NOT NULL,
    last_seen_at   timestamptz NOT NULL,
    dwell_alerted  boolean NOT NULL DEFAULT false,
    PRIMARY KEY (device_id, zone_id)
);
CREATE INDEX zone_presence_zone_id_idx ON zone_presence (zone_id);

CREATE TABLE alerts (
    id           uuid PRIMARY KEY DEFAULT uuidv7(),
    owner_id     uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    zone_id      uuid REFERENCES geozones (id) ON DELETE SET NULL,
    zone_name    text NOT NULL,
    device_id    text NOT NULL,
    kind         text NOT NULL,
    position     geography(Point, 4326) NOT NULL,
    occurred_at  timestamptz NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT alerts_kind_check CHECK (kind IN ('enter', 'exit', 'dwell')),
    CONSTRAINT alerts_zone_id_key UNIQUE (zone_id, device_id, kind, occurred_at)
);
CREATE INDEX alerts_owner_id_idx ON alerts (owner_id, occurred_at DESC, id DESC);

CREATE TABLE outbox (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject        text NOT NULL,
    msg_id         text NOT NULL,
    payload        bytea NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    claimed_until  timestamptz  -- the outbox sweeper's reservation while it publishes the row
);
CREATE INDEX outbox_created_at_idx ON outbox (created_at);

-- Every applied report, for device trails; range-partitioned by event time (see TRACKS_FUNCTION).
CREATE TABLE device_tracks (
    device_id    text NOT NULL,
    recorded_at  timestamptz NOT NULL,
    position     geography(Point, 4326) NOT NULL,
    speed_mps    real,
    heading_deg  real,
    CONSTRAINT device_tracks_pkey PRIMARY KEY (device_id, recorded_at)
) PARTITION BY RANGE (recorded_at);
CREATE TABLE device_tracks_default PARTITION OF device_tracks DEFAULT;

CREATE TABLE partition_epochs (
    partition   smallint PRIMARY KEY,
    generation  bigint NOT NULL DEFAULT 0,
    revision    bigint NOT NULL DEFAULT 0,
    owner       text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
"""


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.execute(ENVELOPE_FUNCTION)
    op.execute(ENVELOPE_COMMENT)
    # asyncpg runs each statement as a prepared statement, which admits one command at a time.
    # Statements end with ";" at the end of a line (the schema has no function bodies).
    for statement in re.split(r";[ \t]*\n", SCHEMA):
        if statement.strip():
            op.execute(statement)
    op.execute(TRACKS_FUNCTION)
    op.execute(TRACKS_GRANTS)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS perimeter_maintain_tracks(interval, interval)")
    op.execute(
        "DROP TABLE IF EXISTS partition_epochs, device_tracks, outbox, alerts, zone_presence, "
        "devices, geozones, users CASCADE"
    )
    op.execute("DROP FUNCTION IF EXISTS perimeter_envelope(geography, double precision)")
