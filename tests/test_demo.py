import json
from pathlib import Path

from src.demo import load_demo_data, DATA, DOCS
from src.store import list_documents, list_obligations


def test_demo_seed_loads_expected_records(tmp_path):
    db_path = tmp_path / "test.db"
    result = load_demo_data(db_path)
    assert result["documents"] == 17
    assert result["obligations"] == 7
    docs = list_documents(db_path)
    obligations = list_obligations(db_path)
    assert len(docs) == 17
    assert len(obligations) == 7


def test_demo_quotes_exist_in_source_files():
    manifest = json.loads((DATA / "demo_manifest.json").read_text(encoding="utf-8"))
    for record in manifest["obligations"]:
        source = (DOCS / record["document"]).read_text(encoding="utf-8")
        assert record["candidate"]["source_quote"] in source


def test_demo_seed_is_idempotent(tmp_path):
    db_path = tmp_path / "idempotent.db"
    first = load_demo_data(db_path)
    second = load_demo_data(db_path)
    assert first["documents"] == second["documents"] == 17
    assert first["obligations"] == second["obligations"] == 7
    assert second["already_loaded"] == 1
