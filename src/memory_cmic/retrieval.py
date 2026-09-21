from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Select, func, literal, or_, select
from sqlalchemy.orm import Session

from memory_cmic.models import MemoryEmbedding, MemoryItem


def _scope_filters(
    *,
    tenant_id: str,
    business_domain: str,
    project_id: str | None,
    now: datetime,
) -> tuple[object, ...]:
    project_filter = MemoryItem.project_domains.is_(None)
    if project_id is not None:
        project_filter = or_(project_filter, MemoryItem.project_domains.any(project_id))

    return (
        MemoryItem.tenant_id == tenant_id,
        MemoryItem.status == "active",
        or_(MemoryItem.expired_at.is_(None), MemoryItem.expired_at > now),
        or_(
            MemoryItem.business_domains.is_(None),
            MemoryItem.business_domains.any(business_domain),
        ),
        project_filter,
    )


def _subject_routes(
    *,
    user_id: str,
    authorized_agent_ids: list[str],
    company_ids: list[str],
) -> list[tuple[object, ...]]:
    routes = [(MemoryItem.subject_type == "user", MemoryItem.subject_id == user_id)]
    if authorized_agent_ids:
        routes.append(
            (
                MemoryItem.subject_type == "agent",
                MemoryItem.subject_id.in_(authorized_agent_ids),
            )
        )
    if company_ids:
        routes.append(
            (MemoryItem.subject_type == "company", MemoryItem.subject_id.in_(company_ids))
        )
    return routes


def _current_content_hash() -> object:
    memory_text = func.concat(MemoryItem.summary, literal("\n"), MemoryItem.content)
    return func.encode(func.sha256(func.convert_to(memory_text, "UTF8")), "hex")


def _vector_statement(
    *,
    route: tuple[object, ...],
    scope_filters: tuple[object, ...],
    model_id: str,
    query_embedding: list[float],
    limit: int,
) -> Select[tuple[MemoryItem, float]]:
    distance = MemoryEmbedding.embedding.cosine_distance(query_embedding).label("distance")
    return (
        select(MemoryItem, distance)
        .join(
            MemoryEmbedding,
            (MemoryEmbedding.tenant_id == MemoryItem.tenant_id)
            & (MemoryEmbedding.memory_id == MemoryItem.id),
        )
        .where(
            *scope_filters,
            *route,
            MemoryEmbedding.model_id == model_id,
            MemoryEmbedding.status == "active",
            MemoryEmbedding.content_hash == _current_content_hash(),
        )
        .order_by(distance)
        .limit(limit)
    )


def search_memories(
    session: Session,
    *,
    tenant_id: str,
    user_id: str,
    business_domain: str,
    project_id: str | None,
    authorized_agent_ids: list[str],
    company_ids: list[str],
    model_id: str | None = None,
    query_embedding: list[float] | None = None,
    limit: int = 10,
    now: datetime | None = None,
) -> list[MemoryItem]:
    """Return eligible user, Agent and company memories, optionally vector-ranked."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    if query_embedding is not None and model_id is None:
        raise ValueError("model_id is required for vector search")

    scope_filters = _scope_filters(
        tenant_id=tenant_id,
        business_domain=business_domain,
        project_id=project_id,
        now=now or datetime.now(UTC),
    )
    routes = _subject_routes(
        user_id=user_id,
        authorized_agent_ids=authorized_agent_ids,
        company_ids=company_ids,
    )

    if query_embedding is None:
        memories: dict[str, MemoryItem] = {}
        for route in routes:
            statement = select(MemoryItem).where(*scope_filters, *route).order_by(MemoryItem.id)
            for memory in session.scalars(statement):
                memories[memory.id] = memory
        return [memories[memory_id] for memory_id in sorted(memories)]

    ranked: dict[str, tuple[MemoryItem, float]] = {}
    for route in routes:
        statement = _vector_statement(
            route=route,
            scope_filters=scope_filters,
            model_id=model_id,
            query_embedding=query_embedding,
            limit=limit,
        )
        for memory, distance in session.execute(statement):
            previous = ranked.get(memory.id)
            if previous is None or distance < previous[1]:
                ranked[memory.id] = (memory, distance)

    ordered = sorted(ranked.values(), key=lambda item: (item[1], item[0].id))
    return [memory for memory, _ in ordered[:limit]]
