"""Evaluation harness for LP Terms Monitor.

Two modes, both scored against eval/gold.json (hand-written labels):

  python -m eval.run_eval            # offline: no API key, deterministic layer only
  python -m eval.run_eval --llm      # also calls the configured OpenRouter model

Offline mode answers: "if the model is wrong in a known way, does the
deterministic layer catch it?" and "are the parts that must be exact (dates,
quotes, matching) exact?". It feeds the gold record plus deliberately broken
variants of it through the same post-processing functions the app uses.

LLM mode answers: "how often is the model right on these 17 documents?" It
needs OPENROUTER_API_KEY and OPENROUTER_MODEL in the environment or .env.

Results are written to eval/results_offline.md (and results_llm.md) plus JSON.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.amendments import extract_explicit_deadline_amendments, match_amendment_to_obligations  # noqa: E402
from src.demo import load_demo_data  # noqa: E402
from src.evidence import build_field_evidence  # noqa: E402
from src.extraction import postprocess_amendment_item, postprocess_obligation_item  # noqa: E402
from src.conflicts import find_precedence_questions  # noqa: E402
from src.rules import calculate_deadline  # noqa: E402
from src.store import list_obligations  # noqa: E402

DOCS = ROOT / "data" / "synthetic_docs"
GOLD = json.loads((ROOT / "eval" / "gold.json").read_text(encoding="utf-8"))
HOLIDAYS = {date.fromisoformat(d) for d in GOLD["holiday_calendar"]["dates"]}
OUT = ROOT / "eval"


def doc_text(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def seeded_register() -> list[dict]:
    """The obligation register state after 'Load synthetic demo' (temporary DB)."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "eval.db"
        load_demo_data(db)
        return list_obligations(db)


# ---------------------------------------------------------------- offline: obligations

def _flatten_and_curl(quote: str) -> str:
    """Simulate a model that re-wraps whitespace and uses typographic quotes/dashes."""
    out = quote.replace(" within ", "  within\n").replace("(", "( ").replace("( ", "(")
    return out.replace('"', "“", 1).replace('"', "”", 1).replace(" - ", " – ")


def obligation_offline(case: dict) -> list[dict]:
    text = doc_text(case["document"])
    rows = []
    for idx, gold in enumerate(case["obligations"]):
        tag = f"{case['id']}.{idx + 1}"
        base = {k: v for k, v in gold.items() if k not in {"expect_escalation", "deadline_check", "known_gap", "escalation_reason"}}

        # 1. Gold record passes validation; escalation matches expectation.
        item = postprocess_obligation_item(copy.deepcopy(base), text)
        escalated = bool(item["consistency_warnings"])
        rows.append({
            "case": tag, "check": "gold record accepted",
            "expected": f"no errors; escalated={gold['expect_escalation']}",
            "actual": f"errors={len(item['validation_errors'])}; escalated={escalated}",
            "pass": not item["validation_errors"] and escalated == gold["expect_escalation"],
            "detail": "; ".join(item["validation_errors"] + item["consistency_warnings"]),
        })

        # 2. Invented / computed deadline must be caught.
        bad = copy.deepcopy(base)
        if bad.get("deadline_value") is None:
            bad["deadline_value"], bad["deadline_unit"] = 30, "calendar_days"
        else:
            bad["deadline_value"] += 15
        item = postprocess_obligation_item(bad, text)
        caught = any("not stated in the cited source quote" in w for w in item["consistency_warnings"])
        rows.append({"case": tag, "check": "invented deadline caught", "expected": "escalated",
                     "actual": "escalated" if caught else "accepted", "pass": caught,
                     "detail": f"injected {bad['deadline_value']} {bad['deadline_unit']}"})

        # 3. Paraphrased quote must fail grounding.
        para = copy.deepcopy(base)
        para["source_quote"] = para["source_quote"].replace("shall", "must").replace("within", "no later than")
        item = postprocess_obligation_item(para, text)
        caught = item["source_quote_grounding"] == "not_found" and any("substring" in e for e in item["validation_errors"])
        rows.append({"case": tag, "check": "paraphrased quote rejected", "expected": "save blocked",
                     "actual": "save blocked" if caught else "accepted", "pass": caught, "detail": ""})

        # 4. Whitespace/typography-mangled quote must be restored to the exact raw span.
        mangled = copy.deepcopy(base)
        mangled["source_quote"] = _flatten_and_curl(mangled["source_quote"])
        item = postprocess_obligation_item(mangled, text)
        ok = item["source_quote"] in text and not item["validation_errors"]
        rows.append({"case": tag, "check": "re-wrapped quote restored to raw span", "expected": "grounded",
                     "actual": item["source_quote_grounding"], "pass": ok, "detail": ""})

        # 5. Unit swap must be caught (only where a unit exists).
        if base.get("deadline_unit") in {"calendar_days", "business_days"}:
            swap = copy.deepcopy(base)
            swap["deadline_unit"] = "business_days" if base["deadline_unit"] == "calendar_days" else "calendar_days"
            item = postprocess_obligation_item(swap, text)
            caught = any("contradicts the unit" in w for w in item["consistency_warnings"])
            rows.append({"case": tag, "check": "calendar/business unit swap caught", "expected": "escalated",
                         "actual": "escalated" if caught else "accepted", "pass": caught, "detail": ""})

        # 6. Wrong investor must be caught.
        if base["investor"].startswith("LP-"):
            wrong = copy.deepcopy(base)
            wrong["investor"] = "LP-Z"
            item = postprocess_obligation_item(wrong, text)
            caught = any("not named in the cited quote" in w for w in item["consistency_warnings"])
            rows.append({"case": tag, "check": "wrong investor caught", "expected": "escalated",
                         "actual": "escalated" if caught else "accepted", "pass": caught, "detail": ""})

        # 7. Field evidence helper (informational: shows where literal lookup fails).
        ev = build_field_evidence(text, base)
        deadline_status = ev["fields"]["deadline"][0]["status"]
        expected_deadline_status = "not_explicitly_specified" if base.get("deadline_value") is None else "verified_substring"
        rows.append({"case": tag, "check": "evidence helper locates deadline", "expected": expected_deadline_status,
                     "actual": deadline_status, "pass": deadline_status == expected_deadline_status,
                     "detail": f"{ev['unlocated_field_count']} field(s) unlocated overall"})

        # 8. Deterministic due-date calculation.
        dc = gold.get("deadline_check")
        if dc:
            pe = date.fromisoformat(dc["period_end"])
            naive = calculate_deadline(pe, base["deadline_value"], base["deadline_unit"])
            with_cal = calculate_deadline(pe, base["deadline_value"], base["deadline_unit"], holidays=HOLIDAYS)
            rows.append({"case": tag, "check": "due date, weekends-only default", "expected": dc["expected_due"],
                         "actual": naive.isoformat(), "pass": naive.isoformat() == dc["expected_due"],
                         "detail": f"period end {dc['period_end']}"})
            if base["deadline_unit"] == "business_days":
                rows.append({"case": tag, "check": "due date, with LPA holiday calendar", "expected": dc["expected_due"],
                             "actual": with_cal.isoformat(), "pass": with_cal.isoformat() == dc["expected_due"],
                             "detail": f"period end {dc['period_end']}"})
    if not case["obligations"]:
        rows.append({"case": case["id"], "check": "out-of-scope document", "expected": "0 reporting obligations in gold",
                     "actual": "n/a offline (LLM decides)", "pass": None, "detail": "scored in --llm mode"})
    return rows


# ---------------------------------------------------------------- offline: amendments

def _gold_amendment(case: dict) -> dict:
    return {
        "investor": case["investor"], "fund": "ABC Private Equity Fund IV",
        "target_clause": case["target_clause"], "change_type": "replace_value",
        "field_name": case["field_name"], "old_value": copy.deepcopy(case["old_value"]),
        "new_value": copy.deepcopy(case["new_value"]), "effective_date": case["effective_date"],
        "source_quote": case["source_quote"],
    }


def amendment_offline(case: dict, register: list[dict]) -> list[dict]:
    text = doc_text(case["document"])
    rows = []
    fast = extract_explicit_deadline_amendments(text, case["document"])
    if fast:
        f = fast[0]
        fields_ok = (f["investor"] == case["investor"] and f["target_clause"] == case["target_clause"]
                     and f["effective_date"] == case["effective_date"]
                     and {k: f["old_value"][k] for k in ("value", "unit")} == case["old_value"]
                     and {k: f["new_value"][k] for k in ("value", "unit")} == case["new_value"])
        actual = "extract (fields correct)" if fields_ok else "extract (FIELDS WRONG)"
        passed = case["expected_fast_path"] == "extract" and fields_ok
    else:
        actual = "decline -> LLM fallback"
        passed = case["expected_fast_path"] == "decline"
    rows.append({"case": case["id"], "check": "deterministic fast path", "expected": case["expected_fast_path"],
                 "actual": actual, "pass": passed, "detail": ""})

    gold = postprocess_amendment_item(_gold_amendment(case), text)
    match = match_amendment_to_obligations(gold, register)
    rows.append({"case": case["id"], "check": "match against seeded register", "expected": case["expected_match"],
                 "actual": match["match_status"], "pass": match["match_status"] == case["expected_match"], "detail": ""})
    blocked = bool(gold["validation_errors"])
    rows.append({"case": case["id"], "check": "auto-apply blocked", "expected": str(case["expected_blocked"]),
                 "actual": str(blocked), "pass": blocked == case["expected_blocked"],
                 "detail": "; ".join(gold["validation_errors"])})
    if case.get("expected_review_note"):
        rows.append({"case": case["id"], "check": "unit-change review note shown", "expected": "note",
                     "actual": "note" if gold["review_notes"] else "none", "pass": bool(gold["review_notes"]), "detail": ""})

    if isinstance(case["new_value"], dict) and not case["expected_blocked"]:
        tampered = _gold_amendment(case)
        tampered["new_value"]["value"] += 7
        tampered = postprocess_amendment_item(tampered, text)
        caught = any("new_value" in e for e in tampered["validation_errors"])
        rows.append({"case": case["id"], "check": "tampered new value blocked", "expected": "blocked",
                     "actual": "blocked" if caught else "accepted", "pass": caught, "detail": ""})
    return rows


# ---------------------------------------------------------------- LLM mode

def _norm(v):
    return (v or "").strip().casefold() if isinstance(v, str) else v


def _clause_num(v):
    import re
    m = re.search(r"\d+(?:\.\d+)*(?:\([a-z]\))?", v or "")
    return m.group(0) if m else None


def llm_eval(register: list[dict]) -> tuple[list[dict], dict]:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from src.extraction import extract_amendments_with_llm, extract_obligations_with_llm
    rows, raw = [], {}
    for case in GOLD["obligation_cases"]:
        text = doc_text(case["document"])
        try:
            out = extract_obligations_with_llm(text, case["document"], None, "ABC Private Equity Fund IV")
        except Exception as exc:  # report, do not hide
            rows.append({"case": case["id"], "check": "LLM call", "expected": "ok", "actual": f"error: {exc}", "pass": False, "detail": ""})
            continue
        raw[case["id"]] = out
        preds = [o for o in out.get("obligations", []) if o.get("obligation_type") == "reporting"]
        rows.append({"case": case["id"], "check": "reporting obligation count", "expected": len(case["obligations"]),
                     "actual": len(preds), "pass": len(preds) == len(case["obligations"]), "detail": ""})
        if case["expected_excluded_min"]:
            n = len(out.get("excluded_clauses", [])) + sum(o.get("obligation_type") != "reporting" for o in out.get("obligations", []))
            rows.append({"case": case["id"], "check": "out-of-scope clauses excluded", "expected": f">={case['expected_excluded_min']}",
                         "actual": n, "pass": n >= case["expected_excluded_min"], "detail": ""})
        for gold in case["obligations"]:
            # pair by deadline value first, then by clause
            pred = next((p for p in preds if p.get("deadline_value") == gold["deadline_value"]), None) or (preds[0] if len(preds) == 1 else None)
            if pred is None:
                rows.append({"case": case["id"], "check": "obligation found", "expected": gold["clause_reference"], "actual": "missing", "pass": False, "detail": ""})
                continue
            for field in ("investor", "frequency", "deadline_value", "deadline_unit"):
                ok = _norm(pred.get(field)) == _norm(gold[field])
                if field == "investor" and gold[field] == "All Limited Partners":
                    ok = "limited partner" in (pred.get(field) or "").casefold() or "all" in (pred.get(field) or "").casefold()
                rows.append({"case": case["id"], "check": f"field: {field}", "expected": gold[field], "actual": pred.get(field), "pass": ok, "detail": ""})
            rows.append({"case": case["id"], "check": "field: clause_reference", "expected": gold["clause_reference"],
                         "actual": pred.get("clause_reference"), "pass": (_clause_num(pred.get("clause_reference")) or "").startswith(_clause_num(gold["clause_reference"]).split("(")[0]), "detail": ""})
            rows.append({"case": case["id"], "check": "quote grounded in source", "expected": "grounded",
                         "actual": pred.get("source_quote_grounding"), "pass": pred.get("source_quote_grounding") != "not_found", "detail": ""})
            rows.append({"case": case["id"], "check": "consistency warnings", "expected": "none" if not gold["expect_escalation"] else "some",
                         "actual": "; ".join(pred.get("consistency_warnings", [])) or "none",
                         "pass": bool(pred.get("consistency_warnings")) == gold["expect_escalation"], "detail": ""})
        time.sleep(float(os.getenv("EVAL_SLEEP_SECONDS", "3")))  # free-tier rate limits

    for case in GOLD["amendment_cases"]:
        text = doc_text(case["document"])
        try:
            out = extract_amendments_with_llm(text, case["document"], None, "ABC Private Equity Fund IV")
        except Exception as exc:
            rows.append({"case": case["id"], "check": "LLM call", "expected": "ok", "actual": f"error: {exc}", "pass": False, "detail": ""})
            continue
        raw[case["id"]] = out
        changes = out.get("amendment_changes", [])
        method = out.get("extraction_method")
        rows.append({"case": case["id"], "check": "extraction method", "expected": "deterministic" if case["expected_fast_path"] == "extract" else "llm",
                     "actual": method, "pass": None, "detail": "informational"})
        if not changes:
            rows.append({"case": case["id"], "check": "change detected", "expected": "1", "actual": "0", "pass": False, "detail": ""})
            continue
        c = changes[0]
        for key in ("old_value", "new_value"):
            exp = case[key]
            act = c.get(key)
            ok = ({k: act.get(k) for k in ("value", "unit")} == exp) if isinstance(exp, dict) and isinstance(act, dict) else (str(exp) in json.dumps(act).lower())
            rows.append({"case": case["id"], "check": f"field: {key}", "expected": exp, "actual": act if not isinstance(act, dict) else {k: act.get(k) for k in ('value', 'unit')}, "pass": ok, "detail": ""})
        rows.append({"case": case["id"], "check": "field: effective_date", "expected": case["effective_date"], "actual": c.get("effective_date"),
                     "pass": c.get("effective_date") == case["effective_date"], "detail": ""})
        blocked = bool(c.get("validation_errors"))
        rows.append({"case": case["id"], "check": "auto-apply blocked", "expected": str(case["expected_blocked"]), "actual": str(blocked),
                     "pass": blocked == case["expected_blocked"], "detail": "; ".join(c.get("validation_errors", []))})
        m = match_amendment_to_obligations(c, register)
        rows.append({"case": case["id"], "check": "match against seeded register", "expected": case["expected_match"], "actual": m["match_status"],
                     "pass": m["match_status"] == case["expected_match"], "detail": ""})
        if method == "llm":
            time.sleep(float(os.getenv("EVAL_SLEEP_SECONDS", "3")))
    return rows, raw


# ---------------------------------------------------------------- reporting

def to_markdown(title: str, rows: list[dict], preface: str) -> str:
    scored = [r for r in rows if r["pass"] is not None]
    passed = sum(bool(r["pass"]) for r in scored)
    lines = [f"# {title}", "", preface, "",
             f"**{passed} / {len(scored)} scored checks passed** ({len(rows) - len(scored)} informational).", "",
             "## Failures", ""]
    fails = [r for r in scored if not r["pass"]]
    if not fails:
        lines.append("None.")
    else:
        lines += ["| Case | Check | Expected | Actual | Detail |", "|---|---|---|---|---|"]
        lines += [f"| {r['case']} | {r['check']} | {r['expected']} | {r['actual']} | {r['detail']} |" for r in fails]
    lines += ["", "## All checks", "", "| Case | Check | Expected | Actual | Result |", "|---|---|---|---|---|"]
    for r in rows:
        res = "info" if r["pass"] is None else ("PASS" if r["pass"] else "FAIL")
        lines.append(f"| {r['case']} | {r['check']} | {r['expected']} | {r['actual']} | {res} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", action="store_true", help="also run live extraction with the configured OpenRouter model")
    args = parser.parse_args()
    register = seeded_register()

    rows = []
    for case in GOLD["obligation_cases"]:
        rows += obligation_offline(case)
    for case in GOLD["amendment_cases"]:
        rows += amendment_offline(case, register)
    # Cross-document precedence flags over the gold register (all gold obligations as live records).
    gold_register = [
        {**o, "id": f"{c['id']}.{i + 1}", "status": "approved",
         "document_role": "lpa" if c["document"].startswith("00_") else "side_letter"}
        for c in GOLD["obligation_cases"] for i, o in enumerate(c["obligations"])
    ]
    flagged = sorted(f["investor"] for f in find_precedence_questions(gold_register))
    rows.append({"case": "C01+C02+C10", "check": "LPA vs side-letter precedence questions flagged",
                 "expected": "LP-A, LP-I", "actual": ", ".join(flagged) or "none",
                 "pass": flagged == ["LP-A", "LP-I"], "detail": "same deliverable, different deadline; app flags, human decides"})

    md = to_markdown(
        "Offline evaluation (deterministic layer, no LLM)", rows,
        f"17 synthetic documents: {len(GOLD['obligation_cases'])} obligation cases ({sum(len(c['obligations']) for c in GOLD['obligation_cases'])} gold obligations) "
        f"and {len(GOLD['amendment_cases'])} amendment cases. Each gold record is also broken on purpose "
        "(invented deadline, paraphrased quote, re-wrapped quote, unit swap, wrong investor, tampered amendment value) "
        "to test whether the deterministic layer catches the error. Generated by `python -m eval.run_eval`.",
    )
    (OUT / "results_offline.md").write_text(md, encoding="utf-8")
    (OUT / "results_offline.json").write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    print(md.split("## All checks")[0])

    if args.llm:
        model = os.getenv("OPENROUTER_MODEL") or "(from .env)"
        llm_rows, raw = llm_eval(register)
        md = to_markdown(f"LLM evaluation", llm_rows,
                         f"Model: `{os.getenv('OPENROUTER_MODEL', model)}` via OpenRouter, temperature 0, run on {date.today().isoformat()}. "
                         "Each document extracted once. Free-tier models vary run to run; treat this as one sample, not an accuracy estimate.")
        (OUT / "results_llm.md").write_text(md, encoding="utf-8")
        (OUT / "results_llm.json").write_text(json.dumps({"rows": llm_rows, "raw_outputs": raw}, indent=2, default=str), encoding="utf-8")
        print(md.split("## All checks")[0])


if __name__ == "__main__":
    main()
