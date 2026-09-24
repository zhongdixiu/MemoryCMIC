from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ProviderError(RuntimeError):
    pass


class ExtractedFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory: str = Field(min_length=1, max_length=4096)
    message_ids: list[str] = Field(min_length=1)


class ExtractionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facts: list[ExtractedFact]


class DuplicateOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duplicate_memory_id: str | None


class FactModel(Protocol):
    def extract(
        self, *, targets: list[dict[str, Any]], history: list[dict[str, Any]]
    ) -> list[ExtractedFact]: ...

    def find_duplicate(
        self, *, fact: str, candidates: list[dict[str, Any]]
    ) -> str | None: ...


class Embedder(Protocol):
    model_id: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _parse_json_object(value: str) -> dict[str, Any]:
    stripped = value.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines)
    parsed = json.loads(stripped)
    if not isinstance(parsed, dict):
        raise ValueError("model response must be a JSON object")
    return parsed


@dataclass
class QwenFactModel:
    api_key: str
    base_url: str
    model: str = "qwen3.8-max"

    def __post_init__(self) -> None:
        self._client = OpenAI(
            api_key=self.api_key, base_url=self.base_url, timeout=120, max_retries=0
        )

    def _json_completion(self, system: str, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for _ in range(3):
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0,
                    extra_body={"enable_thinking": False},
                )
                content = response.choices[0].message.content
                if not content:
                    raise ValueError("model returned empty content")
                return _parse_json_object(content)
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                last_error = exc
            except Exception as exc:
                raise ProviderError(f"Qwen request failed: {exc}") from exc
        raise ProviderError(f"Qwen returned invalid structured output: {last_error}")

    def extract(
        self, *, targets: list[dict[str, Any]], history: list[dict[str, Any]]
    ) -> list[ExtractedFact]:
        system = (
            "You extract durable user facts from conversation targets. "
            "History is context only. Extract only facts newly stated or explicitly confirmed "
            "by target user messages. Do not treat assistant acknowledgements, system messages, "
            "or failed/unknown tool results as proof of a user fact. Keep each fact atomic and "
            "self-contained. Every fact must cite one or more target message_id values. "
            "Return JSON exactly as {\"facts\":[{\"memory\":str,\"message_ids\":[str]}]}. "
            "Return an empty facts array when there is no durable fact."
        )
        target_ids = {message["message_id"] for message in targets}
        try:
            output = ExtractionOutput.model_validate(
                self._json_completion(system, {"history": history, "targets": targets})
            )
        except ValidationError as exc:
            raise ProviderError(f"invalid extraction output: {exc}") from exc
        for fact in output.facts:
            if not set(fact.message_ids).issubset(target_ids):
                raise ProviderError("extraction output cites a non-target message_id")
        return output.facts

    def find_duplicate(
        self, *, fact: str, candidates: list[dict[str, Any]]
    ) -> str | None:
        system = (
            "Decide whether the new fact has exactly the same durable meaning as one candidate. "
            "Related, complementary, temporally changed, corrected, or contradictory facts are "
            "not duplicates. Similar wording alone is insufficient. Return JSON exactly as "
            "{\"duplicate_memory_id\": string|null}. The ID must come from candidates."
        )
        try:
            output = DuplicateOutput.model_validate(
                self._json_completion(system, {"fact": fact, "candidates": candidates})
            )
        except ValidationError as exc:
            raise ProviderError(f"invalid duplicate output: {exc}") from exc
        candidate_ids = {candidate["id"] for candidate in candidates}
        if (
            output.duplicate_memory_id is not None
            and output.duplicate_memory_id not in candidate_ids
        ):
            raise ProviderError("duplicate output cites an unknown memory ID")
        return output.duplicate_memory_id


@dataclass
class SiliconFlowEmbedder:
    api_key: str
    base_url: str
    model_id: str = "BAAI/bge-m3"

    def __post_init__(self) -> None:
        self._client = OpenAI(
            api_key=self.api_key, base_url=self.base_url, timeout=30, max_retries=0
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), 32):
            batch = texts[start : start + 32]
            try:
                response = self._client.embeddings.create(
                    model=self.model_id, input=batch, encoding_format="float"
                )
            except Exception as exc:
                raise ProviderError(f"SiliconFlow embedding request failed: {exc}") from exc
            ordered = sorted(response.data, key=lambda item: item.index)
            vectors.extend(list(item.embedding) for item in ordered)
        if len(vectors) != len(texts) or any(len(vector) != 1024 for vector in vectors):
            raise ProviderError("SiliconFlow returned an unexpected embedding shape")
        return vectors
