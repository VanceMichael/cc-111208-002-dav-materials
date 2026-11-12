"""mirror 的 SQLite 状态与内容寻址正文存储。

库文件位于 ``<state>/mirror.sqlite3``，正文位于 ``<state>/blobs``，
本地样册树位于 ``<state>/tree``。所有写入在事务内完成，进程中途退出后
可依据库内状态恢复，重复执行同一条 REPORT 不会重复安装文件。

代际（generation）是独立重扫的边界：新代际完整遍历期间旧代际保持
``active`` 可读；只有新代际全部集合遍历完成、待取队列清空后才切换。
"""
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS generations (
    id INTEGER PRIMARY KEY,
    root_url TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('scanning', 'active', 'superseded', 'failed')),
    created_at REAL NOT NULL,
    activated_at REAL,
    completed_at REAL
);
CREATE TABLE IF NOT EXISTS collections (
    identity TEXT NOT NULL,
    generation_id INTEGER NOT NULL REFERENCES generations(id),
    parent_identity TEXT,
    sync_token TEXT,
    listed INTEGER NOT NULL DEFAULT 0,
    discovered_at REAL NOT NULL,
    listed_at REAL,
    PRIMARY KEY (identity, generation_id)
);
CREATE TABLE IF NOT EXISTS entries (
    identity TEXT NOT NULL,
    generation_id INTEGER NOT NULL REFERENCES generations(id),
    kind TEXT NOT NULL CHECK (kind IN ('collection', 'file')),
    parent_identity TEXT NOT NULL,
    etag TEXT,
    digest TEXT,
    verified_at REAL,
    deleted INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (identity, generation_id)
);
CREATE TABLE IF NOT EXISTS properties (
    entry_identity TEXT NOT NULL,
    generation_id INTEGER NOT NULL REFERENCES generations(id),
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    trusted INTEGER NOT NULL CHECK (trusted IN (0, 1)),
    updated_at REAL NOT NULL,
    PRIMARY KEY (entry_identity, generation_id, name)
);
CREATE TABLE IF NOT EXISTS files (
    etag TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    size INTEGER NOT NULL,
    stored_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fetch_queue (
    entry_identity TEXT NOT NULL,
    generation_id INTEGER NOT NULL REFERENCES generations(id),
    etag TEXT NOT NULL,
    reason TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    enqueued_at REAL NOT NULL,
    PRIMARY KEY (entry_identity, generation_id)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    generation_id INTEGER,
    kind TEXT NOT NULL,
    identity TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_entries_generation ON entries(generation_id);
CREATE INDEX IF NOT EXISTS idx_collections_generation ON collections(generation_id);
CREATE INDEX IF NOT EXISTS idx_fetch_generation ON fetch_queue(generation_id);
"""


class MirrorStore:
    def __init__(self, state_dir):
        self.root = Path(state_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.blobs = self.root / 'blobs'
        self.blobs.mkdir(exist_ok=True)
        self.tree = self.root / 'tree'
        self.tree.mkdir(exist_ok=True)
        self.database = sqlite3.connect(self.root / 'mirror.sqlite3')
        self.database.row_factory = sqlite3.Row
        self.database.execute('PRAGMA journal_mode=WAL')
        self.database.execute('PRAGMA foreign_keys=ON')
        self.database.executescript(SCHEMA)

    def close(self):
        self.database.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- 代际 -----------------------------------------------------------
    def active_generation(self):
        return self.database.execute(
            'SELECT * FROM generations WHERE state = ? ORDER BY id DESC LIMIT 1',
            ('active',)).fetchone()

    def get_generation(self, generation_id):
        return self.database.execute(
            'SELECT * FROM generations WHERE id = ?', (generation_id,)).fetchone()

    def create_generation(self, root_url):
        now = time.time()
        cursor = self.database.execute(
            'INSERT INTO generations (root_url, state, created_at) VALUES (?, ?, ?)',
            (root_url, 'scanning', now))
        generation_id = cursor.lastrowid
        self.add_event(generation_id, 'generation_start', root_url)
        return generation_id

    def activate_generation(self, generation_id):
        """切换活跃代际：旧代际标记 superseded，新代际置 active。"""
        now = time.time()
        with self.database:
            self.database.execute(
                "UPDATE generations SET state = 'superseded', completed_at = ? "
                "WHERE state = 'active'", (now,))
            self.database.execute(
                "UPDATE generations SET state = 'active', activated_at = ? WHERE id = ?",
                (now, generation_id))
            self.add_event(generation_id, 'generation_activate')

    # -- 集合 -----------------------------------------------------------
    def upsert_collection(self, identity, generation_id, parent_identity):
        """登记集合；已存在时保留 listed/sync_token，崩溃恢复不会丢进度。"""
        self.database.execute(
            'INSERT INTO collections (identity, generation_id, parent_identity, discovered_at) '
            'VALUES (?, ?, ?, ?) ON CONFLICT(identity, generation_id) DO UPDATE SET '
            'parent_identity = excluded.parent_identity',
            (identity, generation_id, parent_identity, time.time()))

    def get_collection(self, identity, generation_id):
        return self.database.execute(
            'SELECT * FROM collections WHERE identity = ? AND generation_id = ?',
            (identity, generation_id)).fetchone()

    def mark_collection_listed(self, identity, generation_id, sync_token):
        self.database.execute(
            'UPDATE collections SET listed = 1, listed_at = ?, sync_token = ? '
            'WHERE identity = ? AND generation_id = ?',
            (time.time(), sync_token, identity, generation_id))

    def unlisted_collections(self, generation_id):
        return self.database.execute(
            'SELECT * FROM collections WHERE generation_id = ? AND listed = 0 ORDER BY discovered_at',
            (generation_id,)).fetchall()

    # -- 条目 -----------------------------------------------------------
    def upsert_entry(self, identity, generation_id, kind, parent_identity, etag=None):
        now = time.time()
        self.database.execute(
            'INSERT INTO entries (identity, generation_id, kind, parent_identity, etag, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(identity, generation_id) DO UPDATE SET '
            'kind = excluded.kind, parent_identity = excluded.parent_identity, '
            'etag = COALESCE(excluded.etag, entries.etag), deleted = 0, updated_at = excluded.updated_at',
            (identity, generation_id, kind, parent_identity, etag, now))

    def get_entry(self, identity, generation_id):
        return self.database.execute(
            'SELECT * FROM entries WHERE identity = ? AND generation_id = ?',
            (identity, generation_id)).fetchone()

    def delete_entry(self, identity, generation_id):
        """response 层 404/410 的唯一删除路径。"""
        now = time.time()
        with self.database:
            cursor = self.database.execute(
                'UPDATE entries SET deleted = 1, updated_at = ? '
                'WHERE identity = ? AND generation_id = ? AND deleted = 0',
                (now, identity, generation_id))
            self.database.execute(
                'DELETE FROM fetch_queue WHERE entry_identity = ? AND generation_id = ?',
                (identity, generation_id))
            if cursor.rowcount:
                self.add_event(generation_id, 'entry_tombstone', identity)

    def prune_collection_members(self, collection_identity, generation_id, seen_identities):
        """完整遍历边界：集合已读完，未再出现的成员才标记删除。

        ``seen_identities`` 是本次 207 中实际出现的直接成员身份；
        集合自身不在删除范围内（集合身份以斜杠结尾，成员是其直接下级）。
        """
        now = time.time()
        with self.database:
            rows = self.database.execute(
                'SELECT identity FROM entries WHERE generation_id = ? '
                'AND parent_identity = ? AND deleted = 0',
                (generation_id, collection_identity)).fetchall()
            removed = 0
            for row in rows:
                if row['identity'] not in seen_identities:
                    self.database.execute(
                        'UPDATE entries SET deleted = 1, updated_at = ? '
                        'WHERE identity = ? AND generation_id = ?',
                        (now, row['identity'], generation_id))
                    self.database.execute(
                        'DELETE FROM fetch_queue WHERE entry_identity = ? AND generation_id = ?',
                        (row['identity'], generation_id))
                    removed += 1
            if removed:
                self.add_event(generation_id, 'collection_prune', collection_identity,
                               str(removed) + ' members removed')

    def live_entries(self, generation_id, kind=None):
        sql = 'SELECT * FROM entries WHERE generation_id = ? AND deleted = 0'
        parameters = [generation_id]
        if kind:
            sql += ' AND kind = ?'
            parameters.append(kind)
        return self.database.execute(sql, parameters).fetchall()

    # -- 属性 -----------------------------------------------------------
    def trust_property(self, entry_identity, generation_id, name, value):
        """可信属性（propstat 200）：写入或覆盖。"""
        self.database.execute(
            'INSERT INTO properties (entry_identity, generation_id, name, value, trusted, updated_at) '
            'VALUES (?, ?, ?, ?, 1, ?) ON CONFLICT(entry_identity, generation_id, name) '
            'DO UPDATE SET value = excluded.value, trusted = 1, updated_at = excluded.updated_at',
            (entry_identity, generation_id, name, value, time.time()))

    def mark_property_unknown(self, entry_identity, generation_id, name):
        """403/500 属性：登记为不可信，绝不擦掉已有可信值。"""
        self.database.execute(
            'INSERT OR IGNORE INTO properties (entry_identity, generation_id, name, value, trusted, updated_at) '
            "VALUES (?, ?, ?, '', 0, ?)",
            (entry_identity, generation_id, name, time.time()))

    def get_properties(self, entry_identity, generation_id):
        return self.database.execute(
            'SELECT name, value, trusted FROM properties WHERE entry_identity = ? AND generation_id = ?',
            (entry_identity, generation_id)).fetchall()

    # -- 正文与待取队列 -------------------------------------------------
    def store_blob(self, etag, payload):
        digest = hashlib.sha256(payload).hexdigest()
        destination = self.blobs / digest
        if not destination.exists():
            descriptor, name = tempfile.mkstemp(dir=self.blobs)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, destination)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        with self.database:
            self.database.execute(
                'INSERT OR IGNORE INTO files (etag, digest, size, stored_at) VALUES (?, ?, ?, ?)',
                (etag, digest, len(payload), time.time()))
        return digest

    def file_by_etag(self, etag):
        return self.database.execute(
            'SELECT * FROM files WHERE etag = ?', (etag,)).fetchone()

    def blob_path(self, digest):
        return self.blobs / digest

    def mark_verified(self, entry_identity, generation_id, etag, digest):
        self.database.execute(
            'UPDATE entries SET etag = ?, digest = ?, verified_at = ? '
            'WHERE identity = ? AND generation_id = ?',
            (etag, digest, time.time(), entry_identity, generation_id))
        self.database.execute(
            'DELETE FROM fetch_queue WHERE entry_identity = ? AND generation_id = ?',
            (entry_identity, generation_id))

    def enqueue_fetch(self, entry_identity, generation_id, etag, reason):
        self.database.execute(
            'INSERT OR IGNORE INTO fetch_queue (entry_identity, generation_id, etag, reason, enqueued_at) '
            'VALUES (?, ?, ?, ?, ?)',
            (entry_identity, generation_id, etag, reason, time.time()))

    def pending_fetches(self, generation_id):
        return self.database.execute(
            'SELECT * FROM fetch_queue WHERE generation_id = ? ORDER BY enqueued_at',
            (generation_id,)).fetchall()

    def bump_fetch_attempt(self, entry_identity, generation_id):
        self.database.execute(
            'UPDATE fetch_queue SET attempts = attempts + 1 WHERE entry_identity = ? AND generation_id = ?',
            (entry_identity, generation_id))

    # -- 事件 -----------------------------------------------------------
    def add_event(self, generation_id, kind, identity=None, detail=None):
        self.database.execute(
            'INSERT INTO events (at, generation_id, kind, identity, detail) VALUES (?, ?, ?, ?, ?)',
            (time.time(), generation_id, kind, identity, detail))

    def events(self, limit=200):
        return self.database.execute(
            'SELECT * FROM events ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
