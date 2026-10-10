#if UNITY_ANDROID
using System;
using System.IO;
using System.Xml;
using UnityEditor;
using UnityEditor.Android;

namespace Novostroy.Editor
{
    public sealed class AndroidLocalNetwork : IPostGenerateGradleAndroidProject
    {
        public int callbackOrder => 100;

        public void OnPostGenerateGradleAndroidProject(string path)
        {
            if (!EditorUserBuildSettings.development) return;
            // Patch Unity's generated manifest, keeping its version-specific activity intact.
            string manifestPath = Path.Combine(path, "src/main/AndroidManifest.xml");
            if (!File.Exists(manifestPath))
                throw new FileNotFoundException("Unity Android manifest не найден", manifestPath);
            var document = new XmlDocument();
            document.Load(manifestPath);
            XmlElement application = document.DocumentElement?["application"];
            if (application == null)
                throw new InvalidOperationException("Unity Android manifest без application");
            application.SetAttribute("usesCleartextTraffic",
                "http://schemas.android.com/apk/res/android", "true");
            document.Save(manifestPath);
        }
    }
}
#endif
