from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.agent.workflow import _reviewer
from app.settings import Settings
from app.storage.database import Database
from app.system.compose_parser import parse_compose
from app.system.graph import role_for
from app.system.inventory import SystemOnboarding
from app.tools.registry import sanitize_payload

ROOT = Path(__file__).resolve().parents[1]


def test_both_existing_demos_and_equivalent_snapshots(tmp_path):
    db = Database(tmp_path / "runtime.sqlite")
    db.initialize()
    settings = Settings(repository_roots=str(ROOT / "demo"))
    service = SystemOnboarding(db, settings)
    for key in ("system-a", "system-b"):
        result = service.create_snapshot(key, key, "compose.yaml")
        with db.transaction() as tx:
            assert (
                tx.execute(
                    "SELECT COUNT(*) FROM graph_nodes WHERE snapshot_id=? AND role='application'",
                    (result["snapshot_id"],),
                ).fetchone()[0]
                == 1
            )
        again = service.create_snapshot(key, key, "compose.yaml")
        assert again["system_id"] == result["system_id"]
        assert again["version"] == 2


def test_more_than_eight_services(tmp_path):
    path = tmp_path / "compose.yaml"
    path.write_text("services:\n" + "".join(f"  s{i}: {{image: python}}\n" for i in range(12)))
    assert len(parse_compose(path)[0]["services"]) == 12
    assert role_for("unrelated-db-name", {"image": "python:3"}) == "application"
    assert role_for("database", {}) == "unknown"


def test_recursive_secrets():
    assert (
        sanitize_payload({"DATABASE_PASSWORD": "private value with spaces"})["DATABASE_PASSWORD"]
        == "[REDACTED]"
    )


def gate_state():
    now = datetime.now(timezone.utc)
    node = {"id": "app", "name": "app", "kind": "service", "role": "application"}
    evidence = [
        {
            "evidence_id": "a",
            "call_id": "c1",
            "tool_id": "container.logs",
            "target_node_id": "app",
            "observed_at": now.isoformat(),
            "payload": {"text": now.isoformat() + " startup failed: connection refused"},
        },
        {
            "evidence_id": "b",
            "call_id": "c2",
            "tool_id": "compose.status",
            "observed_at": now.isoformat(),
            "payload": {"containers": [{"service": "app", "state": "exited"}]},
        },
    ]
    hyp = {
        "target_node_id": "app",
        "target_role": "application",
        "failure_mode": "startup_failure",
        "prediction": "Application must be exited",
        "causal_chain": ["dependency refused", "startup exits"],
        "rationale": "The startup failure causes application exit and the unavailable response.",
        "evidence_links": [
            {
                "evidence_id": x,
                "relation": "supports",
                "explanation": "Current application failure observation.",
            }
            for x in ("a", "b")
        ],
    }
    return {
        "incident": {"window_start": (now - timedelta(minutes=1)).isoformat()},
        "graph": {"nodes": [node]},
        "evidence": evidence,
        "report": {"hypotheses": [hyp]},
    }


def test_evidence_gate_positive_and_duplicates():
    state = gate_state()
    assert _reviewer(state)["gate"]["status"] == "supported"
    state["evidence"][1] = {**state["evidence"][0], "evidence_id": "b", "call_id": "c2"}
    assert _reviewer(state)["gate"]["status"] == "unresolved"


def test_gate_rejects_stale_refutation_and_offline():
    state = gate_state()
    state["report"]["analysis_status"] = "offline"
    assert _reviewer(state)["gate"]["status"] == "unresolved"
    state = gate_state()
    state["report"]["hypotheses"][0]["evidence_links"][1]["relation"] = "refutes"
    assert _reviewer(state)["gate"]["status"] == "unresolved"
    state = gate_state()
    state["evidence"][1]["observed_at"] = "2000-01-01T00:00:00+00:00"
    assert _reviewer(state)["gate"]["status"] == "unresolved"
