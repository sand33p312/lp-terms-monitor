"""LLM-assisted extraction through OpenRouter's OpenAI-compatible API."""
from __future__ import annotations

import json
import os
from typing import Any

import requests

from .amendments import extract_explicit_deadline_amendments
from .validation import validate_candidate
from .quote_grounding import ground_source_quote
from .consistency import check_obligation_consistency, check_amendment_consistency, amendment_review_notes

SYSTEM_PROMPT = """You are an information-extraction assistant for a private-fund operations prototype.
Extract only explicit reporting obligations and closely related operational terms from
provided contract text. Do not provide legal advice and do not decide whether a clause
is legally enforceable, superseded, or applicable over another document.

Rules:
- Never infer a deadline that is not explicit. Use null and deadline_unit='unspecified'.
- Preserve the exact supporting source_quote as a verbatim substring.
- If the clause is ambiguous, mark requires_human_review=true and explain why.
- Do not decide legal priority between an LPA, side letter or amendment.
- Do not extract notification, excuse, MFN, fee, or investment restriction provisions
  as reporting obligations. You may return them in excluded_clauses for audit visibility.
- Return valid JSON only, matching the schema in the user prompt.
"""

AMENDMENT_SYSTEM_PROMPT = """You identify proposed changes in private-fund amendment documents.
This is information extraction only, not legal interpretation or legal advice.

Extract explicit changes that modify an existing clause. Return one amendment_changes
entry for each distinct proposed change. In particular, detect old-value to new-value
replacements such as a deadline changing from 45 calendar days to 30 calendar days.

Rules:
- Do not turn an amendment into a new standalone reporting obligation.
- Identify the investor, target agreement, target clause, field being changed, old value,
  new value, effective date, and terms expressly left unchanged.
- For deadline values, old_value/new_value must each contain integer 'value', unit
  ('calendar_days' or 'business_days'), and exact 'raw' wording.
- source_quote must be an exact verbatim substring of the amendment document.
- Do not decide whether the amendment is valid, executed, controlling, or legally effective.
- Every candidate requires human review.
- If the target obligation or old value cannot be identified, return null and explain.
- Return valid JSON only.
"""


def _response_schema() -> dict[str, Any]:
    return {
        "obligations": [{
            "investor": "string or null", "fund": "string or null",
            "obligation_type": "reporting|notification|other", "deliverables": ["string"],
            "frequency": "monthly|quarterly|semi_annual|annual|event_driven|one_time|unspecified",
            "trigger": "string", "deadline_value": "integer or null",
            "deadline_unit": "calendar_days|business_days|unspecified",
            "responsible_party": "string or null", "conditions": ["string"],
            "effective_from": "YYYY-MM-DD string or null", "clause_reference": "string or null",
            "source_quote": "exact verbatim quote", "requires_human_review": True,
            "review_reason": "string", "document_role": "lpa|side_letter|unknown",
            "potential_amendment_note": "string or null",
        }],
        "excluded_clauses": [{"reason": "string", "source_quote": "exact quote"}],
    }


def _amendment_schema() -> dict[str, Any]:
    return {
        "amendment_changes": [{
            "investor": "LP-A or null", "fund": "string or null",
            "target_document": "string or null", "target_clause": "Clause 4.2 or null",
            "change_type": "replace_value|add_term|delete_term|other", "field_name": "deadline",
            "old_value": {"value": 45, "unit": "calendar_days", "raw": "forty-five (45) calendar days"},
            "new_value": {"value": 30, "unit": "calendar_days", "raw": "thirty (30) calendar days"},
            "effective_date": "YYYY-MM-DD or null", "unchanged_terms": ["string"],
            "source_quote": "exact verbatim quote", "requires_human_review": True,
            "review_reason": "string",
        }],
        "excluded_clauses": [{"reason": "string", "source_quote": "exact quote"}],
    }


def _load_json_content(content: Any) -> dict[str, Any]:
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    content = str(content or "").strip()
    if content.startswith("```"):
        content = content.strip("`").strip()
        if content.lower().startswith("json"):
            content = content[4:].strip()
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("LLM response must be a JSON object")
    return data


def _openrouter_json(system_prompt: str, user_prompt: str) -> dict[str, Any]:
    api_key = os.getenv("OPENROUTER_API_KEY")
    model = os.getenv("OPENROUTER_MODEL")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set. Add it to your environment or .env file.")
    if not model:
        raise RuntimeError("OPENROUTER_MODEL is not set. Choose a model available to your OpenRouter account.")

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:8501", "X-Title": "LP Terms Monitor Prototype",
        },
        json={
            "model": model, "temperature": 0,
            "messages": [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": user_prompt}],
        },
        timeout=90,
    )
    response.raise_for_status()
    payload = response.json()
    try:
        return _load_json_content(payload["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("OpenRouter returned a response that could not be parsed as the required JSON.") from exc


def extract_obligations_with_llm(
    source_text: str, filename: str, investor_hint: str | None = None,
    fund_hint: str | None = None,
) -> dict[str, Any]:
    user_prompt = f"""Extract candidate reporting obligations from this document.
Filename: {filename}
Investor hint: {investor_hint or 'not supplied'}
Fund hint: {fund_hint or 'not supplied'}
Return JSON matching this schema:\n{json.dumps(_response_schema(), indent=2)}
SOURCE DOCUMENT TEXT BEGIN\n{source_text[:60000]}\nSOURCE DOCUMENT TEXT END"""
    data = _openrouter_json(SYSTEM_PROMPT, user_prompt)
    if not isinstance(data.get("obligations", []), list):
        raise ValueError("LLM response did not contain an obligations array")
    for item in data.get("obligations", []):
        item.setdefault("fund", fund_hint)
        item.setdefault("investor", investor_hint)
        postprocess_obligation_item(item, source_text)
    return data


def postprocess_obligation_item(item: dict[str, Any], source_text: str) -> dict[str, Any]:
    """Deterministic checks applied to every model-proposed obligation (in place)."""
    item["requires_human_review"] = True  # enforced by application, never trusted to model
    item["review_reason"] = item.get("review_reason") or "Mandatory human verification of contractual interpretation."
    grounded = ground_source_quote(source_text, item.get("source_quote"))
    if grounded["matched"]:
        item["source_quote"] = grounded["quote"]
        item["source_quote_grounding"] = grounded["method"]
    else:
        item["source_quote_grounding"] = "not_found"
    item["validation_errors"] = validate_candidate(item, source_text)
    item["consistency_warnings"] = check_obligation_consistency(item)
    return item


def extract_amendments_with_llm(
    source_text: str, filename: str, investor_hint: str | None = None,
    fund_hint: str | None = None,
) -> dict[str, Any]:
    """Extract amendment changes; use a narrow deterministic fast path when exact replacement syntax exists."""
    deterministic = extract_explicit_deadline_amendments(source_text, filename)
    if deterministic:
        for item in deterministic:
            grounded = ground_source_quote(source_text, item.get("source_quote"))
            if grounded["matched"]:
                item["source_quote"] = grounded["quote"]
                item["source_quote_grounding"] = grounded["method"]
                item["validation_errors"] = check_amendment_consistency(item)
            else:
                item["source_quote_grounding"] = "not_found"
                item["validation_errors"] = ["source_quote could not be grounded to an exact source span (after whitespace/typography normalization)"]
            item["review_notes"] = amendment_review_notes(item)
        return {"amendment_changes": deterministic, "excluded_clauses": [], "extraction_method": "deterministic_explicit_replacement_pattern"}

    user_prompt = f"""Extract proposed changes from this amendment document.
Filename: {filename}
Investor hint: {investor_hint or 'not supplied'}
Fund hint: {fund_hint or 'not supplied'}
Return JSON matching this schema:\n{json.dumps(_amendment_schema(), indent=2)}
AMENDMENT DOCUMENT TEXT BEGIN\n{source_text[:60000]}\nAMENDMENT DOCUMENT TEXT END"""
    data = _openrouter_json(AMENDMENT_SYSTEM_PROMPT, user_prompt)
    if not isinstance(data.get("amendment_changes", []), list):
        raise ValueError("LLM response did not contain an amendment_changes array")
    for item in data.get("amendment_changes", []):
        postprocess_amendment_item(item, source_text)
        item["extraction_method"] = "llm"
    data["extraction_method"] = "llm"
    return data


def postprocess_amendment_item(item: dict[str, Any], source_text: str) -> dict[str, Any]:
    """Deterministic checks applied to every model-proposed amendment change (in place)."""
    item["requires_human_review"] = True
    item["review_reason"] = item.get("review_reason") or "Mandatory human confirmation of amendment applicability and legal effect."
    errors = []
    grounded = ground_source_quote(source_text, item.get("source_quote") or "")
    if grounded["matched"]:
        # Store the exact raw source span, never the model's normalized paraphrase.
        item["source_quote"] = grounded["quote"]
        item["source_quote_grounding"] = grounded["method"]
    else:
        item["source_quote_grounding"] = "not_found"
        errors.append("source_quote could not be grounded to an exact source span (after whitespace/typography normalization)")
    if item.get("field_name") not in (None, "deadline"):
        errors.append(f"change to '{item.get('field_name')}' is not a deadline replacement; manual handling only")
    for key in ("old_value", "new_value"):
        value = item.get(key)
        if not isinstance(value, dict) or not isinstance(value.get("value"), int) or value.get("unit") not in {"calendar_days", "business_days"}:
            errors.append(f"{key} must contain an integer value and supported deadline unit")
    if grounded["matched"]:
        errors.extend(check_amendment_consistency(item))
    item["validation_errors"] = errors
    item["review_notes"] = amendment_review_notes(item)
    return item
