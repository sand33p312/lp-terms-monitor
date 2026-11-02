"""Strict source-quote grounding for model-generated quotations.

The model may flatten line breaks or normalize typographic whitespace/quotes.
This module maps such a quote back to an exact raw span in the stored source.
It does not use fuzzy matching, semantic similarity, or edit distance.
"""
from __future__ import annotations

import unicodedata
from typing import Any

_CHAR_MAP = str.maketrans({
    "\u2018": "'",  # left single quotation mark
    "\u2019": "'",  # right single quotation mark
    "\u201a": "'",  # single low-9 quotation mark
    "\u201c": '"',  # left double quotation mark
    "\u201d": '"',  # right double quotation mark
    "\u201e": '"',  # double low-9 quotation mark
    "\u2010": "-",  # hyphen
    "\u2011": "-",  # non-breaking hyphen
    "\u2012": "-",  # figure dash
    "\u2013": "-",  # en dash
    "\u2014": "-",  # em dash
    "\u2212": "-",  # minus sign
})


def _normalise_with_offsets(text: str) -> tuple[str, list[int], list[int]]:
    """Normalize Unicode/whitespace while retaining offsets into original text."""
    output: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    pending_space: tuple[int, int] | None = None

    for index, original_char in enumerate(text):
        expanded = unicodedata.normalize("NFKC", original_char).translate(_CHAR_MAP)
        for char in expanded:
            if char.isspace():
                if output:
                    if pending_space is None:
                        pending_space = (index, index + 1)
                    else:
                        pending_space = (pending_space[0], index + 1)
                continue

            if pending_space is not None:
                output.append(" ")
                starts.append(pending_space[0])
                ends.append(pending_space[1])
                pending_space = None

            output.append(char)
            starts.append(index)
            ends.append(index + 1)

    # Trailing whitespace is intentionally omitted from normalized text.
    return "".join(output), starts, ends


def ground_source_quote(source_text: str, model_quote: str | None) -> dict[str, Any]:
    """Locate a model quote and return the exact raw source span.

    Returns a grounded raw quote and method when either an exact substring or
    a strict normalized-text match is found. Otherwise returns no quote.
    """
    quote = (model_quote or "").strip()
    if not quote:
        return {"matched": False, "quote": None, "method": "not_found"}

    exact_start = source_text.find(quote)
    if exact_start >= 0:
        return {
            "matched": True,
            "quote": source_text[exact_start:exact_start + len(quote)],
            "method": "exact_substring",
            "start": exact_start,
            "end": exact_start + len(quote),
        }

    normalized_source, starts, ends = _normalise_with_offsets(source_text)
    normalized_quote, _, _ = _normalise_with_offsets(quote)
    if not normalized_quote:
        return {"matched": False, "quote": None, "method": "not_found"}

    position = normalized_source.find(normalized_quote)
    if position < 0:
        return {"matched": False, "quote": None, "method": "not_found"}

    last = position + len(normalized_quote) - 1
    raw_start = starts[position]
    raw_end = ends[last]
    raw_quote = source_text[raw_start:raw_end]

    # The returned quotation is always an exact substring of the original
    # document, even when the incoming model quotation had normalized spaces.
    return {
        "matched": True,
        "quote": raw_quote,
        "method": "normalized_whitespace_or_typography",
        "start": raw_start,
        "end": raw_end,
    }
