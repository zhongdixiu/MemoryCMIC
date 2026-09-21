from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from memory_cmic.models import MemoryItem, MemoryTask


def enqueue_task(session: Session, data: Mapping[str, Any]) -> MemoryTask:
    existing = session.scalars(
        select(MemoryTask).where(
            MemoryTask.tenant_id == data["tenant_id"],
            MemoryTask.idempotency_key == data["idempotency_key"],
        )
    ).one_or_none()
    if existing is not None:
        return existing

    task = MemoryTask(**dict(data))
    session.add(task)
    session.flush()
    return task


def claim_tasks(
    session: Session,
    *,
    tenant_id: str,
    worker_id: str,
    limit: int = 1,
    lease_seconds: int = 60,
) -> list[MemoryTask]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")

    claimed_ids = list(
        session.execute(
            text(
                """
                WITH candidates AS (
                    SELECT id
                    FROM memory_task
                    WHERE tenant_id = :tenant_id
                      AND (
                           (status = 'pending' AND available_at <= CURRENT_TIMESTAMP)
                        OR (status = 'processing'
                            AND locked_until < CURRENT_TIMESTAMP
                            AND attempt_count < max_attempts)
                      )
                    ORDER BY priority DESC, available_at ASC, created_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT :limit
                )
                UPDATE memory_task AS task
                SET status = 'processing',
                    worker_id = :worker_id,
                    locked_until = CURRENT_TIMESTAMP
                                   + make_interval(secs => :lease_seconds),
                    attempt_count = CASE
                        WHEN task.status = 'processing' THEN task.attempt_count + 1
                        ELSE task.attempt_count
                    END,
                    started_at = COALESCE(task.started_at, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP
                FROM candidates
                WHERE task.id = candidates.id
                RETURNING task.id
                """
            ),
            {
                "tenant_id": tenant_id,
                "worker_id": worker_id,
                "limit": limit,
                "lease_seconds": lease_seconds,
            },
        ).scalars()
    )
    if not claimed_ids:
        return []

    session.expire_all()
    tasks_by_id = {
        task.id: task
        for task in session.scalars(select(MemoryTask).where(MemoryTask.id.in_(claimed_ids)))
    }
    return sorted(
        (tasks_by_id[task_id] for task_id in claimed_ids),
        key=lambda task: (-task.priority, task.available_at, task.created_at),
    )


def renew_task_lease(
    session: Session,
    *,
    tenant_id: str,
    task_id: str,
    worker_id: str,
    lease_seconds: int,
) -> bool:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    result = session.execute(
        text(
            """
            UPDATE memory_task
            SET locked_until = GREATEST(locked_until, CURRENT_TIMESTAMP)
                               + make_interval(secs => :lease_seconds),
                updated_at = CURRENT_TIMESTAMP
            WHERE tenant_id = :tenant_id
              AND id = :task_id
              AND status = 'processing'
              AND worker_id = :worker_id
              AND locked_until > CURRENT_TIMESTAMP
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": task_id,
            "worker_id": worker_id,
            "lease_seconds": lease_seconds,
        },
    )
    session.expire_all()
    return result.rowcount == 1


def complete_task(
    session: Session,
    *,
    tenant_id: str,
    task_id: str,
    worker_id: str,
) -> bool:
    result = session.execute(
        text(
            """
            UPDATE memory_task
            SET status = 'succeeded',
                completed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE tenant_id = :tenant_id
              AND id = :task_id
              AND status = 'processing'
              AND worker_id = :worker_id
              AND locked_until > CURRENT_TIMESTAMP
            """
        ),
        {"tenant_id": tenant_id, "task_id": task_id, "worker_id": worker_id},
    )
    session.expire_all()
    return result.rowcount == 1


def fail_and_retry_task(
    session: Session,
    *,
    tenant_id: str,
    task_id: str,
    worker_id: str,
    error: str,
    base_backoff_seconds: int = 1,
) -> MemoryTask | None:
    if base_backoff_seconds <= 0:
        raise ValueError("base_backoff_seconds must be positive")
    returned_id = session.execute(
        text(
            """
            UPDATE memory_task
            SET attempt_count = attempt_count + 1,
                status = CASE
                    WHEN attempt_count + 1 >= max_attempts THEN 'failed'
                    ELSE 'pending'
                END,
                available_at = CASE
                    WHEN attempt_count + 1 >= max_attempts THEN available_at
                    ELSE CURRENT_TIMESTAMP + make_interval(
                        secs => :base_backoff_seconds * power(2, attempt_count)
                    )
                END,
                worker_id = CASE
                    WHEN attempt_count + 1 >= max_attempts THEN worker_id
                    ELSE NULL
                END,
                locked_until = CASE
                    WHEN attempt_count + 1 >= max_attempts THEN locked_until
                    ELSE NULL
                END,
                completed_at = CASE
                    WHEN attempt_count + 1 >= max_attempts THEN CURRENT_TIMESTAMP
                    ELSE NULL
                END,
                last_error = :error,
                updated_at = CURRENT_TIMESTAMP
            WHERE tenant_id = :tenant_id
              AND id = :task_id
              AND status = 'processing'
              AND worker_id = :worker_id
              AND locked_until > CURRENT_TIMESTAMP
            RETURNING id
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": task_id,
            "worker_id": worker_id,
            "error": error,
            "base_backoff_seconds": base_backoff_seconds,
        },
    ).scalar_one_or_none()
    if returned_id is None:
        return None
    session.expire_all()
    return session.get(MemoryTask, returned_id)


def apply_memory_content_if_current(
    session: Session,
    *,
    task_id: str,
    worker_id: str,
    new_content: str,
) -> bool:
    task = session.scalars(
        select(MemoryTask)
        .where(
            MemoryTask.id == task_id,
            MemoryTask.status == "processing",
            MemoryTask.worker_id == worker_id,
            MemoryTask.locked_until > func.current_timestamp(),
        )
        .with_for_update()
    ).one_or_none()
    if task is None or task.target_type != "memory" or task.input_version is None:
        return False

    result = session.execute(
        update(MemoryItem)
        .where(
            MemoryItem.tenant_id == task.tenant_id,
            MemoryItem.id == task.target_id,
            MemoryItem.version == task.input_version,
        )
        .values(
            content=new_content,
            version=MemoryItem.version + 1,
            updated_at=func.current_timestamp(),
        )
    )
    if result.rowcount == 0:
        task.status = "cancelled"
    else:
        task.status = "succeeded"
    task.completed_at = func.current_timestamp()
    task.updated_at = func.current_timestamp()
    session.flush()
    return result.rowcount == 1
