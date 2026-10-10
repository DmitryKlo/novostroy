using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;

string root = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory,
    "../../../../../UnityProject/Assets"));
string[] symbols = ["UNITY_EDITOR", "UNITY_ANDROID", "UNITY_IOS"];
int files = 0;
int errors = 0;
foreach (string path in Directory.GetFiles(root, "*.cs", SearchOption.AllDirectories))
{
    files++;
    foreach (string symbol in symbols)
    {
        var options = CSharpParseOptions.Default.WithPreprocessorSymbols("UNITY_EDITOR", symbol);
        var tree = CSharpSyntaxTree.ParseText(File.ReadAllText(path), options, path);
        foreach (Diagnostic diagnostic in tree.GetDiagnostics())
        {
            if (diagnostic.Severity != DiagnosticSeverity.Error) continue;
            Console.Error.WriteLine(symbol + ": " + diagnostic);
            errors++;
        }
    }
}
Console.WriteLine($"Parsed {files} Unity C# files under Editor, Android, and iOS symbols: {errors} syntax errors");
return errors == 0 ? 0 : 1;
