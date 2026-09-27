"""Initial schema: users, geozones with an indexable envelope, devices, presence, alerts, outbox.

Revision ID: 0001
Revises:
Create Date: 2026-09-26
"""

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
CREATE INDEX devices_position_gix ON devices USING gist (position);
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
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject     text NOT NULL,
    msg_id      text NOT NULL,
    payload     bytea NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX outbox_created_at_idx ON outbox (created_at);

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
    # The schema contains no function bodies, so splitting on ";" is exact here.
    for statement in SCHEMA.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    op.execute(
        "DROP TABLE IF EXISTS partition_epochs, outbox, alerts, zone_presence, devices, "
        "geozones, users CASCADE"
    )
    op.execute("DROP FUNCTION IF EXISTS perimeter_envelope(geography, double precision)")
