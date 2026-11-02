from pathlib import Path

from src.evidence import build_field_evidence, build_amendment_evidence
from src.amendments import extract_explicit_deadline_amendments
from src.demo import DOCS


def test_lp_a_field_evidence_is_exact_source_text():
    text = (DOCS / "01_LP_A_SIDE_LETTER.txt").read_text(encoding="utf-8")
    candidate = {
        "investor": "LP-A",
        "fund": "ABC PRIVATE EQUITY FUND IV",
        "deliverables": ["unaudited quarterly financial statements", "portfolio exposure schedule"],
        "frequency": "quarterly",
        "deadline_value": 45,
        "deadline_unit": "calendar_days",
        "trigger": "end of each fiscal quarter",
        "responsible_party": "General Partner",
        "conditions": ["The portfolio exposure schedule shall be delivered as a spreadsheet in the format agreed with Investor LP-A."],
        "clause_reference": "Clause 4.2",
    }
    evidence = build_field_evidence(text, candidate)
    flattened = [item for group in evidence["fields"].values() for item in group]
    for item in flattened:
        if item["status"] == "verified_substring":
            assert item["quote"] in text
            assert item["quote_is_exact_source_substring"] is True
    assert evidence["fields"]["deadline"][0]["status"] == "verified_substring"
    assert evidence["fields"]["conditions"][0]["clause"] == "Clause 4.3"


def test_amendment_evidence_locates_old_new_and_effective_date():
    text = (DOCS / "06_LP_A_AMENDMENT_01.txt").read_text(encoding="utf-8")
    candidate = extract_explicit_deadline_amendments(text, "06_LP_A_AMENDMENT_01.txt")[0]
    evidence = build_amendment_evidence(text, candidate)
    assert evidence["fields"]["old_value"][0]["status"] == "verified_substring"
    assert evidence["fields"]["new_value"][0]["status"] == "verified_substring"
    assert evidence["fields"]["effective_date"][0]["status"] == "verified_substring"
    for group in evidence["fields"].values():
        for item in group:
            if item.get("quote"):
                assert item["quote"] in text


def test_lp_c_evidence_marks_inferred_frequency_and_unspecified_deadline():
    text = (DOCS / "03_LP_C_SIDE_LETTER.txt").read_text(encoding="utf-8")
    candidate = {
        "investor": "LP-C", "fund": "ABC PRIVATE EQUITY FUND IV",
        "deliverables": ["additional portfolio information"],
        "frequency": "quarterly", "trigger": "end of each fiscal quarter",
        "deadline_value": None, "deadline_unit": "unspecified",
        "responsible_party": "General Partner", "conditions": ["commercially reasonable efforts"],
        "clause_reference": "Clause 5.1",
    }
    evidence = build_field_evidence(text, candidate)
    assert evidence["fields"]["frequency"][0]["status"] == "inferred_from_trigger"
    assert evidence["fields"]["deadline"][0]["status"] == "not_explicitly_specified"
    assert evidence["fields"]["deadline"][0]["quote"] in text
