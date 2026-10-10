using Novostroy.Domain;

static void Check(bool condition, string name)
{
    if (!condition) throw new Exception("FAILED: " + name);
    Console.WriteLine("PASS " + name);
}

static void Throws(Action action, string name)
{
    try { action(); }
    catch (ArgumentException) { Console.WriteLine("PASS " + name); return; }
    throw new Exception("FAILED: " + name);
}

Check(ApiAddress.Normalize(" http://192.168.1.20:8000/ ") == "http://192.168.1.20:8000",
    "LAN API origin");
Check(ApiAddress.Tile("https://example.test", "/assets/city/tileset.json") ==
    "https://example.test/assets/city/tileset.json", "shared tiles route");
Throws(() => ApiAddress.Tile("https://example.test", "https://evil.test/tileset.json"),
    "reject external tiles URL");
Throws(() => ApiAddress.Tile("https://example.test", "/assets/../v1/health"),
    "reject path traversal");
Throws(() => ApiAddress.Normalize("https://name:secret@example.test"),
    "reject credentials in API URL");
Check(FilterRules.ParseRubles("12 000 000") == 12000000L, "rubles input");
Check(FilterRules.ParseRubles(" ") == null, "empty budget");
Throws(() => FilterRules.ParseRubles("12млн"), "invalid budget");
Throws(() => FilterRules.ValidateRange(200, 100), "inverted budget");

var state = new SelectionState();
Check(!state.CanCompare, "compare requires two offers");
Check(state.ToggleCompare("a") && state.ToggleCompare("b") && state.CanCompare,
    "two offers can be compared");
Check(state.ToggleCompare("c") && state.ToggleCompare("d") && !state.ToggleCompare("e"),
    "compare capped at four");
Check(string.Join(",", state.CompareIds) == "a,b,c,d", "compare preserves selection order");
Check(state.ToggleCompare("b") && !state.IsCompared("b"), "compare removal");
Check(state.ToggleFavorite("a") && state.IsFavorite("a"), "favorite add");
Check(!state.ToggleFavorite("a") && !state.IsFavorite("a"), "favorite remove");
state.RestoreFavorites(new[] { "x", "x", " ", "y" });
Check(state.Favorites.Count == 2, "favorite restore deduplicates IDs");

var floor = new PredicateInput
{
    Key = "unit.floor", ValueType = "integer", Operators = new[] { "min", "max" },
    Minimum = "7", Maximum = "12"
};
var cctv = new PredicateInput
{
    Key = "building.cctv", ValueType = "boolean", Operators = new[] { "equals" },
    BooleanChoice = 0
};
var ratio = new PredicateInput
{
    Key = "offer.price_per_sqm_rub", ValueType = "ratio",
    Operators = new[] { "max" }, Maximum = "350000,25"
};
var predicates = PredicateBuilder.Build(new[] { floor, cctv, ratio });
Check(predicates.Count == 4 && predicates[0].Op == "min" &&
    predicates[1].Op == "max", "inclusive numeric range predicates");
Check(predicates[2].Value is bool value && !value, "explicit false is distinct from any");
Check((string)predicates[3].Value == "350000.25", "exact ratio threshold string");
cctv.BooleanChoice = -1;
Check(PredicateBuilder.Build(new[] { cctv }).Count == 0, "boolean any omits predicate");
floor.Minimum = "20";
Throws(() => PredicateBuilder.Build(new[] { floor }), "reject reversed criterion range");
floor.Minimum = "7";
floor.Operators = new[] { "min" };
Throws(() => PredicateBuilder.Build(new[] { floor }), "reject unsupported criterion operator");
var energy = new PredicateInput
{
    Key = "building.energy_class", ValueType = "enum",
    Operators = new[] { "equals" }, EnumValues = new[] { "A", "B" }, Choice = "C"
};
Throws(() => PredicateBuilder.Build(new[] { energy }), "reject enum outside registry");
energy.Choice = "B";
Check((string)PredicateBuilder.Build(new[] { energy })[0].Value == "B",
    "enum equality uses registry value");
var tooMany = Enumerable.Range(0, 25).Select(i => new PredicateInput
{
    Key = "key." + i, ValueType = "boolean", Operators = new[] { "equals" },
    BooleanChoice = 1
});
Throws(() => PredicateBuilder.Build(tooMany), "24-predicate contract limit");

Check(CoverageRules.ValidDatasetIds(new[]
    { "demo_offers", "real_current_offers", "city_geometry" }),
    "coverage keeps demo, real, and city separate");
Check(CoverageRules.ValidDatasetIds(new[]
    { "demo_offers", "real_current_offers", "city_geometry", "building_registry" }),
    "coverage accepts a separate building registry");
Check(CoverageRules.ValidDatasetIds(new[]
    { "demo_offers", "real_current_offers", "city_geometry", "developer_building_registry" }),
    "coverage accepts the developer building snapshot");
Check(!CoverageRules.ValidDatasetIds(new[]
    { "demo_offers", "real_current_offers", "city_geometry", "building_registry", "developer_building_registry" }),
    "coverage rejects mixed old and new registries");
Check(!CoverageRules.ValidDatasetIds(new[]
    { "demo_offers", "demo_offers", "city_geometry" }),
    "coverage rejects duplicate dataset");
Check(CoverageRules.ValidDatasetMode("city_geometry", "geospatial") &&
    CoverageRules.ValidDatasetMode("building_registry", "registry") &&
    CoverageRules.ValidDatasetMode("developer_building_registry", "registry") &&
    !CoverageRules.ValidDatasetMode("real_current_offers", "demo"),
    "coverage dataset modes stay distinct");
Check(CoverageRules.ValidField(10, 6, 3, 1, new long[] { 2, 1, 1, 2, 0 }),
    "field known unknown stale and quality totals");
Check(!CoverageRules.ValidField(10, 6, 3, 1, new long[] { 2, 1, 1, 1, 0 }),
    "field rejects missing quality count");
Check(CoverageRules.ValidDistribution(10, 10, 5, 5, 0,
    new long[] { 1, 2, 2 }), "distribution sample bins sum exactly");
Check(CoverageRules.ValidDistribution(10, 10, 7, 1, 2,
    new long[] { 2, 5 }), "conditional price exclusions stay separate");
Check(!CoverageRules.ValidDistribution(10, 10, 7, 1, 1,
    new long[] { 2, 5 }), "distribution rejects lost records");

string[] layerKinds =
    { "facade", "facade", "facade", "ground_buildings", "city_volumes", "roads", "parks", "water" };
long[] layerCache = TileBudget.Allocate(layerKinds);
Check(layerCache.Length == 8 &&
    layerCache.Sum() + layerKinds.Length * TileBudget.HeadroomBytesPerTileset <=
    TileBudget.TotalBytes, "tile cache stays within 192 MiB including headroom");
Check(layerCache[3] > layerCache[4] && layerCache[4] > layerCache[5] &&
    layerCache[5] > layerCache[6] && layerCache[6] > layerCache[0],
    "ground and city cache priorities match web layer weights");
Check(layerCache[0] < 4L * 1024 * 1024 && layerCache[0] == layerCache[1] &&
    layerCache[1] == layerCache[2], "small demo facades share a small cache budget");
Check(TileBudget.Allocate(new[] { "city_volumes" })[0] ==
    TileBudget.TotalBytes - TileBudget.HeadroomBytesPerTileset,
    "single city layer can use remaining cache budget");
Throws(() => TileBudget.Allocate(Enumerable.Repeat("facade", 96).ToArray()),
    "reject more tilesets than cache headroom can support");

Console.WriteLine("Domain tests passed.");
