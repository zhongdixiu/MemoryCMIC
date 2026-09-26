from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExtractionBudget:
    input_tokens: int = 12000
    output_tokens: int = 2048
    max_model_calls: int = 20

    def __post_init__(self):
        if self.input_tokens <= self.output_tokens or self.output_tokens <= 0:
            raise ValueError("input budget must exceed the positive output reserve")
        if self.max_model_calls <= 0:
            raise ValueError("max_model_calls must be positive")


def input_size(system: str, payload: dict[str, Any]) -> int:
    # Count UTF-8 bytes conservatively, rather than assuming a characters/token ratio.
    return len(system.encode()) + len(json.dumps(payload, ensure_ascii=False).encode()) + 512


def build_slices(
    targets: list[dict[str, Any]],
    history: list[dict[str, Any]],
    *,
    system: str,
    budget: ExtractionBudget,
) -> list[dict[str, Any]]:
    limit = budget.input_tokens - budget.output_tokens
    fragments = []
    for message in targets:
        start = 0
        content = message["content"]
        while start < len(content):
            low, high, end = start + 1, len(content), start
            while low <= high:
                midpoint = (low + high) // 2
                fragment = {
                    **message,
                    "content": content[start:midpoint],
                    "start_char": start,
                    "end_char": midpoint,
                }
                if input_size(system, {"targets": [fragment], "history": []}) <= limit:
                    end, low = midpoint, midpoint + 1
                else:
                    high = midpoint - 1
            if end == start:
                raise ValueError("message metadata cannot fit in the model input budget")
            if end < len(content):
                paragraph = content.rfind("\n", start, end)
                if paragraph > start:
                    end = paragraph + 1
            fragments.append(
                {**message, "content": content[start:end], "start_char": start, "end_char": end}
            )
            start = end
    groups = []
    group = []
    for fragment in fragments:
        if group and (
            fragment["message_id"] in {m["message_id"] for m in group}
            or input_size(system, {"targets": [*group, fragment], "history": []}) > limit
        ):
            groups.append(group)
            group = []
        group.append(fragment)
    if group:
        groups.append(group)
    slices = []
    prior = list(history)
    for group in groups:
        selected = []
        related = {m.get("reply_to_message_id") for m in group}
        choices = sorted(
            enumerate(prior[-30:]),
            key=lambda pair: (pair[1]["message_id"] in related, pair[0]),
            reverse=True,
        )
        chosen = set()
        for position, previous in choices:
            proposed = [m for i, m in enumerate(prior[-30:]) if i in chosen or i == position]
            if input_size(system, {"targets": group, "history": proposed}) <= limit:
                selected = proposed
                chosen.add(position)
        slices.append({"targets": group, "history": selected, "split_depth": 0})
        prior.extend(group)
    return slices


def split_slice(current: dict[str, Any]) -> list[dict[str, Any]]:
    if current["split_depth"] >= 2:
        raise ValueError("output truncation recovery exhausted")
    targets = current["targets"]
    if len(targets) > 1:
        midpoint = len(targets) // 2
        first_targets, second_targets = targets[:midpoint], targets[midpoint:]
    else:
        target = targets[0]
        content = target["content"]
        if len(content) < 2:
            raise ValueError("target fragment cannot be split further")
        midpoint = len(content) // 2
        start = target["start_char"]
        first_targets = [{**target, "content": content[:midpoint], "end_char": start + midpoint}]
        second_targets = [{**target, "content": content[midpoint:], "start_char": start + midpoint}]
    return [
        {**current, "targets": first_targets, "split_depth": current["split_depth"] + 1},
        {
            **current,
            "targets": second_targets,
            "history": [*current["history"], *first_targets],
            "split_depth": current["split_depth"] + 1,
        },
    ]
