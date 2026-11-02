from __future__ import annotations

from src.quote_grounding import ground_source_quote
from src import extraction


def test_exact_substring_is_returned_unchanged():
    source = 'The report is due within thirty (30) calendar days.'
    quote = 'thirty (30) calendar days'
    result = ground_source_quote(source, quote)
    assert result["matched"] is True
    assert result["quote"] == quote
    assert result["method"] == "exact_substring"
    assert result["quote"] in source


def test_whitespace_flattened_llm_quote_maps_back_to_raw_source_span():
    source = (
        'With effect from 1 January 2027, the reference to\n'
        '"thirty (30) calendar days" in Clause 4.2 of the LP-A Side Letter\n'
        'is deleted and replaced with "twenty-five (25) calendar days".\n'
        'All other terms of Clause 4.2 remain unchanged.'
    )
    model_quote = (
        'With effect from 1 January 2027, the reference to "thirty (30) calendar days" '
        'in Clause 4.2 of the LP-A Side Letter is deleted and replaced with '
        '"twenty-five (25) calendar days". All other terms of Clause 4.2 remain unchanged.'
    )
    result = ground_source_quote(source, model_quote)
    assert result["matched"] is True
    assert result["method"] == "normalized_whitespace_or_typography"
    assert result["quote"] in source
    assert "\n" in result["quote"]


def test_smart_quotes_are_grounded_to_exact_original_characters():
    source = 'The LP says “thirty (30) calendar days” apply.'
    model_quote = 'The LP says "thirty (30) calendar days" apply.'
    result = ground_source_quote(source, model_quote)
    assert result["matched"] is True
    assert result["quote"] == source
    assert result["quote"] in source


def test_semantic_paraphrase_is_not_accepted_as_evidence():
    source = 'The deadline is thirty (30) calendar days.'
    result = ground_source_quote(source, 'The deadline is 30 days.')
    assert result["matched"] is False
    assert result["quote"] is None


def test_llm_amendment_quote_is_grounded_before_validation(monkeypatch):
    source = (
        'AMENDMENT NO. 2 TO SIDE LETTER\n'
        'INVESTOR LP-A\n'
        'With effect from 1 January 2027, the reference to\n'
        '"thirty (30) calendar days" in Clause 4.2 of the LP-A Side Letter\n'
        'is deleted and replaced with "twenty-five (25) calendar days".\n'
    )
    model_quote = (
        'With effect from 1 January 2027, the reference to "thirty (30) calendar days" '
        'in Clause 4.2 of the LP-A Side Letter is deleted and replaced with '
        '"twenty-five (25) calendar days".'
    )
    payload = {
        "amendment_changes": [{
            "investor": "LP-A", "fund": "ABC Private Equity Fund IV",
            "target_document": "LP-A Side Letter", "target_clause": "Clause 4.2",
            "change_type": "replace_value", "field_name": "deadline",
            "old_value": {"value": 30, "unit": "calendar_days", "raw": "thirty (30) calendar days"},
            "new_value": {"value": 25, "unit": "calendar_days", "raw": "twenty-five (25) calendar days"},
            "effective_date": "2027-01-01", "unchanged_terms": [],
            "source_quote": model_quote, "requires_human_review": False,
            "review_reason": "",
        }],
        "excluded_clauses": [],
    }
    monkeypatch.setattr(extraction, "extract_explicit_deadline_amendments", lambda *_args: [])
    monkeypatch.setattr(extraction, "_openrouter_json", lambda *_args: payload)

    result = extraction.extract_amendments_with_llm(source, "08_LP_A_AMENDMENT_02.txt")
    item = result["amendment_changes"][0]
    assert item["requires_human_review"] is True
    assert item["source_quote"] in source
    assert "source_quote could not be grounded" not in " ".join(item["validation_errors"])
    assert item["validation_errors"] == []
    assert item["source_quote_grounding"] == "normalized_whitespace_or_typography"
