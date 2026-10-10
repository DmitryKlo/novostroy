-- Target storage design. Requires PostgreSQL with PostGIS; not exercised locally.
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS catalogue_snapshots (
    snapshot_id text PRIMARY KEY,
    captured_at timestamptz NOT NULL,
    source_manifest jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS districts (
    district_id text PRIMARY KEY,
    boundary_version text NOT NULL,
    name text NOT NULL,
    geometry geometry(MultiPolygon, 4326),
    display_json jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS districts_geom_gix ON districts USING gist (geometry);

CREATE TABLE IF NOT EXISTS complexes (
    complex_id text PRIMARY KEY,
    district_id text REFERENCES districts(district_id),
    location geometry(Point, 4326) NOT NULL,
    rights_status text NOT NULL CHECK (rights_status IN ('cleared', 'restricted', 'unknown')),
    display_json jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS complexes_location_gix ON complexes USING gist (location);
CREATE INDEX IF NOT EXISTS complexes_district_idx ON complexes (district_id);

CREATE TABLE IF NOT EXISTS offers (
    offer_id text PRIMARY KEY,
    complex_id text NOT NULL REFERENCES complexes(complex_id),
    district_id text REFERENCES districts(district_id),
    amount_rub bigint,
    price_basis text CHECK (price_basis IN ('asking_total', 'conditional_total')),
    price_observed_at timestamptz,
    record_mode text NOT NULL CHECK (record_mode IN ('developer_offer', 'asking_price')),
    rights_status text NOT NULL CHECK (rights_status IN ('cleared', 'restricted', 'unknown')),
    source_quality text NOT NULL CHECK (source_quality IN ('dated_asking', 'unknown')),
    source_record_id text NOT NULL,
    source_fields jsonb NOT NULL,
    display_json jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS offers_district_price_idx ON offers (district_id, amount_rub) WHERE rights_status = 'cleared';
CREATE INDEX IF NOT EXISTS offers_freshness_idx ON offers (price_observed_at) WHERE rights_status = 'cleared';

-- Keep external source rights, provenance, and import observations explicit.
CREATE TABLE IF NOT EXISTS source_manifests (
    source_id text PRIMARY KEY,
    source_url text,
    access_basis text NOT NULL,
    permitted_display boolean NOT NULL DEFAULT false,
    permitted_storage boolean NOT NULL DEFAULT false,
    observed_at timestamptz NOT NULL,
    source_updated_at timestamptz,
    sha256 text,
    manifest_json jsonb NOT NULL
);
