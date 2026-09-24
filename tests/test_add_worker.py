from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from memory_cmic.api import create_app
from memory_cmic.auth import StaticCredentialStore
from memory_cmic.models import (
    ConversationSession,
    MemoryAuditLog,
    MemoryEmbedding,
    MemoryEvidence,
    MemoryItem,
    MemoryTask,
    MemoryTaskSource,
    SourceRecord,
)
from memory_cmic.providers import ExtractedFact
from memory_cmic.settings import Settings
from memory_cmic.worker import run_once


@dataclass
class FakeFactModel:
    duplicate_semantic_candidates: bool = False
    duplicate_checks: list[list[dict]] = field(default_factory=list)

    def extract(self, *, targets: list[dict], history: list[dict]) -> list[ExtractedFact]:
        user_target = next(message for message in targets if message["role"] == "user")
        return [
            ExtractedFact(
                memory=user_target["content"], message_ids=[user_target["message_id"]]
            )
        ]

    def find_duplicate(self, *, fact: str, candidates: list[dict]) -> str | None:
        self.duplicate_checks.append(candidates)
        return candidates[0]["id"] if self.duplicate_semantic_candidates else None


class FakeEmbedder:
    model_id = "BAAI/bge-m3"

    def embed(self, texts: list[str]) -> list[list[float]]:
        vector = [0.0] * 1024
        vector[0] = 1.0
        return [list(vector) for _ in texts]


def _settings(tenant_id: str) -> Settings:
    return Settings(
        database_url="unused",
        credentials=StaticCredentialStore(
            [
                {
                    "token": "worker-token",
                    "tenant_id": tenant_id,
                    "caller_agent_id": "agent_worker",
                    "allowed_source_systems": ["email_agent"],
                }
            ]
        ),
        dashscope_api_key=None,
        dashscope_base_url="https://example.invalid/v1",
        dashscope_model="qwen3.8-max",
        siliconflow_api_key=None,
        siliconflow_base_url="https://example.invalid/v1",
        siliconflow_embedding_model="BAAI/bge-m3",
        duplicate_candidate_threshold=0.85,
    )


@pytest.fixture
def worker_context(engine: Engine) -> Iterator[tuple[TestClient, str]]:
    tenant_id = f"tenant_worker_{uuid4().hex}"
    with TestClient(create_app(_settings(tenant_id), engine)) as client:
        yield client, tenant_id
    with engine.begin() as connection:
        connection.execute(delete(MemoryAuditLog).where(MemoryAuditLog.tenant_id == tenant_id))
        connection.execute(delete(MemoryEvidence).where(MemoryEvidence.tenant_id == tenant_id))
        connection.execute(delete(MemoryEmbedding).where(MemoryEmbedding.tenant_id == tenant_id))
        connection.execute(delete(MemoryTaskSource).where(MemoryTaskSource.tenant_id == tenant_id))
        connection.execute(delete(MemoryTask).where(MemoryTask.tenant_id == tenant_id))
        connection.execute(delete(MemoryItem).where(MemoryItem.tenant_id == tenant_id))
        connection.execute(delete(SourceRecord).where(SourceRecord.tenant_id == tenant_id))
        connection.execute(
            delete(ConversationSession).where(ConversationSession.tenant_id == tenant_id)
        )


def _add(client: TestClient, *, key: str, message_id: str, content: str) -> str:
    response = client.post(
        "/api/v1/memories:add",
        headers={
            "Authorization": "Bearer worker-token",
            "Idempotency-Key": key,
        },
        json={
            "source_system": "email_agent",
            "user_id": "user_worker",
            "session_id": "session_worker",
            "messages": [
                {
                    "message_id": message_id,
                    "role": "user",
                    "content": content,
                    "occurred_at": "2026-09-24T09:00:00+08:00",
                }
            ],
        },
    )
    assert response.status_code == 202
    return response.json()["task_id"]


def _run(engine: Engine, model: FakeFactModel) -> None:
    assert run_once(
        sessionmaker(engine, expire_on_commit=False),
        worker_id=f"worker_{uuid4().hex}",
        fact_model=model,
        embedder=FakeEmbedder(),
        duplicate_threshold=0.85,
    )


def test_worker_creates_fact_evidence_embedding_and_public_result(
    worker_context: tuple[TestClient, str], engine: Engine
) -> None:
    client, tenant_id = worker_context
    task_id = _add(
        client,
        key="worker-new",
        message_id="msg_new",
        content="用户偏好使用项目符号写周报。",
    )
    _run(engine, FakeFactModel())

    response = client.get(
        f"/api/v1/tasks/{task_id}", headers={"Authorization": "Bearer worker-token"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    assert response.json()["results"][0]["event"] == "ADD"

    with Session(engine) as session:
        assert session.scalar(
            select(func.count()).select_from(MemoryItem).where(
                MemoryItem.tenant_id == tenant_id
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(MemoryEvidence).where(
                MemoryEvidence.tenant_id == tenant_id
            )
        ) == 1
        embedding = session.scalars(
            select(MemoryEmbedding).where(MemoryEmbedding.tenant_id == tenant_id)
        ).one()
        assert len(embedding.embedding) == 1024


def test_worker_exact_duplicate_only_adds_evidence(
    worker_context: tuple[TestClient, str], engine: Engine
) -> None:
    client, tenant_id = worker_context
    content = "用户偏好使用项目符号写周报。"
    _add(client, key="exact-1", message_id="msg_exact_1", content=content)
    _run(engine, FakeFactModel())
    second_task = _add(client, key="exact-2", message_id="msg_exact_2", content=content)
    _run(engine, FakeFactModel())

    response = client.get(
        f"/api/v1/tasks/{second_task}", headers={"Authorization": "Bearer worker-token"}
    )
    assert response.json()["results"] == []
    with Session(engine) as session:
        assert session.scalar(
            select(func.count()).select_from(MemoryItem).where(
                MemoryItem.tenant_id == tenant_id
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(MemoryEvidence).where(
                MemoryEvidence.tenant_id == tenant_id
            )
        ) == 2


def test_high_similarity_is_classified_instead_of_hard_dropped(
    worker_context: tuple[TestClient, str], engine: Engine
) -> None:
    client, tenant_id = worker_context
    _add(client, key="semantic-1", message_id="msg_semantic_1", content="用户现在住在北京。")
    _run(engine, FakeFactModel())
    _add(client, key="semantic-2", message_id="msg_semantic_2", content="用户现在住在上海。")
    classifier = FakeFactModel(duplicate_semantic_candidates=False)
    _run(engine, classifier)

    assert classifier.duplicate_checks
    with Session(engine) as session:
        assert session.scalar(
            select(func.count()).select_from(MemoryItem).where(
                MemoryItem.tenant_id == tenant_id
            )
        ) == 2
