# 统一记忆服务数据模型验证报告

> 状态：第一批已完成  
> 模型版本：V1.0  
> 验证范围：工程环境、PostgreSQL DDL、约束和索引

## 1. 环境和版本

| 组件 | 版本 | 状态 |
| --- | --- | --- |
| Python | 3.12.10 | 已确认 |
| Poetry | 2.4.1 | 已确认 |
| PostgreSQL | 15.17 | 已验证 |
| pgvector | 0.8.2 | 已验证 |
| Docker Compose | 2.32.4-desktop.1 | 已确认 |

## 2. 第一批验证项目

- [x] 独立 `memory-cmic-postgres` 容器运行在 Host 端口 5433；
- [x] 数据持久化到 `memory_cmic_pgdata`，容器重启后 migration 版本仍存在；
- [x] `memory_cmic` 数据库和用户可连接；
- [x] Alembic upgrade、downgrade、再次 upgrade 成功；
- [x] 七张运行期核心表和一张迁移辅助表存在；
- [x] 必要 CHECK、复合外键、唯一约束和索引存在；
- [x] 17 项数据库结构与约束测试通过；
- [x] SQLAlchemy metadata 与数据库无 migration 漂移；
- [x] `honcho-postgres` 未被修改或重启。

实际对象统计：

| 对象 | 数量 |
| --- | ---: |
| `public` 业务表 | 7 |
| `migration` 业务表 | 1 |
| 非主键索引 | 27 |
| 业务约束 | 54 |

执行结果：

```text
alembic upgrade head              通过
alembic downgrade base           通过
alembic upgrade head             通过
alembic check                    No new upgrade operations detected
pytest -q                        17 passed
ruff check .                     All checks passed
poetry check                     All set
```

## 3. 设计偏差

- 经确认，Python 包由实施方案示例中的 `memory_validation` 统一命名为
  `memory_cmic`。该调整仅影响工程命名，不改变冻结数据模型。

## 4. 未决问题

- “一个 evidence group 只能对应一个下游对象”按冻结方案由后续最小写入函数校验，
  不新增 evidence group 表；不属于本批 DDL 可强制约束。
- 业务域和项目域数组的去重、排序及非法值校验由后续写入层实现；本批数据库已验证空数组被拒绝。

## 5. 阶段结论

第一批工程与数据库基线验证通过，可以进入样例数据和最小领域函数实施。

本结论只覆盖数据库结构可行性，不代表数据模型 V1.0 整体 GO。完整 Go/No-Go 仍需完成
19 项核心行为用例、任务并发、检索、性能冒烟和 Honcho 迁移验证后给出。
