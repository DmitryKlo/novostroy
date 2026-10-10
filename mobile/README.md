# Native mobile source: Unity / Cesium for Unity

This is a Unity source project for Android ARM64 and iOS. It is **not an installed iPhone app**. A signed iOS app requires macOS, the pinned Unity Editor with iOS Build Support, Xcode, an Apple signing team, and a device test. Those tools and credentials are not present on the current Linux host.

The map streams the same `/assets/...` 3D Tiles as the web client in the offer-catalog demo. The app reads offers from the shared `/v1` API; it has no bundled listings. When the API selects `catalog_view=developer_buildings` or the older `building_registry`, this native source shows an explicit unsupported-registry screen and does not fetch the demo offer map, search, or recommendations. The developer snapshot contains observed apartment rows, but it does not prove current availability or prices; use the web client to browse its buildings. The demo mode always shows **“ДЕМО: цены и геометрия иллюстративные”** and labels each offer's source quality. Synthetic building volumes and synthetic facade tiles are explicitly identified; a licensed facade source and apartment floor geometry are still required for production detail/slices. Missing building heights filled solely to render city massing are estimates; they never become floor counts, apartment facts, or recommendations.

The additive `/v1/models/candidates` DTOs are defined in `ApiModels.cs` for contract compatibility. They include independently mapped construction outlines with a nullable registry link and separate render-readiness and project-link statuses. This native source does not yet browse or render that review catalogue; the unsupported-registry screen remains explicit and never substitutes demo offers or a project model for an unlinked outline. The web `/models` screen is the older review interface; it is hidden while the developer-building snapshot is active.

The additive `/v1/map/points` DTO preserves opaque targets for developer buildings in the new `developer_map_points` snapshot and for the older point types in legacy contracts. It is source compatibility only: the native registry screen remains unsupported, so these points are not routed into demo offers or loaded as individual 3D assets. The web map uses this route for markers tied to the active registry snapshot and immutable map run.

The additive `/v1/map/nearby` and `/v1/map/nearby/viewport` DTOs preserve the point-scoped POI layers, route status, nullable travel time, explicit air-distance selection policy, and dated OMS badge. They are contract compatibility only: this native source does not render the POI overlay or its routes while registry browsing is unsupported. A mapped point or an approximate polygon marker must not be presented as a verified entrance, and an unavailable route must not become a travel-time estimate.

## Project and dependency pins

- Unity Editor **6000.3.16f1** (Unity 6.3 LTS), set in `ProjectSettings/ProjectVersion.txt`.
- `com.cesium.unity` **1.26.0** from the official `https://unity.pkg.cesium.com` scoped registry. Its published package metadata declares Unity 2022.3 minimum and ships Android ARM64 and iOS binaries. [Cesium platform requirements](https://cesium.com/learn/cesium-unity/ref-doc/supported-platforms.html) require Android IL2CPP, ARM64, and Internet access.
- `com.unity.nuget.newtonsoft-json` **3.2.2** for the API JSON.
- Built-in Unity render pipeline. Cesium added built-in pipeline support in v1.0.0; no ion account is needed for `/assets` URLs. See the [Cesium changelog](https://cesium.com/learn/cesium-unity/ref-doc/changes.html).
- Noto Sans Regular in `Assets/Resources` for Russian text, distributed under the license in `OFL-NotoSans.txt`.

Unity creates `Assets/Scenes/Main.unity` on the first Editor import through `NovostroyBuild.EnsureScene`, adds it to Build Settings, and boots the app at runtime. This source generation step must run in the real Editor; there is no fabricated `.unity` scene or build artifact in this folder. In the Editor, select **Novostroy → Create or check scene**, open `Assets/Scenes/Main.unity`, and press Play.

## Connect to the API

Start the managed local servers from the repository root with `rtk python scripts/dev.py start`. In the app's **Адрес API** screen enter `http://<computer-LAN-IP>:5173`: Vite proxies both `/v1` and `/assets` to the local API. If you start with `rtk python scripts/dev.py start --lan`, `http://<computer-LAN-IP>:8000` also works directly. With an imported building registry, the API selects its registry view automatically and this native source displays the compatibility message above. Use the computer's current reachable LAN address; it is not fixed. `127.0.0.1` inside an iPhone means the phone itself. A release app should use an HTTPS endpoint. The Editor defaults to `http://127.0.0.1:8000`; mobile devices require explicit configuration.

The visible flow is map → filters → offers → backend recommendations with reasons → favorites, with a **Данные** tab for coverage. Filters include rubles, districts, a Russian query, and the `/v1/criteria` registry's typed numeric ranges, enum choices, and three-state yes/no controls. The client sends the same hard predicates to search, recommendations, and map; the backend decides which records qualify. An unknown, stale, conflicting, or unconfirmed value never becomes a positive filter match. Criterion availability describes source feasibility, not populated coverage. Detail cards show available numeric values, units, status, date, and coverage without provider identity. Comparison remains an API capability, but its controls are hidden in this product iteration.

In offer-catalog mode, the **Данные** tab reads `/v1/data-coverage` and keeps demo offers, current real offers, and city geometry in separate views. The response may also contain either `building_registry` or `developer_building_registry`; the native validator accepts one registry dataset at a time, while this source does not yet render registry records. The tab shows dataset date, record and complex counts, per-field populated/unknown/stale and quality counts, and up to five-bin price/area charts only for the selected offer dataset. Zero real offers is shown as no confirmed current offers; demo counts and charts never substitute for it. These charts describe the loaded dataset, not the Moscow market. The client validates count partitions before displaying the response and never shows provider identities or internal QA events.

Selecting any map marker requests `/v1/search` with the marker's `complex_id`, independent of the first result page. Applied filters also refresh `/v1/map`; the backend owns hard-filter semantics and returns only matching markers. Old markers and offers clear while a new result loads. The map accepts separate ground-building, city-volume, facade, road, park, and water tile descriptors from the shared API, and reconciles their URLs on refresh, unloading removed sets without recreating unchanged ones. The flat ground is a separate persistent tileset while present in the API response; Unity does not yet gate 3D creation on visible ground content, so native ground-first timing remains unverified. The 192 MiB aggregate cache target gives the ground layer the largest share using the web viewer's relative weights (ground 12, city 8, roads 2, parks/water 1 each, facades 0.25 each), with 2 MiB per tileset left as headroom. Total simultaneous tile requests remain bounded to roughly eight across the sets. This is a source-level budget, not a measured Unity memory or LOD result; profile actual tile detail and memory on a device. New cartographic road ribbons and park/water surfaces render from their GLBs without being treated as surveyed road geometry or property facts. Tile detail scales with physical screen density. Each loaded set requires attribution, displayed in a dedicated wrapping panel on the map. A missing/failed tileset and empty result are shown as errors or empty states; they never create fallback offers.

Markers use API `complex_id` and `location`; facade and city meshes remain embedded in the shared 3D Tiles map. The current tile content does not provide a verified per-facade picking contract, so native selection uses the anchored marker ID. This is an explicit parity gate for future tile metadata work. Unit/building/floor IDs are shown only when supplied by the API; they are not inferred from a visible exterior. UI events use only the server's five allowlisted event names and a fresh in-memory random session token, without query or object identifiers.

## Build on a machine with Unity installed

Install the pinned Editor and Android Build Support or iOS Build Support in Unity Hub. Open `mobile/UnityProject`; allow Package Manager to resolve the scoped Cesium registry. Check the Console for package import errors. The Editor menu commands are:

- **Novostroy → Build Android development APK**: IL2CPP, ARM64, Internet required. Output `Builds/Android/Novostroy-dev.apk`. A build callback adds local HTTP permission to Unity's generated manifest, preserving the Unity 6 activity.
- **Novostroy → Export iOS Xcode project**: output `Builds/iOS/NovostroyXcode`. On a Mac, open that project in Xcode, set your own signing team/bundle ID if required, then build to a connected iPhone. The development export adds a local-network purpose string and an HTTP transport exception. Use HTTPS and review transport settings for a release.

Batch mode after selecting the correct target module (replace the Editor binary path):

```bash
"/Applications/Unity/Hub/Editor/6000.3.16f1/Unity.app/Contents/MacOS/Unity" \
  -batchmode -nographics -quit -projectPath "$PWD/mobile/UnityProject" \
  -buildTarget iOS -executeMethod Novostroy.Editor.NovostroyBuild.ExportIos \
  -logFile "$PWD/mobile/ios-build.log"
```

`ExportIos` only produces an Xcode project. It does not sign, install, or validate the app on a device.

## Checks available on this host

```bash
rtk dotnet run --project mobile/Tests/Novostroy.Domain.Tests.csproj
rtk dotnet run --project mobile/Tests/Novostroy.Source.Syntax.csproj
rtk proxy python mobile/Tests/check_live_api.py http://127.0.0.1:8000
rtk proxy python mobile/Tests/check_live_coverage.py http://127.0.0.1:8000
rtk proxy python mobile/Tests/check_live_map_layers.py http://127.0.0.1:8000
```

The first command checks URL validation, strict budget and predicate parsing, coverage count partitions including the optional registry dataset, compare state retained for API compatibility, and favorite state without Unity. The second parses Unity C# source under Editor, Android, and iOS preprocessor symbols; it is a syntax check, not a Unity compile. The third checks either the registry compatibility contract or, in demo mode, live API routes, shared tiles, criterion descriptors and public facts, predicate map/search parity, a complex outside the first offer page, and recommendations. The fourth checks coverage datasets and price/area bins against the live route. The fifth checks ground, city, road, park, and water descriptors, attribution, and reachable GLBs through any external region JSON once those routes are published. These checks do not compile Unity/Cesium scripts or create native builds. Once the Editor is available, run Play and both platform builds, inspect 3D Tiles on devices, and confirm local network permission and performance on a real iPhone.
