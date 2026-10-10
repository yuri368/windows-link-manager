"""Exercise the release command and extracted source without personal files."""
from __future__ import annotations

from pathlib import Path
import shutil
import secrets
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

from linkmanager import __version__


ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "scripts/build_release.py"


class SourceReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="linkmanager-release-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / "source"
        self.source.mkdir()
        # A clean checkout has no private configuration. Copy public source
        # directories, then seed common local-only files into this fixture.
        for name in (
            "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "requirements.txt",
            "pyproject.toml", "launcher.ps1", "启动.vbs", "启动.cmd", "演示.cmd",
            ".gitignore",
        ):
            shutil.copy2(ROOT / name, self.source / name)
        for name in ("linkmanager", "tests", "examples", "docs", "scripts", ".github"):
            if (ROOT / name).is_dir():
                shutil.copytree(ROOT / name, self.source / name,
                                ignore=shutil.ignore_patterns("__pycache__"))

    def run_build(self, *arguments):
        return subprocess.run(
            [sys.executable, str(BUILD_SCRIPT), "--source", str(self.source),
             "--output", str(self.folder / "out"), *arguments],
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )

    def test_zip_contains_runnable_source_and_excludes_local_data(self):
        sentinel = "PRIVATE_" + secrets.token_hex(16)
        private_files = (
            "软链接.txt", "创建交接点.ps1", "运行创建交接点.cmd",
            "使用说明.txt", "发布前权属与许可证审查.md", "架构评估报告.md",
            ".state/history.json", ".backups/original.txt", "artifacts/screenshot.png",
            ".venv/Lib/site-packages/PySide6/Qt6Core.dll", "fonts/private.ttf",
            "linkmanager/__pycache__/main.pyc", "docs/images/private.ttf",
            "scripts/private.log", "examples/.state/history.json",
        )
        for name in private_files:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(sentinel, encoding="utf-8")
        example = self.source / "examples/软链接.txt"
        example.parent.mkdir(parents=True, exist_ok=True)
        example.write_text("# Public example configuration\n", encoding="utf-8")
        result = self.run_build("--tag", f"v{__version__}")
        self.assertEqual(result.returncode, 0, result.stderr)
        archive = Path(result.stdout.strip())
        self.assertEqual(archive.name, f"windows-link-manager-{__version__}-source.zip")
        prefix = f"windows-link-manager-{__version__}/"
        with ZipFile(archive) as release:
            names = set(release.namelist())
            for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "requirements.txt",
                         "pyproject.toml", "launcher.ps1", "启动.vbs", "启动.cmd",
                         "演示.cmd", "linkmanager/__main__.py", "tests/test_launcher.py",
                         "scripts/build_release.py", ".github/workflows/release.yml",
                         "examples/软链接.txt"):
                self.assertIn(prefix + name, names)
            for name in private_files:
                self.assertNotIn(prefix + name, names)
            self.assertTrue(all(".." not in Path(name).parts for name in names))
            for name in names:
                self.assertNotIn(sentinel.encode("utf-8"), release.read(name), name)
            extracted = self.folder / "extracted"
            release.extractall(extracted)
        project = extracted / prefix.rstrip("/")
        result = subprocess.run([sys.executable, "-m", "linkmanager", "--version"],
                                cwd=project, capture_output=True, text=True,
                                encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(__version__, result.stdout)
        self.assertFalse((project / "软链接.txt").exists())

    def test_mismatched_release_tag_does_not_create_archive(self):
        result = self.run_build("--tag", "v0.0.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match project version", result.stderr)
        self.assertFalse((self.folder / "out").exists())

    def test_missing_license_prevents_release(self):
        (self.source / "LICENSE").unlink()
        result = self.run_build()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing release files: LICENSE", result.stderr)
        self.assertFalse((self.folder / "out").exists())

    def test_empty_license_prevents_release(self):
        (self.source / "LICENSE").write_text("", encoding="utf-8")
        result = self.run_build()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LICENSE is empty", result.stderr)
        self.assertFalse((self.folder / "out").exists())


if __name__ == "__main__":
    unittest.main()
