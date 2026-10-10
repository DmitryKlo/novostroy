using System;
using System.IO;
using UnityEditor;
using UnityEditor.Build;
using UnityEditor.Build.Reporting;
using UnityEditor.SceneManagement;
using UnityEngine;

namespace Novostroy.Editor
{
    public static class NovostroyBuild
    {
        private const string ScenePath = "Assets/Scenes/Main.unity";
        private const string ExpectedEditor = "6000.3.16f1";

        [InitializeOnLoadMethod]
        private static void RegisterSceneCreation()
        {
            EditorApplication.delayCall += () =>
            {
                if (!BuildPipeline.isBuildingPlayer) EnsureScene();
            };
        }

        [MenuItem("Novostroy/Create or check scene")]
        public static void EnsureScene()
        {
            if (!File.Exists(ScenePath))
            {
                Directory.CreateDirectory("Assets/Scenes");
                var scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Additive);
                if (!EditorSceneManager.SaveScene(scene, ScenePath))
                    throw new InvalidOperationException("Не удалось сохранить Main.unity");
                EditorSceneManager.CloseScene(scene, true);
                AssetDatabase.Refresh();
            }
            EditorBuildSettings.scenes = new[] { new EditorBuildSettingsScene(ScenePath, true) };
        }

        [MenuItem("Novostroy/Build Android development APK")]
        public static void BuildAndroid()
        {
            Prepare();
            PlayerSettings.SetScriptingBackend(NamedBuildTarget.Android, ScriptingImplementation.IL2CPP);
            PlayerSettings.Android.targetArchitectures = AndroidArchitecture.ARM64;
            PlayerSettings.Android.forceInternetPermission = true;
            PlayerSettings.SetApplicationIdentifier(BuildTargetGroup.Android, "ru.novostroy.mobile");
            Directory.CreateDirectory("Builds/Android");
            Build(BuildTarget.Android, "Builds/Android/Novostroy-dev.apk");
        }

        [MenuItem("Novostroy/Export iOS Xcode project")]
        public static void ExportIos()
        {
            Prepare();
            PlayerSettings.SetScriptingBackend(NamedBuildTarget.iOS, ScriptingImplementation.IL2CPP);
            PlayerSettings.SetApplicationIdentifier(BuildTargetGroup.iOS, "ru.novostroy.mobile");
            Directory.CreateDirectory("Builds/iOS");
            Build(BuildTarget.iOS, "Builds/iOS/NovostroyXcode");
        }

        private static void Prepare()
        {
            if (Application.unityVersion != ExpectedEditor)
                throw new InvalidOperationException(
                    $"Ожидается Unity {ExpectedEditor}, найден {Application.unityVersion}");
            EnsureScene();
            PlayerSettings.companyName = "Novostroy";
            PlayerSettings.productName = "Новострой";
            PlayerSettings.defaultInterfaceOrientation = UIOrientation.Portrait;
            PlayerSettings.insecureHttpOption = InsecureHttpOption.DevelopmentOnly;
            EditorUserBuildSettings.development = true;
        }

        private static void Build(BuildTarget target, string location)
        {
            var report = BuildPipeline.BuildPlayer(new BuildPlayerOptions
            {
                scenes = new[] { ScenePath },
                locationPathName = location,
                target = target,
                options = BuildOptions.Development
            });
            if (report.summary.result != BuildResult.Succeeded)
                throw new InvalidOperationException(
                    $"Unity build {target} завершился: {report.summary.result}");
            Debug.Log($"Novostroy {target}: {report.summary.outputPath}");
        }
    }
}
