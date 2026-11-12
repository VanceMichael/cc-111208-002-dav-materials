# 样册共享目录协议

来源配置限定 origin 与 /dav/ 根。集合身份使用以斜杠结尾的绝对 href。PROPFIND 必须带 Depth:1，列出集合本身及直接成员；DAV:resourcetype 含 collection 的条目需要进一步遍历。REPORT 使用 sync-collection 和集合自己的 sync-token，令牌是不透明字符串。403 响应包含 DAV:valid-sync-token 时，该集合进入独立重扫代际。

DAV:multistatus 中 response 自身状态与各 propstat 状态不同。支持 getetag、getcontentlength、displayname、resourcetype，属性缺失或 403/500 表示未知，不是用空值擦掉旧值；只有 response 层的 404/410 明确删除资源。重复报告允许重放。GET 使用列表 ETag 发送 If-Match；412 时重新核对列表，不把新正文安装到旧 ETag 下。

相对 href 以实际响应地址解析，规范化点段及 Unicode 时不能把编码斜杠误当路径分隔符，禁止根外与其他 origin。目录不完整不能证明文件消失。令牌失效后保留旧代际可读，所有发现的子集合完成才切换。source.json 提供属性局部失败、删除、令牌失效及条件下载，支持扩充重复条目与竞态响应。范围不含锁、写操作或真实共享盘凭据。
