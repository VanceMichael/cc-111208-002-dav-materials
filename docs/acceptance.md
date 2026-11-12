# 接收流程核验

原文留存与单份检查（`inspect`/`capture`）由 tests/test_baseline.py 覆盖；
mirror 业务流程由 tests/test_mirror.py 做独立端到端回归，两者互不替代。

## 验收点与覆盖

- propstat 500/403 保留旧属性，response 404 才删除
  - `MessageTests.test_propstat_failure_does_not_emit_value`
  - `MirrorEndToEndTests.test_incremental_tombstone_move_and_property_trust`
  - `MirrorEndToEndTests.test_invalid_token_builds_separate_rescan_generation`
- ETag 列表/下载竞态返回 412，重列后按新 ETag 绑定，不装错版
  - `MirrorEndToEndTests.test_download_race_rebinds_after_412`（fixtures/mirror/race.json）
- token 失效（403 + valid-sync-token）建立独立重扫代际，子集合失败时
  旧代际保持可读、未完成不切换
  - `MirrorEndToEndTests.test_invalid_token_builds_separate_rescan_generation`
  - `MirrorEndToEndTests.test_subprocess_crash_resumes_same_generation`
- 移动后的新 href 复用同 ETag 正文，重复 REPORT 不重复安装
  - `MirrorEndToEndTests.test_repeated_report_does_not_reinstall_files`
- 百分号编码、Unicode、集合尾斜杠、跨集合同名、根外地址
  - `IdentityTests` 全部用例 + `test_initial_scan_materializes_authorized_set`

## 合成 DAV 服务约定

- 场景位于 fixtures/mirror/*.json，由 scripts/source_server.py 重放；
  路由按调用次序推进，重复请求停在最后一条，可在 /__requests 核对
  真实方法、请求头（含 If-Match、Depth）与正文。
- `delay_seconds` 仅用于崩溃测试：计数先推进再挂起，恢复请求不会
  卡在同一路由；客户端断开时服务端静默，不打印堆栈。
- 崩溃测试启动真实子进程并发送 SIGTERM，不是进程内模拟异常；
  断言 scanning 代际保留、tree 不暴露半成品，再用同一状态目录恢复。
- 每次演示使用新的临时状态目录；准备依赖与构建在演示前完成，
  演示总时长控制在 90 秒以内。
