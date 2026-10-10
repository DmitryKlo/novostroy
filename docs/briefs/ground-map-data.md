# Ground map data for exact building footprints

> Historical assessment before ground-v2/city-v8 publication. Current artifacts
> and checks are recorded in [validation](../../reports/validation.md); statements
> below about what the map "currently" uses describe the former v7 baseline.

## Finding

The local source already has the right geometry and useful tile grid. The
permanent flat layer should be an independent ground tileset built from the
same clipped Overture Building polygons as the city volumes. Keep roads,
parks, and water as their existing separate cartographic tilesets. A low
resolution overview may help while vector cells load, but the current v7
silhouette cannot serve as the close-zoom footprint layer.

The map currently points at `public-citywide-v7` plus separate public roads,
parks, and water sets. `MapView` disables the globe base imagery, uses an
ellipsoid provider, and adds those sets independently. City v7 begins with 212
regional raster masks; its maximum reported pixel footprint is 11.31 m, and
222,868 features were classified as subpixel. The builder turns a feature
that fills no pixel into a single representative-point pixel. That is useful
only as a distant hint: it cannot show a house outline at street zoom, preserve
small courtyards, or define the permanent ground. The v7 mask contains 1.47 MB
of GLB data but reserves 129.8 MB of RGBA-equivalent texture memory.

## Pinned geometry and existing tiling

Use [`overture_moscow_cityclipped_2026-09-23.1.parquet`](../../data/city/overture_moscow_cityclipped_2026-09-23.1.manifest.json), the input already used by `build_citywide.py`. Its SHA256 is
`a905f22507f4e676479dccd984d1f338985bce9ac40c25694b21546b8e6538da`. The
clipped snapshot has 564,031 unique valid polygon features, including 563,329
Polygon and 702 MultiPolygon rows. The source composition is 289,858 OSM and
274,173 Microsoft ML records; 541 intersecting geometries were clipped to the
pinned Moscow mask. “Exact” here means faithful to this mapped source geometry,
not surveyed building boundaries.

The existing builder assigns each feature to the 0.01-degree cell containing
its bounding-box center. That produces 3,396 detailed cells under 212 parents,
grouped in 5-by-5 cell regions. Near Moscow, a cell is roughly 630 m east-west
by 1,110 m north-south. Reuse this assignment and parent graph for ground
footprints so a polygon has one owner and aligns with the massing/detail
tiles. Roads already use this grid; parks and water use the same cell and
region constants. The ground builder must include each full polygon bounds in
the cell tile bounds even when a footprint crosses a grid edge.

The building-part allowlist (800 parts on 166 parents) is a detail overlay,
not another ground polygon source. Ground footprints should come from the
Building layer only; using both Building and BuildingPart geometry would
overdraw the same parcel with overlapping surfaces.

## Sample surface-only tile measurements

I built these GLBs in memory only, without writing product files. Each sample
uses the existing cell assignment, original polygon rings with no simplifier,
constrained Delaunay triangles, and the current `Mesh`/`glb` writer. The writer
duplicates vertex attributes for every triangle; all sample feature IDs were
set to zero, and no source ID list was embedded. Gzip is a local level-9 size
measurement, not a browser transfer benchmark.

| Cell selection | Source polygons | Source ring vertices | Holes / multipolygons | Surface triangles | GLB raw | gzip |
|---|---:|---:|---:|---:|---:|---:|
| 10th-percentile cell, 3696/5575 | 8 | 46 | 0 / 0 | 22 | 5,636 B | 1,984 B |
| Median-count cell, 3775/5566 | 132 | 1,264 | 1 / 1 | 863 | 106,592 B | 28,428 B |
| Densest cell, 3721/5560 | 1,576 | 8,555 | 0 / 0 | 3,827 | 462,280 B | 137,289 B |

The middle and dense cells show that one batched GLB per cell is practical;
tile size follows geometry density, so fixed “bytes per tile” assumptions are
not safe. A ring-count estimate over the whole source gives about 2.08 million
triangles before the collinear/degenerate cases that constrained triangulation
can omit, or about 250 MB at 120 serialized bytes per triangle with the current
duplicating writer. This is a sizing estimate, not a full build. Shared
vertices, omission of noninteractive feature-ID attributes, and a lean ground
material should reduce the size. Do not create a primitive or request per
building.

The source has 995 interior rings and 702 MultiPolygon rows. For each surface,
triangulate each valid polygon with constrained Delaunay and retain only
triangles covered by that polygon; this leaves courtyards and holes empty. Do
not replace small buildings with point markers or square fallbacks. If an
overview is needed, rasterize the actual polygon coverage at that pixel
tolerance and allow subpixel area to disappear or contribute fractional
coverage. The current representative-point fallback is not suitable for that
overview policy.

## Proposed ground and building layers

Publish a versioned, separate ground root, for example
`public-ground-footprints-v1`. Its region parents can hold only a deliberately
coarse overview, with the 3,396 exact flat vector cell contents as `REPLACE`
children. Keep the ground tree independent from the city-volume tree: the
ground remains under the building massing/detail instead of disappearing when
the city tileset replaces its own parent. While vector children are loading,
keep the overview parent visible. The existing v7 raster may remain part of
the city-volume overview; it should not be the permanent footprint surface.

Use the original clipped WKB and a flat, unlit ground material. Transform each
footprint vertex through WGS84 ECEF into a **cell-local** ENU frame, with a
small positive ground offset that clears the ellipsoid. Do not flatten a
whole 5-by-5 region to one tangent plane: at Moscow latitude a 0.01-degree cell
has about 0.03 m of edge sag relative to a center tangent plane, while a
5-by-5 region approaches 0.8 m near its corners. Per-vertex ECEF or the small
cell frame keeps the footprint above the ellipsoid without bending courtyards
or introducing region seams. Roads/parks/water currently use assumed offsets
of 1.6/1.0/1.3 m; place the ground footprint below those cartographic surfaces
and validate z-fighting and overlap with the volume bases.

The current 3D Tiles assets already batch geometry by cell and load through
Cesium's view-dependent traversal. Keep that pattern: region fallback,
cell-level detail, one batched material primitive per cell. Let distant cells
stay unloaded and let nearby ground cells remain cacheable under versioned
URLs. `MapView` currently budgets Cesium's in-memory tile cache at 128 MiB for
mobile-width screens and 160 MiB otherwise, shared among sets with a 2 MiB
overflow per set. A new ground tileset needs an explicit cache share and a
street-zoom policy; do not precache all 3,396 GLBs. `RenderAssetFiles` and the
public publisher currently expose city, roads, parks, and water roots, so the
new root needs its own sanitized public pointer and publisher allowlist entry.
Its immutable versioned GLBs can use the existing one-year HTTP cache policy.

## Validation boundary

This is a local data/pipeline assessment only. No product tiles were built or
published, and no live rendering or network benchmark was run. The source
manifest reports all 564,031 clipped geometries valid, but mapping completeness
and survey accuracy are unknown. The three GLB samples measure exporter and
compression size only; they do not establish Cesium GPU memory, frame rate, or
mobile request timing.
