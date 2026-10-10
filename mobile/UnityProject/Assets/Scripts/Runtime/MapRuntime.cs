using System;
using System.Collections.Generic;
using System.Linq;
using CesiumForUnity;
using Novostroy.Domain;
using UnityEngine;

namespace Novostroy.Runtime
{
    public sealed class MapMarker
    {
        public MapComplex Complex;
        public Transform Transform;
    }

    public sealed class MapRuntime : MonoBehaviour
    {
        private readonly List<MapMarker> markers = new List<MapMarker>();
        private readonly Dictionary<string, GameObject> tileNodes = new Dictionary<string, GameObject>();
        private CesiumGeoreference georeference;
        private Camera mapCamera;
        private GameObject mapRoot;
        private double longitude;
        private double latitude;
        private float cameraDistance = 6500f;

        public IReadOnlyList<MapMarker> Markers => markers;
        public Camera MapCamera => mapCamera;
        public string TileError { get; private set; }
        public string Attribution { get; private set; }
        public int TilesetCount { get; private set; }
        public float TopUiPixels { get; set; } = 160f;
        public float BottomUiPixels { get; set; } = 80f;

        private void OnEnable() =>
            Cesium3DTileset.OnCesium3DTilesetLoadFailure += OnTileFailure;

        private void OnDisable() =>
            Cesium3DTileset.OnCesium3DTilesetLoadFailure -= OnTileFailure;

        private void OnTileFailure(Cesium3DTilesetLoadFailureDetails details)
        {
            if (mapRoot != null && details.tileset != null &&
                details.tileset.transform.IsChildOf(mapRoot.transform))
                TileError = $"{details.tileset.gameObject.name}: HTTP {details.httpStatusCode}: {details.message}";
        }

        public void Clear()
        {
            if (mapRoot != null) Destroy(mapRoot);
            mapRoot = null;
            georeference = null;
            markers.Clear();
            tileNodes.Clear();
            TilesetCount = 0;
            TileError = null;
            Attribution = null;
        }

        public void ClearMarkers()
        {
            foreach (MapMarker marker in markers)
                if (marker.Transform != null) Destroy(marker.Transform.gameObject);
            markers.Clear();
        }

        public void Load(string baseUrl, MapResponse data)
        {
            if (data?.Center == null || data.Center.Length != 2 ||
                data.Center[0] < -180 || data.Center[0] > 180 ||
                data.Center[1] < -90 || data.Center[1] > 90)
                throw new ArgumentException("Карта API не содержит допустимый центр [lon,lat]");

            TileError = null;
            var attributions = new List<string>();

            if (mapRoot == null)
            {
                longitude = data.Center[0];
                latitude = data.Center[1];
                mapRoot = new GameObject("Novostroy Georeference");
                georeference = mapRoot.AddComponent<CesiumGeoreference>();
                georeference.SetOriginLongitudeLatitudeHeight(longitude, latitude, 0);
                mapCamera = Camera.main;
                if (mapCamera == null)
                {
                    var cameraObject = new GameObject("Main Camera");
                    cameraObject.tag = "MainCamera";
                    mapCamera = cameraObject.AddComponent<Camera>();
                    cameraObject.AddComponent<AudioListener>();
                }
                mapCamera.transform.SetParent(mapRoot.transform, false);
                mapCamera.nearClipPlane = 1f;
                mapCamera.farClipPlane = 200000f;
                mapCamera.fieldOfView = 55f;
                mapCamera.clearFlags = CameraClearFlags.SolidColor;
                mapCamera.backgroundColor = new Color(0.075f, 0.11f, 0.16f);
                PositionCamera();
            }

            var wanted = new HashSet<string>(StringComparer.Ordinal);
            List<Tileset> tilesets = data.Tilesets ?? new List<Tileset>();
            int tileCount = Math.Max(1, tilesets.Count);
            long[] cacheBytes = TileBudget.Allocate(tilesets.Select(item => item?.Kind).ToArray());
            int loadsPerTileset = Math.Min(4, Math.Max(1, 8 / tileCount));
            for (int index = 0; index < tilesets.Count; index++)
            {
                Tileset item = tilesets[index];
                if (item == null) continue;
                try
                {
                    if (string.IsNullOrWhiteSpace(item.Attribution))
                        throw new ArgumentException("Нет атрибуции для слоя " + (item.Id ?? item.Kind));
                    string url = ApiAddress.Tile(baseUrl, item.Url);
                    string key = (item.Id ?? item.Url) + "|" + url;
                    wanted.Add(key);
                    if (!attributions.Contains(item.Attribution))
                        attributions.Add(item.Attribution);
                    if (tileNodes.TryGetValue(key, out GameObject existing))
                    {
                        Cesium3DTileset existingTileset = existing.GetComponent<Cesium3DTileset>();
                        existingTileset.maximumCachedBytes = cacheBytes[index];
                        existingTileset.maximumSimultaneousTileLoads = loadsPerTileset;
                        continue;
                    }
                    GameObject node = new GameObject("Tileset " + (item.Id ?? item.Kind));
                    node.SetActive(false);
                    node.transform.SetParent(mapRoot.transform, false);
                    Cesium3DTileset tileset = node.AddComponent<Cesium3DTileset>();
                    tileset.tilesetSource = CesiumDataSource.FromUrl;
                    tileset.url = url;
                    float dpr = Screen.dpi > 0 ? Screen.dpi / 160f : Screen.width / 430f;
                    tileset.maximumScreenSpaceError = 24f * Mathf.Clamp(dpr, 1f, 3f);
                    tileset.createPhysicsMeshes = false;
                    tileset.maximumCachedBytes = cacheBytes[index];
                    tileset.maximumSimultaneousTileLoads = loadsPerTileset;
                    tileNodes[key] = node;
                    node.SetActive(true);
                }
                catch (ArgumentException ex)
                {
                    TileError = ex.Message;
                }
            }
            Attribution = string.Join(" · ", attributions);
            foreach (string key in new List<string>(tileNodes.Keys))
            {
                if (wanted.Contains(key)) continue;
                tileNodes[key].SetActive(false);
                Destroy(tileNodes[key]);
                tileNodes.Remove(key);
            }
            TilesetCount = tileNodes.Count;
            if (TilesetCount == 0 && TileError == null)
                TileError = "API не вернул 3D Tiles для карты";

            ClearMarkers();
            if (data.Complexes != null)
            {
                foreach (MapComplex complex in data.Complexes)
                {
                    if (complex?.Location == null || complex.Location.Length != 2 ||
                        complex.Location[0] < -180 || complex.Location[0] > 180 ||
                        complex.Location[1] < -90 || complex.Location[1] > 90 ||
                        string.IsNullOrWhiteSpace(complex.ComplexId)) continue;
                    var marker = GameObject.CreatePrimitive(PrimitiveType.Sphere);
                    marker.name = "ЖК " + complex.Name;
                    marker.transform.SetParent(mapRoot.transform, false);
                    marker.transform.localScale = Vector3.one * 70f;
                    var anchor = marker.AddComponent<CesiumGlobeAnchor>();
                    anchor.SetPositionLongitudeLatitudeHeight(complex.Location[0], complex.Location[1], 90);
                    var renderer = marker.GetComponent<Renderer>();
                    renderer.material.color = complex.ModelQuality == "licensed_facade"
                        ? new Color(0.15f, 0.85f, 0.64f)
                        : new Color(1f, 0.68f, 0.25f);
                    markers.Add(new MapMarker { Complex = complex, Transform = marker.transform });
                }
            }

            if (FindAnyObjectByType<Light>() == null)
            {
                var light = new GameObject("Sun").AddComponent<Light>();
                light.type = LightType.Directional;
                light.intensity = 1.2f;
                light.transform.rotation = Quaternion.Euler(45f, -30f, 0f);
            }
        }

        public void Focus(MapComplex complex)
        {
            if (complex?.Location == null || complex.Location.Length != 2 || georeference == null) return;
            longitude = complex.Location[0];
            latitude = complex.Location[1];
            cameraDistance = 1800f;
            georeference.SetOriginLongitudeLatitudeHeight(longitude, latitude, 0);
            PositionCamera();
        }

        private void PositionCamera()
        {
            if (mapCamera == null) return;
            mapCamera.transform.localPosition = new Vector3(0, cameraDistance * 0.85f, -cameraDistance);
            mapCamera.transform.LookAt(mapRoot.transform.position);
        }

        private void Update()
        {
            if (georeference == null || Input.touchCount == 0) return;
            if (Input.touchCount == 1)
            {
                Touch touch = Input.GetTouch(0);
                if (touch.phase != TouchPhase.Moved || !WithinMap(touch.position)) return;
                double metersPerPixel = cameraDistance * 1.3 / Math.Max(1, Screen.height);
                double latRadians = latitude * Math.PI / 180.0;
                longitude -= touch.deltaPosition.x * metersPerPixel /
                    (111320.0 * Math.Max(0.2, Math.Cos(latRadians)));
                latitude += touch.deltaPosition.y * metersPerPixel / 111320.0;
                latitude = Math.Max(-85, Math.Min(85, latitude));
                georeference.SetOriginLongitudeLatitudeHeight(longitude, latitude, 0);
            }
            else if (Input.touchCount == 2)
            {
                Touch a = Input.GetTouch(0);
                Touch b = Input.GetTouch(1);
                float before = Vector2.Distance(a.position - a.deltaPosition, b.position - b.deltaPosition);
                float after = Vector2.Distance(a.position, b.position);
                if (before > 0 && after > 0)
                {
                    cameraDistance = Mathf.Clamp(cameraDistance * before / after, 300, 25000);
                    PositionCamera();
                }
            }
        }

        private bool WithinMap(Vector2 screenPosition) =>
            screenPosition.y > BottomUiPixels && screenPosition.y < Screen.height - TopUiPixels;
    }
}
