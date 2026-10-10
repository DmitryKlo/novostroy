# Additive criteria contract v1.1

This extends v1 without changing existing paths, fields, eligibility, or demo/real
separation. `GET /v1/criteria` returns `contracts/criteria_registry.json` wrapped in
`{snapshot_id, data_mode, version, criteria}`. The registry lists stable criterion
keys, value types, units, scopes, operators, source feasibility, coverage policy,
and evidence. Its metadata describes what *could* be collected; it is not a claim
that a fact is populated. Clients must not infer coverage from registry presence.

`Offer.facts` and, where relevant, `Complex.facts` are maps keyed by registry keys.
Internally each fact is `{value, unit, status, scope, observed_at, provenance}`. `status` is
`observed`, `derived`, `estimated`, `illustrative`, `unknown`, `stale`, `conflict`,
or `not_applicable`. The last four require `value:null`; explicit boolean `false`
is a known value only if a source supplies it. `observed` means a dated source
assertion, not a site survey. Internal `provenance` retains source ID, record ID, field,
source revision time, rights, method, source version, coverage, approximation,
and, for derived facts, inputs and formula. No null/absent field becomes zero,
false, or `none`. Sample nearest features must keep sample scope and partial or
unknown coverage. Building/complex facts projected to offers retain original
scope and provenance; the backend uses that projection for map/search parity.

POST search/recommendations add optional `filters.predicates`, up to 24 entries:
`[{"key":"unit.floor","op":"min","value":7}]`. Operators are `min`, `max`
(inclusive), `equals`, `in` (scalar in supplied array, or any overlap for a
known string-array fact), and `contains` (element in a supplied array or
case-insensitive substring in a known string). Each key
allows only its registry operators and typed values. Unknown key/operator/type
is HTTP 422, not an empty or relaxed result. `in` accepts at most 64 values;
text values at most 256 characters. All predicates are hard AND filters before
ranking, limit, and map marker counts. `GET /v1/map` accepts the same
predicates as one URL-encoded JSON array in the `predicates` query parameter.
Its `applied_filters.predicates` echoes the canonical array. Existing
budget/room/area filters and Russian query parsing continue unchanged.

Only `observed` and `derived` facts from cleared sources may satisfy real-mode
predicates. In demo, `illustrative` facts may satisfy them with demo labeling.
Estimated, unknown, stale, conflict, absent and not-applicable values never
satisfy a hard predicate. A registry criterion requiring complete coverage also
requires `provenance.coverage_status="complete"` and cannot be satisfied by a
sample-scoped fact. Real offer price retains the v1 24-hour current-UTC cutoff;
other facts retain per-field observed/source times and refresh policy. The
registry does not invent an SLA where none is documented.

`offer.price_total_rub` and `offer.price_per_sqm_rub` are canonical *asking*
values only. A conditional price is never substituted unless a separate
explicitly named criterion is introduced. Price/m² is the exact ratio of
integer total RUB asking price to positive decimal total area in m², represented
as `{numerator_rub:int, denominator_sqm:decimal-string}` and compared using
exact rational arithmetic. API threshold values are decimal strings or JSON
numbers, never rounded vendor square-price fields. A derived fact records both
inputs and the formula; a vendor square-price remains raw evidence.

The default public criterion descriptor allowlist is `key`, `label_ru`, `group`,
`type`, `value_type`, `unit`, `operators`, `enum_values`, `availability`,
`filterable`, and `coverage_requirement`. Source IDs and free-text registry
evidence, scope, refresh and coverage notes remain server-internal because they
may identify a provider. `availability` states source feasibility, never the
number of populated facts. The default public fact projection exposes `value`, `unit`, `status`, `scope`,
`observed_at`, `coverage_status`, `approximate`, and `method` on a fact. It
omits `provenance` and provider identity from all API objects, including
`source.name`, URLs, source IDs/fields, and registry `source_ids`/`evidence`.
The backend may expose those internal fields only via a server-startup
`expose_sources` setting that defaults off; no HTTP parameter, header, request
body, or frontend control can enable it. Source quality, record mode, rights
status, and dates remain public to avoid hiding demo or evidence quality.
Mandatory license/copyright attribution on map tiles remains visible
independent of this setting. The comparison endpoint remains for compatibility,
while comparison controls are hidden in this product iteration.

The map may add streamed road, park, and water tilesets with kinds `roads`,
`parks`, and `water`. Roads use `model_quality="cartographic_ribbon"`; parks and
water use `model_quality="cartographic_surface"`. These are cartographic map
layers, not surveyed physical surfaces. Each source license attribution remains
public. Building materials may be darker, parks pastel green, and water pastel
blue while the surrounding interface stays in the existing light theme.

`contracts/build_registry.py` merges processed matrices under `docs/criteria/`
into a deterministic registry; refresh it after source-matrix updates. The
backend must serve the generated file and use the same registry when invoking
the pure recommendation engine. Run `integration/check_live.py` for shared
snapshot and map/search parity after controlled server restart.

The approved separate «Данные» view consumes provider-hidden
`GET /v1/data-coverage`; its dataset denominators, quality partitions, and
offer-only distributions are specified in `DATA-COVERAGE.md`. QA funnel
metrics from `/v1/metrics` are not shown as user or market results.
