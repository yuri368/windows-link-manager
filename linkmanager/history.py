"""Validated history persistence and a reentrant, process-wide file lease."""
from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
import json
import os
import tempfile
import threading
import time

from .paths import normalize_path, path_key, reject_reparse, reparse
from .models import HistoryRecord, HistoryState

SCHEMA_VERSION = 1
_REGISTRY_LOCK = threading.Lock()
_LEASES: dict[str, '_LeaseState'] = {}


class _LeaseState:
    def __init__(self):
        self.lock = threading.RLock()
        self.depth = 0
        self.handle = None


class HistoryLease(AbstractContextManager):
    """Serialize the complete filesystem transaction, including its journal."""

    def __init__(self, state_dir: Path, timeout: float = 10.0):
        self.state_dir = normalize_path(state_dir)
        self.path = self.state_dir / 'service.lock'
        self.timeout = timeout
        with _REGISTRY_LOCK:
            self._state = _LEASES.setdefault(path_key(self.path), _LeaseState())

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        if not self._state.lock.acquire(timeout=self.timeout):
            raise TimeoutError('其他线程正在操作链接或历史，请稍后重试')
        try:
            if self._state.depth:
                self._state.depth += 1
                return self
            reject_reparse(self.state_dir)
            if reparse(self.path):
                raise ValueError('事务锁文件不能是重解析点')
            self._state.handle = self.path.open('a+b')
            if self._state.handle.seek(0, os.SEEK_END) == 0:
                self._state.handle.write(b'0')
                self._state.handle.flush()
            while True:
                try:
                    self._state.handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(self._state.handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self._state.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('其他进程正在操作链接或历史，请稍后重试')
                    time.sleep(0.05)
            reject_reparse(self.state_dir)
            self._state.depth = 1
            return self
        except BaseException:
            if self._state.handle is not None:
                self._state.handle.close()
                self._state.handle = None
            self._state.lock.release()
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self._state.depth -= 1
            if not self._state.depth:
                try:
                    self._state.handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(self._state.handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self._state.handle.fileno(), fcntl.LOCK_UN)
                finally:
                    self._state.handle.close()
                    self._state.handle = None
        finally:
            self._state.lock.release()
        return False


def _validated_records(data) -> list[HistoryRecord]:
    if not isinstance(data, list):
        raise ValueError('历史文件格式错误，已停止操作以保护审计记录')
    records = []
    identities = set()
    for raw in data:
        if not isinstance(raw, dict) or not isinstance(raw.get('id'), str) or not raw['id']:
            raise ValueError('历史记录缺少有效 ID，已停止操作')
        if raw['id'] in identities:
            raise ValueError('历史记录 ID 重复，已停止操作')
        identities.add(raw['id'])
        version = raw.get('schema_version', 0)
        if type(version) is not int or version not in (0, SCHEMA_VERSION):
            raise ValueError('历史记录版本不受支持，已停止操作')
        if raw.get('action') not in ('create', 'unlink', 'undo', 'recover'):
            raise ValueError('历史操作类型无效，已停止操作')
        state = raw.get('state', HistoryState.COMPLETED if raw.get('success') else HistoryState.FAILED)
        if not isinstance(state, str) or state not in set(HistoryState):
            raise ValueError('历史状态无效，已停止操作')
        if (not isinstance(raw.get('operations', []), list)
                or not all(isinstance(item, dict) for item in raw.get('operations', []))
                or not isinstance(raw.get('results', []), list)
                or not all(isinstance(item, dict) for item in raw.get('results', []))):
            raise ValueError('历史操作或结果格式错误，已停止操作')
        record = dict(raw)
        record['state'] = state
        # Legacy text is interpreted once at the persistence boundary.
        if 'recovery_required' not in record:
            message = str(record.get('message', ''))
            operations = record.get('operations', [])
            record['recovery_required'] = (record.get('state') == 'pending'
                or (record.get('state') == 'failed' and
                    (any(item.get('phase') not in ('rolled_back', 'restored') for item in operations)
                     or '回滚未完成' in message or '恢复未完成' in message)))
        for flag in ('success', 'undone', 'irreversible', 'recovered', 'recovery_required'):
            if flag in record and type(record[flag]) is not bool:
                raise ValueError(f'历史字段 {flag} 格式错误，已停止操作')
        for operation in record.get('operations', []):
            for flag in ('delete_existing', 'irreversible', 'deletion_attempted', 'new_link_removed'):
                if flag in operation and type(operation[flag]) is not bool:
                    raise ValueError(f'历史操作字段 {flag} 格式错误，已停止操作')
        records.append(record)
    return records


class HistoryRepository:
    def __init__(self, state_dir: Path):
        self.state_dir = normalize_path(state_dir)
        self.path = self.state_dir / 'history.json'
        self.lease = HistoryLease(self.state_dir)

    def load(self) -> list[HistoryRecord]:
        with self.lease:
            reject_reparse(self.state_dir)
            if reparse(self.path):
                raise ValueError('历史文件不能是重解析点')
            if not self.path.exists():
                return []
            try:
                return _validated_records(json.loads(self.path.read_text(encoding='utf-8')))
            except (json.JSONDecodeError, UnicodeError) as error:
                raise ValueError('历史文件损坏，已停止操作以保护审计记录') from error

    def store(self, record: HistoryRecord) -> None:
        with self.lease:
            records = self.load()
            records = [record if item['id'] == record['id'] else item for item in records]
            if not any(item['id'] == record['id'] for item in records):
                records.append(record)
            _validated_records(records)
            fd, temporary = tempfile.mkstemp(prefix='history-', suffix='.tmp', dir=self.state_dir)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                    json.dump(records, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                reject_reparse(self.state_dir)
                if reparse(self.path):
                    raise ValueError('历史文件不能是重解析点')
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
