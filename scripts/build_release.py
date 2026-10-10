"""Build the source release from an explicit public-file allowlist.

No virtual environment, Qt binary, font, personal configuration, operation
history, backup, legacy script, or local report belongs in this archive.
"""
from __future__ import annotations

import argparse
import ast
from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
PROJECT_NAME = "windows-link-manager"
ROOT_FILES = (
    "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "requirements.txt",
    "pyproject.toml", "launcher.ps1", "启动.vbs", "启动.cmd", "演示.cmd",
    ".gitignore",
)
PUBLIC_PATTERNS = (
    "linkmanager/*.py", "tests/*.py", "examples/*.txt",
    "docs/images/*.png", "docs/images/*.jpg", "docs/images/*.jpeg",
    "docs/images/*.webp", "docs/images/*.svg", "docs/release-notes.md",
    "scripts/*.py", "packaging/*.iss", ".github/workflows/*.yml", ".github/workflows/*.yaml",
)


def read_version(source: Path) -> str:
    """Read the same literal version used by setuptools without importing Qt."""
    tree = ast.parse((source / "linkmanager/__init__.py").read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ):
            version = ast.literal_eval(node.value)
            if isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version):
                return version
    raise ValueError("linkmanager.__version__ must be a literal major.minor.patch version")


def source_files(source: Path) -> list[Path]:
    """Return only public files, rejecting links to files outside the project."""
    source = source.resolve()
    missing = [name for name in ROOT_FILES if not (source / name).is_file()]
    if missing:
        raise ValueError("Missing release files: " + ", ".join(missing))
    if not (source / "LICENSE").read_text(encoding="utf-8-sig").strip():
        raise ValueError("LICENSE is empty")
    paths = {source / name for name in ROOT_FILES}
    for pattern in PUBLIC_PATTERNS:
        paths.update(path for path in source.glob(pattern) if path.is_file())
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ValueError(f"Release source must be a regular project file: {path}")
    return sorted(paths, key=lambda path: path.relative_to(source).as_posix())


def build_release(source: Path, output: Path, tag: str | None = None) -> Path:
    source = source.resolve()
    version = read_version(source)
    if tag is not None and tag != f"v{version}":
        raise ValueError(f"Tag {tag!r} does not match project version v{version}")
    files = source_files(source)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    folder = f"{PROJECT_NAME}-{version}"
    archive = output / f"{folder}-source.zip"
    with ZipFile(archive, "w", compression=ZIP_DEFLATED, compresslevel=9) as release:
        for path in files:
            release.write(path, f"{folder}/{path.relative_to(source).as_posix()}")
    return archive


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/releases")
    parser.add_argument("--tag", help="Require an exact match with linkmanager.__version__")
    args = parser.parse_args(argv)
    try:
        archive = build_release(args.source, args.output, args.tag)
    except (OSError, ValueError, SyntaxError) as exc:
        parser.error(str(exc))
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
