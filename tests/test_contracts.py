"""Behaviour shared by the real service and its safe demonstration backend."""
from pathlib import Path
import tempfile
import unittest

from linkmanager.config import LinkSpec, parse_config
from linkmanager.core import LinkService
from linkmanager.demo import DEMO_TEXT, DemoService
from linkmanager.models import PlanAction, can_undo


class ServiceContractTests(unittest.TestCase):
    def test_inspection_exposes_normalized_quoted_paths(self):
        with tempfile.TemporaryDirectory(prefix='linkmanager-contract-path-') as folder:
            root = Path(folder)
            source = root / '中文 空格'
            source.mkdir()
            target = root / 'target'
            spec = LinkSpec('路径', '"' + str(source) + '"', '"' + str(target) + '"')
            for service in (LinkService(root / 'state'), DemoService()):
                with self.subTest(backend=type(service).__name__):
                    state = service.inspect(spec)
                    self.assertEqual(state['resolved_source'], str(source))
                    self.assertEqual(state['resolved_target'], str(target))
                    self.assertTrue(state['source_exists'])
                    self.assertFalse(state['target_exists'])

    def test_empty_operations_do_not_create_history(self):
        with tempfile.TemporaryDirectory(prefix='linkmanager-contract-empty-') as folder:
            services = (LinkService(Path(folder) / 'state'), DemoService())
            for service in services:
                with self.subTest(backend=type(service).__name__):
                    self.assertTrue(service.execute([])['success'])
                    self.assertTrue(service.unlink([])['success'])
                    self.assertEqual(service.history(), [])

    def test_creation_noop_and_undo_preserve_preexisting_link(self):
        for backend in ('real', 'demo'):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory(prefix='linkmanager-contract-') as folder:
                root = Path(folder)
                source = root / 'source'
                source.mkdir()
                (source / 'payload').write_text('keep', encoding='utf-8')
                spec = LinkSpec('契约', str(source), str(root / 'target'))
                service = LinkService(root / 'state') if backend == 'real' else DemoService()
                first = service.execute(service.preview([spec]))
                self.assertTrue(first['success'], first['message'])
                second_plan = service.preview([spec])
                self.assertEqual(second_plan[0]['action_code'], PlanAction.SKIP)
                second = service.execute(second_plan)
                self.assertTrue(second['success'], second['message'])
                noop_record = service.history()[0]
                self.assertTrue(can_undo(noop_record))
                self.assertTrue(service.undo(noop_record['id'])['success'])
                self.assertTrue(service.inspect(spec)['correct'])
                self.assertFalse(service.undo(noop_record['id'])['success'])
                first_record = next(record for record in service.history()
                                    if record['action'] == 'create' and record['operations'])
                self.assertTrue(service.undo(first_record['id'])['success'])
                self.assertFalse(service.inspect(spec)['target_exists'])
                self.assertEqual((source / 'payload').read_text(encoding='utf-8'), 'keep')

    def test_display_action_does_not_control_execution(self):
        for backend in ('real', 'demo'):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory(prefix='linkmanager-contract-label-') as folder:
                root = Path(folder)
                source = root / 'source'
                source.mkdir()
                spec = LinkSpec('契约', str(source), str(root / 'target'))
                service = LinkService(root / 'state') if backend == 'real' else DemoService()
                plan = service.preview([spec])
                plan[0]['action'] = 'Localized label can change without changing behaviour'
                result = service.execute(plan)
                self.assertTrue(result['success'], result['message'])
                self.assertTrue(service.inspect(spec)['correct'])
                record = service.history()[0]
                self.assertTrue(service.undo(record['id'])['success'])

    def test_indirect_cycle_is_blocked_before_any_mutation(self):
        with tempfile.TemporaryDirectory(prefix='linkmanager-contract-cycle-') as folder:
            root = Path(folder)
            a, b = root / 'A', root / 'B'
            a.mkdir()
            b.mkdir()
            specs = [LinkSpec('循环', str(a), str(b / 'linkA')),
                     LinkSpec('循环', str(b), str(a / 'linkB'))]
            for service in (LinkService(root / 'state'), DemoService()):
                with self.subTest(backend=type(service).__name__):
                    plan = service.preview(specs)
                    self.assertFalse(any(item['valid'] for item in plan))
                    self.assertFalse(service.execute(plan)['success'])
                    self.assertEqual(service.history(), [])
                    self.assertFalse((a / 'linkB').exists())
                    self.assertFalse((b / 'linkA').exists())


class DemoSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.service = DemoService()
        self.specs = parse_config(DEMO_TEXT)

    def test_mixed_batch_undo_restores_previous_states(self):
        before = [self.service.inspect(spec) for spec in self.specs]
        result = self.service.execute(self.service.preview(self.specs, backup_existing=True))
        self.assertTrue(result['success'])
        record = self.service.history()[0]
        self.assertTrue(self.service.undo(record['id'])['success'])
        after = [self.service.inspect(spec) for spec in self.specs]
        for initial, restored in zip(before, after):
            self.assertEqual(initial['target_kind'], restored['target_kind'])
            self.assertEqual(initial['actual_target'], restored['actual_target'])
            self.assertEqual(initial['correct'], restored['correct'])

    def test_invalid_batch_leaves_states_and_history_unchanged(self):
        before = self.service.states.copy()
        result = self.service.execute(self.service.preview(self.specs))
        self.assertFalse(result['success'])
        self.assertEqual(self.service.states, before)
        self.assertEqual(self.service.history(), [])

    def test_target_changed_after_preview_prevents_whole_batch(self):
        missing = self.specs[1]
        plan = self.service.preview([missing])
        self.assertTrue(self.service.execute(self.service.preview([missing]))['success'])
        self.assertFalse(self.service.execute(plan)['success'])
        self.assertTrue(self.service.inspect(missing)['correct'])

    def test_recovery_preview_does_not_modify_state_and_restore_is_explicit(self):
        spec = self.specs[1]
        self.assertTrue(self.service.execute(self.service.preview([spec]))['success'])
        record = self.service.records[-1]
        record.update(state='pending', success=False, recovery_required=True)
        states_before = self.service.states.copy()
        self.assertTrue(self.service.preview_recovery(record['id'])['success'])
        self.assertEqual(self.service.states, states_before)
        self.assertTrue(self.service.recover(record['id'])['success'])
        self.assertFalse(self.service.inspect(spec)['target_exists'])
        self.assertFalse(self.service.recover(record['id'])['success'])

    def test_permanent_deletion_cannot_be_undone_or_recovered(self):
        spec = next(spec for spec in self.specs if 'Pictures' in spec.target)
        result = self.service.execute(self.service.preview([spec], delete_existing=True))
        self.assertTrue(result['irreversible'])
        record = self.service.records[-1]
        self.assertFalse(self.service.undo(record['id'])['success'])
        record.update(state='pending', success=False, recovery_required=True)
        self.assertFalse(self.service.preview_recovery(record['id'])['success'])
        self.assertFalse(self.service.recover(record['id'])['success'])


if __name__ == '__main__':
    unittest.main()
