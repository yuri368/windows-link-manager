"""界面演示的内存服务，不写入真实链接或历史。"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from copy import deepcopy
import uuid

from .config import KINDS, LinkSpec, parse_config
from .models import (ACTION_LABELS, HistoryRecord, HistoryState, Inspection,
                     OperationResult, PlanAction, PlanItem, can_recover, can_undo)
from .paths import normalize_path, path_key, within
from .planning import ordered_indices
from .recovery_view import recovery_message

DEMO_TEXT = r'''# 演示配置：所有执行仅发生在内存
【示例应用】
H:\PortableApps\ExampleApp\Data\Local\profile
C:\Users\Demo\AppData\Local\App

H:\PortableApps\ExampleApp\Data\Roaming\profile
C:\Users\Demo\AppData\Roaming\App

【Documents】
H:\Data\Documents
C:\Users\Demo\Documents

H:\Data\Pictures
C:\Users\Demo\Pictures

【开发工具】
# @type=dir_symlink
H:\Development\shared
C:\Workspace\shared

# @type=file_symlink
H:\Development\settings.json
C:\Workspace\settings.json

# @type=hardlink
H:\Development\common.ini
H:\Workspace\common.ini
'''


@dataclass(frozen=True)
class VirtualEntry:
    kind: str = 'missing'
    source: str = ''


class DemoService:
    """Memory backend with the same plan and undo semantics as LinkService."""

    def __init__(self):
        self.states: dict[str, VirtualEntry] = {}
        self.records: list[HistoryRecord] = []
        for spec in parse_config(DEMO_TEXT):
            key = path_key(normalize_path(spec.target))
            if 'Local\\App' in spec.target or 'Documents' in spec.target:
                self.states[key] = VirtualEntry(spec.kind, str(normalize_path(spec.source)))
            elif 'Pictures' in spec.target:
                self.states[key] = VirtualEntry('directory')

    def _entry(self, target: str) -> VirtualEntry:
        return self.states.get(path_key(normalize_path(target)), VirtualEntry())

    def inspect(self, spec: LinkSpec) -> Inspection:
        try:
            source, target = normalize_path(spec.source), normalize_path(spec.target)
            entry = self._entry(str(target))
            correct = (entry.kind == spec.kind and bool(entry.source)
                       and path_key(normalize_path(entry.source)) == path_key(source))
            status = ('链接正确' if correct else '目标不存在' if entry.kind == 'missing'
                      else '普通文件夹' if entry.kind == 'directory' else '链接指向不匹配')
            return {'status': status, 'correct': correct, 'source': str(source),
                    'target': str(target), 'resolved_source': str(source),
                    'resolved_target': str(target), 'target_kind': entry.kind,
                    'actual_target': entry.source, 'source_exists': True,
                    'target_exists': entry.kind != 'missing',
                    'detail': '演示状态，仅用于展示交互。'}
        except (OSError, ValueError) as error:
            return {'status': '检查失败', 'correct': False, 'source': spec.source,
                    'target': spec.target, 'target_kind': 'other',
                    'resolved_source': '', 'resolved_target': '',
                    'actual_target': '', 'source_exists': False,
                    'target_exists': False, 'detail': str(error)}

    def _validate(self, specs: list[LinkSpec]) -> None:
        pairs = [(normalize_path(s.source), normalize_path(s.target)) for s in specs]
        for index, (source, target) in enumerate(pairs):
            if specs[index].kind not in KINDS:
                raise ValueError('未知链接类型')
            if within(source, target) or within(target, source):
                raise ValueError('源与目标重叠，可能形成循环')
            for _, previous_target in pairs[:index]:
                if within(target, previous_target) or within(previous_target, target):
                    raise ValueError('目标重复或重叠')
        ordered_indices(pairs)
        planned = {path_key(target): (source, spec.kind)
                   for (source, target), spec in zip(pairs, specs)}
        for (source, target), spec in zip(pairs, specs):
            if spec.kind not in ('junction', 'dir_symlink'):
                continue
            pending, visited = [source], set()
            while pending:
                directory = pending.pop()
                key = path_key(directory)
                if key in visited:
                    continue
                visited.add(key)
                if within(target, directory) or within(directory, target):
                    raise ValueError('源目录内已有或计划链接可到达目标，可能形成循环')
                for (new_source, new_target), new_spec in zip(pairs, specs):
                    if within(new_target, directory) and new_spec.kind in ('junction', 'dir_symlink'):
                        pending.append(new_source)
                for raw, entry in self.states.items():
                    if (raw not in planned and entry.kind in ('junction', 'dir_symlink')
                            and within(normalize_path(raw), directory)):
                        pending.append(normalize_path(entry.source))

    def preview(self, specs: list[LinkSpec], backup_existing: bool = False,
                delete_existing: bool = False, move_existing: bool = False) -> list[PlanItem]:
        error = ''
        try:
            self._validate(specs)
            if sum(map(bool, (backup_existing, delete_existing, move_existing))) > 1:
                raise ValueError('备份、永久删除与移入源目录不能同时选择。')
        except (OSError, ValueError) as exception:
            error = str(exception)
        plan: list[PlanItem] = []
        for spec in specs:
            state = self.inspect(spec)
            ordinary = state['target_kind'] in ('directory', 'file')
            item_error = error or (state['detail'] if state['target_kind'] == 'other' else '')
            if move_existing and spec.kind != 'junction':
                item_error = item_error or '移入源目录后创建仅适用于交接点。'
            if move_existing and state['target_kind'] == 'file':
                item_error = item_error or '迁移目标必须为普通文件夹。'
            if ordinary and not (backup_existing or delete_existing or move_existing):
                item_error = item_error or '普通目标含有数据；请选择备份、迁移或永久删除后替换。'
            if item_error:
                code = PlanAction.BLOCK
            elif delete_existing and state['target_exists']:
                code = PlanAction.DELETE_DATA if ordinary else PlanAction.DELETE_LINK
            elif state['correct']:
                code = PlanAction.SKIP
            elif ordinary:
                code = PlanAction.MOVE_DATA if move_existing else PlanAction.BACKUP
            elif state['target_exists']:
                code = PlanAction.REPLACE_LINK
            else:
                code = PlanAction.CREATE
            before = asdict(self._entry(spec.target)) if not item_error else {}
            plan.append({'spec': spec, 'source': state['source'], 'target': state['target'],
                         'action_code': code, 'action': ACTION_LABELS[code],
                         'valid': not item_error, 'error': item_error,
                         'status': state['status'], 'fingerprint': before,
                         'backup': state['target'] + '.linkmanager-backup-demo'
                         if code == PlanAction.BACKUP else '',
                         'backup_existing': bool(backup_existing),
                         'delete_existing': bool(delete_existing),
                         'move_existing': bool(move_existing),
                         'irreversible': code == PlanAction.DELETE_DATA})
        return plan

    def record(self, action: str, results: list[dict], success: bool = True) -> HistoryRecord:
        record: HistoryRecord = {
            'schema_version': 1, 'id': uuid.uuid4().hex,
            'time': datetime.now(timezone.utc).isoformat(), 'action': action,
            'state': HistoryState.COMPLETED if success else HistoryState.FAILED,
            'success': success, 'message': '演示操作完成，真实磁盘未修改。',
            'results': results, 'operations': [], 'recovery_required': False,
        }
        self.records.append(record)
        return record

    @staticmethod
    def _failure(message: str) -> OperationResult:
        return {'success': False, 'message': message, 'results': []}

    def execute(self, plan: list[PlanItem]) -> OperationResult:
        if not plan:
            return {'success': True, 'results': [], 'message': '没有选中的配置项'}
        try:
            self._validate([item['spec'] for item in plan])
            for item in plan:
                if not item['valid']:
                    raise ValueError('校验失败，未执行模拟操作。')
                fresh = self.preview([item['spec']], item['backup_existing'],
                                     item['delete_existing'], item.get('move_existing', False))[0]
                if (not fresh['valid'] or fresh['action_code'] != item['action_code']
                        or fresh['fingerprint'] != item['fingerprint']
                        or fresh['source'] != item['source'] or fresh['target'] != item['target']):
                    raise ValueError('预览后状态已改变，请重新预览。')
        except (KeyError, OSError, ValueError) as error:
            return self._failure(str(error))
        results, operations = [], []
        pairs = [(normalize_path(item['source']), normalize_path(item['target'])) for item in plan]
        for index in ordered_indices(pairs):
            item = plan[index]
            spec = item['spec']
            if item['action_code'] != PlanAction.SKIP:
                before = self._entry(item['target'])
                after = VirtualEntry(spec.kind, item['source'])
                operations.append({'spec': asdict(spec), 'source': item['source'],
                                   'target': item['target'], 'phase': 'created',
                                   'before_virtual': asdict(before),
                                   'created_virtual': asdict(after),
                                   'backup': item.get('backup', ''),
                                   'move_existing': item.get('action_code') == PlanAction.MOVE_DATA,
                                   'irreversible': item.get('irreversible', False)})
                self.states[path_key(normalize_path(item['target']))] = after
            results.append({'project': spec.project, 'target': item['target'],
                            'success': True, 'message': item['action']})
        record = self.record('create', results)
        record['operations'] = operations
        record['irreversible'] = any(op['irreversible'] for op in operations)
        if record['irreversible']:
            record['message'] = '已模拟永久删除目标后创建；此类记录无法恢复普通目标数据。真实磁盘未修改。'
        return record

    def unlink(self, specs: list[LinkSpec]) -> OperationResult:
        if not specs:
            return {'success': True, 'results': [], 'message': '没有选中的配置项'}
        if any(not self.inspect(spec)['correct'] for spec in specs):
            return self._failure('包含非链接记录，本批次未执行。')
        results = []
        for spec in specs:
            target = str(normalize_path(spec.target))
            self.states[path_key(normalize_path(target))] = VirtualEntry()
            results.append({'project': spec.project, 'target': target,
                            'success': True, 'message': '已模拟移除链接入口'})
        return self.record('unlink', results)

    def history(self) -> list[HistoryRecord]:
        return deepcopy(list(reversed(self.records)))

    def _find(self, record_id: str) -> HistoryRecord | None:
        return next((record for record in self.records if record['id'] == record_id), None)

    def _check_restore(self, record: HistoryRecord) -> None:
        for operation in record.get('operations', []):
            if operation.get('phase') == 'rolled_back':
                continue
            if asdict(self._entry(operation['target'])) != operation.get('created_virtual'):
                raise ValueError('目标已改变，拒绝恢复模拟记录。')

    def _restore(self, record: HistoryRecord) -> list[dict]:
        results = []
        for operation in reversed(record.get('operations', [])):
            if operation.get('phase') == 'rolled_back':
                continue
            key = path_key(normalize_path(operation['target']))
            self.states[key] = VirtualEntry(**operation['before_virtual'])
            operation['phase'] = 'rolled_back'
            results.append({'target': operation['target'], 'success': True,
                            'message': '已模拟恢复执行前的入口或普通数据。'})
        return results

    def undo(self, record_id: str) -> OperationResult:
        record = self._find(record_id)
        if not record or not can_undo(record):
            return self._failure('此记录不可自动撤销。')
        try:
            self._check_restore(record)
        except ValueError as error:
            return self._failure(str(error))
        results = self._restore(record)
        record['undone'] = True
        return self.record('undo', results)

    def preview_recovery(self, record_id: str) -> OperationResult:
        record = self._find(record_id)
        if not record or not can_recover(record):
            return self._failure('此记录不可自动恢复；永久删除记录只能人工核对。')
        try:
            self._check_restore(record)
            results = [{'target': op['target'], 'message': recovery_message(
                op, demo=True, already_restored=op.get('phase') == 'rolled_back',
                target_exists=self._entry(op['target']).kind != 'missing'),
                'success': True} for op in reversed(record.get('operations', []))]
        except ValueError as error:
            return self._failure(str(error))
        return {'success': True, 'message': '已核对未完成的模拟操作，可恢复执行前状态。',
                'results': results}

    def recover(self, record_id: str) -> OperationResult:
        preview = self.preview_recovery(record_id)
        if not preview['success']:
            return preview
        record = self._find(record_id)
        results = self._restore(record)
        record.update(state=HistoryState.RECOVERED, recovered=True, recovery_required=False,
                      message='已模拟恢复中断前状态，真实磁盘未修改。')
        return self.record('recover', results)
