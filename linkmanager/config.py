"""Compatible parser for the user's compact, source-first link configuration."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import os
import re
import tempfile

KINDS = ('junction', 'dir_symlink', 'file_symlink', 'hardlink')


@dataclass(frozen=True)
class LinkSpec:
    project: str
    source: str
    target: str
    kind: str = 'junction'


def parse_config(text: str) -> list[LinkSpec]:
    result: list[LinkSpec] = []
    project = '未分组'
    source: tuple[int, str] | None = None
    kind = 'junction'
    for number, raw in enumerate(text.lstrip('\ufeff').splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#'):
            if re.match(r'#\s*@type\s*=', line, re.I):
                if source:
                    raise ValueError(f'第 {number} 行：类型注释必须放在一对路径之前')
                kind = line.split('=', 1)[1].strip().lower()
                if kind not in KINDS:
                    raise ValueError(f'第 {number} 行：未知链接类型 {kind}')
            continue
        if line.startswith('【'):
            if not line.endswith('】') or len(line) < 3:
                raise ValueError(f'第 {number} 行：项目标题应为【项目名称】')
            if source:
                raise ValueError(f'第 {source[0]} 行：源路径缺少对应目标路径')
            project = line[1:-1].strip()
            if not project:
                raise ValueError(f'第 {number} 行：项目名称不能为空')
            continue
        if '\x00' in line:
            raise ValueError(f'第 {number} 行：路径包含空字符')
        if source is None:
            source = (number, line)
        else:
            result.append(LinkSpec(project, source[1], line, kind))
            source = None
            kind = 'junction'
    if source:
        raise ValueError(f'第 {source[0]} 行：源路径缺少对应目标路径')
    return result


def serialize_config(specs: list[LinkSpec]) -> str:
    lines: list[str] = []
    previous: str | None = None
    for spec in specs:
        if spec.kind not in KINDS:
            raise ValueError(f'未知链接类型：{spec.kind}')
        if any('\n' in item or '\r' in item for item in (spec.project, spec.source, spec.target)):
            raise ValueError('项目和路径不能包含换行')
        if previous != spec.project:
            if lines:
                lines.append('')
            lines.append(f'【{spec.project}】')
            previous = spec.project
        if spec.kind != 'junction':
            lines.append(f'# @type={spec.kind}')
        lines.extend((spec.source, spec.target, ''))
    text = '\n'.join(lines).rstrip() + ('\n' if lines else '')
    if parse_config(text) != specs:
        raise ValueError('项目名或路径无法由当前配置格式无损表示；请检查标题、注释符号和首尾空格')
    return text


def read_config_text(path: Path) -> str:
    data = Path(path).read_bytes()
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        try:
            return data.decode('utf-16')
        except UnicodeError as error:
            raise ValueError('UTF-16 配置文件编码损坏') from error
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            return data.decode(encoding)
        except UnicodeError:
            continue
    raise ValueError('配置文件编码无法识别')


def load_config(path: Path) -> list[LinkSpec]:
    return parse_config(read_config_text(path))


def save_config(path: Path, text: str) -> None:
    parse_config(text)
    path = Path(path).absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup_dir = path.parent / '.backups'
        backup_dir.mkdir(exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        backup = backup_dir / f'{path.name}.{stamp}.bak'
        with backup.open('xb') as handle:
            handle.write(path.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
