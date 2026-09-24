# 运行与维护

## 单进程与分离模式

一体启动：`uv run --env-file .env image-verifier serve`。

分离模式，在两个PowerShell窗口分别执行：

```powershell
uv run --env-file .env image-verifier serve --api-only
```

```powershell
uv run --env-file .env image-verifier worker
```

两个进程必须使用同一绝对IV_DB_PATH及相同模型/预算配置。仅支持同一主机的本地文件系统，不能将SQLite放入SMB/NFS共享目录。API-only没有匹配工作进程时ready检查返回503，但保留手动排队能力，受队列上限保护。

正常退出等待当前调用按原deadline结束；强制退出后queued任务保留，running任务在租约到期后标记failed/execution_unknown。人工决定是否重新提交这些任务；重新提交需新幂等键，并接受原上游可能已经执行的事实。

## 后端暂停与恢复

设置独立IV_ADMIN_KEY后使用`PUT /admin/resource`，请求体为：

```json
{"state":"paused","reason":"维护窗口"}
```

恢复使用`state=ready`并填写诊断理由。恢复接口表示管理员已经确认可以重启调度，不会自动证明认证有效，也不会清除服务端限流等待时间。请勿将密钥写入reason，管理动作会写入audit表。

## 修改模型与预算

同一预算域的配置不一致会阻止启动，防止不同进程使用不同额度。修改前先暂停接入、完成或取消现有queued/running任务，停止所有进程；修改.env后执行：

```powershell
uv run --env-file .env image-verifier reconfigure
```

再以相同配置启动全部进程。此命令保留历史attempt与限流等待，不重置已有消耗。数据库尚未建立时先正常启动服务创建初始结构。不同版本数据库目前需要显式迁移；不要删除数据库来解决生产配置冲突。

## 备份与恢复

```powershell
uv run --env-file .env image-verifier backup '.\data\backups\snapshot-01.sqlite3'
```

使用SQLite备份API生成一致副本，包含输入图片、任务、结果和审计记录。目标必须是尚不存在的新文件，不会覆盖已有备份。不能只复制运行中数据库主文件而忽略WAL。

恢复时停止全部进程，配置IV_DB_PATH指向备份副本的工作拷贝，以匹配策略启动。备份时仍在running的任务会按租约过期规则处理。保留原数据库直到验证恢复成功，避免覆盖唯一副本。

恢复到过去的快照也会恢复过去的配额历史。重新连接真实后端前，必须根据最新上游消耗重新协调或等待完整配额窗口结束，不能把旧快照预算当作当前可用额度。

## 数据保留

默认不会自动删除输入与结果。请在试点开始时设置清理计划并监控数据库大小。先预览：

```powershell
uv run --env-file .env image-verifier prune --older-than-days 30
```

确认后执行：

```powershell
uv run --env-file .env image-verifier prune --older-than-days 30 --apply
```

只清理整批全部终态且已超过保留期的批次；每次最多100批。存在queued/running任务的批次不会被删除。最短保留1天以保留小时窗口计账。清理会同时移除相关尝试、无任务引用的图片、已过期缓存和陈旧心跳；audit管理记录暂不自动清理。

清理后旧批次ID返回404，旧幂等键不再保留。数据库文件空间可能不会立即缩小；文件压缩应另行安排停机窗口与备份。不要在高峰进行大量清理。

## 监控建议

- `/health/live`监控进程；`/health/ready`监控本地调度依赖和工作进程心跳。
- `/v1/metrics`关注queued数量、oldest_queued_at、failed/expired、attempts与cache_hits。
- 定期检查任务attempt_history中的execution_unknown与认证错误。
- 监控磁盘剩余空间和SQLite锁等待；达到业务峰值前做真实图片负载测试。

本版提供JSON指标，没有Prometheus直方图或自动通知。生产告警、服务管理器自动重启、TLS终止、密钥托管和跨主机容灾需要在部署阶段补齐。
