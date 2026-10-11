from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from linkmanager import core
from linkmanager.config import LinkSpec, parse_config
from linkmanager.core import LinkService
from linkmanager.demo import DEMO_TEXT, DemoService, VirtualEntry
from linkmanager.paths import normalize_path, path_key
from linkmanager.recovery_view import recovery_message


class ProcessInterrupted(BaseException):
    pass


class RecoveryDescriptionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='linkmanager-recovery-view-')
        self.root = Path(self.temporary.name)
        self.service = LinkService(self.root / 'state')
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'source.txt').write_text('source remains intact', encoding='utf-8')
        self.target = self.root / 'target'
        self.spec = LinkSpec('恢复描述', str(self.source), str(self.target))

    def tearDown(self):
        self.temporary.cleanup()

    def interrupt_phase(self, phase):
        store = self.service._store

        def interrupted(record):
            store(record)
            if record['action'] == 'create' and any(op['phase'] == phase for op in record['operations']):
                raise ProcessInterrupted(phase)

        return patch.object(self.service, '_store', side_effect=interrupted)

    def interrupt_create(self, phase, *, backup=False):
        with self.interrupt_phase(phase), self.assertRaises(ProcessInterrupted):
            self.service.execute(self.service.preview([self.spec], backup_existing=backup))
        return self.service.history()[0]

    def test_migration_preview_describes_return_even_when_target_root_is_unchanged(self):
        self.target.mkdir()
        (self.target / 'empty').mkdir()
        with self.interrupt_phase('moving'):
            with self.assertRaises(ProcessInterrupted):
                self.service.execute(self.service.preview([self.spec], move_existing=True))
        record = self.service.history()[0]
        preview = self.service.preview_recovery(record['id'])
        self.assertTrue(preview['success'], preview['message'])
        message = preview['results'][0]['message']
        self.assertIn('移回原目标目录', message)
        self.assertNotIn('原入口尚未变动', message)
        self.assertIn(str(self.source), message)
        self.assertIn(str(self.target), message)
        self.assertTrue((self.source / 'empty').is_dir())
        self.assertTrue((self.target / 'empty').is_dir())

    def preview_without_changes(self, record):
        history = deepcopy(self.service.history())
        state_files = {str(path): path.read_bytes() for path in (self.root / 'state').rglob('*.json')}
        target_before = core._fingerprint(self.target)
        backup = record['operations'][0].get('backup')
        backup_before = core._tree_fingerprint(Path(backup)) if backup and Path(backup).exists() else None
        result = self.service.preview_recovery(record['id'])
        self.assertEqual(self.service.history(), history)
        self.assertEqual({str(path): path.read_bytes() for path in (self.root / 'state').rglob('*.json')}, state_files)
        self.assertEqual(core._fingerprint(self.target), target_before)
        if backup_before is not None:
            self.assertEqual(core._tree_fingerprint(Path(backup)), backup_before)
        self.assertEqual((self.source / 'source.txt').read_text(encoding='utf-8'), 'source remains intact')
        return result

    def test_real_backup_preview_names_complete_backup_and_does_not_restore_it(self):
        self.target.mkdir()
        (self.target / 'original.txt').write_text('original data', encoding='utf-8')
        record = self.interrupt_create('backed_up', backup=True)
        backup = record['operations'][0]['backup']
        preview = self.preview_without_changes(record)
        self.assertTrue(preview['success'], preview)
        message = preview['results'][0]['message']
        self.assertIn(backup, message)
        self.assertIn('将备份移回目标', message)
        self.assertNotIn('永久删除', message)
        self.assertNotIn('创建链接', message)
        self.assertFalse(self.target.exists())
        self.assertEqual((Path(backup) / 'original.txt').read_text(encoding='utf-8'), 'original data')

    def test_real_original_untouched_stage_keeps_existing_data(self):
        self.target.mkdir()
        (self.target / 'original.txt').write_text('original data', encoding='utf-8')
        record = self.interrupt_create('backup_pending', backup=True)
        preview = self.preview_without_changes(record)
        self.assertTrue(preview['success'], preview)
        self.assertIn('原入口尚未变动', preview['results'][0]['message'])
        self.assertIn(record['operations'][0]['backup'], preview['results'][0]['message'])
        self.assertNotIn('将备份移回', preview['results'][0]['message'])
        self.assertEqual((self.target / 'original.txt').read_text(encoding='utf-8'), 'original data')

    def test_real_old_link_preview_names_type_and_complete_original_destination(self):
        old_source = self.root / 'old-source'
        old_source.mkdir()
        core._create(old_source, self.target, 'junction')
        record = self.interrupt_create('old_removed')
        preview = self.preview_without_changes(record)
        self.assertTrue(preview['success'], preview)
        message = preview['results'][0]['message']
        self.assertIn('恢复旧链接入口', message)
        self.assertIn('Junction', message)
        self.assertIn(str(old_source), message)
        self.assertFalse(self.target.exists())

    def test_real_changed_backup_is_not_described_as_recoverable(self):
        self.target.mkdir()
        (self.target / 'original.txt').write_text('original data', encoding='utf-8')
        record = self.interrupt_create('backed_up', backup=True)
        (Path(record['operations'][0]['backup']) / 'original.txt').write_text('external edit', encoding='utf-8')
        preview = self.preview_without_changes(record)
        self.assertFalse(preview['success'])
        self.assertEqual(preview['results'], [])
        self.assertIn('备份已改变或丢失', preview['message'])

    def test_real_restored_checkpoint_keeps_recovered_data_in_place(self):
        self.target.mkdir()
        (self.target / 'original.txt').write_text('original data', encoding='utf-8')
        record = self.interrupt_create('backed_up', backup=True)
        with self.interrupt_phase('restored'), self.assertRaises(ProcessInterrupted):
            self.service.recover(record['id'])
        restored_record = next(item for item in self.service.history() if item['id'] == record['id'])
        preview = self.preview_without_changes(restored_record)
        self.assertTrue(preview['success'], preview)
        message = preview['results'][0]['message']
        self.assertIn('已恢复到操作前状态；保持现状', message)
        self.assertIn('恢复所用备份路径', message)
        self.assertNotIn('将备份移回', message)
        self.assertNotIn('移除本次', message)
        self.assertEqual((self.target / 'original.txt').read_text(encoding='utf-8'), 'original data')

    def test_real_missing_original_identity_is_not_described_as_recoverable(self):
        record = self.interrupt_create('prepared')
        record['operations'][0]['original_fingerprint'] = {}
        self.service._store(record)
        preview = self.preview_without_changes(record)
        self.assertFalse(preview['success'])
        self.assertEqual(preview['results'], [])

    def test_parent_cleanup_and_already_restored_description_are_conditional(self):
        operation = {'original_fingerprint': {'exists': False}, 'phase': 'created',
                     'parents': [str(self.root / 'new-parent')]}
        message = recovery_message(operation)
        self.assertIn('仅移除本次创建的链接入口', message)
        self.assertIn('操作前目标不存在', message)
        self.assertIn('身份一致且仍为空', message)
        self.assertIn('非空目录保留', message)
        operation['phase'] = 'rolled_back'
        restored = recovery_message(operation, already_restored=True, target_exists=False)
        self.assertIn('已恢复到操作前状态；保持现状', restored)
        self.assertNotIn('仅移除', restored)
        self.assertNotIn('父目录', restored)


class DemoRecoveryDescriptionTests(unittest.TestCase):
    def setUp(self):
        self.service = DemoService()
        self.specs = parse_config(DEMO_TEXT)

    def create_pending_record(self, spec, *, backup=False):
        plan = self.service.preview([spec], backup_existing=backup)
        self.assertTrue(self.service.execute(plan)['success'])
        record = self.service.records[-1]
        record.update(state='pending', success=False, recovery_required=True)
        return record, plan

    def preview_without_changes(self, record):
        states, records = deepcopy(self.service.states), deepcopy(self.service.records)
        preview = self.service.preview_recovery(record['id'])
        self.assertEqual(self.service.states, states)
        self.assertEqual(self.service.records, records)
        return preview

    def test_demo_backup_names_saved_preview_path_and_preview_has_no_side_effects(self):
        spec = next(spec for spec in self.specs if 'Pictures' in spec.target)
        record, plan = self.create_pending_record(spec, backup=True)
        self.assertEqual(record['operations'][0]['backup'], plan[0]['backup'])
        preview = self.preview_without_changes(record)
        self.assertTrue(preview['success'], preview)
        self.assertIn(plan[0]['backup'], preview['results'][0]['message'])

    def test_demo_old_link_shows_type_and_original_destination(self):
        spec = self.specs[1]
        original = r'H:\Demo\Original folder\nested'
        self.service.states[path_key(normalize_path(spec.target))] = VirtualEntry('junction', original)
        record, _ = self.create_pending_record(spec)
        preview = self.preview_without_changes(record)
        self.assertTrue(preview['success'], preview)
        self.assertIn(original, preview['results'][0]['message'])
        self.assertIn('Junction', preview['results'][0]['message'])

    def test_demo_missing_original_target_explains_remove_and_empty_result(self):
        record, _ = self.create_pending_record(self.specs[1])
        preview = self.preview_without_changes(record)
        self.assertTrue(preview['success'], preview)
        self.assertIn('仅移除本次创建的链接入口', preview['results'][0]['message'])
        self.assertIn('还原为目标不存在', preview['results'][0]['message'])

    def test_old_demo_record_does_not_invent_backup_path(self):
        spec = next(spec for spec in self.specs if 'Pictures' in spec.target)
        record, _ = self.create_pending_record(spec, backup=True)
        record['operations'][0].pop('backup')
        preview = self.preview_without_changes(record)
        self.assertTrue(preview['success'], preview)
        self.assertIn('恢复原普通目录的模拟状态', preview['results'][0]['message'])
        self.assertNotIn('.linkmanager-backup-demo', preview['results'][0]['message'])

    def test_demo_unknown_original_state_is_not_described_as_recoverable(self):
        record, _ = self.create_pending_record(self.specs[1])
        record['operations'][0]['before_virtual'] = {'kind': 'unknown', 'source': ''}
        preview = self.preview_without_changes(record)
        self.assertFalse(preview['success'])
        self.assertEqual(preview['results'], [])

    def test_demo_already_rolled_back_is_kept_in_place(self):
        record, _ = self.create_pending_record(self.specs[1])
        record['operations'][0]['phase'] = 'rolled_back'
        preview = self.preview_without_changes(record)
        self.assertTrue(preview['success'], preview)
        self.assertIn('已恢复到操作前状态；保持现状', preview['results'][0]['message'])


if __name__ == '__main__':
    unittest.main()
