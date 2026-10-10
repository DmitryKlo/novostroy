using System;
using System.Globalization;

namespace Novostroy.Domain
{
    public static class FilterRules
    {
        public static long? ParseRubles(string text)
        {
            if (string.IsNullOrWhiteSpace(text)) return null;
            string digits = text.Replace(" ", "").Replace("\u00a0", "");
            if (!long.TryParse(digits, NumberStyles.None, CultureInfo.InvariantCulture, out long value) ||
                value < 0)
                throw new ArgumentException("Бюджет должен быть целым числом рублей");
            return value;
        }

        public static void ValidateRange(long? min, long? max)
        {
            if (min.HasValue && max.HasValue && min.Value > max.Value)
                throw new ArgumentException("Минимальный бюджет больше максимального");
        }
    }
}
