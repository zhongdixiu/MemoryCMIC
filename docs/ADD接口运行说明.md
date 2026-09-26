# ADD 接口运行说明

## 范围

当前提供标准消息接入、来源持久化、事实抽取、范围判定、重复补证据、明确替代、争议处理、可恢复切片和任务查询。
不包含框架适配器、webhook、项目范围写入、定时记忆整理或治理队列执行器。

## 适用范围与治理

`source_system` 仅标识来源，与业务域没有映射。抽取按信息适用场景输出标签，以 `communication`（短信、电话、即时消息等非邮件通信）、`email`（邮件读写、处理和管理）、`disk`（个人云盘备份、存储、管理和分享）为初始标准标签，并允许其他场景，如“方案编写”。初始同义表达归一化，新场景标签校验非空及长度，不因不在初始词表中丢弃。通用个人事实不限定业务；确实无法判断的业务限制不能写成通用事实。

项目字段仅预留，`project_domains` 暂不赋值，不解析或猜测项目 ID。有效项目记忆正常生产，正文及内部 `metadata.project_context` 保留项目限定；未知或重名项目的自足陈述也保留，不推广成一般偏好。内部上下文是文本条件，不提供唯一项目身份或授权。

判重和治理针对同用户及等价的实际适用条件。不同项目上下文保守分开，已知不同初始场景不误合并；其他场景标签结合语义比较，不只按字符串判断。明确纠正或状态更新建立新事实和 `supersedes_id`，同步失效旧事实及受影响派生；无法裁决的矛盾双方进入同一争议组并暂停使用。争议只有新的明确用户澄清才能解决。历史事实保留时间区间，迟到来源不能覆盖更新事实，也不能作为另一个无期限的当前状态。

内部 `search_memories` 保留精确场景路径；没有任务上下文时，不返回尚未绑定项目 ID 的项目限定记忆。需要语义适用性判断时同时传入 `query` 和 `applicability_model`，在服务端授权主体及生命周期过滤后的候选中，根据正文及项目上下文筛选；场景标签不充当授权条件。语义路径仅判断有限候选窗口，向量排序优先，未接入公开 Search 接口。

公共回执继续只返回 `event=ADD`。替代后的旧值、争议事实和已过期历史事实不作为本批可用 ADD 结果返回；补证据或只形成争议时 `succeeded` 的结果可以为空，不能据此承诺纠正成功。

## 长输入与重试

默认输入预算为 12000，输出预留 2048；当前用 UTF-8 字节数保守估算输入用量，包括提示词、JSON 和消息开销，不声称使用 Qwen 的精确 tokenizer。小消息按预算合片，长消息按段落或字符边界分片，证据保留原文字符位置。历史先裁剪，目标不会静默截断。预算配置见 `.env.example`。

任务首次处理保存来源/历史输入、切片计划、预算和 prompt 版本。每片的记忆、证据、审计、结果与进度在短事务内提交；模型调用不持有事务。重试从未提交片继续，消息全部片完成才设置来源处理版本。输出截断最多连续拆分两轮，模型调用有默认 20 次的每任务总预算；每个抽取、治理请求及 embedding 批次都计数，失败调用也不退还额度。

服务内部重试期间仍返回 `pending`，不暴露已提交的中间结果。最终失败时，有任何已提交记忆或证据变更则返回 `partial`，否则 `failed`；只补证据的 `partial` 允许结果为空。公共接口未增加重试入口，终态不通过普通 Add 重放来强制重处理。

同会话前序 `partial/failed/cancelled` 会阻断后续任务。向量清理及派生重建任务仍待 P03/P04 消费；同步失效保证旧值立即退出使用。

## 配置与启动

复制 `.env.example` 中新增的鉴权及模型配置到本地 `.env`，不要提交真实 token 或 API Key。

```bash
set -a
source .env
set +a
poetry install
poetry run alembic upgrade head
poetry run memory-cmic-api
```

另开进程启动 Worker：

```bash
set -a
source .env
set +a
poetry run memory-cmic-worker
```

## 提交消息

```bash
curl -X POST http://localhost:8000/api/v1/memories:add \
  -H 'Authorization: Bearer <configured-token>' \
  -H 'Idempotency-Key: example-request-1' \
  -H 'Content-Type: application/json' \
  -d '{
    "source_system": "email_agent",
    "user_id": "user_1001",
    "session_id": "session_101",
    "messages": [{
      "message_id": "msg_101",
      "role": "user",
      "content": "以后周报都使用项目符号。",
      "occurred_at": "2026-09-24T09:00:00+08:00"
    }]
  }'
```

默认返回 `202` 和 pending 任务。使用相同 `Idempotency-Key` 重试同一请求会返回原 task_id。

## 查询任务

```bash
curl http://localhost:8000/api/v1/tasks/<task_id> \
  -H 'Authorization: Bearer <configured-token>'
```

重复事实不会生成新的结果项；Worker 会把新来源补充为已有记忆的证据，任务返回
`succeeded` 和空 `results`。
