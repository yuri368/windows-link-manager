"""Windows link operations with batch validation, durable history and safe undo.

Ordinary targets are renamed for backup or permanently deleted only when that
mode is explicitly enabled. Every changed entry is recorded before mutation;
a process crash leaves a visible pending record for manual review.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import ctypes
import hashlib
import json
import os
import stat
import uuid

from .config import KINDS, LinkSpec

from .paths import (normalize_path as _path, path_key as _key, within as _within,
                    exists as _exists, reparse as _reparse, reject_reparse as _reject_reparse)
from .windows_backend import (JUNCTION_TAG, SYMLINK_TAG, create_junction as _junction,
                              create_link as _create)
from .history import HistoryRepository
from .planning import ordered_indices
from .recovery_view import recovery_message
from .models import (ACTION_LABELS, Fingerprint, HistoryRecord, HistoryState,
                     Inspection, OperationRecord, OperationResult, PlanAction,
                     PlanItem, can_recover)


def _readlink(path: Path) -> Path:
    raw = os.readlink(path)
    if raw.startswith('\\\\?\\UNC\\'):
        raw = '\\\\' + raw[8:]
    elif raw.startswith(('\\\\?\\', '\\??\\')):
        raw = raw[4:]
    if not os.path.isabs(raw):
        raw = str(path.parent / raw)
    return Path(os.path.abspath(raw))


def _kind(path: Path, source: Path | None = None) -> str:
    if not _exists(path):
        return 'missing'
    info = path.lstat()
    tag = getattr(info, 'st_reparse_tag', 0)
    if tag == JUNCTION_TAG:
        return 'junction'
    if tag == SYMLINK_TAG or stat.S_ISLNK(info.st_mode):
        is_dir = bool(getattr(info, 'st_file_attributes', 0) & 0x10) if os.name == 'nt' else path.is_dir()
        return 'dir_symlink' if is_dir else 'file_symlink'
    if _reparse(path):
        return 'unknown_reparse'
    if stat.S_ISDIR(info.st_mode):
        return 'directory'
    if stat.S_ISREG(info.st_mode):
        if source is not None and source.is_file() and info.st_nlink > 1 and os.path.samefile(path, source):
            return 'hardlink'
        return 'file'
    return 'other'


def _fingerprint(path: Path) -> Fingerprint:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {'exists': False}
    result = {'exists': True, 'dev': info.st_dev, 'ino': info.st_ino, 'mode': info.st_mode,
              'size': info.st_size, 'mtime': info.st_mtime_ns, 'ctime': info.st_ctime_ns,
              'nlink': info.st_nlink, 'attributes': getattr(info, 'st_file_attributes', 0),
              'tag': getattr(info, 'st_reparse_tag', 0)}
    if _reparse(path):
        try:
            result['link'] = str(_readlink(path))
        except OSError:
            result['link'] = '<unreadable>'
    return result


def _plan_fingerprint(source: Path, target: Path) -> dict:
    # Parent identity and reparse state matter; directory timestamps legitimately
    # change as sibling entries in a batch are created, so are not compared.
    parents = []
    for path in target.parents:
        info = _fingerprint(path)
        parents.append([str(path), {k: info.get(k) for k in ('exists', 'dev', 'ino', 'mode', 'tag', 'link')}])
    return {'source': _fingerprint(source), 'target': _fingerprint(target), 'parents': parents}


def _matches_fingerprint(path: Path, expected: dict, hardlink: bool = False) -> bool:
    actual = _fingerprint(path)
    expected = dict(expected)
    if hardlink:
        # Creating/removing another hardlink in this batch changes these two
        # fields on every name of the same file, without changing its contents.
        for field in ('nlink', 'ctime'):
            actual.pop(field, None)
            expected.pop(field, None)
    return actual == expected


def _check_parent_identity(target: Path, expected: dict,
                           created_parents: dict[str, Fingerprint] | None = None) -> None:
    fields = ('exists', 'dev', 'ino', 'mode', 'tag', 'link')
    for raw, fingerprint in expected.get('parents', []):
        if not fingerprint.get('exists'):
            actual = _fingerprint(_path(raw))
            if actual.get('exists'):
                # Shared missing ancestors may be created by an earlier item,
                # but an unrelated directory must not be accepted as ours.
                known = (created_parents or {}).get(raw)
                if not known or any(actual.get(field) != known.get(field) for field in fields):
                    raise ValueError(f'预览时缺失的目标父目录已由外部创建或替换：{raw}')
            continue
        actual = _fingerprint(_path(raw))
        actual = {key: actual.get(key) for key in fields}
        if actual != fingerprint:
            raise ValueError(f'目标父目录发生变化：{raw}')


def _reject_existing_cycles(pairs: list[tuple[Path, Path]], specs: list[LinkSpec]) -> None:
    """Check directory reachability including links already inside sources.

    Reading entries never follows an unknown reparse point. Known directory
    links are expanded explicitly, with a visited set to bound existing loops.
    A planned target substitutes its new source, rather than its old pointer.
    """
    planned = {_key(target): (source, spec.kind) for (source, target), spec in zip(pairs, specs)}
    for (source, target), spec in zip(pairs, specs):
        if spec.kind not in ('junction', 'dir_symlink') or not source.is_dir():
            continue
        pending = [source]
        visited: set[str] = set()
        while pending:
            directory = pending.pop()
            key = _key(directory)
            if key in visited:
                continue
            visited.add(key)
            if _within(target, directory) or _within(directory, target):
                raise ValueError(f'源目录内已有链接可到达目标，可能形成循环：{source} → {target}，经过 {directory}')
            if _reparse(directory):
                kind = _kind(directory)
                if kind not in ('junction', 'dir_symlink'):
                    raise ValueError(f'循环检查遇到无法安全跟随的重解析目录：{directory}')
                pending.append(_readlink(directory))
                continue
            if not directory.is_dir():
                continue  # A broken existing link cannot currently be traversed.
            # Planned entries which do not yet exist are also traversable after
            # this batch and therefore contribute edges to the graph.
            for (new_source, new_target), new_spec in zip(pairs, specs):
                if _within(new_target, directory) and new_spec.kind in ('junction', 'dir_symlink'):
                    pending.append(new_source)
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    replacement = planned.get(_key(path))
                    if replacement is not None:
                        new_source, new_kind = replacement
                        if new_kind in ('junction', 'dir_symlink'):
                            pending.append(new_source)
                        continue
                    if _reparse(path):
                        kind = _kind(path)
                        if kind in ('junction', 'dir_symlink'):
                            pending.append(_readlink(path))
                    elif entry.is_dir(follow_symlinks=False):
                        pending.append(path)


def _tree_fingerprint(path: Path) -> str:
    """Detect edits to a renamed backup without following reparse points."""
    digest = hashlib.sha256()
    if not _exists(path):
        return 'missing'
    stack = [(path, '.')]
    while stack:
        item, relative = stack.pop()
        info = _fingerprint(item)
        if _reparse(item):
            info['link'] = os.readlink(item)  # Relative links survive a rename.
        if relative == '.':
            info.pop('ctime', None)  # Renaming the backup changes this field.
        digest.update(json.dumps([relative, info], sort_keys=True).encode('utf-8'))
        if _reparse(item):
            continue
        if item.is_dir():
            for child in sorted(item.iterdir(), key=lambda p: p.name, reverse=True):
                stack.append((child, relative + '/' + child.name))
        elif item.is_file():
            with item.open('rb') as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(block)
    return digest.hexdigest()


def _deletion_inventory(root: Path) -> dict[str, dict]:
    """Snapshot a target tree without traversing any reparse entry."""
    root = _path(root)
    if root == Path(root.anchor):
        raise ValueError('禁止删除磁盘根目录')
    _reject_reparse(root, include_self=False)
    inventory = {}
    pending = [root]
    while pending:
        path = pending.pop()
        if not _within(_path(path), root):
            raise ValueError(f'删除路径超出指定目标：{path}')
        _reject_reparse(path, include_self=False)
        kind = _kind(path)
        if kind in ('unknown_reparse', 'other', 'missing'):
            raise ValueError(f'目标树包含无法安全删除的入口：{path}')
        inventory[str(path)] = _fingerprint(path)
        _reject_shared_readonly(path, inventory[str(path)])
        if kind == 'directory':
            with os.scandir(path) as entries:
                pending.extend(Path(entry.path) for entry in entries)
    return inventory


def _reject_shared_readonly(path: Path, fingerprint: dict) -> None:
    if (stat.S_ISREG(fingerprint.get('mode', 0))
            and fingerprint.get('nlink', 1) > 1
            and fingerprint.get('attributes', 0) & 1):
        raise ValueError(f'只读文件有多个硬链接名称，清除属性会影响源或外部数据，拒绝删除：{path}')


def _delete_target(root: Path, operation: OperationRecord, checkpoint) -> None:
    """Remove one explicitly authorized target, never following its links.

    All entries are checked before the first deletion. Every removal also
    rechecks the absolute boundary, ancestor reparse state and entry identity.
    """
    root = _path(root)
    inventory = _deletion_inventory(root)
    if _tree_fingerprint(root) != operation['deletion_fingerprint']:
        raise ValueError('永久删除前目标内容发生变化，请重新预览')
    # Descendants precede parents. A directory's times/size change as its own
    # children are removed, so its identity is compared separately.
    paths = sorted(inventory, key=lambda raw: len(Path(raw).parts), reverse=True)
    for raw in paths:
        path = _path(raw)
        if not _within(path, root) or root == Path(root.anchor):
            raise ValueError(f'删除路径超出指定目标：{path}')
        _reject_reparse(path, include_self=False)
        expected = inventory[raw]
        actual = _fingerprint(path)
        _reject_shared_readonly(path, actual)
        ordinary_dir = stat.S_ISDIR(expected['mode']) and not expected.get('tag') and not stat.S_ISLNK(expected['mode'])
        if ordinary_dir:
            fields = ('exists', 'dev', 'ino', 'mode', 'attributes', 'tag')
            equal = all(actual.get(k) == expected.get(k) for k in fields)
        else:
            # Other names of an NTFS hardlink may have been removed earlier.
            equal = _matches_fingerprint(path, expected, hardlink=True)
        if not equal:
            raise ValueError(f'永久删除期间入口发生变化：{path}')
        kind = _kind(path)
        if kind in ('unknown_reparse', 'other', 'missing'):
            raise ValueError(f'拒绝删除无法识别的入口：{path}')
        operation['deletion'] = 'in_progress'
        operation['deletion_current'] = str(path)
        checkpoint()  # Intent is durable even if the process stops here.
        operation['deletion_attempted'] = True
        try:
            if kind in ('directory', 'junction') or (os.name == 'nt' and kind == 'dir_symlink'):
                path.rmdir()
            else:
                path.unlink()
        except PermissionError:
            # Clear only an ordinary entry's read-only attribute. Never change
            # attributes through a reparse point to its external destination.
            current = _fingerprint(path)
            _reject_shared_readonly(path, current)
            same = (all(current.get(k) == expected.get(k) for k in fields) if ordinary_dir
                    else _matches_fingerprint(path, expected, hardlink=True))
            if _reparse(path) or not same:
                raise
            if os.name == 'nt':
                from ctypes import wintypes
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                setter = kernel.SetFileAttributesW
                setter.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
                setter.restype = wintypes.BOOL
                attributes = getattr(path.lstat(), 'st_file_attributes', 0)
                if not attributes & 1 or not setter(str(path), (attributes & ~1) or 0x80):
                    raise
            else:
                path.chmod(path.lstat().st_mode | stat.S_IWUSR)
            _reject_reparse(path, include_self=False)
            if _reparse(path):
                raise ValueError('清除只读属性后入口变成重解析点，已停止删除')
            if kind == 'directory':
                path.rmdir()
            else:
                path.unlink()
        operation['deleted_entries'] = operation.get('deleted_entries', 0) + 1
        checkpoint()
    operation['deletion'] = 'completed'
    operation.pop('deletion_current', None)
    checkpoint()


def _remove_link(path: Path, kind: str, source: Path | None = None, expected: dict | None = None) -> None:
    _reject_reparse(path, include_self=False)
    if expected is not None and not _matches_fingerprint(path, expected, kind == 'hardlink'):
        raise ValueError('删除前目标已改变，拒绝移除')
    if kind == 'hardlink':
        if source is None:
            raise ValueError('硬链接删除必须再次确认源名称')
        _reject_reparse(source)
        if not source.is_file() or _kind(path, source) != 'hardlink':
            raise ValueError('硬链接源已丢失、目标已改变或只剩最后一个名称，拒绝删除')
    elif _kind(path) != kind:
        raise ValueError('删除前入口已不再是预期链接，拒绝删除普通数据')
    if kind == 'junction' or (os.name == 'nt' and kind == 'dir_symlink'):
        path.rmdir()
    elif kind in KINDS:
        path.unlink()
    else:
        raise ValueError('拒绝删除普通数据或无法识别的重解析点')


def _result(success: bool, results: list[dict], message: str) -> OperationResult:
    return {'success': success, 'results': results, 'message': message}


class LinkService:
    def __init__(self, state_dir: Path, protected_paths: list[Path] | None = None):
        self.state_dir = _path(state_dir)
        self.protected_paths = [_path(Path.cwd()), self.state_dir]
        self.protected_paths.extend(_path(p) for p in (protected_paths or []))
        _reject_reparse(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.history_path = self.state_dir / 'history.json'
        self._history = HistoryRepository(self.state_dir)
        self._lock = self._history.lease

    def inspect(self, spec: LinkSpec) -> Inspection:
        result: Inspection = {'status': '检查失败', 'correct': False, 'target_kind': 'unknown', 'actual_target': '',
                  'source_exists': False, 'target_exists': False, 'detail': '',
                  'resolved_source': '', 'resolved_target': ''}
        try:
            source, target = _path(spec.source), _path(spec.target)
            result.update(source_exists=source.exists(), target_exists=_exists(target),
                          resolved_source=str(source), resolved_target=str(target))
            # Do not traverse target parents which are themselves links.
            _reject_reparse(target, include_self=False)
            _reject_reparse(source)
            kind = _kind(target, source)
            result['target_kind'] = kind
            if kind in ('junction', 'dir_symlink', 'file_symlink'):
                actual = _readlink(target)
                result['actual_target'] = str(actual)
                correct = kind == spec.kind and _key(actual) == _key(source)
                result['correct'] = correct and source.exists()
                result['status'] = '链接正确' if correct else '链接不匹配'
                if not source.exists():
                    result['status'] = '链接源缺失'
            elif kind == 'hardlink':
                result['correct'] = spec.kind == 'hardlink'
                result['actual_target'] = str(source)
                result['status'] = '链接正确' if spec.kind == 'hardlink' else '目标为硬链接'
            elif kind == 'missing':
                result['status'] = '待创建' if source.exists() else '源路径不存在'
            elif kind == 'unknown_reparse':
                result['status'] = '未知重解析点'
            else:
                result['status'] = '目标已有普通数据'
            result['detail'] = {
                '链接正确': '链接类型和指向均与配置一致。',
                '链接不匹配': '当前链接的类型或指向与配置不一致。',
                '链接源缺失': '配置的源路径不存在，请检查数据位置。',
                '目标为硬链接': '目标与源共享同一文件，但配置的链接类型不同。',
                '待创建': '目标不存在，创建时将建立新的链接入口。',
                '源路径不存在': '源路径不存在，不能创建链接。',
                '未知重解析点': '目标是无法识别的重解析点，不能安全替换。',
                '目标已有普通数据': '目标已有普通数据，替换前需选择备份或永久删除。',
            }[result['status']]
        except (OSError, ValueError) as error:
            result['detail'] = str(error)
        return result

    def _validate(self, specs: list[LinkSpec], require_source: bool = True) -> list[tuple[Path, Path]]:
        pairs: list[tuple[Path, Path]] = []
        for spec in specs:
            if spec.kind not in KINDS:
                raise ValueError(f'未知链接类型：{spec.kind}')
            source, target = _path(spec.source), _path(spec.target)
            if target == Path(target.anchor):
                raise ValueError(f'禁止以磁盘根目录为目标：{target}')
            if _within(target, source) or _within(source, target):
                raise ValueError(f'源与目标不能相同或互相包含：{source} → {target}')
            for protected in self.protected_paths:
                if _within(protected, target) or _within(target, protected):
                    raise ValueError(f'目标与受保护目录重叠：{target} / {protected}')
            _reject_reparse(source)
            _reject_reparse(target, include_self=False)
            if require_source:
                if not source.exists():
                    raise ValueError(f'源路径不存在：{source}')
                directory = spec.kind in ('junction', 'dir_symlink')
                if directory and not source.is_dir():
                    raise ValueError(f'{spec.kind} 需要目录源：{source}')
                if not directory and not source.is_file():
                    raise ValueError(f'{spec.kind} 需要文件源：{source}')
                if spec.kind == 'junction' and str(source).startswith('\\\\'):
                    raise ValueError('Junction 源不支持 UNC 网络路径')
                ancestor = target.parent
                while not ancestor.exists():
                    if ancestor == ancestor.parent:
                        break
                    ancestor = ancestor.parent
                if not ancestor.is_dir():
                    raise ValueError(f'目标父路径不是目录：{ancestor}')
                if spec.kind == 'hardlink' and source.stat().st_dev != ancestor.stat().st_dev:
                    raise ValueError('硬链接源和目标必须位于同一文件系统')
            pairs.append((source, target))
        for index, (source, target) in enumerate(pairs):
            for other_index, (_, other_target) in enumerate(pairs):
                if index != other_index and (_within(target, other_target) or _within(other_target, target)):
                    raise ValueError(f'目标重复或相互包含：{target} / {other_target}')
                if _within(source, other_target):
                    raise ValueError(f'源位于本批次目标之内，可能形成循环：{source} / {other_target}')
        # Links created inside another source can also form an indirect cycle,
        # e.g. A -> B/linkA and B -> A/linkB. Lexical source-in-target checks
        # alone do not catch that graph.
        edges = {index: [other for other, (_, target) in enumerate(pairs)
                         if _within(target, source)]
                 for index, (source, _) in enumerate(pairs)}
        visiting: set[int] = set()
        visited: set[int] = set()
        def visit(index: int) -> None:
            if index in visiting:
                raise ValueError('本批次链接存在间接循环，请调整源或目标路径')
            if index in visited:
                return
            visiting.add(index)
            for other in edges[index]:
                visit(other)
            visiting.remove(index)
            visited.add(index)
        for index in edges:
            visit(index)
        if require_source:
            _reject_existing_cycles(pairs, specs)
        return pairs

    def preview(self, specs: list[LinkSpec], backup_existing: bool = False,
                delete_existing: bool = False) -> list[PlanItem]:
        plan: list[PlanItem] = []
        try:
            if backup_existing and delete_existing:
                raise ValueError('备份已有数据与永久删除目标不能同时启用')
            self._validate(specs)
            batch_error = ''
        except (OSError, ValueError) as error:
            batch_error = str(error)
        for spec in specs:
            item: PlanItem = {'spec': spec, 'source': spec.source, 'target': spec.target,
                    'action': ACTION_LABELS[PlanAction.BLOCK], 'action_code': PlanAction.BLOCK,
                    'valid': False, 'error': batch_error,
                    'status': '检查失败', 'backup': '', 'fingerprint': {},
                    'backup_existing': bool(backup_existing),
                    'delete_existing': bool(delete_existing)}
            try:
                source, target = _path(spec.source), _path(spec.target)
                item.update(source=str(source), target=str(target))
                inspected = self.inspect(spec)
                item['status'] = inspected['status']
                if batch_error:
                    plan.append(item)
                    continue
                kind = inspected['target_kind']
                if delete_existing and kind not in ('missing', 'unknown_reparse', 'other'):
                    if kind in ('junction', 'dir_symlink', 'file_symlink', 'hardlink'):
                        item['action_code'] = PlanAction.DELETE_LINK
                    else:
                        item['action_code'] = PlanAction.DELETE_DATA
                    _deletion_inventory(target)  # Reject unknown nested reparse entries.
                    item['deletion_fingerprint'] = _tree_fingerprint(target)
                elif inspected['correct']:
                    item['action_code'] = PlanAction.SKIP
                elif kind == 'missing':
                    item['action_code'] = PlanAction.CREATE
                elif kind in ('junction', 'dir_symlink', 'file_symlink'):
                    item['action_code'] = PlanAction.REPLACE_LINK
                elif kind == 'unknown_reparse' or kind == 'other':
                    raise ValueError('目标类型无法安全替换')
                elif backup_existing:
                    item['action_code'] = PlanAction.BACKUP
                    item['backup'] = str(target.with_name(target.name + '.linkmanager-backup-' + uuid.uuid4().hex[:12]))
                else:
                    raise ValueError('目标已有普通数据；请启用“先备份已有数据”或“永久删除目标后创建”')
                item['fingerprint'] = _plan_fingerprint(source, target)
                item['action'] = ACTION_LABELS[item['action_code']]
                item['valid'] = True
            except (OSError, ValueError) as error:
                item.update(valid=False, error=str(error), action=ACTION_LABELS[PlanAction.BLOCK],
                            action_code=PlanAction.BLOCK)
            plan.append(item)
        return plan

    def history(self) -> list[HistoryRecord]:
        return list(reversed(self._history.load()))

    def _store(self, record: HistoryRecord) -> None:
        self._history.store(record)

    def _record(self, action: str) -> HistoryRecord:
        record: HistoryRecord = {'schema_version': 1, 'recovery_required': False,
                  'id': uuid.uuid4().hex, 'time': datetime.now(timezone.utc).isoformat(),
                  'action': action, 'success': False, 'message': '操作进行中；若程序中断请检查已记录的变更',
                  'results': [], 'operations': [], 'state': 'pending'}
        self._store(record)
        return record

    def _finish(self, record: HistoryRecord, success: bool, message: str) -> OperationResult:
        record.update(success=success, message=message, state='completed' if success else 'failed')
        try:
            self._store(record)
        except Exception as error:
            return _result(False, record['results'], message + f'；最终历史记录保存失败：{error}。请检查实际入口与现有 pending 记录')
        return _result(success, record['results'], message)

    def _ensure_parents(self, target: Path, operation: OperationRecord, record: HistoryRecord) -> None:
        missing = []
        ancestor = target.parent
        while not ancestor.exists():
            missing.append(ancestor)
            ancestor = ancestor.parent
        for directory in reversed(missing):
            _reject_reparse(directory)
            operation['parent_pending'] = str(directory)
            self._store(record)
            directory.mkdir()
            operation.setdefault('parents', []).append(str(directory))
            operation.setdefault('parent_fingerprints', {})[str(directory)] = _fingerprint(directory)
            operation.pop('parent_pending', None)
            self._store(record)

    @staticmethod
    def _ordered_batch(plan: list[PlanItem], pairs: list[tuple[Path, Path]]) -> list[tuple[PlanItem, tuple[Path, Path]]]:
        """Read a source before any other operation mutates its directory tree."""
        return [(plan[index], pairs[index]) for index in ordered_indices(pairs)]

    def execute(self, plan: list[PlanItem]) -> OperationResult:
        with self._lock:
            if not plan:
                return _result(True, [], '没有选中的配置项')
            record: HistoryRecord | None = None
            try:
                if any(not p.get('valid') for p in plan):
                    raise ValueError('预览中存在被阻止的项目，整批操作未执行')
                specs = [p['spec'] for p in plan]
                pairs = self._validate(specs)
                ordered = self._ordered_batch(plan, pairs)
                # Validate every item before even writing the pending transaction.
                for item, (source, target) in zip(plan, pairs):
                    if item.get('source') != str(source) or item.get('target') != str(target):
                        raise ValueError('配置或展开后的路径已变化，请重新预览')
                    if item.get('fingerprint') != _plan_fingerprint(source, target):
                        raise ValueError(f'预览后路径发生变化，请重新预览：{target}')
                    if item.get('delete_existing') and item.get('deletion_fingerprint'):
                        _deletion_inventory(target)
                        if _tree_fingerprint(target) != item['deletion_fingerprint']:
                            raise ValueError(f'预览后目标内容发生变化，请重新预览：{target}')
                    if item.get('backup') and _exists(_path(item['backup'])):
                        raise ValueError(f'备份路径已存在：{item["backup"]}')
                    fresh = self.preview([item['spec']], bool(item.get('backup_existing')),
                                         bool(item.get('delete_existing')))[0]
                    if not fresh['valid'] or fresh['action_code'] != item.get('action_code'):
                        raise ValueError(f'操作条件已变化：{target}')
                record = self._record('create')
                for item, (source, target) in ordered:
                    self._validate(specs)
                    created_parents = {raw: fingerprint for previous in record['operations']
                                       for raw, fingerprint in previous.get('parent_fingerprints', {}).items()}
                    _check_parent_identity(target, item['fingerprint'], created_parents)
                    hardlink = item['spec'].kind == 'hardlink'
                    if not _matches_fingerprint(source, item['fingerprint']['source'], hardlink) or not _matches_fingerprint(target, item['fingerprint']['target'], hardlink):
                        raise ValueError(f'执行期间源或目标发生变化：{target}')
                    if item.get('delete_existing') and item.get('deletion_fingerprint'):
                        _deletion_inventory(target)
                        if _tree_fingerprint(target) != item['deletion_fingerprint']:
                            raise ValueError(f'执行期间目标内容发生变化：{target}')
                    if item['action_code'] == PlanAction.SKIP:
                        if not self.inspect(item['spec'])['correct']:
                            raise ValueError(f'执行期间链接状态发生变化：{target}')
                        record['results'].append({'project': item['spec'].project, 'target': str(target), 'success': True, 'message': '链接已正确，无需更改'})
                        self._store(record)
                        continue
                    operation: OperationRecord = {'spec': asdict(item['spec']), 'source': str(source), 'target': str(target),
                                 'backup': item.get('backup', ''), 'phase': 'prepared', 'parents': [],
                                 'original_fingerprint': _fingerprint(target),
                                 'delete_existing': bool(item.get('delete_existing')),
                                 'irreversible': False}
                    old_kind = _kind(target, source)
                    if old_kind in ('junction', 'dir_symlink', 'file_symlink'):
                        operation['old_link'] = {'kind': old_kind, 'source': str(_readlink(target))}
                    elif old_kind == 'hardlink' and item.get('delete_existing'):
                        operation['old_link'] = {'kind': old_kind, 'source': str(source)}
                    record['operations'].append(operation)
                    self._store(record)
                    self._ensure_parents(target, operation, record)
                    created_parents.update(operation.get('parent_fingerprints', {}))
                    _check_parent_identity(target, item['fingerprint'], created_parents)
                    if operation['delete_existing'] and old_kind != 'missing':
                        operation['deletion_fingerprint'] = item['deletion_fingerprint']
                        operation['phase'] = 'delete_pending'
                        operation['deletion'] = 'pending'
                        # A pending destructive operation must already disclose
                        # the inability to restore ordinary data after a crash.
                        operation['irreversible'] = old_kind in ('file', 'directory')
                        record['irreversible'] = any(op.get('irreversible') for op in record['operations'])
                        self._store(record)
                        _delete_target(target, operation, lambda: self._store(record))
                        operation['phase'] = 'target_deleted'
                        self._store(record)
                    elif operation['backup']:
                        backup = _path(operation['backup'])
                        if backup.parent != target.parent or _exists(backup):
                            raise ValueError('备份必须为同一目录内的未使用路径')
                        operation['backup_fingerprint'] = _tree_fingerprint(target)
                        operation['phase'] = 'backup_pending'
                        self._store(record)
                        target.rename(backup)
                        operation['phase'] = 'backed_up'
                        self._store(record)
                    elif 'old_link' in operation:
                        operation['phase'] = 'remove_pending'
                        self._store(record)
                        _remove_link(target, old_kind, source, operation['original_fingerprint'])
                        operation['phase'] = 'old_removed'
                        self._store(record)
                    _reject_reparse(target, include_self=False)
                    _reject_reparse(source)
                    if _exists(target):
                        raise ValueError(f'创建前目标再次出现：{target}')
                    operation['phase'] = 'create_pending'
                    self._store(record)
                    _create(source, target, item['spec'].kind)
                    operation['phase'] = 'created'
                    operation['created_fingerprint'] = _fingerprint(target)
                    self._store(record)
                    if not self.inspect(item['spec'])['correct']:
                        raise OSError(f'创建后的链接验证失败：{target}')
                    detail = '已创建并验证链接'
                    if operation['delete_existing']:
                        detail += '；未备份'
                    if operation['irreversible']:
                        detail += '；普通目标数据已永久删除，不能恢复'
                    elif operation['backup']:
                        detail += f'；原数据备份：{operation["backup"]}'
                    record['results'].append({'project': item['spec'].project, 'target': str(target),
                                              'success': True, 'message': detail,
                                              'irreversible': operation['irreversible'],
                                              'backup': operation['backup']})
                    self._store(record)
                message = f'已完成 {len(plan)} 项；所有链接均已验证'
                if record.get('irreversible'):
                    message += '；普通目标数据已永久删除，不能恢复'
                return self._finish(record, True, message)
            except Exception as error:
                if isinstance(error, OSError) and getattr(error, 'winerror', None) == 1314:
                    error = OSError('创建符号链接需要启用 Windows 开发者模式或使用管理员权限（WinError 1314）')
                if record is None:
                    results = [{'project': getattr(p.get('spec'), 'project', ''), 'target': p.get('target', ''), 'success': False, 'message': str(error)} for p in plan]
                    return _result(False, results, f'整批未执行：{error}')
                rollback_errors = []
                for operation in reversed(record['operations']):
                    try:
                        if operation.get('irreversible'):
                            # Remove only a known newly created link. A partially
                            # deleted ordinary tree is preserved for inspection.
                            if operation.get('created_fingerprint') and _exists(_path(operation['target'])):
                                _remove_link(_path(operation['target']), operation['spec']['kind'],
                                             _path(operation['source']), operation['created_fingerprint'])
                                operation['new_link_removed'] = True
                            operation['phase'] = 'irreversible_failure'
                            self._store(record)
                            continue
                        self._revert(operation, lambda: self._store(record))
                        operation['phase'] = 'rolled_back'
                        self._store(record)
                    except Exception as rollback_error:
                        rollback_errors.append(f'{operation["target"]}：{rollback_error}')
                for result in record['results']:
                    result.update(success=False, message=('事务失败；普通目标数据已永久删除，不能恢复；已尝试移除本次新链接'
                                  if result.get('irreversible') else '事务失败，已尝试回滚；请查看整体结果'))
                record['results'].append({'project': '', 'target': '', 'success': False, 'message': str(error)})
                if record.get('irreversible'):
                    attempted = any(op.get('deletion_attempted') for op in record['operations']
                                    if op.get('irreversible'))
                    if attempted:
                        message = f'执行失败：{error}。永久删除已开始，可能部分删除或已全部删除；普通目标数据不能恢复；未备份。已尝试移除本次新链接，保留尚未删除的数据'
                    else:
                        message = f'执行失败：{error}。未执行普通数据的永久删除；目标保留，请重新预览；此记录包含永久删除准备阶段，不能自动撤销'
                    if rollback_errors:
                        message += '；链接回滚未完成：' + '；'.join(rollback_errors)
                else:
                    message = f'执行失败：{error}。' + ('回滚未完成：' + '；'.join(rollback_errors) if rollback_errors else '本次变更已回滚')
                record['recovery_required'] = bool(rollback_errors) or (
                    bool(record.get('irreversible')) and any(op.get('deletion_attempted') for op in record['operations']))
                return self._finish(record, False, message)

    def _check_revert(self, operation: OperationRecord) -> None:
        self._validate([LinkSpec(**operation['spec'])], require_source=False)
        target = _path(operation['target'])
        _reject_reparse(target, include_self=False)
        for raw in operation.get('parents', []):
            parent = _path(raw)
            if parent == target or not _within(target, parent) or parent == Path(parent.anchor):
                raise ValueError('历史父目录不属于目标路径，拒绝自动清理')
            if _exists(parent):
                _reject_reparse(parent)
                expected = operation.get('parent_fingerprints', {}).get(raw)
                actual = _fingerprint(parent)
                if not expected or any(actual.get(field) != expected.get(field)
                                       for field in ('dev', 'ino', 'mode', 'attributes', 'tag')):
                    raise ValueError(f'父目录身份缺失或发生变化，拒绝撤销：{parent}')
        if operation.get('phase') == 'rolled_back':
            expected = operation.get('restored_fingerprint', operation.get('original_fingerprint'))
            if not expected or not _matches_fingerprint(target, expected, operation['spec']['kind'] == 'hardlink'):
                raise ValueError('已回滚的入口再次改变，拒绝继续恢复')
            return
        if operation.get('phase') == 'restore_pending' and self._restoration_completed(operation):
            return
        if operation.get('phase') == 'restored':
            old_hardlink = operation.get('old_link', {}).get('kind') == 'hardlink'
            if not _matches_fingerprint(target, operation.get('restored_fingerprint', {}), old_hardlink):
                raise ValueError('已恢复的入口再次改变，拒绝继续撤销')
            return
        if operation.get('phase') in ('prepared', 'backup_pending', 'remove_pending', 'delete_pending'):
            # A failed mutation can leave its original entry untouched. Never
            # treat such an entry as a newly created link to remove.
            if _exists(target):
                original = operation.get('original_fingerprint')
                if original and _matches_fingerprint(target, original, operation['spec']['kind'] == 'hardlink'):
                    return
                raise ValueError('准备阶段的原入口已改变，拒绝恢复')
            if operation.get('phase') == 'prepared' and operation.get('original_fingerprint', {}).get('exists'):
                # No original entry has been moved or removed at this phase.
                # A second name appearing later cannot be assumed to be ours.
                raise ValueError('准备阶段的原入口已丢失，缺少已核验的变更记录，拒绝恢复')
        if _exists(target):
            expected = operation.get('created_fingerprint')
            if expected is None or not _matches_fingerprint(target, expected, operation['spec']['kind'] == 'hardlink'):
                raise ValueError('目标已改变，拒绝撤销')
            spec = LinkSpec(**operation['spec'])
            if not self.inspect(spec)['correct']:
                raise ValueError('链接指向或源已改变，拒绝撤销')
        elif operation.get('phase') == 'created':
            raise ValueError('已创建的链接已被外部删除，拒绝自动撤销')
        backup = operation.get('backup')
        if backup:
            backup_path = _path(backup)
            for protected in self.protected_paths:
                if _within(protected, backup_path) or _within(backup_path, protected):
                    raise ValueError(f'原数据备份与受保护目录重叠：{backup_path}')
        if backup and operation.get('phase') not in ('prepared', 'backup_pending'):
            path = _path(backup)
            _reject_reparse(path, include_self=False)
            expected_backup = operation.get('backup_fingerprint')
            if not expected_backup or _tree_fingerprint(path) != expected_backup:
                raise ValueError('原数据备份已改变或丢失，拒绝恢复')
        elif backup and operation.get('phase') == 'backup_pending' and not _exists(target):
            path = _path(backup)
            _reject_reparse(path, include_self=False)
            if _tree_fingerprint(path) != operation.get('backup_fingerprint'):
                raise ValueError('未完成备份的原数据已改变或丢失，拒绝恢复')
        if operation.get('old_link'):
            old = operation['old_link']
            old_source = _path(old['source'])
            _reject_reparse(old_source)
            self._validate([LinkSpec(operation['spec']['project'], str(old_source),
                                     str(target), old['kind'])], require_source=False)
            if old['kind'] == 'hardlink':
                original = operation.get('original_fingerprint')
                if not original or not _matches_fingerprint(old_source, original, hardlink=True):
                    raise ValueError('原硬链接源身份或内容已改变，拒绝恢复')

    def _restoration_completed(self, operation: OperationRecord) -> bool:
        """Recognize restoration after the final phase write was interrupted."""
        target = _path(operation['target'])
        if not _exists(target):
            return False
        backup = operation.get('backup')
        if backup and not _exists(_path(backup)):
            return _tree_fingerprint(target) == operation.get('backup_fingerprint')
        old = operation.get('old_link')
        if old and old['kind'] == 'hardlink':
            return (_kind(target, _path(old['source'])) == 'hardlink'
                    and _matches_fingerprint(target, operation.get('original_fingerprint', {}), hardlink=True))
        if old and _kind(target) == old['kind']:
            return _key(_readlink(target)) == _key(_path(old['source']))
        return False

    def _revert(self, operation: OperationRecord, checkpoint=None) -> None:
        if operation.get('phase') == 'rolled_back':
            return
        self._check_revert(operation)
        target = _path(operation['target'])
        if operation.get('phase') == 'restore_pending' and self._restoration_completed(operation):
            operation['phase'] = 'restored'
            operation['restored_fingerprint'] = _fingerprint(target)
            if checkpoint:
                checkpoint()
        if operation.get('phase') in ('prepared', 'backup_pending', 'remove_pending', 'delete_pending') and _exists(target):
            original = operation.get('original_fingerprint')
            if original and _matches_fingerprint(target, original, operation['spec']['kind'] == 'hardlink'):
                return
        if operation.get('phase') != 'restored':
            if _exists(target):
                operation['phase'] = 'remove_created_pending'
                if checkpoint:
                    checkpoint()
                _remove_link(target, operation['spec']['kind'], _path(operation['source']), operation.get('created_fingerprint'))
                operation['phase'] = 'created_removed'
                if checkpoint:
                    checkpoint()
            backup = operation.get('backup')
            if backup and _exists(_path(backup)):
                operation['phase'] = 'restore_pending'
                if checkpoint:
                    checkpoint()
                _path(backup).rename(target)
                operation['phase'] = 'restored'
                operation['restored_fingerprint'] = _fingerprint(target)
                if checkpoint:
                    checkpoint()
            elif operation.get('old_link') and operation.get('phase') != 'prepared':
                old = operation['old_link']
                operation['phase'] = 'restore_pending'
                if checkpoint:
                    checkpoint()
                _create(_path(old['source']), target, old['kind'])
                operation['phase'] = 'restored'
                operation['restored_fingerprint'] = _fingerprint(target)
                if checkpoint:
                    checkpoint()
        for parent in reversed(operation.get('parents', [])):
            path = _path(parent)
            if _exists(path):
                _reject_reparse(path)
                expected = operation.get('parent_fingerprints', {}).get(str(path))
                if not expected:
                    raise ValueError(f'本次创建的父目录身份缺失，拒绝清理：{path}')
                actual = _fingerprint(path)
                if any(actual.get(field) != expected.get(field)
                       for field in ('dev', 'ino', 'mode', 'attributes', 'tag')):
                    raise ValueError(f'本次创建的父目录身份已变化，拒绝清理：{path}')
                try:
                    path.rmdir()
                except OSError:
                    pass  # New unrelated contents are preserved.

    def unlink(self, specs: list[LinkSpec]) -> OperationResult:
        with self._lock:
            if not specs:
                return _result(True, [], '没有选中的配置项')
            record = None
            try:
                pairs = self._validate(specs, require_source=False)
                fingerprints = []
                for spec, (source, target) in zip(specs, pairs):
                    kind = _kind(target, source)
                    if kind in ('junction', 'dir_symlink', 'file_symlink'):
                        if kind != spec.kind or _key(_readlink(target)) != _key(source):
                            raise ValueError(f'目标链接与配置不匹配，拒绝移除：{target}')
                    elif kind == 'hardlink' and spec.kind == 'hardlink':
                        pass
                    else:
                        raise ValueError(f'目标不是可确认的配置链接，拒绝删除：{target}')
                    fingerprints.append(_fingerprint(target))
                record = self._record('unlink')
                for spec, (source, target), fingerprint in zip(specs, pairs, fingerprints):
                    self._validate(specs, require_source=False)
                    if not _matches_fingerprint(target, fingerprint, spec.kind == 'hardlink'):
                        raise ValueError(f'移除前目标发生变化：{target}')
                    operation: OperationRecord = {'spec': asdict(spec), 'source': str(source), 'target': str(target), 'phase': 'remove_pending'}
                    record['operations'].append(operation)
                    self._store(record)
                    _remove_link(target, spec.kind, source, fingerprint)
                    operation['phase'] = 'removed'
                    record['results'].append({'project': spec.project, 'target': str(target), 'success': True, 'message': '已移除链接入口；源数据保留'})
                    self._store(record)
                return self._finish(record, True, f'已移除 {len(specs)} 个链接入口；此记录暂不支持自动撤销')
            except Exception as error:
                if record is None:
                    return _result(False, [{'project': s.project, 'target': s.target, 'success': False, 'message': str(error)} for s in specs], f'整批未执行：{error}')
                record['results'].append({'project': '', 'target': '', 'success': False, 'message': str(error)})
                return self._finish(record, False, f'移除失败：{error}；已移除项见结果，源数据保留，未执行项保持原状')

    def undo(self, record_id: str) -> OperationResult:
        with self._lock:
            transaction = None
            try:
                record = next((r for r in self.history() if r['id'] == record_id), None)
                if record is None:
                    raise ValueError('找不到该历史记录')
                if record.get('action') != 'create' or not record.get('success') or record.get('state') != 'completed':
                    raise ValueError('仅支持撤销成功完成的创建操作；移除、失败和中断记录请手动检查')
                if record.get('undone'):
                    raise ValueError('该记录已经撤销')
                if record.get('irreversible') or any(op.get('irreversible') for op in record.get('operations', [])):
                    raise ValueError('该记录包含普通目标数据的永久删除，未备份，不能恢复或自动撤销')
                for operation in record['operations']:
                    self._check_revert(operation)
                transaction = self._record('undo')
                transaction['undo_of'] = record_id
                self._store(transaction)
                for operation in reversed(record['operations']):
                    self._revert(operation, lambda: self._store(record))
                    operation['phase'] = 'rolled_back'
                    self._store(record)
                    transaction['results'].append({'project': operation['spec']['project'], 'target': operation['target'], 'success': True, 'message': '已撤销创建，恢复先前入口或普通数据'})
                    self._store(transaction)
                self._resolve_attempts(record_id, transaction['id'])
                record['undone'] = True
                record['recovery_required'] = False
                self._store(record)
                return self._finish(transaction, True, '已撤销创建操作，源数据保留')
            except Exception as error:
                if transaction is None:
                    return _result(False, [], f'撤销未执行：{error}')
                record['recovery_required'] = True
                try:
                    self._store(record)
                except Exception:
                    pass  # The previous durable phase still identifies the interruption.
                transaction['results'].append({'project': '', 'target': '', 'success': False, 'message': str(error)})
                return self._finish(transaction, False, f'撤销未全部完成：{error}；已处理项见结果，需手动检查')

    def _resolve_attempts(self, record_id: str, current_id: str) -> None:
        """Close interrupted attempts only after their original entry is restored."""
        for attempt in self.history():
            if (attempt['id'] != current_id and attempt.get('action') in ('undo', 'recover')
                    and (attempt.get('undo_of') == record_id or attempt.get('recovery_of') == record_id)
                    and attempt.get('state') in (HistoryState.PENDING, HistoryState.FAILED)):
                attempt.update(recovered=True, recovery_required=False, state=HistoryState.RECOVERED,
                               message='原创建记录已由后续操作核验并恢复；本次中断尝试已解决')
                self._store(attempt)

    def _recovery_record(self, record_id: str) -> HistoryRecord:
        record = next((item for item in self.history() if item['id'] == record_id), None)
        if record is None:
            raise ValueError('找不到该历史记录')
        if not can_recover(record):
            raise ValueError('仅可恢复尚未恢复的、可逆的中断或回滚未完成创建记录；永久删除必须手动检查')
        known_phases = {'prepared', 'backup_pending', 'backed_up', 'remove_pending',
                        'old_removed', 'create_pending', 'created', 'remove_created_pending',
                        'created_removed', 'restore_pending', 'restored', 'rolled_back',
                        'delete_pending', 'target_deleted'}
        for operation in record.get('operations', []):
            try:
                spec = LinkSpec(**operation['spec'])
                source, target = _path(operation['source']), _path(operation['target'])
                original = operation['original_fingerprint']
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError('旧操作的路径或原始指纹不完整，拒绝自动恢复') from error
            if (source != _path(spec.source) or target != _path(spec.target)
                    or not isinstance(original, dict) or type(original.get('exists')) is not bool
                    or operation.get('phase') not in known_phases):
                raise ValueError('历史操作与配置不一致或阶段/指纹不完整，拒绝自动恢复')
            if original['exists'] and not all(field in original for field in ('dev', 'ino', 'mode', 'size', 'mtime', 'ctime', 'nlink')):
                raise ValueError('原入口身份指纹不完整，拒绝自动恢复')
            if original['exists']:
                ordinary = (not original.get('tag') and not stat.S_ISLNK(original['mode']))
                if ordinary and operation.get('delete_existing') and operation.get('old_link', {}).get('kind') != 'hardlink':
                    raise ValueError('记录涉及普通目标的永久删除，拒绝自动恢复')
                if ordinary and not operation.get('backup') and not operation.get('old_link'):
                    raise ValueError('原数据没有可核验的备份，拒绝自动恢复')
                if not ordinary and not operation.get('old_link'):
                    raise ValueError('原链接信息缺失，拒绝自动恢复')
            # A journal interrupted between mkdir and its identity checkpoint
            # cannot distinguish our directory from an external replacement.
            pending_parent = operation.get('parent_pending')
            if pending_parent and _exists(_path(pending_parent)):
                raise ValueError(f'父目录已出现但身份未记录，需手动检查：{pending_parent}')
            backup = operation.get('backup')
            if backup:
                backup_path = _path(backup)
                if backup_path.parent != target.parent or backup_path == target:
                    raise ValueError('备份路径不在原目标同一目录内，拒绝自动恢复')
                # prepared/backup_pending may still have the original target.
                # An unexpected second ordinary entry must never be consumed.
                if (_exists(target) and _matches_fingerprint(target, original, spec.kind == 'hardlink')
                        and _exists(backup_path)):
                    raise ValueError('原目标与备份同时存在，需手动检查')
            if operation.get('old_link'):
                old = operation['old_link']
                if old.get('kind') not in KINDS or not isinstance(old.get('source'), str):
                    raise ValueError('原链接信息不完整，拒绝自动恢复')
            self._check_revert(operation)
        return record

    def preview_recovery(self, record_id: str) -> OperationResult:
        with self._lock:
            try:
                record = self._recovery_record(record_id)
                results = []
                for operation in reversed(record.get('operations', [])):
                    target = _path(operation['target'])
                    target_exists = _exists(target)
                    already_restored = (operation.get('phase') in ('restored', 'rolled_back')
                                        or (operation.get('phase') == 'restore_pending'
                                            and self._restoration_completed(operation)))
                    original_untouched = (
                        operation.get('phase') in ('prepared', 'backup_pending', 'remove_pending', 'delete_pending')
                        and target_exists
                        and _matches_fingerprint(target, operation['original_fingerprint'],
                                                 operation['spec']['kind'] == 'hardlink'))
                    results.append({'project': operation['spec']['project'], 'target': operation['target'],
                                    'success': True, 'message': recovery_message(
                                        operation, already_restored=already_restored,
                                        original_untouched=original_untouched, target_exists=target_exists)})
                return _result(True, results, '恢复检查通过；执行时会再次核验现有入口与备份')
            except Exception as error:
                return _result(False, [], f'恢复检查未通过：{error}')

    def recover(self, record_id: str) -> OperationResult:
        with self._lock:
            transaction = None
            record = None
            try:
                record = self._recovery_record(record_id)
                transaction = self._record('recover')
                transaction['recovery_of'] = record_id
                self._store(transaction)
                for operation in reversed(record.get('operations', [])):
                    self._revert(operation, lambda: self._store(record))
                    operation['phase'] = 'rolled_back'
                    self._store(record)
                    transaction['results'].append({'project': operation['spec']['project'],
                                                   'target': operation['target'], 'success': True,
                                                   'message': '已恢复到创建操作前的入口或普通数据'})
                    self._store(transaction)
                self._resolve_attempts(record_id, transaction['id'])
                record.update(recovered=True, recovery_required=False, state=HistoryState.RECOVERED)
                self._store(record)
                return self._finish(transaction, True, '已恢复中断的创建操作，源数据保留')
            except Exception as error:
                if transaction is None:
                    return _result(False, [], f'恢复未执行：{error}')
                record['recovery_required'] = True
                try:
                    self._store(record)
                except Exception:
                    pass
                transaction['results'].append({'project': '', 'target': '', 'success': False,
                                               'message': str(error)})
                return self._finish(transaction, False, f'恢复未全部完成：{error}；已处理项见结果，可重新检查后重试')
