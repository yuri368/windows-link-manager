"""Shared absolute path normalization and filesystem boundary policy."""
from __future__ import annotations

from pathlib import Path
import os
import re
import stat

def normalize_path(value: str | Path) -> Path:
    value = str(value).strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    value = os.path.expanduser(os.path.expandvars(value))
    if not value.strip() or '\x00' in value:
        raise ValueError('路径不能为空或包含空字符')
    if re.search(r'%[^%]+%', value):
        raise ValueError(f'环境变量未定义：{value}')
    if not os.path.isabs(value):
        raise ValueError(f'请使用绝对路径：{value}')
    if os.name == 'nt':
        if value.startswith(('\\\\?\\', '\\\\.\\')):
            raise ValueError('不支持设备路径或扩展路径前缀')
        drive, tail = os.path.splitdrive(value)
        if ':' in tail:
            raise ValueError('路径不能使用 NTFS 备用数据流')
        if any(character in tail for character in '<>"|?*'):
            raise ValueError('路径包含 Windows 不允许的字符')
        if drive and not os.path.isabs(value):
            raise ValueError('不支持相对驱动器路径')
        for component in Path(value).parts[1:]:
            if component.endswith((' ', '.')):
                raise ValueError('路径分段不能以空格或句点结尾')
            stem = component.split('.', 1)[0].upper()
            if stem in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
                raise ValueError('路径包含 Windows 保留名称')
    return Path(os.path.abspath(value))


def path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def within(path: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath((path_key(path), path_key(parent))) == path_key(parent)
    except ValueError:
        return False


def exists(path: Path) -> bool:
    return os.path.lexists(path)


def reparse(path: Path) -> bool:
    try:
        info = path.lstat()
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)
    except FileNotFoundError:
        return False


def reject_reparse(path: Path, include_self: bool = True) -> None:
    items = (path, *path.parents) if include_self else tuple(path.parents)
    for item in items:
        if reparse(item):
            raise ValueError(f'路径经过重解析点：{item}')


