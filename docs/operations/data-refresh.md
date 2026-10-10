# Local data refresh and retention

`pipeline.maintenance.refresh` checks approved **local** sources. It does not
download feeds, launch a crawler, replace the city GLBs, or start a server.
An unchanged input produces `nochange` and no new model release or asset root.

```bash
cd /home/pirojban/novostroy
rtk proxy .runtime/dev-env/bin/python -m pipeline.maintenance.refresh status
rtk proxy .runtime/dev-env/bin/python -m pipeline.maintenance.refresh dry-run
rtk proxy .runtime/dev-env/bin/python -m pipeline.maintenance.refresh run
rtk proxy .runtime/dev-env/bin/python -m pipeline.maintenance.refresh stage-bootstrap
rtk proxy .runtime/dev-env/bin/python -m pipeline.maintenance.retention plan
```

The `status`/`dry-run` commands show the dependency graph, source dates, hash
problems, and planned work. `run` takes a nonblocking exclusive lock. It hashes
the 730 configured EstateLine XLSX reports and validates the normalized
manifest. A changed local report set is imported into a temporary directory;
new output must parse every report, retain every previous stable registry ID
in accepted or review, preserve rights, and have a nondecreasing **source
report** date. Missing files, parse errors, lost IDs, and backwards dates leave
the prior normalized files and active model pointer intact. Rejection details,
including unresolved IDs, are written to `data/runtime/refresh/rejected/`.

When validated normalized inputs change, the deterministic model builder and
auditor stage a content-hash run. The source, model, and visibility outputs
remain immutable. One `data/property/estateline/normalized/active.json` flip
commits a `estate-generation/1` bundle pinning all three with hashes. The
backend reads that pointer once per request; legacy normalized files and
`data/models/active.json` remain for old deployments but are not authoritative
after the bundle reader is enabled. A crash before the flip retains the old
generation; the next local run verifies and promotes a complete pending
generation. A crash after the flip leaves the new complete generation. A
manual `rollback-generation` uses a compare-and-swap transition record and
retained previous bundle, without rewriting source files. The older
`rollback-last` applies only to pre-generation journaled sidecar writes.

Initial migration requires a staged bootstrap and backend resolver gate before
the first pointer flip. The staged bundle can be inspected at the path printed
by `stage-bootstrap`; after backend validation, promote its pending JSON once:

```bash
rtk proxy .runtime/dev-env/bin/python -m pipeline.maintenance.refresh promote --pending data/runtime/refresh/pending/bootstrap-generation-<hash16>.json
```

Later successful changed-source or changed-policy `run` commands promote under
the same lock automatically. A new sanitized model asset root is pre-allowlisted
as a compatibility root before the single data pointer flip; its static bytes
can be fetched then, but selected API records still refer to the old bundle.
Near-by OMS sidecar changes are rebuilt from the pinned POI file and official
local evidence, then validated and journaled independently. Model geometry
cache has a 512 MiB retention target; a cache file becomes eligible only after
seven days and only when the cache exceeds that target.

The registry visibility sidecar has immutable runs. Its legacy active pointer
remains for older deployments; after bootstrap the backend uses the visibility
run pinned in the normalized generation bundle instead.
It archives only when `latest_known_date` or a newer exact official project
status observation is **more than 365 days** old, an exact official terminal
event is proven, the typed source category is outside the new-building set,
or the source title has an audited standalone facade-repair/restoration purpose.
Four exact source rows are pinned by ID, source row, name, and report hash;
ordinary `Фасадные работы и остекление` construction stages are retained.
It does not use file mtime, an OSM coordinate timestamp, a missing partial
feed, or a developer bankruptcy alone as project termination. Source rows and
historical model assessments remain available for review. The selected run
records `as_of_date`, `max_age_days`, reason codes, source snapshot hash, and
`next_refresh_due`; daily runs reuse its bytes until a decision changes. New
visibility runs are prepared with:

```bash
rtk proxy .runtime/dev-env/bin/python -m pipeline.maintenance.visibility age-preview
rtk proxy .runtime/dev-env/bin/python -m pipeline.maintenance.visibility stage
rtk proxy .runtime/dev-env/bin/python -m pipeline.maintenance.visibility publish
```

After bootstrap, those standalone visibility stage/publish commands alone do
not select a new API policy. `refresh run` detects changed official evidence or
the next age transition, builds the new visibility run, and commits it in a
new generation bundle. The standalone publish command is for older deployments.

The currently approved local snapshots checked on 2026-10-10 yielded
`nochange`: EstateLine 730 source files and 1,473 accepted rows, active model
run 2,317 assessments, OSM construction 756 mapped rows, social POI 50,330
rows, and 7 OMS sidecar rows at the final check. The active visibility run has
639 visible and 834 archived EstateLine rows; all 10 EstateLine rows with
verified coordinates fall in the archived historical set. The 756 separate
OSM mapped construction candidates are not EstateLine links and are unaffected
by this registry age decision. The next age transition is 2026-12-08.
The first active immutable bundle is
`generation-8ee3cc078a8f6a4f` (SHA-256
`8ee3cc078a8f6a4f3cf32bae515e2ac5dd4120009c421f04c46aca0fa268aa6f`).
Both `status` and `run` returned current/nochange immediately after the
pointer flip. The retained legacy normalized files have the same source hash
for bootstrap, and are no longer the refresh authority. Backend actual-pointer
direct and controlled ASGI checks passed for registry 639 visible/834 archived,
756 independent OSM points, and 2,317 model assessments; the release packager
dry-run included the pinned generation, visibility, and model files.

Minstroy's pinned XLS and normalized **model inputs** are checked by hash.
The current builder's `audit.json` self-hash is necessarily stale because it
records its own previous bytes; only this self-reference is omitted from the
gate. A new Minstroy raw file requires its official published version metadata
before reimport. OSM construction and the city/social POI/Valhalla datasets
have versioned, pinned inputs but no automatic raw reimport in this command.
Their changed/unavailable state is reported and the old validated outputs are
retained. Market offer feed access is not configured. The existing collection
runner has separate checkpointed jobs; this command does not call its network
jobs or synthetic demo jobs.

Retention produces a saved, content-fingerprinted plan under
`data/runtime/refresh/plans/`. It resolves active and compatibility asset
roots, active model run, local release-export manifests, explicit
`release_pins.json`, and in-flight journals. Existing public URL roots are
conservatively held for at least 365 days **after a recorded publication**;
untracked older roots, raw source evidence, deployment exports, and historical
manifests are never automatically deleted. Refresh-owned temporary folders
need seven days of grace. The plan reports logical bytes and a conservative
physical reclaim estimate that accounts for hard links. An apply command
rechecks the lock, path allowlist, references, age, and fingerprints and refuses
plans older than 24 hours:

```bash
# Only after reviewing the exact saved plan:
rtk proxy .runtime/dev-env/bin/python -m pipeline.maintenance.retention apply --plan data/runtime/refresh/plans/<sha256>.json
```

The actual project plan on 2026-10-10 has **zero eligible deletions** and
reclaimable bytes 0. Five older untracked public roots and two local deployment
exports remain protected until their client/release use is established. No
actual cleanup was applied.

Daily local scheduling is proposed in
`pipeline/maintenance/systemd/novostroy-local-refresh.{service,timer}`. The
units are **user-manager units** (install under `~/.config/systemd/user/`, then
`systemctl --user daemon-reload` and `systemctl --user enable --now
novostroy-local-refresh.timer`). The host user timer was installed and enabled
on 2026-10-10; `systemd-analyze --user verify` passed. A manual service start
finished successfully at 17:02:52 MSK with `nochange`, exit status 0, and
49.7 MB peak memory. Its next scheduled run was 2026-10-11 04:27:50 MSK.
The older collection timers were not listed as active at the last check.
The one timer runs
only the offline local command. Its
cadence can be changed without changing the refresh policy. The user service
retries failures after 10 minutes, at most three starts per two hours; a
missing source never clears the last committed generation. The last reviewed
retention plan had zero eligible deletions and no apply was run.
