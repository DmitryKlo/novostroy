# Developer experience enrichment candidate, 2026-10-11

The immutable candidate is `developer-enrichment-v1-46d4dfa83de8d234` at
`data/property/developer_enrichment/runs/`. Its manifest SHA-256 is
`dbd76a0de7e93af69441767ac1350d78327acab86d2afbe6c03356f7ceb1beb2`.
It pins the active developer map (`7092646e…`), apartment menu (`e527bb9c…`),
and selected models (`5a525ff1…`) by their complete run IDs and manifest hashes.
The active experience pointer has not been switched by this producer.

The candidate has 1,163 building rows, 45,466 source-scoped apartment attribute
rows, and 63,583 exact media bindings. Of the buildings, 1,027 mapped points are
inside the pinned Moscow boundary and have a unique cartographic district/okrug
join; 965 joins have normal confidence and 62 are within roughly 100 m of a
district polygon edge. The remaining 136 buildings have no exact point and keep
unknown municipality/district/okrug values. No building was excluded as outside
Moscow on insufficient evidence. District/okrug IDs are stable name-derived
handles, not governmental codes. The join source manifest and individual row
hashes are pinned in the enrichment manifest.

The 1,027 mapped buildings have an internal 0–100 OSM proximity noise index
(range 6–81, higher means closer to selected mapped source classes); 136
unlocated buildings have `null`, never zero. The versioned formula uses major
roads, surface rail, industrial/works candidates, and aerodrome/runway geometry
from the pinned Central Federal District PBF with a 15 km search halo. This is
neither measured dB(A) nor evidence that mapped industrial premises operate.
The raw feature evidence and formula remain in the noise run manifest; the
experience overlay carries compact component distances and source IDs.

No district air measurement has been established. Official reports contain
city/TiNAO or station-level values. A separate Open-Meteo/CAMS response sampled
132 mapped district candidate points in one ordinary HTTP 200 request, returning
30 distinct roughly 11 km model cells. The active buildings occupy 87 of those
districts and 25 cells: 1,027 located buildings have a dated `air_model` with
PM2.5, PM10 and NO2 in µg/m³; 136 unlocated buildings keep `null`. The sample
forecast is valid at 2026-10-11 03:00 MSK and was retrieved at
2026-10-10 23:15:31 UTC. The local one-hour request-cache refresh time is
separate from the consumer's 24-hour stale marker. These are model grid-cell
samples near district candidate representative points, never district averages
or monitoring-station measurements. `air_value` remains `null` for measured
district air. The corrected air handoff manifest SHA-256 is
`48635ba75ee4b436f78929f36afa70eb679d54c8e4a4fd35ffc1da236311ff74`;
its rows SHA-256 is `1e0987e13c03db7819409a07bed14806575225a99384361c88296b1f36bacbcf`.
Attribution: Open-Meteo.com (CC BY 4.0) / CAMS European Air Quality Forecast.
The captured free API request is documented for this personal, noncommercial
project; a different use would need its own provider terms review.

The menu source additions include finishing and developer-reported delivery
fields where observed, with source dates. No row asserts current availability
or unconditional discounted payment terms. The media registry contains 62,553
unit/floor plan references, three single-corpus Lake gallery references, and
1,027 project-scope render references. Project renders carry the label
“Визуализация ЖК”; they are not house-specific photographs. Four existing menu
plans are already prepared; other images are bounded lazy candidates by exact
media ID and allowlisted CDN host. Source rights remain `unknown` independently
of the user-authorized factual preview policy.

A host ordinary-GET smoke retrieved one non-Lake apartment plan as a legible
PNG (61,210 bytes, SHA-256 `b36c176cd868dddde80fc10eeff6e6b1f85f18d4d6623b56706bb8c7032cfac9`)
and one project CGI render as a JPEG (593,273 bytes, SHA-256
`e8552ae6286ac54f355b734a89ba7c5a054e97b5487de51d23fd4976d48cfcb2`).
The private `.runtime/research/environment-20261011/media/smoke-20261011/host-results.json`
also preserves earlier
agent-environment DNS failures as separate events. These two samples do not
validate every source URL.

The v2 experience pointer publisher requires an exact prior pointer SHA and
the reviewed enrichment manifest SHA, validates all four pinned generations,
then flips one JSON pointer. Its rollback restores the previous pointer from a
hash-checked journal. An isolated real-input v1→v2→v1 drill passed; the final
air candidate passed the producer's full bundle verifier and 14 focused tests.
Activation still requires the final backend/API and package gates. The active
pointer was not changed by the producer.
