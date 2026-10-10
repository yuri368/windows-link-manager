"""Locate writable user data without confusing it with packaged resources."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys


@dataclass(frozen=True)
class RuntimePaths:
    data_dir: Path
    portable: bool

    @property
    def config(self) -> Path:
        return self.data_dir / "软链接.txt"

    @property
    def state(self) -> Path:
        return self.data_dir / ".state"

    def write_error(self, error: object) -> str:
        if self.portable:
            remedy = (
                "请将整个程序目录复制到当前用户可写的位置，保留软链接.txt和.state，"
                "或为此目录授予写入权限。"
            )
        else:
            remedy = "请检查当前用户对此目录的写入权限。"
        return f"无法写入运行数据目录：{self.data_dir}\n{remedy}\n原始错误：{error}"


def resolve_runtime_paths(source_root: Path | None = None) -> RuntimePaths:
    """Return paths only; even the first run and --check create nothing here.

    A frozen distribution is portable beside the actual executable, never in
    PyInstaller's temporary resource directory. Source checkouts retain their
    existing configuration and recovery history. Installed wheels use the
    current user's local application data directory.
    """
    if getattr(sys, "frozen", False):
        return RuntimePaths(Path(sys.executable).resolve().parent, portable=True)
    root = (source_root or Path(__file__).resolve().parent.parent).resolve()
    if (root / "pyproject.toml").is_file() and (root / "launcher.ps1").is_file():
        return RuntimePaths(root, portable=True)
    local = os.environ.get("LOCALAPPDATA")
    user_root = Path(local) if local else Path.home() / "AppData" / "Local"
    return RuntimePaths((user_root / "WindowsLinkManager").absolute(), portable=False)
