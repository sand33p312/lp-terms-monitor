"""Load the fictional demo fund and its human-curated synthetic gold-standard records."""
from __future__ import annotations
import json
from pathlib import Path
from .store import initialize, add_document, add_obligation, list_documents, list_obligations, DEFAULT_DB

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DOCS = DATA / "synthetic_docs"
MANIFEST = DATA / "demo_manifest.json"


def load_demo_data(db_path=DEFAULT_DB, reset: bool = False) -> dict[str, int]:
    from .store import clear_database
    initialize(db_path)
    if reset:
        clear_database(db_path)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    existing_docs = {d["filename"]: d["id"] for d in list_documents(db_path)}
    document_ids: dict[str, str] = dict(existing_docs)
    added_documents = 0
    for doc in manifest["documents"]:
        if doc["filename"] in document_ids:
            continue
        path = DOCS / doc["filename"]
        text = path.read_text(encoding="utf-8")
        doc_id = add_document(
            filename=doc["filename"], text_content=text, document_type=doc["document_type"],
            investor_hint=doc.get("investor_hint"), fund_hint="ABC Private Equity Fund IV",
            is_synthetic=True, db_path=db_path,
        )
        document_ids[doc["filename"]] = doc_id
        added_documents += 1

    existing_records = {(o["filename"], o["investor"], o["source_quote"]) for o in list_obligations(db_path)}
    added_obligations = 0
    document_types = {item["filename"]: item["document_type"] for item in manifest["documents"]}
    for record in manifest["obligations"]:
        candidate = dict(record["candidate"])
        candidate.setdefault("document_role", document_types.get(record["document"], "unknown"))
        candidate.setdefault("requires_human_review", record["status"] != "approved")
        key = (record["document"], candidate.get("investor") or "Unknown investor", candidate.get("source_quote", ""))
        if key in existing_records:
            continue
        add_obligation(document_ids[record["document"]], candidate,
                       status=record["status"], db_path=db_path)
        added_obligations += 1
    return {
        "documents": len([d for d in manifest["documents"] if d["filename"] in document_ids]),
        "obligations": len(list_obligations(db_path)),
        "added_documents": added_documents,
        "added_obligations": added_obligations,
        "already_loaded": int(added_documents == 0 and added_obligations == 0),
    }
