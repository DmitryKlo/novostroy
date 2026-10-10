using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;

namespace Novostroy.Domain
{
    public sealed class PredicateInput
    {
        public string Key { get; set; }
        public string ValueType { get; set; }
        public IReadOnlyList<string> Operators { get; set; } = Array.Empty<string>();
        public IReadOnlyList<string> EnumValues { get; set; } = Array.Empty<string>();
        public string Minimum { get; set; } = "";
        public string Maximum { get; set; } = "";
        public string Choice { get; set; } = "";
        public string Text { get; set; } = "";
        // -1 = any, 0 = explicit false, 1 = explicit true.
        public int BooleanChoice { get; set; } = -1;
    }

    public sealed class FactPredicate
    {
        public string Key { get; set; }
        public string Op { get; set; }
        public object Value { get; set; }
    }

    public static class PredicateBuilder
    {
        public static List<FactPredicate> Build(IEnumerable<PredicateInput> inputs)
        {
            var result = new List<FactPredicate>();
            foreach (PredicateInput input in inputs ?? Array.Empty<PredicateInput>())
            {
                if (input == null || string.IsNullOrWhiteSpace(input.Key)) continue;
                string type = input.ValueType ?? "";
                if (type == "integer" || type == "decimal" || type == "ratio")
                {
                    object min = ParseNumber(input.Minimum, type);
                    object max = ParseNumber(input.Maximum, type);
                    if (min != null && max != null &&
                        DecimalValue(min) > DecimalValue(max))
                        throw new ArgumentException($"{input.Key}: минимум больше максимума");
                    if (min != null) Add(result, input, "min", min);
                    if (max != null) Add(result, input, "max", max);
                }
                else if (type == "boolean" && input.BooleanChoice != -1)
                {
                    if (input.BooleanChoice != 0 && input.BooleanChoice != 1)
                        throw new ArgumentException($"{input.Key}: недопустимое булево значение");
                    Add(result, input, "equals", input.BooleanChoice == 1);
                }
                else if (type == "enum" && !string.IsNullOrWhiteSpace(input.Choice))
                {
                    string choice = input.Choice.Trim();
                    if (input.EnumValues.Count > 0 && !input.EnumValues.Contains(choice))
                        throw new ArgumentException($"{input.Key}: значение вне каталога");
                    Add(result, input, "equals", choice);
                }
                else if ((type == "string" || type == "string_array") &&
                    !string.IsNullOrWhiteSpace(input.Text))
                {
                    string op = input.Operators.Contains("contains") ? "contains" : "equals";
                    Add(result, input, op, input.Text.Trim());
                }
            }
            if (result.Count > 24)
                throw new ArgumentException("Не больше 24 условий за один поиск");
            return result;
        }

        private static void Add(List<FactPredicate> result, PredicateInput input,
            string op, object value)
        {
            if (!input.Operators.Contains(op))
                throw new ArgumentException($"{input.Key}: оператор {op} не поддерживается");
            result.Add(new FactPredicate { Key = input.Key, Op = op, Value = value });
        }

        private static object ParseNumber(string text, string type)
        {
            if (string.IsNullOrWhiteSpace(text)) return null;
            string canonical = text.Trim().Replace(',', '.');
            if (type == "integer")
            {
                if (!long.TryParse(canonical, NumberStyles.AllowLeadingSign,
                    CultureInfo.InvariantCulture, out long integer))
                    throw new ArgumentException("Введите целое число");
                return integer;
            }
            if (!decimal.TryParse(canonical, NumberStyles.AllowLeadingSign |
                NumberStyles.AllowDecimalPoint, CultureInfo.InvariantCulture, out decimal number))
                throw new ArgumentException("Введите число");
            // Decimal strings preserve the exact user threshold for ratio facts.
            return number.ToString(CultureInfo.InvariantCulture);
        }

        private static decimal DecimalValue(object value) =>
            Convert.ToDecimal(value, CultureInfo.InvariantCulture);
    }
}
