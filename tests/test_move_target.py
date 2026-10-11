from __future__ import annotations

import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from linkmanager import core, migration
from linkmanager.config import LinkSpec
from linkmanager.core import LinkService
from linkmanager.models import PlanAction


class ProcessInterrupted(BaseException):
    pass


class MoveTargetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='linkmanager-move-')
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.target = self.root / 'target'
        self.source.mkdir()
        self.target.mkdir()
        (self.source / 'keep.txt').write_text('source original', encoding='utf-8')
        (self.target / 'incoming.txt').write_text('target original', encoding='utf-8')
        self.spec = LinkSpec('move-test', str(self.source), str(self.target), 'junction')
        self.service = LinkService(self.root / 'state')

    def tearDown(self):
        self.temporary.cleanup()

    def plan(self):
        plan = self.service.preview([self.spec], move_existing=True)
        self.assertTrue(plan[0]['valid'], plan)
        self.assertEqual(plan[0]['action_code'], PlanAction.MOVE_DATA)
        return plan

    def create(self):
        result = self.service.execute(self.plan())
        self.assertTrue(result['success'], result)
        return next(r for r in self.service.history() if r['action'] == 'create')['id']

    def assert_original(self):
        self.assertFalse(core._reparse(self.target))
        self.assertEqual((self.target / 'incoming.txt').read_text(encoding='utf-8'), 'target original')
        self.assertEqual((self.source / 'keep.txt').read_text(encoding='utf-8'), 'source original')
        self.assertFalse((self.source / 'incoming.txt').exists())

    def test_recursive_merge_and_undo_preserves_original_and_added_source_data(self):
        (self.source / 'nested').mkdir()
        (self.source / 'nested' / 'source.txt').write_text('keep')
        (self.target / 'nested' / 'deeper').mkdir(parents=True)
        (self.target / 'nested' / 'deeper' / 'target.txt').write_text('return')
        (self.target / 'empty').mkdir()
        record_id = self.create()
        self.assertEqual((self.target / 'nested' / 'source.txt').read_text(), 'keep')
        (self.source / 'nested' / 'deeper' / 'added.txt').write_text('new source data')
        result = self.service.undo(record_id)
        self.assertTrue(result['success'], result)
        self.assert_original()
        self.assertEqual((self.target / 'nested' / 'deeper' / 'target.txt').read_text(), 'return')
        self.assertTrue((self.target / 'empty').is_dir())
        self.assertEqual((self.source / 'nested' / 'source.txt').read_text(), 'keep')
        self.assertEqual((self.source / 'nested' / 'deeper' / 'added.txt').read_text(), 'new source data')
        self.assertFalse((self.source / 'empty').exists())

    def test_same_name_file_even_identical_content_blocks_entire_item(self):
        (self.source / 'incoming.txt').write_text('target original')
        plan = self.service.preview([self.spec], move_existing=True)
        self.assertFalse(plan[0]['valid'])
        self.assertFalse(self.service.execute(plan)['success'])
        self.assertEqual((self.target / 'incoming.txt').read_text(), 'target original')
        self.assertFalse(self.service.history())

    def test_nested_file_directory_conflict_blocks_without_moving_siblings(self):
        (self.target / 'sub').mkdir()
        (self.target / 'sub' / 'conflict').mkdir()
        (self.source / 'sub').mkdir()
        (self.source / 'sub' / 'conflict').write_text('file')
        plan = self.service.preview([self.spec], move_existing=True)
        self.assertFalse(plan[0]['valid'])
        self.assert_original()

    def test_only_junction_and_ordinary_directory_can_migrate(self):
        for kind in ('dir_symlink', 'file_symlink', 'hardlink'):
            spec = LinkSpec('invalid-kind', str(self.source), str(self.target), kind)
            self.assertFalse(self.service.preview([spec], move_existing=True)[0]['valid'])
        self.target.joinpath('incoming.txt').unlink()
        self.target.rmdir()
        self.target.write_text('ordinary file')
        self.assertFalse(self.service.preview([self.spec], move_existing=True)[0]['valid'])

    def test_three_policies_are_mutually_exclusive(self):
        for kwargs in ({'backup_existing': True}, {'delete_existing': True},
                       {'backup_existing': True, 'delete_existing': True}):
            self.assertFalse(self.service.preview([self.spec], move_existing=True, **kwargs)[0]['valid'])
        self.assert_original()

    def test_target_edit_after_preview_blocks_before_journal(self):
        plan = self.plan()
        (self.target / 'incoming.txt').write_text('target changed')
        result = self.service.execute(plan)
        self.assertFalse(result['success'], result)
        self.assertEqual((self.target / 'incoming.txt').read_text(), 'target changed')
        self.assertFalse((self.source / 'incoming.txt').exists())
        self.assertFalse(self.service.history())

    def test_nested_source_collision_after_preview_blocks_before_moving(self):
        (self.source / 'common').mkdir()
        (self.target / 'common').mkdir()
        (self.target / 'common' / 'nested.txt').write_text('target')
        plan = self.plan()
        (self.source / 'common' / 'nested.txt').write_text('source')
        self.assertFalse(self.service.execute(plan)['success'])
        self.assert_original()
        self.assertEqual((self.source / 'common' / 'nested.txt').read_text(), 'source')

    def test_create_failure_rolls_back_all_migrated_content(self):
        with patch.object(core, '_create', side_effect=OSError('injected creation failure')):
            result = self.service.execute(self.plan())
        self.assertFalse(result['success'])
        self.assert_original()
        self.assertFalse(self.service.history()[0]['recovery_required'])

    def test_second_file_failure_returns_previously_moved_file(self):
        (self.target / 'second.txt').write_text('second')
        original_transfer = migration._transfer
        def fail_second(origin, destination, *args, **kwargs):
            if origin.parent == self.target and origin.name == 'second.txt':
                raise OSError('injected second move failure')
            return original_transfer(origin, destination, *args, **kwargs)
        with patch.object(migration, '_transfer', side_effect=fail_second):
            result = self.service.execute(self.plan())
        self.assertFalse(result['success'])
        self.assert_original()
        self.assertEqual((self.target / 'second.txt').read_text(), 'second')

    def test_migrated_file_modified_after_creation_blocks_undo_without_removing_link(self):
        record_id = self.create()
        (self.source / 'incoming.txt').write_text('user changed it')
        result = self.service.undo(record_id)
        self.assertFalse(result['success'])
        self.assertTrue(core._reparse(self.target))
        self.assertEqual((self.source / 'incoming.txt').read_text(), 'user changed it')

    def test_content_change_with_original_size_and_mtime_still_blocks_undo(self):
        record_id = self.create()
        incoming = self.source / 'incoming.txt'
        info = incoming.stat()
        incoming.write_text('TARGET ORIGINAL')
        os.utime(incoming, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertFalse(self.service.undo(record_id)['success'])
        self.assertTrue(core._reparse(self.target))

    def test_crash_after_rename_before_checkpoint_recovers_original(self):
        rename = migration.os.rename
        def interrupted(origin, destination):
            rename(origin, destination)
            if Path(origin).parent == self.target:
                raise ProcessInterrupted()
        with patch.object(migration.os, 'rename', side_effect=interrupted):
            with self.assertRaises(ProcessInterrupted):
                self.service.execute(self.plan())
        record = self.service.history()[0]
        self.assertTrue(self.service.preview_recovery(record['id'])['success'])
        self.assertTrue(self.service.recover(record['id'])['success'])
        self.assert_original()

    def test_crash_after_empty_target_removed_recovers_original(self):
        rmdir = Path.rmdir
        def interrupted(path):
            rmdir(path)
            if path == self.target:
                raise ProcessInterrupted()
        with patch.object(Path, 'rmdir', interrupted):
            with self.assertRaises(ProcessInterrupted):
                self.service.execute(self.plan())
        record = self.service.history()[0]
        self.assertTrue(self.service.recover(record['id'])['success'])
        self.assert_original()

    def test_crash_after_created_checkpoint_can_recover(self):
        store = self.service._store
        def interrupted(record):
            store(record)
            if record['action'] == 'create' and record['operations'] and record['operations'][-1]['phase'] == 'created':
                raise ProcessInterrupted()
        with patch.object(self.service, '_store', side_effect=interrupted):
            with self.assertRaises(ProcessInterrupted):
                self.service.execute(self.plan())
        record = self.service.history()[0]
        self.assertTrue(self.service.recover(record['id'])['success'])
        self.assert_original()

    def test_cross_volume_copy_fallback_and_undo(self):
        with patch.object(migration.os, 'rename', side_effect=OSError(errno.EXDEV, 'simulated cross-volume')):
            record_id = self.create()
            result = self.service.undo(record_id)
        self.assertTrue(result['success'], result)
        self.assert_original()

    def test_cross_volume_partial_copy_failure_preserves_original_and_rolls_back(self):
        def partial(incoming, output, length):
            output.write(incoming.read(3))
            raise OSError('simulated full disk')
        with patch.object(migration.os, 'rename', side_effect=OSError(errno.EXDEV, 'cross-volume')):
            with patch.object(migration.shutil, 'copyfileobj', side_effect=partial):
                result = self.service.execute(self.plan())
        self.assertFalse(result['success'])
        self.assert_original()
        self.assertFalse(self.service.history()[0]['recovery_required'])

    def test_cross_volume_crash_before_original_unlink_recovers(self):
        store = self.service._store
        def interrupted(record):
            store(record)
            if record['action'] == 'create' and record['operations']:
                files = record['operations'][-1].get('move_journal', {}).get('files', {})
                if any(info.get('state') == 'copied' for info in files.values()):
                    raise ProcessInterrupted()
        with patch.object(migration.os, 'rename', side_effect=OSError(errno.EXDEV, 'cross-volume')):
            with patch.object(self.service, '_store', side_effect=interrupted):
                with self.assertRaises(ProcessInterrupted):
                    self.service.execute(self.plan())
        record = self.service.history()[0]
        self.assertTrue(self.service.recover(record['id'])['success'])
        self.assert_original()

    def test_cross_volume_crash_during_partial_copy_preserves_both_names_for_review(self):
        def interrupted(incoming, output, length):
            output.write(incoming.read(3))
            output.flush()
            raise ProcessInterrupted()
        with patch.object(migration.os, 'rename', side_effect=OSError(errno.EXDEV, 'cross-volume')):
            with patch.object(migration.shutil, 'copyfileobj', side_effect=interrupted):
                with self.assertRaises(ProcessInterrupted):
                    self.service.execute(self.plan())
        record = self.service.history()[0]
        result = self.service.recover(record['id'])
        self.assertFalse(result['success'])
        self.assertEqual((self.target / 'incoming.txt').read_text(), 'target original')
        self.assertEqual((self.source / 'incoming.txt').read_text(), 'tar')
        self.assertFalse(core._reparse(self.target))

    def test_interrupted_undo_after_return_rename_can_resume_recovery(self):
        record_id = self.create()
        rename = migration.os.rename
        def interrupted(origin, destination):
            rename(origin, destination)
            if Path(origin) == self.source / 'incoming.txt':
                raise ProcessInterrupted()
        with patch.object(migration.os, 'rename', side_effect=interrupted):
            with self.assertRaises(ProcessInterrupted):
                self.service.undo(record_id)
        result = self.service.recover(record_id)
        self.assertTrue(result['success'], result)
        self.assert_original()

    def test_two_targets_into_same_source_merge_and_undo(self):
        second_target = self.root / 'second-target'
        (second_target / 'common').mkdir(parents=True)
        (second_target / 'common' / 'two.txt').write_text('two')
        (self.target / 'common').mkdir()
        (self.target / 'common' / 'one.txt').write_text('one')
        other = LinkSpec('second', str(self.source), str(second_target), 'junction')
        plan = self.service.preview([self.spec, other], move_existing=True)
        self.assertTrue(all(item['valid'] for item in plan), plan)
        result = self.service.execute(plan)
        self.assertTrue(result['success'], result)
        record = next(r for r in self.service.history() if r['action'] == 'create')
        self.assertTrue(self.service.undo(record['id'])['success'])
        self.assert_original()
        self.assertEqual((second_target / 'common' / 'two.txt').read_text(), 'two')
        self.assertEqual((self.target / 'common' / 'one.txt').read_text(), 'one')

    def test_two_targets_into_same_source_collision_blocks_entire_batch(self):
        second_target = self.root / 'second-target'
        second_target.mkdir()
        (second_target / 'incoming.txt').write_text('other')
        other = LinkSpec('second', str(self.source), str(second_target), 'junction')
        plan = self.service.preview([self.spec, other], move_existing=True)
        self.assertTrue(all(not item['valid'] for item in plan))
        self.assertFalse(self.service.execute(plan)['success'])
        self.assert_original()
        self.assertFalse(self.service.history())

    @unittest.skipUnless(os.name == 'nt', 'Windows Junction test')
    def test_nested_target_junction_is_rejected_without_following_external_data(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'keep.txt').write_text('external')
        pointer = self.target / 'pointer'
        core._junction(outside, pointer)
        try:
            plan = self.service.preview([self.spec], move_existing=True)
            self.assertFalse(plan[0]['valid'])
            self.assert_original()
            self.assertEqual((outside / 'keep.txt').read_text(), 'external')
        finally:
            pointer.rmdir()

    def test_empty_target_restored_as_empty_directory(self):
        (self.target / 'incoming.txt').unlink()
        record_id = self.create()
        self.assertTrue(self.service.undo(record_id)['success'])
        self.assertTrue(self.target.is_dir())
        self.assertFalse(core._reparse(self.target))
        self.assertEqual(list(self.target.iterdir()), [])

