"""Flag possible precedence questions between fund-wide and investor-specific terms.

When the LPA sets a deadline for all Limited Partners and a side letter sets a
different deadline for the same deliverable, the register holds two live
records. Which one controls is a legal question this module does not answer.
It only puts the pair in front of a reviewer.

Matching is deliberately conservative: deliverables must be equal after
lower-casing and whitespace collapsing. Paraphrased deliverables are missed
(a known false negative), but nothing is flagged on a loose similarity score.
"""
from __future__ import annotations

import re
from typing import Any

_FUND_WIDE = {"all limited partners", "all lps", "each limited partner"}


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _is_fund_wide(obligation: dict[str, Any]) -> bool:
    return _norm(obligation.get("investor")) in _FUND_WIDE or obligation.get("document_role") == "lpa"


def find_precedence_questions(obligations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    live = [o for o in obligations if o.get("status") not in {"superseded", "rejected"}]
    fund_wide = [o for o in live if _is_fund_wide(o)]
    specific = [o for o in live if not _is_fund_wide(o)]
    flags = []
    for base in fund_wide:
        base_deliverables = {_norm(d) for d in base.get("deliverables") or []}
        for other in specific:
            if _norm(other.get("fund")) != _norm(base.get("fund")):
                continue
            shared = base_deliverables & {_norm(d) for d in other.get("deliverables") or []}
            if not shared:
                continue
            same_deadline = (base.get("deadline_value"), base.get("deadline_unit")) == (
                other.get("deadline_value"), other.get("deadline_unit"))
            if same_deadline:
                continue
            flags.append({
                "investor": other.get("investor"),
                "deliverables": sorted(shared),
                "fund_wide": f"{base.get('clause_reference')}: {base.get('deadline_value')} {base.get('deadline_unit')}",
                "investor_specific": f"{other.get('clause_reference')}: {other.get('deadline_value')} {other.get('deadline_unit')}",
                "fund_wide_id": base.get("id"), "investor_specific_id": other.get("id"),
                "note": "Two live records set different deadlines for the same deliverable. Precedence is a legal question; confirm which applies to this investor.",
            })
    return flags
