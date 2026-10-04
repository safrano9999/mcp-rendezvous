import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mcp_rendezvous import Rendezvous

CONTRACT = json.loads((Path(__file__).parent / 'fixtures/contract.json').read_text())['base']


class TableTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.rv = Rendezvous(CONTRACT, self.root)
        self.rv.heartbeat()

    def test_persists_after_disconnect_restart_and_database_reopen(self):
        class Session: pass
        session = Session()
        identifier = self.rv.queue('build_images', 'run',
            self.rv.endpoint('http://test.local/hook', 'secret-not-for-the-table'),
            session=session, command=['private-command'])
        cursor = self.rv.table.snapshot()['cursor']
        self.rv.disconnect(session)
        self.rv = Rendezvous(CONTRACT, self.root)
        self.assertEqual(self.rv.table.snapshot()['cursor'], cursor)
        self.assertEqual(self.rv.table.snapshot(cursor)['entries'], [])
        self.rv.complete(identifier, 'success')
        result = self.rv.table.snapshot(cursor)
        self.assertTrue(result['persistent']); self.assertFalse(result['ephemeral'])
        self.assertEqual(result['entries'][0]['state'], 'finished')
        self.assertEqual(self.rv.table.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('secret-not-for-the-table', json.dumps(result))
        self.assertNotIn('private-command', json.dumps(result))

    def test_parallel_processes_keep_all_jobs_and_all_field_updates(self):
        shared = self.rv.queue('build_images', 'run', None)
        config = self.root / 'config.json'; config.write_text(json.dumps(CONTRACT))
        code = '''
import sys
from mcp_rendezvous import Rendezvous
rv = Rendezvous(sys.argv[1], sys.argv[2])
for index in range(8):
    key = "writer_" + sys.argv[4] + "_" + str(index)
    rv.update(sys.argv[3], **{key: True})
    identifier = rv.queue("build_images", "run", None, writer=key)
    rv.complete(identifier, "success")
'''
        processes = [subprocess.Popen([sys.executable, '-c', code, str(config), str(self.root), shared, str(i)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(4)]
        for process in processes:
            _, error = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, error)
        rows = self.rv.table.snapshot()['entries']
        self.assertEqual(len(rows), 33)
        shared_row = next(r for r in rows if r['id'] == shared)
        self.assertEqual(len([key for key in shared_row if key.startswith('writer_')]), 32)
        self.assertEqual(sum(r['state'] == 'finished' for r in rows), 32)
        self.assertEqual(len({r['change_cursor'] for r in rows}), len(rows))

    def test_recovery_repairs_interrupted_publication_before_next_notification(self):
        identifier = self.rv.queue('build_images', 'run', None)
        with patch.object(self.rv.table, 'save', side_effect=RuntimeError('simulated interruption')):
            with self.assertRaises(RuntimeError): self.rv.complete(identifier, 'success')
        self.assertEqual(self.rv.table.snapshot()['entries'][0]['state'], 'pending')
        reopened = Rendezvous(CONTRACT, self.root)
        self.assertEqual(reopened.table.snapshot()['entries'][0]['state'], 'finished')
        self.assertEqual(reopened.table.snapshot()['entries'][0]['outcome'], 'success')

    def test_finished_table_row_is_committed_before_webhook(self):
        identifier = self.rv.queue('build_images', 'run', self.rv.endpoint('http://test.local/hook'))
        self.rv.complete(identifier, 'failure', error='test_failure')
        def notified(_):
            row = Rendezvous(CONTRACT, self.root).table.snapshot()['entries'][0]
            self.assertEqual(row['state'], 'finished')
            self.assertEqual(row['error'], 'test_failure')
            self.assertIsNotNone(row['timing']['finished_at'])
        with patch.object(self.rv, 'notify', side_effect=notified) as notify:
            self.rv.deliver_pending()
            notify.assert_called_once()


if __name__ == '__main__': unittest.main()
