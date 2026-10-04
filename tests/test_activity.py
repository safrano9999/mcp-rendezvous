import gc
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import weakref

from mcp_rendezvous import Rendezvous
from mcp_rendezvous.core import write

CONTRACT = json.loads((Path(__file__).parent / 'fixtures/contract.json').read_text())


class Session: pass


class ActivityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.rv = Rendezvous(CONTRACT['base'], temp.name)
        self.rv.heartbeat()
        self.session = Session()
        self.table = self.rv.connection(self.session)

    def queue(self, **values):
        return self.rv.queue('build_images', 'run', None, session=self.session, **values)

    def test_isolation_descendants_and_no_payload_secrets(self):
        root = self.queue(auto_pull=True, command=['private-command'])
        child = self.rv.queue('podman_smart1', 'pull', None, parent_id=root)
        grandchild = self.rv.queue('podman_smart1', 'pull', None, parent_id=child)
        other = Session()
        foreign = self.rv.queue('build_images', 'run', self.rv.destination('http://test.local/hook', 'private-secret'), session=other)
        snapshot = self.table.snapshot()
        self.assertEqual({r['id'] for r in snapshot['entries']}, {root, child, grandchild})
        self.assertEqual([r['id'] for r in self.rv.connection(other).snapshot()['entries']], [foreign])
        self.assertNotIn('private', json.dumps(snapshot))
        self.assertNotIn('private', json.dumps(self.rv.connection(other).snapshot()))
        self.assertTrue(next(r for r in snapshot['entries'] if r['id'] == root)['auto_pull'])

    def test_explicit_cursor_replay_and_no_duration_only_changes(self):
        identifier = self.queue()
        initial = self.table.snapshot()
        cursor = initial['cursor']
        self.assertEqual(self.table.snapshot(cursor)['entries'], [])
        self.rv.update(identifier, state='running', phase='build')
        changes = self.table.snapshot(cursor)
        self.assertEqual(changes['entries'][0]['state'], 'running')
        self.assertEqual(self.table.snapshot(cursor)['cursor'], changes['cursor'])
        self.assertEqual(self.table.snapshot(changes['cursor'])['entries'], [])
        with self.assertRaises(ValueError): self.table.snapshot(changes['cursor'] + 1)
        with self.assertRaises(ValueError): self.table.snapshot(True)

    def test_times_and_timeline_survive_worker_writes_and_noops(self):
        base = self.rv.root.joinpath('heartbeat').stat().st_mtime
        with patch('mcp_rendezvous.core.time.time', return_value=base): identifier = self.queue()
        path = self.rv.path(identifier)
        with patch('mcp_rendezvous.core.time.time', return_value=base + 3):
            data = json.loads(path.read_text()); data.update(state='running', phase='build'); write(path, data)
        with patch('mcp_rendezvous.core.time.time', return_value=base + 8):
            self.rv.complete(identifier, 'success')
        row = self.table.snapshot()['entries'][0]
        self.assertEqual(row['timing']['queue_seconds'], 3)
        self.assertEqual(row['timing']['run_seconds'], 5)
        self.assertEqual(row['timing']['elapsed_seconds'], 8)
        self.assertEqual([event['changes']['state'] for event in row['timeline']], ['pending', 'running', 'finished'])
        self.rv.update(identifier, outcome='success')
        self.assertEqual(self.rv.status(identifier)['revision'], row['revision'])
        self.assertEqual(self.rv.status(identifier)['timeline'], row['timeline'])

    def test_webhook_and_herdr_observe_finished_result_before_signal(self):
        for transport in ('webhook', 'herdr'):
            identifier = self.queue()
            path = self.rv.path(identifier)
            data = json.loads(path.read_text())
            # Intentionally pass an as-yet unpublished completion to deliver.
            data.update(state='finished', outcome='failure', error='build_failed', notification='pending')
            data['callback'] = {'kind': 'herdr'} if transport == 'herdr' else self.rv.destination('http://test.local/hook')
            def signal(*args):
                row = next(r for r in self.table.snapshot()['entries'] if r['id'] == identifier)
                self.assertEqual(row['error'], 'build_failed')
                self.assertEqual(row['state'], 'finished')
                self.assertIsNotNone(row['timing']['finished_at'])
            herdr = Mock(); herdr.send.side_effect = signal
            with patch.object(self.rv, 'notify', side_effect=signal) as webhook, \
                 patch.object(self.rv, 'herdr', herdr), patch.object(self.rv.policy, 'route'):
                self.rv.deliver(path, data)
                (herdr.send if transport == 'herdr' else webhook).assert_called_once()

    def test_separate_process_completion_is_immediately_visible(self):
        identifier = self.queue()
        cursor = self.table.snapshot()['cursor']
        subprocess.run([sys.executable, '-c',
            'import json,sys; from pathlib import Path; from mcp_rendezvous.core import write; '
            'p=Path(sys.argv[1]); d=json.loads(p.read_text()); '
            'd.update(state="finished",outcome="failure",error="external_failure"); write(p,d)',
            str(self.rv.path(identifier))], check=True)
        row = self.table.snapshot(cursor)['entries'][0]
        self.assertEqual(row['error'], 'external_failure')
        self.assertIsNotNone(row['timing']['finished_at'])

    def test_disconnect_and_garbage_collection_discard_only_membership(self):
        identifier = self.queue()
        ref = weakref.ref(self.session)
        self.session = None; gc.collect()
        self.assertIsNone(ref())
        with self.assertRaisesRegex(ValueError, 'closed'): self.table.snapshot()
        replacement = Session()
        self.assertEqual(self.rv.connection(replacement).snapshot()['entries'], [])
        self.assertEqual(self.rv.status(identifier)['state'], 'pending')
        self.rv.disconnect(replacement)
        self.assertEqual(self.rv.connection(replacement).snapshot()['entries'], [])

    def test_table_survives_missing_executor_file_and_reading_never_dispatches(self):
        identifier = self.queue()
        self.rv.path(identifier).unlink()
        with patch.object(self.rv, 'queue', side_effect=AssertionError('Read cannot dispatch')):
            row = self.table.snapshot()['entries'][0]
        self.assertEqual(row['state'], 'pending')
        self.assertNotIn('outcome', row)


if __name__ == '__main__': unittest.main()
