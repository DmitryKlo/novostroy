using System;
using System.Collections.Generic;
using System.Linq;

namespace Novostroy.Domain
{
    public static class CoverageRules
    {
        private static readonly HashSet<string> DatasetIds = new HashSet<string>(StringComparer.Ordinal)
        {
            "demo_offers", "real_current_offers", "city_geometry"
        };
        private const string RegistryDatasetId = "building_registry";
        private const string DeveloperRegistryDatasetId = "developer_building_registry";

        public static bool ValidDatasetIds(IEnumerable<string> ids)
        {
            string[] values = ids?.ToArray() ?? Array.Empty<string>();
            var unique = new HashSet<string>(values, StringComparer.Ordinal);
            return values.Length == unique.Count &&
                (values.Length == DatasetIds.Count || values.Length == DatasetIds.Count + 1) &&
                DatasetIds.IsSubsetOf(unique) &&
                unique.All(id => DatasetIds.Contains(id) || id == RegistryDatasetId || id == DeveloperRegistryDatasetId) &&
                !(unique.Contains(RegistryDatasetId) && unique.Contains(DeveloperRegistryDatasetId));
        }

        public static bool ValidDatasetMode(string id, string mode) =>
            (id == "demo_offers" && mode == "demo") ||
            (id == "real_current_offers" && mode == "real") ||
            (id == "city_geometry" && mode == "geospatial") ||
            ((id == RegistryDatasetId || id == DeveloperRegistryDatasetId) && mode == "registry");

        public static bool ValidField(long records, long populated, long unknown,
            long stale, IEnumerable<long> qualities)
        {
            long[] counts = qualities?.ToArray() ?? Array.Empty<long>();
            if (records < 0 || populated < 0 || unknown < 0 || stale < 0 ||
                counts.Length != 5 || counts.Any(value => value < 0) ||
                populated > records || unknown > records - populated ||
                stale != records - populated - unknown) return false;
            long remaining = populated;
            foreach (long count in counts)
            {
                if (count > remaining) return false;
                remaining -= count;
            }
            return remaining == 0;
        }

        public static bool ValidDistribution(long records, long population,
            long sample, long missing, long excluded, IEnumerable<long> bins)
        {
            long[] counts = bins?.ToArray() ?? Array.Empty<long>();
            if (records < 0 || population != records || sample < 0 || missing < 0 ||
                excluded < 0 || sample > population ||
                missing > population - sample ||
                excluded != population - sample - missing ||
                counts.Length > 5 || counts.Any(value => value < 0)) return false;
            long remaining = sample;
            foreach (long count in counts)
            {
                if (count > remaining) return false;
                remaining -= count;
            }
            return remaining == 0;
        }
    }
}
