using System;
using System.Collections.Generic;

namespace Novostroy.Domain
{
    public sealed class SelectionState
    {
        private readonly List<string> compareIds = new List<string>();
        private readonly HashSet<string> favorites = new HashSet<string>(StringComparer.Ordinal);

        public IReadOnlyList<string> CompareIds => compareIds;
        public IReadOnlyCollection<string> Favorites => favorites;
        public bool CanCompare => compareIds.Count >= 2 && compareIds.Count <= 4;

        public bool ToggleCompare(string offerId)
        {
            if (string.IsNullOrWhiteSpace(offerId)) return false;
            if (compareIds.Remove(offerId)) return true;
            if (compareIds.Count == 4) return false;
            compareIds.Add(offerId);
            return true;
        }

        public bool ToggleFavorite(string offerId)
        {
            if (string.IsNullOrWhiteSpace(offerId)) return false;
            if (!favorites.Add(offerId)) favorites.Remove(offerId);
            return favorites.Contains(offerId);
        }

        public bool IsFavorite(string offerId) => favorites.Contains(offerId);
        public bool IsCompared(string offerId) => compareIds.Contains(offerId);

        public void RestoreFavorites(IEnumerable<string> ids)
        {
            favorites.Clear();
            if (ids == null) return;
            foreach (string id in ids)
                if (!string.IsNullOrWhiteSpace(id)) favorites.Add(id);
        }
    }
}
