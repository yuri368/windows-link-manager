"""Collect the exact Qt/PySide source archives and upstream license notices.

Run during release preparation with the same Python environment as the GUI build.
The untouched source archives belong alongside the binary in its GitHub Release;
the dependency-licenses directory is for inclusion in the portable binary ZIP.
No download occurs when an end user runs the application.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
from urllib.error import URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
VERSION = "6.11.2"
SOURCE_PATHS = (
    f"qt/6.11/{VERSION}/submodules/qtbase-everywhere-src-{VERSION}.tar.xz",
    f"qt/6.11/{VERSION}/submodules/qtsvg-everywhere-src-{VERSION}.tar.xz",
    f"qt/6.11/{VERSION}/submodules/qttranslations-everywhere-src-{VERSION}.tar.xz",
    f"QtForPython/pyside6/PySide6-{VERSION}-src/pyside-setup-everywhere-src-{VERSION}.tar.xz",
)
OFFICIAL_BASE = "https://download.qt.io/official_releases/"
FALLBACK_BASE = "https://mirrors.qt.io/official_releases/"


def check_installed_version() -> None:
    import PySide6
    from PySide6.QtCore import qVersion

    if PySide6.__version__ != VERSION or qVersion() != VERSION:
        raise ValueError(
            f"Source version {VERSION} differs from installed PySide6 "
            f"{PySide6.__version__} / Qt {qVersion()}; update the source list first"
        )


def validate_archive(path: Path) -> None:
    with tarfile.open(path, "r:xz") as archive:
        if not archive.getmembers():
            raise ValueError(f"Empty source archive: {path}")


def download_source(relative: str, destination: Path) -> Path:
    """Cache only a complete, readable tar archive; retry one official mirror."""
    target = destination / PurePosixPath(relative).name
    if target.is_file():
        validate_archive(target)
        print(f"Cached source: {target.name}", flush=True)
        return target
    last_error: Exception | None = None
    for base in (OFFICIAL_BASE, FALLBACK_BASE):
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination, suffix=".partial", delete=False) as output:
                temporary = Path(output.name)
                print(f"Downloading: {base}{relative}", flush=True)
                request = Request(base + relative, headers={"User-Agent": "WindowsLinkManager-release-builder"})
                with urlopen(request, timeout=60) as response:
                    shutil.copyfileobj(response, output, length=1024 * 1024)
            validate_archive(temporary)
            temporary.replace(target)
            return target
        except (OSError, URLError, tarfile.TarError, EOFError, ValueError) as exc:
            last_error = exc
            if base == OFFICIAL_BASE:
                print(f"Official download failed; trying official mirror: {exc}", flush=True)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    raise ValueError(f"Could not download {relative}: {last_error}")


def safe_target(root: Path, relative: PurePosixPath) -> Path:
    """Do not permit archive paths (or existing links) to escape the notice root."""
    if relative.is_absolute() or any(part in ("", ".", "..") or ":" in part or "\\" in part
                                     for part in relative.parts):
        raise ValueError(f"Unsafe archive notice path: {relative}")
    target = root.joinpath(*relative.parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Archive notice path escapes the output directory: {relative}")
    return target


def attribution_entries(value: object):
    if isinstance(value, list):
        for entry in value:
            yield from attribution_entries(entry)
    elif isinstance(value, dict):
        if "License" in value or "LicenseFile" in value:
            yield value
        else:
            for entry in value.values():
                yield from attribution_entries(entry)


def license_references(entry: dict) -> list[str]:
    references = []
    for field in ("LicenseFile", "LicenseFiles", "CopyrightFile"):
        value = entry.get(field, [])
        if isinstance(value, str):
            references.extend(line.strip() for line in value.splitlines() if line.strip())
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            references.extend(item.strip() for item in value if item.strip())
        else:
            raise ValueError(f"Unsupported {field} value: {value!r}")
    return references


def collect_licenses(source: Path, destination: Path) -> int:
    """Read only regular files and preserve their upstream relative locations."""
    with tarfile.open(source, "r:xz") as archive:
        members = {PurePosixPath(member.name): member for member in archive.getmembers() if member.isfile()}
        if not members:
            raise ValueError(f"Source archive has no regular files: {source}")
        selected: set[PurePosixPath] = set()
        for path in members:
            name = path.name.lower()
            # The release uses the open-source terms. Do not package a commercial
            # agreement as if it were permission granted to this application's users.
            # The original source archive remains untouched, including this file.
            if name == "licenseref-qt-commercial.txt":
                continue
            root_notice = len(path.parts) == 2 and name.startswith(("license", "copying", "copyright", "readme", "notice"))
            third_party = "3rdparty" in path.parts
            named_notice = name.startswith(("license", "licence", "copying", "copyright", "notice", "readme", "authors"))
            if "LICENSES" in path.parts or root_notice or (third_party and named_notice):
                selected.add(path)
            if name != "qt_attribution.json":
                continue
            selected.add(path)
            stream = archive.extractfile(members[path])
            assert stream is not None
            with stream:
                # Qt's own attribution files allow literal newlines in strings.
                entries = json.load(stream, strict=False)
            for entry in attribution_entries(entries):
                for reference in license_references(entry):
                    # Qt attribution paths are relative to the JSON's directory.
                    referenced = PurePosixPath(path.parent, reference)
                    # Resolve lexical '..' against the upstream archive root.
                    components: list[str] = []
                    for component in referenced.parts:
                        if component == "..":
                            if not components:
                                raise ValueError(f"Escaping LicenseFile in {path}: {reference}")
                            components.pop()
                        elif component != ".":
                            components.append(component)
                    referenced = PurePosixPath(*components)
                    if referenced not in members:
                        raise ValueError(f"Missing LicenseFile in {path}: {reference}")
                    selected.add(referenced)
        for path in sorted(selected, key=str):
            target = safe_target(destination, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = archive.extractfile(members[path])
            assert stream is not None
            with stream, target.open("wb") as output:
                shutil.copyfileobj(stream, output)
        return len(selected)


def collect_dependencies(output: Path = ROOT / "artifacts") -> tuple[list[Path], Path]:
    check_installed_version()
    output = output.resolve()
    source_directory = output / "dependency-sources"
    license_directory = output / "dependency-licenses"
    source_directory.mkdir(parents=True, exist_ok=True)
    license_directory.mkdir(parents=True, exist_ok=True)
    archives = []
    total_notices = 0
    for relative in SOURCE_PATHS:
        source = download_source(relative, source_directory)
        count = collect_licenses(source, license_directory)
        archives.append(source)
        total_notices += count
        print(f"Collected {count} notice files from {source.name}", flush=True)
    total_bytes = sum(source.stat().st_size for source in archives)
    print(f"Source archives: {len(archives)} ({total_bytes:,} bytes)", flush=True)
    print(f"Upstream notice files: {total_notices}", flush=True)
    print(f"Source attachment directory: {source_directory}", flush=True)
    print(f"Binary license directory: {license_directory}", flush=True)
    return archives, license_directory


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts",
                        help="Parent of dependency-sources and dependency-licenses directories")
    args = parser.parse_args(argv)
    try:
        collect_dependencies(args.output)
    except (OSError, ValueError, tarfile.TarError, EOFError, ImportError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
