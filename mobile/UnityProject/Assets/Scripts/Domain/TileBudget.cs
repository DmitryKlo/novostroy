using System;
using System.Collections.Generic;

namespace Novostroy.Domain
{
    public static class TileBudget
    {
        public const long TotalBytes = 192L * 1024L * 1024L;
        public const long HeadroomBytesPerTileset = 2L * 1024L * 1024L;

        // Match the web viewer's relative ground-first cache priority. The
        // flat footprint floor is an independent persistent tileset.
        private static long Weight(string kind)
        {
            switch (kind)
            {
                case "ground_buildings": return 48;
                case "city_volumes": return 32;
                case "roads": return 8;
                case "facade": return 1;
                case "parks":
                case "water": return 4;
                default: return 4;
            }
        }

        public static long[] Allocate(IReadOnlyList<string> kinds)
        {
            if (kinds == null || kinds.Count == 0) return Array.Empty<long>();
            if (kinds.Count >= TotalBytes / HeadroomBytesPerTileset)
                throw new ArgumentException("Слишком много наборов 3D Tiles для бюджета карты");
            long cachePool = TotalBytes - kinds.Count * HeadroomBytesPerTileset;
            long weightTotal = 0;
            foreach (string kind in kinds) weightTotal += Weight(kind);
            var allocations = new long[kinds.Count];
            for (int i = 0; i < kinds.Count; i++)
                allocations[i] = cachePool * Weight(kinds[i]) / weightTotal;
            return allocations;
        }
    }
}
