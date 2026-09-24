# ADD 接口运行说明

## 范围

首期提供标准消息接入、来源持久化、事实抽取任务、在线重复判定、证据补充和任务查询。
不包含框架适配器、webhook、记忆替代/冲突裁决和定时记忆整理。

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
