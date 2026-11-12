# materialdav

设计机构样册共享集合的 WebDAV 原文查看与本地镜像工具。

## 本地运行

使用 Python 3.11 标准库，无 pip 依赖。所有来源与业务数据都是本地合成数据，不连接真实用户系统。

```sh
python -m scripts.materialize
python -m materialdav inspect fixtures/properties.xml
python -m compileall -q .
python -m unittest discover -s tests -v
python -m scripts.demo
```

检查命令输出单份来源的结构摘要。`capture URL --method GET --state var` 仅接受 loopback HTTP，保存原始响应为 SHA256 文件，并在 SQLite captures 表登记；可用 `--header 'Name:Value'` 传头、`--body FILE` 传正文，不跟随重定向。检查器不执行多页、业务去重或正式接收。

## mirror 镜像流程

`mirror` 让运营维护的本地样册副本跟上对方授权集合的变化，避免“只列一次目录、把 207 局部错误当成删除”：

```sh
python -m materialdav mirror http://127.0.0.1:8765/dav/ --state ./var-mirror
python -m materialdav mirror-status --state ./var-mirror
```

- `Depth:1` PROPFIND 逐集合完整遍历，`sync-collection` REPORT 走增量；
  response 层 404/410 才是删除，propstat 403/500 只登记属性不可信、不擦掉旧值。
- 身份按响应基地址解析，覆盖百分号编码、Unicode NFC、集合尾斜杠与跨集合同名项；
  根外地址与其他 origin 一律拒绝。
- GET 以列表 ETag 作 `If-Match`，412 时重列核对，正文按 ETag 内容寻址、只安装一次。
- 令牌失效开启独立重扫代际，旧代际在新代际完整遍历前保持可读；中途退出可恢复。
- 目录身份、属性可信度、待重取项的 SQLite 查询见 docs/mirror.md。

该流程只实现协议约定的子集（Depth:1 PROPFIND、sync-collection REPORT、条件 GET、
collection 层级），**不支持锁、写操作或完整 WebDAV**，详见 docs/mirror.md 与
docs/source-contract.md。合成场景在 fixtures/mirror/，端到端回归在 tests/test_mirror.py。

## 可复现容器

```sh
docker build -t materialdav:local .
docker run --rm --network none materialdav:local python -m unittest discover -s tests -v
docker run --rm --network none materialdav:local
```

镜像固定基础摘要。构建在正式运行前完成；运行和测试不安装包、不下载数据。演示命令实际执行检查器、本地来源及一个 mirror 业务闭环（初始镜像、增量墓碑/移动、令牌失效重扫），通常不超过 90 秒。

## 协议与来源

- docs/source-contract.md 给出支持子集、字段及失败语义。
- docs/acceptance.md 给出业务验收边界。
- fixtures/source.json 与 fixtures/mirror/*.json 是本地可重放 HTTP 路由；`python -m scripts.source_server --port 8765` 启动，`/__requests` 可检查实际请求。
- scripts/materialize.py 将版本化 base64 二进制夹具还原为原始字节，重复执行安全；二进制文件不是用户附件。
- journal.py 只记录 inspect/capture 的原始响应；mirror 自己的事务、代际与恢复状态在 mirror.sqlite3，设计见 docs/mirror.md。
