from __future__ import annotations

import hashlib
import logging
import signal
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from memory_cmic.db import create_database_engine
from memory_cmic.models import (
    MemoryAuditLog,
    MemoryEmbedding,
    MemoryEvidence,
    MemoryItem,
    MemoryTask,
    MemoryTaskSource,
    SourceRecord,
)
from memory_cmic.providers import (
    Embedder,
    ExtractedFact,
    FactModel,
    ProviderError,
    QwenFactModel,
    SiliconFlowEmbedder,
)
from memory_cmic.settings import Settings
from memory_cmic.task_queue import claim_tasks, fail_and_retry_task, finish_task

logger = logging.getLogger(__name__)


class WorkerInputError(RuntimeError):
    pass


class LeaseLostError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskInput:
    task_id: str
    tenant_id: str
    caller_agent_id: str
    user_id: str
    source_system: str
    session_id: str
    batch_seq: int
    targets: list[dict[str, Any]]
    history: list[dict[str, Any]]
    source_ids: dict[str, str]


@dataclass(frozen=True)
class FactDecision:
    fact: ExtractedFact
    embedding: list[float]
    duplicate_memory_id: str | None


def _normalized_fact(value: str) -> str:
    return " ".join(value.casefold().split())


def _fact_hash(value: str) -> str:
    return hashlib.sha256(_normalized_fact(value).encode()).hexdigest()


def _memory_content_hash(summary: str, content: str) -> str:
    return hashlib.sha256(f"{summary}\n{content}".encode()).hexdigest()


def _source_message(source: SourceRecord) -> dict[str, Any]:
    metadata = source.metadata_json or {}
    return {
        "message_id": source.message_id,
        "role": metadata.get("role"),
        "content": source.raw_content,
        "occurred_at": source.occurred_at.isoformat(),
        "author_id": source.author_id,
        "tool_status": metadata.get("tool_status"),
    }


def _load_task_input(session: Session, task: MemoryTask) -> TaskInput:
    if not all(
        [
            task.caller_agent_id,
            task.user_id,
            task.source_system,
            task.session_id,
            task.batch_seq is not None,
        ]
    ):
        raise WorkerInputError("fact extraction task is missing conversation identity")

    associations = session.execute(
        select(MemoryTaskSource, SourceRecord)
        .join(
            SourceRecord,
            (SourceRecord.tenant_id == MemoryTaskSource.tenant_id)
            & (SourceRecord.id == MemoryTaskSource.source_id),
        )
        .where(
            MemoryTaskSource.tenant_id == task.tenant_id,
            MemoryTaskSource.task_id == task.id,
        )
        .order_by(MemoryTaskSource.kind, MemoryTaskSource.position)
    ).all()
    target_rows = [row for row in associations if row[0].kind == "target"]
    history_rows = [row for row in associations if row[0].kind == "history"]
    if not target_rows:
        raise WorkerInputError("fact extraction task has no target sources")

    targets: list[dict[str, Any]] = []
    source_ids: dict[str, str] = {}
    for association, source in target_rows:
        if source.status != "active" or source.version != association.source_version:
            raise WorkerInputError("target source is unavailable")
        if source.processed_version == source.version:
            association.status = "skipped"
            continue
        if source.message_id is None:
            raise WorkerInputError("target source has no message_id")
        targets.append(_source_message(source))
        source_ids[source.message_id] = source.id

    explicit_history = [
        _source_message(source)
        for association, source in history_rows
        if source.status == "active" and source.version == association.source_version
    ]
    previous_rows = session.execute(
        select(SourceRecord)
        .join(
            MemoryTaskSource,
            (MemoryTaskSource.tenant_id == SourceRecord.tenant_id)
            & (MemoryTaskSource.source_id == SourceRecord.id),
        )
        .join(
            MemoryTask,
            (MemoryTask.tenant_id == MemoryTaskSource.tenant_id)
            & (MemoryTask.id == MemoryTaskSource.task_id),
        )
        .where(
            MemoryTask.tenant_id == task.tenant_id,
            MemoryTask.source_system == task.source_system,
            MemoryTask.user_id == task.user_id,
            MemoryTask.session_id == task.session_id,
            MemoryTask.batch_seq < task.batch_seq,
            MemoryTask.status.in_(["succeeded", "partial"]),
            MemoryTaskSource.kind == "target",
            SourceRecord.status == "active",
        )
        .order_by(MemoryTask.batch_seq.desc(), MemoryTaskSource.position.desc())
        .limit(30)
    ).scalars().all()
    previous_history = [_source_message(source) for source in reversed(previous_rows)]
    seen = {message.get("message_id") for message in previous_history}
    history = [*previous_history]
    history.extend(
        message for message in explicit_history if message.get("message_id") not in seen
    )
    return TaskInput(
        task_id=task.id,
        tenant_id=task.tenant_id,
        caller_agent_id=task.caller_agent_id,
        user_id=task.user_id,
        source_system=task.source_system,
        session_id=task.session_id,
        batch_seq=task.batch_seq,
        targets=targets,
        history=history,
        source_ids=source_ids,
    )


def _similar_candidates(
    session: Session,
    *,
    task_input: TaskInput,
    model_id: str,
    embedding: list[float],
    limit: int = 5,
) -> list[tuple[MemoryItem, float]]:
    distance = MemoryEmbedding.embedding.cosine_distance(embedding).label("distance")
    current_hash = func.encode(
        func.sha256(
            func.convert_to(func.concat(MemoryItem.summary, "\n", MemoryItem.content), "UTF8")
        ),
        "hex",
    )
    rows = session.execute(
        select(MemoryItem, distance)
        .join(
            MemoryEmbedding,
            (MemoryEmbedding.tenant_id == MemoryItem.tenant_id)
            & (MemoryEmbedding.memory_id == MemoryItem.id),
        )
        .where(
            MemoryItem.tenant_id == task_input.tenant_id,
            MemoryItem.subject_type == "user",
            MemoryItem.subject_id == task_input.user_id,
            MemoryItem.cognitive_type == "fact",
            MemoryItem.status == "active",
            or_(MemoryItem.expired_at.is_(None), MemoryItem.expired_at > datetime.now(UTC)),
            MemoryEmbedding.model_id == model_id,
            MemoryEmbedding.status == "active",
            MemoryEmbedding.content_hash == current_hash,
        )
        .order_by(distance)
        .limit(limit)
    ).all()
    return [(memory, 1.0 - float(value)) for memory, value in rows]


def _add_evidence(
    session: Session,
    *,
    task_input: TaskInput,
    fact: ExtractedFact,
    memory_id: str,
) -> None:
    group_id = f"evg_{uuid4().hex}"
    for message_id in dict.fromkeys(fact.message_ids):
        source_id = task_input.source_ids[message_id]
        source = session.get(SourceRecord, source_id)
        session.add(
            MemoryEvidence(
                id=f"evi_{uuid4().hex}",
                tenant_id=task_input.tenant_id,
                relationship_type="supports",
                evidence_group_id=group_id,
                upstream_source_id=source_id,
                downstream_memory_id=memory_id,
                upstream_version=source.version,
                evidence_snippet=source.raw_content,
                evidence_locator={"message_id": message_id},
            )
        )


def _plan_decisions(
    session_factory: sessionmaker[Session],
    *,
    task_input: TaskInput,
    facts: list[ExtractedFact],
    embeddings: list[list[float]],
    fact_model: FactModel,
    embedder: Embedder,
    duplicate_threshold: float,
) -> list[FactDecision]:
    decisions: list[FactDecision] = []
    for fact, embedding in zip(facts, embeddings, strict=True):
        with session_factory() as session:
            duplicate_id = session.scalars(
                select(MemoryItem.id).where(
                    MemoryItem.tenant_id == task_input.tenant_id,
                    MemoryItem.subject_type == "user",
                    MemoryItem.subject_id == task_input.user_id,
                    MemoryItem.cognitive_type == "fact",
                    MemoryItem.status == "active",
                    MemoryItem.semantic_hash == _fact_hash(fact.memory),
                )
            ).first()
            candidates = (
                []
                if duplicate_id is not None
                else _similar_candidates(
                    session,
                    task_input=task_input,
                    model_id=embedder.model_id,
                    embedding=embedding,
                )
            )
            routed = [
                {"id": memory.id, "memory": memory.content, "similarity": round(score, 6)}
                for memory, score in candidates
                if score >= duplicate_threshold
            ]
        if duplicate_id is None and routed:
            duplicate_id = fact_model.find_duplicate(fact=fact.memory, candidates=routed)
        decisions.append(
            FactDecision(
                fact=fact,
                embedding=embedding,
                duplicate_memory_id=duplicate_id,
            )
        )
    return decisions


def _commit_results(
    session: Session,
    *,
    task: MemoryTask,
    task_input: TaskInput,
    decisions: list[FactDecision],
    embedder: Embedder,
) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for decision in decisions:
        fact = decision.fact
        embedding = decision.embedding
        semantic_hash = _fact_hash(fact.memory)
        session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(semantic_hash, 0)))
        )
        duplicate = None
        if decision.duplicate_memory_id is not None:
            duplicate = session.scalars(
                select(MemoryItem).where(
                    MemoryItem.tenant_id == task_input.tenant_id,
                    MemoryItem.id == decision.duplicate_memory_id,
                    MemoryItem.status == "active",
                )
            ).one_or_none()
        if duplicate is None:
            duplicate = session.scalars(
                select(MemoryItem).where(
                    MemoryItem.tenant_id == task_input.tenant_id,
                    MemoryItem.subject_type == "user",
                    MemoryItem.subject_id == task_input.user_id,
                    MemoryItem.cognitive_type == "fact",
                    MemoryItem.status == "active",
                    MemoryItem.semantic_hash == semantic_hash,
                )
            ).first()

        if duplicate is not None:
            _add_evidence(
                session,
                task_input=task_input,
                fact=fact,
                memory_id=duplicate.id,
            )
            continue

        memory_id = f"mem_{uuid4().hex}"
        summary = fact.memory[:512]
        memory = MemoryItem(
            id=memory_id,
            tenant_id=task_input.tenant_id,
            subject_type="user",
            subject_id=task_input.user_id,
            cognitive_type="fact",
            summary=summary,
            content=fact.memory,
            semantic_hash=semantic_hash,
            confidence=1,
            effective_at=max(
                source.occurred_at
                for source in session.scalars(
                    select(SourceRecord).where(
                        SourceRecord.id.in_(
                            [task_input.source_ids[message_id] for message_id in fact.message_ids]
                        )
                    )
                )
            ),
            created_by=task_input.caller_agent_id,
        )
        session.add(memory)
        session.flush()
        _add_evidence(
            session, task_input=task_input, fact=fact, memory_id=memory.id
        )
        session.add(
            MemoryEmbedding(
                tenant_id=task_input.tenant_id,
                memory_id=memory.id,
                model_id=embedder.model_id,
                content_hash=_memory_content_hash(summary, fact.memory),
                embedding=embedding,
            )
        )
        session.add(
            MemoryAuditLog(
                tenant_id=task_input.tenant_id,
                action="INSERT",
                target_type="memory",
                target_id=memory.id,
                operator_type="agent",
                operator_id=task_input.caller_agent_id,
                reason_code="FACT_EXTRACTED",
                correlation_id=task.correlation_id,
                state_after={"content": fact.memory, "status": "active"},
            )
        )
        results.append({"id": memory.id, "memory": fact.memory, "event": "ADD"})

    source_ids = list(task_input.source_ids.values())
    sources = session.scalars(
        select(SourceRecord).where(
            SourceRecord.tenant_id == task_input.tenant_id,
            SourceRecord.id.in_(source_ids),
        )
    ).all()
    for source in sources:
        source.processed_version = source.version
    for association in session.scalars(
        select(MemoryTaskSource).where(
            MemoryTaskSource.tenant_id == task_input.tenant_id,
            MemoryTaskSource.task_id == task_input.task_id,
            MemoryTaskSource.kind == "target",
        )
    ):
        association.status = "processed"
    return results


def process_task(
    session_factory: sessionmaker[Session],
    *,
    task_id: str,
    tenant_id: str,
    worker_id: str,
    lease_token: str,
    fact_model: FactModel,
    embedder: Embedder,
    duplicate_threshold: float,
) -> None:
    with session_factory.begin() as session:
        task = session.get(MemoryTask, task_id)
        if task is None or task.tenant_id != tenant_id:
            raise WorkerInputError("task does not exist")
        prior_failure = session.scalars(
            select(MemoryTask.id).where(
                MemoryTask.tenant_id == task.tenant_id,
                MemoryTask.source_system == task.source_system,
                MemoryTask.user_id == task.user_id,
                MemoryTask.session_id == task.session_id,
                MemoryTask.batch_seq < task.batch_seq,
                MemoryTask.status.in_(["failed", "partial", "cancelled"]),
            )
        ).first()
        if prior_failure is not None:
            if not finish_task(
                session,
                tenant_id=tenant_id,
                task_id=task_id,
                worker_id=worker_id,
                lease_token=lease_token,
                status="failed",
                results=[],
                error={
                    "code": "PRIOR_TASK_FAILED",
                    "message": "a prior task in this conversation did not complete",
                },
            ):
                raise LeaseLostError("task lease was lost")
            return
        task_input = _load_task_input(session, task)

    if not task_input.targets:
        facts: list[ExtractedFact] = []
        embeddings: list[list[float]] = []
    else:
        facts = fact_model.extract(targets=task_input.targets, history=task_input.history)
        embeddings = embedder.embed([fact.memory for fact in facts])
    decisions = _plan_decisions(
        session_factory,
        task_input=task_input,
        facts=facts,
        embeddings=embeddings,
        fact_model=fact_model,
        embedder=embedder,
        duplicate_threshold=duplicate_threshold,
    )

    with session_factory.begin() as session:
        task = session.get(MemoryTask, task_id)
        if task is None:
            raise WorkerInputError("task disappeared")
        results = _commit_results(
            session,
            task=task,
            task_input=task_input,
            decisions=decisions,
            embedder=embedder,
        )
        if not finish_task(
            session,
            tenant_id=tenant_id,
            task_id=task_id,
            worker_id=worker_id,
            lease_token=lease_token,
            status="succeeded",
            results=results,
        ):
            raise LeaseLostError("task lease was lost")


def run_once(
    session_factory: sessionmaker[Session],
    *,
    worker_id: str,
    fact_model: FactModel,
    embedder: Embedder,
    duplicate_threshold: float,
) -> bool:
    with session_factory() as session:
        tenant_ids = session.scalars(
            select(MemoryTask.tenant_id)
            .where(
                MemoryTask.task_type == "fact_extract",
                or_(
                    (MemoryTask.status == "pending")
                    & (MemoryTask.available_at <= func.current_timestamp()),
                    (MemoryTask.status == "processing")
                    & (MemoryTask.locked_until < func.current_timestamp()),
                ),
            )
            .distinct()
        ).all()
    for tenant_id in tenant_ids:
        with session_factory.begin() as session:
            claimed = claim_tasks(
                session,
                tenant_id=tenant_id,
                worker_id=worker_id,
                limit=1,
                lease_seconds=180,
            )
            task = claimed[0] if claimed else None
            if task is not None:
                task_id = task.id
                lease_token = task.lease_token
        if task is None:
            continue
        try:
            process_task(
                session_factory,
                task_id=task_id,
                tenant_id=tenant_id,
                worker_id=worker_id,
                lease_token=lease_token,
                fact_model=fact_model,
                embedder=embedder,
                duplicate_threshold=duplicate_threshold,
            )
        except WorkerInputError as exc:
            with session_factory.begin() as session:
                finish_task(
                    session,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    worker_id=worker_id,
                    lease_token=lease_token,
                    status="failed",
                    results=[],
                    error={"code": "INPUT_PROCESSING_FAILED", "message": str(exc)},
                )
        except ProviderError as exc:
            with session_factory.begin() as session:
                fail_and_retry_task(
                    session,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    worker_id=worker_id,
                    lease_token=lease_token,
                    error=str(exc),
                    error_code="EXTRACTION_FAILED",
                    base_backoff_seconds=2,
                )
        except LeaseLostError:
            logger.warning("task %s lost its lease before completion", task_id)
        except Exception as exc:
            logger.exception("unexpected task failure for %s", task_id)
            with session_factory.begin() as session:
                fail_and_retry_task(
                    session,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    worker_id=worker_id,
                    lease_token=lease_token,
                    error=str(exc),
                    error_code="EXTRACTION_FAILED",
                    base_backoff_seconds=2,
                )
        return True
    return False


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    if not settings.dashscope_api_key or not settings.siliconflow_api_key:
        raise RuntimeError("DASHSCOPE_API_KEY and SILICONFLOW_API_KEY must be set")
    engine = create_database_engine(settings.database_url)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    fact_model = QwenFactModel(
        api_key=settings.dashscope_api_key,
        base_url=settings.dashscope_base_url,
        model=settings.dashscope_model,
    )
    embedder = SiliconFlowEmbedder(
        api_key=settings.siliconflow_api_key,
        base_url=settings.siliconflow_base_url,
        model_id=settings.siliconflow_embedding_model,
    )
    worker_id = f"worker_{uuid4().hex}"
    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        handled = run_once(
            session_factory,
            worker_id=worker_id,
            fact_model=fact_model,
            embedder=embedder,
            duplicate_threshold=settings.duplicate_candidate_threshold,
        )
        if not handled:
            time.sleep(1)


if __name__ == "__main__":
    main()
