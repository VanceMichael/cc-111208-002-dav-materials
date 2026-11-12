# mirror 镜像流程

`materialdav mirror` 把合作机构授权的 WebDAV 集合镜像为本地可读样册副本，
解决“只列一次目录、把 207 局部错误误判为删除”的问题。本文说明支持的
协议子集、状态模型与恢复语义；**这不是完整 WebDAV 客户端**。

## 支持的协议子集

仅实现 docs/source-contract.md 固定的范围：

| 能力 | 支持情况 |
| --- | --- |
| `PROPFIND`（固定 `Depth: 1`，逐集合遍历） | 是 |
| `REPORT`（`sync-collection`，`Depth: 1`，不透明 sync-token） | 是 |
| 条件 `GET`（列表 ETag 作为 `If-Match`） | 是 |
| 明确的 collection 层级（`DAV:resourcetype`） | 是 |
| 207 中 response 层与 propstat 层各自的状态码 | 是，分别处理 |
| 锁（LOCK/UNLOCK） | **不支持** |
| 写操作（PUT/POST/MKCOL/DELETE/PROPPATCH/MOVE/COPY） | **不支持** |
| 重定向跟随、代理、非 loopback origin、URL 内嵌凭据 | **拒绝** |
| 分页、配额、ACL、版本树等完整 RFC 4918/6578 能力 | **不支持** |

## 两类状态码的不同后果

- **response 层 404/410** 是资源墓碑：仅这种条目可以删除本地条目
  （增量路径）或在完整遍历后配合剪枝删除（全量路径）。
- **propstat 层 403/404/500** 只表示该属性未知：在 `properties` 表登记
  `trusted=0`，不会写入空值，也不会覆盖上一代际或上一轮的可信值
  （`trusted=1`，仅来自 propstat 200）。
- 一次列举不完整（PROPFIND 非 207、子集合没读完、待取文件未校验完）
  时，不允许用它证明任何文件消失。

## 稳定身份

身份是规范化后的绝对 URL（`materialdav/identity.py`）：

- 相对 href 以**实际响应基地址**（发出该 PROPFIND/REPORT 的集合 URL）
  用 `urljoin` 解析；
- 路径按段做 Unicode NFC 规范化后重新百分号编码，`%2F` 属于段内容，
  不会被当作路径分隔符（落盘时段内 `/` 替换为 `_`）；
- 集合身份以 `/` 结尾，资源不以 `/` 结尾；
- 与配置根不同 origin、不同 scheme/端口、路径逃出根目录、带查询串或
  fragment 的地址一律拒绝，不发起请求；
- 服务器重复返回的集合自身与重复条目按身份 upsert，不产生重复数据。

## ETag 绑定与下载竞态

1. 只有 propstat 200 的 `getetag` 才是可信 ETag。
2. GET 必须带 `If-Match: <列表 ETag>`；正文按 ETag 内容寻址存入
   `blobs/`，`files` 表记录 ETag→SHA256 绑定。
3. 收到 **412** 说明列表与下载之间资源改版：立即对父集合重发
   PROPFIND 核对，按新 ETag 重新入队；旧 ETag 名下绝不安装新正文。
4. 200 响应的 `ETag` 与请求 ETag 不一致同样视为错误，保留待取状态。
5. 移动后的新 href 若带回相同 ETag，直接复用已绑定正文，不重复下载；
   重复 REPORT 重放同理。

## 代际与令牌失效

每次初始同步或令牌失效重扫都会创建独立**代际**（`generations` 表）：

- 新代际处于 `scanning` 时，旧代际保持 `active`，`tree/` 继续可读；
- 必须发现的所有集合都 `listed=1`、待取队列清空、无错误，才切换为
  `active`，旧代际置 `superseded`（数据保留可读，不删除）；
- 增量 REPORT 对某集合返回 403 且正文含 `DAV:valid-sync-token` 时，
  立即开启新代际完整重扫当前集合树，完成后再切换；
- 进程在 `scanning` 期间退出，下次 `mirror` 调用恢复同一代际，
  已 `listed=1` 的集合与已校验文件不重复劳动。

## SQLite 查询方式

数据库位于 `<state>/mirror.sqlite3`，`python -m materialdav mirror-status --state <state>`
输出下列查询的 JSON 结果，也可直接用 sqlite3 执行。

目录身份（集合、父集合、sync-token、列举完成情况）：

```sql
SELECT c.generation_id, g.state AS generation_state, c.identity,
       c.parent_identity, c.sync_token, c.listed, c.listed_at
FROM collections c JOIN generations g ON g.id = c.generation_id
ORDER BY c.generation_id, c.discovered_at;
```

属性可信度（`trusted=1` 为 propstat 200 的已知值，`0` 为 403/500 登记的未知项）：

```sql
SELECT generation_id, entry_identity, name, value, trusted
FROM properties
WHERE generation_id = (SELECT MAX(id) FROM generations)
ORDER BY entry_identity, name;
```

待重取项（未完成 ETag 校验的文件、原因与尝试次数）：

```sql
SELECT generation_id, entry_identity, etag, reason, attempts, enqueued_at
FROM fetch_queue ORDER BY generation_id, enqueued_at;
```

其他表：`entries`（文件/集合条目与删除标记、已校验 digest）、
`files`（ETag→SHA256）、`events`（墓碑、剪枝、重扫等审计事件）。

## 本地布局与命令

```
<state>/
  mirror.sqlite3   # 代际、身份、属性可信度、待取队列
  blobs/<sha256>   # 按 ETag 绑定的内容寻址正文（硬链接进 tree）
  tree/            # 当前 active 代际的可读样册树，原子替换重建
```

```sh
python -m materialdav mirror http://127.0.0.1:8765/dav/ --state ./var-mirror
python -m materialdav mirror-status --state ./var-mirror
```

`mirror` 每次调用执行一个同步过程并打印 JSON 摘要；未完成时退出码仍为
0 但 `activated=false`，保留现场等待下次调用，网络/协议硬错误返回 2。
原有 `inspect` 与 `capture` 命令的行为不变。
