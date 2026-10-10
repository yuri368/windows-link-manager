"""Build the Windows x64 GUI directory and its portable release archive.

Install the fixed build tool in the project environment first:
    python -m pip install pyinstaller==6.22.3

The application stays in onedir form so its Qt DLLs remain replaceable.
Only PyInstaller's dependency output and explicit public license files are copied.
"""
from __future__ import annotations

import argparse
from importlib import metadata
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
from zipfile import ZIP_DEFLATED, ZipFile

from build_release import PROJECT_NAME, ROOT, read_version


PYINSTALLER_VERSION = "6.22.3"
EXE_NAME = "WindowsLinkManager"


def copy_licenses(bundle: Path, source: Path, dependency_licenses: Path) -> None:
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(source / name, bundle / name)
    license_root = bundle / "licenses"
    # The original wheel may only contain a commercial-license option. The
    # upstream open-source license texts come from the separate source collector.
    shutil.copytree(dependency_licenses, license_root / "Qt-and-PySide", dirs_exist_ok=True)
    # Copy licenses for the bindings, Qt modules, and bootloader actually shipped.
    for name in ("PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6", "pyinstaller"):
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            if name == "PySide6_Addons":
                continue
            raise
        files = distribution.files or []
        if name == "PySide6_Addons" and not any(
            (bundle / "_internal" / Path(str(item))).is_file()
            for item in files if not ".dist-info/" in str(item)
        ):
            continue
        license_files = [item for item in files if ".dist-info/licenses/" in str(item)]
        if not license_files:
            raise ValueError(f"No installed license files for {name}")
        for item in license_files:
            relative = str(item).split(".dist-info/licenses/", 1)[1]
            target = license_root / f"{name}-{distribution.version}" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(distribution.locate_file(item), target)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise ValueError(f"Python runtime license is missing: {python_license}")
    target = license_root / f"Python-{platform.python_version()}" / "LICENSE.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(python_license, target)


def build_windows(source: Path, output: Path, tag: str | None = None,
                  dependency_licenses: Path | None = None) -> tuple[Path, Path]:
    source = source.resolve()
    version = read_version(source)
    if tag is not None and tag != f"v{version}":
        raise ValueError(f"Tag {tag!r} does not match project version v{version}")
    if (sys.platform != "win32" or struct.calcsize("P") != 8
            or platform.machine().lower() not in ("amd64", "x86_64")):
        raise ValueError("Build with a 64-bit x64 Python interpreter on Windows")
    if metadata.version("pyinstaller") != PYINSTALLER_VERSION:
        raise ValueError(f"Install the fixed build tool: pip install pyinstaller=={PYINSTALLER_VERSION}")
    dependency_licenses = (dependency_licenses or source / "artifacts/dependency-licenses").resolve()
    if not dependency_licenses.is_dir():
        raise ValueError(f"Collect dependency license texts first: {dependency_licenses}")
    build = source / "artifacts/build/windows"
    # PyInstaller --noconfirm can replace its output directory. Verify this target
    # stays inside the intended build directory before handing it to the tool.
    bundle = build / "dist" / EXE_NAME
    if not bundle.resolve().is_relative_to(build.resolve()):
        raise ValueError(f"Build output escapes the build directory: {bundle}")
    if build.is_symlink() or build.resolve() != build.absolute():
        raise ValueError(f"Build directory must not be redirected by a filesystem link: {build}")
    build.mkdir(parents=True, exist_ok=True)
    work = build / "work"
    if not work.resolve().is_relative_to(build.resolve()):
        raise ValueError(f"Build cache escapes the build directory: {work}")
    if work.exists():
        shutil.rmtree(work)
    hooks = build / "hooks"
    if not hooks.resolve().is_relative_to(build.resolve()):
        raise ValueError(f"Build hooks escape the build directory: {hooks}")
    hooks.mkdir(exist_ok=True)
    # Omit optional PDF, virtual keyboard, and QtImageFormats plugins before DLL
    # analysis. The Widgets app uses raster drawing and the QtBase/SVG formats;
    # Chromium, QML, Quick and the extra image-format module are unnecessary.
    (hooks / "hook-PySide6.QtGui.py").write_text(
        "from pathlib import Path\n"
        "from PyInstaller.utils.hooks.qt import add_qt6_dependencies\n"
        "hiddenimports, binaries, datas = add_qt6_dependencies(__file__)\n"
        "binaries = [(src, dest) for src, dest in binaries "
        "if Path(src).name.lower() not in {"
        "'qpdf.dll', 'qtvirtualkeyboardplugin.dll', 'qicns.dll', 'qtga.dll', "
        "'qtiff.dll', 'qwbmp.dll', 'qwebp.dll'}]\n",
        encoding="utf-8",
    )
    # This app does not create OpenGL contexts; the optional software renderer
    # is not needed and would add a separate Mesa runtime to the distribution.
    (hooks / "hook-PySide6.py").write_text(
        "from pathlib import Path\n"
        "from PyInstaller.utils.hooks import check_requirement\n"
        "from PyInstaller.utils.hooks.qt import pyside6_library_info, ensure_single_qt_bindings_package\n"
        "ensure_single_qt_bindings_package('PySide6')\n"
        "if pyside6_library_info.version is not None:\n"
        "    hiddenimports = ['shiboken6', 'inspect']\n"
        "    if check_requirement('PySide6 >= 6.4.0'):\n"
        "        hiddenimports.append('PySide6.support.deprecated')\n"
        "    binaries = [(src, dest) for src, dest in pyside6_library_info.collect_extra_binaries() "
        "if Path(src).name.lower() != 'opengl32sw.dll']\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    windows = Path(environment.get("SYSTEMROOT") or environment.get("SystemRoot") or "C:/Windows")
    # Foreign tools on PATH can supply a DLL with the same name and a different
    # ABI (for example Poppler's ICU instead of Windows ICU used by Qt6Core).
    environment["PATH"] = os.pathsep.join(map(str, (
        Path(sys.executable).parent, Path(sys.base_prefix),
        Path(sys.base_prefix) / "DLLs", windows / "System32", windows,
    )))
    environment.pop("PYTHONPATH", None)
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm",
        "--onedir", "--windowed", "--noupx", "--name", EXE_NAME,
        "--distpath", str(build / "dist"), "--workpath", str(work),
        "--specpath", str(build), "--paths", str(source),
        "--additional-hooks-dir", str(hooks),
        "--exclude-module", "PyQt5", "--exclude-module", "PyQt6",
        "--exclude-module", "PySide2",
        str(source / "scripts/windows_entry.py"),
    ], cwd=source, env=environment, check=True)
    copy_licenses(bundle, source, dependency_licenses)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"{PROJECT_NAME}-{version}-windows-x64.zip"
    with ZipFile(archive, "w", compression=ZIP_DEFLATED, compresslevel=9) as release:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                relative = path.relative_to(bundle)
                if relative.parts[0] in (".state", "软链接.txt"):
                    continue
                release.write(path, f"{PROJECT_NAME}-{version}/{relative.as_posix()}")
    return bundle, archive


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/releases")
    parser.add_argument("--tag", help="Require an exact match with linkmanager.__version__")
    parser.add_argument("--dependency-licenses", type=Path,
                        help="Collected upstream license directory (default: artifacts/dependency-licenses)")
    args = parser.parse_args(argv)
    try:
        bundle, archive = build_windows(args.source, args.output, args.tag, args.dependency_licenses)
    except (OSError, ValueError, SyntaxError, metadata.PackageNotFoundError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    print(f"GUI directory: {bundle}")
    print(f"Portable release: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
