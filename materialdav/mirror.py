"""mirror 同步引擎：让本地样册副本跟上授权集合的变化。

设计要点（详见 docs/mirror.md）：

- 初始与重扫是独立代际：新代际完整遍历当前集合树之前，旧代际保持
  ``active`` 可读；任何子集合没读完，都不能据此判断成员消失。
- 只有 response 层 404/410（墓碑）或已完整遍历集合后的剪枝能删除
  本地条目；propstat 403/500 只登记属性不可信，不擦掉上次已知值。
- GET 一律带列表 ETag 作为 If-Match；412 表示列表与下载间发生改版，
  重新 PROPFIND 核对后再处理，绝不把新正文挂到旧 ETag 名下。
- 所有进度落 SQLite，重复执行可恢复；同 ETag 正文只安装一次。
"""
import os
from pathlib import Path
import shutil
import tempfile
import xml.etree.ElementTree as ET

from .client import DavClient
from .davmsg import KNOWN_PROPERTIES, parse_multistatus
from .identity import identity_url, relative_segments
from .store import MirrorStore

SYNC_HEADER = ('{DAV:}sync-token', '{http://webdav.org/ns/token/}sync-token')


class MirrorEngine:
    def __init__(self, root_url, state_dir, client=None):
        self.root_url = root_url.rstrip('/') + '/'
        self.store = MirrorStore(state_dir)
        self.client = client or DavClient(self.root_url)

    def close(self):
        self.store.close()

    # -- 入口 -----------------------------------------------------------
    def run_once(self):
        """执行一次同步；可反复调用，中途退出后再次调用即恢复。"""
        active = self.store.active_generation()
        scanning = self.store.database.execute(
            "SELECT * FROM generations WHERE state = 'scanning' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if scanning is not None:
            # 上次进程在重扫中途退出：恢复同一代际，旧代际仍 active 可读。
            result = self.full_scan(scanning['id'])
        elif active is None:
            result = self.full_scan()
        elif active['root_url'] != self.root_url:
            raise ValueError('state directory is bound to another collection root')
        else:
            result = self.incremental(active['id'])
        if result.get('activated'):
            self.materialize_tree()
        return result

    # -- 全量重扫代际 ---------------------------------------------------
    def full_scan(self, generation_id=None):
        if generation_id is None:
            generation_id = self.store.create_generation(self.root_url)
            self.store.database.commit()
        summary = {'generation_id': generation_id, 'mode': 'full',
                   'responses': 0, 'tombstones': 0, 'fetches': 0,
                   'refreshes': 0, 'reused': 0, 'errors': []}
        try:
            self._scan_tree(generation_id, summary)
            self._drain_fetches(generation_id, summary)
            pending = self.store.pending_fetches(generation_id)
            incomplete = self.store.unlisted_collections(generation_id)
            if summary['errors'] or pending or incomplete:
                # 子集合没读完或正文未校验完：保留 scanning，下次调用恢复。
                self.store.database.commit()
                summary['activated'] = False
                summary['pending_fetches'] = len(pending)
                summary['incomplete_collections'] = len(incomplete)
                return summary
            self.store.activate_generation(generation_id)
            self.store.database.commit()
            summary['activated'] = True
        except Exception:
            self.store.database.rollback()
            raise
        return summary

    def _scan_tree(self, generation_id, summary):
        self.store.upsert_collection(self.root_url, generation_id, None)
        self.store.database.commit()
        while True:
            pending = self.store.unlisted_collections(generation_id)
            if not pending:
                return
            for collection in pending:
                identity = collection['identity']
                status, _, payload = self.client.propfind(identity)
                if status != 207:
                    summary['errors'].append('PROPFIND %s -> %s' % (identity, status))
                    continue
                multistatus = parse_multistatus(payload)
                seen = self._apply_response(identity, multistatus, generation_id, summary)
                # 完整读完该集合才能剪枝，并写入集合自己的 sync-token。
                self.store.prune_collection_members(identity, generation_id, seen)
                self.store.mark_collection_listed(identity, generation_id,
                                                   multistatus.sync_token)
                self.store.database.commit()

    # -- 增量同步 -------------------------------------------------------
    def incremental(self, generation_id):
        summary = {'generation_id': generation_id, 'mode': 'incremental',
                   'responses': 0, 'tombstones': 0, 'fetches': 0,
                   'refreshes': 0, 'reused': 0, 'errors': [], 'rescan': False}
        collections = self.store.database.execute(
            'SELECT * FROM collections WHERE generation_id = ? ORDER BY discovered_at',
            (generation_id,)).fetchall()
        for collection in collections:
            token = collection['sync_token']
            if not token:
                # 集合从未完成列举，不能做增量，转入全量重扫。
                return self._trigger_rescan(generation_id, summary)
            status, headers, payload = self.client.sync_report(collection['identity'], token)
            if status == 403 and self._has_valid_sync_token(payload):
                return self._trigger_rescan(generation_id, summary)
            if status != 207:
                summary['errors'].append('REPORT %s -> %s' % (collection['identity'], status))
                self.store.database.commit()
                return self._finish_incremental(generation_id, summary)
            multistatus = parse_multistatus(payload)
            self._apply_response(collection['identity'], multistatus,
                                 generation_id, summary)
            # REPORT 是增量变化集而非完整列表：未出现者不能推断为删除，
            # 只有响应内的 404/410 墓碑会删除条目；令牌与变化在同一事务提交。
            if multistatus.sync_token:
                self.store.mark_collection_listed(collection['identity'], generation_id,
                                                   multistatus.sync_token)
            self.store.database.commit()
        self._drain_fetches(generation_id, summary)
        pending = self.store.pending_fetches(generation_id)
        if pending:
            summary['errors'].append('%d entries still need re-fetch' % len(pending))
        return self._finish_incremental(generation_id, summary)

    def _finish_incremental(self, generation_id, summary):
        if summary['errors']:
            summary['activated'] = False
        else:
            summary['activated'] = True
        self.store.database.commit()
        return summary

    def _trigger_rescan(self, generation_id, summary):
        self.store.add_event(generation_id, 'token_invalid_rescan')
        self.store.database.commit()
        summary['rescan'] = True
        rescan = self.full_scan()
        summary['rescan_summary'] = rescan
        summary['activated'] = rescan['activated']
        return summary

    @staticmethod
    def _has_valid_sync_token(payload):
        try:
            root = ET.fromstring(payload)
        except ET.ParseError:
            return False
        for tag in SYNC_HEADER:
            if root.find(tag) is not None:
                return True
        return root.find('.//{DAV:}valid-sync-token') is not None

    # -- 响应应用 -------------------------------------------------------
    def _apply_response(self, base_url, multistatus, generation_id, summary):
        """应用一份 207，返回本次实际见到的成员身份集合（用于剪枝）。"""
        summary['responses'] += 1
        seen = set()
        for response in multistatus.responses:
            if not response.href:
                continue
            try:
                identity = identity_url(self.root_url, base_url, response.href,
                                         response.is_collection)
            except ValueError as error:
                summary['errors'].append(str(error))
                self.store.add_event(generation_id, 'href_rejected', response.href, str(error))
                continue
            if response.tombstone:
                # response 层 404/410：唯一允许的明确删除。
                self.store.delete_entry(identity, generation_id)
                summary['tombstones'] += 1
                continue
            seen.add(identity)
            parent = self._parent_identity(identity, response.is_collection)
            self.store.upsert_entry(identity, generation_id,
                                    'collection' if response.is_collection else 'file',
                                    parent, response.properties.get('getetag'))
            self._apply_properties(identity, generation_id, response)
            if response.is_collection:
                self.store.upsert_collection(identity, generation_id, parent)
            else:
                self._schedule_file(identity, generation_id, response, summary)
        return seen

    def _apply_properties(self, identity, generation_id, response):
        for name in KNOWN_PROPERTIES:
            status = response.prop_status.get(name)
            if status == 200 and name in response.properties:
                self.store.trust_property(identity, generation_id, name, response.properties[name])
            elif status in (403, 404, 500):
                # 未知/不可信：保留上次已知值，只登记可信度。
                self.store.mark_property_unknown(identity, generation_id, name)

    def _schedule_file(self, identity, generation_id, response, summary):
        etag = response.properties.get('getetag') if response.trusted('getetag') else None
        entry = self.store.get_entry(identity, generation_id)
        if entry and entry['etag'] == etag and entry['digest']:
            # 已校验的同 ETag 正文：不重复下载、不重复安装。
            return
        if not etag:
            self.store.enqueue_fetch(identity, generation_id, etag or '', 'etag-unknown')
            return
        known = self.store.file_by_etag(etag)
        if known is not None:
            # 移动后的新 href 或重复条目：ETag 已绑定正文，直接复用。
            self.store.mark_verified(identity, generation_id, etag, known['digest'])
            summary['reused'] += 1
            return
        self.store.enqueue_fetch(identity, generation_id, etag, 'new-etag')

    # -- 条件下载与竞态核对 ---------------------------------------------
    def _drain_fetches(self, generation_id, summary, max_work=64):
        """处理待取队列直到清空、无 ETag 可绑或达到工作量上限。

        412 核对会删旧 ETag 队列项、按重列结果重新入队，因此按
        (身份, ETag) 去重的有界循环处理；失败项保留在队列中，代际
        不会在有待取项时切换，下次 run_once 继续。
        """
        handled = set()
        while len(handled) < max_work:
            item = self.store.database.execute(
                'SELECT * FROM fetch_queue WHERE generation_id = ? ORDER BY enqueued_at LIMIT 1',
                (generation_id,)).fetchone()
            if item is None:
                return
            key = (item['entry_identity'], item['etag'])
            if key in handled:
                # 同一 (身份, ETag) 已试过且未取得进展：留给下次运行。
                return
            handled.add(key)
            identity = item['entry_identity']
            etag = item['etag']
            if not etag:
                # ETag 未知无法条件下载：等待下一份列表给出可信 ETag。
                return
            self.store.bump_fetch_attempt(identity, generation_id)
            status, headers, payload = self.client.get(identity, etag)
            if status == 412:
                # 列表与下载间改版：重新 PROPFIND 核对，不挂错版本。
                self._refresh_after_precondition(identity, generation_id, etag, summary)
                self.store.database.commit()
                continue
            if status != 200:
                summary['errors'].append('GET %s -> %s' % (identity, status))
                return
            bound = _etag_value(headers.get('ETag'))
            if bound != etag:
                summary['errors'].append(
                    'GET %s returned ETag %s without precondition failure' % (identity, bound))
                return
            digest = self.store.store_blob(etag, payload)
            self.store.mark_verified(identity, generation_id, etag, digest)
            summary['fetches'] += 1
            self.store.database.commit()

    def _refresh_after_precondition(self, identity, generation_id, stale_etag, summary):
        parent = self.store.get_entry(identity, generation_id)
        collection = parent['parent_identity'] if parent else self.root_url
        status, _, payload = self.client.propfind(collection)
        summary['refreshes'] += 1
        if status != 207:
            summary['errors'].append('refresh PROPFIND %s -> %s' % (collection, status))
            self.store.enqueue_fetch(identity, generation_id, stale_etag, 'refresh-failed')
            return
        multistatus = parse_multistatus(payload)
        self._apply_response(collection, multistatus, generation_id, summary)
        self.store.mark_collection_listed(collection, generation_id, multistatus.sync_token)
        refreshed = self.store.get_entry(identity, generation_id)
        # 412 核对完成：旧 ETag 的队列项作废，按重列结果重新排队。
        self.store.database.execute(
            'DELETE FROM fetch_queue WHERE entry_identity = ? AND generation_id = ?',
            (identity, generation_id))
        if refreshed is None or refreshed['deleted']:
            return
        if refreshed['etag'] and refreshed['digest']:
            return
        if refreshed['etag'] and refreshed['etag'] != stale_etag:
            known = self.store.file_by_etag(refreshed['etag'])
            if known is not None:
                self.store.mark_verified(identity, generation_id, refreshed['etag'], known['digest'])
            else:
                self.store.enqueue_fetch(identity, generation_id, refreshed['etag'], 'post-412-etag')
        elif refreshed['etag']:
            self.store.enqueue_fetch(identity, generation_id, refreshed['etag'], 'post-412-retry')
        else:
            self.store.enqueue_fetch(identity, generation_id, '', 'etag-unknown')

    # -- 本地样册树 -----------------------------------------------------
    def materialize_tree(self):
        """按当前 active 代际在 tree/ 原子重建可读样册副本。

        先在同级临时目录构建完整树，再整体替换 tree/，避免半成品暴露给
        运营；代际切换前的旧树随旧代际保留在 blob 中，可随时重建。
        """
        active = self.store.active_generation()
        if active is None:
            return
        generation_id = active['id']
        staging = Path(tempfile.mkdtemp(prefix='tree-', dir=self.store.root))
        try:
            for entry in self.store.live_entries(generation_id):
                segments = relative_segments(self.root_url, entry['identity'])
                if not segments:
                    continue
                target = self._safe_tree_path(staging, segments)
                if entry['kind'] == 'collection':
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if entry['digest']:
                    source = self.store.blob_path(entry['digest'])
                    if source.exists():
                        os_link_or_copy(source, target)
            (staging / '.generation').write_text(str(generation_id))
            backup = None
            if self.store.tree.exists():
                backup = Path(tempfile.mkdtemp(prefix='tree-old-', dir=self.store.root))
                shutil.move(str(self.store.tree), str(backup / 'tree'))
            shutil.move(str(staging), str(self.store.tree))
            if backup is not None:
                shutil.rmtree(backup, ignore_errors=True)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _safe_tree_path(self, base, segments):
        safe = [segment.replace('/', '_') for segment in segments]
        target = base.joinpath(*safe)
        resolved = target.resolve()
        if base.resolve() not in resolved.parents and resolved != base.resolve():
            raise ValueError('materialized path escapes the state directory')
        return target

    def _parent_identity(self, identity, is_collection):
        if is_collection:
            if identity == self.root_url:
                return self.root_url
            base = identity.rstrip('/')
            slash = base.rfind('/')
            return base[:slash + 1]
        slash = identity.rfind('/')
        return identity[:slash + 1]


def _etag_value(value):
    if not value:
        return None
    return value.split(',', 1)[0].strip()


def os_link_or_copy(source, target):
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)
