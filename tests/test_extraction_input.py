from memory_cmic.extraction_input import ExtractionBudget, build_slices, input_size, split_slice
from memory_cmic.providers import EXTRACTION_PROMPT


def _message(mid, text, **extra):
    return {
        "message_id": mid,
        "role": "user",
        "content": text,
        "occurred_at": "2026-09-27T09:00:00+08:00",
        **extra,
    }


def test_slices_preserve_every_character_and_budget():
    budget = ExtractionBudget(input_tokens=8000, output_tokens=512)
    text = ("甲乙丙丁。\n" * 500) + "尾部"
    slices = build_slices([_message("long", text)], [], system=EXTRACTION_PROMPT, budget=budget)
    assert len(slices) > 1
    assert "".join(s["targets"][0]["content"] for s in slices) == text
    cursor = 0
    for current in slices:
        fragment = current["targets"][0]
        assert fragment["start_char"] == cursor
        cursor = fragment["end_char"]
        assert text[fragment["start_char"] : cursor] == fragment["content"]
        assert (
            input_size(
                EXTRACTION_PROMPT, {"targets": current["targets"], "history": current["history"]}
            )
            <= 7488
        )
    assert cursor == len(text)


def test_small_messages_share_slice_and_far_history_is_trimmed():
    targets = [_message("m1", "请简洁回复邮件。"), _message("m2", "称呼我林舟。")]
    history = [_message("old", "无关历史" * 10000)]
    slices = build_slices(targets, history, system=EXTRACTION_PROMPT, budget=ExtractionBudget())
    assert len(slices) == 1
    assert len(slices[0]["targets"]) == 2
    assert slices[0]["history"] == []


def test_split_preserves_text_offsets_and_prior_context():
    current = {
        "targets": [{**_message("m1", "甲乙丙丁"), "start_char": 10, "end_char": 14}],
        "history": [],
        "split_depth": 0,
    }
    left, right = split_slice(current)
    assert left["targets"][0]["end_char"] == right["targets"][0]["start_char"] == 12
    assert right["history"] == left["targets"]
    assert left["targets"][0]["content"] + right["targets"][0]["content"] == "甲乙丙丁"
