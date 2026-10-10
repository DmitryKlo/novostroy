using System;

namespace Novostroy.Domain
{
    public static class ApiAddress
    {
        public static string Normalize(string input)
        {
            if (!Uri.TryCreate(input?.Trim(), UriKind.Absolute, out Uri uri) ||
                (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps) ||
                string.IsNullOrWhiteSpace(uri.Host) ||
                !string.IsNullOrEmpty(uri.UserInfo) ||
                !string.IsNullOrEmpty(uri.Query) ||
                !string.IsNullOrEmpty(uri.Fragment) ||
                uri.AbsolutePath != "/")
                throw new ArgumentException("Укажите только адрес API: http(s)://host:port");
            return uri.GetLeftPart(UriPartial.Authority);
        }

        public static string Route(string baseUrl, string route)
        {
            string origin = Normalize(baseUrl);
            string path = route?.Split('?')[0];
            if (string.IsNullOrWhiteSpace(route) || !route.StartsWith("/", StringComparison.Ordinal) ||
                route.StartsWith("//", StringComparison.Ordinal) || path.Contains(".."))
                throw new ArgumentException("Недопустимый маршрут API");
            return origin + route;
        }

        public static string Tile(string baseUrl, string tilePath)
        {
            if (string.IsNullOrWhiteSpace(tilePath) ||
                !tilePath.StartsWith("/assets/", StringComparison.Ordinal) ||
                tilePath.Contains("..") || tilePath.Contains("?") || tilePath.Contains("#"))
                throw new ArgumentException("Тайлы должны иметь локальный маршрут /assets/...");
            return Route(baseUrl, tilePath);
        }
    }
}
