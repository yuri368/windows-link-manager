"""Exercise the real Windows PowerShell 5.1 process boundary and VBS entrypoint."""
from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
WSCRIPT = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/wscript.exe"


def ps_string(value):
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(os.name == "nt" and POWERSHELL.is_file(), "Windows PowerShell 5.1 required")
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="linkmanager-launcher-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_functions(self, body):
        script = self.root / "检查入口 含空格.ps1"
        # Parse the actual launcher, importing function definitions without its
        # environment creation, dependency installation, dialogs, or GUI entry.
        prefix = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile({ps_string(ROOT / 'launcher.ps1')}, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) {{ throw ($parseErrors | Out-String) }}
$functions = $ast.FindAll({{param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]}}, $false)
foreach ($function in $functions) {{ Invoke-Expression $function.Extent.Text }}
$projectRoot = {ps_string(self.root)}
$logPath = {ps_string(self.root / 'launcher.log')}
$venvPython = {ps_string(sys.executable)}
$requirementsFile = {ps_string(ROOT / 'requirements.txt')}
"""
        script.write_text(prefix + body, encoding="utf-8-sig")
        result = subprocess.run(
            [str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", str(script)],
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_installed_version_probe_uses_real_powershell_and_python(self):
        result = self.run_functions("""
$required = Get-PinnedPySide6Version
$installed = Get-InstalledPySide6Version
[pscustomobject]@{Required=$required; Installed=$installed; Mismatch=(Test-PySide6VersionMismatch $required $installed)} | ConvertTo-Json -Compress
""")
        self.assertEqual(result["Installed"], importlib.metadata.version("PySide6"))
        self.assertEqual(result["Mismatch"], result["Required"] != result["Installed"])

    def test_native_arguments_preserve_quotes_unicode_and_trailing_slashes(self):
        arguments = [str(self.root / "中文 路径") + "\\tail\\", 'a"b', "", "\\"]
        code = "import json,sys; print(json.dumps(sys.argv[1:], ensure_ascii=False))"
        ps_arguments = ", ".join(ps_string(arg) for arg in ["-c", code, *arguments])
        result = self.run_functions(f"""
$result = Invoke-CapturedProcess $venvPython @({ps_arguments})
$result | ConvertTo-Json -Compress
""")
        self.assertEqual(result["ExitCode"], 0, result["Stderr"])
        self.assertEqual(json.loads(result["Stdout"]), arguments)

    def test_failed_python_keeps_complete_traceback_and_exit_code(self):
        code = 'print("before error"); raise RuntimeError("启动检查失败 中文")'
        result = self.run_functions(f"""
$code = Invoke-Logged $venvPython @('-c', {ps_string(code)}) '回归检查'
[pscustomobject]@{{ExitCode=$code; Log=([string](Get-Content -LiteralPath $logPath -Raw -Encoding UTF8))}} | ConvertTo-Json -Compress
""")
        self.assertEqual(result["ExitCode"], 1)
        self.assertIn("before error", result["Log"])
        self.assertIn('File "<string>", line 1', result["Log"])
        self.assertIn("RuntimeError: 启动检查失败 中文", result["Log"])

    def test_failed_dependency_probe_returns_missing_version_and_logs_error(self):
        (self.root / "PySide6.py").write_text('raise ImportError("dependency probe failure")', encoding="utf-8")
        result = self.run_functions("""
$version = Get-InstalledPySide6Version
[pscustomobject]@{Missing=($null -eq $version); Log=([string](Get-Content -LiteralPath $logPath -Raw -Encoding UTF8))} | ConvertTo-Json -Compress
""")
        self.assertTrue(result["Missing"])
        self.assertIn("ImportError: dependency probe failure", result["Log"])

    def test_vbs_demo_screenshot_runs_complete_launcher_chain(self):
        required = (ROOT / "requirements.txt").read_text(encoding="utf-8-sig").strip().split("==", 1)[1]
        if importlib.metadata.version("PySide6") != required:
            self.skipTest("Full entrypoint test must not install dependencies")
        screenshot = self.root / "演示截图 含空格.png"
        config = ROOT / "软链接.txt"
        config_before = config.read_bytes() if config.exists() else None
        result = subprocess.run(
            [str(WSCRIPT), "//B", str(ROOT / "启动.vbs"), "--demo", "--screenshot", str(screenshot)],
            cwd=self.root, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        deadline = time.monotonic() + 15
        completed_screenshot = screenshot.with_name("已完成截图.png")
        while time.monotonic() < deadline:
            try:
                data = screenshot.read_bytes()
                if data.endswith(b"\x00\x00\x00\x00IEND\xaeB\x60\x82"):
                    # The entrypoint launches asynchronously. Wait for PNG
                    # completion and a closed write handle before TEMP cleanup.
                    screenshot.rename(completed_screenshot)
                    break
            except (FileNotFoundError, PermissionError):
                pass
            time.sleep(0.05)
        self.assertTrue(completed_screenshot.is_file(), "VBS → PowerShell → GUI did not finish its screenshot")
        self.assertEqual(completed_screenshot.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        if config_before is None:
            self.assertFalse(config.exists(), "Demo created a personal configuration")
        else:
            self.assertEqual(config.read_bytes(), config_before)


if __name__ == "__main__":
    unittest.main()
