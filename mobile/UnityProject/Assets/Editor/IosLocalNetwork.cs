#if UNITY_IOS
using System.IO;
using UnityEditor;
using UnityEditor.Callbacks;
using UnityEditor.iOS.Xcode;

namespace Novostroy.Editor
{
    public static class IosLocalNetwork
    {
        [PostProcessBuild(100)]
        public static void Apply(BuildTarget target, string path)
        {
            if (target != BuildTarget.iOS || !EditorUserBuildSettings.development) return;
            string plistPath = Path.Combine(path, "Info.plist");
            var plist = new PlistDocument();
            plist.ReadFromFile(plistPath);
            plist.root.SetString("NSLocalNetworkUsageDescription",
                "Подключение к серверу карты и предложений в вашей локальной сети.");
            // This exception is for the local development export only.
            var ats = plist.root.CreateDict("NSAppTransportSecurity");
            ats.SetBoolean("NSAllowsArbitraryLoads", true);
            plist.WriteToFile(plistPath);
        }
    }
}
#endif
