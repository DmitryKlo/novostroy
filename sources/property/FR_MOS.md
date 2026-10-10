# FR renovation-map cross-check

`fr_mos.py` collects the public renovation-program map for private address
cross-checking. It does not create apartment offers and is not connected to the
product map. The CSVs are **renovation-map registry rows, not unit prices**.

## Offline reparse

Run from the repository root. This is the default and makes no network requests:

```bash
rtk python -m sources.property.fr_mos
```

The default inputs are the saved, complete FR captures under
`.runtime/research/fr-mos-20261010/`. A new timestamped run is written under
`.runtime/research/fr-mos-20261010/runs/`; previous runs and source captures are
preserved. Each run contains an outer manifest, a processed manifest, the
source-row JSONL, projected JSONL, and two CSVs:

- `source-records.csv`: every source map object, including existing `OLD` homes
  and `START` sites.
- `crosscheck-candidates.csv`: only `PROCESSING` or `FINISHED` objects with a
  positive integer source `flats` field.

The map items have no unit prices, availability, type/category, or corpus field.
The candidate rule is a narrow address cross-check signal; it does not assert
that every object is an apartment, and it never adds rows to the product map.
Developer values and map polygons remain unverified source claims.

## Live refresh

Live refresh is opt-in and must be run through RTK only after checking the
physical route. The CLI pins the same verified route (`enp10s0`, source
`192.168.1.111`, gateway `192.168.1.1`) and server IP `212.11.155.166`; if any
route field differs, it stops before making a request.

```bash
rtk python -m sources.property.fr_mos --live --interface enp10s0 --noproxy
```

It requests only the two confirmed JSON endpoints, sequentially, with a 64 MiB
body cap and 120-second request timeout. It disables proxies, binds curl with
`if!enp10s0`, pins the observed IP, sends no cookies, does not follow redirects,
and does not retry. HTTP 401, 403, or 429 stops the run with no alternate route,
proxy, or IP attempt. Saved response headers omit `Set-Cookie` and authorization
values. Live captures and processed outputs stay under `.runtime` in a new
versioned run with SHA-256 entries.

The source URLs are:

- `https://fr.mos.ru/pokupka-nedvizhimosti-dlya-vseh/ajax.php?cmd=filters`
- `https://fr.mos.ru/pokupka-nedvizhimosti-dlya-vseh/ajax.php?category[]=ALL&map=ren`
