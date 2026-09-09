# Token 用量记录

每次收到 `thread/tokenUsage/updated`，立即将 `token_usage.last` 的六个增量字段作为一行写入 SQLite 并提交。不会保存或累加 `token_usage.total`。`total_tokens` 表示当次增量的总 tokens。

原表追加 `runId`，同一次任务的明细共享网关提供的 `run_id`。直接调用未提供该参数时，为用量记录生成独立 UUID。首次写入自动追加旧表缺少的字段，旧行保留且 `runId` 为空，无法从旧行恢复此前漏记的增量。

`timeStamp` 是收到该次用量时的本地时间。`model` 仍来自任务配置。`taskDuration_ms` 在任务运行中为 0（尚未取得总耗时），收到 `turn/completed` 后按 SDK 提供的耗时回填同一任务的全部行。断流或取消时可能仍为 0，不能解释为实际耗时为零。汇总任务耗时用 `MAX`，不要用 `SUM`。

```sql
SELECT runId, userId, threadId,
       SUM(input_tokens) AS input_tokens,
       SUM(output_tokens) AS output_tokens,
       SUM(total_tokens) AS total_tokens,
       MAX(taskDuration_ms) AS taskDuration_ms
FROM token_usage
WHERE runId <> ''
GROUP BY runId, userId, threadId;
```

表名自定义时，需替换示例中的 `token_usage`。缓存、推理字段保留为细分指标，不再次加进 `total_tokens`。

任务退出不再新增末次记录，也不在未收到用量时补写零用量行。每个收到的事件写一次；本次没有增加上游重复事件去重。写入失败会记录异常，不自动重试，以免提交成功但 CSV 失败时重复计数。SQLite 提交后继续沿用现有全量 CSV 导出；导出失败不会撤销已提交的记录。正常取消保护正在进行的单次写入，但进程被强制终止或存储故障时无法保证尚未提交的数据持久化。
