using System;
using System.Collections;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using Novostroy.Domain;
using UnityEngine.Networking;

namespace Novostroy.Runtime
{
    public sealed class ApiClient
    {
        public string BaseUrl { get; private set; }

        public ApiClient(string baseUrl) => BaseUrl = ApiAddress.Normalize(baseUrl);

        public IEnumerator Get<T>(string path, Action<T> ok, Action<string> error)
        {
            using (UnityWebRequest request = UnityWebRequest.Get(ApiAddress.Route(BaseUrl, path)))
                yield return Send(request, ok, error);
        }

        public IEnumerator Post<T>(string path, object payload, Action<T> ok, Action<string> error)
        {
            byte[] body = System.Text.Encoding.UTF8.GetBytes(JsonConvert.SerializeObject(payload));
            using (var request = new UnityWebRequest(ApiAddress.Route(BaseUrl, path), "POST"))
            {
                request.uploadHandler = new UploadHandlerRaw(body);
                request.downloadHandler = new DownloadHandlerBuffer();
                request.SetRequestHeader("Content-Type", "application/json; charset=utf-8");
                yield return Send(request, ok, error);
            }
        }

        private static IEnumerator Send<T>(UnityWebRequest request, Action<T> ok, Action<string> error)
        {
            request.timeout = 20;
            yield return request.SendWebRequest();
            if (request.result != UnityWebRequest.Result.Success)
            {
                string detail = request.error;
                try
                {
                    string response = request.downloadHandler?.text;
                    string message = JObject.Parse(response)["detail"]?["message"]?.Value<string>();
                    if (!string.IsNullOrWhiteSpace(message)) detail = message;
                }
                catch (JsonException) { }
                error?.Invoke($"HTTP {request.responseCode}: {detail}");
                yield break;
            }

            try
            {
                T value = JsonConvert.DeserializeObject<T>(request.downloadHandler.text);
                if (value == null) throw new JsonSerializationException("Пустой JSON");
                ok?.Invoke(value);
            }
            catch (JsonException ex)
            {
                error?.Invoke("Неверный ответ API: " + ex.Message);
            }
        }
    }
}
