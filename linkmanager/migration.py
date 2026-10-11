"""Journaled, non-overwriting directory merges for Junction creation.

Only ordinary files/directories participate. Every file keeps its own identity
and content checkpoint so undo never takes unrelated source data with it.
"""
from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
import shutil
import stat

from .paths import exists, reject_reparse


IDENTITY = ('exists', 'dev', 'ino', 'mode', 'attributes', 'tag')


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _same_identity(actual: dict, expected: dict) -> bool:
    return all(actual.get(field) == expected.get(field) for field in IDENTITY)


def _file_matches(path: Path, expected: dict, digest: str, fingerprint,
                  *, renamed: bool = False) -> bool:
    reject_reparse(path)
    actual = fingerprint(path)
    ignored = ('ctime',) if renamed else ()
    return (all(actual.get(key) == value for key, value in expected.items() if key not in ignored)
            and stat.S_ISREG(actual.get('mode', 0)) and _digest(path) == digest)


def manifest(source: Path, target: Path, fingerprint) -> dict:
    """Fully inspect the target and every destination before moving any entry."""
    reject_reparse(source)
    reject_reparse(target)
    if not source.is_dir() or not target.is_dir():
        raise ValueError('移入源目录仅支持普通目标文件夹和已存在的源文件夹')
    entries = {}
    source_dirs = {}
    pending = [target]
    while pending:
        path = pending.pop()
        reject_reparse(path)
        relative = path.relative_to(target).as_posix()
        info = fingerprint(path)
        directory = stat.S_ISDIR(info.get('mode', 0))
        if not directory and not stat.S_ISREG(info.get('mode', 0)):
            raise ValueError(f'迁移目标包含非普通文件或目录：{path}')
        entry = {'kind': 'directory' if directory else 'file', 'fingerprint': info}
        if not directory:
            entry['digest'] = _digest(path)
            if fingerprint(path) != info:
                raise ValueError(f'迁移预检期间文件发生变化：{path}')
        entries[relative] = entry
        destination = source / relative
        reject_reparse(destination)
        if exists(destination):
            if not directory or not destination.is_dir():
                raise ValueError(f'源目录存在同名文件或类型冲突，拒绝覆盖：{destination}')
            source_dirs[relative] = {key: fingerprint(destination).get(key) for key in IDENTITY}
        if directory:
            pending.extend(sorted(path.iterdir(), key=lambda child: child.name, reverse=True))
    return {'target_entries': entries, 'source_dirs': source_dirs}


def validate_batch(plan: list[dict]) -> None:
    """Reject collisions across separate targets destined for the same source."""
    destinations = {}
    for item in plan:
        for relative, entry in item.get('move_manifest', {}).get('target_entries', {}).items():
            path = os.path.normcase(os.path.normpath(str(Path(item['source']) / relative)))
            previous = destinations.get(path)
            if previous and (previous != 'directory' or entry['kind'] != 'directory'):
                raise ValueError(f'本批次多个目标迁入同名路径，拒绝覆盖：{path}')
            destinations[path] = entry['kind']
    # A file introduced by one target cannot become another target's ancestor.
    for raw, kind in destinations.items():
        if kind == 'file':
            for other in destinations:
                try:
                    child = other != raw and os.path.commonpath((raw, other)) == raw
                except ValueError:
                    child = False
                if child:
                    raise ValueError(f'本批次迁入路径存在文件/目录冲突：{raw}')


def _transfer(origin: Path, destination: Path, entry: dict, journal: dict,
              fingerprint, checkpoint, *, returning: bool = False) -> None:
    """Rename or exclusively copy; durably checkpoint before unlinking origin."""
    prefix = 'return_' if returning else ''
    state_key = prefix + 'state'
    fp_key = prefix + 'fingerprint'
    expected = entry['fingerprint']
    digest = entry['digest']
    reject_reparse(origin)
    reject_reparse(destination)
    if exists(destination) or not _file_matches(origin, expected, digest, fingerprint):
        raise ValueError(f'移动前文件或目的路径已变化：{origin} → {destination}')
    journal[state_key] = 'pending'
    checkpoint()
    try:
        # Windows rename refuses an existing destination, including races.
        os.rename(origin, destination)
    except OSError as error:
        if error.errno != errno.EXDEV and getattr(error, 'winerror', None) != 17:
            raise
        if exists(destination):
            raise ValueError(f'跨盘移动目的路径已经出现：{destination}')
        # No shutil.move fallback: its copy function can silently overwrite.
        with destination.open('xb') as output:
            journal[state_key] = 'copying'
            journal[fp_key] = fingerprint(destination)
            checkpoint()
            try:
                with origin.open('rb') as incoming:
                    shutil.copyfileobj(incoming, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            except Exception:
                output.flush()
                journal[state_key] = 'partial'
                journal[fp_key] = fingerprint(destination)
                journal[prefix + 'partial_digest'] = _digest(destination)
                checkpoint()
                raise
        shutil.copystat(origin, destination)
        journal[fp_key] = fingerprint(destination)
        journal[state_key] = 'copied'
        checkpoint()
        if (not _file_matches(origin, expected, digest, fingerprint)
                or not _file_matches(destination, journal[fp_key], digest, fingerprint)):
            raise ValueError(f'跨盘复制期间文件发生变化：{origin}')
        origin.unlink()
    journal[fp_key] = fingerprint(destination)
    journal[state_key] = 'returned' if returning else 'moved'
    checkpoint()


def move(source: Path, target: Path, operation: dict, fingerprint, checkpoint,
         *, known_source_dirs: dict | None = None) -> None:
    expected = operation['move_manifest']
    current = manifest(source, target, fingerprint)
    extra_dirs = set(current['source_dirs']) - set(expected['source_dirs'])
    allowed = all(_same_identity(current['source_dirs'][relative],
                                (known_source_dirs or {}).get(str(source / relative), {}))
                  for relative in extra_dirs)
    existing_dirs_unchanged = all(current['source_dirs'].get(relative) == info
                                  for relative, info in expected['source_dirs'].items())
    if current['target_entries'] != expected['target_entries'] or not allowed or not existing_dirs_unchanged:
        raise ValueError('迁移执行前源目录或目标内容发生变化，请重新预览')
    if extra_dirs:
        expected = {**expected, 'source_dirs': current['source_dirs']}
        operation['move_manifest'] = expected
    journal = operation.setdefault('move_journal', {'files': {}, 'created_dirs': {}, 'removed_dirs': {}})
    entries = expected['target_entries']
    operation['phase'] = 'move_pending'
    checkpoint()
    for relative, entry in sorted(entries.items(), key=lambda pair: (len(Path(pair[0]).parts), pair[0])):
        if entry['kind'] != 'directory' or relative == '.':
            continue
        destination = source / relative
        reject_reparse(destination)
        if exists(destination):
            known = expected['source_dirs'].get(relative)
            if not known or not _same_identity(fingerprint(destination), known):
                raise ValueError(f'合并源目录身份发生变化：{destination}')
        else:
            journal['directory_pending'] = relative
            checkpoint()
            destination.mkdir()
            journal['created_dirs'][relative] = fingerprint(destination)
            journal.pop('directory_pending', None)
            checkpoint()
    operation['phase'] = 'moving'
    checkpoint()
    for relative, entry in entries.items():
        if entry['kind'] != 'file':
            continue
        file_journal = journal['files'].setdefault(relative, {})
        _transfer(target / relative, source / relative, entry, file_journal, fingerprint, checkpoint)
    operation['phase'] = 'target_empty_pending'
    checkpoint()
    for relative, entry in sorted(entries.items(), key=lambda pair: len(Path(pair[0]).parts), reverse=True):
        if entry['kind'] != 'directory':
            continue
        directory = target / relative
        reject_reparse(directory)
        if not _same_identity(fingerprint(directory), entry['fingerprint']):
            raise ValueError(f'清理空目标目录前身份发生变化：{directory}')
        journal['remove_directory_pending'] = relative
        checkpoint()
        directory.rmdir()  # Unexpected new data is never recursively removed.
        journal['removed_dirs'][relative] = entry['fingerprint']
        journal.pop('remove_directory_pending', None)
        checkpoint()
    operation['phase'] = 'target_emptied'
    checkpoint()


def _file_state(source: Path, target: Path, relative: str, entry: dict, journal: dict,
                fingerprint, *, linked: bool) -> str:
    destination, original = source / relative, target / relative
    # A freshly returned file may not have its final checkpoint yet.
    returning = journal.get('return_state')
    if returning:
        expected = journal.get('return_fingerprint')
        moved_expected = journal.get('fingerprint')
        if not linked and exists(original):
            if (returning == 'copy_remove_pending' and not exists(destination)
                    and _file_matches(original, entry['fingerprint'], entry['digest'], fingerprint)):
                return 'returned'
            if expected and _file_matches(original, expected, entry['digest'], fingerprint):
                if not exists(destination):
                    return 'returned'
                if (returning == 'copied' and moved_expected
                        and _file_matches(destination, moved_expected, entry['digest'], fingerprint)):
                    return 'return_copied'
            if (returning == 'pending' and moved_expected
                    and _file_matches(original, moved_expected, entry['digest'], fingerprint, renamed=True)
                    and not exists(destination)):
                return 'returned'
            raise ValueError(f'退回期间文件发生变化，拒绝继续：{original}')
        if returning not in ('pending',):
            raise ValueError(f'退回文件状态不完整，需手动检查：{original}')
    state = journal.get('state')
    if not state:
        if linked or not _file_matches(original, entry['fingerprint'], entry['digest'], fingerprint):
            raise ValueError(f'尚未迁移的原文件已变化：{original}')
        return 'untouched'
    if not linked and exists(original):
        if not _file_matches(original, entry['fingerprint'], entry['digest'], fingerprint):
            raise ValueError(f'原目标文件已变化：{original}')
        if not exists(destination):
            if state == 'pending':
                return 'untouched'
            raise ValueError(f'迁移副本已丢失：{destination}')
        expected = journal.get('fingerprint')
        copied_digest = journal.get('partial_digest') if state == 'partial' else entry['digest']
        if state in ('copied', 'partial') and expected and _file_matches(destination, expected, copied_digest, fingerprint):
            return 'duplicate'
        raise ValueError(f'迁移中断留下未经核验的副本，需手动检查：{destination}')
    expected = journal.get('fingerprint', entry['fingerprint'])
    if not exists(destination) or not _file_matches(destination, expected, entry['digest'], fingerprint,
                                                   renamed=state == 'pending'):
        raise ValueError(f'迁入源目录的文件已修改或丢失，拒绝倒移：{destination}')
    return 'moved'


def check_restore(source: Path, target: Path, operation: dict, fingerprint, *, linked: bool = False) -> dict:
    """Preflight every reverse action, without following the new Junction."""
    reject_reparse(source)
    if not linked:
        reject_reparse(target)
    manifest_data = operation.get('move_manifest')
    if not isinstance(manifest_data, dict) or not isinstance(manifest_data.get('target_entries'), dict):
        raise ValueError('迁移清单缺失，拒绝自动恢复')
    entries = manifest_data['target_entries']
    journal = operation.get('move_journal', {'files': {}, 'created_dirs': {}, 'removed_dirs': {}})
    relative_paths = set(entries)
    for table in ('files', 'created_dirs', 'removed_dirs', 'restored_dirs'):
        relative_paths.update(journal.get(table, {}))
    relative_paths.update(manifest_data.get('source_dirs', {}))
    relative_paths.update(journal.get('cleaned_dirs', []))
    for field in ('directory_pending', 'restore_directory_pending', 'remove_directory_pending', 'cleanup_directory_pending'):
        if journal.get(field):
            relative_paths.add(journal[field])
    for relative in relative_paths:
        relpath = Path(relative)
        if (relative not in entries or relpath.is_absolute() or '..' in relpath.parts
                or relpath.anchor or str(source / relative) == str(target)):
            raise ValueError('迁移清单路径超出原目标，拒绝恢复')
    pending = journal.get('directory_pending')
    if pending and exists(source / pending):
        raise ValueError(f'源目录已出现但创建身份尚未记录，需手动检查：{source / pending}')
    for relative, expected in {**manifest_data.get('source_dirs', {}), **journal.get('created_dirs', {})}.items():
        if (relative in journal.get('cleaned_dirs', []) or relative == journal.get('cleanup_directory_pending')) and not exists(source / relative):
            continue
        reject_reparse(source / relative)
        if not _same_identity(fingerprint(source / relative), expected):
            raise ValueError(f'迁移源目录身份已变化：{source / relative}')
    states = {}
    restoring_directory = journal.get('restore_directory_pending')
    if restoring_directory and exists(target / restoring_directory):
        raise ValueError(f'退回目标目录已出现但身份尚未记录，需手动检查：{target / restoring_directory}')
    for relative, entry in entries.items():
        if entry['kind'] == 'file':
            states[relative] = _file_state(source, target, relative, entry,
                                           journal.get('files', {}).get(relative, {}), fingerprint, linked=linked)
        elif not linked and exists(target / relative):
            restored = journal.get('restored_dirs', {}).get(relative)
            expected = restored or entry['fingerprint']
            reject_reparse(target / relative)
            if not _same_identity(fingerprint(target / relative), expected):
                raise ValueError(f'原目标目录身份已变化：{target / relative}')
    return states


def restore(source: Path, target: Path, operation: dict, fingerprint, checkpoint) -> None:
    states = check_restore(source, target, operation, fingerprint)
    journal = operation.setdefault('move_journal', {'files': {}, 'created_dirs': {}, 'removed_dirs': {}})
    entries = operation['move_manifest']['target_entries']
    operation['phase'] = 'move_restore_pending'
    checkpoint()
    for relative, entry in sorted(entries.items(), key=lambda pair: len(Path(pair[0]).parts)):
        if entry['kind'] == 'directory' and not exists(target / relative):
            journal['restore_directory_pending'] = relative
            checkpoint()
            (target / relative).mkdir()
            journal.setdefault('restored_dirs', {})[relative] = fingerprint(target / relative)
            journal.pop('restore_directory_pending', None)
            checkpoint()
    for relative, state in states.items():
        info = journal['files'].setdefault(relative, {})
        if state in ('untouched', 'returned'):
            continue
        if state in ('duplicate', 'return_copied'):
            path = source / relative
            # This copy's exact identity/content was checked above and again now.
            expected = info['fingerprint']
            digest = info.get('partial_digest', entries[relative]['digest'])
            if not _file_matches(path, expected, digest, fingerprint):
                raise ValueError(f'移除迁移副本前数据已改变：{path}')
            info['return_state'] = 'copy_remove_pending'
            checkpoint()
            path.unlink()
            info['return_state'] = 'returned'
            info['return_fingerprint'] = fingerprint(target / relative)
            checkpoint()
        else:
            expected = fingerprint(source / relative)
            _transfer(source / relative, target / relative,
                      {'fingerprint': expected, 'digest': entries[relative]['digest']},
                      info, fingerprint, checkpoint, returning=True)
    for relative, expected in sorted(journal.get('created_dirs', {}).items(),
                                      key=lambda pair: len(Path(pair[0]).parts), reverse=True):
        directory = source / relative
        if exists(directory):
            reject_reparse(directory)
            if not _same_identity(fingerprint(directory), expected):
                raise ValueError(f'本次创建的源目录已改变：{directory}')
            try:
                journal['cleanup_directory_pending'] = relative
                checkpoint()
                directory.rmdir()
            except OSError:
                pass  # Files added after migration stay in the source.
            else:
                journal.setdefault('cleaned_dirs', []).append(relative)
                journal.pop('cleanup_directory_pending', None)
                checkpoint()
    operation['phase'] = 'move_restored'
    operation['restored_fingerprint'] = fingerprint(target)
    checkpoint()
