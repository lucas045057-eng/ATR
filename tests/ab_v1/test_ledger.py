import json
from pathlib import Path

import pytest

from scripts.ab_acceptance import record_case


def case(tmp_path):
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    return dict(case_id="V09", subcase="offset", SHA="e269d97", environment="isolated",
                model_fingerprint=None, run_id=None, expected="valid UTC", actual="valid UTC",
                verdict="PASS", evidence_path=str(evidence), failure_reason=None)


def test_ledger_preserves_first_failure_and_rerun(tmp_path):
    record = case(tmp_path)
    destination = tmp_path / "ledger.jsonl"
    record_case({**record, "verdict": "FAIL", "failure_reason": "parse error"}, destination)
    record_case(record, destination)
    rows = [json.loads(line) for line in destination.read_text().splitlines()]
    assert [row["verdict"] for row in rows] == ["FAIL", "PASS"]


@pytest.mark.parametrize("change", [
    {"verdict": "SKIP"}, {"evidence_path": "does-not-exist"},
    {"api_key": "secret"}, {"actual": "password=synthetic-secret"},
])
def test_ledger_rejects_invalid_or_sensitive_records(tmp_path, change):
    with pytest.raises(ValueError):
        record_case({**case(tmp_path), **change}, tmp_path / "ledger.jsonl")
    assert not (tmp_path / "ledger.jsonl").exists()


def test_record_requires_source_revision(tmp_path):
    record = case(tmp_path)
    del record["SHA"]
    with pytest.raises(ValueError):
        record_case(record, Path(tmp_path / "ledger.jsonl"))
