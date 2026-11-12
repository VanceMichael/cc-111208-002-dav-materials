"""mirror 状态的只读查询。

下列 SQL 同时写在 docs/mirror.md，供运营直接核对目录身份、
属性可信度与待重取项。
"""
from .store import MirrorStore

QUERIES = {
    # 目录身份：每个代际发现的集合与其 sync-token、列举完成情况。
    'collections': """
        SELECT c.generation_id, g.state AS generation_state, c.identity,
               c.parent_identity, c.sync_token, c.listed, c.listed_at
        FROM collections c JOIN generations g ON g.id = c.generation_id
        ORDER BY c.generation_id, c.discovered_at
    """,
    # 属性可信度：trusted=1 来自 propstat 200；trusted=0 是 403/500
    # 登记的未知项，不会覆盖旧可信值。
    'properties': """
        SELECT p.generation_id, p.entry_identity, p.name, p.value, p.trusted
        FROM properties p
        WHERE p.generation_id = (SELECT MAX(id) FROM generations)
        ORDER BY p.entry_identity, p.name
    """,
    # 待重取项：未完成 ETag 校验的文件及失败尝试次数。
    'pending': """
        SELECT f.generation_id, f.entry_identity, f.etag, f.reason,
               f.attempts, f.enqueued_at
        FROM fetch_queue f
        ORDER BY f.generation_id, f.enqueued_at
    """,
    'generations': """
        SELECT id, root_url, state, created_at, activated_at, completed_at
        FROM generations ORDER BY id
    """,
    'files': """
        SELECT e.identity, e.generation_id, e.etag, e.digest, e.verified_at,
               e.deleted
        FROM entries e WHERE e.kind = 'file'
        ORDER BY e.generation_id, e.identity
    """,
}


def query_status(state_dir):
    store = MirrorStore(state_dir)
    try:
        result = {}
        for name, sql in QUERIES.items():
            result[name] = [dict(row) for row in store.database.execute(sql)]
        result['events'] = [dict(row) for row in store.events()]
        return result
    finally:
        store.close()
