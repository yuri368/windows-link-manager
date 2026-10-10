from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from linkmanager import core, history
from linkmanager.config import LinkSpec
from linkmanager.history import HistoryLease, HistoryRepository


def record(identity: str) -> dict:
    return {'schema_version': 1, 'id': identity, 'action': 'create',
            'state': 'pending', 'success': False, 'results': [], 'operations': []}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='linkmanager-history-')
        self.root = Path(self.temporary.name)
        self.state_dir = self.root / 'state'
        self.state_dir.mkdir()
        self.repository = HistoryRepository(self.state_dir)

    def tearDown(self):
        self.temporary.cleanup()

    def test_different_instances_are_reentrant_on_same_thread(self):
        first = HistoryLease(self.state_dir, timeout=0.15)
        second = HistoryLease(self.state_dir, timeout=0.15)
        with first:
            with second:
                self.repository.store(record('nested'))
                self.assertEqual(self.repository.load()[0]['id'], 'nested')
            self.repository.store(record('after-inner'))
        self.assertEqual(len(self.repository.load()), 2)

    def test_exception_releases_lock_for_another_thread(self):
        lease = HistoryLease(self.state_dir, timeout=0.15)
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            with lease:
                with HistoryLease(self.state_dir):
                    raise RuntimeError('interrupted')
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(self.repository.store, record('after-error')).result(timeout=2)
        self.assertEqual(self.repository.load()[0]['id'], 'after-error')

    def test_another_thread_times_out_and_an_unrelated_state_can_progress(self):
        def try_lease(directory):
            with HistoryLease(directory, timeout=0.15):
                return True
        other = self.root / 'other-state'
        other.mkdir()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with HistoryLease(self.state_dir):
                with self.assertRaises(TimeoutError):
                    executor.submit(try_lease, self.state_dir).result(timeout=2)
                self.assertTrue(executor.submit(try_lease, other).result(timeout=2))
            self.assertTrue(executor.submit(try_lease, self.state_dir).result(timeout=2))

    def test_another_process_cannot_enter_held_lease(self):
        script = """
import sys
from pathlib import Path
from linkmanager.history import HistoryLease
try:
    with HistoryLease(Path(sys.argv[1]), timeout=0.15):
        raise AssertionError('entered another process lease')
except TimeoutError:
    print('locked')
"""
        with HistoryLease(self.state_dir):
            result = subprocess.run([sys.executable, '-c', script, str(self.state_dir)],
                                    capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'locked')
        # The contender's failed acquisition must not poison a later lease.
        self.repository.store(record('after-process'))

    def test_concurrent_process_writers_keep_all_records(self):
        script = """
import sys
from pathlib import Path
from linkmanager.history import HistoryRepository
repository = HistoryRepository(Path(sys.argv[1]))
for number in range(12):
    repository.store({'schema_version': 1, 'id': sys.argv[2] + str(number),
                      'action': 'create', 'state': 'pending', 'success': False,
                      'operations': [], 'results': []})
"""
        processes = [subprocess.Popen([sys.executable, '-c', script, str(self.state_dir), prefix],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for prefix in ('first-', 'second-')]
        try:
            for process in processes:
                _, errors = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, errors)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        self.assertEqual({r['id'] for r in self.repository.load()},
                         {prefix + str(number) for prefix in ('first-', 'second-') for number in range(12)})

    def test_atomic_replace_failure_preserves_previous_history_and_cleans_temp(self):
        self.repository.store(record('original'))
        before = self.repository.path.read_bytes()
        with patch.object(history.os, 'replace', side_effect=OSError('injected replace failure')):
            with self.assertRaises(OSError):
                self.repository.store(record('new'))
        self.assertEqual(self.repository.path.read_bytes(), before)
        self.assertEqual(list(self.state_dir.glob('history-*.tmp')), [])
        self.assertEqual([r['id'] for r in self.repository.load()], ['original'])

    def test_malformed_history_is_not_overwritten(self):
        malformed = ['{not json', '{}', json.dumps([record('same'), record('same')]),
                     json.dumps([dict(record('version'), schema_version=999)]),
                     json.dumps([dict(record('flags'), success='yes')]),
                     json.dumps([dict(record('state'), state={'pending': True})]),
                     json.dumps([dict(record('operations'), operations=[{'irreversible': 'false'}])])]
        for content in malformed:
            with self.subTest(content=content):
                self.repository.path.write_text(content, encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.repository.load()
                with self.assertRaises(ValueError):
                    self.repository.store(record('next'))
                self.assertEqual(self.repository.path.read_text(encoding='utf-8'), content)

    def test_corrupt_history_blocks_filesystem_transaction_before_mutation(self):
        service = core.LinkService(self.state_dir)
        source, target = self.root / 'source', self.root / 'target'
        source.mkdir()
        (source / 'keep.txt').write_text('keep source')
        spec = LinkSpec('corrupt', str(source), str(target), 'junction')
        self.repository.path.write_text('broken audit', encoding='utf-8')
        result = service.execute(service.preview([spec]))
        self.assertFalse(result['success'], result)
        self.assertFalse(os.path.lexists(target))
        self.assertEqual((source / 'keep.txt').read_text(), 'keep source')
        self.assertEqual(self.repository.path.read_text(), 'broken audit')

    def test_legacy_state_and_recovery_flag_normalize_once_at_boundary(self):
        legacy = {'id': 'legacy', 'action': 'create', 'success': False,
                  'state': 'failed', 'message': '回滚未完成', 'operations': [], 'results': []}
        self.repository.path.write_text(json.dumps([legacy]), encoding='utf-8')
        loaded = self.repository.load()[0]
        self.assertTrue(loaded['recovery_required'])
        loaded['message'] = 'localized text changed'
        self.repository.store(loaded)
        self.assertTrue(self.repository.load()[0]['recovery_required'])

    @unittest.skipUnless(os.name == 'nt', 'Junction test needs Windows')
    def test_junction_lock_and_history_are_rejected_without_touching_destination(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'keep.txt').write_text('external data')
        for filename in ('service.lock', 'history.json'):
            with self.subTest(filename=filename):
                entry = self.state_dir / filename
                if entry.exists():
                    entry.unlink()
                core._junction(outside, entry)
                try:
                    with self.assertRaises(ValueError):
                        self.repository.load()
                    self.assertEqual((outside / 'keep.txt').read_text(), 'external data')
                    self.assertEqual({p.name for p in outside.iterdir()}, {'keep.txt'})
                finally:
                    entry.rmdir()


if __name__ == '__main__':
    unittest.main()
