\set ON_ERROR_STOP on

SELECT PostGIS_Full_Version();

BEGIN;
CREATE TEMP TABLE property_spatial_smoke (
    id text PRIMARY KEY,
    geom geometry(Point, 4326) NOT NULL
);
INSERT INTO property_spatial_smoke (id, geom)
VALUES ('synthetic-moscow-point', ST_SetSRID(ST_MakePoint(37.6173, 55.7558), 4326));

DO $$
BEGIN
    IF (SELECT ST_SRID(geom) FROM property_spatial_smoke WHERE id = 'synthetic-moscow-point') <> 4326 THEN
        RAISE EXCEPTION 'unexpected source SRID';
    END IF;
    IF NOT ST_Within(
        (SELECT geom FROM property_spatial_smoke WHERE id = 'synthetic-moscow-point'),
        ST_MakeEnvelope(37.5, 55.6, 37.8, 55.9, 4326)
    ) THEN
        RAISE EXCEPTION 'synthetic point is outside the expected bounding box';
    END IF;
    IF ST_X(ST_Transform(
        (SELECT geom FROM property_spatial_smoke WHERE id = 'synthetic-moscow-point'), 3857
    )) <= 0 THEN
        RAISE EXCEPTION '3857 transformation returned an invalid x coordinate';
    END IF;
END;
$$;

SELECT id, ST_SRID(geom) AS srid, ST_AsText(geom) AS wkt,
       round(ST_X(ST_Transform(geom, 3857))::numeric, 2) AS web_mercator_x
FROM property_spatial_smoke;
ROLLBACK;
