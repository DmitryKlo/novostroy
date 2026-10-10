using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Security.Cryptography;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using Novostroy.Domain;
using UnityEngine;

namespace Novostroy.Runtime
{
    public sealed class NovostroyApp : MonoBehaviour
    {
        private enum Page { Map, Filters, Offers, Compare, Recommendations, Favorites, Settings, ComplexDetail, Data }

        private const string ApiKey = "novostroy.api_url";
        private const string FavoritesKey = "novostroy.favorites";
        private readonly SelectionState selection = new SelectionState();
        private readonly HashSet<string> districtIds = new HashSet<string>(StringComparer.Ordinal);
        private readonly Dictionary<string, Offer> offerCache = new Dictionary<string, Offer>(StringComparer.Ordinal);
        private readonly Dictionary<string, MapComplex> complexes = new Dictionary<string, MapComplex>(StringComparer.Ordinal);
        private readonly Dictionary<string, PredicateInput> criterionInputs =
            new Dictionary<string, PredicateInput>(StringComparer.Ordinal);
        private List<Offer> offers = new List<Offer>();
        private List<Offer> recommendations = new List<Offer>();
        private List<District> districts = new List<District>();
        private List<CriterionDescriptor> criteria = new List<CriterionDescriptor>();
        private CompareResponse comparison;
        private DataCoverageResponse dataCoverage;
        private ComplexDetail complexDetail;
        private MapComplex selectedComplex;
        private Offer selectedOffer;
        private ApiClient api;
        private MapRuntime map;
        private Page page = Page.Map;
        private string dataMode;
        private string catalogView;
        private string snapshotId;
        private string apiInput;
        private string queryInput = "";
        private string minInput = "";
        private string maxInput = "";
        private string status = "Укажите адрес API в настройках";
        private string clarification;
        private string criteriaError;
        private string coverageError;
        private string criteriaGroup = "property";
        private string sessionId;
        private bool busy;
        private bool coverageBusy;
        private int coverageRevision;
        private int coverageDatasetIndex;
        private int searchRevision;
        private int mapRevision;
        private Vector2 scroll;
        private Vector2 compareScroll;
        private Font russianFont;

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
        private static void StartApp()
        {
            if (FindAnyObjectByType<NovostroyApp>() != null) return;
            var app = new GameObject("Novostroy App");
            DontDestroyOnLoad(app);
            app.AddComponent<NovostroyApp>();
        }

        private void Awake()
        {
            map = gameObject.AddComponent<MapRuntime>();
            byte[] random = new byte[32];
            using (RandomNumberGenerator generator = RandomNumberGenerator.Create())
                generator.GetBytes(random);
            sessionId = Convert.ToBase64String(random).TrimEnd('=').Replace('+', '-').Replace('/', '_');
            russianFont = Resources.Load<Font>("NotoSans-Regular");
            string favoritesJson = PlayerPrefs.GetString(FavoritesKey, "[]");
            try { selection.RestoreFavorites(JsonConvert.DeserializeObject<string[]>(favoritesJson)); }
            catch (JsonException) { selection.RestoreFavorites(Array.Empty<string>()); }
            apiInput = PlayerPrefs.GetString(ApiKey, "");
#if UNITY_EDITOR
            if (string.IsNullOrWhiteSpace(apiInput)) apiInput = "http://127.0.0.1:8000";
#endif
            if (!string.IsNullOrWhiteSpace(apiInput)) Connect();
            else page = Page.Settings;
        }

        private void Connect()
        {
            coverageRevision++;
            dataCoverage = null;
            coverageError = null;
            coverageBusy = false;
            try
            {
                api = new ApiClient(apiInput);
                PlayerPrefs.SetString(ApiKey, api.BaseUrl);
                PlayerPrefs.Save();
            }
            catch (ArgumentException ex)
            {
                api = null;
                map.Clear();
                dataMode = null;
                catalogView = null;
                snapshotId = null;
                status = ex.Message;
                page = Page.Settings;
                return;
            }
            dataMode = null;
            catalogView = null;
            snapshotId = null;
            selectedComplex = null;
            selectedOffer = null;
            map.Clear();
            offers.Clear();
            recommendations.Clear();
            offerCache.Clear();
            complexes.Clear();
            districts.Clear();
            criteria.Clear();
            criterionInputs.Clear();
            criteriaError = null;
            comparison = null;
            status = "Подключение к API…";
            busy = true;
            StartCoroutine(api.Get<HealthResponse>("/v1/health", health =>
            {
                busy = false;
                dataMode = health.DataMode;
                catalogView = health.CatalogView;
                snapshotId = health.SnapshotId;
                coverageDatasetIndex = dataMode == "real" ? 1 : 0;
                if (IsRegistryCatalog())
                {
                    status = catalogView == "developer_buildings"
                        ? "Откройте веб-версию для просмотра корпусов из каталога квартир. Текущее наличие и цены не подтверждены."
                        : "Откройте веб-версию для просмотра реестра зданий. Квартиры и цены в выгрузке отсутствуют.";
                    page = Page.Settings;
                    return;
                }
                if (catalogView != null && catalogView != "demo")
                {
                    status = "API вернул неизвестный вид каталога";
                    page = Page.Settings;
                    return;
                }
                if (dataMode != "demo" && dataMode != "real")
                {
                    status = "API вернул неизвестный режим данных";
                    return;
                }
                status = "Загрузка карты и предложений…";
                LoadDistricts();
                LoadCriteria();
                Search(false);
            }, Fail));
        }

        private bool IsRegistryCatalog() =>
            catalogView == "building_registry" || catalogView == "developer_buildings";

        private void LoadMap()
        {
            string path;
            try { path = MapPath(); }
            catch (ArgumentException ex) { status = ex.Message; return; }
            map.ClearMarkers();
            complexes.Clear();
            selectedComplex = null;
            complexDetail = null;
            int revision = ++mapRevision;
            StartCoroutine(api.Get<MapResponse>(path, response =>
            {
                if (revision != mapRevision) return;
                if (response.DataMode != dataMode || response.SnapshotId != snapshotId)
                {
                    status = "Карта и каталог относятся к разным снимкам данных";
                    return;
                }
                try
                {
                    map.Load(api.BaseUrl, response);
                    foreach (MapComplex item in response.Complexes ?? new List<MapComplex>())
                        if (!string.IsNullOrWhiteSpace(item?.ComplexId)) complexes[item.ComplexId] = item;
                    status = response.Clarification?.Message ?? map.TileError ??
                        $"Карта: {map.TilesetCount} набор(ов) 3D Tiles";
                    if (page == Page.Map) SendEvent("map_view");
                }
                catch (ArgumentException ex) { status = ex.Message; }
            }, message => { if (revision == mapRevision) Fail(message); }));
        }

        private string MapPath()
        {
            long? minimum = FilterRules.ParseRubles(minInput);
            long? maximum = FilterRules.ParseRubles(maxInput);
            FilterRules.ValidateRange(minimum, maximum);
            var parts = new List<string>();
            if (minimum.HasValue) parts.Add("budget_min_rub=" + minimum.Value);
            if (maximum.HasValue) parts.Add("budget_max_rub=" + maximum.Value);
            foreach (string id in districtIds.OrderBy(x => x))
                parts.Add("district_ids=" + Uri.EscapeDataString(id));
            if (!string.IsNullOrWhiteSpace(queryInput))
                parts.Add("query=" + Uri.EscapeDataString(queryInput.Trim()));
            object[] predicates = PredicatePayload();
            if (predicates.Length > 0)
                parts.Add("predicates=" + Uri.EscapeDataString(JsonConvert.SerializeObject(predicates)));
            return "/v1/map" + (parts.Count == 0 ? "" : "?" + string.Join("&", parts));
        }

        private void LoadCriteria()
        {
            StartCoroutine(api.Get<CriteriaResponse>("/v1/criteria", response =>
            {
                if (response.Version != "1.1" || response.DataMode != dataMode ||
                    response.SnapshotId != snapshotId || response.Criteria == null)
                {
                    criteriaError = "Каталог критериев не совпадает со снимком API";
                    return;
                }
                criteria = response.Criteria;
                criterionInputs.Clear();
                foreach (CriterionDescriptor item in criteria)
                {
                    if (string.IsNullOrWhiteSpace(item?.Key) || !item.Filterable) continue;
                    criterionInputs[item.Key] = new PredicateInput
                    {
                        Key = item.Key,
                        ValueType = item.ValueType,
                        Operators = item.Operators ?? new List<string>(),
                        EnumValues = item.EnumValues ?? new List<string>()
                    };
                }
                criteriaError = null;
            }, message => criteriaError = message));
        }

        private void LoadCoverage()
        {
            if (api == null) { page = Page.Settings; return; }
            page = Page.Data;
            int revision = ++coverageRevision;
            dataCoverage = null;
            coverageError = null;
            coverageBusy = true;
            StartCoroutine(api.Get<DataCoverageResponse>("/v1/data-coverage", result =>
            {
                if (revision != coverageRevision) return;
                coverageBusy = false;
                if (result.SnapshotId != snapshotId || result.DataMode != dataMode ||
                    !ValidCoverage(result))
                {
                    coverageError = "Ответ о данных не соответствует текущему снимку или схеме API";
                    return;
                }
                dataCoverage = result;
            }, message =>
            {
                if (revision != coverageRevision) return;
                coverageBusy = false;
                coverageError = message;
            }));
        }

        private static bool ValidCoverage(DataCoverageResponse result)
        {
            if (result == null || result.CriteriaTotal < 0 ||
                !DateTimeOffset.TryParse(result.GeneratedAt, CultureInfo.InvariantCulture,
                    DateTimeStyles.AssumeUniversal, out _) || result.Datasets == null ||
                !CoverageRules.ValidDatasetIds(result.Datasets.Select(item => item?.DatasetId)))
                return false;
            foreach (CoverageDataset dataset in result.Datasets)
            {
                if (dataset == null || !CoverageRules.ValidDatasetMode(dataset.DatasetId, dataset.DataMode) ||
                    dataset.RecordsTotal < 0 || string.IsNullOrWhiteSpace(dataset.Status) ||
                    dataset.Fields == null || dataset.Distributions == null ||
                    ((dataset.DatasetId == "city_geometry" || dataset.DatasetId == "building_registry") &&
                     (dataset.ComplexesTotal.HasValue || dataset.Distributions.Count != 0)) ||
                    (dataset.DatasetId == "developer_building_registry" &&
                     (!dataset.ComplexesTotal.HasValue || dataset.ComplexesTotal.Value < 0 ||
                      dataset.ComplexesTotal.Value > dataset.RecordsTotal || dataset.Distributions.Count != 0)) ||
                    (dataset.DatasetId != "city_geometry" && dataset.DatasetId != "building_registry" &&
                     dataset.DatasetId != "developer_building_registry" &&
                     (!dataset.ComplexesTotal.HasValue || dataset.ComplexesTotal.Value < 0 ||
                      dataset.ComplexesTotal.Value > dataset.RecordsTotal)))
                    return false;
                foreach (CoverageField field in dataset.Fields)
                {
                    CoverageQualityCounts quality = field?.QualityCounts;
                    if (string.IsNullOrWhiteSpace(field?.Key) || quality == null ||
                        !CoverageRules.ValidField(dataset.RecordsTotal, field.PopulatedCount,
                            field.UnknownCount, field.StaleCount, new[]
                            { quality.Observed, quality.Derived, quality.Estimated,
                              quality.Illustrative, quality.RenderFallback })) return false;
                }
                if (dataset.Distributions.Count > 2) return false;
                foreach (CoverageDistribution distribution in dataset.Distributions)
                {
                    if (distribution?.Bins == null || distribution.Bins.Any(bin => bin == null) ||
                        !CoverageRules.ValidDistribution(dataset.RecordsTotal,
                            distribution.PopulationCount, distribution.SampleCount,
                            distribution.MissingCount, distribution.ExcludedCount,
                            distribution.Bins.Select(bin => bin.Count)) ||
                        distribution.Bins.Any(bin => bin.Lower > bin.Upper) ||
                        (distribution.Key == "offer.price_total_rub" &&
                         (distribution.Unit != "RUB" || distribution.Basis != "asking_total")) ||
                        (distribution.Key == "unit.area_sqm" &&
                         (distribution.Unit != "m²" || distribution.Basis != "total_area")) ||
                        (distribution.Key != "offer.price_total_rub" &&
                         distribution.Key != "unit.area_sqm")) return false;
                }
            }
            return true;
        }

        private object[] PredicatePayload() => PredicateBuilder.Build(
            criteria.Where(item => item.Filterable && criterionInputs.ContainsKey(item.Key))
                .Select(item => criterionInputs[item.Key]))
            .Select(item => (object)new { key = item.Key, op = item.Op, value = item.Value })
            .ToArray();

        private void LoadDistricts()
        {
            StartCoroutine(api.Get<DistrictResponse>("/v1/districts", response =>
            {
                if (response.DataMode != dataMode) { status = "Районы относятся к другому режиму данных"; return; }
                districts = response.Items ?? new List<District>();
            }, Fail));
        }

        private object BuildRequest()
        {
            long? minimum = FilterRules.ParseRubles(minInput);
            long? maximum = FilterRules.ParseRubles(maxInput);
            FilterRules.ValidateRange(minimum, maximum);
            var filters = new Dictionary<string, object> { ["limit"] = 50 };
            if (minimum.HasValue) filters["budget_min_rub"] = minimum.Value;
            if (maximum.HasValue) filters["budget_max_rub"] = maximum.Value;
            if (districtIds.Count > 0) filters["district_ids"] = districtIds.OrderBy(x => x).ToArray();
            object[] predicates = PredicatePayload();
            if (predicates.Length > 0) filters["predicates"] = predicates;
            return new Dictionary<string, object>
            {
                ["query"] = string.IsNullOrWhiteSpace(queryInput) ? null : queryInput.Trim(),
                ["filters"] = filters
            };
        }

        private void Search(bool showOffers)
        {
            if (api == null) { page = Page.Settings; return; }
            object request;
            try { request = BuildRequest(); }
            catch (ArgumentException ex) { status = ex.Message; return; }
            if (showOffers) page = Page.Offers;
            if (showOffers) SendEvent("search");
            int revision = ++searchRevision;
            offers = new List<Offer>();
            selectedOffer = null;
            clarification = null;
            LoadMap();
            status = "Поиск…";
            busy = true;
            StartCoroutine(api.Post<SearchResponse>("/v1/search", request, result =>
            {
                if (revision != searchRevision) return;
                busy = false;
                if (!AcceptSearch(result)) return;
                offers = result.Items ?? new List<Offer>();
                Cache(offers);
                clarification = result.Clarification?.Message;
                status = clarification ?? (offers.Count == 0 ? "По заданным условиям ничего не найдено" :
                    $"Найдено {result.Count} предложений, показано {offers.Count}");
            }, message => { if (revision == searchRevision) Fail(message); }));
        }

        private bool AcceptSearch(SearchResponse result)
        {
            if (result.DataMode == dataMode && result.SnapshotId == snapshotId) return true;
            status = "Ответ относится к другому снимку данных. Обновите API.";
            return false;
        }

        private void GetRecommendations()
        {
            if (api == null) { page = Page.Settings; return; }
            object request;
            try { request = BuildRequest(); }
            catch (ArgumentException ex) { status = ex.Message; return; }
            page = Page.Recommendations;
            SendEvent("recommendation_view");
            int revision = ++searchRevision;
            recommendations = new List<Offer>();
            selectedOffer = null;
            clarification = null;
            LoadMap();
            status = "Подбор рекомендаций…";
            busy = true;
            StartCoroutine(api.Post<SearchResponse>("/v1/recommendations", request, result =>
            {
                if (revision != searchRevision) return;
                busy = false;
                if (!AcceptSearch(result)) return;
                recommendations = result.Items ?? new List<Offer>();
                Cache(recommendations);
                clarification = result.Clarification?.Message;
                status = clarification ?? (recommendations.Count == 0 ? "Подходящих рекомендаций нет" :
                    $"Рекомендаций: {recommendations.Count}");
            }, message => { if (revision == searchRevision) Fail(message); }));
        }

        private void Compare()
        {
            page = Page.Compare;
            if (!selection.CanCompare || api == null) return;
            SendEvent("compare");
            string[] selectedIds = selection.CompareIds.ToArray();
            status = "Сравнение…";
            busy = true;
            StartCoroutine(api.Post<CompareResponse>("/v1/compare",
                new { offer_ids = selectedIds }, result =>
                {
                    busy = false;
                    if (result.DataMode != dataMode || result.SnapshotId != snapshotId ||
                        result.Items == null || result.Items.Count != selectedIds.Length ||
                        !result.Items.Select(item => item?.OfferId).SequenceEqual(selectedIds) ||
                        !selection.CompareIds.SequenceEqual(selectedIds))
                    {
                        status = "Сравнение API не соответствует выбранным предложениям";
                        comparison = null;
                        return;
                    }
                    comparison = result;
                    Cache(result.Items);
                    status = "Сравнение готово";
                }, Fail));
        }

        private void SelectComplex(MapComplex complex)
        {
            selectedComplex = complex;
            complexDetail = null;
            map.Focus(complex);
            SendEvent("search");
            SearchComplex(complex.ComplexId);
            StartCoroutine(api.Get<ComplexDetail>("/v1/complexes/" + Uri.EscapeDataString(complex.ComplexId),
                detail =>
                {
                    if (selectedComplex?.ComplexId == complex.ComplexId &&
                        detail.ComplexId == complex.ComplexId) complexDetail = detail;
                }, Fail));
        }

        private void SearchComplex(string complexId)
        {
            object request;
            try
            {
                var payload = (Dictionary<string, object>)BuildRequest();
                var filters = (Dictionary<string, object>)payload["filters"];
                filters["complex_ids"] = new[] { complexId };
                filters["limit"] = 100;
                request = payload;
            }
            catch (ArgumentException ex) { status = ex.Message; return; }
            int revision = ++searchRevision;
            offers = new List<Offer>();
            selectedOffer = null;
            clarification = null;
            status = "Поиск в выбранном ЖК…";
            busy = true;
            StartCoroutine(api.Post<SearchResponse>("/v1/search", request, result =>
            {
                if (revision != searchRevision) return;
                busy = false;
                if (!AcceptSearch(result)) return;
                offers = result.Items ?? new List<Offer>();
                Cache(offers);
                clarification = result.Clarification?.Message;
                status = clarification ?? (offers.Count == 0 ? "В этом ЖК по заданным условиям предложений нет" :
                    $"В ЖК найдено {result.Count} предложений, показано {offers.Count}");
            }, message => { if (revision == searchRevision) Fail(message); }));
        }

        private void SelectOffer(Offer offer)
        {
            selectedOffer = offer;
            int revision = searchRevision;
            SendEvent("offer_view");
            StartCoroutine(api.Get<Offer>("/v1/offers/" + Uri.EscapeDataString(offer.OfferId),
                detail =>
                {
                    if (revision != searchRevision || selectedOffer?.OfferId != offer.OfferId ||
                        detail.OfferId != offer.OfferId) return;
                    selectedOffer = detail;
                    Cache(new[] { detail });
                }, Fail));
        }

        private void Cache(IEnumerable<Offer> items)
        {
            foreach (Offer offer in items)
                if (!string.IsNullOrWhiteSpace(offer?.OfferId)) offerCache[offer.OfferId] = offer;
        }

        private void ShowFavorites()
        {
            if (api == null) { page = Page.Settings; return; }
            page = Page.Favorites;
            foreach (string id in selection.Favorites)
            {
                if (offerCache.ContainsKey(id)) continue;
                string copy = id;
                StartCoroutine(api.Get<Offer>("/v1/offers/" + Uri.EscapeDataString(copy),
                    item => Cache(new[] { item }), Fail));
            }
        }

        private void ToggleFavorite(Offer offer)
        {
            selection.ToggleFavorite(offer.OfferId);
            PlayerPrefs.SetString(FavoritesKey, JsonConvert.SerializeObject(selection.Favorites.ToArray()));
            PlayerPrefs.Save();
        }

        private void Fail(string message)
        {
            busy = false;
            status = message;
        }

        private void SendEvent(string eventType)
        {
            if (api == null || string.IsNullOrEmpty(sessionId)) return;
            // The server allowlist has only these five event names. Do not send
            // query text, project/offer IDs, device identifiers, or other properties.
            StartCoroutine(api.Post<EventAck>("/v1/events",
                new { event_type = eventType, session_id = sessionId },
                _ => { }, _ => { }));
        }

        private void OnGUI()
        {
            float scale = Screen.width / 430f;
            if (scale <= 0) return;
            GUI.matrix = Matrix4x4.Scale(new Vector3(scale, scale, 1));
            if (russianFont != null) GUI.skin.font = russianFont;
            GUI.skin.label.fontSize = 15;
            GUI.skin.button.fontSize = 15;
            GUI.skin.textField.fontSize = 16;
            float width = Screen.width / scale;
            float height = Screen.height / scale;
            float topInset = (Screen.height - Screen.safeArea.yMax) / scale;
            float bottomInset = Screen.safeArea.yMin / scale;
            float headerHeight = dataMode == "demo" && !IsRegistryCatalog() ? 118 : 88;
            map.TopUiPixels = (topInset + headerHeight) * scale;
            map.BottomUiPixels = (bottomInset + 64) * scale;

            if (page == Page.Map) DrawMapMarkers(scale, topInset + headerHeight, height - bottomInset - 64);
            DrawPanel(new Rect(0, topInset, width, headerHeight), new Color(0.09f, 0.16f, 0.22f, 0.97f));
            GUILayout.BeginArea(new Rect(12, topInset + 6, width - 24, headerHeight - 10));
            GUILayout.Label("НОВОСТРОЙ  ·  Москва", HeaderStyle());
            GUILayout.Label(map.TileError ?? status, SmallStyle());
            if (dataMode == "demo" && !IsRegistryCatalog())
            {
                GUILayout.Label("ДЕМО: цены и геометрия иллюстративные", DemoStyle());
            }
            GUILayout.EndArea();

            float bodyTop = topInset + headerHeight + 6;
            float bodyBottom = height - bottomInset - 70;
            if (IsRegistryCatalog())
            {
                DrawPanel(new Rect(0, bodyTop, width, height - bottomInset - bodyTop),
                    new Color(0.94f, 0.96f, 0.97f, 0.98f));
                GUILayout.BeginArea(new Rect(12, bodyTop + 8, width - 24,
                    height - bottomInset - bodyTop - 16));
                scroll = GUILayout.BeginScrollView(scroll);
                GUILayout.Label("Реестр новостроек", TitleStyle());
                GUILayout.Label("Этот мобильный клиент пока не показывает записи реестра. " +
                    "Для просмотра откройте веб-версию по адресу сервера.", InfoStyle());
                GUILayout.Label("Текущее наличие квартир и цены в этом клиенте не подтверждены.", InfoStyle());
                DrawSettings();
                GUILayout.EndScrollView();
                GUILayout.EndArea();
                return;
            }
            if (page != Page.Map)
            {
                DrawPanel(new Rect(0, bodyTop, width, bodyBottom - bodyTop), new Color(0.94f, 0.96f, 0.97f, 0.98f));
                GUILayout.BeginArea(new Rect(12, bodyTop + 8, width - 24, bodyBottom - bodyTop - 16));
                scroll = GUILayout.BeginScrollView(scroll);
                DrawPage();
                GUILayout.EndScrollView();
                GUILayout.EndArea();
            }
            else DrawMapCard(width, bodyBottom);

            DrawPanel(new Rect(0, height - bottomInset - 64, width, 64), new Color(0.09f, 0.16f, 0.22f, 0.98f));
            GUILayout.BeginArea(new Rect(8, height - bottomInset - 59, width - 16, 54));
            GUILayout.BeginHorizontal();
            if (GUILayout.Button("Карта", GUILayout.Height(48)))
            {
                if (page != Page.Map) SendEvent("map_view");
                page = Page.Map;
            }
            if (GUILayout.Button("Поиск", GUILayout.Height(48))) page = Page.Filters;
            if (GUILayout.Button("Список", GUILayout.Height(48))) page = Page.Offers;
            if (GUILayout.Button("Для вас", GUILayout.Height(48))) GetRecommendations();
            if (GUILayout.Button("Данные", GUILayout.Height(48))) LoadCoverage();
            GUILayout.EndHorizontal();
            GUILayout.EndArea();
        }

        private void DrawPage()
        {
            switch (page)
            {
                case Page.Filters: DrawFilters(); break;
                case Page.Offers: DrawOfferList(offers, "Предложения"); break;
                case Page.Recommendations: DrawOfferList(recommendations, "Рекомендации"); break;
                case Page.Favorites:
                    DrawOfferList(selection.Favorites.Select(id => offerCache.TryGetValue(id, out Offer item) ? item : null)
                        .Where(item => item != null).ToList(), "Избранное");
                    break;
                case Page.Settings: DrawSettings(); break;
                case Page.ComplexDetail: DrawComplexDetail(); break;
                case Page.Data: DrawDataCoverage(); break;
            }
        }

        private void DrawDataCoverage()
        {
            GUILayout.Label("Данные", TitleStyle());
            if (coverageBusy) { GUILayout.Label("Загрузка сведений…", InfoStyle()); return; }
            if (dataCoverage == null)
            {
                GUILayout.Label(coverageError ?? "Сведения пока недоступны.", InfoStyle());
                if (GUILayout.Button("Обновить", GUILayout.Height(42))) LoadCoverage();
                return;
            }

            GUILayout.Label("Обновлено: " + DateText(dataCoverage.GeneratedAt), InfoStyle());
            GUILayout.Label("Критериев в каталоге: " + CountText(dataCoverage.CriteriaTotal) +
                ". Это не число заполненных значений.", InfoStyle());
            coverageDatasetIndex = GUILayout.Toolbar(coverageDatasetIndex,
                new[] { "Демо", "Реальные", "Город" }, GUILayout.Height(42));
            coverageDatasetIndex = Mathf.Clamp(coverageDatasetIndex, 0, 2);
            string id = coverageDatasetIndex == 0 ? "demo_offers" :
                coverageDatasetIndex == 1 ? "real_current_offers" : "city_geometry";
            CoverageDataset dataset = dataCoverage.Datasets.First(item => item.DatasetId == id);
            GUILayout.Space(8);
            GUILayout.Label(DatasetTitle(id), TitleStyle());
            GUILayout.Label("Срез: " + DateText(dataset.SnapshotAt) +
                " · " + DatasetStatusText(dataset.Status), InfoStyle());
            GUILayout.Label((id == "city_geometry" ? "Зданий: " : "Предложений: ") +
                CountText(dataset.RecordsTotal), TitleStyle());
            if (dataset.ComplexesTotal.HasValue)
                GUILayout.Label("ЖК с предложениями: " + CountText(dataset.ComplexesTotal.Value));
            if (id == "real_current_offers" && dataset.RecordsTotal == 0)
                GUILayout.Label("Нет подтверждённых текущих предложений.", InfoStyle());
            if (id == "demo_offers")
                GUILayout.Label("Демо-цены и распределения иллюстративны; это не рынок.", InfoStyle());
            if (id == "city_geometry")
                GUILayout.Label("Оценённая и запасная высота служат только для отображения карты. " +
                    "Этажи и характеристики квартир из неё не выводятся.", InfoStyle());

            GUILayout.Space(12);
            GUILayout.Label("Заполненность полей", TitleStyle());
            foreach (CoverageField field in dataset.Fields)
            {
                GUILayout.BeginVertical(GUI.skin.box);
                GUILayout.Label(CoverageFieldTitle(field.Key), InfoStyle());
                GUILayout.Label("Есть: " + CountText(field.PopulatedCount) +
                    " · неизвестно: " + CountText(field.UnknownCount) +
                    " · устарело: " + CountText(field.StaleCount), InfoStyle());
                GUILayout.Label(QualityCountsText(field.QualityCounts), InfoStyle());
                GUILayout.EndVertical();
            }
            if (dataset.Fields.Count == 0)
                GUILayout.Label("Для этого набора поля не загружены.", InfoStyle());

            if (id != "city_geometry")
            {
                GUILayout.Space(12);
                GUILayout.Label("Распределения выбранного набора", TitleStyle());
                foreach (CoverageDistribution distribution in dataset.Distributions)
                    DrawDistribution(distribution);
                if (dataset.Distributions.Count == 0)
                    GUILayout.Label("Для этого набора распределений нет.", InfoStyle());
            }
            if (GUILayout.Button("Обновить данные", GUILayout.Height(42))) LoadCoverage();
        }

        private void DrawDistribution(CoverageDistribution distribution)
        {
            GUILayout.BeginVertical(GUI.skin.box);
            GUILayout.Label(distribution.Key == "offer.price_total_rub" ?
                "Запрашиваемая цена, ₽" : "Общая площадь, м²", TitleStyle());
            GUILayout.Label("В расчёте: " + CountText(distribution.SampleCount) +
                " · неизвестно: " + CountText(distribution.MissingCount) +
                " · исключено: " + CountText(distribution.ExcludedCount), InfoStyle());
            if (distribution.Key == "offer.price_total_rub")
                GUILayout.Label("Условные цены исключены из распределения запрашиваемых цен.",
                    InfoStyle());
            if (distribution.Bins.Count == 0)
                GUILayout.Label("Подтверждённых значений для графика нет.", InfoStyle());
            long maximum = distribution.Bins.Count == 0 ? 0 :
                distribution.Bins.Max(bin => bin.Count);
            foreach (CoverageBin bin in distribution.Bins)
            {
                string format = distribution.Unit == "RUB" ? "N0" : "0.##";
                string range = bin.Lower.ToString(format, CultureInfo.GetCultureInfo("ru-RU")) +
                    "–" + bin.Upper.ToString(format, CultureInfo.GetCultureInfo("ru-RU")) +
                    " " + (distribution.Unit == "RUB" ? "₽" : distribution.Unit) +
                    (bin.UpperInclusive ? " · до включительно" : " · верхняя граница не включена");
                GUILayout.Label(range + " · " + CountText(bin.Count), InfoStyle());
                Rect bar = GUILayoutUtility.GetRect(1, 12, GUILayout.ExpandWidth(true));
                DrawPanel(bar, new Color(0.82f, 0.87f, 0.9f));
                if (maximum > 0)
                    DrawPanel(new Rect(bar.x, bar.y,
                        bar.width * (float)((double)bin.Count / maximum), bar.height),
                        new Color(0.13f, 0.39f, 0.53f));
            }
            GUILayout.EndVertical();
        }

        private string CoverageFieldTitle(string key)
        {
            if (key == "city.footprint_geometry") return "Контур здания";
            if (key == "city.source_height_m") return "Высота в исходных городских данных";
            if (key == "city.source_num_floors") return "Этажность в исходных городских данных";
            if (key == "city.render_height_from_floors_m") return "Высота отображения из исходной этажности";
            if (key == "city.render_height_imputed_m") return "Высота отображения при пропуске данных";
            if (key == "city.render_height_m") return "Высота 3D-объёма на карте";
            CriterionDescriptor descriptor = criteria.FirstOrDefault(item => item.Key == key);
            return string.IsNullOrWhiteSpace(descriptor?.LabelRu) ? key : descriptor.LabelRu;
        }

        private static string DatasetTitle(string id) => id == "demo_offers" ?
            "Демонстрационные предложения" : id == "real_current_offers" ?
            "Подтверждённые текущие предложения" : "Геометрия города";

        private static string DatasetStatusText(string value) => value == "not_loaded" ?
            "не загружен" : value == "no_licensed_feed" ?
            "нет разрешённого источника предложений" : value == "illustrative" ?
            "иллюстративный набор" : value == "available" || value == "ready" ?
            "загружен" : "состояние: " + (value ?? "неизвестно");

        private static string CountText(long value) =>
            value.ToString("N0", CultureInfo.GetCultureInfo("ru-RU"));

        private static string QualityCountsText(CoverageQualityCounts quality)
        {
            var parts = new List<string>();
            if (quality.Observed > 0) parts.Add("по источнику " + CountText(quality.Observed));
            if (quality.Derived > 0) parts.Add("рассчитано " + CountText(quality.Derived));
            if (quality.Estimated > 0) parts.Add("оценка " + CountText(quality.Estimated));
            if (quality.Illustrative > 0) parts.Add("демо " + CountText(quality.Illustrative));
            if (quality.RenderFallback > 0)
                parts.Add("запасная геометрия " + CountText(quality.RenderFallback));
            return parts.Count == 0 ? "Заполненных значений нет." : string.Join(" · ", parts);
        }

        private void DrawFilters()
        {
            GUILayout.Label("Поиск и фильтры", TitleStyle());
            GUILayout.Label("Запрос на русском языке");
            queryInput = GUILayout.TextField(queryInput, GUILayout.Height(42));
            GUILayout.Label("Бюджет от, ₽");
            minInput = GUILayout.TextField(minInput, GUILayout.Height(42));
            GUILayout.Label("Бюджет до, ₽");
            maxInput = GUILayout.TextField(maxInput, GUILayout.Height(42));
            GUILayout.Space(10);
            GUILayout.Label("Районы", TitleStyle());
            foreach (District district in districts)
            {
                bool chosen = districtIds.Contains(district.DistrictId);
                bool next = GUILayout.Toggle(chosen, district.Name + " · " + district.BoundaryVersion,
                    GUILayout.Height(38));
                if (next && !chosen) districtIds.Add(district.DistrictId);
                else if (!next && chosen) districtIds.Remove(district.DistrictId);
            }
            if (districts.Count == 0) GUILayout.Label("Районы ещё не загружены.");
            GUILayout.Space(12);
            DrawCriteriaFilters();
            GUILayout.Space(12);
            if (GUILayout.Button("Найти предложения", GUILayout.Height(48))) Search(true);
            if (GUILayout.Button("Подобрать с объяснением", GUILayout.Height(48))) GetRecommendations();
            if (GUILayout.Button("Сбросить фильтры", GUILayout.Height(42)))
            {
                minInput = maxInput = queryInput = "";
                districtIds.Clear();
                foreach (PredicateInput input in criterionInputs.Values)
                {
                    input.Minimum = input.Maximum = input.Choice = input.Text = "";
                    input.BooleanChoice = -1;
                }
                Search(true);
            }
            GUILayout.Space(16);
            if (GUILayout.Button("Избранное", GUILayout.Height(42))) ShowFavorites();
            if (GUILayout.Button("Адрес API", GUILayout.Height(42))) page = Page.Settings;
        }

        private void DrawCriteriaFilters()
        {
            GUILayout.Label("Дополнительные критерии", TitleStyle());
            GUILayout.Label("Неизвестные и неподтверждённые значения не проходят строгий фильтр.",
                InfoStyle());
            if (criteria.Count == 0)
            {
                GUILayout.Label(criteriaError ?? "Каталог критериев загружается.", InfoStyle());
                return;
            }
            int groupIndex = criteriaGroup == "social" ? 1 : criteriaGroup == "environment" ? 2 : 0;
            groupIndex = GUILayout.Toolbar(groupIndex, new[] { "Квартира", "Рядом", "Среда" },
                GUILayout.Height(42));
            criteriaGroup = groupIndex == 1 ? "social" : groupIndex == 2 ? "environment" : "property";
            foreach (CriterionDescriptor item in criteria.Where(value => value.Group == criteriaGroup))
                DrawCriterionInput(item);
        }

        private void DrawCriterionInput(CriterionDescriptor item)
        {
            GUILayout.BeginVertical(GUI.skin.box);
            GUILayout.Label(string.IsNullOrWhiteSpace(item.LabelRu) ? item.Key : item.LabelRu,
                TitleStyle());
            GUILayout.Label(item.Key + " · " + CriterionAvailabilityText(item.Availability), InfoStyle());
            if (!item.Filterable || !criterionInputs.TryGetValue(item.Key, out PredicateInput input))
            {
                GUILayout.Label("Для этого поля поиск пока недоступен.", InfoStyle());
                GUILayout.EndVertical();
                return;
            }

            if (input.ValueType == "integer" || input.ValueType == "decimal" ||
                input.ValueType == "ratio")
            {
                if (input.Operators.Contains("min"))
                {
                    GUILayout.Label("От · " + item.Unit);
                    input.Minimum = GUILayout.TextField(input.Minimum, GUILayout.Height(40));
                }
                if (input.Operators.Contains("max"))
                {
                    GUILayout.Label("До · " + item.Unit);
                    input.Maximum = GUILayout.TextField(input.Maximum, GUILayout.Height(40));
                }
            }
            else if (input.ValueType == "boolean")
            {
                int current = input.BooleanChoice == 1 ? 1 : input.BooleanChoice == 0 ? 2 : 0;
                int selected = GUILayout.Toolbar(current, new[] { "Любое", "Да", "Нет" },
                    GUILayout.Height(40));
                input.BooleanChoice = selected == 1 ? 1 : selected == 2 ? 0 : -1;
            }
            else if (input.ValueType == "enum")
            {
                if (input.EnumValues.Count > 0)
                {
                    var options = new List<string> { "Любое" };
                    options.AddRange(input.EnumValues);
                    int current = Math.Max(0, options.IndexOf(input.Choice));
                    int selected = GUILayout.SelectionGrid(current, options.ToArray(), 3);
                    input.Choice = selected == 0 ? "" : options[selected];
                }
                else
                {
                    GUILayout.Label("Значение · из подтверждённого источника");
                    input.Choice = GUILayout.TextField(input.Choice, GUILayout.Height(40));
                }
            }
            else if (input.ValueType == "string" || input.ValueType == "string_array")
            {
                GUILayout.Label("Текст / элемент списка");
                input.Text = GUILayout.TextField(input.Text, GUILayout.Height(40));
            }
            GUILayout.EndVertical();
        }

        private static string CriterionAvailabilityText(string value)
        {
            switch (value)
            {
                case "blocked": return "источник пока не готов";
                case "conditional": return "при наличии разрешённых данных";
                case "source_dependent": return "зависит от покрытия источника";
                default: return "покрытие уточняется";
            }
        }

        private void DrawOfferList(IReadOnlyList<Offer> items, string title)
        {
            GUILayout.Label(title, TitleStyle());
            GUILayout.BeginHorizontal();
            if (page != Page.Favorites && GUILayout.Button("Избранное", GUILayout.Height(40))) ShowFavorites();
            GUILayout.EndHorizontal();
            if (!string.IsNullOrWhiteSpace(clarification)) GUILayout.Label(clarification);
            if (busy && items.Count == 0) GUILayout.Label("Загрузка…");
            else if (items.Count == 0) GUILayout.Label("Нет предложений для показа.");
            foreach (Offer offer in items) DrawOfferCard(offer);
        }

        private void DrawOfferCard(Offer offer)
        {
            GUILayout.BeginVertical(GUI.skin.box);
            string complexName = complexes.TryGetValue(offer.ComplexId ?? "", out MapComplex value)
                ? value.Name : offer.ComplexId;
            GUILayout.Label(complexName ?? "ЖК без названия", TitleStyle());
            GUILayout.Label($"{offer.Rooms?.ToString() ?? "?"} комн. · {offer.AreaSqm?.ToString("0.#", CultureInfo.InvariantCulture) ?? "?"} м² · этаж {offer.Floor?.ToString() ?? "?"}");
            GUILayout.Label(PriceText(offer));
            GUILayout.Label("Качество данных: " + QualityText(offer.Source?.Quality));
            GUILayout.Label("Дата сведений: " + DateText(offer.Source?.ObservedAt));
            GUILayout.Label("Статус: " + AvailabilityText(offer.Availability));
            if (offer.Recommendation != null)
            {
                foreach (string reason in offer.Recommendation.Explanation ?? new List<string>())
                    GUILayout.Label("• " + reason);
                GUILayout.Label("Покрытие: " + (offer.Recommendation.Coverage?.Status ?? "неизвестно"));
            }
            GUILayout.BeginHorizontal();
            if (GUILayout.Button(selection.IsFavorite(offer.OfferId) ? "★" : "☆", GUILayout.Height(40)))
                ToggleFavorite(offer);
            if (GUILayout.Button("На карте", GUILayout.Height(40)) &&
                complexes.TryGetValue(offer.ComplexId ?? "", out MapComplex complex))
            {
                page = Page.Map;
                SendEvent("map_view");
                SelectComplex(complex);
            }
            GUILayout.EndHorizontal();
            if (GUILayout.Button("Подробнее", GUILayout.Height(38))) SelectOffer(offer);
            if (selectedOffer?.OfferId == offer.OfferId)
            {
                GUILayout.Label("ID: " + selectedOffer.OfferId + " · unit ID: " +
                    (selectedOffer.UnitId ?? "не указан"));
                DrawFacts(selectedOffer.Facts);
            }
            GUILayout.EndVertical();
            GUILayout.Space(6);
        }

        private void DrawComparison()
        {
            GUILayout.Label("Сравнение 2–4 предложений", TitleStyle());
            GUILayout.Label("Выбрано: " + selection.CompareIds.Count);
            if (!selection.CanCompare)
            {
                GUILayout.Label("Выберите ещё предложения в списке.");
                if (GUILayout.Button("К предложениям", GUILayout.Height(42))) page = Page.Offers;
                return;
            }
            if (GUILayout.Button("Обновить сравнение", GUILayout.Height(42))) Compare();
            if (comparison?.Items == null) return;
            compareScroll = GUILayout.BeginScrollView(compareScroll, true, true, GUILayout.Height(420));
            GUILayout.BeginHorizontal();
            foreach (Offer offer in comparison.Items)
            {
                GUILayout.BeginVertical(GUI.skin.box, GUILayout.Width(225));
                GUILayout.Label(offer.OfferId, TitleStyle());
                GUILayout.Label(PriceText(offer));
                GUILayout.Label("Комнат: " + (offer.Rooms?.ToString() ?? "?"));
                GUILayout.Label("Площадь: " + (offer.AreaSqm?.ToString("0.#") ?? "?"));
                GUILayout.Label("Этаж: " + (offer.Floor?.ToString() ?? "?"));
                GUILayout.Label("Качество: " + QualityText(offer.Source?.Quality));
                GUILayout.Label("Наличие: " + AvailabilityText(offer.Availability));
                DrawFacts(offer.Facts);
                GUILayout.EndVertical();
            }
            GUILayout.EndHorizontal();
            GUILayout.EndScrollView();
        }

        private void DrawComplexDetail()
        {
            GUILayout.Label(complexDetail?.Name ?? selectedComplex?.Name ?? "ЖК", TitleStyle());
            if (complexDetail == null)
            {
                GUILayout.Label("Сведения ЖК загружаются или недоступны.", InfoStyle());
                return;
            }
            GUILayout.Label("Модель: " + ModelQualityText(complexDetail.ModelQuality));
            GUILayout.Label("Качество данных: " + QualityText(complexDetail.Source?.Quality));
            GUILayout.Label("Дата сведений: " + DateText(complexDetail.Source?.ObservedAt));
            DrawFacts(complexDetail.Facts);
            if (GUILayout.Button("Назад к карте", GUILayout.Height(42))) page = Page.Map;
        }

        private void DrawFacts(IDictionary<string, CriterionFact> facts)
        {
            GUILayout.Label("Сведения по критериям", TitleStyle());
            if (facts == null || facts.Count == 0)
            {
                GUILayout.Label("Подтверждённые значения по критериям не поступили.", InfoStyle());
                return;
            }
            foreach (KeyValuePair<string, CriterionFact> entry in facts.OrderBy(pair => pair.Key))
            {
                CriterionDescriptor descriptor = criteria.FirstOrDefault(item => item.Key == entry.Key);
                CriterionFact fact = entry.Value;
                GUILayout.BeginVertical(GUI.skin.box);
                GUILayout.Label(descriptor?.LabelRu ?? entry.Key, InfoStyle());
                GUILayout.Label(FactValueText(fact), InfoStyle());
                if (fact != null)
                {
                    GUILayout.Label("Статус: " + FactStatusText(fact.Status) +
                        " · дата: " + DateText(fact.ObservedAt), InfoStyle());
                    if (!string.IsNullOrWhiteSpace(fact.CoverageStatus))
                        GUILayout.Label("Покрытие: " + CoverageText(fact.CoverageStatus), InfoStyle());
                    if (fact.Approximate == true)
                        GUILayout.Label("Приближённое значение", InfoStyle());
                    if (!string.IsNullOrWhiteSpace(fact.Scope))
                        GUILayout.Label("Область значения: " + fact.Scope, InfoStyle());
                    if (!string.IsNullOrWhiteSpace(fact.Method))
                        GUILayout.Label("Метод: " + fact.Method, InfoStyle());
                }
                GUILayout.EndVertical();
            }
        }

        private static string FactValueText(CriterionFact fact)
        {
            if (fact == null || fact.Status == "unknown" || fact.Status == "stale" ||
                fact.Status == "conflict" || fact.Status == "not_applicable" ||
                fact.Value == null || fact.Value.Type == JTokenType.Null)
                return "Значение: нет данных";
            string value;
            bool ratioValue = false;
            if (fact.Value.Type == JTokenType.Boolean)
                value = fact.Value.Value<bool>() ? "Да" : "Нет";
            else if (fact.Value is JObject ratio &&
                ratio["numerator_rub"] != null && ratio["denominator_sqm"] != null)
            {
                value = ratio["numerator_rub"] + " ₽ / " + ratio["denominator_sqm"] + " м²";
                ratioValue = true;
            }
            else if (fact.Value.Type == JTokenType.String)
                value = fact.Value.Value<string>();
            else value = fact.Value.ToString(Formatting.None);
            string unit = ratioValue || string.IsNullOrWhiteSpace(fact.Unit) || fact.Unit == "boolean"
                ? "" : " " + fact.Unit;
            return "Значение: " + value + unit;
        }

        private static string FactStatusText(string status)
        {
            switch (status)
            {
                case "observed": return "сообщено источником";
                case "derived": return "рассчитано";
                case "estimated": return "оценка";
                case "illustrative": return "иллюстративно (демо)";
                case "stale": return "устарело";
                case "conflict": return "противоречие";
                case "not_applicable": return "не применимо";
                default: return "неизвестно";
            }
        }

        private static string CoverageText(string coverage)
        {
            switch (coverage)
            {
                case "complete": return "полное";
                case "partial": return "частичное";
                case "source_bounded": return "в пределах источника";
                default: return "не подтверждено (" + coverage + ")";
            }
        }

        private static string DateText(string observedAt)
        {
            if (DateTimeOffset.TryParse(observedAt, CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal, out DateTimeOffset date))
                return date.UtcDateTime.ToString("dd.MM.yyyy HH:mm 'UTC'", CultureInfo.GetCultureInfo("ru-RU"));
            return "не указана";
        }

        private void DrawSettings()
        {
            GUILayout.Label("Адрес сервера", TitleStyle());
            GUILayout.Label("Укажите http(s)://адрес:порт. На iPhone нужен адрес компьютера в одной сети, не localhost.");
            apiInput = GUILayout.TextField(apiInput ?? "", GUILayout.Height(44));
            if (GUILayout.Button("Подключить", GUILayout.Height(48))) Connect();
            GUILayout.Space(12);
            if (IsRegistryCatalog())
                GUILayout.Label(catalogView == "developer_buildings"
                    ? "Вид каталога: корпуса из каталога квартир" : "Вид каталога: реестр зданий");
            else
                GUILayout.Label("Режим: " + (dataMode ?? "нет соединения"));
            GUILayout.Label("Снимок: " + (snapshotId ?? "нет"));
            GUILayout.Label("Тайлы: " + map.TilesetCount);
            if (!string.IsNullOrWhiteSpace(map.TileError)) GUILayout.Label(map.TileError);
            if (!IsRegistryCatalog() && GUILayout.Button("К карте", GUILayout.Height(42)))
            {
                page = Page.Map;
                SendEvent("map_view");
            }
        }

        private void DrawMapMarkers(float scale, float top, float bottom)
        {
            Camera camera = map.MapCamera;
            if (camera == null) return;
            foreach (MapMarker marker in map.Markers)
            {
                Vector3 point = camera.WorldToScreenPoint(marker.Transform.position);
                if (point.z <= 0) continue;
                float x = point.x / scale;
                float y = (Screen.height - point.y) / scale;
                if (y < top || y > bottom) continue;
                string title = marker.Complex.Name ?? marker.Complex.ComplexId;
                float w = Mathf.Min(170, 50 + title.Length * 7);
                if (GUI.Button(new Rect(x - w / 2, y - 20, w, 40), title)) SelectComplex(marker.Complex);
            }
        }

        private void DrawMapCard(float width, float bodyBottom)
        {
            string attribution = map.Attribution ?? "";
            float cardBottom = bodyBottom;
            if (!string.IsNullOrWhiteSpace(attribution))
            {
                GUIStyle attributionStyle = AttributionStyle();
                float attributionHeight = attributionStyle.CalcHeight(
                    new GUIContent(attribution), width - 40) + 12;
                float attributionTop = bodyBottom - attributionHeight - 6;
                DrawPanel(new Rect(8, attributionTop, width - 16, attributionHeight),
                    new Color(0.09f, 0.16f, 0.22f, 0.92f));
                GUI.Label(new Rect(20, attributionTop + 6, width - 40,
                    attributionHeight - 12), attribution, attributionStyle);
                cardBottom = attributionTop - 6;
            }
            if (selectedComplex == null)
            {
                DrawPanel(new Rect(12, cardBottom - 54, width - 24, 54), new Color(0.09f, 0.16f, 0.22f, 0.9f));
                GUI.Label(new Rect(20, cardBottom - 50, width - 40, 46),
                    map.MapCamera == null ? "Подключите API для просмотра 3D карты" :
                    "Нажмите на ЖК · перетаскивание и масштабирование двумя пальцами", SmallStyle());
                return;
            }
            DrawPanel(new Rect(8, cardBottom - 166, width - 16, 160), new Color(0.09f, 0.16f, 0.22f, 0.95f));
            GUILayout.BeginArea(new Rect(18, cardBottom - 160, width - 36, 148));
            GUILayout.Label(selectedComplex.Name ?? selectedComplex.ComplexId, HeaderStyle());
            GUILayout.Label("Модель: " + ModelQualityText(selectedComplex.ModelQuality), SmallStyle());
            GUILayout.Label("Предложений по API: " + selectedComplex.OfferCount +
                (complexDetail?.Developer == null ? "" : " · " + complexDetail.Developer), SmallStyle());
            if (GUILayout.Button("Открыть найденные предложения", GUILayout.Height(36))) page = Page.Offers;
            if (GUILayout.Button("Сведения и качество ЖК", GUILayout.Height(36))) page = Page.ComplexDetail;
            GUILayout.EndArea();
        }

        private string PriceText(Offer offer)
        {
            if (offer.Price == null) return "Цена не указана";
            if (dataMode == "real" && (offer.Source?.Quality == "illustrative" ||
                offer.Source?.RecordMode == "demo")) return "Цена не подтверждена";
            string amount = offer.Price.AmountRub.ToString("N0", CultureInfo.GetCultureInfo("ru-RU"));
            string basis = offer.Price.Basis == "conditional_total" ? "условная цена" : "цена предложения";
            return $"{amount} ₽ · {basis}" + (dataMode == "demo" ? " · ДЕМО" : "");
        }

        private static string QualityText(string quality)
        {
            switch (quality)
            {
                case "illustrative": return "иллюстративные данные";
                case "dated_asking": return "цена на дату источника";
                default: return "качество не подтверждено";
            }
        }

        private static string AvailabilityText(string status) => status == "available" ? "указано доступно" :
            status == "unavailable" ? "указано недоступно" : "неизвестно";

        private static string ModelQualityText(string value) => value == "licensed_facade" ?
            "лицензированный фасад" : value == "synthetic_facade" ?
            "синтетический фасад" : value == "synthetic_massing" ?
            "условный объём здания" : "качество модели не подтверждено";

        private static void DrawPanel(Rect rect, Color color)
        {
            Color old = GUI.color;
            GUI.color = color;
            GUI.DrawTexture(rect, Texture2D.whiteTexture);
            GUI.color = old;
        }

        private static GUIStyle HeaderStyle() => new GUIStyle(GUI.skin.label)
        {
            fontSize = 19,
            fontStyle = FontStyle.Bold,
            normal = { textColor = Color.white }
        };

        private static GUIStyle SmallStyle() => new GUIStyle(GUI.skin.label)
        {
            fontSize = 13,
            normal = { textColor = Color.white }
        };

        private static GUIStyle DemoStyle() => new GUIStyle(GUI.skin.label)
        {
            fontSize = 14,
            fontStyle = FontStyle.Bold,
            normal = { textColor = new Color(1f, 0.76f, 0.24f) }
        };

        private static GUIStyle AttributionStyle() => new GUIStyle(GUI.skin.label)
        {
            fontSize = 11,
            wordWrap = true,
            normal = { textColor = new Color(0.8f, 0.85f, 0.89f) }
        };

        private static GUIStyle InfoStyle() => new GUIStyle(GUI.skin.label)
        {
            fontSize = 13,
            wordWrap = true
        };

        private static GUIStyle TitleStyle() => new GUIStyle(GUI.skin.label)
        {
            fontSize = 18,
            fontStyle = FontStyle.Bold
        };
    }
}
