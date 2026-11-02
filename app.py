from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

# Resolve the project directory and load its .env BEFORE importing application
# modules. This is more reliable under Streamlit than relying on find_dotenv()
# to infer the current working directory. Loading first also helps modules
# that read environment variables at import time.
ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=ENV_PATH, override=True)
except ImportError:
    pass

import streamlit as st

from src.document_parser import extract_text
from src.amendments import detect_document_role, match_amendment_to_obligations
from src.extraction import extract_obligations_with_llm, extract_amendments_with_llm
from src.rules import calculate_deadline, status_for_deadline
from src.store import (
    DEFAULT_DB, add_document, add_obligation, add_fulfilment_event,
    get_document, initialize, list_documents, list_fulfilment_events,
    list_obligations, review_obligation, delete_obligation,
    add_amendment_candidate, list_amendment_candidates, review_amendment_candidate,
    list_audit_events,
)
from src.validation import validate_candidate
from src.consistency import check_obligation_consistency
from src.conflicts import find_precedence_questions
from src.demo import load_demo_data
from src.evidence import build_field_evidence, build_amendment_evidence
from src.amendment_lab import (
    DEFAULT_DB as DEFAULT_LAB_DB,
    AmendmentWorkflowError,
    initialize as initialize_lab,
    reset_scenario as reset_lab_scenario,
    get_obligations as list_lab_obligations,
    get_candidates as list_lab_candidates,
    get_audit_events as list_lab_audit_events,
    review_amendment as review_lab_amendment,
)

DB_PATH = Path(os.getenv("LTM_DB_PATH", str(DEFAULT_DB)))
if not DB_PATH.is_absolute():
    DB_PATH = (ROOT / DB_PATH).resolve()
LAB_DB_PATH = DEFAULT_LAB_DB
initialize(DB_PATH)
initialize_lab(LAB_DB_PATH, seed=True)

st.set_page_config(page_title="LP Terms Monitor v0.5", page_icon="📑", layout="wide")
st.title("📑 LP Terms Monitor · v0.5")
st.caption("Integrated AI-assisted obligation extraction, amendment review, version control and audit history")
st.info("Prototype only · Synthetic data · Not legal advice · Every extracted obligation requires authorized human review.")

with st.sidebar:
    st.header("Workspace")
    st.caption("ABC Private Equity Fund IV is a fictional demonstration fund.")
    if st.button("Load synthetic demo", use_container_width=True):
        result = load_demo_data(DB_PATH)
        st.success(f"Loaded {result['documents']} documents and {result['obligations']} obligation records." if not result.get("already_loaded") else "Demo data is already loaded.")
        st.rerun()
    if st.button("Reset and reload demo", use_container_width=True):
        result = load_demo_data(DB_PATH, reset=True)
        st.success(f"Reset complete: {result['documents']} documents, {result['obligations']} obligations.")
        st.rerun()
    st.divider()
    st.markdown("**LLM extraction configuration**")
    api_key_configured = bool((os.getenv("OPENROUTER_API_KEY") or "").strip())
    model_configured = bool((os.getenv("OPENROUTER_MODEL") or "").strip())
    if api_key_configured and model_configured:
        st.success(f"Enabled · {os.getenv('OPENROUTER_MODEL').strip()}")
    else:
        st.warning("Not configured. Set OPENROUTER_API_KEY and OPENROUTER_MODEL to enable extraction.")
        if not ENV_PATH.is_file():
            st.caption(f"No .env file found at: {ENV_PATH}")
        else:
            missing = []
            if not api_key_configured:
                missing.append("OPENROUTER_API_KEY")
            if not model_configured:
                missing.append("OPENROUTER_MODEL")
            st.caption(".env found, but these values are empty/unavailable: " + ", ".join(missing))
    st.caption("Documents and records are stored in a local SQLite database. Do not upload confidential client data.")
    st.caption(f"Main database: {DB_PATH.resolve()}")
    st.caption(f"Isolated simulator database: {LAB_DB_PATH.resolve()}")


tab_overview, tab_docs, tab_register, tab_amendments, tab_simulator, tab_fulfilment, tab_audit, tab_method = st.tabs(
    ["Overview", "Documents & extraction", "Obligation register", "Amendment review",
     "Lifecycle simulator", "Fulfilment", "Audit history", "Method & limitations"]
)

with tab_overview:
    obligations = list_obligations(DB_PATH)
    documents = list_documents(DB_PATH)
    events = list_fulfilment_events(DB_PATH)
    amendment_records = list_amendment_candidates(DB_PATH)
    audit_records = list_audit_events(DB_PATH, limit=500)
    approved = [o for o in obligations if o["status"] == "approved"]
    needs_review = [o for o in obligations if o["status"] in {"pending_review", "needs_review"}]
    superseded = [o for o in obligations if o["status"] == "superseded"]
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Documents", len(documents))
    c2.metric("Obligation records", len(obligations))
    c3.metric("Approved", len(approved))
    c4.metric("Needs review", len(needs_review))
    c5.metric("Pending amendments", len([a for a in amendment_records if a["status"] in {"pending_review", "needs_review"}]))
    c6.metric("Audit events", len(audit_records))
    st.subheader("Recent obligation records")
    if obligations:
        rows = []
        for o in obligations:
            rows.append({
                "Investor": o["investor"], "Deliverables": "; ".join(o["deliverables"]),
                "Deadline": f"{o['deadline_value']} {o['deadline_unit']}" if o["deadline_value"] is not None else "Not specified",
                "Effective from": o["effective_from"] or "Not specified", "Status": o["status"],
                "Source": o["filename"],
            })
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.info("No records yet. Load the synthetic demo or upload a document.")
    st.caption(f"Fulfilment instances recorded: {len(events)} · Superseded records retained for history: {len(superseded)}")
    if amendment_records:
        st.subheader("Recent amendment proposals")
        st.dataframe([{
            "Investor": a.get("investor") or "Unknown", "Clause": a.get("target_clause") or "Unknown",
            "Change": f"{(a.get('old_value') or {}).get('raw', 'Unknown')} → {(a.get('new_value') or {}).get('raw', 'Unknown')}",
            "Effective date": a.get("effective_date") or "Unspecified", "Match": a.get("match_status"),
            "Status": a.get("status"), "Source": a.get("filename"),
        } for a in amendment_records[:5]], use_container_width=True, hide_index=True)

with tab_docs:
    st.subheader("Upload a fund document")
    st.write("Supported formats: PDF, TXT and Markdown. The first version extracts text only; scanned PDFs need OCR and are not supported yet.")
    uploaded = st.file_uploader("Choose a document", type=["pdf", "txt", "md"])
    col1, col2 = st.columns(2)
    with col1:
        doc_type = st.selectbox("Document type", ["unknown", "lpa", "side_letter", "amendment"])
        investor_hint = st.text_input("Investor hint (optional)", placeholder="e.g. LP-A")
    with col2:
        fund_hint = st.text_input("Fund name (optional)", value="ABC Private Equity Fund IV")
        st.caption("Hints help extraction but are not treated as contractual evidence.")
    if uploaded:
        try:
            parsed_text = extract_text(uploaded.name, uploaded.getvalue())
            detected_type = detect_document_role(uploaded.name, parsed_text, doc_type)
            detected_synthetic = "SYNTHETIC TRAINING DOCUMENT" in parsed_text.upper() or "NOT A LEGAL AGREEMENT" in parsed_text.upper()
            st.caption(f"Detected document role: {detected_type} · Synthetic marker: {'Yes' if detected_synthetic else 'No'}")
            st.text_area("Extracted document text preview", parsed_text[:6000], height=180)
            if st.button("Save document and prepare for extraction", type="primary"):
                doc_id = add_document(uploaded.name, parsed_text, detected_type, investor_hint or None,
                                      fund_hint or None, detected_synthetic, DB_PATH)
                st.session_state["last_uploaded_doc_id"] = doc_id
                st.success("Document saved locally.")
                st.rerun()
        except Exception as exc:
            st.error(str(exc))

    docs = list_documents(DB_PATH)
    if docs:
        st.subheader("Saved documents")
        doc_options = {f"{d['filename']} · {d['document_type']} · {d['id'][:8]}": d["id"] for d in docs}
        chosen_label = st.selectbox("Select a saved document", list(doc_options.keys()))
        chosen_id = doc_options[chosen_label]
        chosen_doc = get_document(chosen_id, DB_PATH)
        if chosen_doc:
            selected_role = detect_document_role(chosen_doc["filename"], chosen_doc["text_content"], chosen_doc.get("document_type"))
            has_synthetic_marker = "SYNTHETIC TRAINING DOCUMENT" in chosen_doc["text_content"].upper() or "NOT A LEGAL AGREEMENT" in chosen_doc["text_content"].upper()
            synthetic_label = "Yes" if chosen_doc["is_synthetic"] or has_synthetic_marker else "No"
            st.caption(f"Uploaded: {chosen_doc['uploaded_at']} · Detected role: {selected_role} · Synthetic: {synthetic_label}")
            if st.button("Extract candidate reporting obligations with LLM", type="primary"):
                try:
                    role = detect_document_role(chosen_doc["filename"], chosen_doc["text_content"], chosen_doc.get("document_type"))
                    if role == "amendment":
                        with st.spinner("Identifying amendment changes and matching them to existing obligations..."):
                            result = extract_amendments_with_llm(
                                chosen_doc["text_content"], chosen_doc["filename"],
                                chosen_doc.get("investor_hint"), chosen_doc.get("fund_hint"),
                            )
                        enriched = []
                        current_obligations = list_obligations(DB_PATH)
                        for item in result.get("amendment_changes", []):
                            match = match_amendment_to_obligations(item, current_obligations)
                            enriched.append({**item, "match_result": match})
                        st.session_state["candidate_amendments"] = {**result, "amendment_changes": enriched}
                        st.session_state["candidate_amendment_document_id"] = chosen_id
                        st.session_state.pop("candidate_extractions", None)
                        st.success(f"Detected {len(enriched)} amendment change candidate(s).")
                    else:
                        with st.spinner("Extracting candidate obligations. The model output will still require human review."):
                            result = extract_obligations_with_llm(
                                chosen_doc["text_content"], chosen_doc["filename"],
                                chosen_doc.get("investor_hint"), chosen_doc.get("fund_hint"),
                            )
                        st.session_state["candidate_extractions"] = result
                        st.session_state["candidate_document_id"] = chosen_id
                        st.session_state.pop("candidate_amendments", None)
                        st.success(f"Received {len(result.get('obligations', []))} candidate obligation(s).")
                except Exception as exc:
                    st.error(f"Extraction failed: {exc}")
                    st.caption("Check the OpenRouter API key, model identifier, rate limit and document text.")

    amendment_results = st.session_state.get("candidate_amendments")
    amendment_doc_id = st.session_state.get("candidate_amendment_document_id")
    if amendment_results and amendment_doc_id:
        st.subheader("Review extracted amendment changes")
        st.warning("Amendment candidates are proposed changes only. No existing obligation has been modified.")
        st.caption(f"Extraction method: {amendment_results.get('extraction_method', 'LLM')} · Every candidate requires human approval.")
        for index, candidate in enumerate(amendment_results.get("amendment_changes", [])):
            with st.expander(f"Amendment {index + 1}: {candidate.get('investor') or 'Investor not identified'} · {candidate.get('target_clause') or 'Clause unknown'}", expanded=True):
                st.json({k: v for k, v in candidate.items() if k != "match_result"})
                match = candidate.get("match_result", {})
                match_status = match.get("match_status", "no_match")
                if match_status == "matched":
                    st.success("Matched to an existing approved obligation. The old record remains unchanged until human approval.")
                elif match_status == "already_applied":
                    st.info("This amendment already appears in the register. Duplicate application is blocked.")
                else:
                    st.warning(match.get("match_note", "Manual matching is required."))
                for note in candidate.get("review_notes") or []:
                    st.info("Reviewer note: " + note)
                if candidate.get("validation_errors"):
                    st.error("Validation errors: " + " · ".join(candidate["validation_errors"]))
                    if candidate.get("source_quote_grounding") == "not_found":
                        st.caption("Saving is disabled: the source quotation could not be located in the document.")
                    else:
                        st.caption("This candidate can be saved only as needs_review. The approval step will refuse to apply it automatically.")
                elif candidate.get("source_quote_grounding") == "normalized_whitespace_or_typography":
                    st.caption("The model flattened whitespace or typographic characters. The application restored the exact original source span before validation.")
                quote_ungrounded = candidate.get("source_quote_grounding") == "not_found"
                save_label = ("Save amendment candidate for manual handling (needs_review, cannot be auto-applied)"
                              if candidate.get("validation_errors") else "Save amendment candidate for human review")
                if st.button(
                    save_label,
                    key=f"save_amendment_{index}",
                    disabled=quote_ungrounded,
                ):
                    add_amendment_candidate(amendment_doc_id, candidate, match, DB_PATH)
                    st.success("Amendment candidate saved. Existing obligations were not changed.")
                    st.rerun()
        if amendment_results.get("excluded_clauses"):
            st.subheader("Other clauses excluded from amendment extraction")
            st.json(amendment_results["excluded_clauses"])

    candidates = st.session_state.get("candidate_extractions")
    candidate_doc_id = st.session_state.get("candidate_document_id")
    if candidates and candidate_doc_id:
        st.subheader("Review extracted candidates")
        st.warning("These are AI-generated candidates, not approved contractual interpretations.")
        for index, candidate in enumerate(candidates.get("obligations", [])):
            with st.expander(f"Candidate {index + 1}: {candidate.get('investor') or 'Investor not identified'}", expanded=True):
                doc = get_document(candidate_doc_id, DB_PATH)
                errors = validate_candidate(candidate, doc["text_content"] if doc else "")
                candidate["requires_human_review"] = True
                if not candidate.get("review_reason"):
                    candidate["review_reason"] = "Mandatory human verification: confirm clause, investor, deliverables, deadline, effective date and interactions with other fund documents."
                candidate["validation_errors"] = errors
                consistency = check_obligation_consistency(candidate)
                candidate["consistency_warnings"] = consistency
                if doc:
                    candidate["field_evidence"] = build_field_evidence(doc["text_content"], candidate)
                st.json(candidate)
                if errors:
                    st.error("Validation errors: " + " · ".join(errors))
                    st.caption("Saving is disabled until required fields and source evidence pass validation.")
                elif candidate.get("source_quote_grounding") == "normalized_whitespace_or_typography":
                    st.caption("The model normalized whitespace or typographic characters; the application restored the exact original source span.")
                if consistency:
                    st.error("Value/quote consistency: " + " · ".join(consistency))
                    st.caption("This candidate will be saved as needs_review (escalated), not pending_review. No due dates should be relied on until a reviewer corrects or confirms the values.")
                st.warning(candidate["review_reason"])
                save_status = "needs_review" if consistency else "pending_review"
                if st.button(
                    f"Add candidate to register as {save_status}",
                    key=f"save_candidate_{index}",
                    disabled=bool(errors),
                ):
                    if consistency:
                        candidate["review_reason"] = (candidate.get("review_reason") or "") + " | Escalated: " + "; ".join(consistency)
                    add_obligation(candidate_doc_id, candidate, save_status, DB_PATH)
                    st.success(f"Candidate saved as {save_status}. It is not approved.")
                    st.rerun()
        if candidates.get("excluded_clauses"):
            st.subheader("Clauses excluded from reporting MVP")
            st.json(candidates["excluded_clauses"])

with tab_register:
    st.subheader("Obligation register")
    obligations = list_obligations(DB_PATH)
    if not obligations:
        st.info("Load the synthetic demo or extract a document first.")
    else:
        precedence = find_precedence_questions(obligations)
        if precedence:
            with st.expander(f"Possible precedence questions ({len(precedence)}) — human decision required", expanded=False):
                st.caption("The LPA and a side letter set different deadlines for the same deliverable. The app does not decide which controls.")
                st.dataframe([{k: v for k, v in p.items() if not k.endswith("_id")} for p in precedence],
                             use_container_width=True, hide_index=True)
        statuses = sorted({o["status"] for o in obligations})
        selected_statuses = st.multiselect("Filter by status", statuses, default=statuses)
        filtered = [o for o in obligations if o["status"] in selected_statuses]
        table = [{
            "Investor": o["investor"], "Type": o["obligation_type"],
            "Deliverables": "; ".join(o["deliverables"]), "Frequency": o["frequency"],
            "Deadline": f"{o['deadline_value']} {o['deadline_unit']}" if o["deadline_value"] is not None else "Unspecified",
            "Effective from": o["effective_from"] or "Unspecified", "Status": o["status"],
            "Source": o["filename"],
        } for o in filtered]
        st.dataframe(table, use_container_width=True, hide_index=True)
        option_map = {f"{o['investor']} · {o['filename']} · {o['id'][:8]}": o for o in filtered}
        selected = st.selectbox("Inspect an obligation", list(option_map.keys()))
        obligation = option_map[selected]
        st.markdown("**Exact source quote**")
        st.code(obligation["source_quote"] or "No source quote stored")
        source_doc = get_document(obligation["document_id"], DB_PATH)
        if source_doc:
            with st.expander("Field-level source evidence", expanded=False):
                evidence = build_field_evidence(source_doc["text_content"], obligation)
                st.metric("Fields located literally in source", evidence["verified_field_evidence_count"])
                st.caption(f"Traceability: {evidence['summary']} · {evidence['unlocated_field_count']} field(s) could not be located by exact phrase matching.")
                st.json(evidence["fields"])
        st.write({
            "Clause reference": obligation["clause_reference"],
            "Document role": obligation.get("document_role"),
            "Potential amendment note": obligation.get("potential_amendment_note"),
            "AI requests human review": bool(obligation.get("requires_human_review", 1)),
            "Responsible party": obligation["responsible_party"],
            "Conditions": obligation["conditions"],
            "Review reason": obligation["review_reason"],
            "Validation errors": obligation["validation_errors"],
        })
        st.markdown("**Human review**")
        reviewer = st.text_input("Reviewer identifier", value="demo-reviewer", key="reviewer_id")
        review_note = st.text_area("Review note", key="review_note")
        c1, c2, c3 = st.columns(3)
        if c1.button("Approve", disabled=bool(obligation["validation_errors"]) or obligation["status"] == "superseded"):
            review_obligation(obligation["id"], "approved", reviewer, review_note, DB_PATH)
            st.success("Approved by the named demo reviewer. This is a prototype record, not legal advice.")
            st.rerun()
        if c2.button("Needs review"):
            review_obligation(obligation["id"], "needs_review", reviewer, review_note, DB_PATH)
            st.rerun()
        if c3.button("Reject"):
            review_obligation(obligation["id"], "rejected", reviewer, review_note, DB_PATH)
            st.rerun()

        st.divider()
        with st.expander("Danger zone — delete an accidental candidate"):
            st.caption(
                "Permanent deletion is available only for pending-review, needs-review "
                "or rejected records with no fulfilment history. Approved and superseded "
                "records are protected to preserve the audit trail."
            )
            deletable = [
                o for o in obligations
                if o["status"] in {"pending_review", "needs_review", "rejected"}
            ]
            if not deletable:
                st.info("There are no unapproved records available for deletion.")
            else:
                delete_options = {
                    f"{o['investor']} · {o['filename']} · {o['id'][:8]} · {o['status']}": o
                    for o in deletable
                }
                delete_label = st.selectbox(
                    "Select the exact record to delete",
                    list(delete_options.keys()),
                    key="delete_obligation_select",
                )
                delete_target = delete_options[delete_label]
                st.warning(
                    "This permanently removes the selected obligation record. "
                    "The source document will remain saved."
                )
                confirm_delete = st.checkbox(
                    "I understand this deletion is permanent.",
                    key="confirm_delete_obligation",
                )
                if st.button(
                    "Permanently delete selected obligation",
                    type="secondary",
                    disabled=not confirm_delete,
                    key="delete_obligation_button",
                ):
                    try:
                        delete_obligation(delete_target["id"], DB_PATH)
                        st.success("Obligation record deleted. The source document was retained.")
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))

with tab_amendments:
    st.subheader("Integrated amendment review")
    st.caption("An amendment is a proposed change, not an automatic update. The system matches the affected investor/clause/value; an authorized reviewer must approve before a new obligation version is created.")
    amendment_records = list_amendment_candidates(DB_PATH)
    if not amendment_records:
        st.info("No amendment candidates have been saved yet. Upload an amendment in Documents & extraction and save its candidate for review.")
    else:
        st.dataframe([{
            "Investor": a.get("investor") or "Unknown",
            "Target clause": a.get("target_clause") or "Unknown",
            "Change": f"{(a.get('old_value') or {}).get('raw', 'Unknown')} → {(a.get('new_value') or {}).get('raw', 'Unknown')}",
            "Effective date": a.get("effective_date") or "Unspecified",
            "Match": a.get("match_status"), "Status": a.get("status"),
            "Source": a.get("filename"),
        } for a in amendment_records], use_container_width=True, hide_index=True)
        amendment_options = {
            f"{a.get('investor') or 'Unknown'} · {a.get('target_clause') or 'Clause?'} · {a['id'][:8]} · {a.get('status')}": a
            for a in amendment_records
        }
        selected_amendment = st.selectbox("Inspect amendment candidate", list(amendment_options.keys()), key="integrated_amendment_select")
        amendment = amendment_options[selected_amendment]
        old_value = amendment.get("old_value") or {}
        new_value = amendment.get("new_value") or {}

        st.markdown("#### Proposed contractual change")
        old_col, new_col, date_col = st.columns(3)
        old_col.metric("Existing value", f"{old_value.get('value', '—')} {old_value.get('unit', '')}")
        new_col.metric("Proposed value", f"{new_value.get('value', '—')} {new_value.get('unit', '')}")
        date_col.metric("Effective date", amendment.get("effective_date") or "Unspecified")
        st.write({
            "Investor": amendment.get("investor"), "Fund": amendment.get("fund"),
            "Target document": amendment.get("target_document"), "Target clause": amendment.get("target_clause"),
            "Change type": amendment.get("change_type"), "Changed field": amendment.get("field_name"),
            "Unchanged terms stated": amendment.get("unchanged_terms") or [],
            "Extraction method": amendment.get("extraction_method"),
        })

        st.markdown("#### Match against the current obligation")
        st.info(amendment.get("match_note") or "No match note available.")
        obligations_now = list_obligations(DB_PATH)
        matched = next((o for o in obligations_now if o["id"] == amendment.get("matched_obligation_id")), None)
        active = next((o for o in obligations_now if o["id"] == amendment.get("active_obligation_id")), None)
        if matched:
            st.dataframe([{
                "Investor": matched["investor"], "Clause": matched.get("clause_reference"),
                "Current deadline": f"{matched['deadline_value']} {matched['deadline_unit']}" if matched["deadline_value"] is not None else "Unspecified",
                "Status": matched["status"], "Source": matched["filename"],
                "Effective from": matched.get("effective_from") or "Unspecified",
            }], use_container_width=True, hide_index=True)
        if active:
            st.success(f"Already reflected by approved version: {active['deadline_value']} {active['deadline_unit']} from {active['filename']}. Duplicate application is blocked.")

        st.markdown("#### Compare the source evidence")
        amendment_doc = get_document(amendment["document_id"], DB_PATH)
        if amendment_doc:
            amendment_evidence = build_amendment_evidence(amendment_doc["text_content"], amendment)
            with st.expander("Amendment source — exact extracted evidence", expanded=True):
                st.code(amendment.get("source_quote") or "No amendment source quote stored")
                if amendment.get("source_quote_grounding") == "normalized_whitespace_or_typography":
                    st.caption("Quote was grounded after whitespace/typography normalization; the stored quote is the exact raw source span.")
                st.caption(f"Field-level source lookup: {amendment_evidence['summary']} · {amendment_evidence['verified_field_evidence_count']} field(s) located.")
                st.json(amendment_evidence["fields"])
        if matched:
            original_doc = get_document(matched["document_id"], DB_PATH)
            with st.expander("Original agreement — matched obligation evidence", expanded=True):
                st.code(matched.get("source_quote") or "No original source quote stored")
                if original_doc:
                    st.caption(f"Original document: {original_doc['filename']}")
                    st.text_area("Original document text", original_doc["text_content"], height=170, disabled=True, key=f"original_amendment_doc_{amendment['id']}")
        if amendment_doc:
            with st.expander("Full amendment document text", expanded=False):
                st.text_area("Amendment document text", amendment_doc["text_content"], height=170, disabled=True, key=f"full_amendment_doc_{amendment['id']}")

        if amendment.get("validation_errors"):
            st.error("Validation errors: " + " · ".join(amendment["validation_errors"]))
        st.markdown("#### Authorized human decision")
        amendment_reviewer = st.text_input("Reviewer identifier", value="demo-reviewer", key=f"amendment_reviewer_{amendment['id']}")
        amendment_note = st.text_area("Review note (required for approval or rejection)", key=f"amendment_review_note_{amendment['id']}")
        confirm_review = st.checkbox(
            "I checked the investor, target clause, original obligation, amendment quotation and effective date against the source documents.",
            key=f"amendment_confirm_{amendment['id']}",
        )
        can_review = amendment.get("status") in {"pending_review", "needs_review"}
        can_apply = (
            can_review and amendment.get("match_status") == "matched"
            and not amendment.get("validation_errors") and bool(amendment.get("effective_date"))
            and amendment.get("field_name") == "deadline"
        )
        review_inputs_ok = bool(amendment_reviewer.strip() and amendment_note.strip() and confirm_review)
        approve_col, reject_col, escalate_col = st.columns(3)
        if approve_col.button("Approve and create new version", type="primary", disabled=not (can_apply and review_inputs_ok), key=f"approve_amendment_{amendment['id']}"):
            try:
                new_obligation_id = review_amendment_candidate(
                    amendment["id"], "approved", amendment_reviewer, amendment_note, DB_PATH
                )
                st.success(f"Amendment approved. New obligation version created: {new_obligation_id[:12]}")
                st.rerun()
            except Exception as exc:
                st.error(f"Amendment was not applied: {exc}")
        if reject_col.button("Reject amendment", disabled=not (can_review and review_inputs_ok), key=f"reject_amendment_{amendment['id']}"):
            try:
                review_amendment_candidate(amendment["id"], "rejected", amendment_reviewer, amendment_note, DB_PATH)
                st.warning("Amendment rejected. The original obligation remains unchanged.")
                st.rerun()
            except Exception as exc:
                st.error(f"Could not reject amendment: {exc}")
        if escalate_col.button("Keep in needs-review", disabled=not (can_review and review_inputs_ok), key=f"escalate_amendment_{amendment['id']}"):
            try:
                review_amendment_candidate(amendment["id"], "needs_review", amendment_reviewer, amendment_note, DB_PATH)
                st.info("Candidate retained for further review. Existing obligations were not changed.")
                st.rerun()
            except Exception as exc:
                st.error(f"Could not update review status: {exc}")

        if amendment.get("status") == "already_applied":
            st.info("This amendment is already represented by an approved version. Duplicate application is blocked.")
        elif amendment.get("status") == "approved":
            st.success(f"Approved. Resulting obligation version: {amendment.get('resulting_obligation_id') or 'recorded'}")
        elif amendment.get("status") == "rejected":
            st.warning("Rejected. The matched original obligation remains active.")
        elif amendment.get("match_status") != "matched":
            st.warning("Approval is disabled until the system identifies exactly one eligible approved original obligation. Resolve the match manually first.")

with tab_simulator:
    st.subheader("Isolated amendment lifecycle simulator")
    st.caption("Same application, separate SQLite database. Use this sandbox to demonstrate approval and rejection without changing the main fund register.")
    lab_obligations = list_lab_obligations(LAB_DB_PATH)
    lab_candidates = list_lab_candidates(LAB_DB_PATH)
    lab_events = list_lab_audit_events(LAB_DB_PATH)
    lab_active = [o for o in lab_obligations if o["status"] == "approved"]
    lab_superseded = [o for o in lab_obligations if o["status"] == "superseded"]
    lab_pending = [c for c in lab_candidates if c["status"] == "pending_review"]
    lm1, lm2, lm3, lm4 = st.columns(4)
    lm1.metric("Active obligations", len(lab_active))
    lm2.metric("Superseded versions", len(lab_superseded))
    lm3.metric("Pending amendments", len(lab_pending))
    lm4.metric("Audit events", len(lab_events))
    st.info(f"Sandbox database: {LAB_DB_PATH.resolve()} · Main database is not used by this simulator.")
    lab_scenario_tab, lab_review_tab, lab_history_tab = st.tabs(["Scenario & state", "Human review", "Audit history"])
    with lab_scenario_tab:
        st.markdown("**Test scenario: LP-A deadline amendment**")
        st.write("The original signed side letter requires quarterly financial statements and a portfolio exposure schedule within 45 calendar days after each fiscal quarter-end. Amendment 01 proposes replacing the deadline with 30 calendar days, effective 1 January 2026.")
        st.markdown("**Current obligation versions**")
        st.dataframe([{
            "ID": o["id"], "Investor": o["investor"], "Clause": o["clause_reference"],
            "Deliverables": "; ".join(o["deliverables"]),
            "Deadline": f"{o['deadline_value']} {o['deadline_unit']}",
            "Effective from": o["effective_from"], "Version": o["version"], "Status": o["status"],
            "Previous version": o.get("previous_version_id") or "—",
        } for o in sorted(lab_obligations, key=lambda item: item["version"])], use_container_width=True, hide_index=True)
        lab_candidate = lab_candidates[0] if lab_candidates else None
        if lab_candidate:
            st.markdown("**Proposed amendment**")
            st.write({"Investor": lab_candidate["investor"], "Target clause": lab_candidate["target_clause"],
                      "Old value": f"{lab_candidate['old_value']} {lab_candidate['old_unit']}",
                      "New value": f"{lab_candidate['new_value']} {lab_candidate['new_unit']}",
                      "Effective date": lab_candidate["effective_date"], "Status": lab_candidate["status"]})
            with st.expander("Inspect synthetic amendment source evidence"):
                st.code(lab_candidate["source_quote"])
        st.caption("This single-scenario simulator is a workflow test harness, not a full legal amendment interpretation engine.")
        with st.expander("Reset isolated simulator scenario"):
            st.warning("This resets only the isolated simulator database. It does not change the main obligation register.")
            reset_confirm = st.checkbox("I understand this resets only the isolated simulator.", key="integrated_lab_reset_confirm")
            if st.button("Reset simulator to 45-day baseline", disabled=not reset_confirm, key="integrated_lab_reset_button"):
                reset_lab_scenario(LAB_DB_PATH)
                st.success("Isolated simulator reset. The original 45-day obligation and pending 30-day proposal are restored.")
                st.rerun()
    with lab_review_tab:
        lab_candidate = next((c for c in lab_candidates if c["status"] == "pending_review"), None)
        if not lab_candidate:
            st.info("No pending simulator amendment. Reset the isolated scenario to run another approval/rejection branch.")
        else:
            st.markdown("**Human review decision**")
            st.write({"Investor": lab_candidate["investor"], "Clause": lab_candidate["target_clause"],
                      "Current deadline": f"{lab_candidate['old_value']} {lab_candidate['old_unit']}",
                      "Proposed deadline": f"{lab_candidate['new_value']} {lab_candidate['new_unit']}",
                      "Effective date": lab_candidate["effective_date"]})
            with st.expander("Source evidence", expanded=True):
                st.code(lab_candidate["source_quote"])
                st.caption("Compare this quote with the executed amendment before approving in a real workflow.")
            lab_reviewer = st.text_input("Simulator reviewer identifier", value="placement-demo-reviewer", key="lab_reviewer")
            lab_note = st.text_area("Simulator review note (required)", key="lab_review_note")
            lab_c1, lab_c2 = st.columns(2)
            approve_lab_confirm = lab_c1.checkbox("I approve the proposed 30-day deadline.", key="lab_approve_confirm")
            reject_lab_confirm = lab_c2.checkbox("I reject this proposed amendment.", key="lab_reject_confirm")
            note_and_reviewer_ok = bool(lab_reviewer.strip() and lab_note.strip())
            la1, la2 = st.columns(2)
            if la1.button("Approve and create version 2", type="primary", disabled=not (approve_lab_confirm and note_and_reviewer_ok), key="lab_approve_button"):
                try:
                    new_id = review_lab_amendment(lab_candidate["id"], "approved", lab_reviewer, lab_note, LAB_DB_PATH)
                    st.success(f"Approved. New version created: {new_id}")
                    st.rerun()
                except AmendmentWorkflowError as exc:
                    st.error(str(exc))
            if la2.button("Reject and retain version 1", disabled=not (reject_lab_confirm and note_and_reviewer_ok), key="lab_reject_button"):
                try:
                    review_lab_amendment(lab_candidate["id"], "rejected", lab_reviewer, lab_note, LAB_DB_PATH)
                    st.warning("Rejected. The original 45-day obligation remains approved.")
                    st.rerun()
                except AmendmentWorkflowError as exc:
                    st.error(str(exc))
    with lab_history_tab:
        if lab_events:
            st.dataframe([{
                "Event": e["id"], "Time (UTC)": e["event_at"], "Entity": e["entity_type"],
                "Entity ID": e["entity_id"], "Action": e["action"], "Actor": e["actor"],
                "Details": json.dumps(e["details"], sort_keys=True),
            } for e in lab_events], use_container_width=True, hide_index=True)
        else:
            st.info("No simulator audit events recorded yet.")

with tab_fulfilment:
    st.subheader("Create a reporting-period instance")
    approved = [o for o in list_obligations(DB_PATH) if o["status"] == "approved" and o["obligation_type"] == "reporting"]
    if not approved:
        st.info("Approve at least one reporting obligation before creating a fulfilment instance.")
    else:
        option_map = {f"{o['investor']} · {'; '.join(o['deliverables'])} · {o['deadline_value']} {o['deadline_unit']}": o for o in approved}
        chosen = st.selectbox("Approved obligation", list(option_map.keys()))
        obligation = option_map[chosen]
        period_end = st.date_input("Reporting period end", value=date(2027, 3, 31))
        if obligation["deadline_value"] is None or obligation["deadline_unit"] == "unspecified":
            st.warning("No explicit numeric deadline is available. This instance must be handled manually; no date will be calculated.")
            deadline = None
        else:
            try:
                deadline = calculate_deadline(period_end, obligation["deadline_value"], obligation["deadline_unit"])
                st.metric("Calculated deadline", deadline.isoformat(), status_for_deadline(deadline))
                if obligation["deadline_unit"] == "business_days":
                    st.caption("Prototype calculation excludes weekends only; configure the applicable holiday calendar before real use.")
            except ValueError as exc:
                st.error(str(exc))
                deadline = None
        state = st.selectbox("Fulfilment state", ["not_started", "in_progress", "fulfilled", "overdue", "requires_review"])
        delivered_at = st.date_input("Delivery date (optional)", value=None) if state == "fulfilled" else None
        evidence = st.text_input("Evidence reference (e.g. secure portal receipt ID or internal file reference)")
        if st.button("Save fulfilment instance", type="primary"):
            add_fulfilment_event(
                obligation["id"], period_end.isoformat(), deadline.isoformat() if deadline else None,
                state, delivered_at.isoformat() if delivered_at else None, evidence or None, DB_PATH,
            )
            st.success("Fulfilment instance saved.")
            st.rerun()
    events = list_fulfilment_events(DB_PATH)
    st.subheader("Fulfilment history")
    if events:
        st.dataframe([{
            "Investor": e["investor"], "Deliverables": "; ".join(e["deliverables"]),
            "Period end": e["reporting_period_end"], "Deadline": e["deadline_date"] or "Manual review",
            "State": e["state"], "Delivered at": e["delivered_at"] or "—", "Evidence": e["evidence_reference"] or "—",
        } for e in events], use_container_width=True, hide_index=True)
    else:
        st.caption("No fulfilment instances recorded yet.")

with tab_audit:
    st.subheader("Application audit history")
    st.caption("Application-level append-only event history for documents, obligation reviews, amendment proposals, version changes, fulfilment events and permitted deletions. This local SQLite log is not a tamper-proof or regulator-certified ledger.")
    audit_records = list_audit_events(DB_PATH, limit=1000)
    if not audit_records:
        st.info("No audit events recorded yet. Save a document, review an obligation or process an amendment to create events.")
    else:
        event_actions = sorted({event["action"] for event in audit_records})
        selected_actions = st.multiselect("Filter by action", event_actions, default=event_actions, key="audit_action_filter")
        filtered_audit = [event for event in audit_records if event["action"] in selected_actions]
        st.dataframe([{
            "Time (UTC)": event["event_at"], "Entity": event["entity_type"],
            "Entity ID": event["entity_id"], "Action": event["action"],
            "Actor": event["actor"], "Details": json.dumps(event["details"], sort_keys=True),
        } for event in filtered_audit], use_container_width=True, hide_index=True)
        if filtered_audit:
            selected_event = st.selectbox(
                "Inspect an audit event",
                [f"{e['event_at']} · {e['action']} · {e['id'][:8]}" for e in filtered_audit],
                key="audit_event_inspector",
            )
            event_index = [f"{e['event_at']} · {e['action']} · {e['id'][:8]}" for e in filtered_audit].index(selected_event)
            st.json(filtered_audit[event_index])

with tab_method:
    st.subheader("How the prototype separates responsibilities")
    st.markdown("""
    | Component | Responsibility | What it must not do |
    |---|---|---|
    | LLM | Find candidate clauses and extract fields with exact quotes | Decide legal enforceability or supersession |
    | Source validator | Check required fields and exact quote substring | Prove the interpretation is legally correct |
    | Python rules | Calculate explicit calendar/business-day offsets | Infer ambiguous contractual deadlines |
    | Human reviewer | Approve, reject or escalate candidate records | Be bypassed for ambiguous or conflicting clauses |
    | SQLite | Retain documents, obligations, reviews, fulfilment and application audit events | Be treated as production-grade secure storage or tamper-proof audit infrastructure |
    | Amendment simulator | Demonstrate the full approval/rejection lifecycle in a separate database | Modify the main fund register |

    **Known limitations**
    - Scanned PDFs and OCR are not supported in this version.
    - LLM extraction is optional and requires an OpenRouter key plus a model identifier configured by the user.
    - Extraction quality has not yet been benchmarked; do not interpret this demo as a measured accuracy claim.
    - Business-day calculations exclude weekends only, not public holidays.
    - Explicit deadline replacements in amendments use a narrow deterministic parser; more complex amendments use an LLM extraction fallback.
    - Amendment candidates must match an approved old obligation and receive human approval before a new version is created and the old version is superseded.
    - The amendment workflow currently supports deadline changes only; other change types are flagged but cannot be applied automatically.
    - The local SQLite database is not encrypted and is not suitable for confidential client documents.
    - No emails, reminders or external integrations are sent.
    - Field-level evidence uses literal source-text lookup; it supports traceability but does not prove legal interpretation.
    - The integrated simulator uses a separate local database at `data/amendment_lifecycle_lab.sqlite3` and is intentionally limited to a single synthetic deadline replacement.
    """)
    st.subheader("Suggested next evaluation")
    st.write("Use the included synthetic document pack and compare extracted fields with the manually curated records in data/demo_manifest.json. Report field-level precision/recall, exact deadline accuracy, amendment-change detection, quote validity and correct escalation of ambiguous cases.")
