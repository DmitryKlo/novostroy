# Ground-map visual QA without a TCP server

> Pre-change QA brief. The current web code has a ground-first barrier; its
> offline tests passed, while browser screenshots remain unavailable here.
> See the [current validation](../../reports/validation.md).

## Existing harnesses

`web/playwright.config.ts` is not suitable in the current restricted run: its
`webServer` starts Vite on `127.0.0.1:5173`. `web/tests/*.spec.ts` depend on that
origin. `web/scripts/diagnose-mobile-map.mjs` also navigates to the running
preview and its optional route handler calls `route.fetch()`. The cache
measurement script does not start a server, but requires the same live preview.
`web/tests/cache.unit.mjs` is offline Node coverage for request/tile-cache logic;
it does not render a map. Existing screenshots in `web/artifacts/` are prior
server-backed captures and do not prove the new flat-ground sequence.

The repo has `@playwright/test`, `web/dist` bundles, copied Cesium assets in
`web/dist/cesium`, and local tiles under `assets/public-citywide-v7`,
`assets/public-roads-v1`, `assets/public-parks-v1`, and
`assets/public-water-v1`. Playwright-managed Chromium is present under
`/home/pirojban/.cache/ms-playwright`; no system `chromium` executable is on
PATH.

## No-server route option

A standalone Node runner can avoid Playwright Test's `webServer`: launch
Chromium directly, register one `page.route('**/*', ...)`, then navigate to a
fixture origin such as `http://127.0.0.1:5173/`. Fulfill every request from
local files or fixed JSON: `/` from `web/dist/index.html`, `/bundled/*` and
`/cesium/*` from `web/dist`, `/assets/*` from the local asset root, and
`/v1/health` plus registry list/map endpoints from fixtures. Never call
`route.fetch()` or fall through to the origin. This uses Playwright's browser
pipe and route fulfillment; it does not launch Vite, FastAPI, or a TCP listener.

This option was not executable in the current environment. The one Chromium
launch attempt failed before page creation with:

```text
[FATAL:content/browser/sandbox_host_linux.cc:41] Check failed: . shutdown: Operation not permitted (1)
process did exit ... signal=SIGTRAP
```

No retry or socket workaround was attempted, so no new screenshot is claimed.
Route fulfillment is a viable harness design, not a visually verified result
on this host. Intercepted responses should also not be used to claim browser
HTTP disk-cache behavior; a same-page cache-warm check can test retained app /
Cesium state instead.

## Required capture set

Use the same local registry fixture, local source-backed ground tiles, viewport,
camera, and screenshot crop throughout. Save screenshots and a request/state
JSON trace under `.runtime/ground-map-qa/` (ignored generated output). Keep the
fixture building at a known point and with zero offers/prices.

1. **`01-flat-first-frame.png`** — wait for an explicit ground-ready signal;
   capture the first stable frame while the 3D layer route is still held. It
   must show the flat map floor with roads, parks, water, and exact footprint
   contours. The request trace must show that no building/volume GLB or 3D
   tileset was requested before ground-ready.
2. **`02-ground-after-3d.png`** — release the 3D fixture after the flat capture,
   wait for its visible detail, then capture at the same camera. The source
   ground remains in the same position and remains legible beneath/around the
   volumes; compare fixed feature pixels or contours as well as the screenshot.
3. **`03-cache-warm.png`** — switch to Data and back to Map in the same page and
   context, then capture again. Assert unchanged camera and no duplicate ground
   or 3D asset requests. This proves retained app/Cesium cache state, not HTTP
   disk cache.
4. **`04-zoom-mid.png` / `05-zoom-close.png`** — use the visible zoom controls
   and capture two closer levels. Record camera height before/after, assert it
   decreases, and verify that the same ground features remain aligned while
   3D detail appears only after the ground stage.

The runner should fail on a ground/3D request-order violation, missing local
asset, external request, console/page error, fallback view, or absent feature
control. Current `MapView` starts every tileset in one `Promise.all` after the
map response, so a no-early-3D assertion is expected to fail until the new
ground-first stage is implemented and exposes a test-ready signal.
