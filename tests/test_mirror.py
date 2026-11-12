import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from scripts.source_server import running
from materialdav.davmsg import parse_multistatus
from materialdav.identity import (ensure_within, identity_url, normalize_path,
                                  relative_segments, resolve_href)
from materialdav.mirror import MirrorEngine
from materialdav.status import query_status

FIXTURES = str(Path(__file__).resolve().parent.parent / 'fixtures' / 'mirror')


class IdentityTests(unittest.TestCase):
    ROOT = 'http://127.0.0.1:8000/dav/'

    def test_percent_encoded_slash_is_a_segment(self):
        url = identity_url(self.ROOT, self.ROOT, '/dav/a%2Fb.pdf', False)
        self.assertEqual(url, 'http://127.0.0.1:8000/dav/a%2Fb.pdf')
        self.assertEqual(relative_segments(self.ROOT, url), ['a/b.pdf'])

    def test_unicode_names_are_nfc_and_stable(self):
        url = identity_url(self.ROOT, self.ROOT,
                           '/dav/%E6%A0%B7%E5%86%8C.pdf', False)
        again = identity_url(self.ROOT, self.ROOT, '/dav/样册.pdf', False)
        self.assertEqual(url, again)
        self.assertEqual(relative_segments(self.ROOT, url), ['样册.pdf'])

    def test_relative_href_uses_response_base(self):
        url = identity_url(self.ROOT, 'http://127.0.0.1:8000/dav/catalogs/',
                           'child.pdf', False)
        self.assertEqual(url, 'http://127.0.0.1:8000/dav/catalogs/child.pdf')

    def test_collection_identity_keeps_trailing_slash(self):
        url = identity_url(self.ROOT, self.ROOT, '/dav/catalogs', True)
        self.assertTrue(url.endswith('/'))
        self.assertEqual(normalize_path('/dav/catalogs'), '/dav/catalogs')
        self.assertEqual(normalize_path('/dav/catalogs/'), '/dav/catalogs/')

    def test_dot_segments_cannot_escape_root(self):
        with self.assertRaises(ValueError):
            identity_url(self.ROOT, self.ROOT, '/dav/../etc/passwd', False)
        with self.assertRaises(ValueError):
            ensure_within(self.ROOT, 'http://127.0.0.1:8000/other/x')
        with self.assertRaises(ValueError):
            ensure_within(self.ROOT, 'http://example.com/dav/x')

    def test_same_name_in_different_collections_is_distinct(self):
        first = identity_url(self.ROOT, self.ROOT, '/dav/a/name.pdf', False)
        second = identity_url(self.ROOT, self.ROOT, '/dav/b/name.pdf', False)
        self.assertNotEqual(first, second)


class MessageTests(unittest.TestCase):
    def test_propstat_failure_does_not_emit_value(self):
        payload = (b'<d:multistatus xmlns:d="DAV:"><d:response><d:href>/dav/x</d:href>'
                   b'<d:propstat><d:prop><d:getetag>"v1"</d:getetag></d:prop>'
                   b'<d:status>HTTP/1.1 200 OK</d:status></d:propstat>'
                   b'<d:propstat><d:prop><d:getcontentlength/></d:prop>'
                   b'<d:status>HTTP/1.1 500 Internal Server Error</d:status></d:propstat>'
                   b'</d:response></d:multistatus>')
        message = parse_multistatus(payload)
        response = message.responses[0]
        self.assertTrue(response.trusted('getetag'))
        self.assertFalse(response.trusted('getcontentlength'))
        self.assertNotIn('getcontentlength', response.properties)
        self.assertEqual(response.prop_status['getcontentlength'], 500)
        self.assertFalse(response.tombstone)

    def test_only_response_level_status_is_tombstone(self):
        payload = (b'<d:multistatus xmlns:d="DAV:"><d:response>'
                   b'<d:href>/dav/gone</d:href>'
                   b'<d:status>HTTP/1.1 404 Not Found</d:status></d:response>'
                   b'<d:sync-token>T9</d:sync-token></d:multistatus>')
        message = parse_multistatus(payload)
        self.assertTrue(message.responses[0].tombstone)
        self.assertEqual(message.sync_token, 'T9')


class MirrorEndToEndTests(unittest.TestCase):
    def _engine(self, server, state):
        return MirrorEngine(server.origin + '/dav/', state)

    def test_initial_scan_materializes_authorized_set(self):
        with running(FIXTURES, 'initial.json') as server, tempfile.TemporaryDirectory() as state:
            engine = self._engine(server, state)
            try:
                result = engine.run_once()
            finally:
                engine.close()
            self.assertTrue(result['activated'])
            self.assertEqual(result['errors'], [])
            tree = Path(state) / 'tree'
            self.assertEqual((tree / 'root-only.pdf').read_text(), 'root-only-body')
            self.assertEqual((tree / '样册.pdf').read_text(), 'unicode-book-body')
            self.assertEqual((tree / 'catalogs' / 'alpha.pdf').read_text(), 'alpha-body')
            self.assertEqual((tree / 'catalogs' / 'relative name.pdf').read_text(),
                             'relative-name-body')
            # 编码斜杠不成为路径分隔；跨集合同名项各自独立。
            self.assertTrue((tree / 'catalogs' / 'a_b.pdf').is_file())
            self.assertEqual((tree / 'catalogs' / 'namesake.pdf').read_text(),
                             'catalog-namesake')
            self.assertEqual((tree / 'namesake.pdf').read_text(), 'root-namesake-body')
            # 重复条目没有触发重复下载。
            gets = [request for request in server.requests if request['method'] == 'GET']
            self.assertEqual(len(gets), len({request['path'] for request in gets}))
            status = query_status(state)
            self.assertTrue(all(row['listed'] for row in status['collections']))
            untrusted = [row for row in status['properties']
                         if row['name'] == 'getcontentlength' and not row['trusted']]
            self.assertEqual(len(untrusted), 1)

    def test_incremental_tombstone_move_and_property_trust(self):
        with running(FIXTURES, 'incremental.json') as server, tempfile.TemporaryDirectory() as state:
            engine = self._engine(server, state)
            try:
                first = engine.run_once()
                self.assertTrue(first['activated'])
                second = engine.run_once()
                self.assertEqual(second['mode'], 'incremental')
                self.assertEqual(second['tombstones'], 1)
                self.assertEqual(second['reused'], 1)
                self.assertTrue(second['activated'])
                db = engine.store.database
                old = db.execute(
                    "SELECT deleted FROM entries WHERE identity LIKE '%/dav/moving.pdf' "
                    "AND generation_id = 1").fetchall()
                self.assertTrue(any(row[0] == 1 for row in old))
                moved = db.execute(
                    "SELECT digest FROM entries WHERE generation_id = 1 AND deleted = 0 "
                    "AND identity LIKE '%catalogs/moving.pdf'").fetchone()
                self.assertIsNotNone(moved)
                # 增量 REPORT 中 403 的 getcontentlength 不能擦掉上次已知值。
                current = tuple(db.execute(
                    "SELECT value, trusted FROM properties WHERE generation_id = 1 "
                    "AND entry_identity LIKE '%fragile.pdf' AND name = 'getcontentlength'"
                ).fetchone())
                self.assertEqual(current, ('42', 1))
                unknown_row = db.execute(
                    "SELECT COUNT(*) FROM properties WHERE name = 'getcontentlength' "
                    "AND trusted = 0 AND entry_identity LIKE '%fragile.pdf'"
                ).fetchone()
                # 同代际内可信值保留；不可信登记只发生在重扫后的新代际。
                self.assertEqual(unknown_row[0], 0)
                gets = [request for request in server.requests if request['method'] == 'GET']
                self.assertNotIn('/dav/catalogs/moving.pdf',
                                 [request['path'] for request in gets])
            finally:
                engine.close()

    def test_invalid_token_builds_separate_rescan_generation(self):
        with running(FIXTURES, 'incremental.json') as server, tempfile.TemporaryDirectory() as state:
            engine = self._engine(server, state)
            try:
                engine.run_once()
                engine.run_once()  # 消费第一次 REPORT（207）
                result = engine.run_once()  # 第二次 REPORT 返回 403 valid-sync-token
                self.assertTrue(result['rescan'])
                self.assertTrue(result['rescan_summary']['activated'])
                rows = engine.store.database.execute(
                    'SELECT id, state FROM generations ORDER BY id').fetchall()
                self.assertEqual([(row['id'], row['state']) for row in rows],
                                 [(1, 'superseded'), (2, 'active')])
                # 旧代际数据仍可查询（保留可读）。
                old = engine.store.database.execute(
                    'SELECT COUNT(*) FROM entries WHERE generation_id = 1').fetchone()[0]
                self.assertGreater(old, 0)
                # 旧代际里 fragile 的长度仍是可信旧值；新代际登记为不可信而非空值覆盖。
                old_prop = tuple(engine.store.database.execute(
                    "SELECT value, trusted FROM properties WHERE generation_id = 1 "
                    "AND entry_identity LIKE '%fragile.pdf' AND name = 'getcontentlength'"
                ).fetchone())
                self.assertEqual(old_prop, ('42', 1))
                new_prop = tuple(engine.store.database.execute(
                    "SELECT value, trusted FROM properties WHERE generation_id = 2 "
                    "AND entry_identity LIKE '%fragile.pdf' AND name = 'getcontentlength'"
                ).fetchone())
                self.assertEqual(new_prop, ('', 0))
            finally:
                engine.close()

    def test_download_race_rebinds_after_412(self):
        with running(FIXTURES, 'race.json') as server, tempfile.TemporaryDirectory() as state:
            engine = self._engine(server, state)
            try:
                result = engine.run_once()
                self.assertTrue(result['activated'])
                self.assertEqual(result['refreshes'], 1)
                entry = engine.store.database.execute(
                    "SELECT etag, digest FROM entries WHERE identity LIKE '%price-list.pdf'"
                ).fetchone()
                self.assertEqual(entry['etag'], '"v2"')
                self.assertIsNotNone(entry['digest'])
                self.assertEqual((Path(state) / 'tree' / 'price-list.pdf').read_text(),
                                 'revised-price-body')
                # 只有 v2 正文被安装，v1 从未落盘。
                files = {row['etag'] for row in
                         engine.store.database.execute('SELECT etag FROM files')}
                self.assertEqual(files, {'"v2"'})
                gets = [request for request in server.requests if request['method'] == 'GET']
                self.assertEqual(gets[0]['headers']['If-Match'], '"v1"')
                self.assertEqual(gets[1]['headers']['If-Match'], '"v2"')
            finally:
                engine.close()

    def test_repeated_report_does_not_reinstall_files(self):
        with running(FIXTURES, 'initial.json') as server, tempfile.TemporaryDirectory() as state:
            engine = self._engine(server, state)
            try:
                engine.run_once()
                gets_after_first = len([r for r in server.requests if r['method'] == 'GET'])
                engine.run_once()
                engine.run_once()
                gets_total = len([r for r in server.requests if r['method'] == 'GET'])
                self.assertEqual(gets_total, gets_after_first)
            finally:
                engine.close()

    def test_subprocess_crash_resumes_same_generation(self):
        # 验收要求：崩溃测试必须启动子进程并实际终止。
        with running(FIXTURES, 'crash.json') as server:
            root = server.origin + '/dav/'
            with tempfile.TemporaryDirectory() as state:
                proc = subprocess.Popen(
                    [sys.executable, '-m', 'materialdav', 'mirror', root, '--state', state],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                time.sleep(1.5)
                proc.send_signal(signal.SIGTERM)
                proc.communicate(timeout=10)
                self.assertEqual(proc.returncode, -signal.SIGTERM)
                db = sqlite3.connect(Path(state) / 'mirror.sqlite3')
                self.assertEqual(db.execute(
                    "SELECT state FROM generations").fetchone()[0], 'scanning')
                unlisted = db.execute(
                    'SELECT COUNT(*) FROM collections WHERE listed = 0').fetchone()[0]
                self.assertEqual(unlisted, 1)
                db.close()
                # 旧代际尚未激活，tree 不暴露半成品。
                self.assertFalse((Path(state) / 'tree' / '.generation').exists())

                resumed = subprocess.run(
                    [sys.executable, '-m', 'materialdav', 'mirror', root, '--state', state],
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                summary = json.loads(resumed.stdout)
                self.assertTrue(summary['activated'])
                self.assertEqual(summary['generation_id'], 1)
                self.assertTrue((Path(state) / 'tree' / 'second' / 'two.pdf').is_file())

                gets_before = len([r for r in server.requests if r['method'] == 'GET'])
                again = subprocess.run(
                    [sys.executable, '-m', 'materialdav', 'mirror', root, '--state', state],
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(again.returncode, 0)
                gets_after = len([r for r in server.requests if r['method'] == 'GET'])
                self.assertEqual(gets_before, gets_after)

    def test_off_root_href_is_rejected_not_followed(self):
        with running(FIXTURES, 'initial.json') as server, tempfile.TemporaryDirectory() as state:
            engine = self._engine(server, state)
            try:
                with self.assertRaises(ValueError):
                    identity_url(server.origin + '/dav/', server.origin + '/dav/',
                                 'http://example.com/dav/x', False)
            finally:
                engine.close()


if __name__ == '__main__':
    unittest.main()
