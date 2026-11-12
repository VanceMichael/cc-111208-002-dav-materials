import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from materialdav.parser import inspect
from materialdav.journal import Journal
from materialdav.transport import capture
from scripts.materialize import main as materialize
from scripts.source_server import running


class BaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        materialize()

    def test_capture_journal_is_content_addressed(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            try:
                first = journal.save(b'synthetic', 'source-1')
                second = journal.save(b'synthetic', 'source-1')
                self.assertEqual(first, second)
                self.assertEqual(Path(first['path']).read_bytes(), b'synthetic')
                self.assertEqual(journal.database.execute('SELECT COUNT(*) FROM captures').fetchone()[0], 1)
            finally:
                journal.close()

    def test_remote_transport_is_rejected_before_io(self):
        with self.assertRaises(ValueError):
            capture('https://example.invalid/private')

    def test_cli_help(self):
        result = subprocess.run([sys.executable, '-m', 'materialdav', '--help'], capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn(b'inspect', result.stdout)

    def test_local_source_and_request_log(self):
        with running() as server:
            status, headers, payload = capture(server.origin + '/dav/', 'PROPFIND', headers={'Depth': '1'})
            self.assertEqual(status, 207)
            self.assertGreater(len(payload), 0)
            self.assertEqual(server.requests[0]['method'], 'PROPFIND')

    def test_unknown_source_route_is_not_success(self):
        with running() as server:
            self.assertEqual(capture(server.origin + '/not-present')[0], 404)

    def test_property_statuses_are_distinct(self):
        result = inspect(Path('fixtures/properties.xml').read_bytes())
        self.assertEqual(result['sync_token'], 'T1')
        self.assertEqual(len(result['resources'][1]['properties']), 2)
        self.assertIn('500', result['resources'][1]['properties'][1]['status'])

    def test_tombstone_is_response_status(self):
        result = inspect(Path('fixtures/deletion.xml').read_bytes())
        self.assertIn('404', result['resources'][0]['status'])
        self.assertEqual(result['resources'][0]['properties'], [])

    def test_download_precondition(self):
        with running() as server:
            self.assertEqual(capture(server.origin + '/dav/%E6%A0%B7%E5%86%8C.pdf')[0], 412)
            self.assertEqual(capture(server.origin + '/dav/%E6%A0%B7%E5%86%8C.pdf', headers={'If-Match': '"v1"'})[0], 200)


if __name__ == '__main__':
    unittest.main()
