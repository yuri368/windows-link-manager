"""Shared contracts for real and demonstration services.

The public mappings remain compatible with existing callers. Stable codes carry
behaviour; localized labels are presentation values only.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Literal, NotRequired, Protocol, TypedDict

from .config import LinkSpec


class PlanAction(StrEnum):
    BLOCK = 'block'
    SKIP = 'skip'
    CREATE = 'create'
    REPLACE_LINK = 'replace_link'
    BACKUP = 'backup'
    MOVE_DATA = 'move_data'
    DELETE_LINK = 'delete_link'
    DELETE_DATA = 'delete_data'


ACTION_LABELS = {
    PlanAction.BLOCK: '阻止操作',
    PlanAction.SKIP: '无需更改',
    PlanAction.CREATE: '创建链接',
    PlanAction.REPLACE_LINK: '替换已有链接',
    PlanAction.BACKUP: '备份普通数据后创建',
    PlanAction.MOVE_DATA: '移入源目录后创建交接点',
    PlanAction.DELETE_LINK: '删除已有链接后重建',
    PlanAction.DELETE_DATA: '永久删除目标及其内容后创建',
}


class HistoryState(StrEnum):
    PENDING = 'pending'
    COMPLETED = 'completed'
    FAILED = 'failed'
    RECOVERED = 'recovered'


class Inspection(TypedDict, total=False):
    status: str
    correct: bool
    source: str
    target: str
    resolved_source: str
    resolved_target: str
    source_exists: bool
    target_exists: bool
    target_kind: str
    actual_target: str
    detail: str


class Fingerprint(TypedDict, total=False):
    exists: bool
    dev: int
    ino: int
    mode: int
    size: int
    mtime: int
    ctime: int
    nlink: int
    attributes: int
    tag: int
    link: str


class StoredSpec(TypedDict):
    project: str
    source: str
    target: str
    kind: str


class OperationRecord(TypedDict, total=False):
    spec: StoredSpec
    source: str
    target: str
    phase: Literal['prepared', 'backup_pending', 'backed_up', 'remove_pending',
                   'old_removed', 'create_pending', 'created', 'delete_pending',
                   'target_deleted', 'irreversible_failure', 'remove_created_pending',
                   'created_removed', 'restore_pending', 'restored', 'rolled_back', 'removed',
                   'move_pending', 'moving', 'target_empty_pending', 'target_emptied',
                   'move_restore_pending', 'move_restored']
    backup: str
    original_fingerprint: Fingerprint
    created_fingerprint: Fingerprint
    restored_fingerprint: Fingerprint
    backup_fingerprint: str
    deletion_fingerprint: str
    old_link: dict[str, str]
    parents: list[str]
    parent_fingerprints: dict[str, Fingerprint]
    parent_pending: str
    delete_existing: bool
    move_existing: bool
    move_manifest: dict
    move_journal: dict
    irreversible: bool
    deletion_attempted: bool
    deletion: str
    deletion_current: str
    deleted_entries: int
    new_link_removed: bool
    before_virtual: dict[str, str]
    created_virtual: dict[str, str]


class PlanItem(TypedDict):
    spec: LinkSpec
    source: str
    target: str
    action: str
    action_code: PlanAction
    valid: bool
    error: str
    status: str
    backup: str
    backup_existing: bool
    delete_existing: bool
    move_existing: NotRequired[bool]
    move_manifest: NotRequired[dict]
    fingerprint: dict
    deletion_fingerprint: NotRequired[str]
    irreversible: NotRequired[bool]


class OperationResult(TypedDict):
    success: bool
    results: list[dict]
    message: str
    record_id: NotRequired[str]
    irreversible: NotRequired[bool]


class HistoryRecord(TypedDict, total=False):
    schema_version: int
    id: str
    time: str
    action: str
    state: str
    success: bool
    message: str
    results: list[dict]
    operations: list[OperationRecord]
    irreversible: bool
    undone: bool
    recovered: bool
    recovery_required: bool
    undo_of: str
    recovery_of: str


def can_undo(record: HistoryRecord) -> bool:
    return (record.get('action') == 'create' and bool(record.get('success'))
            and record.get('state', HistoryState.COMPLETED) == HistoryState.COMPLETED
            and not record.get('irreversible') and not record.get('undone')
            and not record.get('recovered'))


def record_needs_attention(record: HistoryRecord) -> bool:
    return (not record.get('recovered') and not record.get('undone')
            and (record.get('state') == HistoryState.PENDING
                 or bool(record.get('recovery_required'))))


def can_recover(record: HistoryRecord) -> bool:
    return (record.get('action') == 'create' and record_needs_attention(record)
            and not record.get('irreversible')
            and not any(op.get('irreversible') for op in record.get('operations', [])))


class LinkManagerService(Protocol):
    def inspect(self, spec: LinkSpec) -> Inspection: ...
    def preview(self, specs: list[LinkSpec], backup_existing: bool = False,
                delete_existing: bool = False, move_existing: bool = False) -> list[PlanItem]: ...
    def execute(self, plan: list[PlanItem]) -> OperationResult: ...
    def unlink(self, specs: list[LinkSpec]) -> OperationResult: ...
    def history(self) -> list[HistoryRecord]: ...
    def undo(self, record_id: str) -> OperationResult: ...
    def preview_recovery(self, record_id: str) -> OperationResult: ...
    def recover(self, record_id: str) -> OperationResult: ...
